import os


def _payload(data):
    return {"data": data, "artifacts": [], "warnings": [], "nextCursor": None,
            "truncated": False}


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
    return _payload({"pong": True, "instanceId": runtime.instance_id})


def get_status(runtime, context):
    return _payload({
        "bridge": {
            "instanceId": runtime.instance_id,
            "version": runtime.bridge_version,
            "renderdocVersion": runtime.renderdoc_version,
            "pid": os.getpid(),
            "methods": list(runtime.methods),
        },
        "capture": _capture_summary(context.state),
    })


def get_context(runtime, context):
    state = context.state
    if not state.get("captureLoaded"):
        return _payload({"captureLoaded": False, "action": None, "pipeline": None})
    return _payload({
        "captureLoaded": True,
        "api": state.get("api") or "",
        "currentEventId": state.get("currentEventId"),
        "selectedEventId": state.get("selectedEventId"),
        "action": state.get("currentAction"),
        "selectedAction": state.get("selectedAction"),
        "pipeline": None,
    })


HANDLERS = {
    "ping": ping,
    "get_status": get_status,
    "get_context": get_context,
}
