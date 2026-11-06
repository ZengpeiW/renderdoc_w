from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

import fake_renderdoc
from fake_renderdoc import FakeContext, ShaderStage, VarType, module, shader_value
from rendertest_agent_bridge.indexes import SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError
from rendertest_agent_bridge.lifecycle import CaptureState
from rendertest_agent_bridge.methods import cbuffer_members as members
from rendertest_agent_bridge.methods import pipeline
from rendertest_agent_bridge.paging import encode_cursor, query_key
from rendertest_agent_bridge.replay import ReplayRunner


SCOPE = ("a" * 32, 5, "pixel", "300", 0, "200", 64, 8192)


def variable(name, value=0, children=(), rows=1, columns=1, kind=VarType.UInt):
    return NS(name=name, type=VarType.Struct if children else kind,
              rows=0 if children else rows, columns=0 if children else columns,
              members=list(children), value=shader_value(u32v=[value], f32v=[value]))


def constant(name, offset=0, children=(), elements=1, stride=0, type_name="uint"):
    return NS(name=name, byteOffset=offset,
              type=NS(name=type_name, members=list(children), elements=elements,
                      arrayByteStride=stride))


def project(variables, constants=(), scope=SCOPE, check=None, **params):
    params.setdefault("member_path", "")
    return members.project_members(variables, NS(variables=list(constants)),
                                   members.validate_request(params), scope,
                                   pipeline.convert_variable, check_cancelled=check)


def error(code, callback):
    with pytest.raises(AgentBridgeError) as raised:
        callback()
    assert raised.value.code == code
    return raised.value


def test_legacy_detection_and_targeted_defaults():
    assert members.validate_request({}) is None
    assert members.validate_request(dict(member_path=None, cursor=None, limit=None,
                                        max_depth=None)) is None
    request = members.validate_request(dict(member_path=""))
    assert request == dict(path="", tokens=[], limit=200, depth=0, cursor=None)


@pytest.mark.parametrize("params", [
    {"limit": 1}, {"cursor": "x"}, {"max_depth": 0},
    {"member_path": False}, {"member_path": []}, {"member_path": "x" * 4097},
    {"member_path": "[0]" * 65}, {"member_path": ".a"}, {"member_path": "a."},
    {"member_path": "a..b"}, {"member_path": "a.[0]"}, {"member_path": "a[-1]"},
    {"member_path": "a[1.0]"}, {"member_path": "a[true]"}, {"member_path": "a[01]"},
    {"member_path": "a[0]b"}, {"member_path": "a[]"}, {"member_path": '["x"'},
    {"member_path": "", "limit": True}, {"member_path": "", "limit": 1.0},
    {"member_path": "", "limit": "1"}, {"member_path": "", "limit": 0},
    {"member_path": "", "limit": 1001}, {"member_path": "", "max_depth": True},
    {"member_path": "", "max_depth": 1.0}, {"member_path": "", "max_depth": -1},
    {"member_path": "", "max_depth": 5}, {"member_path": "", "cursor": False},
    {"member_path": "", "cursor": ""}, {"member_path": "", "cursor": "a" * 1025},
])
def test_invalid_request_rejected(params):
    error("INVALID_REQUEST", lambda: members.validate_request(params))


def test_large_array_drilldown_precedes_legacy_serialization_budget():
    lights = variable("Lights", children=[
        variable("Lights[%d]" % i, children=[variable("Color", value=i)]) for i in range(5000)
    ])
    constants = [constant("Lights", offset=16, children=[constant("Color", offset=4)],
                          elements=5000, stride=32, type_name="Light")]
    legacy = pipeline.convert_variable(lights, [pipeline.CBUFFER_VARIABLE_LIMIT],
                                       constants[0].type, 16)
    assert len(legacy["members"]) < 4999
    payload, cursor, truncated, warnings = project([lights], constants,
                                                  member_path="Lights[4999].Color")
    assert payload["canonicalPath"] == "[0][4999][0]"
    assert payload["selected"]["value"] == [4999]
    assert payload["selected"]["byteOffset"] == 16 + 4999 * 32 + 4
    assert payload["selected"]["typeName"] == "uint"
    assert payload["variables"] == [] and payload["childCount"] == 0
    assert payload["byteOffsetBasis"] == "block"
    assert cursor is None and truncated is False and warnings == []


@pytest.mark.parametrize("child_name", ["[%d]", "Lights[%d]", "renamed%d"])
def test_numeric_array_paths_ignore_native_child_name_conventions(child_name):
    lights = variable("Lights", children=[variable(child_name % i, value=i) for i in range(3)])
    constants = [constant("Lights", offset=4, elements=3, stride=16)]
    payload, _, _, _ = project([lights], constants, member_path="Lights[2]")
    assert payload["selected"]["value"] == [2]
    assert payload["selected"]["byteOffset"] == 36


def test_array_pages_cover_all_children_and_allow_page_size_change():
    values = [variable("Values", children=[variable("[%d]" % i, value=i) for i in range(5001)])]
    cursor, indices = None, []
    while True:
        payload, cursor, truncated, warnings = project(values, member_path="Values",
                                                      limit=137 if cursor is None else 1000,
                                                      cursor=cursor)
        indices.extend(item["value"][0] for item in payload["variables"])
        assert payload["childCount"] == 5001
        assert "members" not in payload["selected"]
        assert truncated == (cursor is not None)
        assert bool(warnings) == truncated
        if cursor is None:
            break
    assert indices == list(range(5001))


def test_direct_children_default_and_bounded_expansion():
    values = [variable("Outer", children=[
        variable("Inner", children=[variable("Value", value=42)]), variable("Other", value=7)])]
    shallow, _, truncated, _ = project(values)
    assert shallow["selected"] is None
    assert shallow["variables"][0]["childCount"] == 2
    assert shallow["variables"][0]["membersTruncated"] is True and truncated
    assert "members" not in shallow["variables"][0]
    deeper, _, truncated, _ = project(values, max_depth=1)
    assert deeper["variables"][0]["membersTruncated"] is False
    assert deeper["variables"][0]["members"][0]["membersTruncated"] is True and truncated
    complete, _, truncated, warnings = project(values, max_depth=2)
    assert complete["variables"][0]["members"][0]["members"][0]["value"] == [42]
    assert not truncated and not warnings
    selected, _, truncated, _ = project(values, member_path="Outer.Inner")
    assert selected["variables"][0]["value"] == [42] and not truncated


def test_quoted_names_case_sensitivity_duplicates_and_canonical_roundtrip():
    special = 'Light.Data[0]"\\\u5149'
    values = [variable(special, children=[variable("", value=9)]), variable("same", value=1),
              variable("same", value=2), variable("Case", value=3)]
    payload, _, _, _ = project(values, member_path="[" + json.dumps(special) + '][""]')
    assert payload["selected"]["value"] == [9]
    again, _, _, _ = project(values, member_path=payload["canonicalPath"])
    assert again["selected"] == payload["selected"]
    error("INVALID_REQUEST", lambda: project(values, member_path="same"))
    error("INVALID_REQUEST", lambda: project(values, member_path="case"))
    error("INVALID_REQUEST", lambda: project(values, member_path="[4]"))
    error("INVALID_REQUEST", lambda: project(values, member_path="Case[0]"))
    numeric, _, _, _ = project(values, member_path="[2]")
    assert numeric["selected"]["value"] == [2]


def test_length_one_and_nested_arrays_use_actual_tree_and_reflection_offsets():
    # Native reflection flattens length-one arrays. Preserve that topology instead of inventing [0].
    one = variable("One", children=[variable("Field", value=8)])
    constants = [constant("One", 20, children=[constant("Field", 4)], elements=1, stride=16)]
    payload, _, _, _ = project([one], constants, member_path="One.Field")
    assert payload["selected"]["byteOffset"] == 24
    outer = variable("Outer", children=[variable("Outer[%d]" % i, children=[
        variable("Values", children=[variable("[%d]" % j, value=i * 10 + j) for j in range(3)])
    ]) for i in range(2)])
    constants = [constant("Outer", 64, children=[constant("Values", 8, elements=3, stride=4)],
                          elements=2, stride=32)]
    payload, _, _, _ = project([outer], constants, member_path="Outer[1].Values[2]")
    assert payload["selected"]["value"] == [12]
    assert payload["selected"]["byteOffset"] == 64 + 32 + 8 + 8


def test_missing_or_ambiguous_reflection_omits_unreliable_layout():
    values = [variable("x", value=3)]
    for constants in ([], [constant("x", 4), constant("x", 8)]):
        payload, _, _, _ = project(values, constants, member_path="x")
        assert payload["selected"]["value"] == [3]
        assert "byteOffset" not in payload["selected"]
        assert "typeName" not in payload["selected"]


def test_matrix_leaf_preserves_existing_value_conversion():
    matrix = variable("Matrix", rows=2, columns=2, kind=VarType.Float)
    matrix.value = shader_value(f32v=[1.0, float("nan"), float("inf"), -float("inf")])
    payload, _, _, _ = project([matrix], member_path="Matrix")
    assert payload["selected"]["value"] == [1.0, "NaN", "Infinity", "-Infinity"]
    assert payload["selected"]["rows"] == payload["selected"]["columns"] == 2


@pytest.mark.parametrize("scope_index", range(len(SCOPE)))
def test_cursor_rejects_changed_capture_event_stage_shader_block_and_binding(scope_index):
    values = [variable("a"), variable("b")]
    _, cursor, _, _ = project(values, limit=1)
    scope = list(SCOPE)
    scope[scope_index] = str(scope[scope_index]) + "changed"
    error("INVALID_REQUEST", lambda: project(values, scope=tuple(scope), cursor=cursor))


def test_cursor_rejects_changed_path_depth_and_out_of_range():
    values = [variable("a", children=[variable("b"), variable("c")]), variable("d")]
    _, cursor, _, _ = project(values, limit=1)
    error("INVALID_REQUEST", lambda: project(values, member_path="a", cursor=cursor))
    error("INVALID_REQUEST", lambda: project(values, max_depth=1, cursor=cursor))
    forged = encode_cursor("get_cbuffer_members", SCOPE[0], query_key(SCOPE, "", 0), 3)
    error("INVALID_REQUEST", lambda: project(values, cursor=forged))


def test_node_budget_preserves_drilldown_and_next_sibling(monkeypatch):
    monkeypatch.setattr(members, "MAX_NODES", 8)
    values = [variable("Big", children=[variable("v%d" % i, value=i) for i in range(20)]),
              variable("Tail", value=99)]
    payload, cursor, truncated, warnings = project(values, max_depth=1)
    assert len(payload["variables"][0]["members"]) == 7
    assert payload["variables"][0]["membersTruncated"] and truncated and warnings
    assert cursor is not None
    tail, _, _, _ = project(values, cursor=cursor, max_depth=1)
    assert tail["variables"][0]["value"] == [99]
    child, _, _, _ = project(values, member_path="Big[19]")
    assert child["selected"]["value"] == [19]


def test_byte_budget_paginates_without_losing_large_names():
    values = [variable("n" * 2048 + str(i), value=i) for i in range(300)]
    indices, cursor = [], None
    while True:
        payload, cursor, truncated, _ = project(values, limit=1000, cursor=cursor)
        assert len(json.dumps(payload, ensure_ascii=True).encode("utf-8")) < members.MAX_PAYLOAD_BYTES
        indices.extend(item["value"][0] for item in payload["variables"])
        assert truncated == (cursor is not None)
        if cursor is None:
            break
    assert indices == list(range(300))
    error("LIMIT_EXCEEDED", lambda: project([variable("x" * members.MAX_PAYLOAD_BYTES)]))
    error("LIMIT_EXCEEDED", lambda: project([variable("x" * members.MAX_PAYLOAD_BYTES)],
                                            member_path="[0]"))


def test_cancellation_checked_while_resolving_and_projecting():
    calls = []

    def cancel():
        calls.append(1)
        if len(calls) == 4:
            raise AgentBridgeError("CANCELLED", "cancelled")

    values = [variable("v%d" % i) for i in range(1000)]
    error("CANCELLED", lambda: project(values, member_path="v999", check=cancel))
    calls.clear()
    error("CANCELLED", lambda: project(values, check=cancel))


class Context:
    method = "get_cbuffer"
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
    return NS(root=str(tmp_path), instance_id="test-members", state=state, replay=replay,
              cache=SessionCache(), ctx=ctx)


def native_call(runtime, **params):
    values = dict(stage="pixel", event_id=5, index=0)
    values.update(params)
    return pipeline.get_cbuffer(runtime, Context(runtime.state, values))


def test_native_callback_preserves_legacy_and_restores_event(runtime):
    legacy = native_call(runtime).result
    assert legacy == native_call(runtime, member_path=None, limit=None, max_depth=None).result
    assert "selected" not in legacy["data"] and "canonicalPath" not in legacy["data"]
    response = native_call(runtime, member_path="tint")
    data = response.result["data"]
    assert data["selected"]["value"] == [1.0, 0.5, 0.25, "NaN"]
    assert data["selected"]["byteOffset"] == 0 and data["binding"]["resourceId"] == "200"
    assert response.meta["replayEventRestored"] is True
    assert response.meta["eventIdBefore"] == response.meta["eventIdAfter"] == 9
    assert runtime.ctx.controller.current == 9
    error("INVALID_REQUEST", lambda: native_call(runtime, member_path="missing"))
    assert runtime.ctx.controller.current == 9
    assert runtime.ctx.controller.frame_events[-2:] == [5, 9]


def test_native_root_cursor_continues_and_bad_parameters_do_not_replay(runtime):
    first = native_call(runtime, member_path="", limit=1).result
    assert first["data"]["selected"] is None and first["data"]["childCount"] == 2
    second = native_call(runtime, member_path="", limit=1, cursor=first["nextCursor"]).result
    assert second["data"]["variables"][0]["name"] == "count"
    assert second["nextCursor"] is None
    events = list(runtime.ctx.controller.frame_events)
    error("INVALID_REQUEST", lambda: native_call(runtime, member_path="", limit=True))
    assert runtime.ctx.controller.frame_events == events


def test_native_large_tree_reads_member_after_4096_and_keeps_offsets_relative(runtime, monkeypatch):
    original = fake_renderdoc.reflection
    reflection = original(ShaderStage.Pixel)
    reflection.constantBlocks[0].variables = [constant("Values", 16, elements=5000, stride=4)]
    monkeypatch.setattr(fake_renderdoc, "reflection", lambda stage: reflection)
    variables = [variable("Values", children=[variable("[%d]" % i, value=i) for i in range(5000)])]
    monkeypatch.setattr(runtime.ctx.controller, "GetCBufferVariableContents", lambda *args: variables)
    original_block = fake_renderdoc.FakePipe.GetConstantBlock

    def get_block(self, stage, index, element):
        bound = original_block(self, stage, index, element)
        bound.descriptor.byteOffset = 64
        return bound

    monkeypatch.setattr(fake_renderdoc.FakePipe, "GetConstantBlock", get_block)
    payload = native_call(runtime, member_path="Values[4999]").result["data"]
    assert payload["selected"]["value"] == [4999]
    assert payload["selected"]["byteOffset"] == 16 + 4999 * 4
    assert payload["binding"]["byteOffset"] == 64
    assert runtime.ctx.controller.current == 9


def test_helper_retains_python36_syntax():
    ast.parse(Path(members.__file__).read_text(encoding="utf-8"), feature_version=(3, 6))
