from __future__ import annotations

import ast
from enum import IntEnum, IntFlag
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

from fake_renderdoc import FakeContext, ResourceType, action, module
from rendertest_agent_bridge.indexes import ActionIndex, SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError
from rendertest_agent_bridge.lifecycle import CaptureState
from rendertest_agent_bridge.methods import pass_resources as passes
from rendertest_agent_bridge.paging import encode_cursor, query_key
from rendertest_agent_bridge.replay import ReplayRunner


class Flags(IntFlag):
    Clear = 1
    Drawcall = 2
    Dispatch = 4
    SetMarker = 32
    PushMarker = 64
    PopMarker = 128
    Present = 256
    MultiAction = 512
    Copy = 1024
    PassBoundary = 8192
    BeginPass = 0x400000
    EndPass = 0x800000


_known = sorted(passes.READ_USAGES | passes.WRITE_USAGES | passes.READ_WRITE_USAGES |
                {"Unused", "Barrier", "Discard"})
Usage = IntEnum("Usage", {name: index for index, name in enumerate(_known)})


def usage(eid, name, **kwargs):
    return NS(eventId=eid, usage=getattr(Usage, name), **kwargs)


def node(eid, flags, events, children=()):
    item = action(eid, "Action %d" % eid, flags, eid, list(children))
    item.events = [NS(eventId=event, chunkIndex=event) for event in events]
    return item


class Context:
    method = "get_pass_resources"
    deadline_at = float("inf")

    def __init__(self, state, params):
        self.state = state.snapshot()
        self.params = params

    def check_cancelled(self):
        pass


@pytest.fixture
def usage_runtime(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "renderdoc", module())
    state = CaptureState()
    state.capture_loaded("usage.rdc", "D3D12", selected=(10, None), current=(30, None))
    ctx = FakeContext()
    replay = ReplayRunner(state, lambda message: None)
    replay.manager = ctx.manager
    children = [node(20, Flags.Drawcall, list(range(11, 21))),
                node(25, Flags.Dispatch, [21, 22, 25])]
    roots = [node(10, Flags.PushMarker, [8, 9, 10], children),
             node(30, Flags.Present, [26, 27, 28, 29, 30])]
    ctx.controller.GetRootActions = lambda: roots
    runtime = NS(root=str(tmp_path), instance_id="test-pass-usage", state=state, replay=replay,
                 cache=SessionCache(), ctx=ctx, actions=roots, reads=[], fatal=0,
                 usages={100: [usage(9, "Clear"), usage(19, "Barrier"), usage(20, "ColorTarget"),
                               usage(20, "ColorTarget"), usage(25, "CS_Resource"),
                               usage(26, "ResolveSrc"), usage(26, "Barrier")],
                         101: [usage(20, "DepthStencilTarget"), usage(25, "Discard")],
                         200: [usage(0, "Unused"), usage(20, "VS_Constants")]})
    def get_usage(resource):
        key = int(resource)
        runtime.reads.append(key)
        return runtime.usages.get(key, [])
    ctx.controller.GetUsage = get_usage
    ctx.controller.SetFrameEvent = lambda *args: pytest.fail("GetUsage must never move replay state")
    ctx.controller.GetFatalErrorStatus = lambda: NS(code=runtime.fatal)
    return runtime


def call(runtime, **params):
    response = passes.get_pass_resources(runtime, Context(runtime.state, params))
    assert response.meta == {}
    assert response.result["artifacts"] == []
    assert len(json.dumps(response.result, ensure_ascii=False, allow_nan=False).encode()) <= passes.MAX_PAGE_BYTES
    assert runtime.ctx.controller.frame_events == []
    return response.result


def error(runtime, code="INVALID_REQUEST", **params):
    with pytest.raises(AgentBridgeError) as caught:
        call(runtime, **params)
    assert caught.value.code == code


def cache_of(runtime):
    session = runtime.state.snapshot()["captureSessionId"]
    return runtime.cache.get(session, passes.CACHE_NAMESPACE, dict)


def test_whole_capture_roles_raw_duplicates_sentinel_and_no_replay(usage_runtime):
    runtime = usage_runtime
    original = runtime.state.snapshot()
    response = call(runtime)
    data = response["data"]
    assert data["scope"]["kind"] == "capture"
    assert data["scope"]["startEventId"] is None and data["scope"]["endEventId"] is None
    assert data["scope"]["actionCount"] == 4
    assert [row["resourceId"] for row in data["resources"]] == ["100", "101", "200"]
    assert runtime.reads == [100, 101, 200]
    row = data["resources"][0]
    assert row["usageRecordCount"] == 7 and row["eventCount"] == 5
    assert row["firstEventId"] == 9 and row["lastEventId"] == 26
    assert row["hasReadUsage"] and row["hasWriteUsage"]
    assert row["previousUsage"] is None and row["nextUsage"] is None
    color = next(item for item in row["usageTypes"] if item["usage"] == "ColorTarget")
    assert color["access"] == "write" and color["count"] == 2
    assert sum(item["usage"] == "ColorTarget" for item in row["usageExamples"]) == 2
    assert data["coverage"]["complete"]
    assert data["coverage"]["unusedSentinelCount"] == 1
    assert runtime.state.snapshot() == original
    assert "not producers" in " ".join(response["warnings"])


@pytest.mark.parametrize("name,expected", [
    ("ColorTarget", "write"), ("DepthStencilTarget", "readWrite"), ("Discard", "discard"),
    ("GenMips", "readWrite"), ("Resolve", "readWrite"), ("ResolveSrc", "read"),
    ("ResolveDst", "write"), ("Copy", "readWrite"), ("CopySrc", "read"), ("CopyDst", "write"),
    ("InputTarget", "read"), ("Barrier", "barrier"), ("Unused", "unused"),
    ("Future_Resource", "unknown"), ("Future_RWResource", "unknown"),
])
def test_explicit_native_classification(name, expected):
    assert passes._access(name) == expected


def test_real_marker_excludes_its_owned_prefix_and_neighbours_retain_same_eid_records(usage_runtime):
    runtime = usage_runtime
    response = call(runtime, parent_event_id=10)
    scope = response["data"]["scope"]
    assert scope["kind"] == "actionSubtree" and not scope["syntheticParent"]
    assert (scope["startEventId"], scope["endEventId"]) == (10, 25)
    row = response["data"]["resources"][0]
    assert row["usageRecordCount"] == 4 and row["firstEventId"] == 19
    assert row["previousUsage"]["eventId"] == 9
    assert row["nextUsage"]["eventId"] == 26 and row["nextUsage"]["recordCount"] == 2
    assert {entry["usage"] for entry in row["nextUsage"]["usages"]} == {"ResolveSrc", "Barrier"}
    assert row["usageExamples"][0]["actionEventId"] == 20


def test_synthetic_parent_clips_first_child_prefix_and_is_invalid_range_endpoint(usage_runtime):
    runtime = usage_runtime
    root = runtime.actions[0]
    root.eventId = 1000
    root.events = [NS(eventId=1000, chunkIndex=passes.NO_CHUNK)]
    response = call(runtime, parent_event_id=1000)
    scope = response["data"]["scope"]
    assert scope["syntheticParent"] and (scope["startEventId"], scope["endEventId"]) == (20, 25)
    row = response["data"]["resources"][0]
    assert row["usageRecordCount"] == 3 and row["previousUsage"]["eventId"] == 19
    assert all(item["eventId"] != 19 for item in row["usageExamples"])
    error(runtime, "INVALID_EVENT", start_event_id=20, end_event_id=1000)
    # The genuine API call 19 is usable as a range endpoint, despite belonging to action 20.
    ranged = call(runtime, start_event_id=19, end_event_id=20)["data"]["resources"][0]
    assert ranged["usageRecordCount"] == 3 and ranged["firstEventId"] == 19


def test_indirect_execution_without_api_events_is_valid_anchor(usage_runtime):
    runtime = usage_runtime
    root = runtime.actions[0]
    root.eventId = 1000
    root.events = [NS(eventId=1000, chunkIndex=passes.NO_CHUNK)]
    root.children[0].events = []
    root.children[1].events = []
    data = call(runtime, parent_event_id=1000)["data"]
    assert (data["scope"]["startEventId"], data["scope"]["endEventId"]) == (20, 25)
    assert data["scope"]["selectedEventCount"] == 2
    assert call(runtime, start_event_id=20, end_event_id=25)["data"]["coverage"]["complete"]


def test_synthetic_first_real_begin_clips_begin_owned_setup(usage_runtime):
    runtime = usage_runtime
    root = runtime.actions[0]
    root.eventId = 2712
    root.events = [NS(eventId=2712, chunkIndex=passes.NO_CHUNK)]
    root.children = [node(1696, Flags.PassBoundary | Flags.BeginPass, list(range(1690, 1697))),
                     node(1700, Flags.Drawcall, list(range(1697, 1701))),
                     node(1722, Flags.PassBoundary | Flags.EndPass, [1722])]
    runtime.usages = {100: [usage(1695, "Barrier"), usage(1696, "Discard"), usage(1700, "ColorTarget")]}
    data = call(runtime, parent_event_id=2712)["data"]
    assert (data["scope"]["startEventId"], data["scope"]["endEventId"]) == (1696, 1722)
    assert data["resources"][0]["usageRecordCount"] == 2
    assert data["resources"][0]["previousUsage"]["eventId"] == 1695


def test_subtree_holes_and_orphans_are_not_guessed_from_owner_or_bounds(usage_runtime):
    runtime = usage_runtime
    runtime.actions.append(node(23, Flags.Copy, [23]))
    runtime.usages[100] += [usage(23, "CopyDst"), usage(24, "Barrier")]
    response = call(runtime, parent_event_id=10)
    assert all(item["eventId"] not in (23, 24) for item in response["data"]["resources"][0]["usageExamples"])
    coverage = response["data"]["coverage"]
    assert coverage["unmappedUsageRecordCount"] == 1 and not coverage["complete"]
    ranged = call(runtime, start_event_id=23, end_event_id=25)
    row = ranged["data"]["resources"][0]
    assert row["usageRecordCount"] == 3
    orphan = next(item for item in row["usageExamples"] if item["eventId"] == 24)
    assert orphan["actionEventId"] is None
    assert ranged["data"]["coverage"]["unmappedUsageRecordCount"] == 1


def test_no_anchors_and_nonaction_parent_are_explicit_errors(usage_runtime):
    runtime = usage_runtime
    error(runtime, "INVALID_EVENT", parent_event_id=19)
    root = runtime.actions[0]
    root.eventId = 1000
    root.events = [NS(eventId=1000, chunkIndex=passes.NO_CHUNK)]
    root.children = []
    runtime.cache.clear()
    error(runtime, "UNSUPPORTED", parent_event_id=1000)
    assert runtime.reads == []


def test_nested_synthetic_marker_inside_anchor_bounds_is_not_a_member(usage_runtime):
    runtime = usage_runtime
    synthetic = node(24, Flags.PushMarker, [24])
    synthetic.events[0].chunkIndex = passes.NO_CHUNK
    runtime.actions[0].children.append(synthetic)
    runtime.usages[100].append(usage(24, "Barrier"))
    data = call(runtime, parent_event_id=10)["data"]
    assert data["scope"]["selectedEventCount"] == 14
    assert all(item["eventId"] != 24 for item in data["resources"][0]["usageExamples"])
    error(runtime, "INVALID_EVENT", start_event_id=24, end_event_id=25)


def test_empty_page_continues_and_queries_share_full_usage_cache(usage_runtime):
    runtime = usage_runtime
    runtime.usages[100] = [usage(9, "Clear")]
    first = call(runtime, parent_event_id=20, scan_budget=1)
    assert first["data"]["resources"] == [] and first["nextCursor"]
    assert first["data"]["coverage"]["processedResourceCount"] == 1
    second = call(runtime, parent_event_id=20, cursor=first["nextCursor"], limit=1, scan_budget=2)
    assert [item["resourceId"] for item in second["data"]["resources"]] == ["101"]
    assert second["nextCursor"]
    third = call(runtime, parent_event_id=20, cursor=second["nextCursor"])
    assert third["data"]["coverage"]["complete"] and third["nextCursor"] is None
    assert runtime.reads == [100, 101, 200]
    ranged = call(runtime, start_event_id=9, end_event_id=9, kind="texture")
    assert [item["resourceId"] for item in ranged["data"]["resources"]] == ["100"]
    assert ranged["data"]["coverage"]["nativeReadsThisPage"] == 0
    assert ranged["data"]["coverage"]["cacheHitsThisPage"] == 2
    assert ranged["data"]["scanId"] == first["data"]["scanId"]


def test_diagnostics_are_cumulative_across_pages_and_unknown_outside_scope_does_not_leak(usage_runtime):
    runtime = usage_runtime
    Unknown = IntEnum("Unknown", {"FutureUsage": 999})
    runtime.usages[100] = [NS(eventId=20, usage=Unknown.FutureUsage), usage(24, "Clear")]
    first = call(runtime, parent_event_id=10, scan_budget=1)
    last = call(runtime, parent_event_id=10, cursor=first["nextCursor"])
    assert last["data"]["coverage"]["unknownUsageRecordCount"] == 1
    assert last["data"]["coverage"]["unmappedUsageRecordCount"] == 1
    assert last["data"]["coverage"]["scanComplete"] and not last["data"]["coverage"]["complete"]
    assert last["data"]["coverage"]["unusedSentinelCount"] == 1
    narrowed = call(runtime, parent_event_id=25)
    assert narrowed["data"]["coverage"]["complete"]
    assert narrowed["data"]["coverage"]["unknownUsageRecordCount"] == 0


def test_all_orphan_subtree_records_still_report_gap_after_empty_pages(usage_runtime):
    runtime = usage_runtime
    runtime.usages = {100: [usage(24, "Barrier")], 101: [], 200: []}
    first = call(runtime, parent_event_id=10, scan_budget=1)
    assert first["data"]["resources"] == []
    assert first["data"]["coverage"]["unmappedUsageRecordCount"] == 1
    last = call(runtime, parent_event_id=10, cursor=first["nextCursor"])
    assert last["data"]["resources"] == [] and last["nextCursor"] is None
    assert last["data"]["coverage"]["unmappedUsageRecordCount"] == 1
    assert last["data"]["coverage"]["scanComplete"] and not last["data"]["coverage"]["complete"]


def test_refresh_invalidates_all_query_cursors_only_after_success(usage_runtime):
    runtime = usage_runtime
    first = call(runtime, scan_budget=1)
    other = call(runtime, kind="texture", scan_budget=1)
    refreshed = call(runtime, kind="buffer", refresh=True)
    assert refreshed["data"]["scanId"] != first["data"]["scanId"]
    error(runtime, cursor=first["nextCursor"])
    error(runtime, kind="texture", cursor=other["nextCursor"])
    assert set(cache_of(runtime)["cache"]["resources"]) == {"200"}


@pytest.mark.parametrize("changed", [{"parent_event_id": 10}, {"kind": "texture"},
                                   {"start_event_id": 10, "end_event_id": 25}])
def test_cursor_scope_kind_bound(usage_runtime, changed):
    first = call(usage_runtime, scan_budget=1)
    error(usage_runtime, cursor=first["nextCursor"], **changed)


def test_cursor_full_session_and_position_bound(usage_runtime):
    runtime = usage_runtime
    first = call(runtime, scan_budget=1)
    error(runtime, cursor=first["nextCursor"], refresh=True)
    session = runtime.state.snapshot()["captureSessionId"]
    cache = cache_of(runtime)["cache"]
    query = query_key(session, cache["scanId"], None, None, None, "all")
    error(runtime, cursor=encode_cursor("get_pass_resources", session, query, 4))
    runtime.state.capture_loaded("different.rdc", "D3D12", current=(30, None))
    error(runtime, cursor=first["nextCursor"])


@pytest.mark.parametrize("params", [
    {"parent_event_id": True}, {"parent_event_id": 1.0}, {"parent_event_id": -1},
    {"parent_event_id": 1 << 32}, {"start_event_id": True, "end_event_id": 25},
    {"start_event_id": 10, "end_event_id": 25.0}, {"start_event_id": 10}, {"end_event_id": 25},
    {"start_event_id": 25, "end_event_id": 10},
    {"parent_event_id": 10, "start_event_id": 10, "end_event_id": 25},
    {"kind": None}, {"kind": "shader"}, {"kind": 1},
    {"limit": None}, {"limit": True}, {"limit": 1.0}, {"limit": 0}, {"limit": 1001},
    {"scan_budget": None}, {"scan_budget": False}, {"scan_budget": 1.0},
    {"scan_budget": 0}, {"scan_budget": 2001}, {"cursor": ""}, {"cursor": 1},
    {"cursor": "x" * 1025}, {"refresh": 1}, {"refresh": None},
])
def test_strict_parameters_before_native_reads(usage_runtime, params):
    error(usage_runtime, **params)
    assert usage_runtime.reads == []


@pytest.mark.parametrize("case", ["native", "fatal", "cancel", "timeout", "session", "malformed"])
@pytest.mark.parametrize("refresh", [False, True])
def test_failure_atomicity_with_partial_existing_cache(usage_runtime, case, refresh):
    runtime = usage_runtime
    first = call(runtime, scan_budget=1)
    context = Context(runtime.state, {"refresh": refresh})
    holder = cache_of(runtime)
    original = holder["cache"]
    before = json.dumps(original, sort_keys=True)
    native = runtime.ctx.controller.GetUsage
    def failing(handle):
        value = native(handle)
        if int(handle) != 101:
            return value
        if case == "native":
            raise RuntimeError("usage read failed")
        if case == "fatal":
            runtime.fatal = 1
        if case == "session":
            runtime.state.capture_loaded("different.rdc", "D3D12", current=(30, None))
        if case == "malformed":
            return [NS(eventId=True, usage=Usage.Clear)]
        if case in ("cancel", "timeout"):
            context.check_cancelled = lambda: (_ for _ in ()).throw(
                AgentBridgeError("CANCELLED" if case == "cancel" else "TIMEOUT", "stop"))
        return value
    runtime.ctx.controller.GetUsage = failing
    with pytest.raises(AgentBridgeError):
        passes.get_pass_resources(runtime, context)
    assert holder["cache"] is original and json.dumps(original, sort_keys=True) == before
    if case != "session":
        runtime.fatal = 0
        runtime.ctx.controller.GetUsage = native
        continued = call(runtime, cursor=first["nextCursor"])
        assert continued["data"]["scanId"] == first["data"]["scanId"]


def test_examples_and_neighbours_are_bounded_but_counts_complete(usage_runtime):
    runtime = usage_runtime
    runtime.usages[100] = ([usage(19, "Barrier")] * 20 + [usage(20, "ColorTarget")] * 30 +
                           [usage(21, "PS_Resource")] * 18)
    response = call(runtime, parent_event_id=20)
    row = response["data"]["resources"][0]
    assert row["usageRecordCount"] == 30 and row["eventCount"] == 1
    assert len(row["usageExamples"]) == 16 and row["usageExamplesTruncated"]
    assert row["previousUsage"]["recordCount"] == 20 and row["previousUsage"]["truncated"]
    assert row["nextUsage"]["recordCount"] == 18 and row["nextUsage"]["truncated"]
    assert len(row["nextUsage"]["usages"]) == 16
    assert response["truncated"] and response["data"]["coverage"]["complete"]


def test_optional_view_metadata_preserves_distinct_and_duplicate_records(usage_runtime):
    from fake_renderdoc import ResourceId
    runtime = usage_runtime
    runtime.usages[100] = [usage(20, "ColorTarget", viewId=ResourceId(400)),
                           usage(20, "ColorTarget", viewId=ResourceId(401)),
                           usage(20, "ColorTarget", viewId=ResourceId(401))]
    row = call(runtime, parent_event_id=20)["data"]["resources"][0]
    assert row["usageRecordCount"] == 3 and row["usageTypes"][0]["count"] == 3
    assert [item["viewId"] for item in row["usageExamples"]] == ["400", "401", "401"]


def test_resource_id_order_is_numeric_and_kind_filter_is_candidate_filter(usage_runtime):
    runtime = usage_runtime
    runtime.ctx.controller.resources.append(runtime.ctx.controller._resource(2, "Small", ResourceType.Buffer))
    runtime.usages[2] = [usage(20, "IndexBuffer")]
    data = call(runtime, kind="buffer")["data"]
    assert [row["resourceId"] for row in data["resources"]] == ["2", "200"]
    assert data["coverage"]["candidateResourceCount"] == 2
    assert runtime.reads == [2, 200]


def test_duplicate_native_resource_descriptions_are_one_candidate(usage_runtime):
    runtime = usage_runtime
    runtime.ctx.controller.resources.append(runtime.ctx.controller.resources[0])
    data = call(runtime)["data"]
    assert [row["resourceId"] for row in data["resources"]] == ["100", "101", "200"]
    assert data["coverage"]["candidateResourceCount"] == 3
    assert data["coverage"]["processedResourceCount"] == 3
    assert runtime.reads == [100, 101, 200]


def test_page_budget_caches_unconsumed_resource_and_resume_hits_it(usage_runtime, monkeypatch):
    runtime = usage_runtime
    row = call(runtime, limit=1)["data"]["resources"][0]
    runtime.cache.clear()
    runtime.reads.clear()
    monkeypatch.setattr(passes, "MAX_PAGE_BYTES", 8192 + passes._size(row) + 40)
    first = call(runtime)
    coverage = first["data"]["coverage"]
    assert coverage["processedResourceCount"] == 1 and coverage["examinedThisPage"] == 2
    assert coverage["nativeReadsThisPage"] == 2 and first["nextCursor"]
    reads = list(runtime.reads)
    second = call(runtime, cursor=first["nextCursor"], limit=1)
    assert second["data"]["resources"][0]["resourceId"] == "101"
    assert second["data"]["coverage"]["cacheHitsThisPage"] == 1
    assert runtime.reads == reads


@pytest.mark.parametrize("setting,value", [("MAX_SCOPE_ACTIONS", 2), ("MAX_SCOPE_EVENTS", 3),
    ("MAX_RESOURCES", 2), ("MAX_RESOURCE_ROWS", 2), ("MAX_CACHE_BYTES", 1),
    ("MAX_PAGE_BYTES", 1), ("MAX_USAGE_TYPES", 1)])
def test_budgets_fail_explicitly_without_publishing(usage_runtime, monkeypatch, setting, value):
    runtime = usage_runtime
    monkeypatch.setattr(passes, setting, value)
    error(runtime, "LIMIT_EXCEEDED")
    holder = cache_of(runtime)
    if "cache" in holder:
        assert holder["cache"]["resources"] == {}


@pytest.mark.parametrize("wide_kind", ["roots", "children", "events"])
def test_wide_scope_rejected_before_copy_or_iteration(wide_kind):
    class Wide:
        def __len__(self):
            return 200001
        def __iter__(self):
            pytest.fail("Oversized scope must be rejected before iteration")
        def __reversed__(self):
            pytest.fail("Oversized scope must be rejected before copying")
    wide = Wide()
    actions = NS(roots=wide if wide_kind == "roots" else [1],
                 nodes={1: {"flags": ["Drawcall"], "children": wide if wide_kind == "children" else []}},
                 action_api_events={1: wide if wide_kind == "events" else []}, api_events={})
    with pytest.raises(AgentBridgeError) as caught:
        passes._scope(actions, None, None, None, lambda: None)
    assert caught.value.code == "LIMIT_EXCEEDED"


def test_native_row_limit_precedes_iteration(usage_runtime):
    runtime = usage_runtime
    class Wide:
        def __len__(self):
            return passes.MAX_RESOURCE_ROWS + 1
        def __iter__(self):
            pytest.fail("Oversized native result must be rejected before iteration")
    runtime.usages[100] = Wide()
    error(runtime, "LIMIT_EXCEEDED")


def test_failure_during_cumulative_diagnostics_keeps_pending_unpublished(usage_runtime, monkeypatch):
    runtime = usage_runtime
    first = call(runtime, scan_budget=1)
    holder = cache_of(runtime)
    before = json.dumps(holder["cache"], sort_keys=True)
    original = passes._diagnostics
    def cancelled(*args, **kwargs):
        raise AgentBridgeError("CANCELLED", "during cumulative diagnostics")
    monkeypatch.setattr(passes, "_diagnostics", cancelled)
    error(runtime, "CANCELLED", cursor=first["nextCursor"])
    assert json.dumps(holder["cache"], sort_keys=True) == before
    monkeypatch.setattr(passes, "_diagnostics", original)
    assert call(runtime, cursor=first["nextCursor"])["data"]["coverage"]["complete"]


@pytest.mark.parametrize("raw", [NS(eventId=-1, usage=Usage.Clear), NS(eventId=1 << 32, usage=Usage.Clear),
    NS(eventId=20.0, usage=Usage.Clear), NS(eventId=0, usage=Usage.Clear),
    NS(eventId=20, usage=None), NS(eventId=20, usage=True), NS(eventId=20, usage=1.0),
    NS(eventId=20, usage="Clear"), NS(eventId=20, usage=-1), NS(eventId=20, usage=1 << 32)])
def test_malformed_native_records_fail_and_are_not_cached(usage_runtime, raw):
    runtime = usage_runtime
    runtime.usages[100] = [raw]
    error(runtime, "REPLAY_ERROR")
    assert cache_of(runtime)["cache"]["resources"] == {}


def test_unknown_numeric_enum_and_invalid_unicode_are_safe(usage_runtime):
    runtime = usage_runtime
    runtime.ctx.controller.resources[0].name = "texture\udcff" * 100
    class OddUsage:
        name = "future\udcff" * 100
        def __int__(self):
            return 1000
    runtime.usages[100] = [NS(eventId=20, usage=OddUsage())]
    response = call(runtime)
    row = response["data"]["resources"][0]
    assert row["nameTruncated"] and row["nameEscapedInvalidUnicode"]
    assert row["usageTypes"][0]["usageTruncated"]
    assert row["usageTypes"][0]["access"] == "unknown"
    assert not row["hasReadUsage"] and not row["hasWriteUsage"]
    assert response["truncated"] and not response["data"]["coverage"]["complete"]


def test_all_known_native_enums_are_covered():
    for name in _known:
        assert passes._access(name) != "unknown"


def test_python36_compatible():
    ast.parse(Path(passes.__file__).read_text(encoding="utf-8"), feature_version=(3, 6))
