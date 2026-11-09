"""Independent native compute-debug reference for an owned test GUI (Python 3.6)."""

import json
import pathlib
import threading
import time
import traceback


def _variable(variable, depth=0):
    name = str(variable.type).split(".")[-1]
    fields = {"UInt": "u32v", "SInt": "s32v", "Float": "f32v", "ULong": "u64v",
              "SLong": "s64v", "Double": "f64v", "Bool": "u32v"}
    item = {"name": str(variable.name), "type": name, "rows": int(variable.rows),
            "columns": int(variable.columns)}
    if variable.members:
        item["members"] = [_variable(child, depth + 1) for child in list(variable.members)[:32]] if depth < 4 else []
    elif name in fields:
        count = max(1, int(variable.rows) * int(variable.columns))
        item["value"] = list(getattr(variable.value, fields[name]))[:min(count, 16)]
    return item


def start_reference(output_path, event_id=None):
    import renderdoc as rd
    import rendertest_agent_bridge as bridge

    def read(controller):
        previous = bridge._runtime.state.snapshot().get("currentEventId")
        actions = []
        pending = list(controller.GetRootActions())
        structured = controller.GetStructuredFile()
        while pending:
            action = pending.pop()
            pending.extend(action.children)
            if (not action.children and action.flags & rd.ActionFlags.Dispatch and
                    not action.flags & getattr(rd.ActionFlags, "DispatchRay", 0) and
                    not action.flags & getattr(rd.ActionFlags, "MeshDispatch", 0)):
                dims = [int(value) for value in action.dispatchDimension]
                base = [int(value) for value in getattr(action, "dispatchBase", (0, 0, 0))]
                if any(base):
                    continue
                if len(dims) == 3 and min(dims) > 0 and max(dims) > 1:
                    actions.append((dims[0] * dims[1] * dims[2], int(action.eventId), action))
        actions.sort(key=lambda item: (item[0], item[1]))
        if event_id is not None:
            actions = [item for item in actions if item[1] == event_id]
        report = {"candidates": [], "selected": None, "debugAttempts": []}
        try:
            for _, eid, action in actions[:24]:
                controller.SetFrameEvent(eid, False)
                pipe = controller.GetPipelineState()
                reflection = pipe.GetShaderReflection(rd.ShaderStage.Compute)
                if reflection is None:
                    continue
                reflected_threads = [int(value) for value in reflection.dispatchThreadsDimension]
                action_threads = [int(value) for value in action.dispatchThreadsDimension]
                # Match the GUI selector: nonzero x selects the override; malformed overrides
                # remain unusable candidates instead of silently falling back to reflection.
                override = bool(action_threads) and action_threads[0] != 0
                threads = action_threads if override else reflected_threads
                dims = [int(value) for value in action.dispatchDimension]
                candidate = {"eventId": eid, "name": str(action.GetName(structured)),
                             "shaderResourceId": str(int(pipe.GetShader(rd.ShaderStage.Compute))),
                             "entryPoint": str(pipe.GetShaderEntryPoint(rd.ShaderStage.Compute)),
                             "encoding": str(reflection.encoding).split(".")[-1],
                             "dispatchDimensions": dims, "threadGroupSize": threads,
                             "threadGroupSizeSource": "action_override" if override else "shader_reflection",
                             "actionThreadsDimension": action_threads,
                             "reflectionThreadsDimension": reflected_threads,
                             "nonActionApiEventId": next((int(item.eventId) for item in action.events
                                                          if int(item.eventId) != eid), None),
                             "debuggable": bool(reflection.debugInfo.debuggable),
                             "debugStatus": str(reflection.debugInfo.debugStatus),
                             "outputSignatureCount": len(reflection.outputSignature)}
                report["candidates"].append(candidate)
            candidates = [item for item in report["candidates"] if item["debuggable"] and
                          len(item["threadGroupSize"]) == 3 and min(item["threadGroupSize"]) > 0 and
                          max(item["threadGroupSize"]) > 1]
            candidates.sort(key=lambda item: (item["dispatchDimensions"][0] *
                item["dispatchDimensions"][1] * item["dispatchDimensions"][2] *
                item["threadGroupSize"][0] * item["threadGroupSize"][1] * item["threadGroupSize"][2],
                item["eventId"]))
            for candidate in candidates[:3]:
                controller.SetFrameEvent(candidate["eventId"], False)
                dims, size = candidate["dispatchDimensions"], candidate["threadGroupSize"]
                group = [1 if value > 1 else 0 for value in dims]
                thread = [1 if value > 1 else 0 for value in size]
                trace = None
                attempt = {"eventId": candidate["eventId"], "group": group, "thread": thread}
                try:
                    trace = controller.DebugThread(group, thread)
                    attempt["debugged"] = trace is not None and trace.debugger is not None
                    if not attempt["debugged"]:
                        continue
                    attempt["inputs"] = [_variable(variable) for variable in list(trace.inputs)[:64]]
                    batch = controller.ContinueDebug(trace.debugger)
                    attempt["firstBatchSteps"] = len(batch)
                    attempt["firstBatchLastStep"] = int(batch[-1].stepIndex) if batch else None
                    names = {value["name"] for value in attempt["inputs"]}
                    usable = "SV_DispatchThreadID" in names or ("SV_GroupID" in names and
                                                               "SV_GroupThreadID" in names)
                    attempt["coordinateInputsAvailable"] = usable
                    if usable and len(batch) > 1:
                        selected = dict(candidate)
                        selected.update(attempt)
                        selected["dispatchThreadId"] = [group[i] * size[i] + thread[i] for i in range(3)]
                        selected["groupIndex"] = thread[0] + size[0] * (thread[1] + size[1] * thread[2])
                        report["selected"] = selected
                        break
                except BaseException:
                    attempt["error"] = traceback.format_exc()
                finally:
                    report["debugAttempts"].append(attempt)
                    if trace is not None:
                        controller.FreeTrace(trace)
            return report
        finally:
            if previous is not None:
                controller.SetFrameEvent(previous, False)

    def work():
        try:
            deadline = time.time() + 240
            while not bridge._runtime.state.snapshot().get("captureLoaded"):
                if time.time() > deadline:
                    raise RuntimeError("Native compute reference capture load timeout")
                time.sleep(.25)
            box = {}
            def invoke(controller):
                try:
                    box["reference"] = read(controller)
                except BaseException:
                    box["reference"] = {"error": traceback.format_exc()}
            bridge._runtime.replay.manager.BlockInvoke(invoke)
            result = box["reference"]
        except BaseException:
            result = {"error": traceback.format_exc()}
        path = pathlib.Path(output_path)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
        temporary.replace(path)

    threading.Thread(target=work, daemon=True).start()
