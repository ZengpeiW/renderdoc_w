"""Incrementally find exact shader bindings at relevant executable action records.

The capture-local cache contains primitive binding observations, never native replay objects. A
refresh replaces the shared generation only after a successful page and replay-state restoration.
"""

import datetime
import json
import time
import uuid

from ..ipc import AgentBridgeError
from ..paging import decode_cursor, encode_cursor, query_key
from ..replay import _check_replay_health
from ..values import resource_id
from .common import (STAGES, STAGE_NAMES, action_index, get_bool, get_int, get_resource_key,
                     get_str, invalid, renderdoc_module, resource_index, result, session_of)

MAX_SCOPE_ACTIONS = 100000
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_PAGE_BYTES = 256 * 1024
GRAPHICS_STAGES = ("vertex", "hull", "domain", "geometry", "pixel")
MESH_STAGES = ("pixel", "amplification", "mesh")
CONTAINERS = ("SetMarker", "PushMarker", "PopMarker", "MultiAction", "CmdList", "PassBoundary",
              "BeginPass", "EndPass", "CommandBufferBoundary")
NON_SHADER = ("Clear", "ClearColor", "ClearDepthStencil", "Copy", "Resolve", "Present",
              "GenMips", "BuildAccStruct")


def _check(runtime, context):
    context.check_cancelled()
    if time.time() > context.deadline_at:
        raise AgentBridgeError("TIMEOUT", "The shader-reference scan deadline has expired.")
    if session_of(context) != (runtime.state.snapshot().get("captureSessionId") or ""):
        raise AgentBridgeError("CAPTURE_CHANGED", "The capture changed during shader-reference scanning.")


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def _text(row, key, value, limit):
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
            "events": {}, "bytes": 0}


def _request(params):
    key = get_resource_key(params)
    if int(key) > (1 << 64) - 1:
        raise invalid("resource_id exceeds the uint64 ResourceId range.")
    stages = params.get("stages")
    if stages is None:
        stages = list(STAGE_NAMES)
    elif (not isinstance(stages, list) or not stages or len(stages) > len(STAGE_NAMES) or
          any(not isinstance(stage, str) or stage not in STAGE_NAMES for stage in stages)):
        raise invalid("stages must be a nonempty list of at most eight supported shader stages.")
    stages = [stage for stage in STAGE_NAMES if stage in stages]
    parent = get_int(params, "parent_event_id", maximum=0xFFFFFFFF)
    limit = get_int(params, "limit", 200, minimum=1, maximum=1000, required=True)
    budget = get_int(params, "scan_budget", 200, minimum=1, maximum=2000, required=True)
    cursor = get_str(params, "cursor", max_length=1024)
    if cursor == "":
        raise invalid("The cursor is invalid.")
    refresh = get_bool(params, "refresh", False)
    if refresh and cursor is not None:
        raise invalid("refresh cannot be combined with a continuation cursor.")
    return key, stages, parent, limit, budget, cursor, refresh


def _classify(node):
    flags = set(node["flags"])
    if node["children"] or flags.intersection(CONTAINERS):
        return "excluded", ()
    if "DispatchRay" in flags:
        return "unsupported", ()
    if "MeshDispatch" in flags:
        if "Dispatch" in flags or flags.intersection(NON_SHADER):
            return "unsupported", ()
        return "candidate", MESH_STAGES
    draw, compute = "Drawcall" in flags, "Dispatch" in flags
    if (draw and compute) or ((draw or compute) and flags.intersection(NON_SHADER)):
        return "unsupported", ()
    if draw:
        return "candidate", GRAPHICS_STAGES
    if compute:
        return "candidate", ("compute",)
    if flags.intersection(NON_SHADER):
        return "excluded", ()
    return "unsupported", ()


def _scope(actions, parent, stages, available, check):
    coverage = {"scopeActionCount": 0, "candidateEventCount": 0, "excludedActionCount": 0,
                "stageExcludedActionCount": 0, "unsupportedActionCount": 0,
                "unsupportedActionExamples": [], "unsupportedStages": []}
    candidates = []
    unsupported_stages = set()
    roots = actions.roots if parent is None else [parent]
    if len(roots) > MAX_SCOPE_ACTIONS:
        raise AgentBridgeError("LIMIT_EXCEEDED", "The shader-reference scope exceeds the "
                               "100000-action budget. Select a smaller subtree.")
    stack = list(reversed(roots))
    while stack:
        if coverage["scopeActionCount"] >= MAX_SCOPE_ACTIONS:
            raise AgentBridgeError("LIMIT_EXCEEDED", "The shader-reference scope exceeds the "
                                   "100000-action budget. Select a smaller subtree.")
        if coverage["scopeActionCount"] % 256 == 0:
            check()
        event_id = stack.pop()
        node = actions.nodes[event_id]
        coverage["scopeActionCount"] += 1
        kind, relevant = _classify(node)
        if kind == "excluded":
            coverage["excludedActionCount"] += 1
        elif kind == "unsupported":
            coverage["unsupportedActionCount"] += 1
            if len(coverage["unsupportedActionExamples"]) < 16:
                coverage["unsupportedActionExamples"].append(event_id)
        elif not set(stages).intersection(relevant):
            coverage["stageExcludedActionCount"] += 1
        else:
            candidates.append(event_id)
            unsupported_stages.update(stage for stage in relevant if stage in stages and stage not in available)
        if coverage["scopeActionCount"] + len(stack) + len(node["children"]) > MAX_SCOPE_ACTIONS:
            raise AgentBridgeError("LIMIT_EXCEEDED", "The shader-reference scope exceeds the "
                                   "100000-action budget. Select a smaller subtree.")
        stack.extend(reversed(node["children"]))
    candidates.sort()
    coverage["candidateEventCount"] = len(candidates)
    coverage["unsupportedStages"] = [stage for stage in STAGE_NAMES if stage in unsupported_stages]
    return candidates, coverage


def _available_stages():
    rd = renderdoc_module()
    available = {}
    for name, aliases in STAGES:
        for alias in aliases:
            stage = getattr(rd.ShaderStage, alias, None)
            if stage is not None:
                available[name] = stage
                break
    return available


def _observe(controller, event_id, node, available, check):
    check()
    controller.SetFrameEvent(event_id, False)
    _check_replay_health(controller)
    pipe = controller.GetPipelineState()
    _, relevant = _classify(node)
    bindings = {}
    for stage in relevant:
        check()
        if stage not in available:
            continue
        shader_key = resource_id(pipe.GetShader(available[stage]))
        value = {"resourceId": shader_key}
        if shader_key is not None:
            entry = pipe.GetShaderEntryPoint(available[stage])
            value["entryPointKnown"] = isinstance(entry, str) and bool(entry)
            _text(value, "entryPoint", entry if isinstance(entry, str) else "", 1024)
        bindings[stage] = value
    _check_replay_health(controller)
    check()
    return bindings


def _reference(node, bindings, shader_key, stages):
    matches = []
    for stage in stages:
        binding = bindings.get(stage)
        if binding is not None and binding["resourceId"] == shader_key:
            item = dict(binding)
            item["stage"] = stage
            matches.append(item)
    if not matches:
        return None
    row = {"eventId": node["eventId"], "parentEventId": node["parentEventId"],
           "flags": list(node["flags"]), "matches": matches}
    _text(row, "name", node["name"], 256)
    return row


def find_shader_references(runtime, context):
    shader_key, stages, parent, limit, budget, cursor, refresh = _request(context.params)
    check = lambda: _check(runtime, context)
    check()
    actions = action_index(runtime, context)
    if parent is not None and parent not in actions.nodes:
        raise AgentBridgeError("INVALID_EVENT", "parent_event_id must be an exact action EID.")
    resources = resource_index(runtime, context)
    shader = resources.items.get(shader_key)
    if shader is None or shader["kind"] != "shader":
        raise AgentBridgeError("INVALID_RESOURCE", "resource_id must identify a shader resource "
                               "in this capture.", details={"resource_id": shader_key})
    available = _available_stages()
    candidates, coverage = _scope(actions, parent, stages, available, check)
    session = session_of(context)
    holder = runtime.cache.get(session, "shader-reference-bindings-v1", lambda: {"cache": _new_cache()})
    cache = _new_cache() if refresh else holder["cache"]
    query = query_key(session, cache["scanId"], shader_key, stages, parent)
    offset = decode_cursor(cursor, "find_shader_references", session, query)
    if offset > len(candidates):
        raise invalid("The shader-reference cursor is beyond the candidate list.")
    pending = {}
    progress = {"position": offset, "examined": 0, "native": 0, "hits": 0, "bytes": 0,
                "pageBytes": 8192, "rows": []}

    def advance(controller=None):
        while (progress["position"] < len(candidates) and progress["examined"] < budget and
               len(progress["rows"]) < limit):
            check()
            event_id = candidates[progress["position"]]
            node = actions.nodes[event_id]
            bindings = cache["events"].get(event_id)
            if bindings is None:
                if controller is None:
                    return True
                bindings = _observe(controller, event_id, node, available, check)
                size = _size(bindings) + 64
                if cache["bytes"] + progress["bytes"] + size > MAX_CACHE_BYTES:
                    raise AgentBridgeError("LIMIT_EXCEEDED", "The shared shader-binding cache "
                                           "exceeds 32 MiB; refresh and narrow the scope.")
                pending[event_id] = bindings
                progress["bytes"] += size
                progress["native"] += 1
            else:
                progress["hits"] += 1
            progress["examined"] += 1
            row = _reference(node, bindings, shader_key, stages)
            if row is not None:
                size = _size(row) + 32
                if progress["pageBytes"] + size > MAX_PAGE_BYTES:
                    if not progress["rows"]:
                        raise AgentBridgeError("LIMIT_EXCEEDED", "A shader-reference row exceeds "
                                               "the 256 KiB response budget.")
                    break
                progress["rows"].append(row)
                progress["pageBytes"] += size
            progress["position"] += 1
        return False

    meta = {}
    if advance():
        _, meta = runtime.replay.call_restoring(context, advance)
    check()
    # Publish only after every native read and the forced restore succeeded. Previously returned
    # pages remain usable if this page is cancelled or a refresh fails.
    cache["events"].update(pending)
    cache["bytes"] += progress["bytes"]
    holder["cache"] = cache
    end = progress["position"]
    next_cursor = (encode_cursor("find_shader_references", session, query, end)
                   if end < len(candidates) else None)
    coverage.update(processedCandidateCount=end, examinedThisPage=progress["examined"],
                    nativeReadsThisPage=progress["native"], cacheHitsThisPage=progress["hits"],
                    scanComplete=end == len(candidates))
    coverage["complete"] = (coverage["scanComplete"] and not coverage["unsupportedActionCount"] and
                            not coverage["unsupportedStages"])
    data = {"resourceId": shader_key, "stages": stages, "parentEventId": parent,
            "scanId": cache["scanId"], "scanStartedAtUtc": cache["scanStartedAtUtc"],
            "references": progress["rows"], "coverage": coverage}
    _text(data, "resourceName", shader["name"], 256)
    text_truncated = (data.get("resourceNameTruncated", False) or
                      any(row.get("nameTruncated", False) or any(
                          item.get("entryPointTruncated", False) for item in row["matches"])
                          for row in progress["rows"]))
    warnings = ["Results establish exact shader ResourceId bindings at selected leaf actions, "
                "not proof that a shader executed, every API call that bound it, or all GPU uses. "
                "Observations are collected in batches, not one atomic snapshot. Read every "
                "nextCursor, including after an empty page; limit bounds matching event rows, "
                "while scan_budget bounds candidate visits including cache hits."]
    if coverage["unsupportedActionCount"] or coverage["unsupportedStages"]:
        warnings.append("Some action types or native shader stages are unsupported; an empty "
                        "result is not evidence of no shader references in that unobserved scope.")
    if text_truncated:
        warnings.append("Some resource/action names or entry-point labels were truncated.")
    return result(data, meta=meta, warnings=warnings, next_cursor=next_cursor,
                  truncated=next_cursor is not None or not coverage["complete"] or text_truncated)


HANDLERS = {"find_shader_references": find_shader_references}
