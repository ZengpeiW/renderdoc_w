from ..ipc import AgentBridgeError
from ..paging import paginate, query_key
from .common import (action_index, get_bool, get_choices, get_int, get_str, resolve_event, result,
                     session_of)

ACTION_FILTERS = {
    "draw": ("Drawcall", "MeshDispatch"),
    "dispatch": ("Dispatch", "DispatchRay"),
    "marker": ("SetMarker", "PushMarker"),
    "clear": ("Clear", "ClearColor", "ClearDepthStencil"),
    "copy": ("Copy",),
    "resolve": ("Resolve",),
    "present": ("Present",),
}


def list_actions(runtime, context):
    params = context.params
    parent = get_int(params, "parent_event_id")
    include_markers = get_bool(params, "include_markers", True)
    actions = action_index(runtime, context)
    if parent is not None and parent not in actions.nodes:
        raise AgentBridgeError("INVALID_EVENT", "Event %d is not an action." % parent,
                               details={"event_id": parent})
    nodes = actions.children(parent, include_markers)
    page, next_cursor, total = paginate(nodes, params, "list_actions", session_of(context),
                                        query_key(parent, include_markers))
    return result({
        "parentEventId": parent,
        "parentPath": ([] if parent is None else
                       actions.path(parent) + [actions.nodes[parent]["name"]]),
        "includeMarkers": include_markers,
        "total": total,
        "lastEventId": actions.last_event_id,
        "actions": [actions.summary(node) for node in page],
    }, next_cursor=next_cursor)


def _matches(node, needle, flags):
    if flags and not any(flag in node["flags"] for name in flags for flag in ACTION_FILTERS[name]):
        return False
    if needle is None:
        return True
    if needle in node["name"].lower():
        return True
    return needle.isdigit() and int(needle) == node["eventId"]


def find_actions(runtime, context):
    params = context.params
    query = get_str(params, "query", required=True)
    flags = get_choices(params, "flags", tuple(ACTION_FILTERS))
    needle = query.strip().lower() or None
    actions = action_index(runtime, context)
    matches = [actions.nodes[event_id] for event_id in actions.order
               if _matches(actions.nodes[event_id], needle, flags)]
    page, next_cursor, total = paginate(matches, params, "find_actions", session_of(context),
                                        query_key(needle, sorted(flags or [])))
    return result({
        "query": query,
        "flags": flags or [],
        "total": total,
        "actions": [actions.summary(node, with_path=True) for node in page],
    }, next_cursor=next_cursor)


def set_event(runtime, context):
    get_int(context.params, "event_id", required=True)
    actions = action_index(runtime, context)
    event_id = resolve_event(context, actions)
    session = session_of(context)
    before = context.state.get("currentEventId")

    def apply():
        if runtime.state.snapshot().get("captureSessionId") != session:
            raise AgentBridgeError("CAPTURE_CHANGED", "The capture changed before the event moved.")
        ctx = runtime.ctx
        ctx.SetEventID([], event_id, event_id)
        return int(ctx.CurSelectedEvent()), int(ctx.CurEvent())

    selected, current = runtime.ui.call(context, apply)
    node = actions.action_for(current)
    return result({
        "eventIdBefore": before,
        "eventIdAfter": current,
        "selectedEventId": selected,
        "action": actions.summary(node, with_path=True) if node else None,
    }, meta={"eventIdBefore": before, "eventIdAfter": current})


HANDLERS = {
    "list_actions": list_actions,
    "find_actions": find_actions,
    "set_event": set_event,
}
