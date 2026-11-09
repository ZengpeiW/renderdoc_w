"""Compute debugger schema, fixed-capture and trace-reader integration contracts."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from fake_bridge import FakeRenderTestBridge
from helpers import ManualResponder, write_manifest
from rendertest_agent_bridge.constants import METHOD_SPECS as BRIDGE_SPECS, P1_METHODS
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.ipc_client import BridgeClient, BridgeResponse
from rendertest_mcp.protocol import LONG_TIMEOUT_SECONDS, METHOD_SPECS, TOOL_NAMES
from rendertest_mcp.server import create_server

INSTANCE = "compute-test"
SESSION = "c" * 32
BASE = {"group": [0, 0, 0], "thread": [1, 2, 0]}


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


def call(server, arguments):
    return asyncio.run(server.call_tool("debug_compute", arguments))[1]


@pytest.mark.parametrize("arguments,expected", [
    (BASE, {**BASE, "max_steps": 20000}),
    ({**BASE, "event_id": None}, {**BASE, "max_steps": 20000}),
    ({**BASE, "event_id": 0, "max_steps": 1}, {**BASE, "event_id": 0, "max_steps": 1}),
    ({"group": [2**32 - 1, 0, 0], "thread": [0, 2**32 - 1, 0],
      "event_id": 2**32 - 1, "max_steps": 200000},
     {"group": [2**32 - 1, 0, 0], "thread": [0, 2**32 - 1, 0],
      "event_id": 2**32 - 1, "max_steps": 200000}),
])
def test_payload_defaults_and_uint32_boundaries(recording, arguments, expected):
    server, bridge = recording
    result = call(server, dict(arguments, instance_id="selected-window"))
    assert bridge.calls == [("debug_compute", expected,
                             {"instance_id": "selected-window", "timeout": LONG_TIMEOUT_SECONDS})]
    assert result["effect"] == "artifact" and result["captureSessionId"] == SESSION


@pytest.mark.parametrize("field", ["group", "thread"])
@pytest.mark.parametrize("value", [
    None, [], [0], [0, 0], [0, 0, 0, 0], {"x": 0, "y": 0, "z": 0}, 0, True,
    (0, 0, 0), "xyz", "[0,0,0]", [True, 0, 0], [0, False, 0], [0, 0, 0.0], [1.5, 0, 0],
    [0, "0", 0], [None, 0, 0], [-1, 0, 0], [0, 0, 2**32], [[0], 0, 0],
])
def test_coordinates_reject_shape_and_numeric_coercion_before_bridge(recording, field, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, dict(BASE, **{field: value}))
    assert bridge.calls == []


@pytest.mark.parametrize("arguments", [{}, {"group": [0, 0, 0]}, {"thread": [0, 0, 0]}])
def test_both_coordinates_are_required(recording, arguments):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, arguments)
    assert bridge.calls == []


@pytest.mark.parametrize("field,value", [
    ("event_id", True), ("event_id", False), ("event_id", 1.0), ("event_id", 1.5),
    ("event_id", "1"), ("event_id", -1), ("event_id", 2**32),
    ("max_steps", True), ("max_steps", False), ("max_steps", 1.0), ("max_steps", 1.5),
    ("max_steps", "1"), ("max_steps", None), ("max_steps", 0), ("max_steps", 200001),
    ("instance_id", "../foreign"),
])
def test_invalid_event_and_step_budget_stop_before_bridge(recording, field, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, dict(BASE, **{field: value}))
    assert bridge.calls == []


def test_catalog_effect_limits_and_dynamic_claude_allowlist(recording):
    from rendertest_mcp.claude_backend import TOOL_NAMES as CLAUDE_TOOLS
    from rendertest_mcp.claude_events import TOOL_NAMES as CLAUDE_EVENT_TOOLS

    server, _ = recording
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert len(tools) == 34
    assert set(tools) == set(TOOL_NAMES) == set(CLAUDE_TOOLS) == set(CLAUDE_EVENT_TOOLS)
    assert "debug_compute" in P1_METHODS
    assert METHOD_SPECS["debug_compute"] == BRIDGE_SPECS["debug_compute"] == {
        "effect": "artifact", "requiresCapture": True}
    tool = tools["debug_compute"]
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.idempotentHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.openWorldHint is False
    assert tool.inputSchema["required"] == ["group", "thread"]
    for field in ("group", "thread"):
        schema = tool.inputSchema["properties"][field]
        assert schema["type"] == "array"
        assert schema["minItems"] == schema["maxItems"] == 3
        assert schema["items"]["type"] == "integer"
        assert schema["items"]["minimum"] == 0 and schema["items"]["maximum"] == 2**32 - 1
    steps = tool.inputSchema["properties"]["max_steps"]
    assert steps["minimum"] == 1 and steps["maximum"] == 200000 and steps["default"] == 20000
    assert "read_artifact" in tool.description and "not a global" in tool.description
    assert "not a complete log of UAV writes" in tool.description


@pytest.mark.parametrize("code", ["UNSUPPORTED", "INVALID_EVENT", "INVALID_REQUEST", "TIMEOUT",
                                  "CANCELLED", "CAPTURE_CHANGED", "LIMIT_EXCEEDED"])
def test_native_failures_preserve_codes_and_coordinate_details(recording, code):
    server, bridge = recording
    bridge.error = RenderTestAgentError(code, "Cannot debug this invocation.",
                                       details={"group": [0, 0, 0], "thread": [1, 2, 0], "stage": "compute"})
    with pytest.raises(ToolError) as raised:
        call(server, BASE)
    assert '"code": "' + code + '"' in str(raised.value)
    assert '"stage": "compute"' in str(raised.value)


@pytest.mark.parametrize("debugged", [False, True])
def test_unavailable_and_partial_trace_metadata_are_not_promoted_to_complete(recording, debugged):
    server, bridge = recording
    data = {"stage": "compute", "debugged": debugged, "executionComplete": False,
            "traceComplete": False, "outputs": [], "steps": 1 if debugged else 0}
    bridge.response = {"data": data, "warnings": ["Step limit reached" if debugged else "No debugger"],
                       "truncated": debugged}
    result = call(server, BASE)
    assert result["data"] == data
    assert result["warnings"] == bridge.response["warnings"]
    assert result["truncated"] is debugged
    assert result["artifacts"] == []


def test_fake_compute_trace_is_scoped_readable_and_keeps_ui_event(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        original = fake.current_event_id
        server = create_server(BridgeClient(root=tmp_path, poll_interval=0.005, environ={
            "RENDERTEST_AGENT_FIXED_INSTANCE_ID": fake.instance_id,
            "RENDERTEST_AGENT_CAPTURE_SESSION_ID": fake.capture_session_id,
        }))
        result = call(server, dict(BASE, event_id=20))
        data, artifact = result["data"], result["artifacts"][0]
        assert data["globalThread"] == [1, 2, 0] and data["outputs"] == []
        assert data["dispatchDimensions"] == [1, 1, 1]
        assert data["threadGroupSize"] == [4, 4, 1]
        assert artifact["kind"] == "shader-debug-trace"
        assert artifact["source"]["group"] == BASE["group"]
        assert artifact["source"]["thread"] == BASE["thread"]
        trace = asyncio.run(server.call_tool("read_artifact", {
            "path": data["traceArtifact"], "limit": 1}))[1]
        header = json.loads(trace["data"]["lines"][0]["text"])
        assert header["schema"] == "rendertest-shader-debug-trace/v1"
        invocation = header["invocation"]
        assert invocation["stage"] == "compute" and invocation["eventId"] == 20
        assert invocation["group"] == BASE["group"] and invocation["thread"] == BASE["thread"]
        assert fake.current_event_id == original
        assert result["meta"]["replayEventRestored"] is True
        assert result["meta"]["eventIdBefore"] == result["meta"]["eventIdAfter"] == original
        with pytest.raises(ToolError, match="INVALID_EVENT"):
            call(server, dict(BASE, event_id=30))
        with pytest.raises(ToolError, match="INVALID_REQUEST"):
            call(server, dict(BASE, event_id=20, group=[1, 0, 0]))
        with pytest.raises(ToolError, match="INVALID_REQUEST"):
            call(server, dict(BASE, event_id=20, thread=[0, 4, 0]))


@pytest.mark.parametrize("wrong_session", [False, True])
def test_compute_requires_current_capture_binding(tmp_path, wrong_session):
    directory = write_manifest(tmp_path, INSTANCE, capture_session_id=SESSION)
    requests = []

    def respond(request):
        requests.append(request)
        return json.dumps({
            "schema": "rendertest-agent-response/v1", "protocol": "rendertest-agent-ipc/v1",
            "id": request["id"], "ok": True,
            "result": {"data": {"debugged": False, "stage": "compute"}},
            "meta": {"instanceId": INSTANCE, "captureSessionId": "d" * 32 if wrong_session else SESSION},
        }).encode()

    server = create_server(BridgeClient(root=tmp_path, poll_interval=0.005, environ={
        "RENDERTEST_AGENT_FIXED_INSTANCE_ID": INSTANCE, "RENDERTEST_AGENT_CAPTURE_SESSION_ID": SESSION,
    }))
    with ManualResponder(directory, respond):
        if wrong_session:
            with pytest.raises(ToolError, match="CAPTURE_CHANGED|CAPTURE_MISMATCH|INVALID_RESPONSE"):
                call(server, dict(BASE, event_id=20))
        else:
            assert call(server, dict(BASE, event_id=20))["captureSessionId"] == SESSION
    assert requests[0]["expected"] == {"instanceId": INSTANCE, "captureSessionId": SESSION}
    assert requests[0]["method"] == "debug_compute"
