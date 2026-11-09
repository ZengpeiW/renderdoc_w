"""Debug one compute invocation using RenderDoc's native DebugThread interpreter."""

from ..artifacts import artifact_path
from ..ipc import AgentBridgeError
from .common import (action_index, get_int, invalid, renderdoc_module, resolve_event,
                     resource_index, session_of, stage_enum)
from .debug import DEFAULT_MAX_STEPS, MAX_STEPS, _debuggable_shader, _finish, _trace

UINT32_MAX = (1 << 32) - 1
_NON_COMPUTE_FLAGS = ("Drawcall", "MeshDispatch", "DispatchRay", "MultiAction", "CmdList",
                      "PushMarker", "PopMarker", "SetMarker")
_COMPUTE_WARNING = ("This trace debugs one selected compute invocation. lastChanges are debugger "
                    "variables, not a complete record of UAV writes or final resource contents; "
                    "the trace does not establish ordering or race behaviour between workgroups.")


def _coordinate(params, name):
    value = params.get(name)
    if (not isinstance(value, list) or len(value) != 3 or
            any(type(component) is not int or component < 0 or component > UINT32_MAX
                for component in value)):
        raise invalid("%s must be an array of three integers between 0 and %d."
                      % (name, UINT32_MAX))
    return list(value)


def _dimensions(value):
    try:
        dims = [int(component) for component in value]
    except (TypeError, ValueError, OverflowError):
        return None
    if len(dims) != 3 or any(component <= 0 or component > UINT32_MAX for component in dims):
        return None
    return dims


def _compute_action(actions, event_id):
    # action_for() intentionally maps non-action API events to an owner for inspection tools. A
    # debugger must instead require the exact executable action, never silently advance an EID.
    node = actions.nodes.get(event_id)
    if (node is None or "Dispatch" not in node["flags"] or node["children"] or
            any(flag in node["flags"] for flag in _NON_COMPUTE_FLAGS)):
        raise AgentBridgeError("INVALID_EVENT", "Event %d is not an individual compute dispatch. "
                               "Select the actual dispatch action, including a resolved indirect "
                               "child where applicable." % event_id,
                               details={"event_id": event_id})
    dispatch = _dimensions(node["dispatchDimension"])
    if dispatch is None:
        raise AgentBridgeError("INVALID_EVENT", "The dispatch has zero or unknown workgroup "
                               "dimensions and cannot identify an executing invocation.",
                               details={"event_id": event_id,
                                        "dispatchDimensions": node["dispatchDimension"]})
    base = node.get("dispatchBase")
    if base is None or len(base) != 3 or any(component != 0 for component in base):
        # Current native Vulkan DebugThread validates group IDs against dispatchDimension and
        # does not account for vkCmdDispatchBase. Do not claim its builtins are the real invocation.
        raise AgentBridgeError("UNSUPPORTED", "Compute debugging with a non-zero or unknown "
                               "dispatch base is not supported.",
                               details={"dispatchBase": base})
    return node, dispatch


def _thread_dimensions(node, reflection):
    override = node["dispatchThreadsDimension"]
    # Match qrenderdoc's ComputeDebugSelector: x == 0 means the dispatch has no size override.
    use_override = bool(override) and override[0] != 0
    source = "action_override" if use_override else "shader_reflection"
    raw = override if use_override else getattr(reflection, "dispatchThreadsDimension", None)
    dims = _dimensions(raw)
    if dims is None:
        raise AgentBridgeError("UNSUPPORTED", "The compute threadgroup size is zero or unknown; "
                               "thread bounds and global thread coordinates cannot be verified.",
                               details={"threadGroupSizeSource": source})
    return dims, source


def _check_bounds(coordinates, dimensions, name):
    for axis, (coordinate, dimension) in enumerate(zip(coordinates, dimensions)):
        if coordinate >= dimension:
            raise invalid("%s[%d] must be below %d." % (name, axis, dimension),
                          coordinate=name, axis=axis, dimensions=dimensions)


def debug_compute(runtime, context):
    params = context.params
    group = _coordinate(params, "group")
    thread = _coordinate(params, "thread")
    get_int(params, "event_id", maximum=UINT32_MAX)
    max_steps = get_int(params, "max_steps", DEFAULT_MAX_STEPS, minimum=1, maximum=MAX_STEPS)
    if max_steps is None:
        raise invalid("max_steps must be an integer.")
    actions = action_index(runtime, context)
    event_id = resolve_event(context, actions)
    node, dispatch = _compute_action(actions, event_id)
    _check_bounds(group, dispatch, "group")
    resources = resource_index(runtime, context)
    session = session_of(context)
    path = artifact_path(runtime, session, "debug-compute-e%d-g%d-%d-%d-t%d-%d-%d"
                         % ((event_id,) + tuple(group) + tuple(thread)), "jsonl")
    invocation = {"eventId": event_id, "stage": "compute", "group": group, "thread": thread,
                  "dispatchDimensions": dispatch}

    def read(controller):
        rd = renderdoc_module()
        properties = getattr(controller, "GetAPIProperties", None)
        if properties is None or not bool(getattr(properties(), "shaderDebugging", False)):
            raise AgentBridgeError("UNSUPPORTED", "This capture's API does not report shader "
                                   "debugging support.")
        if not callable(getattr(controller, "DebugThread", None)):
            raise AgentBridgeError("UNSUPPORTED", "Native compute-thread debugging is unavailable.")
        pipe = controller.GetPipelineState()
        shader, debug_info = _debuggable_shader(controller, pipe, rd, "compute", event_id,
                                                resources)
        if not bool(getattr(debug_info, "debuggable", False)):
            raise AgentBridgeError("UNSUPPORTED", "The compute shader is not known to be debuggable.",
                                   details={"shader": shader})
        reflection = pipe.GetShaderReflection(stage_enum(rd, "compute"))
        thread_dims, source = _thread_dimensions(node, reflection)
        _check_bounds(thread, thread_dims, "thread")
        global_thread = [group[i] * thread_dims[i] + thread[i] for i in range(3)]
        if any(value > UINT32_MAX for value in global_thread):
            raise invalid("The selected global thread coordinate exceeds the native 32-bit "
                          "compute builtin range.", globalThread=global_thread)
        invocation.update(globalThread=global_thread, threadGroupSize=thread_dims,
                          threadGroupSizeSource=source)
        context.check_cancelled()
        trace = controller.DebugThread(group, thread)
        # _trace owns every returned trace, including a null debugger, and FreeTrace runs in finally.
        return _trace(context, controller, trace, path, invocation, shader, debug_info, max_steps)

    data, meta = runtime.replay.at_event(context, event_id, read)
    source = dict(invocation)
    source["event_id"] = source.pop("eventId")
    response = _finish(data, invocation, path, source, max_steps,
                       "RenderDoc could not produce a compute-thread trace for this invocation.", meta)
    response.result["warnings"].append(_COMPUTE_WARNING)
    return response


HANDLERS = {"debug_compute": debug_compute}
