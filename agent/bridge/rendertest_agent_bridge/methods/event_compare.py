"""Bounded, capture-local comparisons of replay state at two exact API events.

The cache holds at most two bounded diff sets, never native replay objects. Continuation cursors
refer to that specific snapshot and fail closed after eviction instead of silently mixing reads.
"""

from collections import OrderedDict
import json
import math
import time
import uuid

from ..ipc import AgentBridgeError
from ..paging import decode_cursor, encode_cursor, query_key
from ..values import resource_id
from . import cbuffer_members, pipeline
from .common import (STAGE_NAMES, action_index, get_bool, get_choice, get_choices, get_int,
                     get_str, invalid, renderdoc_module, resource_index, result, session_of,
                     stage_enum)

MAX_SELECTIONS = 8
MAX_BLOCK_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_NODES = 32768
MAX_STRING_BYTES = 16 * 1024
MAX_DIFFERENCES = 8192
MAX_DIFF_BYTES = 2 * 1024 * 1024
MAX_PAGE_BYTES = 256 * 1024
MAX_ISSUES = 32
MAX_POINTER_BYTES = 4096
CACHE_ENTRIES = 2
_MISSING = object()
_UNKNOWN = object()


class _Sequence(list):
    def __init__(self, values, complete=True):
        super(_Sequence, self).__init__(values)
        self.complete = complete


def _path(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8"))


def _check(runtime, context):
    context.check_cancelled()
    if time.time() > context.deadline_at:
        raise AgentBridgeError("TIMEOUT", "The event comparison deadline has expired.")
    if runtime.state.snapshot().get("captureSessionId") != session_of(context):
        raise AgentBridgeError("CAPTURE_CHANGED", "The capture changed during event comparison.")


def _issue(coverage, path, reason):
    coverage["complete"] = False
    coverage["issueCount"] += 1
    if len(coverage["issues"]) < MAX_ISSUES:
        coverage["issues"].append({"path": path[:128], "pathTruncated": len(path) > 128,
                                   "reason": reason})


def _selections(params):
    selections = params.get("cbuffers")
    if selections is None:
        return []
    if not isinstance(selections, list) or len(selections) > MAX_SELECTIONS:
        raise invalid("cbuffers must be a list of at most %d selections." % MAX_SELECTIONS)
    output = []
    allowed = {"stage", "index", "name", "member_path", "max_depth", "member_limit"}
    for value in selections:
        if not isinstance(value, dict) or set(value) - allowed:
            raise invalid("Each cbuffer selection must contain only supported selection fields.")
        stage = get_choice(value, "stage", STAGE_NAMES, required=True)
        index = get_int(value, "index")
        name = get_str(value, "name")
        if (index is None) == (name is None):
            raise invalid("Each cbuffer selection requires exactly one of index or name.")
        member_path = get_str(value, "member_path", "", max_length=4096, required=True)
        depth = get_int(value, "max_depth", 1, maximum=4, required=True)
        limit = get_int(value, "member_limit", 200, minimum=1, maximum=1000, required=True)
        request = cbuffer_members.validate_request(
            {"member_path": member_path, "max_depth": depth, "limit": limit})
        output.append({"stage": stage, "index": index, "name": name,
                       "member_path": member_path, "max_depth": depth,
                       "member_limit": limit, "request": request})
    return output


def _expected_children(constant_type, element):
    if constant_type is None:
        return None
    if int(constant_type.elements) > 1 and not element:
        return int(constant_type.elements)
    return len(constant_type.members)


def _member_exists(variables, block, tokens, check):
    """Known absence is comparable; ambiguous names remain an invalid request."""
    reference = None
    for token in tokens:
        check()
        children = cbuffer_members._children(reference, variables)
        expected = (len(block.variables) if reference is None else
                    _expected_children(reference["type"], reference["element"]))
        if expected is not None and expected != len(children):
            return _UNKNOWN
        position = (cbuffer_members._unique_named(children, token, check)
                    if isinstance(token, str) else token)
        if position is None or position >= len(children):
            if reference is not None and reference["type"] is None:
                return _UNKNOWN
            reflected = (block.variables if reference is None else
                         (reference["type"].members if reference["type"] is not None else []))
            if isinstance(token, str) and any(item.name == token for item in reflected):
                return _UNKNOWN
            return False
        reference = cbuffer_members._child(reference, variables, block, position, check)
    if reference is not None:
        expected = _expected_children(reference["type"], reference["element"])
        if expected is not None and expected != len(reference["variable"].members):
            return _UNKNOWN
    return True


def _projected_variable(item, path, coverage):
    """Preserve unknown member tails, rather than treating them as deleted members."""
    item = dict(item)
    if item.pop("_decodeIncomplete", False):
        _issue(coverage, path, "constant-member-decode-incomplete")
        return _UNKNOWN
    count = item.get("childCount", 0)
    omitted = item.pop("membersTruncated", False)
    if count:
        children = item.get("members", [])
        item["members"] = _Sequence([
            _projected_variable(child, _path(_path(path, "members"), index), coverage)
            for index, child in enumerate(children)], not omitted and len(children) == count)
    elif "value" not in item:
        # Empty aggregates are known, but an undecoded scalar must not compare as equal.
        if item.get("type") not in ("Struct",):
            _issue(coverage, _path(path, "value"), "unsupported-member-value")
            item["value"] = _UNKNOWN
    if "typeName" not in item or "byteOffset" not in item:
        _issue(coverage, path, "member-layout-unavailable")
    return item


def _read_cbuffer(controller, pipe, selection, resources, context, coverage, path, decoded,
                  check):
    stage_name = selection["stage"]
    stage = stage_enum(renderdoc_module(), stage_name)
    shader = pipe.GetShader(stage)
    shader_key = resource_id(shader)
    if shader_key is None:
        return _MISSING
    reflection = pipe.GetShaderReflection(stage)
    if reflection is None:
        _issue(coverage, path, "shader-reflection-unavailable")
        return _UNKNOWN
    blocks = reflection.constantBlocks
    position = selection["index"]
    if position is None:
        position = cbuffer_members._unique_named(blocks, selection["name"], check)
    if position is None or position >= len(blocks):
        return _MISSING
    block = blocks[position]
    descriptor = pipe.GetConstantBlock(stage, position, 0).descriptor
    buffer_key = resource_id(descriptor.resource)
    data = {"stage": stage_name, "shader": resources.ref(shader_key), "index": position,
            "name": block.name, "byteSize": int(block.byteSize),
            "bufferBacked": bool(block.bufferBacked), "bindPoint": int(block.fixedBindNumber),
            "space": int(block.fixedBindSetOrSpace), "bindArraySize": int(block.bindArraySize),
            "binding": {"resourceId": buffer_key, "byteOffset": int(descriptor.byteOffset),
                        "byteSize": int(descriptor.byteSize)}}
    projection_path = _path(path, "projection")
    if int(block.byteSize) < 0 or int(block.byteSize) > MAX_BLOCK_BYTES:
        _issue(coverage, projection_path, "constant-block-size-budget")
        data["projection"] = _UNKNOWN
        return data
    if block.bufferBacked and buffer_key is None:
        _issue(coverage, projection_path, "constant-buffer-unbound")
        data["projection"] = _UNKNOWN
        return data
    block_bytes = data["byteSize"]
    binding_bytes = data["binding"]["byteSize"]
    if block.bufferBacked and (binding_bytes <= 0 or binding_bytes < block_bytes or
                               (block_bytes == 0 and block.variables)):
        # Native decoding can fill unread bytes with zero. Treat a short/empty range as unknown
        # instead of comparing these synthetic values, even for a narrowly selected member.
        _issue(coverage, projection_path, "constant-buffer-range-incomplete")
        data["projection"] = _UNKNOWN
        return data
    # ReplayController reads `length` bytes before interpreting reflection. The descriptor range
    # can cover a whole buffer, so restricting only block.byteSize would not bound that read.
    # Push/root constants are fetched by FillCBufferVariables and need no backing-buffer read.
    read_length = min(block_bytes, binding_bytes) if block.bufferBacked else 0
    cache_key = (stage_name, position)
    if cache_key not in decoded:
        check()
        decoded[cache_key] = controller.GetCBufferVariableContents(
            pipeline._pipeline_object(pipe, stage_name), shader, stage,
            pipe.GetShaderEntryPoint(stage), position, descriptor.resource,
            descriptor.byteOffset, read_length)
    variables = decoded[cache_key]
    check()
    if len(variables) != len(block.variables):
        _issue(coverage, projection_path, "constant-buffer-decode-incomplete")
        data["projection"] = _UNKNOWN
        return data
    request = selection["request"]
    exists = _member_exists(variables, block, request["tokens"], check)
    if exists is _UNKNOWN:
        _issue(coverage, projection_path, "constant-member-decode-incomplete")
        data["projection"] = _UNKNOWN
        return data
    if not exists:
        data["projection"] = _MISSING
        return data
    scope = (session_of(context), stage_name, shader_key, position, buffer_key,
             int(descriptor.byteOffset), int(descriptor.byteSize))
    def convert(variable, budget, constant_type=None, offset=None, element=False):
        item = pipeline.convert_variable(variable, budget, constant_type, offset, element)
        expected = _expected_children(constant_type, element)
        if expected is not None and expected != len(variable.members):
            item["_decodeIncomplete"] = True
        return item

    try:
        projected, next_cursor, truncated, _ = cbuffer_members.project_members(
            variables, block, request, scope, convert, check_cancelled=check)
    except AgentBridgeError as error:
        if error.code != "LIMIT_EXCEEDED":
            raise
        _issue(coverage, projection_path, "constant-member-response-budget")
        data["projection"] = _UNKNOWN
        return data
    if truncated:
        _issue(coverage, projection_path, "constant-members-truncated")
    selected = projected["selected"]
    if selected is not None:
        # Selected aggregates are intentionally shallow; their children are in variables.
        selected = dict(selected)
        selected.pop("membersTruncated", None)
        if not selected.get("childCount"):
            selected = _projected_variable(selected, _path(projection_path, "selected"), coverage)
        elif "typeName" not in selected or "byteOffset" not in selected:
            _issue(coverage, _path(projection_path, "selected"), "member-layout-unavailable")
        projected["selected"] = selected
    projected["variables"] = _Sequence([
        _projected_variable(item, _path(_path(projection_path, "variables"), index), coverage)
        for index, item in enumerate(projected["variables"])], next_cursor is None)
    data["projection"] = projected
    return data


def _pipeline_coverage(state, include_bindings, coverage):
    for stage, shader in state["shaders"].items():
        if "encoding" not in shader:
            _issue(coverage, "/pipeline/shaders/" + stage, "shader-reflection-unavailable")
            shader["encoding"] = _UNKNOWN
        if include_bindings and stage not in state["bindings"]:
            _issue(coverage, "/pipeline/bindings/" + stage, "shader-bindings-unavailable")
            state["bindings"][stage] = _UNKNOWN
    if include_bindings:
        for stage, bindings in state["bindings"].items():
            if bindings is _UNKNOWN:
                continue
            bindings.pop("truncated", None)
            for category, count in bindings["counts"].items():
                values = bindings[category]
                if len(values) < count:
                    _issue(coverage, "/pipeline/bindings/%s/%s" % (stage, category),
                           "bindings-truncated")
                    bindings[category] = _Sequence(values, False)
    assembly = state.get("inputAssembly")
    if assembly and len(assembly["vertexInputs"]) < assembly["vertexInputCount"]:
        _issue(coverage, "/pipeline/inputAssembly/vertexInputs", "vertex-inputs-truncated")
        assembly["vertexInputs"] = _Sequence(assembly["vertexInputs"], False)


class _Normalizer(object):
    def __init__(self, coverage, check):
        self.coverage = coverage
        self.check = check
        self.nodes = MAX_SNAPSHOT_NODES
        self.bytes = MAX_SNAPSHOT_BYTES

    def read(self, value, path="", depth=0):
        self.check()
        if len(path) > MAX_POINTER_BYTES or _bytes(path) > MAX_POINTER_BYTES:
            _issue(self.coverage, path, "snapshot-path-budget")
            return _UNKNOWN
        if value is _MISSING or value is _UNKNOWN:
            return value
        self.nodes -= 1
        if self.nodes < 0 or self.bytes < 0 or depth > 64:
            _issue(self.coverage, path, "snapshot-budget")
            return _UNKNOWN
        if isinstance(value, dict):
            if len(value) > self.nodes or any(type(key) is not str for key in value):
                _issue(self.coverage, path, "snapshot-object-budget-or-key-type")
                return _UNKNOWN
            output = {}
            for key in sorted(value):
                if len(key) > MAX_STRING_BYTES or _bytes(key) > MAX_STRING_BYTES:
                    _issue(self.coverage, path, "snapshot-key-budget")
                    return _UNKNOWN
                self.bytes -= _bytes(key) + 4
                output[key] = self.read(value[key], _path(path, key), depth + 1)
            return output
        if isinstance(value, (list, tuple)):
            count = min(len(value), max(0, self.nodes))
            complete = getattr(value, "complete", True)
            if not complete:
                _issue(self.coverage, path, "omitted-sequence-tail")
            if count < len(value):
                _issue(self.coverage, path, "snapshot-node-budget")
                complete = False
            output = []
            for index in range(count):
                if self.bytes < 0 or self.nodes <= 0:
                    _issue(self.coverage, path, "snapshot-budget")
                    complete = False
                    break
                output.append(self.read(value[index], _path(path, index), depth + 1))
            self.bytes -= 2 + len(output)
            return _Sequence(output, complete)
        if type(value) is float and not math.isfinite(value):
            value = "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
        if value is not None and type(value) not in (str, int, float, bool):
            _issue(self.coverage, path, "unsupported-value-type")
            return _UNKNOWN
        if type(value) is str and len(value) > MAX_STRING_BYTES:
            _issue(self.coverage, path, "snapshot-string-budget")
            return _UNKNOWN
        size = _bytes(value)
        if size > MAX_STRING_BYTES or size > self.bytes:
            _issue(self.coverage, path, "snapshot-value-budget")
            return _UNKNOWN
        self.bytes -= size
        return value


def _snapshot(runtime, context, event_id, node, resources, stages, include_bindings,
              binding_limit, selections):
    coverage = {"eventId": event_id, "ownerActionEventId": node["eventId"],
                "pipelineClass": "compute" if pipeline._is_compute(node) else "graphics",
                "stagesObserved": pipeline._relevant_stages(node, stages),
                "complete": True, "issueCount": 0, "issues": []}
    check = lambda: _check(runtime, context)

    def read(controller):
        check()
        state, _ = pipeline.read_pipeline_state(controller, resources, node, stages,
                                                include_bindings, binding_limit)
        _pipeline_coverage(state, include_bindings, coverage)
        data = {"pipeline": state, "cbuffers": {}}
        decoded = {}
        if selections:
            pipe = controller.GetPipelineState()
            for index, selection in enumerate(selections):
                check()
                data["cbuffers"][str(index)] = _read_cbuffer(
                    controller, pipe, selection, resources, context, coverage,
                    "/cbuffers/%d" % index, decoded, check)
        return _Normalizer(coverage, check).read(data)

    data, meta = runtime.replay.at_event(context, event_id, read)
    check()
    return data, coverage, meta


class _Differ(object):
    def __init__(self, check):
        self.check = check
        self.items = []
        self.bytes = 0
        self.complete = True

    def emit(self, path, before, after, type_change=False):
        def side(value):
            if isinstance(value, (dict, list)) and type_change:
                return {"present": True, "valueType": _kind(value)}
            return {"present": False} if value is _MISSING else {"present": True, "value": value}
        item = {"path": path, "before": side(before), "after": side(after)}
        if type_change:
            item["kind"] = "type-change"
        size = _bytes(item)
        if len(self.items) >= MAX_DIFFERENCES or self.bytes + size > MAX_DIFF_BYTES:
            self.complete = False
            return
        self.items.append(item)
        self.bytes += size

    def walk(self, before, after, path=""):
        self.check()
        if not self.complete or before is _UNKNOWN or after is _UNKNOWN:
            return
        if before is _MISSING and after is _MISSING:
            return
        before_container = isinstance(before, (dict, list))
        after_container = isinstance(after, (dict, list))
        if isinstance(before, dict) or isinstance(after, dict):
            if ((isinstance(before, dict) or before is _MISSING) and
                    (isinstance(after, dict) or after is _MISSING)):
                left = before if before is not _MISSING else {}
                right = after if after is not _MISSING else {}
                keys = sorted(set(left) | set(right))
                if not keys and (before is _MISSING or after is _MISSING):
                    self.emit(path, before, after)
                for key in keys:
                    self.walk(left.get(key, _MISSING), right.get(key, _MISSING), _path(path, key))
                return
        if isinstance(before, list) or isinstance(after, list):
            if ((isinstance(before, list) or before is _MISSING) and
                    (isinstance(after, list) or after is _MISSING)):
                left = before if before is not _MISSING else []
                right = after if after is not _MISSING else []
                count = max(len(left), len(right))
                if not count and (before is _MISSING or after is _MISSING):
                    self.emit(path, before, after)
                for index in range(count):
                    lvalue = (left[index] if index < len(left) else
                              (_MISSING if getattr(left, "complete", True) else _UNKNOWN))
                    rvalue = (right[index] if index < len(right) else
                              (_MISSING if getattr(right, "complete", True) else _UNKNOWN))
                    self.walk(lvalue, rvalue, _path(path, index))
                return
        if before_container or after_container:
            # Type changes are explicit, but large/partial subtrees must not be serialized whole.
            self.emit(path, before, after, type_change=True)
            if before_container:
                self.walk(before, _MISSING, path)
            if after_container:
                self.walk(_MISSING, after, path)
            return
        if type(before) is not type(after) or before != after:
            self.emit(path, before, after)


def _kind(value):
    if value is _MISSING:
        return _MISSING
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    return {str: "string", int: "integer", float: "number", bool: "boolean"}[type(value)]


def compare_events(runtime, context):
    params = context.params
    before_id = get_int(params, "before_event_id", maximum=0xFFFFFFFF, required=True)
    after_id = get_int(params, "after_event_id", maximum=0xFFFFFFFF, required=True)
    stages = get_choices(params, "stages", STAGE_NAMES)
    if stages is not None:
        stages = [stage for stage in STAGE_NAMES if stage in stages]
    include_bindings = get_bool(params, "include_bindings", True)
    binding_limit = get_int(params, "binding_limit", 64, minimum=1, maximum=256, required=True)
    selections = _selections(params)
    cursor = get_str(params, "cursor", max_length=1024)
    if cursor == "":
        raise invalid("The cursor is invalid.")
    limit = get_int(params, "limit", 200, minimum=1, maximum=1000, required=True)
    _check(runtime, context)
    actions = action_index(runtime, context)
    for event_id in (before_id, after_id):
        if not actions.has_event(event_id):
            raise AgentBridgeError("INVALID_EVENT", "Event %d does not exist in the capture." % event_id,
                                   details={"event_id": event_id})
    session = session_of(context)
    scope = {"stages": stages, "includeBindings": include_bindings, "bindingLimit": binding_limit,
             "cbuffers": [{key: value for key, value in item.items() if key != "request"}
                          for item in selections],
             "pipeline": "read_pipeline_state; graphics sections only for graphics actions",
             "viewportIndices": [0], "scissorIndices": [0], "usedDescriptorsOnly": True,
             "constantBlockArrayElement": 0, "listMatching": "positional",
             "stagePolicy": "Requested/default stages intersect owner action class: "
                            "Dispatch/DispatchRay uses compute; other actions use non-compute stages",
             "constantBufferStagePolicy": "Explicit selections are independent of the stages filter",
             "constantBufferValueMeaning": "Decoded values and layout metadata are compared; "
                                          "changed layouts do not imply semantic correspondence",
             "snapshotPolicy": "A request without cursor refreshes this query and invalidates its "
                               "older cursors; only the two latest query snapshots are retained",
             "typeChanges": "Container type-change rows use valueType instead of inline value",
             "excludes": ["other viewports/scissors", "unused descriptors", "resource contents",
                          "shader source/disassembly", "unselected constant-buffer values"],
             "nonFiniteValues": "NaN, Infinity and -Infinity are stable string tokens"}
    if _bytes(scope) > 64 * 1024:
        raise AgentBridgeError("LIMIT_EXCEEDED", "The combined comparison selectors exceed 64 KiB.")
    key = query_key(session, before_id, after_id, scope)
    cache = runtime.cache.get(session, "event-comparisons", OrderedDict)
    cached = cursor is not None and key in cache
    if not cached:
        if cursor is not None:
            raise invalid("The comparison snapshot has expired; restart without cursor.")
        resources = resource_index(runtime, context)
        before, before_coverage, before_meta = _snapshot(
            runtime, context, before_id, actions.action_for(before_id), resources, stages,
            include_bindings, binding_limit, selections)
        if before_id == after_id:
            after, after_coverage, after_meta = before, before_coverage, before_meta
        else:
            after, after_coverage, after_meta = _snapshot(
                runtime, context, after_id, actions.action_for(after_id), resources, stages,
                include_bindings, binding_limit, selections)
        diff = _Differ(lambda: _check(runtime, context))
        diff.walk(before, after)
        complete = before_coverage["complete"] and after_coverage["complete"] and diff.complete
        comparison = "different" if diff.items else ("equal-in-scope" if complete else "indeterminate")
        coverage = {"complete": complete, "before": before_coverage, "after": after_coverage,
                    "scope": scope, "differencesComplete": diff.complete}
        entry = {"key": query_key(key, uuid.uuid4().hex), "differences": diff.items,
                 "comparison": comparison, "coverage": coverage,
                 "totalDifferencesExact": complete,
                 "meta": {"eventIdBefore": before_meta.get("eventIdBefore"),
                          "eventIdAfter": after_meta.get("eventIdAfter"),
                          "replayEventRestored": before_meta.get("replayEventRestored", False) and
                          after_meta.get("replayEventRestored", False)}}
        _check(runtime, context)
        cache[key] = entry
        cache.move_to_end(key)
        while len(cache) > CACHE_ENTRIES:
            cache.popitem(last=False)
    else:
        entry = cache[key]
        cache.move_to_end(key)
    offset = decode_cursor(cursor, "compare_events", session, entry["key"])
    differences = entry["differences"]
    if offset > len(differences):
        raise invalid("The comparison cursor is beyond the difference list.")
    page, page_bytes = [], _bytes(entry["coverage"]) + 4096
    for item in differences[offset:offset + limit]:
        size = _bytes(item)
        if page_bytes + size > MAX_PAGE_BYTES:
            if not page:
                raise AgentBridgeError("LIMIT_EXCEEDED", "One comparison row exceeds the page budget.")
            break
        page.append(item)
        page_bytes += size
    end = offset + len(page)
    next_cursor = (encode_cursor("compare_events", session, entry["key"], end)
                   if end < len(differences) else None)
    warnings = []
    if not entry["coverage"]["complete"]:
        warnings.append("The comparison is incomplete; inspect coverage issues. Missing observations "
                        "are not evidence of equality. Narrow CBuffer selections or increase limits.")
    if not entry["coverage"]["differencesComplete"]:
        warnings.append("The total difference budget was reached; totalDifferences is a lower bound "
                        "on observed differences and omitted rows cannot be paged.")
    meta = dict(entry["meta"])
    meta["cachedComparison"] = cached
    if cached:
        # action_index above performs a replay-thread session/deadline guard even on cache hits.
        current = runtime.state.snapshot().get("currentEventId")
        meta.update(eventIdBefore=current, eventIdAfter=current, replayEventRestored=True)
    _check(runtime, context)
    return result({"beforeEventId": before_id, "afterEventId": after_id,
                   "comparison": entry["comparison"], "differences": page,
                   "totalDifferences": len(differences),
                   "totalDifferencesExact": entry["totalDifferencesExact"],
                   "coverage": entry["coverage"]}, meta=meta, warnings=warnings,
                  next_cursor=next_cursor,
                  truncated=next_cursor is not None or not entry["coverage"]["complete"])


HANDLERS = {"compare_events": compare_events}
