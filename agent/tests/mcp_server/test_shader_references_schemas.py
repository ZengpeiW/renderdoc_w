"""Shader-reference strict schema, bounded scan pages and capture-local cache contracts."""

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

METHOD = "find_shader_references"
INSTANCE = "shader-reference-test"
SESSION = "a" * 32
DEFAULTS = {"resource_id": "3001", "limit": 200, "scan_budget": 200, "refresh": False}


class RecordingBridge:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.response = None
        self.error = None

    def call(self, method, params, **options):
        self.calls.append((method, params, options))
        if self.error:
            raise self.error
        return BridgeResponse("request", INSTANCE, self.response or {"data": params},
                              {"captureSessionId": SESSION})


@pytest.fixture
def recording(tmp_path):
    bridge = RecordingBridge(tmp_path)
    return create_server(bridge), bridge


def call(server, params):
    return asyncio.run(server.call_tool(METHOD, params))[1]


@pytest.mark.parametrize("params,expected", [
    ({"resource_id": "3001"}, DEFAULTS),
    ({"resource_id": "3001", "stages": None, "parent_event_id": None, "cursor": None}, DEFAULTS),
    ({"resource_id": "3001", "stages": ["pixel", "vertex", "pixel"]},
     dict(DEFAULTS, stages=["vertex", "pixel"])),
    ({"resource_id": str(2**64 - 1), "parent_event_id": 2**32 - 1, "limit": 1000,
      "scan_budget": 2000, "refresh": True},
     {"resource_id": str(2**64 - 1), "parent_event_id": 2**32 - 1, "limit": 1000,
      "scan_budget": 2000, "refresh": True}),
    ({"resource_id": "3001", "parent_event_id": 0, "cursor": "null", "limit": 1, "scan_budget": 1},
     dict(DEFAULTS, parent_event_id=0, cursor="null", limit=1, scan_budget=1)),
])
def test_defaults_canonical_stages_exact_ids_and_timeout(recording, params, expected):
    server, bridge = recording
    result = call(server, dict(params, instance_id="chosen-window"))
    assert bridge.calls == [(METHOD, expected, {"instance_id": "chosen-window", "timeout": LONG_TIMEOUT_SECONDS})]
    assert result["effect"] == "read" and result["captureSessionId"] == SESSION


@pytest.mark.parametrize("value", [None, True, 3001, 3001.0, "", "null", "-1", "+1", "1.0", "0x10",
                                   " 3001", "3001 ", "１２３", "1" * 21, str(2**64), [], {}])
def test_resource_id_is_strict_decimal_uint64_before_bridge(recording, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, {"resource_id": value})
    assert bridge.calls == []


def test_resource_id_required(recording):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, {})
    assert bridge.calls == []


@pytest.mark.parametrize("value", [[], ["pixel"] * 9, "pixel", '["pixel"]', "null", ("pixel",),
                                   ["fragment"], ["raygen"], ["Pixel"], [None], [True], [1], {}])
def test_stages_require_nonempty_actual_list_of_known_stages(recording, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, {"resource_id": "3001", "stages": value})
    assert bridge.calls == []


@pytest.mark.parametrize("field,value", [
    ("parent_event_id", True), ("parent_event_id", 1.0), ("parent_event_id", "1"),
    ("parent_event_id", "null"), ("parent_event_id", -1), ("parent_event_id", 2**32),
    ("limit", True), ("limit", 1.0), ("limit", "1"), ("limit", None), ("limit", 0), ("limit", 1001),
    ("scan_budget", False), ("scan_budget", 1.0), ("scan_budget", "1"), ("scan_budget", None),
    ("scan_budget", 0), ("scan_budget", 2001), ("refresh", 1), ("refresh", 0),
    ("refresh", "true"), ("refresh", "false"), ("refresh", None),
    ("cursor", 1), ("cursor", []), ("cursor", {}), ("cursor", "x" * 1025),
    ("instance_id", "../foreign"),
])
def test_selectors_and_budgets_reject_coercion_before_bridge(recording, field, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, {"resource_id": "3001", field: value})
    assert bridge.calls == []


@pytest.mark.parametrize("cursor", ["continued", "", "null"])
def test_refresh_with_cursor_rejected_before_bridge(recording, cursor):
    server, bridge = recording
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        call(server, {"resource_id": "3001", "refresh": True, "cursor": cursor})
    assert bridge.calls == []


def test_catalog_registry_effect_and_dynamic_claude_allowlist(recording):
    from rendertest_mcp.claude_backend import TOOL_NAMES as CLAUDE_TOOLS
    from rendertest_mcp.claude_events import TOOL_NAMES as CLAUDE_EVENT_TOOLS

    server, _ = recording
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert len(tools) == 34 and set(tools) == set(TOOL_NAMES) == set(CLAUDE_TOOLS) == set(CLAUDE_EVENT_TOOLS)
    assert METHOD in HANDLERS
    assert METHOD_SPECS[METHOD] == BRIDGE_SPECS[METHOD] == {"effect": "read", "requiresCapture": True}
    tool = tools[METHOD]
    assert tool.annotations.readOnlyHint and tool.annotations.idempotentHint
    assert not tool.annotations.destructiveHint and not tool.annotations.openWorldHint
    assert tool.inputSchema["required"] == ["resource_id"]
    props = tool.inputSchema["properties"]
    assert props["resource_id"]["type"] == "string"
    assert props["resource_id"]["pattern"] == r"^[0-9]{1,20}$"
    stages = next(item for item in props["stages"]["anyOf"] if item["type"] == "array")
    assert (stages["minItems"], stages["maxItems"]) == (1, 8)
    assert len(stages["items"]["enum"]) == 8
    assert (props["scan_budget"]["minimum"], props["scan_budget"]["maximum"], props["scan_budget"]["default"]) == (1, 2000, 200)
    assert "empty references page" in tool.description and "cache hits" in tool.description
    assert "do not prove actual shader execution" in tool.description


@pytest.mark.parametrize("code", ["INVALID_RESOURCE", "INVALID_EVENT", "INVALID_REQUEST", "UNSUPPORTED",
                                  "REPLAY_ERROR", "CAPTURE_CHANGED", "CANCELLED", "TIMEOUT", "LIMIT_EXCEEDED"])
def test_native_failures_do_not_become_empty_complete_results(recording, code):
    server, bridge = recording
    bridge.error = RenderTestAgentError(code, "Cannot scan shader references.", details={"resource_id": "3001"})
    with pytest.raises(ToolError) as caught:
        call(server, {"resource_id": "3001"})
    assert '"code": "' + code + '"' in str(caught.value)
    assert '"resource_id": "3001"' in str(caught.value)


def test_unknown_entrypoint_and_unsupported_coverage_preserved(recording):
    server, bridge = recording
    data = {"resourceId": "3001", "scanId": "scan-a", "references": [{"eventId": 10, "matches": [
        {"stage": "vertex", "resourceId": "3001", "entryPoint": None, "entryPointKnown": False}]}],
        "coverage": {"scanComplete": True, "complete": False, "unsupportedActionCount": 1}}
    bridge.response = {"data": data, "truncated": True, "warnings": ["Ray tables are unsupported."]}
    result = call(server, {"resource_id": "3001"})
    assert result["data"] == data and result["truncated"]
    assert result["warnings"] == bridge.response["warnings"] and result["nextCursor"] is None


def fake_server(fake, root):
    return create_server(BridgeClient(root=root, poll_interval=0.005, environ={
        "RENDERTEST_AGENT_FIXED_INSTANCE_ID": fake.instance_id,
        "RENDERTEST_AGENT_CAPTURE_SESSION_ID": fake.capture_session_id,
    }))


def test_empty_page_progress_and_cached_cross_query_bindings(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        server = fake_server(fake, tmp_path)
        before = fake.current_event_id
        params = {"resource_id": "3004", "stages": ["pixel"], "scan_budget": 1}
        first = call(server, params)
        coverage = first["data"]["coverage"]
        assert first["data"]["references"] == [] and first["nextCursor"] and first["truncated"]
        assert coverage["candidateEventCount"] == 2 and coverage["stageExcludedActionCount"] == 1
        assert coverage["processedCandidateCount"] == coverage["examinedThisPage"] == 1
        assert coverage["nativeReadsThisPage"] == 1 and not coverage["scanComplete"]
        second = call(server, dict(params, cursor=first["nextCursor"], limit=1000, scan_budget=2))
        assert second["data"]["scanId"] == first["data"]["scanId"]
        assert [row["eventId"] for row in second["data"]["references"]] == [30]
        assert second["data"]["coverage"]["complete"] and not second["nextCursor"]
        # Prior pixel queries cached all relevant graphics stages, so vertex queries reuse them.
        vertex = call(server, {"resource_id": "3001", "stages": ["vertex"], "scan_budget": 1})
        assert vertex["data"]["scanId"] == first["data"]["scanId"]
        assert vertex["data"]["coverage"]["cacheHitsThisPage"] == 1
        assert vertex["data"]["coverage"]["nativeReadsThisPage"] == 0
        assert vertex["data"]["coverage"]["examinedThisPage"] == 1 and vertex["nextCursor"]
        assert vertex["data"]["references"][0]["matches"] == [
            {"stage": "vertex", "resourceId": "3001", "entryPoint": "main", "entryPointKnown": True}]
        assert fake.current_event_id == before
        assert first["meta"]["eventIdBefore"] == first["meta"]["eventIdAfter"] == before


def test_matching_event_limit_and_refresh_invalidate_all_queries(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        server = fake_server(fake, tmp_path)
        params = {"resource_id": "3001", "limit": 1}
        first = call(server, params)
        assert len(first["data"]["references"]) == 1
        assert first["data"]["coverage"]["processedCandidateCount"] == 1
        second = call(server, dict(params, cursor=first["nextCursor"]))
        assert [row["eventId"] for row in second["data"]["references"]] == [30]
        assert second["data"]["coverage"]["examinedThisPage"] == 2 and not second["nextCursor"]
        for changed in ({"resource_id": "3004"}, {"stages": ["vertex"]}, {"parent_event_id": 1}):
            with pytest.raises(ToolError, match="INVALID_REQUEST"):
                call(server, dict(params, cursor=first["nextCursor"], **changed))
        refreshed = call(server, {"resource_id": "3003", "stages": ["compute"], "refresh": True})
        assert refreshed["data"]["scanId"] != first["data"]["scanId"]
        assert [row["eventId"] for row in refreshed["data"]["references"]] == [20]
        with pytest.raises(ToolError, match="INVALID_REQUEST"):
            call(server, dict(params, cursor=first["nextCursor"]))


def test_exact_shader_resource_scope_and_stage_policy(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        # Deliberately stale states must not turn a marker or graphics action into compute matches.
        fake._actions[1]["shaders"] = {"compute": "3003"}
        fake._actions[10]["shaders"]["compute"] = "3003"
        server = fake_server(fake, tmp_path)
        result = call(server, {"resource_id": "3003", "stages": ["compute"]})
        assert [row["eventId"] for row in result["data"]["references"]] == [20]
        scoped = call(server, {"resource_id": "3001", "parent_event_id": 30})
        assert scoped["data"]["coverage"]["scopeActionCount"] == 1
        assert [row["eventId"] for row in scoped["data"]["references"]] == [30]
        for resource in ("0", "003001", "9999", "1001", "2001"):
            with pytest.raises(ToolError, match="INVALID_RESOURCE"):
                call(server, {"resource_id": resource})
        with pytest.raises(ToolError, match="INVALID_EVENT"):
            call(server, {"resource_id": "3001", "parent_event_id": 29})


def test_old_scan_cursor_cannot_cross_capture_sessions(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        server = fake_server(fake, tmp_path)
        first = call(server, {"resource_id": "3001", "scan_budget": 1})
        fake.close_capture()
        fake.open_capture()
        with pytest.raises(ToolError, match="CAPTURE_CHANGED|CAPTURE_MISMATCH"):
            call(server, {"resource_id": "3001"})
        server = fake_server(fake, tmp_path)
        with pytest.raises(ToolError, match="INVALID_REQUEST"):
            call(server, {"resource_id": "3001", "cursor": first["nextCursor"]})
        assert call(server, {"resource_id": "3001"})["data"]["scanId"] != first["data"]["scanId"]


@pytest.mark.parametrize("wrong_session", [False, True])
def test_fixed_capture_binding_is_sent_and_validated(tmp_path, wrong_session):
    directory = write_manifest(tmp_path, INSTANCE, capture_session_id=SESSION)
    requests = []

    def respond(request):
        requests.append(request)
        return json.dumps({
            "schema": "rendertest-agent-response/v1", "protocol": "rendertest-agent-ipc/v1",
            "id": request["id"], "ok": True, "result": {"data": {"references": []}},
            "meta": {"instanceId": INSTANCE, "captureSessionId": "b" * 32 if wrong_session else SESSION},
        }).encode()

    server = create_server(BridgeClient(root=tmp_path, poll_interval=0.005, environ={
        "RENDERTEST_AGENT_FIXED_INSTANCE_ID": INSTANCE, "RENDERTEST_AGENT_CAPTURE_SESSION_ID": SESSION,
    }))
    with ManualResponder(directory, respond):
        if wrong_session:
            with pytest.raises(ToolError, match="CAPTURE_CHANGED|CAPTURE_MISMATCH|INVALID_RESPONSE"):
                call(server, {"resource_id": "3001"})
        else:
            assert call(server, {"resource_id": "3001"})["captureSessionId"] == SESSION
    assert requests[0]["method"] == METHOD
    assert requests[0]["expected"] == {"instanceId": INSTANCE, "captureSessionId": SESSION}
