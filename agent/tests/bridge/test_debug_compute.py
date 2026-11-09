from __future__ import annotations

import ast
from enum import IntFlag
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

import fake_renderdoc
from fake_renderdoc import FakeContext, ShaderStage, VarType, module, root_actions, shader_value
from rendertest_agent_bridge.indexes import ActionIndex, SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError
from rendertest_agent_bridge.lifecycle import CaptureState
from rendertest_agent_bridge.methods import debug, debug_compute
from rendertest_agent_bridge.replay import ReplayRunner


class Context:
    method = "debug_compute"
    deadline_at = float("inf")

    def __init__(self, state, params):
        self.state = state.snapshot()
        self.params = params

    def check_cancelled(self):
        pass


@pytest.fixture
def compute_runtime(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "renderdoc", module())
    state = CaptureState()
    state.capture_loaded("fixture.rdc", "D3D12", selected=(5, None), current=(9, None))
    ctx = FakeContext()
    replay = ReplayRunner(state, lambda message: None)
    replay.manager = ctx.manager
    actions = root_actions()
    ctx.controller.GetRootActions = lambda: actions
    reflection = fake_renderdoc.reflection(ShaderStage.Compute)
    original = fake_renderdoc.reflection
    monkeypatch.setattr(fake_renderdoc, "reflection",
                        lambda stage: reflection if stage == ShaderStage.Compute else original(stage))
    runtime = NS(root=str(tmp_path), instance_id="test-compute", state=state, replay=replay,
                 cache=SessionCache(), ctx=ctx, actions=actions,
                 dispatch=actions[0].children[1], reflection=reflection, traces=[])

    def debug_thread(group, thread):
        controller = ctx.controller
        controller.debug_calls.append((controller.current, list(group), list(thread)))
        trace = controller._debug_trace("unused")
        if trace.debugger is not None:
            override = runtime.dispatch.dispatchThreadsDimension
            dimensions = override if override[0] else reflection.dispatchThreadsDimension
            global_thread = [group[i] * dimensions[i] + thread[i] for i in range(3)]
            trace.inputs = [NS(name=name, type=VarType.UInt, rows=1, columns=3, members=[],
                               value=shader_value(u32v=values))
                            for name, values in [("SV_GroupID", group),
                                                 ("SV_GroupThreadID", thread),
                                                 ("SV_DispatchThreadID", global_thread)]]
            trace.sourceVars = []
        runtime.traces.append(trace)
        return trace

    ctx.controller.DebugThread = debug_thread
    return runtime


def call(runtime, **params):
    values = dict(event_id=8, group=[1, 2, 0], thread=[3, 4, 0])
    values.update(params)
    return debug_compute.debug_compute(runtime, Context(runtime.state, values))


def assert_restored(runtime):
    snapshot = runtime.state.snapshot()
    assert snapshot["selectedEventId"] == 5
    assert snapshot["currentEventId"] == 9
    assert runtime.ctx.controller.current == 9


def error(runtime, code="INVALID_REQUEST", **params):
    with pytest.raises(AgentBridgeError) as caught:
        call(runtime, **params)
    assert caught.value.code == code
    assert_restored(runtime)


def artifact_records(response):
    artifact = response.result["artifacts"][0]
    path = Path(artifact["path"])
    contents = path.read_bytes()
    assert path.suffix == ".jsonl"
    assert artifact["kind"] == "shader-debug-trace"
    assert artifact["mediaType"] == "application/x-ndjson"
    assert artifact["bytes"] == len(contents)
    assert artifact["sha256"] == hashlib.sha256(contents).hexdigest()
    assert not path.with_suffix(path.suffix + ".part").exists()
    return artifact, [json.loads(line) for line in contents.decode("utf-8").splitlines()]


def test_nonzero_compute_invocation_trace_and_ui_restoration(compute_runtime):
    runtime = compute_runtime
    response = call(runtime)
    data = response.result["data"]
    assert data["eventId"] == 8 and data["stage"] == "compute"
    assert data["group"] == [1, 2, 0] and data["thread"] == [3, 4, 0]
    assert data["globalThread"] == [11, 20, 0]
    assert data["dispatchDimensions"] == [8, 8, 1]
    assert data["threadGroupSize"] == [8, 8, 1]
    assert data["threadGroupSizeSource"] == "shader_reflection"
    assert data["shader"]["resourceId"] == "303"
    assert data["debugged"] and data["executionComplete"] and data["traceComplete"]
    assert data["steps"] == 3 and data["outputs"] == []
    assert data["lastChanges"] and data["nanOrInfSteps"] and data["sampleSteps"] == 1
    inputs = {item["name"]: item["value"] for item in data["inputs"]}
    assert inputs["SV_GroupID"] == [1, 2, 0]
    assert inputs["SV_GroupThreadID"] == [3, 4, 0]
    assert inputs["SV_DispatchThreadID"] == data["globalThread"]
    assert any("not a complete record of UAV writes" in warning
               for warning in response.result["warnings"])
    artifact, records = artifact_records(response)
    expected_invocation = {key: data[key] for key in (
        "eventId", "stage", "group", "thread", "globalThread", "dispatchDimensions",
        "threadGroupSize", "threadGroupSizeSource")}
    assert records[0]["invocation"] == expected_invocation
    assert records[0]["schema"] == "rendertest-shader-debug-trace/v1"
    assert records[-1]["record"] == "summary" and records[-1]["steps"] == 3
    expected_source = dict(expected_invocation)
    expected_source["event_id"] = expected_source.pop("eventId")
    assert artifact["source"] == expected_source
    assert runtime.ctx.controller.debug_calls == [(8, [1, 2, 0], [3, 4, 0])]
    assert runtime.ctx.controller.freed == runtime.traces
    assert runtime.ctx.controller.frame_events == [8, 9]
    assert response.meta["replayEventRestored"] is True
    assert_restored(runtime)


def test_current_event_default_and_action_dimension_override(compute_runtime):
    runtime = compute_runtime
    runtime.state.current_event_changed(8, None)
    runtime.ctx.controller.current = 8
    runtime.dispatch.dispatchThreadsDimension = (4, 2, 2)
    response = call(runtime, event_id=None, group=[2, 3, 0], thread=[3, 1, 1])
    data = response.result["data"]
    assert data["globalThread"] == [11, 7, 1]
    assert data["threadGroupSize"] == [4, 2, 2]
    assert data["threadGroupSizeSource"] == "action_override"
    assert runtime.ctx.controller.current == 8
    assert runtime.state.snapshot()["selectedEventId"] == 5


def test_resolved_indirect_leaf_dispatch_supported(compute_runtime):
    runtime = compute_runtime
    class IndirectFlags(IntFlag):
        Dispatch = int(fake_renderdoc.ActionFlags.Dispatch)
        Indirect = 0x80000
    runtime.dispatch.flags = IndirectFlags.Dispatch | IndirectFlags.Indirect
    parent = runtime.actions[0]
    parent.customName = "ExecuteIndirect"
    response = call(runtime)
    assert response.result["data"]["debugged"]
    assert runtime.ctx.controller.freed == runtime.traces


@pytest.mark.parametrize("event_id", [0, 1, 5, 6, 7, 9, 999])
def test_non_dispatch_and_non_action_api_eids_rejected(compute_runtime, event_id):
    error(compute_runtime, "INVALID_EVENT", event_id=event_id)
    assert compute_runtime.ctx.controller.debug_calls == []
    assert compute_runtime.ctx.controller.frame_events == []


@pytest.mark.parametrize("flags", ["DispatchRay", "MeshDispatch", "Drawcall", "MultiAction",
                                   "PushMarker", "CmdList"])
def test_ray_mesh_and_container_rejected_even_with_dispatch_bit(compute_runtime, flags):
    runtime = compute_runtime
    index = ActionIndex.build(runtime.ctx.controller)
    index.nodes[8]["flags"] = ["Dispatch", flags]
    runtime.cache.get(runtime.state.snapshot()["captureSessionId"], "actions", lambda: index)
    error(runtime, "INVALID_EVENT")
    assert runtime.ctx.controller.debug_calls == []


def test_container_with_children_and_zero_dimension_rejected(compute_runtime):
    runtime = compute_runtime
    runtime.dispatch.children = [runtime.actions[1]]
    error(runtime, "INVALID_EVENT")
    runtime.cache.clear()
    runtime.dispatch.children = []
    runtime.dispatch.dispatchDimension = (8, 0, 1)
    error(runtime, "INVALID_EVENT")
    assert runtime.ctx.controller.debug_calls == []


def test_nonzero_dispatch_base_preserved_and_rejected(compute_runtime):
    runtime = compute_runtime
    runtime.dispatch.dispatchBase = (1, 0, 0)
    index = ActionIndex.build(runtime.ctx.controller)
    assert index.nodes[8]["dispatchBase"] == [1, 0, 0]
    assert index.nodes[5]["dispatchBase"] == [0, 0, 0]
    error(runtime, "UNSUPPORTED")
    assert runtime.ctx.controller.debug_calls == []


@pytest.mark.parametrize("params", [
    {"group": None}, {"group": [0, 0]}, {"group": [0, 0, 0, 0]}, {"group": (0, 0, 0)},
    {"group": "[0,0,0]"}, {"group": [False, 0, 0]}, {"group": [0.0, 0, 0]},
    {"group": [-1, 0, 0]}, {"group": [1 << 32, 0, 0]},
    {"thread": None}, {"thread": [0, 0]}, {"thread": [0, 0, 0, 0]},
    {"thread": [0, True, 0]}, {"thread": [0, 0, 0.0]}, {"thread": [0, -1, 0]},
    {"thread": [0, 0, 1 << 32]}, {"event_id": True}, {"event_id": 8.0},
    {"event_id": -1}, {"event_id": 1 << 32}, {"max_steps": None},
    {"max_steps": False}, {"max_steps": 2.0}, {"max_steps": 0}, {"max_steps": 200001},
])
def test_strict_inputs_rejected_before_replay(compute_runtime, params):
    def forbidden():
        pytest.fail("Bad parameters must not reach native replay")
    compute_runtime.ctx.controller.GetRootActions = forbidden
    error(compute_runtime, **params)


@pytest.mark.parametrize("missing", ["group", "thread"])
def test_coordinates_required(compute_runtime, missing):
    params = {"event_id": 8, "group": [0, 0, 0], "thread": [0, 0, 0]}
    params.pop(missing)
    with pytest.raises(AgentBridgeError) as caught:
        debug_compute.debug_compute(compute_runtime, Context(compute_runtime.state, params))
    assert caught.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("params", [
    {"group": [8, 0, 0]}, {"group": [0, 8, 0]}, {"group": [0, 0, 1]},
    {"thread": [8, 0, 0]}, {"thread": [0, 8, 0]}, {"thread": [0, 0, 1]},
    {"group": [(1 << 32) - 1, 0, 0]}, {"thread": [(1 << 32) - 1, 0, 0]},
])
def test_coordinates_bounded_by_actual_dispatch(compute_runtime, params):
    error(compute_runtime, **params)
    assert compute_runtime.ctx.controller.debug_calls == []


@pytest.mark.parametrize("override,reflection", [
    ((0, 0, 0), (0, 0, 0)), ((0, 0, 0), None), ((4, 0, 1), (8, 8, 1)),
    ((0, 0, 0), (8, 8)), ((0, 0, 0), (8, -1, 1)),
])
def test_unknown_thread_dimensions_not_invented(compute_runtime, override, reflection):
    runtime = compute_runtime
    runtime.dispatch.dispatchThreadsDimension = override
    runtime.reflection.dispatchThreadsDimension = reflection
    error(runtime, "UNSUPPORTED")
    assert runtime.ctx.controller.debug_calls == []


def test_global_builtin_overflow_rejected(compute_runtime):
    runtime = compute_runtime
    runtime.dispatch.dispatchDimension = ((1 << 32) - 1, 1, 1)
    runtime.reflection.dispatchThreadsDimension = (1024, 1, 1)
    error(runtime, group=[(1 << 32) - 2, 0, 0], thread=[0, 0, 0])
    assert runtime.ctx.controller.debug_calls == []


@pytest.mark.parametrize("case", ["api_false", "api_unknown", "shader_false", "shader_unknown",
                                  "native_missing"])
def test_debug_capability_and_reflection_checked(compute_runtime, case):
    runtime = compute_runtime
    if case == "api_false":
        runtime.ctx.controller.GetAPIProperties = lambda: NS(shaderDebugging=False)
    elif case == "api_unknown":
        runtime.ctx.controller.GetAPIProperties = lambda: NS()
    elif case == "shader_false":
        runtime.reflection.debugInfo.debuggable = False
        runtime.reflection.debugInfo.debugStatus = "unsupported opcode"
    elif case == "shader_unknown":
        del runtime.reflection.debugInfo.debuggable
    else:
        runtime.ctx.controller.DebugThread = None
    error(runtime, "UNSUPPORTED")
    assert runtime.ctx.controller.debug_calls == []


@pytest.mark.parametrize("max_steps,steps,complete", [(1, 1, False), (2, 2, False),
                                                     (200000, 3, True)])
def test_step_budget_preserved_in_summary_and_artifact(compute_runtime, max_steps, steps, complete):
    response = call(compute_runtime, max_steps=max_steps)
    data = response.result["data"]
    assert data["steps"] == steps and data["executionComplete"] is complete
    artifact, records = artifact_records(response)
    assert artifact["executionComplete"] is complete
    assert len([r for r in records if r["record"] == "step"]) == steps
    assert records[-1]["executionComplete"] is complete
    assert compute_runtime.ctx.controller.freed == compute_runtime.traces
    assert_restored(compute_runtime)


@pytest.mark.parametrize("none_trace", [False, True])
def test_empty_native_trace_is_honest_and_freed(compute_runtime, none_trace):
    runtime = compute_runtime
    if none_trace:
        runtime.ctx.controller.DebugThread = lambda group, thread: None
    else:
        runtime.ctx.controller.debug_misses = True
    response = call(runtime)
    assert response.result["data"]["debugged"] is False
    assert response.result["artifacts"] == []
    assert "traceArtifact" not in response.result["data"]
    assert any("could not produce" in warning for warning in response.result["warnings"])
    assert not list(Path(runtime.root).rglob("*.part"))
    assert not list(Path(runtime.root).rglob("*.jsonl"))
    assert runtime.ctx.controller.freed == runtime.traces
    assert_restored(runtime)


@pytest.mark.parametrize("kind", ["CANCELLED", "TIMEOUT", "REPLAY_ERROR"])
def test_trace_failure_frees_trace_and_removes_partial(compute_runtime, kind):
    runtime = compute_runtime
    def fail(debugger):
        raise AgentBridgeError(kind, "test failure")
    runtime.ctx.controller.ContinueDebug = fail
    error(runtime, kind)
    assert runtime.ctx.controller.freed == runtime.traces
    assert len(runtime.traces) == 1
    assert not list(Path(runtime.root).rglob("*.part"))
    assert not list(Path(runtime.root).rglob("*.jsonl"))


def test_cancel_after_native_trace_creation_frees_trace(compute_runtime):
    runtime = compute_runtime
    params = dict(event_id=8, group=[0, 0, 0], thread=[0, 0, 0])
    context = Context(runtime.state, params)
    def cancelled():
        if runtime.traces:
            raise AgentBridgeError("CANCELLED", "cancel after native return")
    context.check_cancelled = cancelled
    def no_continuation(debugger):
        pytest.fail("A cancelled native trace must not start another native continuation")
    runtime.ctx.controller.ContinueDebug = no_continuation
    with pytest.raises(AgentBridgeError) as caught:
        debug_compute.debug_compute(runtime, context)
    assert caught.value.code == "CANCELLED"
    assert runtime.ctx.controller.freed == runtime.traces
    assert not list(Path(runtime.root).rglob("*.part"))
    assert_restored(runtime)


def test_cancel_while_serializing_batch_does_not_continue_again(compute_runtime, monkeypatch):
    runtime = compute_runtime
    context = Context(runtime.state, dict(event_id=8, group=[0, 0, 0], thread=[0, 0, 0]))
    cancelled = [False]
    continuations = []
    original_continue = runtime.ctx.controller.ContinueDebug
    original_write = debug._TraceWriter.write
    def check_cancelled():
        if cancelled[0]:
            raise AgentBridgeError("CANCELLED", "cancel during batch serialization")
    def continue_debug(debugger):
        continuations.append(True)
        assert len(continuations) == 1, "Cancellation must prevent the next native continuation"
        return original_continue(debugger)
    def write(self, record, reserve=True):
        value = original_write(self, record, reserve=reserve)
        if record.get("record") == "step":
            cancelled[0] = True
        return value
    context.check_cancelled = check_cancelled
    runtime.ctx.controller.ContinueDebug = continue_debug
    monkeypatch.setattr(debug._TraceWriter, "write", write)
    with pytest.raises(AgentBridgeError) as caught:
        debug_compute.debug_compute(runtime, context)
    assert caught.value.code == "CANCELLED"
    assert len(continuations) == 1
    assert runtime.ctx.controller.freed == runtime.traces
    assert not list(Path(runtime.root).rglob("*.part"))
    assert not list(Path(runtime.root).rglob("*.jsonl"))
    assert_restored(runtime)


def test_native_failure_before_return_restores_event(compute_runtime):
    runtime = compute_runtime
    def fail(group, thread):
        raise RuntimeError("native DebugThread failed")
    runtime.ctx.controller.DebugThread = fail
    with pytest.raises(AgentBridgeError):
        call(runtime)
    assert runtime.ctx.controller.freed == []
    assert_restored(runtime)


@pytest.mark.parametrize("record_kind", ["header", "step", "summary"])
def test_trace_writer_failure_frees_and_cleans(compute_runtime, monkeypatch, record_kind):
    runtime = compute_runtime
    original = debug._TraceWriter.write
    def fail(self, record, reserve=True):
        if record.get("record") == record_kind:
            raise OSError("simulated trace write failure")
        return original(self, record, reserve=reserve)
    monkeypatch.setattr(debug._TraceWriter, "write", fail)
    with pytest.raises(AgentBridgeError):
        call(runtime)
    assert runtime.ctx.controller.freed == runtime.traces and len(runtime.traces) == 1
    assert not list(Path(runtime.root).rglob("*.part"))
    assert not list(Path(runtime.root).rglob("*.jsonl"))
    assert_restored(runtime)


def test_python36_compatible():
    ast.parse(Path(debug_compute.__file__).read_text(encoding="utf-8"), feature_version=(3, 6))
