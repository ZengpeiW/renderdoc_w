"""Available GPU counters and capture-wide EventGPUDuration replay samples.

Sampling is intentionally limited to the built-in duration counter. Scope, sort and pagination are
projections of one completed capture-wide sample, never hints that reduce native FetchCounters work.
"""

import datetime
import json
import math
import uuid

from ..ipc import AgentBridgeError
from ..paging import decode_cursor, encode_cursor, query_key
from ..replay import _check_replay_health
from ..values import enum_name
from .common import (action_index, get_bool, get_choice, get_int, get_str, invalid,
                     renderdoc_module, result, session_of)

UINT32_MAX = (1 << 32) - 1
MAX_PAGE_BYTES = 256 * 1024
MAX_NAME_CHARS = 256
MAX_DESCRIPTION_CHARS = 2048
EVIDENCE_LIMIT = 16
MAX_CATALOG_COUNTERS = 65536
MAX_DESCRIPTION_CACHE_BYTES = 32 * 1024 * 1024
MAX_NATIVE_RESULTS = 1000000
MAX_SAMPLE_BYTES = 64 * 1024 * 1024
_WARNING = ("Durations are GPU timestamp measurements during RenderDoc replay, not the original "
            "application's frame time. They are not additive guarantees across events, queues or "
            "overlapping work; native alias events can reuse the same measurement. Do not sum "
            "these values as frame wall time. A native zero is retained as reported; missing does not mean zero "
            "or no execution, and markers/containers may not be measured. Scope, sorting and "
            "pagination do not reduce the capture-wide native sampling work.")


def _paging(params):
    limit = get_int(params, "limit", 200, minimum=1, maximum=1000)
    if limit is None:
        raise invalid("limit must be an integer.")
    cursor = get_str(params, "cursor", max_length=1024)
    if cursor == "":
        raise invalid("The cursor is invalid.")
    return limit, cursor


def _guard(runtime, context):
    context.check_cancelled()
    if session_of(context) != (runtime.state.snapshot().get("captureSessionId") or ""):
        raise AgentBridgeError("CAPTURE_CHANGED", "The capture changed during GPU counter work.")


def _state(runtime, context):
    return runtime.cache.get(session_of(context), "gpu-counters-v1",
                             lambda: {"catalog": None, "descriptions": {},
                                      "descriptionBytes": 0, "sample": None})


def _counter_id():
    rd = renderdoc_module()
    value = getattr(getattr(rd, "GPUCounter", None), "EventGPUDuration", None)
    if value is None:
        raise AgentBridgeError("UNSUPPORTED", "The native API has no EventGPUDuration counter.")
    return int(value), value


def _text(row, key, value, limit):
    value = str(value)
    preview = value[:limit]
    row[key] = preview.encode("utf-8", "backslashreplace").decode("utf-8")
    if row[key] != preview:
        row[key + "EscapedInvalidUnicode"] = True
    if len(value) > limit:
        row[key + "Truncated"] = True
        row[key + "Length"] = len(value)


def _catalog(controller, holder, context):
    if holder["catalog"] is None:
        enumerate_counters = getattr(controller, "EnumerateCounters", None)
        if enumerate_counters is None:
            raise AgentBridgeError("UNSUPPORTED", "Native GPU counter discovery is unavailable.")
        tokens = {}
        duplicates = 0
        for position, counter in enumerate(enumerate_counters()):
            if position >= MAX_CATALOG_COUNTERS:
                raise AgentBridgeError("LIMIT_EXCEEDED", "The native counter catalog exceeds "
                                       "the %d-entry budget." % MAX_CATALOG_COUNTERS)
            if position % 256 == 0:
                context.check_cancelled()
            counter_id = int(counter)
            if counter_id < 0 or counter_id > UINT32_MAX:
                raise AgentBridgeError("REPLAY_ERROR", "The native counter catalog contains an "
                                       "invalid counter ID.")
            if counter_id in tokens:
                duplicates += 1
            tokens[counter_id] = counter
        context.check_cancelled()
        holder["catalog"] = {"ids": sorted(tokens), "tokens": tokens, "duplicateIds": duplicates}
    return holder["catalog"]


def _description(controller, holder, counter_id, duration_id):
    if counter_id not in holder["descriptions"]:
        describe_counter = getattr(controller, "DescribeCounter", None)
        if describe_counter is None:
            raise AgentBridgeError("UNSUPPORTED", "Native GPU counter descriptions are unavailable.")
        native = describe_counter(holder["catalog"]["tokens"][counter_id])
        if int(native.counter) != counter_id:
            raise AgentBridgeError("REPLAY_ERROR", "The native counter description has a different "
                                   "counter ID.", details={"counterId": counter_id})
        row = {"id": counter_id, "resultType": enum_name(native.resultType),
               "resultByteWidth": int(native.resultByteWidth), "unit": enum_name(native.unit),
               "uuid": [int(word) for word in native.uuid.words]}
        _text(row, "name", native.name, MAX_NAME_CHARS)
        _text(row, "category", native.category, MAX_NAME_CHARS)
        _text(row, "description", native.description, MAX_DESCRIPTION_CHARS)
        row["durationSamplingSupported"] = (
            counter_id == duration_id and row["resultType"] == "Float" and
            row["resultByteWidth"] == 8 and row["unit"] == "Seconds")
        size = _size(row) + 64
        if holder["descriptionBytes"] + size > MAX_DESCRIPTION_CACHE_BYTES:
            raise AgentBridgeError("LIMIT_EXCEEDED", "Counter descriptions exceed the cache budget.")
        holder["descriptions"][counter_id] = row
        holder["descriptionBytes"] += size
    return dict(holder["descriptions"][counter_id])


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def _page(items, offset, limit, build):
    if offset > len(items):
        raise invalid("The cursor is beyond the result list.")
    rows = []
    available_bytes = MAX_PAGE_BYTES - 8192
    end = offset
    for index in range(offset, min(len(items), offset + limit)):
        row = build(items[index])
        size = _size(row) + 32
        if size > available_bytes:
            if not rows:
                raise AgentBridgeError("LIMIT_EXCEEDED", "A GPU counter row exceeds the page budget.")
            break
        rows.append(row)
        available_bytes -= size
        end = index + 1
    return rows, end


def _strings_truncated(rows):
    return any(any(row.get(key, False) for key in
                   ("nameTruncated", "categoryTruncated", "descriptionTruncated")) for row in rows)


def list_gpu_counters(runtime, context):
    limit, cursor = _paging(context.params)
    session = session_of(context)
    key = query_key(session, "gpu-counter-catalog-v1")
    offset = decode_cursor(cursor, "list_gpu_counters", session, key)
    duration_id, _ = _counter_id()
    holder = _state(runtime, context)

    def read(controller):
        catalog = _catalog(controller, holder, context)
        def describe(counter_id):
            context.check_cancelled()
            return _description(controller, holder, counter_id, duration_id)
        rows, end = _page(catalog["ids"], offset, limit, describe)
        return rows, end, catalog

    rows, end, catalog = runtime.replay.call(context, read)
    _guard(runtime, context)
    next_cursor = (encode_cursor("list_gpu_counters", session, key, end)
                   if end < len(catalog["ids"]) else None)
    return result({"counters": rows, "total": len(catalog["ids"]),
                   "duplicateCounterIds": catalog["duplicateIds"],
                   "samplingPolicy": "Only EventGPUDuration with Float/8-byte/Seconds metadata "
                                     "is sampled by get_gpu_duration. Availability does not "
                                     "guarantee a successful measurement."},
                  next_cursor=next_cursor, truncated=next_cursor is not None or _strings_truncated(rows))


def _raw_duration(value):
    if not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    return value


def _normalise(native_results, actions, duration_id, context):
    measured = {}
    sample_bytes = 4096
    diagnostics = {"rawResultCount": 0, "unknownEventResults": 0, "unknownEventIds": [],
                   "counterMismatchResults": 0, "malformedResults": 0,
                   "duplicateResultCount": 0}
    for position, native in enumerate(native_results):
        if position >= MAX_NATIVE_RESULTS:
            raise AgentBridgeError("LIMIT_EXCEEDED", "Native counter results exceed the %d-row "
                                   "budget." % MAX_NATIVE_RESULTS)
        if position % 256 == 0:
            context.check_cancelled()
        diagnostics["rawResultCount"] += 1
        try:
            counter = int(native.counter)
            event_id = int(native.eventId)
        except (AttributeError, TypeError, ValueError, OverflowError):
            diagnostics["malformedResults"] += 1
            continue
        if counter != duration_id:
            diagnostics["counterMismatchResults"] += 1
            continue
        if event_id not in actions.nodes:
            diagnostics["unknownEventResults"] += 1
            if (len(diagnostics["unknownEventIds"]) < EVIDENCE_LIMIT and
                    event_id not in diagnostics["unknownEventIds"]):
                diagnostics["unknownEventIds"].append(event_id)
            continue
        try:
            seconds = float(native.value.d)
            raw = _raw_duration(seconds)
        except (AttributeError, TypeError, ValueError, OverflowError):
            seconds, raw = None, "unreadable"
        milliseconds = seconds * 1000.0 if seconds is not None else None
        valid = (seconds is not None and math.isfinite(seconds) and seconds >= 0 and
                 math.isfinite(milliseconds))
        value = {"status": "valid" if valid else "invalid",
                 "durationSeconds": seconds if valid else None,
                 "durationMs": milliseconds if valid else None}
        if not valid:
            value.update(reason="The native duration is unreadable, negative, non-finite, or "
                                "cannot be represented in milliseconds.", rawDuration=raw)
        previous = measured.get(event_id)
        if previous is not None:
            diagnostics["duplicateResultCount"] += 1
            evidence = list(previous.get("rawDurations", [previous.get("rawDuration",
                                                                       previous["durationSeconds"])]))
            if len(evidence) < 4:
                evidence.append(raw)
            value = {"status": "duplicate", "durationSeconds": None, "durationMs": None,
                     "reason": "Multiple native duration results have this exact EID; no value "
                               "was selected or added.", "rawDurations": evidence,
                     "nativeResultCount": previous.get("nativeResultCount", 1) + 1}
        sample_bytes += _size(value) + 64
        if previous is not None:
            sample_bytes -= _size(previous) + 64
        if sample_bytes > MAX_SAMPLE_BYTES:
            raise AgentBridgeError("LIMIT_EXCEEDED", "The normalised duration sample exceeds "
                                   "the 64 MiB cache budget.")
        measured[event_id] = value
    context.check_cancelled()
    return measured, diagnostics


def _sample(runtime, context, holder, actions, duration_id, native_counter):
    def read(controller):
        catalog = _catalog(controller, holder, context)
        if duration_id not in catalog["ids"]:
            raise AgentBridgeError("UNSUPPORTED", "EventGPUDuration is not available in this capture.")
        descriptor = _description(controller, holder, duration_id, duration_id)
        if not descriptor["durationSamplingSupported"]:
            raise AgentBridgeError("UNSUPPORTED", "EventGPUDuration does not describe an 8-byte "
                                   "floating-point result in seconds.", details={"counter": descriptor})
        fetch = getattr(controller, "FetchCounters", None)
        if fetch is None:
            raise AgentBridgeError("UNSUPPORTED", "Native GPU counter sampling is unavailable.")
        context.check_cancelled()
        native = fetch([native_counter])
        _check_replay_health(controller)
        _guard(runtime, context)
        measured, diagnostics = _normalise(native, actions, duration_id, context)
        return {"sampleId": uuid.uuid4().hex,
                "sampledAtUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z"),
                "measured": measured, "diagnostics": diagnostics}

    sample, meta = runtime.replay.call_restoring(context, read)
    # A failed/abandoned collection or forced restoration must not replace the last good sample.
    _guard(runtime, context)
    holder["sample"] = sample
    return sample, meta


def _scope(actions, parent, context):
    roots = actions.roots if parent is None else [parent]
    ids = []
    stack = list(reversed(roots))
    while stack:
        if len(ids) % 256 == 0:
            context.check_cancelled()
        event_id = stack.pop()
        ids.append(event_id)
        stack.extend(reversed(actions.nodes[event_id]["children"]))
    return ids


def get_gpu_duration(runtime, context):
    params = context.params
    limit, cursor = _paging(params)
    parent = get_int(params, "parent_event_id", maximum=UINT32_MAX)
    sort_by = get_choice(params, "sort_by", ("duration", "event_id"), "duration")
    if sort_by is None:
        raise invalid("sort_by must be duration or event_id.")
    refresh = get_bool(params, "refresh", False)
    if refresh and cursor is not None:
        raise invalid("refresh cannot be combined with a cursor; start a new sample's first page.")
    actions = action_index(runtime, context)
    if parent is not None and parent not in actions.nodes:
        raise AgentBridgeError("INVALID_EVENT", "parent_event_id must identify an exact action, "
                               "not an API event owned by another action.", details={"event_id": parent})
    session = session_of(context)
    holder = _state(runtime, context)
    sample = holder["sample"]
    if cursor is not None and sample is None:
        raise invalid("The sample for this cursor is no longer cached; request a new first page.")
    duration_id, native_counter = _counter_id()
    cached = sample is not None and not refresh
    meta = {}
    if not cached:
        sample, meta = _sample(runtime, context, holder, actions, duration_id, native_counter)
    key = query_key(session, sample["sampleId"], parent, sort_by)
    offset = decode_cursor(cursor, "get_gpu_duration", session, key)
    ids = _scope(actions, parent, context)
    measured = sample["measured"]
    counts = dict((status, 0) for status in ("valid", "invalid", "missing", "duplicate"))
    for event_id in ids:
        counts[measured.get(event_id, {}).get("status", "missing")] += 1
    if sort_by == "event_id":
        ids.sort()
    else:
        def ranking(event_id):
            value = measured.get(event_id, {})
            valid = value.get("status") == "valid"
            return (0 if valid else 1, -value["durationSeconds"] if valid else 0, event_id)
        ids.sort(key=ranking)

    def row(event_id):
        context.check_cancelled()
        node = actions.nodes[event_id]
        value = {"eventId": event_id, "flags": list(node["flags"])}
        _text(value, "name", node["name"], MAX_NAME_CHARS)
        value.update(measured.get(event_id, {"status": "missing", "durationSeconds": None,
                                            "durationMs": None, "reason": "No native duration "
                                            "result was returned for this action; markers and "
                                            "containers may not be measured."}))
        return value

    rows, end = _page(ids, offset, limit, row)
    _guard(runtime, context)
    next_cursor = (encode_cursor("get_gpu_duration", session, key, end) if end < len(ids) else None)
    data = {"counterId": duration_id, "counterName": "EventGPUDuration",
            "sampleId": sample["sampleId"], "sampledAtUtc": sample["sampledAtUtc"],
            "cached": cached, "samplingScope": "capture", "parentEventId": parent,
            "sortBy": sort_by, "scopeActionCount": len(ids), "events": rows,
            "validDurationCount": counts["valid"], "invalidDurationCount": counts["invalid"],
            "missingDurationCount": counts["missing"], "duplicateDurationCount": counts["duplicate"],
            "diagnostics": dict(sample["diagnostics"])}
    warnings = [_WARNING]
    if any(sample["diagnostics"][name] for name in
           ("unknownEventResults", "counterMismatchResults", "malformedResults", "duplicateResultCount")):
        warnings.append("Some native results were quarantined or ambiguous; see diagnostics. "
                        "Non-action EIDs were not mapped to their owning actions.")
    if counts["invalid"]:
        warnings.append("Some native duration values are invalid and were retained as null, not zero.")
    if not sample["diagnostics"]["rawResultCount"]:
        warnings.append("The native sampler returned no results; all scoped actions are missing, "
                        "and no duration or performance conclusion is available.")
    return result(data, meta=meta, warnings=warnings, next_cursor=next_cursor,
                  truncated=next_cursor is not None or _strings_truncated(rows))


HANDLERS = {"list_gpu_counters": list_gpu_counters, "get_gpu_duration": get_gpu_duration}
