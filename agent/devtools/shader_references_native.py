"""Bounded independent shader-binding reference in an owned RenderTest (Python 3.6).

Only the chosen action subtree is replayed. The observer delegates calls unchanged and
records native binding reads and bit-exact texture state around the production scan.
"""

import threading
import time
import traceback

from gpu_counters_native import _fatal, _guards, _pixels, _write


STAGES = ("vertex", "hull", "domain", "geometry", "pixel", "compute", "amplification", "mesh")
STAGE_ENUMS = ("Vertex", "Hull", "Domain", "Geometry", "Pixel", "Compute", "Amplification", "Mesh")


def _stages(action, rd):
    if action.children:
        return []
    flags = action.flags
    if flags & rd.ActionFlags.DispatchRay:
        return []
    if flags & rd.ActionFlags.MeshDispatch:
        return ["amplification", "mesh", "pixel"]
    if flags & rd.ActionFlags.Dispatch:
        return ["compute"]
    if flags & rd.ActionFlags.Drawcall:
        return list(STAGES[:5])
    return []


class _PipelineObserver(object):
    def __init__(self, native, owner):
        self.native, self.owner = native, owner

    def __getattr__(self, name):
        return getattr(self.native, name)

    def GetShader(self, stage):
        value = self.native.GetShader(stage)
        self.owner.reads.append({"eventId": self.owner.event, "stage": self.owner.stage_names[int(stage)],
                                 "resourceId": str(int(value)) if int(value) else None})
        return value


class _ControllerObserver(object):
    def __init__(self, native, owner, rd):
        self.native, self.owner, self.rd = native, owner, rd
        self.moves, self.reads = [], []
        self.event = None
        self.before, self.before_restore = None, None
        self.stage_names = dict((int(getattr(rd.ShaderStage, enum)), name)
                                for name, enum in zip(STAGES, STAGE_ENUMS))

    def __getattr__(self, name):
        return getattr(self.native, name)

    def SetFrameEvent(self, event_id, force):
        if self.before is None:
            self.before = _pixels(self.native, self.owner.guards, self.rd)
        if force and int(event_id) == self.owner.guard_event and self.reads:
            self.before_restore = _pixels(self.native, self.owner.guards, self.rd)
        self.moves.append({"eventId": int(event_id), "force": bool(force)})
        self.event = int(event_id)
        return self.native.SetFrameEvent(event_id, force)

    def GetPipelineState(self):
        return _PipelineObserver(self.native.GetPipelineState(), self)


class _ManagerObserver(object):
    def __init__(self, native, guards, guard_event, path):
        self.native, self.guards, self.guard_event, self.path = native, guards, guard_event, path
        self.batches = []

    def __getattr__(self, name):
        return getattr(self.native, name)

    def BlockInvoke(self, callback):
        import renderdoc as rd
        def invoke(native):
            proxy = _ControllerObserver(native, self, rd)
            fatal_before = _fatal(native)
            try:
                callback(proxy)
            finally:
                if proxy.moves or proxy.reads:
                    self.batches.append({"frameEvents": proxy.moves, "shaderReads": proxy.reads,
                                         "pixelsBefore": proxy.before,
                                         "pixelsBeforeRestore": proxy.before_restore,
                                         "pixelsAfter": _pixels(native, self.guards, rd),
                                         "fatalBefore": fatal_before, "fatalAfter": _fatal(native)})
                    _write(self.path, {"batches": self.batches})
        return self.native.BlockInvoke(invoke)


def start_reference(output_path, observation_path, parent_event, guard_event, guard_resource,
                    probe_only=False, guard_grid=3, guard_points=None, max_candidates=64):
    import renderdoc as rd
    import rendertest_agent_bridge as bridge

    def read(controller):
        previous = bridge._runtime.state.snapshot().get("currentEventId")
        report = {"parentEventId": parent_event, "guardEventId": guard_event,
                  "guardResourceId": guard_resource, "guardGrid": guard_grid, "guardPoints": guard_points,
                  "fatalBefore": _fatal(controller)}
        guards = []
        try:
            structured = controller.GetStructuredFile()
            nodes = {}
            stack = [(action, None) for action in reversed(list(controller.GetRootActions()))]
            while stack:
                action, parent = stack.pop()
                nodes[int(action.eventId)] = (action, parent)
                stack.extend((child, int(action.eventId)) for child in reversed(list(action.children)))
            if parent_event not in nodes:
                raise RuntimeError("Reference scope is not an exact action EID")
            scope, pending = [], [nodes[parent_event][0]]
            while pending:
                action = pending.pop()
                scope.append(action)
                pending.extend(reversed(list(action.children)))
            candidates = sorted([action for action in scope if _stages(action, rd)],
                                key=lambda action: int(action.eventId))
            if not 1 <= len(candidates) <= max_candidates:
                raise RuntimeError("Bounded reference requires 1..%d candidates, got %d" %
                                   (max_candidates, len(candidates)))
            report["scope"] = [{"eventId": int(action.eventId), "parentEventId": nodes[int(action.eventId)][1],
                                "name": str(action.GetName(structured)), "flags": str(action.flags),
                                "children": [int(child.eventId) for child in action.children],
                                "stages": _stages(action, rd)} for action in scope]
            resources = list(controller.GetResources())
            report["shaderResources"] = [{"resourceId": str(int(item.resourceId)), "name": str(item.name)}
                                         for item in resources if item.type == rd.ResourceType.Shader]
            guards = _guards(controller, guard_event, rd, [guard_resource])
            if not guards:
                raise RuntimeError("The selected guard resource is not bound at the guard EID")
            if not 2 <= guard_grid <= 17:
                raise RuntimeError("Guard grid must have 2..17 points per axis")
            if guard_points or guard_grid != 3:
                width, height = max(item[1] for item in guards) + 1, max(item[2] for item in guards) + 1
                resource, _, _, subresource = guards[0]
                if guard_points:
                    if len(guard_points) > 16 or any(not 0 <= x < width or not 0 <= y < height
                                                   for x, y in guard_points):
                        raise RuntimeError("Guard points must be at most 16 in-bounds texture coordinates")
                    guards = [(resource, x, y, subresource) for x, y in guard_points]
                else:
                    xs = sorted(set(min(width - 1, i * width // (guard_grid - 1)) for i in range(guard_grid)))
                    ys = sorted(set(min(height - 1, i * height // (guard_grid - 1)) for i in range(guard_grid)))
                    guards = [(resource, x, y, subresource) for y in ys for x in xs]
            report["pixelsBefore"] = _pixels(controller, guards, rd)
            guard_pipe = controller.GetPipelineState()
            report["guardOutputs"] = {"colors": [str(int(item.resource)) for item in guard_pipe.GetOutputTargets()],
                                       "depth": str(int(guard_pipe.GetDepthTarget().resource))}
            controller.SetFrameEvent(guard_event, True)
            report["pixelsAfterRepeatedForce"] = _pixels(controller, guards, rd)
            if report["pixelsBefore"] != report["pixelsAfterRepeatedForce"]:
                raise RuntimeError("Guard is nondeterministic without a shader scan")
            bindings = []
            started = time.monotonic()
            try:
                for action in candidates:
                    eid = int(action.eventId)
                    controller.SetFrameEvent(eid, False)
                    pipe = controller.GetPipelineState()
                    shaders = []
                    for name in _stages(action, rd):
                        stage = getattr(rd.ShaderStage, STAGE_ENUMS[STAGES.index(name)])
                        resource = pipe.GetShader(stage)
                        if int(resource):
                            entry_point = str(pipe.GetShaderEntryPoint(stage))
                            shaders.append({"stage": name, "resourceId": str(int(resource)),
                                            "entryPoint": entry_point,
                                            "entryPointKnown": bool(entry_point)})
                    bindings.append({"eventId": eid, "parentEventId": nodes[eid][1],
                                     "name": str(action.GetName(structured)), "bindings": shaders,
                                     "outputResources": {"colors": [str(int(item.resource))
                                                                    for item in pipe.GetOutputTargets()],
                                                         "depth": str(int(pipe.GetDepthTarget().resource))}})
                report["pixelsBeforeRestore"] = _pixels(controller, guards, rd)
            finally:
                controller.SetFrameEvent(guard_event, True)
            report["elapsedMs"] = (time.monotonic() - started) * 1000
            report["bindings"] = bindings
            report["pixelsAfterRestore"] = _pixels(controller, guards, rd)
            report["fatalAfter"] = _fatal(controller)
            if report["pixelsBefore"] != report["pixelsAfterRestore"]:
                raise RuntimeError("Native bounded reference did not restore bit-exact guard pixels")
            return report, guards
        finally:
            if previous is not None:
                controller.SetFrameEvent(previous, True)

    def work():
        try:
            deadline = time.time() + 240
            while not bridge._runtime.state.snapshot().get("captureLoaded"):
                if time.time() > deadline:
                    raise RuntimeError("Shader reference capture load timeout")
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
                bridge._runtime.replay.manager = _ManagerObserver(
                    bridge._runtime.replay.manager, guards, guard_event, observation_path)
                _write(observation_path, {"batches": []})
                report["observerInstalled"] = True
        except BaseException:
            report = {"error": traceback.format_exc()}
        _write(output_path, report)

    threading.Thread(target=work, daemon=True).start()
