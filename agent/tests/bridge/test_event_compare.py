from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

import fake_renderdoc
from fake_renderdoc import (FakeContext, FakePipe, ResourceId, ShaderStage, VarType, module,
                            shader_value)
from rendertest_agent_bridge.indexes import SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError
from rendertest_agent_bridge.lifecycle import CaptureState
from rendertest_agent_bridge.methods import event_compare as compare
from rendertest_agent_bridge.methods import pipeline
from rendertest_agent_bridge.paging import encode_cursor
from rendertest_agent_bridge.replay import ReplayRunner


class Context:
    method = "compare_events"
    deadline_at = float("inf")

    def __init__(self, state, params):
        self.state = state.snapshot()
        self.params = params

    def check_cancelled(self):
        pass


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "renderdoc", module())
    state = CaptureState()
    state.capture_loaded("fixture.rdc", "D3D12", current=(9, None))
    ctx = FakeContext()
    replay = ReplayRunner(state, lambda message: None)
    replay.manager = ctx.manager
    return NS(root=str(tmp_path), instance_id="test-compare", state=state, replay=replay,
              cache=SessionCache(), ctx=ctx)


def call(runtime, **params):
    values = dict(before_event_id=5, after_event_id=8)
    values.update(params)
    return compare.compare_events(runtime, Context(runtime.state, values))


def error(code, fn):
    with pytest.raises(AgentBridgeError) as raised:
        fn()
    assert raised.value.code == code
    return raised.value


def minimal(monkeypatch, runtime, before, after):
    def read(controller, *args):
        value = before if controller.current == 5 else after
        value = dict(shaders={}, **value)
        return copy.deepcopy(value), False
    monkeypatch.setattr(pipeline, "read_pipeline_state", read)


def test_native_pipeline_diff_and_exact_api_eids_restore_ui(runtime):
    response = call(runtime, limit=1000)
    data = response.result["data"]
    assert data["comparison"] == "different" and data["coverage"]["complete"]
    assert any(row["path"] == "/pipeline/shaders/compute/resourceId"
               for row in data["differences"]), data["differences"]
    assert response.meta["replayEventRestored"]
    assert response.meta["eventIdBefore"] == response.meta["eventIdAfter"] == 9
    assert runtime.ctx.controller.frame_events == [5, 9, 8, 9]
    api = call(runtime, before_event_id=3, after_event_id=5).result["data"]
    assert api["coverage"]["before"]["eventId"] == 3
    assert api["coverage"]["before"]["ownerActionEventId"] == 5
    assert runtime.ctx.controller.frame_events[-4:] == [3, 9, 5, 9]
    scope = data["coverage"]["scope"]
    assert scope["viewportIndices"] == scope["scissorIndices"] == [0]
    assert scope["usedDescriptorsOnly"] and scope["constantBlockArrayElement"] == 0


def test_same_event_reads_once_and_equal_in_scope(runtime):
    data = call(runtime, after_event_id=5).result["data"]
    assert data["comparison"] == "equal-in-scope" and data["differences"] == []
    assert data["totalDifferencesExact"] and data["coverage"]["complete"]
    assert runtime.ctx.controller.frame_events == [5, 9]


@pytest.mark.parametrize("params", [
    {"before_event_id": None}, {"after_event_id": None}, {"before_event_id": True},
    {"after_event_id": "5"}, {"before_event_id": -1}, {"after_event_id": 2**32},
    {"stages": "pixel"}, {"stages": ["fragment"]}, {"include_bindings": 1},
    {"binding_limit": 0}, {"binding_limit": 257}, {"binding_limit": True},
    {"limit": 0}, {"limit": 1001}, {"limit": True}, {"cursor": ""}, {"cursor": False},
    {"cbuffers": {}}, {"cbuffers": [{}] * 9}, {"cbuffers": [{}]},
    {"cbuffers": [{"stage": "pixel", "index": 0, "name": "x"}]},
    {"cbuffers": [{"stage": "pixel", "index": -1}]},
    {"cbuffers": [{"stage": "pixel", "index": True}]},
    {"cbuffers": [{"stage": "pixel", "name": 2}]},
    {"cbuffers": [{"stage": "pixel", "index": 0, "member_path": None}]},
    {"cbuffers": [{"stage": "pixel", "index": 0, "member_path": "a..b"}]},
    {"cbuffers": [{"stage": "pixel", "index": 0, "max_depth": 5}]},
    {"cbuffers": [{"stage": "pixel", "index": 0, "member_limit": 1001}]},
    {"cbuffers": [{"stage": "pixel", "index": 0, "other": 1}]},
])
def test_invalid_parameters_do_not_move_replay(runtime, params):
    error("INVALID_REQUEST", lambda: call(runtime, **params))
    assert runtime.ctx.controller.frame_events == []


@pytest.mark.parametrize("event", [0, 10, 999999])
def test_nonexistent_events_rejected(runtime, event):
    error("INVALID_EVENT", lambda: call(runtime, after_event_id=event))
    assert runtime.ctx.controller.frame_events == []


def test_missing_null_nonfinite_pointer_escape_and_types(runtime, monkeypatch):
    minimal(monkeypatch, runtime,
            {"x": None, "bool": True, "nonfinite": [float("nan"), float("inf")], "a/b~c": 1},
            {"bool": 1, "nonfinite": [float("nan"), -float("inf")], "a/b~c": 2})
    response = call(runtime, include_bindings=False).result
    rows = {row["path"]: row for row in response["data"]["differences"]}
    assert rows["/pipeline/x"]["before"] == {"present": True, "value": None}
    assert rows["/pipeline/x"]["after"] == {"present": False}
    assert rows["/pipeline/a~1b~0c"]["after"]["value"] == 2
    assert rows["/pipeline/bool"]["before"]["value"] is True
    assert rows["/pipeline/nonfinite/1"]["before"]["value"] == "Infinity"
    assert "/pipeline/nonfinite/0" not in rows
    json.dumps(response, allow_nan=False)


def test_container_type_changes_are_bounded_and_explicit(runtime, monkeypatch):
    minimal(monkeypatch, runtime, {"x": {"a": 1}}, {"x": None})
    rows = call(runtime, include_bindings=False).result["data"]["differences"]
    assert rows[0] == {"path": "/pipeline/x", "kind": "type-change",
                       "before": {"present": True, "valueType": "object"},
                       "after": {"present": True, "value": None}}
    assert rows[1]["path"] == "/pipeline/x/a"


def test_unknown_native_objects_never_repr_or_false_equal(runtime, monkeypatch):
    class Native:
        def __repr__(self):
            raise AssertionError("Native repr must not be called")
    minimal(monkeypatch, runtime, {"x": Native()}, {"x": Native()})
    response = call(runtime, include_bindings=False).result
    assert response["data"]["comparison"] == "indeterminate"
    assert response["truncated"] and response["warnings"]
    assert response["data"]["differences"] == []


def test_paging_snapshot_stability_binding_and_eviction(runtime, monkeypatch):
    minimal(monkeypatch, runtime, {"values": list(range(7))}, {"values": list(range(10, 17))})
    first = call(runtime, include_bindings=False, limit=2).result
    cursor = first["nextCursor"]
    assert first["data"]["totalDifferences"] == 7
    # Pages never reread even if the replay provider subsequently changes/fails.
    monkeypatch.setattr(pipeline, "read_pipeline_state", lambda *a: pytest.fail("cache reread"))
    second = call(runtime, include_bindings=False, limit=5, cursor=cursor)
    assert second.meta["cachedComparison"] and second.result["nextCursor"] is None
    assert len(second.result["data"]["differences"]) == 5
    assert runtime.ctx.controller.frame_events == [5, 9, 8, 9]
    error("INVALID_REQUEST", lambda: call(runtime, cursor=cursor))
    error("INVALID_REQUEST", lambda: call(runtime, include_bindings=False, binding_limit=63,
                                          cursor=cursor))
    error("INVALID_REQUEST", lambda: call(runtime, include_bindings=False, before_event_id=8,
                                          after_event_id=5, cursor=cursor))
    cache = runtime.cache.get(runtime.state.snapshot()["captureSessionId"],
                              "event-comparisons", dict)
    entry = next(iter(cache.values()))
    forged = encode_cursor("compare_events", runtime.state.snapshot()["captureSessionId"],
                           entry["key"], 99)
    error("INVALID_REQUEST", lambda: call(runtime, include_bindings=False, cursor=forged))
    minimal(monkeypatch, runtime, {"values": [1, 2]}, {"values": [3, 4]})
    call(runtime, before_event_id=2, include_bindings=False)
    call(runtime, before_event_id=3, include_bindings=False)
    assert len(cache) == compare.CACHE_ENTRIES
    error("INVALID_REQUEST", lambda: call(runtime, include_bindings=False, cursor=cursor))
    # Rebuilding the same query cannot make an old cursor address a new diff set.
    call(runtime, include_bindings=False)
    error("INVALID_REQUEST", lambda: call(runtime, include_bindings=False, cursor=cursor))


def test_new_initial_request_refreshes_snapshot_and_invalidates_old_cursor(runtime, monkeypatch):
    minimal(monkeypatch, runtime, {"values": [1, 2]}, {"values": [3, 4]})
    first = call(runtime, include_bindings=False, limit=1).result
    minimal(monkeypatch, runtime, {"values": [10, 20]}, {"values": [30, 40]})
    second = call(runtime, include_bindings=False, limit=1)
    assert not second.meta["cachedComparison"]
    assert second.result["data"]["differences"][0]["before"]["value"] == 10
    assert runtime.ctx.controller.frame_events == [5, 9, 8, 9, 5, 9, 8, 9]
    error("INVALID_REQUEST", lambda: call(runtime, include_bindings=False,
                                          cursor=first["nextCursor"]))


def test_refreshed_snapshot_is_newest_for_cache_eviction(runtime, monkeypatch):
    minimal(monkeypatch, runtime, {"values": [1, 2]}, {"values": [3, 4]})
    options = dict(before_event_id=5, after_event_id=8, include_bindings=False, limit=1)
    first_a = call(runtime, **options).result
    query_b = dict(options, stages=["pixel"])
    first_b = call(runtime, **query_b).result
    refreshed_a = call(runtime, **options).result
    query_c = dict(options, stages=["vertex"])
    call(runtime, **query_c)

    continued_a = call(runtime, **dict(options, cursor=refreshed_a["nextCursor"]))
    assert continued_a.meta["cachedComparison"]
    assert continued_a.result["data"]["differences"][0]["path"] == "/pipeline/values/1"
    assert continued_a.result["nextCursor"] is None
    error("INVALID_REQUEST", lambda: call(runtime, **dict(query_b, cursor=first_b["nextCursor"])))
    error("INVALID_REQUEST", lambda: call(runtime, **dict(options, cursor=first_a["nextCursor"])))


def test_after_exception_restores_both_events(runtime, monkeypatch):
    original = pipeline.read_pipeline_state
    def read(controller, *args):
        if controller.current == 8:
            raise RuntimeError("after read failed")
        return original(controller, *args)
    monkeypatch.setattr(pipeline, "read_pipeline_state", read)
    error("REPLAY_ERROR", lambda: call(runtime))
    assert runtime.ctx.controller.frame_events == [5, 9, 8, 9]
    assert runtime.ctx.controller.current == 9


def test_capture_change_and_deadline_cancel_even_cached_results(runtime, monkeypatch):
    response = call(runtime, limit=1)
    context = Context(runtime.state, dict(before_event_id=5, after_event_id=8,
                                         cursor=response.result["nextCursor"]))
    runtime.state.capture_loaded("other.rdc", "D3D12", current=(9, None))
    error("CAPTURE_CHANGED", lambda: compare.compare_events(runtime, context))
    error("INVALID_REQUEST", lambda: call(runtime, cursor=response.result["nextCursor"]))
    context = Context(runtime.state, dict(before_event_id=5, after_event_id=8))
    context.deadline_at = 0
    error("TIMEOUT", lambda: compare.compare_events(runtime, context))
    context.deadline_at = float("inf")
    context.check_cancelled = lambda: (_ for _ in ()).throw(AgentBridgeError("CANCELLED", "stop"))
    error("CANCELLED", lambda: compare.compare_events(runtime, context))


def test_cancellation_inside_second_snapshot_restores_ui(runtime, monkeypatch):
    context = Context(runtime.state, dict(before_event_id=5, after_event_id=8))
    def check():
        if runtime.ctx.controller.current == 8:
            raise AgentBridgeError("CANCELLED", "stop")
    context.check_cancelled = check
    error("CANCELLED", lambda: compare.compare_events(runtime, context))
    assert runtime.ctx.controller.current == 9
    assert runtime.ctx.controller.frame_events == [5, 9, 8, 9]


def test_vertex_inputs_and_bindings_truncation_prevent_equal(runtime, monkeypatch):
    original_inputs = FakePipe.GetVertexInputs
    monkeypatch.setattr(FakePipe, "GetVertexInputs", lambda self: original_inputs(self) * 33)
    response = call(runtime, after_event_id=5).result
    assert response["data"]["comparison"] == "indeterminate"
    assert any(i["reason"] == "vertex-inputs-truncated"
               for i in response["data"]["coverage"]["before"]["issues"])
    runtime.cache.clear()
    monkeypatch.setattr(FakePipe, "GetVertexInputs", original_inputs)
    original = FakePipe.GetReadOnlyResources
    monkeypatch.setattr(FakePipe, "GetReadOnlyResources", lambda self, *a: original(self, *a) * 3)
    data = call(runtime, after_event_id=5, binding_limit=1).result["data"]
    assert data["comparison"] == "indeterminate"
    assert any(i["reason"] == "bindings-truncated" for i in data["coverage"]["before"]["issues"])


def test_missing_reflection_is_unavailable_not_equal(runtime, monkeypatch):
    monkeypatch.setattr(FakePipe, "GetShaderReflection", lambda self, stage: None)
    data = call(runtime, after_event_id=5).result["data"]
    assert data["comparison"] == "indeterminate" and not data["coverage"]["complete"]
    assert any(i["reason"] == "shader-reflection-unavailable"
               for i in data["coverage"]["before"]["issues"])


def test_unknown_sequence_tail_does_not_become_removed_elements(runtime, monkeypatch):
    before = {"values": compare._Sequence([1], False)}
    after = {"values": [1, 2, 3]}
    minimal(monkeypatch, runtime, before, after)
    data = call(runtime, include_bindings=False).result["data"]
    assert data["differences"] == [] and data["comparison"] == "indeterminate"


@pytest.mark.parametrize("budget, value", [("MAX_SNAPSHOT_NODES", 8),
                                           ("MAX_SNAPSHOT_BYTES", 80),
                                           ("MAX_STRING_BYTES", 8)])
def test_normalization_budgets_are_explicit(runtime, monkeypatch, budget, value):
    minimal(monkeypatch, runtime, {"values": ["long-value-" * 20] * 10},
            {"values": ["long-value-" * 20] * 10})
    monkeypatch.setattr(compare, budget, value)
    response = call(runtime, include_bindings=False).result
    assert response["data"]["comparison"] == "indeterminate"
    assert not response["data"]["coverage"]["complete"] and response["warnings"]


def test_difference_and_page_byte_budgets(runtime, monkeypatch):
    minimal(monkeypatch, runtime, {"values": [0] * 10}, {"values": list(range(1, 11))})
    monkeypatch.setattr(compare, "MAX_DIFFERENCES", 3)
    response = call(runtime, include_bindings=False).result
    assert response["data"]["comparison"] == "different"
    assert response["data"]["totalDifferences"] == 3
    assert not response["data"]["totalDifferencesExact"] and response["truncated"]
    assert response["nextCursor"] is None
    runtime.cache.clear()
    monkeypatch.setattr(compare, "MAX_DIFFERENCES", 8192)
    monkeypatch.setattr(compare, "MAX_DIFF_BYTES", 250)
    response = call(runtime, include_bindings=False).result
    assert 0 < response["data"]["totalDifferences"] < 10
    assert not response["data"]["totalDifferencesExact"]
    runtime.cache.clear()
    monkeypatch.setattr(compare, "MAX_DIFF_BYTES", 2 * 1024 * 1024)
    minimal(monkeypatch, runtime, {"values": ["a" * 4000] * 10}, {"values": ["b" * 4000] * 10})
    monkeypatch.setattr(compare, "MAX_PAGE_BYTES", 24 * 1024)
    response = call(runtime, include_bindings=False, limit=1000).result
    assert 0 < len(response["data"]["differences"]) < 10 and response["nextCursor"]
    assert len(json.dumps(response).encode()) < compare.MAX_PAGE_BYTES


def graphics_at_both(monkeypatch):
    monkeypatch.setattr(FakePipe, "_shaders", lambda self: {ShaderStage.Pixel: 300})


def test_cbuffer_scalar_depth_zero_metadata_and_values(runtime, monkeypatch):
    graphics_at_both(monkeypatch)
    original = runtime.ctx.controller.GetCBufferVariableContents
    def read(*args):
        variables = original(*args)
        variables[1].value.u32v[0] = runtime.ctx.controller.current
        return variables
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents", read)
    selection = dict(stage="pixel", name="Globals", member_path="count", max_depth=0)
    data = call(runtime, before_event_id=4, after_event_id=5, cbuffers=[selection]).result["data"]
    assert data["coverage"]["complete"] and data["comparison"] == "different"
    rows = {row["path"]: row for row in data["differences"]}
    row = rows["/cbuffers/0/projection/selected/value/0"]
    assert row["before"]["value"] == 4 and row["after"]["value"] == 5
    same = call(runtime, after_event_id=5, cbuffers=[selection]).result["data"]
    assert same["comparison"] == "equal-in-scope" and same["coverage"]["complete"]


def test_cbuffer_different_layout_preserves_metadata_and_value_diffs(runtime, monkeypatch):
    graphics_at_both(monkeypatch)
    original_reflection = FakePipe.GetShaderReflection
    def reflection(self, stage):
        refl = original_reflection(self, stage)
        if self.controller.current == 5:
            refl.constantBlocks[0].variables[1].byteOffset = 20
            refl.constantBlocks[0].variables[1].type.name = "different-type"
        return refl
    monkeypatch.setattr(FakePipe, "GetShaderReflection", reflection)
    data = call(runtime, before_event_id=4, after_event_id=5,
                cbuffers=[dict(stage="pixel", index=0, member_path="count")]).result["data"]
    rows = {row["path"] for row in data["differences"]}
    assert "/cbuffers/0/projection/selected/byteOffset" in rows
    assert "/cbuffers/0/projection/selected/typeName" in rows


def test_missing_cbuffer_member_block_shader_and_ambiguous_names(runtime, monkeypatch):
    graphics_at_both(monkeypatch)
    original_reflection = FakePipe.GetShaderReflection
    def missing_member_reflection(self, stage):
        refl = original_reflection(self, stage)
        if self.controller.current == 4:
            refl.constantBlocks[0].variables = refl.constantBlocks[0].variables[:1]
        return refl
    monkeypatch.setattr(FakePipe, "GetShaderReflection", missing_member_reflection)
    original = runtime.ctx.controller.GetCBufferVariableContents
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents",
                        lambda *args: original(*args)[:1] if runtime.ctx.controller.current == 4
                        else original(*args))
    selection = dict(stage="pixel", index=0, member_path="count")
    data = call(runtime, before_event_id=4, after_event_id=5, cbuffers=[selection]).result["data"]
    row = next(row for row in data["differences"]
               if row["path"] == "/cbuffers/0/projection/selected/value/0")
    assert row["before"] == {"present": False}
    assert data["coverage"]["complete"]
    missing = call(runtime, after_event_id=5,
                   cbuffers=[dict(stage="pixel", name="does-not-exist")]).result["data"]
    assert missing["comparison"] == "equal-in-scope"
    # Exact names, not a case-insensitive first match.
    wrong_case = call(runtime, after_event_id=5,
                      cbuffers=[dict(stage="pixel", name="globals")]).result["data"]
    assert wrong_case["comparison"] == "equal-in-scope"
    def reflection(self, stage):
        refl = original_reflection(self, stage)
        refl.constantBlocks *= 2
        return refl
    monkeypatch.setattr(FakePipe, "GetShaderReflection", reflection)
    runtime.cache.clear()
    error("INVALID_REQUEST", lambda: call(runtime, cbuffers=[dict(stage="pixel", name="Globals")]))
    assert runtime.ctx.controller.current == 9


def test_cbuffer_decode_once_for_multiple_selectors_and_bound_size(runtime, monkeypatch):
    graphics_at_both(monkeypatch)
    calls = []
    original = runtime.ctx.controller.GetCBufferVariableContents
    def read(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents", read)
    selectors = [dict(stage="pixel", index=0, member_path=name) for name in ("tint", "count")]
    response = call(runtime, after_event_id=5, cbuffers=selectors).result
    assert len(calls) == 1 and response["data"]["comparison"] == "equal-in-scope"
    runtime.cache.clear()
    monkeypatch.setattr(compare, "MAX_BLOCK_BYTES", 16)
    data = call(runtime, after_event_id=5, cbuffers=selectors).result["data"]
    assert len(calls) == 1 and data["comparison"] == "indeterminate"
    assert any(i["reason"] == "constant-block-size-budget"
               for i in data["coverage"]["before"]["issues"])


def test_cbuffer_huge_binding_range_reads_only_block_bytes_and_preserves_metadata(runtime, monkeypatch):
    graphics_at_both(monkeypatch)
    original_block = FakePipe.GetConstantBlock
    def block(self, *args):
        bound = original_block(self, *args)
        bound.descriptor.byteSize = (1 << 40) if self.controller.current == 4 else 64
        return bound
    monkeypatch.setattr(FakePipe, "GetConstantBlock", block)
    original_read = runtime.ctx.controller.GetCBufferVariableContents
    reads = []
    def read(*args):
        reads.append((runtime.ctx.controller.current, args[-1]))
        return original_read(*args)
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents", read)
    data = call(runtime, before_event_id=4, after_event_id=5, include_bindings=False,
                cbuffers=[dict(stage="pixel", index=0)]).result["data"]
    assert reads == [(4, 32), (5, 32)]
    assert data["coverage"]["complete"]
    row = next(row for row in data["differences"] if row["path"] == "/cbuffers/0/binding/byteSize")
    assert row["before"]["value"] == 1 << 40 and row["after"]["value"] == 64


@pytest.mark.parametrize("binding_bytes", [-1, 0, 1, 31])
def test_cbuffer_short_or_empty_binding_is_unknown_without_native_decode(runtime, monkeypatch,
                                                                       binding_bytes):
    original = FakePipe.GetConstantBlock
    def block(self, *args):
        bound = original(self, *args)
        bound.descriptor.byteSize = binding_bytes
        return bound
    monkeypatch.setattr(FakePipe, "GetConstantBlock", block)
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents",
                        lambda *a: pytest.fail("Short range must not decode padded zeros"))
    data = call(runtime, after_event_id=5,
                cbuffers=[dict(stage="pixel", index=0, member_path="count")]).result["data"]
    assert data["comparison"] == "indeterminate" and not data["totalDifferencesExact"]
    assert any(i["reason"] == "constant-buffer-range-incomplete"
               for i in data["coverage"]["before"]["issues"])


@pytest.mark.parametrize("binding_bytes", [0, 1 << 40])
def test_push_constants_do_not_require_or_read_a_descriptor_range(runtime, monkeypatch, binding_bytes):
    original_reflection = FakePipe.GetShaderReflection
    def reflection(self, stage):
        refl = original_reflection(self, stage)
        refl.constantBlocks[0].bufferBacked = False
        return refl
    monkeypatch.setattr(FakePipe, "GetShaderReflection", reflection)
    original_block = FakePipe.GetConstantBlock
    def block(self, *args):
        bound = original_block(self, *args)
        bound.descriptor.byteSize = binding_bytes
        bound.descriptor.resource = ResourceId(0)
        return bound
    monkeypatch.setattr(FakePipe, "GetConstantBlock", block)
    original_read = runtime.ctx.controller.GetCBufferVariableContents
    lengths = []
    def read(*args):
        lengths.append(args[-1])
        return original_read(*args)
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents", read)
    data = call(runtime, after_event_id=5,
                cbuffers=[dict(stage="pixel", index=0, member_path="count")]).result["data"]
    assert lengths == [0]
    assert data["comparison"] == "equal-in-scope" and data["coverage"]["complete"]


def test_cbuffer_limit_depth_unbound_and_decode_failure(runtime, monkeypatch):
    data = call(runtime, after_event_id=5,
                cbuffers=[dict(stage="pixel", index=0, member_limit=1)]).result["data"]
    assert data["comparison"] == "indeterminate"
    assert any(i["reason"] == "constant-members-truncated"
               for i in data["coverage"]["before"]["issues"])
    runtime.cache.clear()
    original = FakePipe.GetConstantBlock
    def block(self, *args):
        bound = original(self, *args)
        bound.descriptor.resource = ResourceId(0)
        return bound
    monkeypatch.setattr(FakePipe, "GetConstantBlock", block)
    data = call(runtime, after_event_id=5, cbuffers=[dict(stage="pixel", index=0)]).result["data"]
    assert data["comparison"] == "indeterminate"
    runtime.cache.clear()
    monkeypatch.setattr(FakePipe, "GetConstantBlock", original)
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents", lambda *a: [])
    data = call(runtime, after_event_id=5, cbuffers=[dict(stage="pixel", index=0)]).result["data"]
    assert data["comparison"] == "indeterminate"


def test_cbuffer_partial_native_decode_is_not_known_member_absence(runtime, monkeypatch):
    original = runtime.ctx.controller.GetCBufferVariableContents
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents",
                        lambda *args: original(*args)[:1])
    data = call(runtime, after_event_id=5,
                cbuffers=[dict(stage="pixel", index=0, member_path="count")]).result["data"]
    assert data["comparison"] == "indeterminate"
    assert not data["totalDifferencesExact"]
    assert any(i["reason"] == "constant-buffer-decode-incomplete"
               for i in data["coverage"]["before"]["issues"])


def test_cbuffer_nested_depth_and_projection_budget_are_incomplete(runtime, monkeypatch):
    original = FakePipe.GetShaderReflection
    def reflection(self, stage):
        refl = original(self, stage)
        refl.constantBlocks[0].variables = [NS(name="Outer", byteOffset=0, type=NS(
            name="OuterType", elements=1, arrayByteStride=0, members=[NS(
                name="Inner", byteOffset=0, type=NS(name="InnerType", elements=1,
                    arrayByteStride=0, members=[fake_renderdoc.constant("value", 0, "uint")]))]))]
        return refl
    monkeypatch.setattr(FakePipe, "GetShaderReflection", reflection)
    leaf = NS(name="value", type=VarType.UInt, rows=1, columns=1, members=[],
              value=shader_value(u32v=[7]))
    inner = NS(name="Inner", type=VarType.Struct, rows=0, columns=0, members=[leaf])
    outer = NS(name="Outer", type=VarType.Struct, rows=0, columns=0, members=[inner])
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents", lambda *a: [outer])
    selector = dict(stage="pixel", index=0, max_depth=0)
    data = call(runtime, after_event_id=5, cbuffers=[selector]).result["data"]
    assert data["comparison"] == "indeterminate"
    selector["max_depth"] = 2
    data = call(runtime, after_event_id=5, cbuffers=[selector]).result["data"]
    assert data["comparison"] == "equal-in-scope"
    selector["max_depth"] = 0
    selector["member_path"] = "Outer.Inner.value"
    data = call(runtime, after_event_id=5, cbuffers=[selector]).result["data"]
    assert data["comparison"] == "equal-in-scope"
    runtime.cache.clear()
    monkeypatch.setattr(compare.cbuffer_members, "MAX_PAYLOAD_BYTES", 32)
    data = call(runtime, after_event_id=5, cbuffers=[selector]).result["data"]
    assert data["comparison"] == "indeterminate"
    assert any(i["reason"] == "constant-member-response-budget"
               for i in data["coverage"]["before"]["issues"])


def test_cbuffer_partial_array_decode_cannot_report_equal_or_missing(runtime, monkeypatch):
    original = FakePipe.GetShaderReflection
    def reflection(self, stage):
        refl = original(self, stage)
        refl.constantBlocks[0].variables = [NS(name="Values", byteOffset=0,
            type=NS(name="uint", elements=3, arrayByteStride=4, members=[]))]
        return refl
    monkeypatch.setattr(FakePipe, "GetShaderReflection", reflection)
    leaf = NS(name="[0]", type=VarType.UInt, rows=1, columns=1, members=[],
              value=shader_value(u32v=[7]))
    array = NS(name="Values", type=VarType.Struct, rows=0, columns=0, members=[leaf])
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents", lambda *a: [array])
    for path in ("", "Values", "Values[2]"):
        data = call(runtime, after_event_id=5,
                    cbuffers=[dict(stage="pixel", index=0, member_path=path)]).result["data"]
        assert data["comparison"] == "indeterminate" and not data["totalDifferencesExact"]
        assert any(i["reason"] == "constant-member-decode-incomplete"
                   for i in data["coverage"]["before"]["issues"])


def test_helper_retains_python36_syntax():
    ast.parse(Path(compare.__file__).read_text(encoding="utf-8"), feature_version=(3, 6))
