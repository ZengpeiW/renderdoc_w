"""API-call and event-comparison contracts through the real FastMCP argument parser."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from helpers import ManualResponder, write_manifest
from rendertest_agent_bridge.constants import METHOD_SPECS as BRIDGE_SPECS, P0_METHODS
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.ipc_client import BridgeClient, BridgeResponse
from rendertest_mcp.protocol import METHOD_SPECS, TOOL_NAMES
from rendertest_mcp.server import create_server

INSTANCE = "event-tools-test"
SESSION = "f" * 32
COMPARE = {"before_event_id": 10, "after_event_id": 30}


class RecordingBridge:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.error = None
        self.response = None

    def call(self, method, params, **options):
        self.calls.append((method, params, options))
        if self.error is not None:
            raise self.error
        return BridgeResponse("request", INSTANCE, self.response or {"data": params},
                              {"captureSessionId": SESSION})


@pytest.fixture
def recording(tmp_path):
    bridge = RecordingBridge(tmp_path)
    return create_server(bridge), bridge


def call(server, name, arguments):
    return asyncio.run(server.call_tool(name, arguments))[1]


@pytest.mark.parametrize("name,arguments,expected", [
    ("get_api_calls", {}, {"limit": 100, "max_depth": 0}),
    ("get_api_calls", {"event_id": None, "api_event_id": None, "parameter_path": None},
     {"limit": 100, "max_depth": 0}),
    ("get_api_calls", {"event_id": 0, "api_event_id": 0, "parameter_path": "", "max_depth": 4,
                       "limit": 1000},
     {"event_id": 0, "api_event_id": 0, "parameter_path": "", "max_depth": 4, "limit": 1000}),
    ("compare_events", COMPARE,
     {**COMPARE, "include_bindings": True, "binding_limit": 64, "limit": 200}),
    ("compare_events", {**COMPARE, "stages": ["compute"], "include_bindings": False,
                        "binding_limit": 256, "limit": 1, "cbuffers": []},
     {**COMPARE, "stages": ["compute"], "include_bindings": False,
      "binding_limit": 256, "limit": 1, "cbuffers": []}),
])
def test_defaults_and_boundaries_reach_bridge(recording, name, arguments, expected):
    server, bridge = recording
    result = call(server, name, dict(arguments, instance_id="selected-window"))
    assert bridge.calls == [(name, expected, {"instance_id": "selected-window", "timeout": None})]
    assert result["effect"] == "read" and result["captureSessionId"] == SESSION


INTEGER_FIELDS = [
    ("get_api_calls", {}, "event_id"),
    ("get_api_calls", {}, "api_event_id"),
    ("get_api_calls", {}, "limit"),
    ("get_api_calls", {}, "max_depth"),
    ("compare_events", COMPARE, "before_event_id"),
    ("compare_events", COMPARE, "after_event_id"),
    ("compare_events", COMPARE, "binding_limit"),
    ("compare_events", COMPARE, "limit"),
]


@pytest.mark.parametrize("name,base,field", INTEGER_FIELDS)
@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, "1", -1])
def test_integer_parameters_reject_coercion_before_bridge(recording, name, base, field, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, name, dict(base, **{field: value}))
    assert bridge.calls == []


@pytest.mark.parametrize("name,base,field,value", [
    ("get_api_calls", {}, "event_id", 2**32),
    ("get_api_calls", {}, "api_event_id", 2**32),
    ("get_api_calls", {}, "limit", 0),
    ("get_api_calls", {}, "limit", 1001),
    ("get_api_calls", {}, "limit", None),
    ("get_api_calls", {}, "max_depth", 5),
    ("get_api_calls", {}, "max_depth", None),
    ("compare_events", COMPARE, "before_event_id", 2**32),
    ("compare_events", COMPARE, "after_event_id", None),
    ("compare_events", COMPARE, "binding_limit", 0),
    ("compare_events", COMPARE, "binding_limit", 257),
    ("compare_events", COMPARE, "binding_limit", None),
    ("compare_events", COMPARE, "limit", 0),
    ("compare_events", COMPARE, "limit", 1001),
    ("compare_events", COMPARE, "limit", None),
    ("compare_events", COMPARE, "include_bindings", 1),
    ("compare_events", COMPARE, "include_bindings", "true"),
    ("compare_events", COMPARE, "stages", ["fragment"]),
    ("compare_events", COMPARE, "stages", [True]),
    ("compare_events", COMPARE, "cbuffers", [{"stage": "pixel", "index": 0}] * 9),
])
def test_invalid_budgets_and_selectors_stop_before_bridge(recording, name, base, field, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, name, dict(base, **{field: value}))
    assert bridge.calls == []


@pytest.mark.parametrize("path", ["", "[1]", "[0][2]", "[0]" * 64])
def test_nullable_ordinal_path_survives_fastmcp_json_preparse(recording, path):
    server, bridge = recording
    call(server, "get_api_calls", {"api_event_id": 29, "parameter_path": path})
    assert bridge.calls[-1][1]["parameter_path"] == path
    assert type(bridge.calls[-1][1]["parameter_path"]) is str


@pytest.mark.parametrize("path", [[1], {"index": 1}, 1, True, "null", '["name"]',
                                "[0]" * 65, "[0].name", "[-1]", "x" * 4097])
def test_invalid_parameter_path_is_not_reinterpreted_as_a_default(recording, path):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "get_api_calls", {"api_event_id": 29, "parameter_path": path})
    assert bridge.calls == []


def test_parameter_path_requires_an_exact_api_event(recording):
    server, bridge = recording
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        call(server, "get_api_calls", {"event_id": 30, "parameter_path": ""})
    assert bridge.calls == []


@pytest.mark.parametrize("path", ["", "[1]", '["name"]', "null", "Lights[123].Color"])
@pytest.mark.parametrize("json_encoded", [False, True])
def test_nested_cbuffer_paths_are_literal_strings_and_defaults_are_serialized(
        recording, path, json_encoded):
    server, bridge = recording
    selections = [{"stage": "pixel", "index": 0, "member_path": path}]
    call(server, "compare_events", {**COMPARE, "cbuffers": json.dumps(selections)
                                    if json_encoded else selections})
    assert bridge.calls[-1][1]["cbuffers"] == [
        {"stage": "pixel", "index": 0, "member_path": path, "max_depth": 1, "member_limit": 200}]


@pytest.mark.parametrize("selection", [
    {"stage": "pixel"}, {"stage": "pixel", "index": 0, "name": "Block"},
    {"stage": "pixel", "index": True}, {"stage": "pixel", "index": 0.0},
    {"stage": "pixel", "index": "0"}, {"stage": "pixel", "index": -1},
    {"stage": "pixel", "name": ""}, {"stage": "pixel", "name": "x" * 257},
    {"stage": "unknown", "index": 0}, {"stage": "pixel", "index": 0, "member_path": [1]},
    {"stage": "pixel", "index": 0, "member_path": None},
    {"stage": "pixel", "index": 0, "member_path": "x" * 4097},
    {"stage": "pixel", "index": 0, "max_depth": True},
    {"stage": "pixel", "index": 0, "max_depth": 1.0},
    {"stage": "pixel", "index": 0, "max_depth": 5},
    {"stage": "pixel", "index": 0, "member_limit": False},
    {"stage": "pixel", "index": 0, "member_limit": "1"},
    {"stage": "pixel", "index": 0, "member_limit": 1001},
    {"stage": "pixel", "index": 0, "member_limit": 0},
    {"stage": "pixel", "index": 0, "typo": 1},
])
def test_cbuffer_selection_validation_precedes_bridge(recording, selection):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "compare_events", {**COMPARE, "cbuffers": [selection]})
    assert bridge.calls == []


def test_new_catalog_is_complete_read_only_and_shared_by_claude(recording):
    from rendertest_mcp.claude_backend import TOOL_NAMES as CLAUDE_TOOLS
    from rendertest_mcp.claude_events import TOOL_NAMES as CLAUDE_EVENT_TOOLS

    server, _ = recording
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert len(tools) == 35
    assert set(tools) == set(TOOL_NAMES) == set(CLAUDE_TOOLS) == set(CLAUDE_EVENT_TOOLS)
    for name in ("get_api_calls", "compare_events"):
        assert name in P0_METHODS
        assert METHOD_SPECS[name] == BRIDGE_SPECS[name] == {"effect": "read", "requiresCapture": True}
        annotation = tools[name].annotations
        assert annotation.readOnlyHint and annotation.idempotentHint
        assert not annotation.destructiveHint and not annotation.openWorldHint
    assert tools["compare_events"].inputSchema["required"] == ["before_event_id", "after_event_id"]
    api = tools["get_api_calls"].inputSchema
    assert not api.get("required")
    assert api["properties"]["limit"]["default"] == 100
    assert api["properties"]["max_depth"]["default"] == 0
    path = api["properties"]["parameter_path"]
    assert path["default"] is None
    assert {item["type"] for item in path["anyOf"]} == {"string", "null"}
    compare = tools["compare_events"].inputSchema
    block = compare["$defs"]["ComparisonCBuffer"]
    assert block["additionalProperties"] is False
    assert block["properties"]["max_depth"]["default"] == 1
    assert block["properties"]["member_limit"]["maximum"] == 1000
    assert "equal-in-scope" in tools["compare_events"].description


@pytest.mark.parametrize("method,arguments", [("get_api_calls", {}), ("compare_events", COMPARE)])
@pytest.mark.parametrize("code", ["INVALID_EVENT", "CAPTURE_CHANGED", "CANCELLED", "LIMIT_EXCEEDED"])
def test_bridge_failures_preserve_details(recording, method, arguments, code):
    server, bridge = recording
    bridge.error = RenderTestAgentError(code, "Cannot complete the requested read.",
                                       details={"eventId": 29, "reason": "fixture"})
    with pytest.raises(ToolError) as raised:
        call(server, method, arguments)
    assert '"code": "' + code + '"' in str(raised.value)
    assert '"eventId": 29' in str(raised.value)


@pytest.mark.parametrize("method,arguments", [("get_api_calls", {"api_event_id": 29}),
                                              ("compare_events", COMPARE)])
@pytest.mark.parametrize("wrong_session", [False, True])
def test_event_tools_cannot_escape_fixed_capture_binding(tmp_path, method, arguments, wrong_session):
    directory = write_manifest(tmp_path, INSTANCE, capture_session_id=SESSION)
    requests = []

    def respond(request):
        requests.append(request)
        return json.dumps({
            "schema": "rendertest-agent-response/v1", "protocol": "rendertest-agent-ipc/v1",
            "id": request["id"], "ok": True, "result": {"data": {"fixture": True}},
            "meta": {"instanceId": INSTANCE, "captureSessionId": "e" * 32 if wrong_session else SESSION},
        }).encode()

    server = create_server(BridgeClient(root=tmp_path, poll_interval=0.005, environ={
        "RENDERTEST_AGENT_FIXED_INSTANCE_ID": INSTANCE, "RENDERTEST_AGENT_CAPTURE_SESSION_ID": SESSION,
    }))
    with ManualResponder(directory, respond):
        if wrong_session:
            with pytest.raises(ToolError, match="CAPTURE_CHANGED|CAPTURE_MISMATCH|INVALID_RESPONSE"):
                call(server, method, arguments)
        else:
            result = call(server, method, arguments)
            assert result["effect"] == "read" and result["captureSessionId"] == SESSION
    assert len(requests) == 1
    assert requests[0]["expected"] == {"instanceId": INSTANCE, "captureSessionId": SESSION}
