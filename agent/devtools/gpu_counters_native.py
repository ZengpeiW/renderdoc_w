"""Independent GPU-counter probe/observer inside the owned test GUI (Python 3.6).

The observer delegates replay calls unchanged and records the SAME native FetchCounters result
used by the tool. It never starts a second measurement or fetches non-duration/vendor counters.
"""

import json
import math
import pathlib
import threading
import time
import traceback


def _name(value):
    return str(value).split(".")[-1]


def _description(value):
    return {"counterId": int(value.counter), "name": str(value.name),
            "category": str(value.category), "description": str(value.description),
            "resultType": _name(value.resultType), "resultByteWidth": int(value.resultByteWidth),
            "unit": _name(value.unit), "uuid": [int(word) for word in value.uuid.words]}


def _fatal(controller):
    status = controller.GetFatalErrorStatus()
    return {"code": int(status.code), "message": str(status)}


def _rows(results, description):
    field = "d" if description["resultByteWidth"] == 8 else "f"
    rows = []
    for result in results:
        value = float(getattr(result.value, field))
        finite = math.isfinite(value)
        rows.append({"eventId": int(result.eventId), "counterId": int(result.counter),
                     "seconds": value if finite else ("NaN" if math.isnan(value) else
                                                        ("Infinity" if value > 0 else "-Infinity")),
                     "valid": finite and value >= 0})
    return rows


def _write(path, value):
    path = pathlib.Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding="utf-8")
    temporary.replace(path)


def _guards(controller, event_id, rd, resource_filter=None):
    controller.SetFrameEvent(event_id, True)
    textures = dict((int(item.resourceId), item) for item in controller.GetTextures())
    pipe = controller.GetPipelineState()
    descriptors = list(pipe.GetOutputTargets()) + [pipe.GetDepthTarget()]
    guards = []
    seen = set()
    for descriptor in descriptors:
        resource = int(descriptor.resource)
        if not resource or resource not in textures or resource in seen:
            continue
        if resource_filter and str(resource) not in resource_filter:
            continue
        seen.add(resource)
        texture = textures[resource]
        mip = int(descriptor.firstMip)
        width, height = max(1, int(texture.width) >> mip), max(1, int(texture.height) >> mip)
        subresource = rd.Subresource()
        subresource.mip, subresource.slice, subresource.sample = mip, int(descriptor.firstSlice), 0
        for y in sorted(set((0, height // 2, height - 1))):
            for x in sorted(set((0, width // 2, width - 1))):
                guards.append((descriptor.resource, x, y, subresource))
        if len(seen) >= 4:
            break
    return guards


def _pixels(controller, guards, rd):
    result = []
    for resource, x, y, subresource in guards:
        value = controller.PickPixel(resource, x, y, subresource, rd.CompType.Typeless)
        # UInt view preserves bit-exact native values even for floating-point attachments.
        result.append({"resourceId": str(int(resource)), "x": x, "y": y,
                       "mip": int(subresource.mip), "slice": int(subresource.slice),
                       "uintBits": [int(component) for component in value.uintValue]})
    return result


class _ControllerObserver(object):
    def __init__(self, native, owner):
        self.native, self.owner = native, owner
        self.moves, self.records = [], []

    def __getattr__(self, name):
        return getattr(self.native, name)

    def SetFrameEvent(self, event_id, force):
        self.moves.append({"eventId": int(event_id), "force": bool(force)})
        return self.native.SetFrameEvent(event_id, force)

    def FetchCounters(self, counters):
        import renderdoc as rd
        ids = [int(value) for value in counters]
        if ids != [int(rd.GPUCounter.EventGPUDuration)]:
            raise RuntimeError("Test permits only EventGPUDuration sampling, got %r" % ids)
        record = {"counterIds": ids, "pixelsBefore": _pixels(self.native, self.owner.guards, rd)}
        record["frameEventOffset"] = len(self.moves)
        record["fatalBefore"] = _fatal(self.native)
        self.records.append(record)
        self.owner.measurements.append(record)
        start = time.monotonic()
        try:
            values = self.native.FetchCounters(counters)
            record["elapsedMs"] = (time.monotonic() - start) * 1000
            record["fatalAfterFetch"] = _fatal(self.native)
            record["rows"] = _rows(values, self.owner.description)
            record["pixelsAfterFetchBeforeRestore"] = _pixels(self.native, self.owner.guards, rd)
            return values
        except BaseException:
            record["error"] = traceback.format_exc()
            raise


class _ManagerObserver(object):
    def __init__(self, native, guards, description, output):
        self.native, self.guards, self.description, self.output = native, guards, description, output
        self.measurements = []

    def __getattr__(self, name):
        return getattr(self.native, name)

    def BlockInvoke(self, callback):
        import renderdoc as rd
        def invoke(native):
            proxy = _ControllerObserver(native, self)
            try:
                callback(proxy)
            finally:
                if proxy.records:
                    after = _pixels(native, self.guards, rd)
                    for record in proxy.records:
                        record["frameEvents"] = proxy.moves
                        record["frameEventsAfterFetch"] = proxy.moves[record["frameEventOffset"]:]
                        record["pixelsAfterCallback"] = after
                        record["pixelsRestored"] = record["pixelsBefore"] == after
                        record["fatalAfterCallback"] = _fatal(native)
                    _write(self.output, {"measurements": self.measurements})
        return self.native.BlockInvoke(invoke)


def start_reference(output_path, measurement_path, event_id, probe_only, resource_filter=None):
    import renderdoc as rd
    import rendertest_agent_bridge as bridge

    def read(controller):
        selected_event = event_id
        previous = bridge._runtime.state.snapshot().get("currentEventId")
        report = {"guardEventId": event_id, "counterId": int(rd.GPUCounter.EventGPUDuration)}
        guards = []
        try:
            counters = list(controller.EnumerateCounters())
            report["counters"] = [_description(controller.DescribeCounter(counter)) for counter in counters]
            description = next((item for item in report["counters"] if
                                item["counterId"] == int(rd.GPUCounter.EventGPUDuration)), None)
            if description is None:
                raise RuntimeError("EventGPUDuration is not available")
            if description["unit"] != "Seconds" or description["resultType"] != "Float":
                raise RuntimeError("Unexpected duration descriptor: %r" % description)
            report["durationDescription"] = description
            structured = controller.GetStructuredFile()
            stack = [(action, None) for action in reversed(list(controller.GetRootActions()))]
            actions, draws = [], []
            while stack:
                action, parent = stack.pop()
                actions.append({"eventId": int(action.eventId), "name": str(action.GetName(structured)),
                                "parentEventId": parent, "children": [int(child.eventId) for child in action.children]})
                if action.flags & rd.ActionFlags.Drawcall and not action.children:
                    draws.append(int(action.eventId))
                stack.extend((child, int(action.eventId)) for child in reversed(list(action.children)))
            report["actions"] = actions
            if selected_event is None:
                for candidate in sorted(draws)[:24]:
                    guards = _guards(controller, candidate, rd, resource_filter)
                    if guards:
                        selected_event = candidate
                        break
                if selected_event is None:
                    raise RuntimeError("No early draw has a usable texture guard")
            else:
                guards = _guards(controller, selected_event, rd, resource_filter)
            report["guardEventId"] = selected_event
            report["pixelsBefore"] = _pixels(controller, guards, rd)
            if not guards:
                raise RuntimeError("No bound texture can establish actual replay-state restoration")
            controller.SetFrameEvent(selected_event, True)
            report["guardStabilityPixels"] = _pixels(controller, guards, rd)
            report["guardStabilityVerified"] = report["pixelsBefore"] == report["guardStabilityPixels"]
            report["guardResourceFilter"] = resource_filter or []
            if not report["guardStabilityVerified"]:
                report["error"] = "Selected guard changes during repeated forced replay without counter sampling."
                return report, guards, description
            if probe_only:
                start = time.monotonic()
                report["fatalBefore"] = _fatal(controller)
                try:
                    values = controller.FetchCounters([rd.GPUCounter.EventGPUDuration])
                    report["elapsedMs"] = (time.monotonic() - start) * 1000
                    report["fatalAfterFetch"] = _fatal(controller)
                    report["rows"] = _rows(values, description)
                    report["pixelsAfterFetchBeforeRestore"] = _pixels(controller, guards, rd)
                finally:
                    controller.SetFrameEvent(selected_event, True)
                report["pixelsAfterForcedRestore"] = _pixels(controller, guards, rd)
                report["fatalAfterForcedRestore"] = _fatal(controller)
                report["pixelsRestored"] = report["pixelsBefore"] == report["pixelsAfterForcedRestore"]
                report["stateChangedByFetch"] = report["pixelsBefore"] != report["pixelsAfterFetchBeforeRestore"]
            return report, guards, description
        finally:
            if previous is not None:
                controller.SetFrameEvent(previous, True)

    def work():
        try:
            deadline = time.time() + 240
            while not bridge._runtime.state.snapshot().get("captureLoaded"):
                if time.time() > deadline:
                    raise RuntimeError("Native GPU counter reference capture load timeout")
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
            result, guards, description = box["reference"]
            if not probe_only and "error" not in result:
                bridge._runtime.replay.manager = _ManagerObserver(
                    bridge._runtime.replay.manager, guards, description, measurement_path)
                result["sameMeasurementObserverInstalled"] = True
                _write(measurement_path, {"measurements": []})
        except BaseException:
            result = {"error": traceback.format_exc()}
        _write(output_path, result)

    threading.Thread(target=work, daemon=True).start()
