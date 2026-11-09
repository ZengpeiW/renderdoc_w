"""Read bounded, lazy projections of a capture's structured API-call data.

These are RenderDoc's serialised fields, which can include return values and internal metadata.
Ordinal paths retain hidden fields and duplicate names. Buffer values are metadata only: their
numeric storage is an SDFile buffer index, never an address or a resource to dereference.
"""

import json
import re

from ..ipc import AgentBridgeError
from ..paging import decode_cursor, encode_cursor, query_key
from ..values import enum_name, flag_names, flags_value, number, resource_id
from .common import action_index, get_int, get_str, invalid, resolve_event, result, session_of

MAX_NODES = 4096
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_PATH_LENGTH = 4096
MAX_PATH_COMPONENTS = 64
MAX_STRING_LENGTH = 4096
MAX_NAME_LENGTH = 256
UINT32_MAX = (1 << 32) - 1
_COMPONENT = re.compile(r"\[(0|[1-9][0-9]*)\]")
_SURROGATE = re.compile("[\ud800-\udfff]")
_FIELD_WARNING = ("These are RenderDoc serialised API fields; they may include return values or "
                  "internal data and are not a guaranteed copy of the application's arguments.")


def _validate(params):
    event_id = get_int(params, "event_id", maximum=UINT32_MAX)
    api_event_id = get_int(params, "api_event_id", maximum=UINT32_MAX)
    path = get_str(params, "parameter_path", max_length=MAX_PATH_LENGTH)
    if api_event_id is None and path is not None:
        raise invalid("parameter_path requires api_event_id.")
    path = "" if path is None else path
    tokens = []
    position = 0
    while position < len(path):
        match = _COMPONENT.match(path, position)
        if match is None or len(tokens) >= MAX_PATH_COMPONENTS:
            raise invalid("parameter_path must contain at most 64 ordinal [index] components.")
        # Child indices cannot exceed the native uint32 API-event/array range.
        digits = match.group(1)
        if len(digits) > 10 or int(digits) > UINT32_MAX:
            raise invalid("A parameter_path child index is too large.")
        tokens.append(int(digits))
        position = match.end()
    limit = get_int(params, "limit", default=100, minimum=1, maximum=1000)
    depth = get_int(params, "max_depth", default=0, minimum=0, maximum=4)
    if limit is None or depth is None:
        raise invalid("limit and max_depth must be integers.")
    cursor = get_str(params, "cursor", max_length=1024)
    if cursor == "":
        raise invalid("The cursor is invalid.")
    return {"event": event_id, "api": api_event_id, "path": path, "tokens": tokens,
            "limit": limit, "depth": depth, "cursor": cursor}


def _text(item, key, value, limit):
    value = str(value)
    preview = value[:limit]
    # Native char/string wrappers can preserve invalid UTF-8 as surrogate escapes. IPC uses strict
    # UTF-8, so retain these code units as explicit escaped text instead of failing the whole reply.
    item[key] = _SURROGATE.sub(lambda match: "\\u%04x" % ord(match.group(0)), preview)
    if item[key] != preview:
        item[key + "EscapedInvalidUnicode"] = True
    if len(value) > limit:
        item[key + "Truncated"] = True
        item[key + "Length"] = len(value)


def _integer(item, value):
    value = int(value)
    item["value"] = value if abs(value) <= (1 << 53) - 1 else str(value)
    if isinstance(item["value"], str):
        item["valueEncoding"] = "decimal"


def _shallow(obj, path):
    basic_type = enum_name(obj.type.basetype)
    item = {"canonicalPath": path, "type": basic_type,
            "flags": flag_names(obj.type.flags), "flagsValue": flags_value(obj.type.flags),
            "byteSize": int(obj.type.byteSize), "childCount": int(obj.NumChildren())}
    _text(item, "name", obj.name, MAX_NAME_LENGTH)
    _text(item, "typeName", obj.type.name, MAX_NAME_LENGTH)
    item["membersTruncated"] = item["childCount"] > 0
    basic = obj.data.basic
    if basic_type == "Null":
        item["value"] = None
    elif basic_type == "String":
        if item["flagsValue"] & 0x8:  # SDTypeFlags.NullString, not an empty array.
            item["value"] = None
        else:
            _text(item, "value", obj.AsString(), MAX_STRING_LENGTH)
    elif basic_type in ("UnsignedInteger", "Enum"):
        _integer(item, basic.u)
    elif basic_type == "SignedInteger":
        _integer(item, basic.i)
    elif basic_type == "Float":
        item["value"] = number(float(basic.d))
    elif basic_type == "Boolean":
        item["value"] = bool(obj.AsBool())
    elif basic_type == "Character":
        _text(item, "value", basic.c, MAX_STRING_LENGTH)
    elif basic_type == "Resource":
        item["value"] = resource_id(obj.AsResourceId())
        item["valueEncoding"] = "resourceId"
    elif basic_type == "GPUAddress":
        item["value"] = "0x%x" % int(basic.u)
        item["valueEncoding"] = "hex"
    elif basic_type == "Buffer":
        item["valueAvailable"] = False
        item["reason"] = "Raw serialised buffer payloads are omitted; this is not a GPU resource address."
    elif basic_type not in ("Chunk", "Struct", "Array"):
        item["valueAvailable"] = False
        item["reason"] = "Unrecognised structured basic type."
    if item["flagsValue"] & 0x1:
        _text(item, "displayValue", obj.AsString(), MAX_STRING_LENGTH)
    return item


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8"))


class _Budget(object):
    def __init__(self, payload, nodes=0):
        self.bytes = MAX_PAYLOAD_BYTES - _size(payload) - 4096
        self.nodes = MAX_NODES - nodes

    def take(self, item):
        size = _size(item) + 32
        if self.nodes <= 0 or size > self.bytes:
            return False
        self.bytes -= size
        self.nodes -= 1
        return True


def _child(obj, index):
    child = obj.GetChild(index)
    if child is None:
        raise AgentBridgeError("INTERNAL_ERROR", "Structured API data contains a missing child.",
                               details={"childIndex": index})
    return child


def _project(obj, path, depth, path_depth, budget, check_cancelled):
    check_cancelled()
    item = _shallow(obj, path)
    if not budget.take(item):
        return None
    count = item["childCount"]
    if count and depth > 0 and path_depth < MAX_PATH_COMPONENTS:
        children = []
        for index in range(count):
            if budget.nodes <= 0 or budget.bytes <= 0:
                break
            child = _project(_child(obj, index), path + "[%d]" % index, depth - 1,
                             path_depth + 1, budget, check_cancelled)
            if child is None:
                break
            children.append(child)
        item["members"] = children
        item["membersTruncated"] = len(children) < count
    return item


def _incomplete(item):
    return (any(item.get(key, False) for key in
                ("membersTruncated", "nameTruncated", "typeNameTruncated", "valueTruncated",
                 "displayValueTruncated")) or
            any(_incomplete(child) for child in item.get("members", [])))


def _chunk(structured, record):
    chunk_index = record["chunkIndex"]
    if chunk_index is None or chunk_index == UINT32_MAX:
        return None, "This API event has no serialised chunk (it may be a synthetic marker)."
    chunks = getattr(structured, "chunks", ())
    if chunk_index < 0 or chunk_index >= len(chunks):
        return None, "The API event's chunk index is outside the structured file."
    chunk = chunks[chunk_index]
    if chunk is None:
        return None, "The API event's serialised chunk is unavailable."
    return chunk, None


def _call_info(structured, record):
    item = dict(record)
    chunk, reason = _chunk(structured, record)
    item["available"] = chunk is not None
    item["parameterCount"] = int(chunk.NumChildren()) if chunk is not None else 0
    _text(item, "name", chunk.name if chunk is not None else "", MAX_NAME_LENGTH)
    if reason:
        item["reason"] = reason
    return item, chunk


def _next(request, session, key, end, total):
    return (encode_cursor("get_api_calls", session, key, end) if end < total else None)


def _list_calls(structured, records, event_id, owner, request, session, check_cancelled):
    data = {"mode": "calls", "eventId": event_id, "actionEventId": owner,
            "calls": [], "total": len(records)}
    key = query_key(session, "calls", event_id, owner, request["depth"])
    offset = decode_cursor(request["cursor"], "get_api_calls", session, key)
    if offset > len(records):
        raise invalid("The API call cursor is beyond the call list.")
    budget = _Budget(data)
    end = offset
    for position in range(offset, min(len(records), offset + request["limit"])):
        check_cancelled()
        item, _ = _call_info(structured, records[position])
        if not budget.take(item):
            break
        data["calls"].append(item)
        end = position + 1
    cursor = _next(request, session, key, end, len(records))
    truncated = cursor is not None or any(item.get("nameTruncated") for item in data["calls"])
    return result(data, warnings=[_FIELD_WARNING], next_cursor=cursor, truncated=truncated)


def _parameters(structured, record, request, session, check_cancelled):
    data, chunk = _call_info(structured, record)
    data.update(mode="parameters", parameterPath=request["path"], canonicalPath=request["path"],
                selected=None, parameters=[], childCount=0)
    key = query_key(session, "parameters", record["actionEventId"], record["apiEventId"],
                    record["chunkIndex"], request["path"], request["depth"])
    offset = decode_cursor(request["cursor"], "get_api_calls", session, key)
    if chunk is None:
        if request["tokens"] or offset:
            raise invalid("The unavailable API call has no parameter path or page.")
        return result(data, warnings=[_FIELD_WARNING])
    obj = chunk
    for index in request["tokens"]:
        check_cancelled()
        count = int(obj.NumChildren())
        if index >= count:
            raise invalid("The parameter_path child index is out of range.", childCount=count)
        obj = _child(obj, index)
    if request["tokens"]:
        data["selected"] = _shallow(obj, request["path"])
    count = int(obj.NumChildren())
    data["childCount"] = count
    if offset > count:
        raise invalid("The API parameter cursor is beyond the child list.")
    budget = _Budget(data, 1 if data["selected"] is not None else 0)
    end = offset
    if len(request["tokens"]) == MAX_PATH_COMPONENTS and count:
        raise AgentBridgeError("LIMIT_EXCEEDED", "The parameter tree exceeds the 64-component path limit.")
    for index in range(offset, min(count, offset + request["limit"])):
        item = _project(_child(obj, index), request["path"] + "[%d]" % index,
                        request["depth"], len(request["tokens"]) + 1,
                        budget, check_cancelled)
        if item is None:
            if not data["parameters"]:
                raise AgentBridgeError("LIMIT_EXCEEDED", "A parameter exceeds the 256 KiB page limit.")
            break
        data["parameters"].append(item)
        end = index + 1
    cursor = _next(request, session, key, end, count)
    selected = data["selected"]
    # Selected metadata is shallow; its children are represented by the separate page.
    if selected is not None:
        selected["membersTruncated"] = offset > 0 or end < count
    truncated = (cursor is not None or any(_incomplete(item) for item in data["parameters"]) or
                 (selected is not None and _incomplete(selected)) or data.get("nameTruncated", False))
    warnings = [_FIELD_WARNING]
    if truncated:
        warnings.append("Some fields are omitted by paging, depth or response limits. Use nextCursor "
                        "for siblings or a returned canonicalPath to page a parameter's children. "
                        "Truncated strings are previews only.")
    return result(data, warnings=warnings, next_cursor=cursor, truncated=truncated)


def get_api_calls(runtime, context):
    request = _validate(context.params)
    actions = action_index(runtime, context)
    session = session_of(context)
    if request["api"] is None:
        event_id = resolve_event(context, actions)
        owner = actions.action_for(event_id)["eventId"]
        records = actions.action_api_events.get(owner, [])
        return runtime.replay.call(context, lambda controller: _list_calls(
            controller.GetStructuredFile(), records, event_id, owner, request, session,
            context.check_cancelled))
    record = actions.api_events.get(request["api"])
    if record is None:
        raise AgentBridgeError("INVALID_EVENT", "The requested EID is not a recorded API event.",
                               details={"api_event_id": request["api"]})
    if request["event"] is not None:
        event_id = resolve_event(context, actions)
        if actions.action_for(event_id)["eventId"] != record["actionEventId"]:
            raise invalid("event_id and api_event_id belong to different actions.")
    return runtime.replay.call(context, lambda controller: _parameters(
        controller.GetStructuredFile(), record, request, session, context.check_cancelled))


HANDLERS = {"get_api_calls": get_api_calls}
