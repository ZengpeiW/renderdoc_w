"""Independent bounded GetUsage metadata reference in an owned GUI (Python 3.6).

This helper never calls SetFrameEvent. The installed observer rejects event moves and
records hashes of the actual native usage lists consumed by the production tool.
"""

import hashlib
import json
import threading
import time
import traceback

from gpu_counters_native import _fatal, _pixels, _write


MAX_RESOURCES = 10000
MAX_RECORDS = 1000000
MAX_BYTES = 64 * 1024 * 1024


def _usage(values):
    rows = []
    for value in values:
        row = {"eventId": int(value.eventId), "usage": str(value.usage).split(".")[-1],
               "usageValue": int(value.usage)}
        view = getattr(value, "viewId", getattr(value, "view", None))
        if view is not None and int(view):
            row["viewId"] = str(int(view))
        rows.append(row)
    return rows


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _state(controller, rd):
    pipe = controller.GetPipelineState()
    shaders = {}
    for name in ("Vertex", "Hull", "Domain", "Geometry", "Pixel", "Compute", "Amplification", "Mesh"):
        stage = getattr(rd.ShaderStage, name, None)
        if stage is not None:
            resource = pipe.GetShader(stage)
            shaders[name] = {"resourceId": str(int(resource)),
                             "entryPoint": str(pipe.GetShaderEntryPoint(stage)) if int(resource) else None}
    return {"shaders": shaders, "outputs": [str(int(item.resource)) for item in pipe.GetOutputTargets()],
            "depth": str(int(pipe.GetDepthTarget().resource))}


class _ControllerObserver(object):
    def __init__(self, native, owner):
        self.native, self.owner, self.calls, self.moves = native, owner, [], []

    def __getattr__(self, name):
        return getattr(self.native, name)

    def SetFrameEvent(self, event_id, force):
        self.moves.append({"eventId": int(event_id), "force": bool(force)})
        raise RuntimeError("GetUsage metadata validation forbids SetFrameEvent")

    def GetUsage(self, resource):
        values = self.native.GetUsage(resource)
        raw = _usage(values)
        self.calls.append({"resourceId": str(int(resource)), "recordCount": len(raw), "sha256": _hash(raw)})
        return values


class _ManagerObserver(object):
    def __init__(self, native, guards, output):
        self.native, self.guards, self.output, self.batches = native, guards, output, []

    def __getattr__(self, name):
        return getattr(self.native, name)

    def BlockInvoke(self, callback):
        import renderdoc as rd
        def invoke(native):
            proxy = _ControllerObserver(native, self)
            before, state_before, fatal_before = _pixels(native, self.guards, rd), _state(native, rd), _fatal(native)
            started = time.monotonic()
            try:
                callback(proxy)
            finally:
                if proxy.calls or proxy.moves:
                    self.batches.append({"usageCalls": proxy.calls, "frameEvents": proxy.moves,
                                         "pixelsBefore": before, "pixelsAfter": _pixels(native, self.guards, rd),
                                         "stateBefore": state_before, "stateAfter": _state(native, rd),
                                         "fatalBefore": fatal_before, "fatalAfter": _fatal(native),
                                         "elapsedMs": (time.monotonic() - started) * 1000})
                    _write(self.output, {"batches": self.batches})
        return self.native.BlockInvoke(invoke)


def start_reference(output_path, observation_path, parent_event, guard_resource, guard_point, probe_only=False):
    import renderdoc as rd
    import rendertest_agent_bridge as bridge

    def read(controller):
        report = {"parentEventId": parent_event, "fatalBefore": _fatal(controller),
                  "uiStateBefore": bridge._runtime.state.snapshot(), "setFrameEventCalls": 0}
        structured = controller.GetStructuredFile()
        actions, stack = [], [(action, None) for action in reversed(list(controller.GetRootActions()))]
        api_count = 0
        while stack:
            action, parent = stack.pop()
            calls = []
            for event in action.events:
                index = int(event.chunkIndex)
                calls.append({"eventId": int(event.eventId), "chunkIndex": index,
                              "name": str(structured.chunks[index].name) if 0 <= index < len(structured.chunks) else None})
            api_count += len(calls)
            if len(actions) >= 100000 or api_count > MAX_RECORDS:
                raise RuntimeError("Native action/API metadata exceeds the bounded reference budget")
            actions.append({"eventId": int(action.eventId), "parentEventId": parent,
                            "children": [int(child.eventId) for child in action.children],
                            "flags": str(action.flags), "name": str(action.GetName(structured)), "apiEvents": calls})
            stack.extend((child, int(action.eventId)) for child in reversed(list(action.children)))
        if parent_event not in {item["eventId"] for item in actions}:
            raise RuntimeError("Reference parent is not an exact action EID")
        textures, buffers, descriptions = list(controller.GetTextures()), list(controller.GetBuffers()), list(controller.GetResources())
        resources = {}
        for item in descriptions:
            kind = str(item.type).split(".")[-1]
            if kind in ("Texture", "SwapchainImage", "Buffer"):
                resources[str(int(item.resourceId))] = {"handle": item.resourceId, "name": str(item.name),
                                                       "kind": "buffer" if kind == "Buffer" else "texture",
                                                       "type": kind}
        for kind, values in (("texture", textures), ("buffer", buffers)):
            for item in values:
                key = str(int(item.resourceId))
                resources.setdefault(key, {"handle": item.resourceId, "name": kind.title() + " " + key,
                                           "kind": kind, "type": kind.title()})
                resources[key]["kind"] = kind
        resources.pop("0", None)
        if not 1 <= len(resources) <= MAX_RESOURCES:
            raise RuntimeError("Native reference resource count exceeds 1..%d" % MAX_RESOURCES)
        texture_map = {str(int(item.resourceId)): item for item in textures}
        selected_resource = guard_resource
        if selected_resource is None:
            pipe = controller.GetPipelineState()
            outputs = list(pipe.GetOutputTargets()) + [pipe.GetDepthTarget()]
            selected_resource = next((str(int(item.resource)) for item in outputs
                                      if str(int(item.resource)) in texture_map), None)
            if selected_resource is None:
                selected_resource = next((key for key, item in sorted(texture_map.items(), key=lambda pair: int(pair[0]))
                                          if int(item.dimension) == 2 and int(item.msSamp) <= 1 and
                                          int(item.width) > 0 and int(item.height) > 0), None)
        texture = texture_map.get(selected_resource)
        selected_point = guard_point if guard_point is not None else (
            [int(texture.width) // 2, int(texture.height) // 2] if texture is not None else None)
        if texture is None or not (0 <= selected_point[0] < int(texture.width) and 0 <= selected_point[1] < int(texture.height)):
            raise RuntimeError("Guard texture/coordinate is unavailable")
        report["guardResourceId"], report["guardPoint"] = selected_resource, selected_point
        subresource = rd.Subresource()
        subresource.mip, subresource.slice, subresource.sample = 0, 0, 0
        guards = [(texture.resourceId, selected_point[0], selected_point[1], subresource)]
        report["pixelsBefore"], report["pipelineBefore"] = _pixels(controller, guards, rd), _state(controller, rd)
        report["actions"] = actions
        rows, record_count, byte_count = [], 0, len(_encoded(actions))
        started = time.monotonic()
        for key in sorted(resources, key=int):
            if time.monotonic() - started > 120:
                raise RuntimeError("Native metadata reference exceeded the 120-second budget")
            item = resources[key]
            raw = _usage(controller.GetUsage(item["handle"]))
            record_count += len(raw)
            row = {"resourceId": key, "name": item["name"], "kind": item["kind"], "type": item["type"], "usages": raw,
                   "usageSha256": _hash(raw)}
            byte_count += len(_encoded(row))
            if record_count > MAX_RECORDS or byte_count > MAX_BYTES:
                raise RuntimeError("Native metadata reference exceeds its record/byte budget")
            rows.append(row)
        report.update(resources=rows, resourceCount=len(rows), usageRecordCount=record_count,
                      elapsedMs=(time.monotonic() - started) * 1000, primitiveBytes=byte_count,
                      pixelsAfter=_pixels(controller, guards, rd), pipelineAfter=_state(controller, rd),
                      fatalAfter=_fatal(controller), uiStateAfter=bridge._runtime.state.snapshot())
        return report, guards

    def work():
        try:
            deadline = time.time() + 240
            while not bridge._runtime.state.snapshot().get("captureLoaded"):
                if time.time() > deadline:
                    raise RuntimeError("Pass-resource reference capture load timeout")
                time.sleep(.25)
            box = {}
            def invoke(controller):
                try:
                    box["reference"] = read(controller)
                except BaseException:
                    box["error"] = traceback.format_exc()
            bridge._runtime.replay.manager.BlockInvoke(invoke)
            if "error" in box:
                raise RuntimeError(box["error"])
            report, guards = box["reference"]
            if not probe_only:
                bridge._runtime.replay.manager = _ManagerObserver(bridge._runtime.replay.manager, guards, observation_path)
                _write(observation_path, {"batches": []})
        except BaseException:
            report = {"error": traceback.format_exc()}
        _write(output_path, report)

    threading.Thread(target=work, daemon=True).start()
