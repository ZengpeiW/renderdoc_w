"""GPU counter discovery, strict sampling inputs and replay-sample transport contracts."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from fake_bridge import FakeRenderTestBridge
from helpers import ManualResponder, write_manifest
from rendertest_agent_bridge.constants import METHOD_SPECS as BRIDGE_SPECS
from rendertest_agent_bridge.methods import HANDLERS
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.ipc_client import BridgeClient, BridgeResponse
from rendertest_mcp.protocol import LONG_TIMEOUT_SECONDS, METHOD_SPECS, TOOL_NAMES
from rendertest_mcp.server import create_server

INSTANCE = "gpu-counter-test"
SESSION = "e" * 32
METHODS = ("list_gpu_counters", "get_gpu_duration")


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


def call(server, method, arguments=None):
    return asyncio.run(server.call_tool(method, arguments or {}))[1]


@pytest.mark.parametrize("method,arguments,expected,timeout", [
    ("list_gpu_counters", {}, {"limit": 200}, None),
    ("list_gpu_counters", {"cursor": None, "limit": 1000}, {"limit": 1000}, None),
    ("list_gpu_counters", {"cursor": "null", "limit": 1}, {"cursor": "null", "limit": 1}, None),
    ("get_gpu_duration", {}, {"limit": 200, "sort_by": "duration", "refresh": False}, LONG_TIMEOUT_SECONDS),
    ("get_gpu_duration", {"parent_event_id": None, "cursor": None},
     {"limit": 200, "sort_by": "duration", "refresh": False}, LONG_TIMEOUT_SECONDS),
    ("get_gpu_duration", {"parent_event_id": 2**32 - 1, "sort_by": "event_id", "refresh": True, "limit": 1},
     {"parent_event_id": 2**32 - 1, "sort_by": "event_id", "refresh": True, "limit": 1}, LONG_TIMEOUT_SECONDS),
    ("get_gpu_duration", {"parent_event_id": 0, "cursor": "null", "limit": 1000},
     {"parent_event_id": 0, "cursor": "null", "limit": 1000, "sort_by": "duration", "refresh": False},
     LONG_TIMEOUT_SECONDS),
])
def test_payload_defaults_literal_cursors_and_long_fetch_timeout(recording, method, arguments, expected, timeout):
    server, bridge = recording
    result = call(server, method, dict(arguments, instance_id="chosen-instance"))
    assert bridge.calls == [(method, expected, {"instance_id": "chosen-instance", "timeout": timeout})]
    assert result["effect"] == "read" and result["captureSessionId"] == SESSION


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("value", [True, False, 0, -1, 1001, 1.0, 1.5, "1", None])
def test_limit_rejects_coercion_and_range_before_bridge(recording, method, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, method, {"limit": value})
    assert bridge.calls == []


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("value", [0, True, [], {}, "x" * 1025])
def test_cursor_rejects_non_strings_and_oversized_values(recording, method, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, method, {"cursor": value})
    assert bridge.calls == []


@pytest.mark.parametrize("field,value", [
    ("parent_event_id", True), ("parent_event_id", False), ("parent_event_id", 1.0),
    ("parent_event_id", 1.5), ("parent_event_id", "1"), ("parent_event_id", "null"),
    ("parent_event_id", -1), ("parent_event_id", 2**32),
    ("refresh", 0), ("refresh", 1), ("refresh", 1.0), ("refresh", "true"),
    ("refresh", "false"), ("refresh", None), ("refresh", []),
    ("sort_by", "ms"), ("sort_by", "eventId"), ("sort_by", None), ("sort_by", True),
    ("instance_id", "../foreign"),
])
def test_duration_selectors_are_strict_before_bridge(recording, field, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "get_gpu_duration", {field: value})
    assert bridge.calls == []


@pytest.mark.parametrize("cursor", ["continued", "", "null"])
def test_refresh_cannot_silently_discard_a_cursor(recording, cursor):
    server, bridge = recording
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        call(server, "get_gpu_duration", {"refresh": True, "cursor": cursor})
    assert bridge.calls == []


def test_catalog_permissions_registry_and_dynamic_claude_tools(recording):
    from rendertest_mcp.claude_backend import TOOL_NAMES as CLAUDE_TOOLS
    from rendertest_mcp.claude_events import TOOL_NAMES as CLAUDE_EVENT_TOOLS

    server, _ = recording
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert len(tools) == 34
    assert set(tools) == set(TOOL_NAMES) == set(CLAUDE_TOOLS) == set(CLAUDE_EVENT_TOOLS)
    for method in METHODS:
        assert method in HANDLERS
        assert METHOD_SPECS[method] == BRIDGE_SPECS[method] == {"effect": "read", "requiresCapture": True}
        annotations = tools[method].annotations
        assert annotations.readOnlyHint and annotations.idempotentHint
        assert not annotations.destructiveHint and not annotations.openWorldHint
        assert not tools[method].inputSchema.get("required")
        limit = tools[method].inputSchema["properties"]["limit"]
        assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 1000, 200)
    props = tools["get_gpu_duration"].inputSchema["properties"]
    assert props["sort_by"]["enum"] == ["duration", "event_id"]
    assert props["sort_by"]["default"] == "duration" and props["refresh"]["default"] is False
    description = tools["get_gpu_duration"].description
    assert "whole capture" in description and "not a reliable frame time" in description
    assert "not immediately cancellable" in description and "sampleId" in description
    assert "does not sample" in tools["list_gpu_counters"].description


@pytest.mark.parametrize("code", ["UNSUPPORTED", "REPLAY_ERROR", "CANCELLED", "TIMEOUT",
                                  "CAPTURE_CHANGED", "INVALID_EVENT", "INVALID_REQUEST", "LIMIT_EXCEEDED"])
def test_native_failure_is_not_an_empty_success_or_zero_duration(recording, code):
    server, bridge = recording
    bridge.error = RenderTestAgentError(code, "Counter sample failed.", details={"counterId": 1})
    with pytest.raises(ToolError) as caught:
        call(server, "get_gpu_duration")
    assert '"code": "' + code + '"' in str(caught.value)
    assert '"counterId": 1' in str(caught.value)


def test_partial_sample_status_counts_and_raw_evidence_are_preserved(recording):
    server, bridge = recording
    data = {"sampleId": "sample-a", "samplingScope": "capture", "scopeActionCount": 4,
            "validDurationCount": 1, "invalidDurationCount": 1, "missingDurationCount": 1,
            "duplicateDurationCount": 1, "events": [
                {"eventId": 2, "status": "valid", "durationSeconds": 0.0, "durationMs": 0.0},
                {"eventId": 3, "status": "invalid", "durationSeconds": None, "durationMs": None,
                 "rawDuration": -1.0, "reason": "negative"},
                {"eventId": 4, "status": "missing", "durationSeconds": None, "durationMs": None},
                {"eventId": 5, "status": "duplicate", "durationSeconds": None, "durationMs": None},
            ]}
    bridge.response = {"data": data, "warnings": ["Only one event has a valid native value."],
                       "truncated": True, "nextCursor": "opaque-sample-page"}
    result = call(server, "get_gpu_duration")
    assert result["data"] == data
    assert result["truncated"] and result["nextCursor"] == "opaque-sample-page"
    assert result["warnings"] == bridge.response["warnings"] and result["artifacts"] == []


def test_fake_catalog_and_duration_pages_share_sample_and_preserve_ui(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        server = create_server(BridgeClient(root=tmp_path, poll_interval=0.005, environ={
            "RENDERTEST_AGENT_FIXED_INSTANCE_ID": fake.instance_id,
            "RENDERTEST_AGENT_CAPTURE_SESSION_ID": fake.capture_session_id,
        }))
        catalog = call(server, "list_gpu_counters", {"limit": 1})
        assert fake._gpu_duration_sample is None
        first_counter = catalog["data"]["counters"][0]
        assert first_counter["id"] == 1 and first_counter["durationSamplingSupported"]
        assert (first_counter["resultType"], first_counter["resultByteWidth"], first_counter["unit"]) == (
            "Float", 8, "Seconds")
        other = call(server, "list_gpu_counters", {"limit": 1, "cursor": catalog["nextCursor"]})
        assert not other["nextCursor"] and not other["data"]["counters"][0]["durationSamplingSupported"]
        before = fake.current_event_id
        first = call(server, "get_gpu_duration", {"limit": 1})
        assert first["data"]["cached"] is False and first["data"]["events"][0]["eventId"] == 10
        second = call(server, "get_gpu_duration", {"limit": 1, "cursor": first["nextCursor"]})
        assert second["data"]["sampleId"] == first["data"]["sampleId"] and second["data"]["cached"]
        assert second["data"]["events"][0]["status"] == "valid"
        assert second["data"]["events"][0]["durationSeconds"] == 0
        scoped = call(server, "get_gpu_duration", {"parent_event_id": 30, "sort_by": "event_id"})
        assert scoped["data"]["sampleId"] == first["data"]["sampleId"]
        assert scoped["data"]["invalidDurationCount"] == 1
        assert scoped["data"]["events"][0]["durationSeconds"] is None
        assert scoped["data"]["events"][0]["rawDuration"] == -1
        assert fake.current_event_id == before
        assert first["meta"]["replayEventRestored"]
        assert first["meta"]["eventIdBefore"] == first["meta"]["eventIdAfter"] == before
        for mismatch in ({"parent_event_id": 1}, {"sort_by": "event_id"}):
            with pytest.raises(ToolError, match="INVALID_REQUEST"):
                call(server, "get_gpu_duration", dict(mismatch, cursor=first["nextCursor"]))
        refreshed = call(server, "get_gpu_duration", {"refresh": True})
        assert refreshed["data"]["sampleId"] != first["data"]["sampleId"]
        assert not refreshed["data"]["cached"]
        with pytest.raises(ToolError, match="INVALID_REQUEST"):
            call(server, "get_gpu_duration", {"cursor": first["nextCursor"]})


@pytest.mark.parametrize("method", METHODS)
def test_capture_is_required_and_old_session_cursors_expire(tmp_path, method):
    with FakeRenderTestBridge(tmp_path) as fake:
        server = create_server(BridgeClient(root=tmp_path, poll_interval=0.005, environ={}))
        cursor = call(server, method, {"limit": 1})["nextCursor"]
        fake.close_capture()
        with pytest.raises(ToolError, match="NO_CAPTURE"):
            call(server, method)
        fake.open_capture()
        with pytest.raises(ToolError, match="CAPTURE_MISMATCH|CAPTURE_CHANGED"):
            call(server, method, {"cursor": cursor})
        server = create_server(BridgeClient(root=tmp_path, poll_interval=0.005, environ={
            "RENDERTEST_AGENT_FIXED_INSTANCE_ID": fake.instance_id,
            "RENDERTEST_AGENT_CAPTURE_SESSION_ID": fake.capture_session_id,
        }))
        with pytest.raises(ToolError, match="INVALID_REQUEST"):
            call(server, method, {"cursor": cursor})


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("wrong_session", [False, True])
def test_fixed_capture_binding_is_sent_and_validated(tmp_path, method, wrong_session):
    directory = write_manifest(tmp_path, INSTANCE, capture_session_id=SESSION)
    requests = []

    def respond(request):
        requests.append(request)
        return json.dumps({
            "schema": "rendertest-agent-response/v1", "protocol": "rendertest-agent-ipc/v1",
            "id": request["id"], "ok": True, "result": {"data": {"available": False}},
            "meta": {"instanceId": INSTANCE, "captureSessionId": "f" * 32 if wrong_session else SESSION},
        }).encode()

    server = create_server(BridgeClient(root=tmp_path, poll_interval=0.005, environ={
        "RENDERTEST_AGENT_FIXED_INSTANCE_ID": INSTANCE, "RENDERTEST_AGENT_CAPTURE_SESSION_ID": SESSION,
    }))
    with ManualResponder(directory, respond):
        if wrong_session:
            with pytest.raises(ToolError, match="CAPTURE_CHANGED|CAPTURE_MISMATCH|INVALID_RESPONSE"):
                call(server, method)
        else:
            assert call(server, method)["captureSessionId"] == SESSION
    assert requests[0]["expected"] == {"instanceId": INSTANCE, "captureSessionId": SESSION}
    assert requests[0]["method"] == method
