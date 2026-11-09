"""Bounded, capture-local summaries of native resource-usage metadata.

GetUsage reads the capture's existing usage records. It does not replay an event, and these records
describe resource roles rather than observed pixel/byte accesses or producer/consumer dependencies.
"""

import datetime
import json
import time
import uuid

from ..ipc import AgentBridgeError
from ..paging import decode_cursor, encode_cursor, query_key
from ..replay import _check_replay_health
from ..values import enum_name, resource_id
from .common import (action_index, get_bool, get_choice, get_int, get_str, invalid,
                     resource_index, result, session_of)

MAX_SCOPE_ACTIONS = 100000
MAX_SCOPE_EVENTS = 200000
MAX_RESOURCES = 100000
MAX_RESOURCE_ROWS = 100000
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_PAGE_BYTES = 256 * 1024
MAX_USAGE_TYPES = 64
MAX_EXAMPLES = 16
NO_CHUNK = 0xFFFFFFFF
CACHE_NAMESPACE = ("pass", "resources", 1)
MARKERS = ("SetMarker", "PushMarker", "PopMarker")
EXECUTION_FLAGS = ("Drawcall", "Dispatch", "MeshDispatch", "DispatchRay", "Clear", "Copy",
                   "Resolve", "GenMips", "Present", "BuildAccStruct", "PassBoundary")
STAGE_PREFIXES = ("VS", "HS", "DS", "GS", "PS", "CS", "TS", "MS", "All")
READ_USAGES = frozenset(("VertexBuffer", "IndexBuffer", "InputTarget", "Indirect", "CopySrc",
                        "ResolveSrc") + tuple(prefix + suffix for prefix in STAGE_PREFIXES
                                              for suffix in ("_Constants", "_Resource")))
WRITE_USAGES = frozenset(("ColorTarget", "StreamOut", "Clear", "CopyDst", "ResolveDst", "CPUWrite"))
READ_WRITE_USAGES = frozenset(("Copy", "Resolve", "GenMips", "DepthStencilTarget") +
                            tuple(prefix + "_RWResource" for prefix in STAGE_PREFIXES))


def _check(runtime, context):
    context.check_cancelled()
    if time.time() > context.deadline_at:
        raise AgentBridgeError("TIMEOUT", "The pass-resource scan deadline has expired.")
    if session_of(context) != (runtime.state.snapshot().get("captureSessionId") or ""):
        raise AgentBridgeError("CAPTURE_CHANGED", "The capture changed during resource scanning.")


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def _text(row, key, value, limit=256):
    value = str(value)
    preview = value[:limit]
    row[key] = preview.encode("utf-8", "backslashreplace").decode("utf-8")
    if row[key] != preview:
        row[key + "EscapedInvalidUnicode"] = True
    if len(value) > limit:
        row[key + "Truncated"] = True
        row[key + "Length"] = len(value)


def _new_cache():
    return {"scanId": uuid.uuid4().hex,
            "scanStartedAtUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
            "resources": {}, "bytes": 0}


def _request(params):
    parent = get_int(params, "parent_event_id", maximum=NO_CHUNK)
    start = get_int(params, "start_event_id", maximum=NO_CHUNK)
    end = get_int(params, "end_event_id", maximum=NO_CHUNK)
    if parent is not None and (start is not None or end is not None):
        raise invalid("parent_event_id cannot be combined with an event range.")
    if (start is None) != (end is None):
        raise invalid("An event range requires both start_event_id and end_event_id.")
    if start is not None and start > end:
        raise invalid("start_event_id must not exceed end_event_id.")
    kind = get_choice(params, "kind", ("all", "texture", "buffer"), "all", required=True)
    limit = get_int(params, "limit", 200, minimum=1, maximum=1000, required=True)
    budget = get_int(params, "scan_budget", 200, minimum=1, maximum=2000, required=True)
    cursor = get_str(params, "cursor", max_length=1024)
    if cursor == "":
        raise invalid("The cursor is invalid.")
    refresh = get_bool(params, "refresh", False)
    if refresh and cursor is not None:
        raise invalid("refresh cannot be combined with a continuation cursor.")
    return parent, start, end, kind, limit, budget, cursor, refresh


def _fake_marker(actions, event_id):
    events = actions.action_api_events.get(event_id, ())
    # Equivalent to native ActionDescription.IsFakeMarker().
    return len(events) == 1 and events[0].get("chunkIndex") == NO_CHUNK


def _real_endpoint(actions, event_id):
    record = actions.api_events.get(event_id)
    if record is not None and record.get("chunkIndex") not in (None, NO_CHUNK):
        return True
    node = actions.nodes.get(event_id)
    return (node is not None and not _fake_marker(actions, event_id) and
            not set(node["flags"]).intersection(MARKERS) and
            bool(set(node["flags"]).intersection(EXECUTION_FLAGS)))


def _scope(actions, parent, start, end, check):
    if parent is not None and parent not in actions.nodes:
        raise AgentBridgeError("INVALID_EVENT", "parent_event_id must be an exact action EID.")
    if start is not None and (not _real_endpoint(actions, start) or not _real_endpoint(actions, end)):
        raise AgentBridgeError("INVALID_EVENT", "Range endpoints must be real API or executable "
                               "action EIDs, not synthetic marker EIDs.")
    roots = actions.roots if parent is None else [parent]
    if len(roots) > MAX_SCOPE_ACTIONS:
        raise AgentBridgeError("LIMIT_EXCEEDED", "The scope exceeds the 100000-action budget.")
    stack = list(reversed(roots))
    nodes, members, anchors = [], set(), []
    api_count = 0
    while stack:
        check()
        event_id = stack.pop()
        node = actions.nodes[event_id]
        nodes.append(event_id)
        if len(nodes) + len(stack) + len(node["children"]) > MAX_SCOPE_ACTIONS:
            raise AgentBridgeError("LIMIT_EXCEEDED", "The scope exceeds the 100000-action budget.")
        events = actions.action_api_events.get(event_id, ())
        api_count += len(events)
        if api_count > MAX_SCOPE_EVENTS:
            raise AgentBridgeError("LIMIT_EXCEEDED", "The scope exceeds the 200000-API-event budget.")
        members.add(event_id)
        for index, event in enumerate(events):
            if index % 256 == 0:
                check()
            members.add(event["apiEventId"])
        if len(members) > MAX_SCOPE_EVENTS:
            raise AgentBridgeError("LIMIT_EXCEEDED", "The scope exceeds the 200000-member-EID budget.")
        if _real_endpoint(actions, event_id):
            anchors.append(event_id)
        stack.extend(reversed(node["children"]))
    scope = {"kind": "capture", "parentEventId": parent, "startEventId": start, "endEventId": end,
             "actionCount": len(nodes), "selectedEventCount": 0,
             "membership": "All native usage EIDs, including usages without an indexed action."}
    if parent is not None:
        if not anchors:
            raise AgentBridgeError("UNSUPPORTED", "This subtree has no real API/action anchors "
                                   "from which to determine a resource-usage boundary.")
        start, end = min(anchors), max(anchors)
        members = set(eid for eid in members if start <= eid <= end and _real_endpoint(actions, eid))
        scope.update(kind="actionSubtree", startEventId=start, endEventId=end,
                     syntheticParent=_fake_marker(actions, parent), selectedEventCount=len(members),
                     membership="Exact subtree action/API EIDs, clipped to real action anchors; "
                     "synthetic marker EIDs and setup before the first anchor are excluded.")
    elif start is not None:
        members = set(eid for eid in members if start <= eid <= end and _real_endpoint(actions, eid))
        scope.update(kind="eventRange", selectedEventCount=len(members),
                     actionCount=sum(1 for eid in nodes if start <= eid <= end and
                                     _real_endpoint(actions, eid)),
                     membership="Inclusive raw usage EID interval; action ownership is metadata only.")
    else:
        scope["selectedEventCount"] = sum(1 for eid in members if _real_endpoint(actions, eid))
    return scope, members if parent is not None else None


def _access(name):
    if name in READ_USAGES:
        return "read"
    if name in WRITE_USAGES:
        return "write"
    if name in READ_WRITE_USAGES:
        return "readWrite"
    return {"Discard": "discard", "Barrier": "barrier", "Unused": "unused"}.get(name, "unknown")


def _observe(controller, handle, check):
    check()
    _check_replay_health(controller)
    native_rows = controller.GetUsage(handle)
    _check_replay_health(controller)
    check()
    if len(native_rows) > MAX_RESOURCE_ROWS:
        raise AgentBridgeError("LIMIT_EXCEEDED", "One resource exceeds the 100000-usage-record budget.")
    rows, sentinels, byte_count = [], 0, 0
    for index, raw in enumerate(native_rows):
        if index % 256 == 0:
            check()
        eid, usage = getattr(raw, "eventId", None), getattr(raw, "usage", None)
        if type(eid) is not int or eid < 0 or eid > NO_CHUNK or isinstance(usage, (bool, float, str)):
            raise AgentBridgeError("REPLAY_ERROR", "GetUsage returned a malformed event or usage enum.")
        try:
            number = int(usage)
        except (TypeError, ValueError, OverflowError):
            raise AgentBridgeError("REPLAY_ERROR", "GetUsage returned a non-integer usage enum.")
        if number < 0 or number > NO_CHUNK:
            raise AgentBridgeError("REPLAY_ERROR", "GetUsage returned an out-of-range usage enum.")
        name = enum_name(usage)
        if eid == 0 and name == "Unused":
            sentinels += 1
            continue
        if eid == 0:
            raise AgentBridgeError("REPLAY_ERROR", "GetUsage returned an invalid zero usage EID.")
        row = {"eventId": eid, "usageValue": number, "access": _access(name)}
        _text(row, "usage", name)
        view = getattr(raw, "viewId", getattr(raw, "view", None))
        if view is not None:
            key = resource_id(view)
            if key is not None:
                if not key.isdigit() or int(key) > (1 << 64) - 1:
                    raise AgentBridgeError("REPLAY_ERROR", "GetUsage returned an invalid view ResourceId.")
                row["viewId"] = key
        byte_count += _size(row) + 2
        if byte_count > MAX_CACHE_BYTES:
            raise AgentBridgeError("LIMIT_EXCEEDED", "One resource exceeds the 64 MiB usage-cache budget.")
        rows.append(row)
    rows.sort(key=lambda item: (item["eventId"], item["usageValue"], item.get("viewId", "")))
    check()
    return {"rows": rows, "unusedSentinelCount": sentinels}, byte_count + 128


def _in_scope(row, scope, members):
    if scope["kind"] == "capture":
        return True
    eid = row["eventId"]
    if not scope["startEventId"] <= eid <= scope["endEventId"]:
        return False
    return members is None or eid in members


def _diagnostics(record, scope, members, actions, check):
    unknown, unmapped = 0, 0
    for index, row in enumerate(record["rows"]):
        if index % 256 == 0:
            check()
        selected = _in_scope(row, scope, members)
        if selected and row["access"] == "unknown":
            unknown += 1
        # In subtree mode an orphan inside the bounds may have been omitted from the membership
        # set. Report that gap instead of guessing ownership from the nearest action.
        relevant = selected or (members is not None and
                                scope["startEventId"] <= row["eventId"] <= scope["endEventId"])
        if relevant and row["eventId"] not in actions.owner:
            unmapped += 1
    return {"unknownUsageRecordCount": unknown, "unmappedUsageRecordCount": unmapped,
            "malformedUsageRecordCount": 0, "unusedSentinelCount": record["unusedSentinelCount"]}


def _example(row, actions):
    item = dict(row)
    item["actionEventId"] = actions.owner.get(row["eventId"])
    return item


def _neighbour(rows, eid, actions):
    if eid is None:
        return None
    selected = [row for row in rows if row["eventId"] == eid]
    return {"eventId": eid, "recordCount": len(selected),
            "usages": [_example(row, actions) for row in selected[:MAX_EXAMPLES]],
            "truncated": len(selected) > MAX_EXAMPLES}


def _project(record, resource, scope, members, actions, check):
    selected, previous, following, groups = [], None, None, {}
    for index, row in enumerate(record["rows"]):
        if index % 256 == 0:
            check()
        eid = row["eventId"]
        if scope["kind"] != "capture":
            if eid < scope["startEventId"]:
                previous = eid
            elif eid > scope["endEventId"] and following is None:
                following = eid
        if not _in_scope(row, scope, members):
            continue
        selected.append(row)
        group_key = (row["usageValue"], row["usage"])
        group = groups.get(group_key)
        if group is None:
            if len(groups) >= MAX_USAGE_TYPES:
                raise AgentBridgeError("LIMIT_EXCEEDED", "A resource has more than 64 usage types "
                                       "in this scope; narrow the event range.")
            group = {key: value for key, value in row.items() if key.startswith("usage") or key == "access"}
            group.update(count=0, firstEventId=eid, lastEventId=eid)
            groups[group_key] = group
        group["count"] += 1
        group["lastEventId"] = eid
    if not selected:
        return None
    usages = sorted(groups.values(), key=lambda group: (group["usageValue"], group["usage"]))
    row = {"resourceId": resource["resourceId"], "kind": resource["kind"],
           "usageRecordCount": len(selected), "eventCount": len(set(item["eventId"] for item in selected)),
           "firstEventId": selected[0]["eventId"], "lastEventId": selected[-1]["eventId"],
           "hasReadUsage": any(item["access"] in ("read", "readWrite") for item in usages),
           "hasWriteUsage": any(item["access"] in ("write", "readWrite") for item in usages),
           "usageTypes": usages,
           "usageExamples": [_example(item, actions) for item in selected[:MAX_EXAMPLES]],
           "usageExamplesTruncated": len(selected) > MAX_EXAMPLES,
           "previousUsage": _neighbour(record["rows"], previous, actions),
           "nextUsage": _neighbour(record["rows"], following, actions)}
    _text(row, "name", resource["name"])
    _text(row, "type", resource["type"])
    return row


def get_pass_resources(runtime, context):
    parent, start, end, kind, limit, budget, cursor, refresh = _request(context.params)
    check = lambda: _check(runtime, context)
    check()
    actions = action_index(runtime, context)
    scope, members = _scope(actions, parent, start, end, check)
    resources = resource_index(runtime, context)
    candidates = []
    for index, (key, item) in enumerate(resources.items.items()):
        if index % 256 == 0:
            check()
        if item["kind"] in ("texture", "buffer") and (kind == "all" or item["kind"] == kind):
            if len(candidates) >= MAX_RESOURCES:
                raise AgentBridgeError("LIMIT_EXCEEDED", "The scope exceeds the 100000-resource budget.")
            candidates.append(key)
    candidates.sort(key=int)
    session = session_of(context)
    holder = runtime.cache.get(session, CACHE_NAMESPACE, lambda: {"cache": _new_cache()})
    cache = _new_cache() if refresh else holder["cache"]
    query = query_key(session, cache["scanId"], parent, start, end, kind)
    offset = decode_cursor(cursor, "get_pass_resources", session, query)
    if offset > len(candidates):
        raise invalid("The resource cursor is beyond the candidate list.")
    pending = {}
    progress = {"position": offset, "examined": 0, "native": 0, "hits": 0, "bytes": 0,
                "pageBytes": 8192, "rows": []}

    def advance(controller=None):
        while (progress["position"] < len(candidates) and progress["examined"] < budget and
               len(progress["rows"]) < limit):
            check()
            key = candidates[progress["position"]]
            record = cache["resources"].get(key)
            if record is None:
                if controller is None:
                    return True
                record, size = _observe(controller, resources.handle(key), check)
                if cache["bytes"] + progress["bytes"] + size > MAX_CACHE_BYTES:
                    raise AgentBridgeError("LIMIT_EXCEEDED", "The shared usage cache exceeds 64 MiB; "
                                           "refresh to release earlier observations.")
                pending[key] = record
                progress["bytes"] += size
                progress["native"] += 1
            else:
                progress["hits"] += 1
            progress["examined"] += 1
            row = _project(record, resources.items[key], scope, members, actions, check)
            if row is not None:
                size = _size(row) + 32
                if progress["pageBytes"] + size > MAX_PAGE_BYTES:
                    if not progress["rows"]:
                        raise AgentBridgeError("LIMIT_EXCEEDED", "A resource row exceeds the 256 KiB page budget.")
                    break
                progress["rows"].append(row)
                progress["pageBytes"] += size
            progress["position"] += 1
        if controller is not None:
            _check_replay_health(controller)
        check()
        return False

    if advance():
        runtime.replay.call(context, advance)
    check()
    end_offset = progress["position"]
    coverage = {"candidateResourceCount": len(candidates), "processedResourceCount": end_offset,
                "examinedThisPage": progress["examined"], "nativeReadsThisPage": progress["native"],
                "cacheHitsThisPage": progress["hits"], "scanComplete": end_offset == len(candidates),
                "unknownUsageRecordCount": 0, "unmappedUsageRecordCount": 0,
                "malformedUsageRecordCount": 0, "unusedSentinelCount": 0}
    # Recompute diagnostics over the bounded consumed prefix. This keeps cursors stateless and
    # prevents the last page from hiding gaps encountered on earlier pages.
    for index in range(end_offset):
        check()
        key = candidates[index]
        record = pending.get(key, cache["resources"].get(key))
        if record is None:
            raise invalid("The cursor refers to resource observations that are no longer available.")
        for name, count in _diagnostics(record, scope, members, actions, check).items():
            coverage[name] += count
    coverage["complete"] = (coverage["scanComplete"] and not coverage["unknownUsageRecordCount"] and
                            not coverage["unmappedUsageRecordCount"] and not coverage["malformedUsageRecordCount"])
    next_cursor = (encode_cursor("get_pass_resources", session, query, end_offset)
                   if end_offset < len(candidates) else None)
    data = {"scope": scope, "kind": kind, "scanId": cache["scanId"],
            "scanStartedAtUtc": cache["scanStartedAtUtc"], "resources": progress["rows"], "coverage": coverage}
    truncated_examples = any(row["usageExamplesTruncated"] or any(
        row[name] is not None and row[name]["truncated"] for name in ("previousUsage", "nextUsage"))
        for row in progress["rows"])
    text_truncated = any(row.get("nameTruncated", False) or row.get("typeTruncated", False) or
                         any(item.get("usageTruncated", False) for item in row["usageTypes"]) or
                         any(row[name] is not None and any(item.get("usageTruncated", False)
                             for item in row[name]["usages"]) for name in ("previousUsage", "nextUsage"))
                         for row in progress["rows"])
    warnings = ["GetUsage reports resource roles/potential access, not actual pixel or byte accesses. "
                "DepthStencilTarget may only test/read; ColorTarget does not prove a write occurred. "
                "Read/write roles do not establish external pass inputs, outputs or dependencies. "
                "Duplicate native records are retained; counts are not GPU access counts. "
                "previousUsage/nextUsage are EID neighbours, not producers, consumers or last writers. "
                "Read every nextCursor, including after empty pages; scan_budget bounds candidate "
                "resources (including cache hits), not native records within one GetUsage call."]
    if coverage["unknownUsageRecordCount"] or coverage["unmappedUsageRecordCount"]:
        warnings.append("Some native usage roles or event ownership are unknown; metadata coverage is incomplete.")
    if truncated_examples or text_truncated:
        warnings.append("Usage examples or text labels were truncated; aggregate native record counts are retained.")
    response = result(data, warnings=warnings, next_cursor=next_cursor,
                      truncated=next_cursor is not None or not coverage["complete"] or
                      truncated_examples or text_truncated)
    if _size(response.result) > MAX_PAGE_BYTES:
        raise AgentBridgeError("LIMIT_EXCEEDED", "The resource response exceeds the 256 KiB page budget.")
    check()
    # Commit after projection, cumulative diagnostics, response budgets and lifecycle checks all
    # succeed. Refresh failures leave both prior observations and their cursors intact.
    cache["resources"].update(pending)
    cache["bytes"] += progress["bytes"]
    holder["cache"] = cache
    return response


HANDLERS = {"get_pass_resources": get_pass_resources}
