from ..paging import paginate, query_key
from ..values import enum_name, flag_names, resource_format, resource_id
from .common import (action_index, get_choice, get_resource_key, get_str, require_resource,
                     resource_index, result, session_of)

RESOURCE_KINDS = ("texture", "buffer", "shader", "pipeline", "sampler", "other")
RELATED_LIMIT = 64
WRITE_USAGES = ("ColorTarget", "DepthStencilTarget", "Clear", "Discard", "GenMips", "Resolve",
                "ResolveDst", "CopyDst", "CPUWrite", "StreamOut")
READ_WRITE_USAGES = ("Copy",)


def usage_access(name):
    if name in WRITE_USAGES:
        return "write"
    if name.endswith("_RWResource") or name in READ_WRITE_USAGES:
        return "readWrite"
    if name in ("Unused", "Barrier"):
        return "other"
    return "read"


def list_resources(runtime, context):
    params = context.params
    kind = get_choice(params, "kind", RESOURCE_KINDS)
    query = get_str(params, "query")
    needle = query.strip().lower() if query else None
    resources = resource_index(runtime, context)
    keys = []
    for key in resources.order:
        item = resources.items[key]
        if kind is not None and item["kind"] != kind:
            continue
        if needle and needle not in item["name"].lower() and needle != key:
            continue
        keys.append(key)
    page, next_cursor, total = paginate(keys, params, "list_resources", session_of(context),
                                        query_key(kind, needle))
    return result({
        "kind": kind,
        "query": query,
        "total": total,
        "resources": [resources.summary(key) for key in page],
    }, next_cursor=next_cursor)


def _related(values):
    keys = [key for key in (resource_id(value) for value in values) if key]
    return keys[:RELATED_LIMIT], len(keys)


def get_resource(runtime, context):
    key = get_resource_key(context.params)
    resources = resource_index(runtime, context)
    require_resource(resources, key)
    data = resources.summary(key)
    description = resources.descriptions[key]
    if hasattr(description, "parentResources"):
        parents, parent_count = _related(description.parentResources)
        derived, derived_count = _related(description.derivedResources)
        data.update({
            "parentResources": [resources.ref(item) for item in parents],
            "parentResourceCount": parent_count,
            "derivedResources": [resources.ref(item) for item in derived],
            "derivedResourceCount": derived_count,
            "initialisationChunkCount": len(description.initialisationChunks),
        })
    texture = resources.textures.get(key)
    if texture is not None:
        data.update({
            "format": resource_format(texture.format),
            "creationFlags": flag_names(texture.creationFlags),
            "byteSize": int(texture.byteSize),
            "msQuality": int(texture.msQual),
        })
    buffer = resources.buffers.get(key)
    if buffer is not None:
        data.update({
            "creationFlags": flag_names(buffer.creationFlags),
            "gpuAddress": int(buffer.gpuAddress),
        })
    return result(data)


def get_resource_usage(runtime, context):
    params = context.params
    key = get_resource_key(params)
    resources = resource_index(runtime, context)
    handle = require_resource(resources, key)
    actions = action_index(runtime, context)
    session = session_of(context)

    def read(controller):
        return [(int(usage.eventId), enum_name(usage.usage))
                for usage in controller.GetUsage(handle)]

    usages = runtime.cache.get(session, ("usage", key),
                               lambda: runtime.replay.call(context, read))
    page, next_cursor, total = paginate(usages, params, "get_resource_usage", session,
                                        query_key(key))
    items = []
    for event_id, usage in page:
        node = actions.action_for(event_id)
        items.append({
            "eventId": event_id,
            "usage": usage,
            "access": usage_access(usage),
            "actionEventId": node["eventId"] if node else None,
            "actionName": node["name"] if node else None,
        })
    return result({
        "resource": resources.ref(key),
        "total": total,
        "usages": items,
    }, next_cursor=next_cursor)


HANDLERS = {
    "list_resources": list_resources,
    "get_resource": get_resource,
    "get_resource_usage": get_resource_usage,
}
