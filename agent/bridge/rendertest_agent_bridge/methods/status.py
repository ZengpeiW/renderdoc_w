import os

from ..ipc import AgentBridgeError
from .common import action_index, payload, resource_index
from .pipeline import read_pipeline_summary


def _capture_summary(state):
    if not state.get("captureLoaded"):
        return None
    return {
        "captureLoaded": True,
        "capturePath": state.get("capturePath") or "",
        "captureName": os.path.basename(state.get("capturePath") or ""),
        "captureSessionId": state.get("captureSessionId") or "",
        "api": state.get("api") or "",
        "selectedEventId": state.get("selectedEventId"),
        "currentEventId": state.get("currentEventId"),
    }


def ping(runtime, context):
    return payload({"pong": True, "instanceId": runtime.instance_id})


def get_status(runtime, context):
    return payload({
        "bridge": {
            "instanceId": runtime.instance_id,
            "version": runtime.bridge_version,
            "renderdocVersion": runtime.renderdoc_version,
            "pid": os.getpid(),
            "methods": list(runtime.methods),
        },
        "capture": _capture_summary(context.state),
    })


def _pipeline_summary(runtime, context):
    if runtime.replay.manager is None:
        return None, []
    try:
        actions = action_index(runtime, context)
        resources = resource_index(runtime, context)
        node = actions.action_for(context.state.get("currentEventId"))
        summary = runtime.replay.call(
            context, lambda controller: read_pipeline_summary(controller, resources, node)[0])
        return summary, []
    except AgentBridgeError as error:
        return None, ["Pipeline summary unavailable: %s: %s" % (error.code, error.message)]


def get_context(runtime, context):
    state = context.state
    if not state.get("captureLoaded"):
        return payload({"captureLoaded": False, "action": None, "pipeline": None})
    pipeline, warnings = _pipeline_summary(runtime, context)
    return payload({
        "captureLoaded": True,
        "api": state.get("api") or "",
        "currentEventId": state.get("currentEventId"),
        "selectedEventId": state.get("selectedEventId"),
        "action": state.get("currentAction"),
        "selectedAction": state.get("selectedAction"),
        "pipeline": pipeline,
    }, warnings=warnings)


HANDLERS = {
    "ping": ping,
    "get_status": get_status,
    "get_context": get_context,
}
