from __future__ import annotations

import ast
from enum import IntFlag
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

from fake_renderdoc import (ActionFlags, FakeContext, ResourceId, ResourceType, ShaderStage,
                            action, module, root_actions)
from rendertest_agent_bridge.indexes import SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError
from rendertest_agent_bridge.lifecycle import CaptureState
from rendertest_agent_bridge.methods import shader_references as refs
from rendertest_agent_bridge.paging import encode_cursor, query_key
from rendertest_agent_bridge.replay import ReplayRunner


class Context:
    method = "find_shader_references"
    deadline_at = float("inf")

    def __init__(self, state, params):
        self.state = state.snapshot()
        self.params = params

    def check_cancelled(self):
        pass


@pytest.fixture
def reference_runtime(tmp_path, monkeypatch):
    rd = module()
    monkeypatch.setitem(sys.modules, "renderdoc", rd)
    state = CaptureState()
    state.capture_loaded("fixture.rdc", "D3D12", selected=(1, None), current=(9, None))
    ctx = FakeContext()
    replay = ReplayRunner(state, lambda message: None)
    replay.manager = ctx.manager
    actions = root_actions()
    ctx.controller.GetRootActions = lambda: actions
    ctx.controller.resources.append(ctx.controller._resource(303, "CS", ResourceType.Shader))
    runtime = NS(root=str(tmp_path), instance_id="test-references", state=state, replay=replay,
                 cache=SessionCache(), ctx=ctx, actions=actions, rd=rd, reads=[], moves=[], fatal=0,
                 bindings={5: {ShaderStage.Vertex: (302, "main"), ShaderStage.Pixel: (300, "ps")},
                           8: {ShaderStage.Compute: (303, "cs")}})
    def get_shader(stage):
        runtime.reads.append((ctx.controller.current, stage))
        return ResourceId(runtime.bindings.get(ctx.controller.current, {}).get(stage, (0, ""))[0])
    def entry(stage):
        return runtime.bindings.get(ctx.controller.current, {}).get(stage, (0, ""))[1]
    # Reflection and GetUsage are deliberately unavailable: neither is evidence of a binding scan.
    pipe = NS(GetShader=get_shader, GetShaderEntryPoint=entry,
              GetShaderReflection=lambda stage: pytest.fail("Binding lookup must not require reflection"))
    ctx.controller.GetPipelineState = lambda: pipe
    ctx.controller.GetUsage = lambda resource: pytest.fail("Shader references must not use GetUsage")
    original = ctx.controller.SetFrameEvent
    def set_event(event, force):
        runtime.moves.append((event, force))
        original(event, force)
    ctx.controller.SetFrameEvent = set_event
    ctx.controller.GetFatalErrorStatus = lambda: NS(code=runtime.fatal)
    return runtime


def call(runtime, **params):
    values = dict(resource_id="300")
    values.update(params)
    result = refs.find_shader_references(runtime, Context(runtime.state, values))
    assert result.result["artifacts"] == []
    assert len(json.dumps(result.result, ensure_ascii=True, allow_nan=False).encode()) < 256 * 1024
    return result


def error(runtime, code="INVALID_REQUEST", **params):
    with pytest.raises(AgentBridgeError) as caught:
        call(runtime, **params)
    assert caught.value.code == code


def test_exact_binding_without_reflection_and_ui_restore(reference_runtime):
    runtime = reference_runtime
    response = call(runtime)
    data = response.result["data"]
    assert data["resourceId"] == "300" and data["resourceName"] == "MainPS"
    assert data["stages"] == list(refs.STAGE_NAMES)
    assert data["references"] == [{"eventId": 5, "parentEventId": 1, "name": "DrawIndexed(36)",
        "flags": ["Drawcall", "Indexed"], "matches": [
            {"resourceId": "300", "stage": "pixel", "entryPoint": "ps", "entryPointKnown": True}]}]
    coverage = data["coverage"]
    assert coverage["scopeActionCount"] == 4 and coverage["candidateEventCount"] == 2
    assert coverage["processedCandidateCount"] == 2 and coverage["excludedActionCount"] == 2
    assert coverage["nativeReadsThisPage"] == 2 and coverage["examinedThisPage"] == 2
    assert coverage["scanComplete"] and coverage["complete"] and not response.result["truncated"]
    assert runtime.moves == [(5, False), (8, False), (9, True)]
    assert response.meta["replayRestoreForced"] is True
    assert runtime.state.snapshot()["selectedEventId"] == 1 and runtime.ctx.controller.current == 9
    assert all(stage != ShaderStage.Compute for event, stage in runtime.reads if event == 5)
    assert all(stage == ShaderStage.Compute for event, stage in runtime.reads if event == 8)


def test_empty_incremental_page_must_continue(reference_runtime):
    runtime = reference_runtime
    first = call(runtime, resource_id="303", scan_budget=1).result
    assert first["data"]["references"] == [] and first["nextCursor"]
    coverage = first["data"]["coverage"]
    assert coverage["processedCandidateCount"] == 1 and not coverage["scanComplete"]
    second = call(runtime, resource_id="303", scan_budget=100, limit=1,
                  cursor=first["nextCursor"]).result
    assert second["data"]["references"][0]["eventId"] == 8
    assert second["data"]["coverage"]["complete"] and second["nextCursor"] is None
    assert first["data"]["scanId"] == second["data"]["scanId"]
    assert "totalMatches" not in second["data"]


def test_stage_filter_and_cross_shader_cache_reuse(reference_runtime):
    runtime = reference_runtime
    first = call(runtime, stages=["pixel", "pixel"]).result
    assert first["data"]["stages"] == ["pixel"]
    assert first["data"]["coverage"]["candidateEventCount"] == 1
    assert first["data"]["coverage"]["stageExcludedActionCount"] == 1
    assert {stage for _, stage in runtime.reads} == set(ShaderStage(i) for i in range(5))
    moves, reads = list(runtime.moves), list(runtime.reads)
    runtime.state.current_event_changed(8, None)
    runtime.ctx.controller.current = 8
    second = call(runtime, resource_id="302", stages=["vertex"])
    assert second.result["data"]["references"][0]["eventId"] == 5
    assert second.result["data"]["scanId"] == first["data"]["scanId"]
    assert second.result["data"]["coverage"]["cacheHitsThisPage"] == 1
    assert second.meta == {} and runtime.moves == moves and runtime.reads == reads
    assert runtime.ctx.controller.current == 8


def test_shared_cache_hits_consume_scan_budget(reference_runtime):
    runtime = reference_runtime
    call(runtime)
    moves = list(runtime.moves)
    first = call(runtime, scan_budget=1).result
    assert first["data"]["coverage"]["cacheHitsThisPage"] == 1
    assert first["data"]["coverage"]["examinedThisPage"] == 1
    assert first["nextCursor"] and runtime.moves == moves


def test_refresh_invalidates_every_query_cursor_and_reobserves(reference_runtime):
    runtime = reference_runtime
    first = call(runtime, scan_budget=1).result
    other = call(runtime, resource_id="302", scan_budget=1).result
    runtime.bindings[5][ShaderStage.Pixel] = (302, "replacement")
    cached = call(runtime, stages=["pixel"]).result
    assert cached["data"]["references"]
    new = call(runtime, resource_id="302", refresh=True, scan_budget=1).result
    assert new["data"]["scanId"] != first["data"]["scanId"]
    assert {item["stage"] for item in new["data"]["references"][0]["matches"]} == {"vertex", "pixel"}
    error(runtime, cursor=first["nextCursor"])
    error(runtime, resource_id="302", cursor=other["nextCursor"])


def test_cursor_binds_resource_stages_scope_and_session(reference_runtime):
    runtime = reference_runtime
    first = call(runtime, scan_budget=1).result
    error(runtime, resource_id="302", cursor=first["nextCursor"])
    error(runtime, stages=["pixel"], cursor=first["nextCursor"])
    error(runtime, parent_event_id=1, cursor=first["nextCursor"])
    error(runtime, refresh=True, cursor=first["nextCursor"])
    runtime.state.capture_loaded("new.rdc", "D3D12", current=(9, None))
    moves = list(runtime.moves)
    error(runtime, cursor=first["nextCursor"])
    assert runtime.moves == moves


def test_scope_leaf_and_no_shader_operations_do_not_report_stale_bindings(reference_runtime):
    runtime = reference_runtime
    assert call(runtime, parent_event_id=5).result["data"]["references"][0]["eventId"] == 5
    before = len(runtime.moves)
    # Present still has the same residual fake shader map, but is not a shader-action candidate.
    runtime.bindings[9] = runtime.bindings[5]
    data = call(runtime, parent_event_id=9).result["data"]
    assert data["references"] == [] and data["coverage"]["complete"]
    assert data["coverage"]["excludedActionCount"] == 1 and len(runtime.moves) == before
    error(runtime, "INVALID_EVENT", parent_event_id=3)


def test_mesh_precedence_and_ray_unknown_coverage(reference_runtime):
    runtime = reference_runtime
    class Flags(IntFlag):
        Drawcall = int(ActionFlags.Drawcall)
        Dispatch = int(ActionFlags.Dispatch)
        MeshDispatch = 1 << 21
        DispatchRay = 1 << 22
    mesh = action(12, "Mesh", ActionFlags.Drawcall, 12)
    mesh.flags = Flags.MeshDispatch | Flags.Drawcall
    ray = action(13, "Ray", ActionFlags.Dispatch, 13)
    ray.flags = Flags.DispatchRay | Flags.Dispatch
    unknown = action(14, "Unknown", ActionFlags.NoFlags, 14)
    runtime.actions.extend([mesh, ray, unknown])
    runtime.bindings[12] = {ShaderStage.Mesh: (300, "meshMain"),
                            ShaderStage.Amplification: (300, "taskMain"),
                            ShaderStage.Pixel: (300, "fragmentMain"),
                            ShaderStage.Compute: (300, "staleCompute")}
    runtime.bindings[13] = {ShaderStage.Compute: (300, "notRay")}
    data = call(runtime).result["data"]
    found = {row["eventId"]: row for row in data["references"]}
    assert set(found) == {5, 12}
    assert [item["stage"] for item in found[12]["matches"]] == ["pixel", "amplification", "mesh"]
    assert not any(event in (13, 14) for event, _ in runtime.reads)
    assert data["coverage"]["unsupportedActionCount"] == 2
    assert data["coverage"]["unsupportedActionExamples"] == [13, 14]
    assert data["coverage"]["scanComplete"] and not data["coverage"]["complete"]


def test_missing_native_enum_is_not_unbound_and_empty_entrypoint_is_known_unknown(reference_runtime):
    runtime = reference_runtime
    runtime.rd.ShaderStage = NS(Vertex=ShaderStage.Vertex, Hull=ShaderStage.Hull, Domain=ShaderStage.Domain,
                               Geometry=ShaderStage.Geometry, Compute=ShaderStage.Compute)
    data = call(runtime, stages=["pixel"]).result["data"]
    assert data["references"] == []
    assert data["coverage"]["unsupportedStages"] == ["pixel"]
    assert not data["coverage"]["complete"]
    runtime.bindings[5][ShaderStage.Vertex] = (302, "")
    # Cache has the old VS entry; refresh is the explicit re-observation boundary.
    data = call(runtime, resource_id="302", stages=["vertex"], refresh=True).result["data"]
    match = data["references"][0]["matches"][0]
    assert match["entryPoint"] == "" and match["entryPointKnown"] is False
    assert data["coverage"]["complete"]


def test_one_module_multiple_stages_and_entrypoints_are_retained_in_shared_cache(reference_runtime):
    runtime = reference_runtime
    runtime.bindings[5] = {ShaderStage.Vertex: (300, "vs_entry"),
                           ShaderStage.Pixel: (300, "ps_entry")}
    pixel = call(runtime, stages=["pixel"]).result["data"]
    moves = list(runtime.moves)
    both = call(runtime, stages=["pixel", "vertex"]).result["data"]
    assert both["scanId"] == pixel["scanId"]
    assert [(m["stage"], m["entryPoint"]) for m in both["references"][0]["matches"]] == [
        ("vertex", "vs_entry"), ("pixel", "ps_entry")]
    assert both["coverage"]["nativeReadsThisPage"] == 0 and runtime.moves == moves


@pytest.mark.parametrize("conflict", ["Dispatch", "Clear", "Copy"])
def test_conflicting_mesh_metadata_is_unsupported(reference_runtime, conflict):
    runtime = reference_runtime
    class Flags(IntFlag):
        MeshDispatch = 1 << 21
        Dispatch = 1 << 22
        Clear = 1 << 23
        Copy = 1 << 24
    runtime.actions[0].children[0].flags = Flags.MeshDispatch | getattr(Flags, conflict)
    data = call(runtime, parent_event_id=5).result["data"]
    assert data["coverage"]["unsupportedActionCount"] == 1
    assert not data["coverage"]["complete"] and runtime.reads == []


@pytest.mark.parametrize("params", [
    {"resource_id": 300}, {"resource_id": True}, {"resource_id": "-1"},
    {"resource_id": str(1 << 64)}, {"resource_id": "1" * 21},
    {"stages": []}, {"stages": "pixel"}, {"stages": ["raygen"]}, {"stages": [True]},
    {"stages": ["pixel"] * 9}, {"parent_event_id": True}, {"parent_event_id": 1.0},
    {"parent_event_id": -1}, {"parent_event_id": 1 << 32},
    {"limit": None}, {"limit": True}, {"limit": 1.0}, {"limit": 0}, {"limit": 1001},
    {"scan_budget": None}, {"scan_budget": False}, {"scan_budget": 1.0},
    {"scan_budget": 0}, {"scan_budget": 2001}, {"cursor": ""}, {"cursor": 1},
    {"cursor": "x" * 1025}, {"refresh": 1}, {"refresh": None},
])
def test_strict_inputs_before_native_reads(reference_runtime, params):
    error(reference_runtime, **params)
    assert reference_runtime.reads == [] and reference_runtime.moves == []


@pytest.mark.parametrize("resource", ["0", "999999", "00300", "100", "200", "400"])
def test_wrong_or_unknown_shader_resource(reference_runtime, resource):
    error(reference_runtime, "INVALID_RESOURCE", resource_id=resource)
    assert reference_runtime.reads == [] and reference_runtime.moves == []


@pytest.mark.parametrize("case", ["shader", "entry", "cancel", "timeout", "session", "fatal", "restore"])
def test_page_failure_does_not_publish_or_replace_generation(reference_runtime, case):
    runtime = reference_runtime
    first = call(runtime, scan_budget=1).result
    context = Context(runtime.state, dict(resource_id="300", refresh=True))
    holder = runtime.cache.get(context.state["captureSessionId"], "shader-reference-bindings-v1", dict)
    original_cache = holder["cache"]
    original_pipe = runtime.ctx.controller.GetPipelineState()
    original_shader = original_pipe.GetShader
    def get_shader(stage):
        if case == "shader":
            raise RuntimeError("native binding failed")
        if case == "session":
            runtime.state.capture_loaded("other.rdc", "D3D12", current=(9, None))
        if case == "fatal":
            runtime.fatal = 1
        return original_shader(stage)
    original_pipe.GetShader = get_shader
    if case == "entry":
        original_pipe.GetShaderEntryPoint = lambda stage: (_ for _ in ()).throw(RuntimeError("entry failed"))
    if case in ("cancel", "timeout"):
        count = len(runtime.reads)
        def check():
            if len(runtime.reads) > count:
                raise AgentBridgeError("CANCELLED" if case == "cancel" else "TIMEOUT", "stop")
        context.check_cancelled = check
    if case == "restore":
        original_set = runtime.ctx.controller.SetFrameEvent
        def fail(event, force):
            original_set(event, force)
            if force:
                raise RuntimeError("restore failed")
        runtime.ctx.controller.SetFrameEvent = fail
    with pytest.raises(AgentBridgeError):
        refs.find_shader_references(runtime, context)
    assert holder["cache"] is original_cache
    assert holder["cache"]["scanId"] == first["data"]["scanId"]
    assert set(original_cache["events"]) == {5}
    assert runtime.moves[-1] == (9, True)


def test_scope_and_cache_budgets_are_explicit(reference_runtime, monkeypatch):
    runtime = reference_runtime
    monkeypatch.setattr(refs, "MAX_SCOPE_ACTIONS", 2)
    error(runtime, "LIMIT_EXCEEDED")
    assert runtime.moves == []
    monkeypatch.setattr(refs, "MAX_SCOPE_ACTIONS", 100000)
    monkeypatch.setattr(refs, "MAX_CACHE_BYTES", 1)
    error(runtime, "LIMIT_EXCEEDED")
    assert runtime.moves[-1] == (9, True)
    holder = runtime.cache.get(runtime.state.snapshot()["captureSessionId"], "shader-reference-bindings-v1", dict)
    assert holder["cache"]["events"] == {}


@pytest.mark.parametrize("at_roots", [False, True])
def test_wide_scope_rejected_before_copying_unbounded_sibling_list(monkeypatch, at_roots):
    class WideChildren:
        def __len__(self):
            return 100001
        def __reversed__(self):
            pytest.fail("Oversized sibling list must be rejected before it is copied")
    wide = WideChildren()
    actions = NS(roots=wide if at_roots else [1], nodes={
        1: {"children": [] if at_roots else wide, "flags": ["PushMarker"]}})
    with pytest.raises(AgentBridgeError) as caught:
        refs._scope(actions, None, ["pixel"], {"pixel": ShaderStage.Pixel}, lambda: None)
    assert caught.value.code == "LIMIT_EXCEEDED"


def test_mixed_cached_and_new_failure_preserves_original_cache(reference_runtime):
    runtime = reference_runtime
    first = call(runtime, scan_budget=1).result
    context = Context(runtime.state, {"resource_id": "300"})
    holder = runtime.cache.get(context.state["captureSessionId"], "shader-reference-bindings-v1", dict)
    cache = holder["cache"]
    before = json.dumps(cache, sort_keys=True)
    pipe = runtime.ctx.controller.GetPipelineState()
    original = pipe.GetShader
    def fail(stage):
        if runtime.ctx.controller.current == 8:
            raise RuntimeError("second candidate read failed")
        return original(stage)
    pipe.GetShader = fail
    error(runtime, "REPLAY_ERROR")
    assert holder["cache"] is cache and json.dumps(cache, sort_keys=True) == before
    pipe.GetShader = original
    continued = call(runtime, cursor=first["nextCursor"]).result
    assert continued["data"]["scanId"] == first["data"]["scanId"]
    assert continued["data"]["coverage"]["processedCandidateCount"] == 2


def test_name_and_entrypoint_bounded_valid_unicode(reference_runtime):
    runtime = reference_runtime
    runtime.bindings[5][ShaderStage.Pixel] = (300, "\u6e90\udcff" * 1000)
    runtime.actions[0].children[0].GetName = lambda structured: "n\udcff" * 1000
    response = call(runtime, stages=["pixel"])
    data = response.result["data"]
    row = data["references"][0]
    assert row["nameTruncated"] and row["nameEscapedInvalidUnicode"]
    assert row["matches"][0]["entryPointTruncated"]
    json.dumps(response.result, ensure_ascii=False).encode("utf-8")
    assert response.result["truncated"]


def test_page_byte_budget_keeps_unconsumed_event_cached(reference_runtime, monkeypatch):
    runtime = reference_runtime
    runtime.bindings[8][ShaderStage.Compute] = (300, "cs")
    first = call(runtime, limit=1).result
    row = first["data"]["references"][0]
    runtime.cache.clear()
    runtime.moves.clear()
    # Room for one match, not two. The second observation can be safely reused on the next page.
    monkeypatch.setattr(refs, "MAX_PAGE_BYTES", 8192 + refs._size(row) + 64)
    first = call(runtime, limit=100).result
    assert first["data"]["coverage"]["processedCandidateCount"] == 1
    assert first["data"]["coverage"]["examinedThisPage"] == 2
    assert first["data"]["coverage"]["nativeReadsThisPage"] == 2
    moves = list(runtime.moves)
    second = call(runtime, cursor=first["nextCursor"]).result
    assert second["data"]["references"][0]["eventId"] == 8
    assert second["data"]["coverage"]["cacheHitsThisPage"] == 1 and runtime.moves == moves


def test_native_restore_target_required_but_cached_only_needs_no_replay(reference_runtime):
    runtime = reference_runtime
    call(runtime)
    runtime.state.current_event_changed(None, None)
    moves = list(runtime.moves)
    assert call(runtime).result["data"]["coverage"]["nativeReadsThisPage"] == 0
    assert runtime.moves == moves
    error(runtime, "INVALID_EVENT", refresh=True)


def test_python36_compatible():
    ast.parse(Path(refs.__file__).read_text(encoding="utf-8"), feature_version=(3, 6))
