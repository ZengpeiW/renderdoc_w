import bisect
import hashlib
import json
import os

from ..artifacts import artifact_path, describe
from ..ipc import AgentBridgeError
from ..values import enum_name, flags_value
from .common import (action_index, get_int, renderdoc_module, resolve_event, resource_index,
                     result, session_of)
from .pipeline import _bound_shader, convert_variable

DEFAULT_MAX_STEPS = 20000
MAX_STEPS = 200000
TRACE_MAX_BYTES = 64 * 1024 * 1024
CHANGE_BUDGET = 256
INPUT_LIMIT = 64
OUTPUT_LIMIT = 32
LAST_CHANGES = 16
EVENT_STEP_LIMIT = 16
NO_PREFERENCE = 0xFFFFFFFF
SHADER_EVENTS = (("SampleLoadGather", 0x1), ("GeneratedNanOrInf", 0x2), ("DebugBreak", 0x4))


def _event_names(flags):
    bits = flags_value(flags)
    return [name for name, bit in SHADER_EVENTS if bits & bit]


def _convert(variable, budget=CHANGE_BUDGET):
    return convert_variable(variable, [budget])


def _lookup(variables, path):
    if path in variables:
        return variables[path]
    for separator in (".", "["):
        head, found, _ = path.partition(separator)
        if found and head in variables:
            rest = path[len(head) + (1 if separator == "." else 0):]
            members = dict((item["name"], item) for item in variables[head].get("members", []))
            return _lookup(members, rest)
    return None


class _LineTable(object):
    def __init__(self, trace, files):
        info = sorted(trace.instInfo, key=lambda item: int(item.instruction))
        self.instructions = [int(item.instruction) for item in info]
        self.lines = [item.lineInfo for item in info]
        self.files = files

    def line(self, instruction):
        position = bisect.bisect_right(self.instructions, instruction) - 1
        if position < 0:
            return None
        info = self.lines[position]
        file_index = int(info.fileIndex)
        line = {"disassemblyLine": int(info.disassemblyLine)}
        if 0 <= file_index < len(self.files):
            line.update({"file": self.files[file_index], "lineStart": int(info.lineStart),
                         "lineEnd": int(info.lineEnd)})
        return line


class _TraceWriter(object):
    def __init__(self, path, header):
        self.path = path
        self.partial = path + ".part"
        self.stream = open(self.partial, "wb")
        self.digest = hashlib.sha256()
        self.bytes = 0
        self.full = False
        self.write(header, reserve=False)

    def write(self, record, reserve=True):
        if self.full:
            return False
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False) + "\n").encode("utf-8")
        if reserve and self.bytes + len(encoded) > TRACE_MAX_BYTES - 4096:
            self.full = True
            return False
        self.stream.write(encoded)
        self.digest.update(encoded)
        self.bytes += len(encoded)
        return True

    def finish(self, summary):
        self.write(summary, reserve=False)
        self.stream.close()
        os.replace(self.partial, self.path)

    def abort(self):
        if not self.stream.closed:
            self.stream.close()
        if os.path.exists(self.partial):
            os.remove(self.partial)


def _outputs(trace, variables):
    outputs = []
    for mapping in trace.sourceVars:
        references = list(mapping.variables)
        if int(mapping.signatureIndex) < 0 or not references:
            continue
        if enum_name(references[0].type) != "Variable":
            continue
        values = []
        for reference in references:
            variable = _lookup(variables, reference.name)
            component = int(reference.component)
            items = (variable or {}).get("value") or []
            values.append(items[component] if component < len(items) else None)
        outputs.append({"name": mapping.name, "signatureIndex": int(mapping.signatureIndex),
                        "type": enum_name(mapping.type), "value": values})
        if len(outputs) >= OUTPUT_LIMIT:
            break
    return outputs


def _run(context, controller, trace, max_steps, lines, writer):
    variables = {}
    order = []
    steps = 0
    events = {"GeneratedNanOrInf": [], "SampleLoadGather": 0, "DebugBreak": []}
    stopped = False
    batch = controller.ContinueDebug(trace.debugger)
    while batch and not stopped:
        context.check_cancelled()
        for state in batch:
            if steps >= max_steps:
                stopped = True
                break
            changes = []
            for change in state.changes:
                before = change.before.name
                after = change.after.name
                if after:
                    converted = _convert(change.after)
                    variables[after] = converted
                    changes.append(converted)
                    name = after
                else:
                    variables.pop(before, None)
                    changes.append({"name": before, "removed": True})
                    name = before
                if name in order:
                    order.remove(name)
                order.append(name)
            instruction = int(state.nextInstruction)
            names = _event_names(state.flags)
            line = lines.line(instruction)
            if "GeneratedNanOrInf" in names and len(events["GeneratedNanOrInf"]) < EVENT_STEP_LIMIT:
                events["GeneratedNanOrInf"].append(
                    {"step": int(state.stepIndex), "instruction": instruction, "line": line,
                     "changes": [item for item in changes if not item.get("removed")][:4]})
            if "SampleLoadGather" in names:
                events["SampleLoadGather"] += 1
            if "DebugBreak" in names and len(events["DebugBreak"]) < EVENT_STEP_LIMIT:
                events["DebugBreak"].append(int(state.stepIndex))
            writer.write({"record": "step", "step": int(state.stepIndex),
                          "instruction": instruction, "events": names, "line": line,
                          "changes": changes})
            steps += 1
        if not stopped:
            batch = controller.ContinueDebug(trace.debugger)
    last = [variables[name] for name in order[-LAST_CHANGES:] if name in variables]
    return variables, steps, stopped, events, list(reversed(last))


def debug_pixel(runtime, context):
    params = context.params
    x = get_int(params, "x", required=True)
    y = get_int(params, "y", required=True)
    sample = get_int(params, "sample")
    primitive = get_int(params, "primitive")
    view = get_int(params, "view")
    max_steps = get_int(params, "max_steps", DEFAULT_MAX_STEPS, minimum=1, maximum=MAX_STEPS)
    actions = action_index(runtime, context)
    event_id = resolve_event(context, actions)
    resources = resource_index(runtime, context)
    session = session_of(context)
    path = artifact_path(runtime, session, "debug-pixel-e%d-x%d-y%d" % (event_id, x, y), "jsonl")
    invocation = {"eventId": event_id, "x": x, "y": y, "sample": sample,
                  "primitive": primitive, "view": view}

    def read(controller):
        rd = renderdoc_module()
        properties = getattr(controller, "GetAPIProperties", None)
        if properties is not None and not getattr(properties(), "shaderDebugging", True):
            raise AgentBridgeError("UNSUPPORTED", "This capture's API does not support shader "
                                   "debugging.")
        pipe = controller.GetPipelineState()
        _, _, key, reflection = _bound_shader(pipe, rd, "pixel", event_id)
        debug_info = reflection.debugInfo
        shader = {"resourceId": key, "name": resources.name(key),
                  "entryPoint": pipe.GetShaderEntryPoint(rd.ShaderStage.Pixel),
                  "encoding": enum_name(debug_info.encoding),
                  "sourceDebugInformation": bool(getattr(debug_info, "sourceDebugInformation",
                                                         False))}
        if not bool(getattr(debug_info, "debuggable", True)):
            raise AgentBridgeError("UNSUPPORTED", "The pixel shader cannot be debugged: %s"
                                   % getattr(debug_info, "debugStatus", ""),
                                   details={"shader": shader})
        inputs = rd.DebugPixelInputs()
        inputs.sample = NO_PREFERENCE if sample is None else sample
        inputs.primitive = NO_PREFERENCE if primitive is None else primitive
        inputs.view = NO_PREFERENCE if view is None else view
        trace = controller.DebugPixel(x, y, inputs)
        try:
            if trace is None or trace.debugger is None:
                return {"shader": shader, "debugged": False}, None
            files = [str(item.filename) for item in debug_info.files]
            writer = _TraceWriter(path, {"record": "header",
                                         "schema": "rendertest-shader-debug-trace/v1",
                                         "invocation": invocation, "shader": shader,
                                         "files": files})
            try:
                variables, steps, stopped, events, last = _run(
                    context, controller, trace, max_steps, _LineTable(trace, files), writer)
                writer.finish({"record": "summary", "steps": steps,
                               "executionComplete": not stopped,
                               "traceComplete": not writer.full})
            except BaseException:
                writer.abort()
                raise
            budget = [INPUT_LIMIT * 16]
            shader_inputs = [item for item in (convert_variable(variable, budget)
                                               for variable in list(trace.inputs)[:INPUT_LIMIT])
                             if item is not None]
            return {
                "shader": shader,
                "debugged": True,
                "steps": steps,
                "executionComplete": not stopped,
                "traceComplete": not writer.full,
                "inputs": shader_inputs,
                "outputs": _outputs(trace, variables),
                "lastChanges": last,
                "nanOrInfSteps": events["GeneratedNanOrInf"],
                "sampleSteps": events["SampleLoadGather"],
                "debugBreakSteps": events["DebugBreak"],
            }, writer
        finally:
            if trace is not None:
                controller.FreeTrace(trace)

    (data, writer), meta = runtime.replay.at_event(context, event_id, read)
    payload = dict(invocation)
    payload.update(data)
    warnings = []
    artifacts = []
    if not data["debugged"]:
        warnings.append("No fragment of this draw reached (%d, %d) for the given sample/primitive. "
                        "Use pixel_history at this EID to see which fragments touched the pixel "
                        "and their primitiveIds." % (x, y))
    else:
        artifact = describe(path, "shader-debug-trace", "application/x-ndjson",
                            source={"event_id": event_id, "x": x, "y": y, "stage": "pixel"},
                            steps=data["steps"], executionComplete=data["executionComplete"],
                            traceComplete=data["traceComplete"])
        artifacts.append(artifact)
        payload["traceArtifact"] = artifact["path"]
        if not data["executionComplete"]:
            warnings.append("Stopped after max_steps=%d; outputs are the values at that point."
                            % max_steps)
        if not data["traceComplete"]:
            warnings.append("The trace artifact hit its size limit; later steps are missing from "
                            "the file but were still executed.")
        if not data["outputs"]:
            warnings.append("The trace has no output signature mapping; see lastChanges for the "
                            "values written last.")
    return result(payload, meta=meta, artifacts=artifacts, warnings=warnings)


HANDLERS = {
    "debug_pixel": debug_pixel,
}
