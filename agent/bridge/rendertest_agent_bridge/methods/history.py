from ..ipc import AgentBridgeError
from ..paging import paginate, query_key
from ..values import enum_name, number
from .common import (action_index, get_bool, get_choice, get_int, invalid, renderdoc_module,
                     result, session_of)
from .textures import INTEGER_COMP_TYPES, _interpret, _prepare, _subresource, _texture_info

ORDERS = ("newest", "oldest")
FRAGMENT_LIMIT = 64
FAILURE_FLAGS = ("sampleMasked", "backfaceCulled", "depthClipped", "depthBoundsFailed",
                 "viewClipped", "scissorClipped", "shaderDiscarded", "depthTestFailed",
                 "stencilTestFailed", "predicationSkipped")
INVALID_MARKER = (0xdeadbeef, 0xdeadf00d)


def _value(texture, value):
    if tuple(int(item) for item in list(value.col.uintValue)[:2]) == INVALID_MARKER:
        return None
    _, color = _interpret(texture, value.col)
    depth = float(value.depth)
    stencil = int(value.stencil)
    return {"color": color, "depth": number(depth) if depth >= 0.0 else None,
            "stencil": stencil if stencil >= 0 else None}


def _fragment(texture, item):
    failures = [name for name in FAILURE_FLAGS if bool(getattr(item, name, False))]
    return {
        "fragIndex": int(item.fragIndex),
        "primitiveId": int(item.primitiveID),
        "passed": not failures,
        "failures": failures,
        "preMod": _value(texture, item.preMod),
        "shaderOut": _value(texture, item.shaderOut),
        "postMod": _value(texture, item.postMod),
        "directShaderWrite": bool(item.directShaderWrite),
        "unboundPS": bool(item.unboundPS),
    }


def _event(actions, event_id, fragments):
    node = actions.action_for(event_id)
    passed = [item for item in fragments if item["passed"]]
    failures = {}
    for item in fragments:
        for name in item["failures"]:
            failures[name] = failures.get(name, 0) + 1
    pre = fragments[0]["preMod"]
    post = fragments[-1]["postMod"]
    return {
        "eventId": event_id,
        "actionName": node["name"] if node else None,
        "fragmentCount": len(fragments),
        "passedCount": len(passed),
        "passed": bool(passed),
        "failureCounts": failures,
        "primitiveIds": sorted(set(item["primitiveId"] for item in passed))[:16],
        "directShaderWrite": any(item["directShaderWrite"] for item in fragments),
        "unboundPS": any(item["unboundPS"] for item in fragments),
        "changed": None if pre is None or post is None else pre != post,
        "preMod": pre,
        "shaderOut": passed[-1]["shaderOut"] if passed else fragments[-1]["shaderOut"],
        "postMod": post,
    }


def _group(texture, actions, history, event_id):
    grouped = []
    for item in history:
        item_event = int(item.eventId)
        if item_event > event_id:
            continue
        if not grouped or grouped[-1][0] != item_event:
            grouped.append((item_event, []))
        grouped[-1][1].append(_fragment(texture, item))
    return [(_event(actions, item_event, fragments), fragments)
            for item_event, fragments in grouped]


def _summary(events):
    passed = [item for item in events if item["passed"]]
    changed = [item for item in passed if item["changed"]]
    return {
        "eventCount": len(events),
        "fragmentCount": sum(item["fragmentCount"] for item in events),
        "passedEventCount": len(passed),
        "firstEventId": events[0]["eventId"] if events else None,
        "lastEventId": events[-1]["eventId"] if events else None,
        "lastPassedEventId": passed[-1]["eventId"] if passed else None,
        "lastChangedEventId": changed[-1]["eventId"] if changed else None,
        "finalValue": events[-1]["postMod"] if events else None,
    }


def _warnings(texture, view, event_id, events, discards):
    warnings = []
    if not events:
        warnings.append("No pixel modifications were recorded up to EID %d; the value predates "
                        "the frame or was written by something pixel history does not track."
                        % event_id)
    last = events[-1]["eventId"] if events else -1
    later = [item for item in discards if item > last]
    if later:
        warnings.append("Discard events %s come after the last recorded modification. Pixel "
                        "history skips discards; afterwards the contents are undefined and the "
                        "replay shows a fill pattern." % ", ".join(str(item) for item in later[-8:]))
    if int(texture.msSamp) > 1:
        warnings.append("The texture is multisampled; the history is for sample %d only."
                        % view["sample"])
    return warnings


def pixel_history(runtime, context):
    params = context.params
    order = get_choice(params, "order", ORDERS, "newest")
    include_fragments = get_bool(params, "include_fragments", False)
    key, resources, event_id, view = _prepare(runtime, context)
    x = get_int(params, "x", required=True)
    y = get_int(params, "y", required=True)
    if x >= view["width"] or y >= view["height"]:
        raise invalid("(%d, %d) is outside the %dx%d mip." % (x, y, view["width"], view["height"]))
    handle = resources.handle(key)
    texture = resources.textures[key]
    actions = action_index(runtime, context)
    session = session_of(context)
    meta = {}

    def read(controller):
        rd = renderdoc_module()
        properties = getattr(controller, "GetAPIProperties", None)
        if properties is not None and not getattr(properties(), "pixelHistory", True):
            raise AgentBridgeError("UNSUPPORTED", "This capture's API does not support pixel "
                                   "history.")
        history = controller.PixelHistory(handle, x, y, _subresource(rd, view),
                                          rd.CompType.Typeless)
        grouped = _group(texture, actions, history, event_id)
        discards = [int(usage.eventId) for usage in controller.GetUsage(handle)
                    if int(usage.eventId) <= event_id and enum_name(usage.usage) == "Discard"]
        return grouped, discards

    def build():
        value, replay_meta = runtime.replay.at_event(context, event_id, read)
        meta.update(replay_meta)
        return value

    cache_key = ("pixelHistory", key, x, y, view["mip"], view["slice"], view["sample"], event_id)
    grouped, discards = runtime.cache.get(session, cache_key, build)
    events = [item for item, _ in grouped]
    ordered = list(reversed(grouped)) if order == "newest" else grouped
    page, next_cursor, total = paginate(ordered, params, "pixel_history", session,
                                        query_key(cache_key, order))
    items = []
    for summary, fragments in page:
        item = dict(summary)
        if include_fragments:
            item["fragments"] = fragments[:FRAGMENT_LIMIT]
            item["fragmentsTruncated"] = len(fragments) > FRAGMENT_LIMIT
        items.append(item)
    return result({
        "eventId": event_id,
        "texture": _texture_info(resources, key, view),
        "x": x,
        "y": y,
        "interpretedAs": INTEGER_COMP_TYPES.get(enum_name(texture.format.compType), "float"),
        "order": order,
        "total": total,
        "summary": _summary(events),
        "discardEventIds": discards[-16:],
        "events": items,
    }, meta=meta, warnings=_warnings(texture, view, event_id, events, discards),
        next_cursor=next_cursor)


HANDLERS = {
    "pixel_history": pixel_history,
}
