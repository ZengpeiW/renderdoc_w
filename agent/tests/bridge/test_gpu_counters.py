from __future__ import annotations

import ast
from enum import IntEnum
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

from fake_renderdoc import FakeContext, module, root_actions
from rendertest_agent_bridge.indexes import SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError
from rendertest_agent_bridge.lifecycle import CaptureState
from rendertest_agent_bridge.methods import gpu_counters
from rendertest_agent_bridge.paging import encode_cursor, query_key
from rendertest_agent_bridge.replay import ReplayRunner


class Counter(IntEnum):
    EventGPUDuration = 1
    Vertices = 2


class NativeToken(int):
    """Vendor ID need not be constructible as a predefined Python GPUCounter enum."""


class Context:
    deadline_at = float("inf")

    def __init__(self, state, method, params):
        self.state = state.snapshot()
        self.method = method
        self.params = params

    def check_cancelled(self):
        pass


def native(event, seconds, counter=1):
    return NS(eventId=event, counter=counter, value=NS(d=seconds))


@pytest.fixture
def counter_runtime(tmp_path, monkeypatch):
    rd = module()
    rd.GPUCounter = Counter
    monkeypatch.setitem(sys.modules, "renderdoc", rd)
    state = CaptureState()
    state.capture_loaded("fixture.rdc", "D3D12", selected=(5, None), current=(9, None))
    ctx = FakeContext()
    replay = ReplayRunner(state, lambda message: None)
    replay.manager = ctx.manager
    actions = root_actions()
    ctx.controller.GetRootActions = lambda: actions
    runtime = NS(root=str(tmp_path), instance_id="test-counters", state=state, replay=replay,
                 cache=SessionCache(), ctx=ctx, actions=actions,
                 tokens=[Counter.EventGPUDuration, Counter.Vertices, NativeToken(1000001)],
                 descriptions={}, describe_calls=[], fetch_calls=[], forced=[], dirty=False,
                 fatal=0, native_results=[native(5, 0.002), native(8, 0.001), native(9, 0.0)])
    for token in runtime.tokens:
        runtime.descriptions[int(token)] = NS(
            counter=int(token), name="Counter %s" % token, category="Vendor" if int(token) > 2 else "Built-in",
            description="Native counter description", resultType=NS(name="Float" if token == 1 else "UInt"),
            resultByteWidth=8, unit=NS(name="Seconds" if token == 1 else "Absolute"),
            uuid=NS(words=(1, 2, 3, int(token))))
    ctx.controller.EnumerateCounters = lambda: runtime.tokens
    def describe(token):
        # Verify original enumeration token is retained for vendor counter descriptions.
        assert any(token is value for value in runtime.tokens)
        runtime.describe_calls.append(token)
        return runtime.descriptions[int(token)]
    ctx.controller.DescribeCounter = describe
    def fetch(counters):
        runtime.fetch_calls.append(list(counters))
        assert counters == [Counter.EventGPUDuration]
        runtime.dirty = True
        return runtime.native_results
    ctx.controller.FetchCounters = fetch
    original_set = ctx.controller.SetFrameEvent
    def set_event(event, force):
        runtime.forced.append((event, force))
        if force or event != ctx.controller.current:
            runtime.dirty = False
        original_set(event, force)
    ctx.controller.SetFrameEvent = set_event
    ctx.controller.GetFatalErrorStatus = lambda: NS(code=runtime.fatal)
    return runtime


def call(runtime, method="get_gpu_duration", **params):
    response = gpu_counters.HANDLERS[method](runtime, Context(runtime.state, method, params))
    assert response.result["artifacts"] == []
    assert len(json.dumps(response.result, ensure_ascii=True, allow_nan=False).encode()) < 256 * 1024
    return response


def error(runtime, code="INVALID_REQUEST", method="get_gpu_duration", **params):
    with pytest.raises(AgentBridgeError) as caught:
        call(runtime, method, **params)
    assert caught.value.code == code


def test_catalog_paging_vendor_identity_and_no_sampling(counter_runtime):
    runtime = counter_runtime
    first = call(runtime, "list_gpu_counters", limit=1).result
    assert first["data"]["total"] == 3
    duration = first["data"]["counters"][0]
    assert duration["id"] == 1 and duration["resultType"] == "Float"
    assert duration["resultByteWidth"] == 8 and duration["unit"] == "Seconds"
    assert duration["uuid"] == [1, 2, 3, 1] and duration["durationSamplingSupported"] is True
    second = call(runtime, "list_gpu_counters", limit=1000, cursor=first["nextCursor"]).result
    assert [row["id"] for row in second["data"]["counters"]] == [2, 1000001]
    assert all(not row["durationSamplingSupported"] for row in second["data"]["counters"])
    assert second["nextCursor"] is None
    assert runtime.fetch_calls == [] and runtime.forced == []


def test_duration_seconds_ms_sort_coverage_and_forced_restore(counter_runtime):
    runtime = counter_runtime
    response = call(runtime)
    data = response.result["data"]
    assert data["samplingScope"] == "capture" and data["cached"] is False
    assert data["sampleId"] and data["sampledAtUtc"].endswith("Z")
    assert data["scopeActionCount"] == 4 and data["validDurationCount"] == 3
    assert data["missingDurationCount"] == 1 and data["invalidDurationCount"] == 0
    assert [r["eventId"] for r in data["events"]] == [5, 8, 9, 1]
    assert data["events"][0]["durationSeconds"] == 0.002
    assert data["events"][0]["durationMs"] == 2.0
    assert data["events"][2]["status"] == "valid" and data["events"][2]["durationMs"] == 0
    assert data["events"][3]["status"] == "missing" and data["events"][3]["durationMs"] is None
    assert not any("sum" in key.lower() for key in data)
    assert runtime.fetch_calls == [[Counter.EventGPUDuration]]
    assert runtime.forced == [(9, True)] and runtime.dirty is False
    assert response.meta["replayRestoreForced"] is True
    assert response.meta["eventIdBefore"] == response.meta["eventIdAfter"] == 9
    assert runtime.state.snapshot()["selectedEventId"] == 5


def test_scope_pagination_ties_and_cache_independent_of_ui_event(counter_runtime):
    runtime = counter_runtime
    runtime.native_results = [native(5, 0.001), native(8, 0.001)]
    first = call(runtime, parent_event_id=1, limit=1).result
    assert first["data"]["scopeActionCount"] == 3
    assert first["data"]["events"][0]["eventId"] == 5
    runtime.state.current_event_changed(5, None)
    runtime.ctx.controller.SetFrameEvent(5, False)
    restored_count = len(runtime.forced)
    second = call(runtime, parent_event_id=1, limit=10, cursor=first["nextCursor"])
    assert second.result["data"]["cached"] is True
    assert second.result["data"]["sampleId"] == first["data"]["sampleId"]
    assert [r["eventId"] for r in second.result["data"]["events"]] == [8, 1]
    assert second.meta == {} and len(runtime.forced) == restored_count
    leaf = call(runtime, parent_event_id=8, sort_by="event_id").result["data"]
    assert leaf["scopeActionCount"] == 1 and leaf["events"][0]["eventId"] == 8
    frame = call(runtime, sort_by="event_id").result["data"]
    assert [r["eventId"] for r in frame["events"]] == [1, 5, 8, 9]
    assert len(runtime.fetch_calls) == 1 and runtime.ctx.controller.current == 5


def test_invalid_duplicate_unknown_and_wrong_counter_quarantined(counter_runtime):
    runtime = counter_runtime
    runtime.native_results = [native(5, -1), native(8, float("nan")), native(9, float("inf")),
                              native(5, 0.3), native(5, 0.4), native(6, 1), native(123456, 2),
                              native(8, 0.8, counter=2), NS(eventId=None)]
    data = call(runtime, sort_by="event_id").result["data"]
    rows = {row["eventId"]: row for row in data["events"]}
    assert rows[5]["status"] == "duplicate" and rows[5]["nativeResultCount"] == 3
    assert rows[5]["rawDurations"] == [-1, 0.3, 0.4]
    assert rows[8]["status"] == "invalid" and rows[8]["rawDuration"] == "NaN"
    assert rows[9]["status"] == "invalid" and rows[9]["rawDuration"] == "Infinity"
    assert all(row["durationMs"] is None and row["durationSeconds"] is None for row in rows.values())
    assert data["validDurationCount"] == 0 and data["duplicateDurationCount"] == 1
    assert data["diagnostics"] == dict(rawResultCount=9, unknownEventResults=2,
        unknownEventIds=[6, 123456], counterMismatchResults=1, malformedResults=1, duplicateResultCount=2)


@pytest.mark.parametrize("seconds", [-1, -float("inf"), float("nan"), float("inf"), 1e308])
def test_invalid_values_never_become_zero(counter_runtime, seconds):
    counter_runtime.native_results = [native(5, seconds)]
    data = call(counter_runtime, parent_event_id=5).result["data"]
    assert data["events"][0]["status"] == "invalid"
    assert data["events"][0]["durationSeconds"] is None and data["events"][0]["durationMs"] is None


def test_empty_sample_is_missing_with_no_performance_claim(counter_runtime):
    counter_runtime.native_results = []
    response = call(counter_runtime)
    assert response.result["data"]["validDurationCount"] == 0
    assert response.result["data"]["missingDurationCount"] == 4
    assert any("no duration or performance conclusion" in w for w in response.result["warnings"])


def test_refresh_changes_sample_and_invalidates_old_cursor(counter_runtime):
    runtime = counter_runtime
    first = call(runtime, limit=1).result
    error(runtime, refresh=True, cursor=first["nextCursor"])
    assert len(runtime.fetch_calls) == 1
    refreshed = call(runtime, refresh=True, limit=1).result
    assert refreshed["data"]["sampleId"] != first["data"]["sampleId"]
    assert refreshed["data"]["cached"] is False and len(runtime.fetch_calls) == 2
    error(runtime, cursor=first["nextCursor"])
    error(runtime, cursor=refreshed["nextCursor"], parent_event_id=1)
    error(runtime, cursor=refreshed["nextCursor"], sort_by="event_id")
    assert len(runtime.fetch_calls) == 2


def test_cursor_session_and_missing_cache_rejected_without_sampling(counter_runtime):
    runtime = counter_runtime
    first = call(runtime, limit=1).result
    runtime.state.capture_loaded("new.rdc", "D3D12", current=(9, None))
    error(runtime, cursor=first["nextCursor"])
    assert len(runtime.fetch_calls) == 1
    new = call(runtime, limit=1).result
    assert len(runtime.fetch_calls) == 2 and new["data"]["sampleId"] != first["data"]["sampleId"]
    session = runtime.state.snapshot()["captureSessionId"]
    forged = encode_cursor("get_gpu_duration", session, query_key(
        session, new["data"]["sampleId"], None, "duration"), 99999)
    error(runtime, cursor=forged)


@pytest.mark.parametrize("params", [
    {"parent_event_id": True}, {"parent_event_id": 1.0}, {"parent_event_id": -1},
    {"parent_event_id": 1 << 32}, {"limit": True}, {"limit": 1.0}, {"limit": None},
    {"limit": 0}, {"limit": 1001}, {"sort_by": None}, {"sort_by": "event"},
    {"refresh": 1}, {"refresh": None}, {"cursor": 1}, {"cursor": ""}, {"cursor": "x" * 1025},
])
def test_bad_parameters_do_not_fetch(counter_runtime, params):
    error(counter_runtime, **params)
    assert counter_runtime.fetch_calls == []


@pytest.mark.parametrize("parent", [6, 7, 999])
def test_scope_requires_exact_action_before_sampling(counter_runtime, parent):
    error(counter_runtime, "INVALID_EVENT", parent_event_id=parent)
    assert counter_runtime.fetch_calls == []


@pytest.mark.parametrize("case", ["absent", "width", "type", "unit"])
def test_unsupported_duration_is_never_sampled(counter_runtime, case):
    runtime = counter_runtime
    desc = runtime.descriptions[1]
    if case == "absent":
        runtime.tokens = runtime.tokens[1:]
    elif case == "width":
        desc.resultByteWidth = 4
    elif case == "type":
        desc.resultType = NS(name="Double")
    else:
        desc.unit = NS(name="Milliseconds")
    error(runtime, "UNSUPPORTED")
    assert runtime.fetch_calls == []


@pytest.mark.parametrize("failure", ["exception", "cancel", "timeout", "fatal", "session", "restore"])
def test_sampling_failure_restores_and_does_not_publish(counter_runtime, failure):
    runtime = counter_runtime
    context = Context(runtime.state, "get_gpu_duration", {})
    original_fetch = runtime.ctx.controller.FetchCounters
    def fetch(counters):
        rows = original_fetch(counters)
        if failure == "exception":
            raise RuntimeError("native counters failed")
        if failure == "fatal":
            runtime.fatal = 1
        if failure == "session":
            runtime.state.capture_loaded("new.rdc", "D3D12", current=(9, None))
        return rows
    runtime.ctx.controller.FetchCounters = fetch
    if failure in ("cancel", "timeout"):
        def check():
            if runtime.fetch_calls:
                raise AgentBridgeError("CANCELLED" if failure == "cancel" else "TIMEOUT", "stop")
        context.check_cancelled = check
    if failure == "restore":
        def restore(event, force):
            runtime.forced.append((event, force))
            raise RuntimeError("restore failed")
        runtime.ctx.controller.SetFrameEvent = restore
    holder = gpu_counters._state(runtime, context)
    with pytest.raises(AgentBridgeError):
        gpu_counters.get_gpu_duration(runtime, context)
    assert runtime.forced == [(9, True)]
    assert holder["sample"] is None


def test_failed_refresh_preserves_last_successful_sample(counter_runtime):
    runtime = counter_runtime
    first = call(runtime, limit=1).result
    def fail(counters):
        raise RuntimeError("refresh failure")
    runtime.ctx.controller.FetchCounters = fail
    error(runtime, "REPLAY_ERROR", refresh=True)
    second = call(runtime, cursor=first["nextCursor"]).result
    assert second["data"]["sampleId"] == first["data"]["sampleId"]
    assert second["data"]["cached"] is True


def test_missing_ui_restore_target_rejects_fetch(counter_runtime):
    runtime = counter_runtime
    runtime.state.current_event_changed(None, None)
    error(runtime, "INVALID_EVENT")
    assert runtime.fetch_calls == [] and runtime.forced == []


def test_restore_latest_ui_event_and_forced_reconcile(counter_runtime):
    runtime = counter_runtime
    original_fetch = runtime.ctx.controller.FetchCounters
    def fetch(counters):
        rows = original_fetch(counters)
        runtime.state.current_event_changed(5, None)
        return rows
    runtime.ctx.controller.FetchCounters = fetch
    original_set = runtime.ctx.controller.SetFrameEvent
    def set_event(event, force):
        original_set(event, force)
        if event == 5:
            runtime.state.current_event_changed(8, None)
    runtime.ctx.controller.SetFrameEvent = set_event
    response = call(runtime)
    assert runtime.forced == [(5, True), (8, True)]
    assert response.meta["eventIdBefore"] == 9 and response.meta["eventIdAfter"] == 8
    assert runtime.ctx.controller.current == 8 and not runtime.dirty


def test_force_reconcile_failure_does_not_publish(counter_runtime):
    runtime = counter_runtime
    original_set = runtime.ctx.controller.SetFrameEvent
    def set_event(event, force):
        original_set(event, force)
        if event == 9:
            runtime.state.current_event_changed(8, None)
        else:
            raise RuntimeError("reconcile failed")
    runtime.ctx.controller.SetFrameEvent = set_event
    error(runtime, "REPLAY_ERROR")
    holder = gpu_counters._state(runtime, Context(runtime.state, "get_gpu_duration", {}))
    assert holder["sample"] is None


@pytest.mark.parametrize("budget", ["native", "sample", "catalog", "descriptions"])
def test_budgets_reject_without_publishing_partial_sample(counter_runtime, monkeypatch, budget):
    runtime = counter_runtime
    name, value = {"native": ("MAX_NATIVE_RESULTS", 1), "sample": ("MAX_SAMPLE_BYTES", 4097),
                   "catalog": ("MAX_CATALOG_COUNTERS", 1),
                   "descriptions": ("MAX_DESCRIPTION_CACHE_BYTES", 1)}[budget]
    monkeypatch.setattr(gpu_counters, name, value)
    error(runtime, "LIMIT_EXCEEDED")
    holder = gpu_counters._state(runtime, Context(runtime.state, "get_gpu_duration", {}))
    assert holder["sample"] is None
    if runtime.fetch_calls:
        assert runtime.forced == [(9, True)] and not runtime.dirty


def test_duplicate_evidence_accounted_in_sample_budget(counter_runtime, monkeypatch):
    runtime = counter_runtime
    runtime.native_results = [native(5, 1.234567890123456), native(5, 2.345678901234567),
                              native(5, 3.456789012345678)]
    response = call(runtime, parent_event_id=5)
    context = Context(runtime.state, "get_gpu_duration", {})
    holder = gpu_counters._state(runtime, context)
    measured = holder["sample"]["measured"]
    exact_budget = 4096 + gpu_counters._size(measured[5]) + 64
    assert response.result["data"]["events"][0]["nativeResultCount"] == 3
    monkeypatch.setattr(gpu_counters, "MAX_SAMPLE_BYTES", exact_budget - 1)
    original_sample = holder["sample"]
    error(runtime, "LIMIT_EXCEEDED", refresh=True)
    assert holder["sample"] is original_sample


def test_catalog_long_strings_unicode_and_page_budget(counter_runtime):
    runtime = counter_runtime
    runtime.tokens = [NativeToken(i) for i in range(1, 100)]
    template = runtime.descriptions[1]
    runtime.descriptions = {int(token): NS(**dict(vars(template), counter=int(token),
        name="n\udcff" * 300, category="c" * 1000, description="\u6e90" * 10000)) for token in runtime.tokens}
    cursor = None
    seen = []
    while True:
        page = call(runtime, "list_gpu_counters", cursor=cursor, limit=1000).result
        json.dumps(page, ensure_ascii=False).encode("utf-8")
        assert page["truncated"]
        seen.extend(row["id"] for row in page["data"]["counters"])
        assert all(row["descriptionTruncated"] for row in page["data"]["counters"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert seen == list(range(1, 100)) and runtime.fetch_calls == []


def test_python36_compatible():
    for filename in (gpu_counters.__file__, sys.modules[ReplayRunner.__module__].__file__):
        ast.parse(Path(filename).read_text(encoding="utf-8"), feature_version=(3, 6))
