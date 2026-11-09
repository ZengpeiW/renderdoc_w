from __future__ import annotations

import ast
import json
from enum import IntEnum, IntFlag
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

from fake_renderdoc import FakeContext, ResourceId, module, root_actions
from rendertest_agent_bridge.indexes import ActionIndex, SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError
from rendertest_agent_bridge.lifecycle import CaptureState
from rendertest_agent_bridge.methods import api_calls
from rendertest_agent_bridge.paging import encode_cursor, query_key
from rendertest_agent_bridge.replay import ReplayRunner


class Basic(IntEnum):
    Chunk = 0
    Struct = 1
    Array = 2
    Null = 3
    Buffer = 4
    String = 5
    Enum = 6
    UnsignedInteger = 7
    SignedInteger = 8
    Float = 9
    Boolean = 10
    Character = 11
    Resource = 12
    GPUAddress = 13


class Flags(IntFlag):
    NoFlags = 0
    HasCustomString = 1
    Hidden = 2
    Nullable = 4
    NullString = 8
    Important = 64


class SDObject:
    """Expose the Python API, deliberately without C++-only data.str/AsUInt64 helpers."""
    def __init__(self, name, kind=Basic.UnsignedInteger, value=0, children=(), flags=0,
                 type_name=None, text="", byte_size=4, count=None, factory=None):
        self.name = name
        self.type = NS(name=type_name or kind.name, basetype=kind, flags=Flags(flags),
                       byteSize=byte_size)
        self.data = NS(basic=NS(u=value, i=value, d=value, b=value, c=value, id=value))
        self.text = value if kind == Basic.String else text
        self.children = children
        self.count = len(children) if count is None else count
        self.factory = factory
        self.reads = []

    def NumChildren(self):
        return self.count

    def GetChild(self, index):
        assert 0 <= index < self.count
        self.reads.append(index)
        return self.factory(index) if self.factory else self.children[index]

    def AsString(self):
        return self.text

    def AsBool(self):
        return bool(self.data.basic.b)

    def AsResourceId(self):
        return ResourceId(self.data.basic.id)


class Context:
    method = "get_api_calls"
    deadline_at = float("inf")

    def __init__(self, state, params):
        self.state = state.snapshot()
        self.params = params
        self.checks = 0

    def check_cancelled(self):
        self.checks += 1


@pytest.fixture
def api_runtime(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "renderdoc", module())
    state = CaptureState()
    state.capture_loaded("fixture.rdc", "D3D12", current=(9, None))
    ctx = FakeContext()
    replay = ReplayRunner(state, lambda message: None)
    replay.manager = ctx.manager
    actions = root_actions()
    chunks = [SDObject("Call%d" % i, Basic.Chunk,
                       children=[SDObject("argument", value=i)]) for i in range(9)]
    stack = list(actions)
    while stack:
        action = stack.pop()
        for event in action.events:
            event.chunkIndex = event.eventId - 1
        stack.extend(action.children)
    structured = NS(chunks=chunks)
    ctx.controller.GetRootActions = lambda: actions
    ctx.controller.GetStructuredFile = lambda: structured
    return NS(root=str(tmp_path), instance_id="test-api", state=state, replay=replay,
              cache=SessionCache(), ctx=ctx, actions=actions, structured=structured)


def call(runtime, **params):
    response = api_calls.get_api_calls(runtime, Context(runtime.state, params))
    assert runtime.ctx.controller.current == 9
    assert runtime.ctx.controller.frame_events == []
    assert response.result["artifacts"] == []
    assert len(json.dumps(response.result, ensure_ascii=True, allow_nan=False).encode()) < 256 * 1024
    return response.result


def error(runtime, code="INVALID_REQUEST", **params):
    with pytest.raises(AgentBridgeError) as caught:
        call(runtime, **params)
    assert caught.value.code == code
    assert runtime.ctx.controller.frame_events == []


def test_original_api_eids_and_action_list_paging(api_runtime):
    first = call(api_runtime, event_id=3, limit=2)
    assert first["data"]["eventId"] == 3
    assert first["data"]["actionEventId"] == 5
    assert first["data"]["total"] == 4
    assert [row["apiEventId"] for row in first["data"]["calls"]] == [2, 3]
    assert [row["chunkIndex"] for row in first["data"]["calls"]] == [1, 2]
    second = call(api_runtime, event_id=3, limit=100, cursor=first["nextCursor"])
    assert [row["apiEventId"] for row in second["data"]["calls"]] == [4, 5]
    assert second["nextCursor"] is None
    assert call(api_runtime)["data"]["actionEventId"] == 9
    assert [row["apiEventId"] for row in call(api_runtime, event_id=1)["data"]["calls"]] == [1]
    detail = call(api_runtime, api_event_id=3)["data"]
    assert detail["apiEventId"] == 3 and detail["actionEventId"] == 5
    assert detail["name"] == "Call2" and detail["parameters"][0]["value"] == 2
    assert call(api_runtime, event_id=2, api_event_id=3)["data"] == detail
    error(api_runtime, event_id=8, api_event_id=3)
    error(api_runtime, "INVALID_EVENT", api_event_id=123456)
    error(api_runtime, "INVALID_EVENT", event_id=123456)


@pytest.mark.parametrize("index", [None, (1 << 32) - 1, 999, -1])
def test_missing_chunks_explicit_and_no_owner_substitution(api_runtime, index):
    event = api_runtime.actions[0].children[0].events[1]
    event.chunkIndex = index
    listed = call(api_runtime, event_id=5)["data"]["calls"][1]
    assert listed["apiEventId"] == 3 and listed["available"] is False
    detail = call(api_runtime, api_event_id=3)["data"]
    assert detail["available"] is False and detail["parameters"] == []
    assert detail["selected"] is None and detail["reason"]
    error(api_runtime, api_event_id=3, parameter_path="[0]")


def test_synthetic_action_is_not_a_recorded_api_event(api_runtime):
    api_runtime.actions[0].events = []
    assert call(api_runtime, event_id=1)["data"]["calls"] == []
    error(api_runtime, "INVALID_EVENT", api_event_id=1)


def test_types_exact_values_and_hidden_ordinal_paths(api_runtime):
    children = [SDObject("same", value=17, flags=Flags.Hidden),
                SDObject("same", Basic.SignedInteger, -(1 << 63)),
                SDObject("wide", value=(1 << 64) - 1),
                SDObject("resource", Basic.Resource, 987),
                SDObject("gpu", Basic.GPUAddress, 0xABC),
                SDObject("enum", Basic.Enum, 7, flags=Flags.HasCustomString, text="CopyDest"),
                SDObject("string", Basic.String, "\ufeff\u6e90\n"),
                SDObject("bool", Basic.Boolean, True),
                SDObject("nan", Basic.Float, float("nan")),
                SDObject("inf", Basic.Float, float("inf")),
                SDObject("char", Basic.Character, "x"),
                SDObject("null", Basic.Null),
                SDObject("nullstring", Basic.String, "ignored", flags=Flags.NullString),
                SDObject("empty", Basic.Array),
                SDObject("opaque", Basic.Buffer, 999, byte_size=4096)]
    api_runtime.structured.chunks[2] = SDObject("Copy", Basic.Chunk, children=children)
    data = call(api_runtime, api_event_id=3)["data"]
    values = data["parameters"]
    assert values[0]["value"] == 17 and values[0]["flags"] == ["Hidden"]
    assert values[1]["value"] == str(-(1 << 63)) and values[1]["valueEncoding"] == "decimal"
    assert values[2]["value"] == str((1 << 64) - 1)
    assert values[3]["value"] == "987" and values[3]["valueEncoding"] == "resourceId"
    assert values[4]["value"] == "0xabc" and values[4]["valueEncoding"] == "hex"
    assert values[5]["value"] == 7 and values[5]["displayValue"] == "CopyDest"
    assert values[6]["value"] == "\ufeff\u6e90\n"
    assert values[7]["value"] is True
    assert [values[i]["value"] for i in (8, 9, 10, 11, 12)] == ["NaN", "Infinity", "x", None, None]
    assert values[13]["type"] == "Array" and "value" not in values[13]
    assert values[14]["valueAvailable"] is False and "value" not in values[14]
    leaf = call(api_runtime, api_event_id=3, parameter_path="[1]")["data"]
    assert leaf["selected"]["value"] == str(-(1 << 63)) and leaf["parameters"] == []
    assert [v["canonicalPath"] for v in values] == ["[%d]" % i for i in range(len(values))]


def test_large_lazy_array_random_access_and_paging(api_runtime):
    array = SDObject("barriers", Basic.Array, count=1000000,
                     factory=lambda i: SDObject("$el", Basic.Struct, children=[
                         SDObject("before", value=i), SDObject("after", value=i + 1)]))
    api_runtime.structured.chunks[2] = SDObject("ResourceBarrier", Basic.Chunk, children=[array])
    root = call(api_runtime, api_event_id=3)
    assert root["data"]["parameters"][0]["childCount"] == 1000000
    assert array.reads == [] and root["truncated"] is True
    leaf = call(api_runtime, api_event_id=3, parameter_path="[0][987654][1]")
    assert leaf["data"]["selected"]["value"] == 987655 and array.reads == [987654]
    first = call(api_runtime, api_event_id=3, parameter_path="[0]", limit=2, max_depth=1)
    assert [v["members"][0]["value"] for v in first["data"]["parameters"]] == [0, 1]
    second = call(api_runtime, api_event_id=3, parameter_path="[0]", limit=3, max_depth=1,
                  cursor=first["nextCursor"])
    assert [v["members"][0]["value"] for v in second["data"]["parameters"]] == [2, 3, 4]
    assert len(array.reads) == 6


def test_cursor_binds_full_session_mode_api_path_depth(api_runtime):
    first = call(api_runtime, event_id=5, limit=1)
    cursor = first["nextCursor"]
    error(api_runtime, event_id=8, cursor=cursor)
    error(api_runtime, api_event_id=3, cursor=cursor)
    error(api_runtime, event_id=5, max_depth=1, cursor=cursor)
    original = api_runtime.state.snapshot()["captureSessionId"]
    api_runtime.state.capture_loaded("fixture.rdc", "D3D12", current=(9, None))
    error(api_runtime, event_id=5, cursor=cursor)
    # Same 16-character session prefix must not allow reuse: query key includes the full ID.
    request = api_calls._validate({"event_id": 5, "limit": 1})
    records = ActionIndex.build(api_runtime.ctx.controller).action_api_events[5]
    prefix_session = original[:16] + "different-session-tail"
    with pytest.raises(AgentBridgeError):
        api_calls._list_calls(api_runtime.structured, records, 5, 5,
                             dict(request, cursor=cursor), prefix_session, lambda: None)
    api_runtime.structured.chunks[2] = SDObject("Call", Basic.Chunk, children=[
        SDObject("a", Basic.Array, children=[SDObject("x"), SDObject("y")]), SDObject("b")])
    page = call(api_runtime, api_event_id=3, limit=1)
    error(api_runtime, api_event_id=2, cursor=page["nextCursor"])
    error(api_runtime, api_event_id=3, parameter_path="[0]", cursor=page["nextCursor"])
    error(api_runtime, api_event_id=3, max_depth=1, cursor=page["nextCursor"])


@pytest.mark.parametrize("params", [
    {"event_id": True}, {"api_event_id": 3.0}, {"event_id": -1}, {"api_event_id": 1 << 32},
    {"limit": True}, {"limit": 0}, {"limit": 1001}, {"limit": 2.0}, {"limit": None},
    {"max_depth": False}, {"max_depth": -1}, {"max_depth": 5}, {"max_depth": 1.0},
    {"parameter_path": ""}, {"cursor": ""}, {"cursor": 17}, {"cursor": "x" * 1025},
    {"api_event_id": 3, "parameter_path": False},
    *({"api_event_id": 3, "parameter_path": p} for p in
      ("name", "[-1]", "[01]", '["name"]', "[0].x", "[0]junk", "[4294967296]",
       "[" + "9" * 4000 + "]", "[0]" * 65, "x" * 4097)),
])
def test_invalid_requests_rejected_before_native_access(api_runtime, params):
    def forbidden():
        pytest.fail("Invalid request reached the native controller")
    api_runtime.ctx.controller.GetRootActions = forbidden
    error(api_runtime, **params)


def test_bad_paths_and_forged_out_of_range_offset(api_runtime):
    error(api_runtime, api_event_id=3, parameter_path="[1]")
    error(api_runtime, api_event_id=3, parameter_path="[0][0]")
    session = api_runtime.state.snapshot()["captureSessionId"]
    key = query_key(session, "parameters", 5, 3, 2, "", 0)
    error(api_runtime, api_event_id=3,
          cursor=encode_cursor("get_api_calls", session, key, 99999))


def test_invalid_native_utf8_is_explicitly_escaped_for_ipc(api_runtime):
    api_runtime.structured.chunks[2] = SDObject("Call\udcff", Basic.Chunk, children=[
        SDObject("char", Basic.Character, "\udc80"),
        SDObject("text", Basic.String, "valid\u6e90\udcfe")])
    page = call(api_runtime, api_event_id=3)
    json.dumps(page, ensure_ascii=False).encode("utf-8")
    assert page["data"]["name"] == "Call\\udcff"
    assert page["data"]["nameEscapedInvalidUnicode"] is True
    assert page["data"]["parameters"][0]["value"] == "\\udc80"
    assert page["data"]["parameters"][1]["value"] == "valid\u6e90\\udcfe"
    assert page["data"]["parameters"][1]["valueEscapedInvalidUnicode"] is True


def test_bounded_text_response_and_page_progress(api_runtime):
    chunks = api_runtime.structured.chunks
    chunks[2] = SDObject("\u6e90" * 10000, Basic.Chunk, count=100,
                         factory=lambda i: SDObject("n" * 300, Basic.String, "\u6e90" * 20000))
    seen = []
    cursor = None
    while True:
        page = call(api_runtime, api_event_id=3, limit=1000, cursor=cursor)
        assert page["data"]["nameTruncated"] and page["truncated"]
        assert page["data"]["parameters"]
        for item in page["data"]["parameters"]:
            assert item["valueTruncated"] and item["valueLength"] == 20000
            assert item["nameTruncated"]
            seen.append(item["canonicalPath"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert seen == ["[%d]" % i for i in range(100)]


def test_recursion_and_node_budgets_with_resume_by_path(api_runtime, monkeypatch):
    monkeypatch.setattr(api_calls, "MAX_NODES", 10)
    array = SDObject("a", Basic.Array, count=100,
                     factory=lambda i: SDObject("$el", value=i))
    api_runtime.structured.chunks[2] = SDObject("C", Basic.Chunk, children=[array, SDObject("tail")])
    page = call(api_runtime, api_event_id=3, max_depth=4)
    assert page["truncated"] and len(array.reads) <= 9
    array_row = page["data"]["parameters"][0]
    assert array_row["membersTruncated"]
    leaf = call(api_runtime, api_event_id=3, parameter_path="[0][99]")
    assert leaf["data"]["selected"]["value"] == 99
    final = call(api_runtime, api_event_id=3, max_depth=4, cursor=page["nextCursor"])
    assert final["data"]["parameters"][0]["name"] == "tail"


def test_cancellation_checked_during_lazy_projection(api_runtime):
    array = SDObject("a", Basic.Array, count=1000000,
                     factory=lambda i: SDObject("el", value=i))
    api_runtime.structured.chunks[2] = SDObject("C", Basic.Chunk, children=[array])
    context = Context(api_runtime.state, {"api_event_id": 3, "parameter_path": "[0]", "limit": 1000})
    def cancel():
        context.checks += 1
        if context.checks >= 12:
            raise AgentBridgeError("CANCELLED", "cancel test")
    context.check_cancelled = cancel
    with pytest.raises(AgentBridgeError) as caught:
        api_calls.get_api_calls(api_runtime, context)
    assert caught.value.code == "CANCELLED"
    assert len(array.reads) < 20 and api_runtime.ctx.controller.frame_events == []


def test_bridge_sources_are_python36_compatible():
    for filename in (api_calls.__file__, sys.modules[ActionIndex.__module__].__file__):
        ast.parse(Path(filename).read_text(encoding="utf-8"), feature_version=(3, 6))
