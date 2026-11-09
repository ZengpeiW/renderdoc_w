"""Pass-resource selectors, incremental resource pages and capture-local usage evidence."""

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

METHOD = "get_pass_resources"
INSTANCE = "pass-resource-test"
SESSION = "a" * 32
DEFAULTS = {"kind": "all", "limit": 200, "scan_budget": 200, "refresh": False}


class RecordingBridge:
    def __init__(self, root):
        self.root, self.calls, self.response, self.error = root, [], None, None

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


def call(server, params=None):
    return asyncio.run(server.call_tool(METHOD, params or {}))[1]


def fake_server(fake, root):
    return create_server(BridgeClient(root=root, poll_interval=0.005, environ={
        "RENDERTEST_AGENT_FIXED_INSTANCE_ID": fake.instance_id,
        "RENDERTEST_AGENT_CAPTURE_SESSION_ID": fake.capture_session_id,
    }))


@pytest.mark.parametrize("params,expected", [
    ({}, DEFAULTS),
    ({"parent_event_id": None, "start_event_id": None, "end_event_id": None, "cursor": None}, DEFAULTS),
    ({"parent_event_id": 0}, dict(DEFAULTS, parent_event_id=0)),
    ({"start_event_id": 0, "end_event_id": 2**32 - 1, "kind": "texture"},
     dict(DEFAULTS, start_event_id=0, end_event_id=2**32 - 1, kind="texture")),
    ({"parent_event_id": 2**32 - 1, "kind": "buffer", "limit": 1000, "scan_budget": 2000, "refresh": True},
     {"parent_event_id": 2**32 - 1, "kind": "buffer", "limit": 1000, "scan_budget": 2000, "refresh": True}),
    ({"cursor": "null", "limit": 1, "scan_budget": 1}, dict(DEFAULTS, cursor="null", limit=1, scan_budget=1)),
    ({"cursor": "[1]"}, dict(DEFAULTS, cursor="[1]")),
])
def test_exact_selectors_defaults_and_long_timeout(recording, params, expected):
    server, bridge = recording
    result = call(server, dict(params, instance_id="chosen-window"))
    assert bridge.calls == [(METHOD, expected, {"instance_id": "chosen-window", "timeout": LONG_TIMEOUT_SECONDS})]
    assert result["effect"] == "read" and result["captureSessionId"] == SESSION


@pytest.mark.parametrize("field", ["parent_event_id", "start_event_id", "end_event_id"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1", "null", "[1]", -1, 2**32, [], {}])
def test_all_eid_selectors_are_strict_and_not_json_preparsed(recording, field, value):
    server, bridge = recording
    params = {"start_event_id": 0, "end_event_id": 2} if field != "parent_event_id" else {}
    with pytest.raises(ToolError):
        call(server, dict(params, **{field: value}))
    assert bridge.calls == []


@pytest.mark.parametrize("params", [
    {"parent_event_id": 1, "start_event_id": 10},
    {"parent_event_id": 1, "end_event_id": 20},
    {"parent_event_id": 1, "start_event_id": 10, "end_event_id": 20},
    {"start_event_id": 10}, {"end_event_id": 20},
    {"start_event_id": 20, "end_event_id": 10},
    {"refresh": True, "cursor": "next"}, {"refresh": True, "cursor": "null"},
    {"refresh": True, "cursor": ""},
])
def test_invalid_scope_combinations_rejected_before_bridge(recording, params):
    server, bridge = recording
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        call(server, params)
    assert bridge.calls == []


@pytest.mark.parametrize("field,value", [
    ("kind", "shader"), ("kind", "Texture"), ("kind", None), ("kind", True), ("kind", []),
    ("limit", True), ("limit", 1.0), ("limit", "1"), ("limit", None), ("limit", 0), ("limit", 1001),
    ("scan_budget", False), ("scan_budget", 1.0), ("scan_budget", "1"), ("scan_budget", None),
    ("scan_budget", 0), ("scan_budget", 2001), ("refresh", 1), ("refresh", "true"),
    ("refresh", "false"), ("refresh", None), ("cursor", 1), ("cursor", []), ("cursor", {}),
    ("cursor", "x" * 1025), ("instance_id", "../foreign"),
])
def test_budgets_and_filters_reject_coercion(recording, field, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, {field: value})
    assert bridge.calls == []


def test_registry_schema_read_policy_and_dynamic_claude_allowlist(recording):
    from rendertest_mcp.claude_backend import TOOL_NAMES as CLAUDE_TOOLS
    from rendertest_mcp.claude_events import TOOL_NAMES as CLAUDE_EVENT_TOOLS

    server, _ = recording
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert len(tools) == 35 and set(tools) == set(TOOL_NAMES) == set(CLAUDE_TOOLS) == set(CLAUDE_EVENT_TOOLS)
    assert METHOD in HANDLERS
    assert METHOD_SPECS[METHOD] == BRIDGE_SPECS[METHOD] == {"effect": "read", "requiresCapture": True}
    tool = tools[METHOD]
    assert tool.annotations.readOnlyHint and tool.annotations.idempotentHint
    assert not tool.annotations.destructiveHint and not tool.annotations.openWorldHint
    assert not tool.inputSchema.get("required")
    props = tool.inputSchema["properties"]
    assert props["kind"]["enum"] == ["all", "texture", "buffer"]
    for name in ("parent_event_id", "start_event_id", "end_event_id"):
        integer = next(item for item in props[name]["anyOf"] if item["type"] == "integer")
        assert (integer["minimum"], integer["maximum"]) == (0, 2**32 - 1)
    assert (props["limit"]["maximum"], props["scan_budget"]["maximum"]) == (1000, 2000)
    assert "empty resources page" in tool.description and "cache hits" in tool.description
    assert "not proven producers/consumers" in tool.description and "without SetFrameEvent" in tool.description


@pytest.mark.parametrize("code", ["INVALID_EVENT", "INVALID_REQUEST", "REPLAY_ERROR", "CAPTURE_CHANGED",
                                  "CANCELLED", "TIMEOUT", "LIMIT_EXCEEDED", "UNSUPPORTED"])
def test_failures_do_not_become_complete_empty_results(recording, code):
    server, bridge = recording
    bridge.error = RenderTestAgentError(code, "Cannot inspect resource usage.", details={"stage": "native-read"})
    with pytest.raises(ToolError, match=code):
        call(server)


def test_usage_evidence_incomplete_diagnostics_and_examples_are_preserved(recording):
    server, bridge = recording
    data = {"resources": [{"resourceId": "18446744073709551615", "hasReadUsage": False,
             "hasWriteUsage": False, "usageExamples": [{"eventId": 29, "actionEventId": 30,
             "usage": "FutureUsage", "usageValue": 999, "access": "unknown", "viewId": "123"}],
             "previousUsage": {"eventId": 10, "recordCount": 1, "usages": [], "truncated": True}}],
            "coverage": {"scanComplete": True, "complete": False, "unknownUsageRecordCount": 1}}
    bridge.response = {"data": data, "truncated": True, "warnings": ["Unknown recorded usage."]}
    result = call(server)
    assert result["data"] == data and result["truncated"] and result["nextCursor"] is None
    assert result["warnings"] == ["Unknown recorded usage."]


def test_whole_capture_default_numeric_order_and_cross_scope_kind_cache(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        server = fake_server(fake, tmp_path)
        fake.current_event_id = 30
        result = call(server)
        assert result["data"]["scope"]["kind"] == "capture"
        assert [row["resourceId"] for row in result["data"]["resources"]] == ["1001", "1002", "1003", "2001"]
        assert result["data"]["coverage"]["complete"] and result["data"]["coverage"]["nativeReadsThisPage"] == 4
        buffer = call(server, {"parent_event_id": 10, "kind": "buffer", "scan_budget": 1})
        assert [row["resourceId"] for row in buffer["data"]["resources"]] == ["2001"]
        assert buffer["data"]["scanId"] == result["data"]["scanId"]
        assert buffer["data"]["coverage"]["cacheHitsThisPage"] == buffer["data"]["coverage"]["examinedThisPage"] == 1
        assert buffer["data"]["coverage"]["nativeReadsThisPage"] == 0
        assert fake.current_event_id == 30 and fake.selected_event_id == 10
        assert "replayEventId" not in result["meta"]


def test_empty_page_cursor_budget_and_matching_resource_limit(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        fake._usage["1001"] = []
        server = fake_server(fake, tmp_path)
        params = {"start_event_id": 30, "end_event_id": 30, "scan_budget": 1}
        first = call(server, params)
        assert first["data"]["resources"] == [] and first["nextCursor"] and first["truncated"]
        assert first["data"]["coverage"]["processedResourceCount"] == 1
        second = call(server, dict(params, cursor=first["nextCursor"], scan_budget=2000, limit=1))
        assert [row["resourceId"] for row in second["data"]["resources"]] == ["1003"]
        assert second["data"]["coverage"]["examinedThisPage"] == 2 and second["nextCursor"]
        last = call(server, dict(params, cursor=second["nextCursor"], scan_budget=2, limit=1000))
        assert last["data"]["resources"] == [] and last["data"]["coverage"]["complete"]
        assert not last["nextCursor"]


def test_parent_clips_setup_and_range_accepts_exact_api_eid_not_fake_marker(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        fake._usage["1002"].append({"eventId": 29, "usage": "PS_Resource"})
        server = fake_server(fake, tmp_path)
        parent = call(server, {"parent_event_id": 30})
        assert "1002" not in [row["resourceId"] for row in parent["data"]["resources"]]
        ranged = call(server, {"start_event_id": 29, "end_event_id": 29})
        assert [row["resourceId"] for row in ranged["data"]["resources"]] == ["1002"]
        assert ranged["data"]["resources"][0]["usageExamples"][0]["actionEventId"] == 30
        assert ranged["data"]["scope"]["actionCount"] == 0
        assert call(server, {"parent_event_id": 1})["data"]["scope"]["syntheticParent"]
        for params in ({"start_event_id": 1, "end_event_id": 10}, {"start_event_id": 28, "end_event_id": 30},
                       {"parent_event_id": 29}):
            with pytest.raises(ToolError, match="INVALID_EVENT"):
                call(server, params)


def test_synthetic_marker_without_real_anchor_is_not_a_guessed_pass(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        fake._actions[40] = {"eventId": 40, "name": "Empty marker", "flags": ["marker"],
                             "parentEventId": None, "children": []}
        with pytest.raises(ToolError, match="UNSUPPORTED"):
            call(fake_server(fake, tmp_path), {"parent_event_id": 40})


def test_previous_next_group_all_roles_at_nearest_eid_and_potential_access(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        fake._usage["1001"].extend([{"eventId": 10, "usage": "Barrier"},
                                    {"eventId": 20, "usage": "Discard"}])
        server = fake_server(fake, tmp_path)
        row = call(server, {"start_event_id": 20, "end_event_id": 20})["data"]["resources"][0]
        assert row["hasReadUsage"] and row["hasWriteUsage"]
        assert {item["access"] for item in row["usageTypes"]} == {"readWrite", "discard"}
        assert row["previousUsage"]["eventId"] == 10 and row["previousUsage"]["recordCount"] == 2
        assert {item["usage"] for item in row["previousUsage"]["usages"]} == {"ColorTarget", "Barrier"}
        assert row["nextUsage"]["eventId"] == 30


def test_diagnostics_accumulate_prefix_and_unknown_is_not_read(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        fake._usage["1001"] = [{"eventId": 10, "usage": "FutureUsage", "usageValue": 999},
                                {"eventId": 0, "usage": "Unused"}]
        fake._usage["1002"] = [{"eventId": 28, "usage": "Barrier"}]
        server = fake_server(fake, tmp_path)
        first = call(server, {"scan_budget": 1})
        row = first["data"]["resources"][0]
        assert not row["hasReadUsage"] and not row["hasWriteUsage"]
        assert first["data"]["coverage"]["unknownUsageRecordCount"] == 1
        last = call(server, {"cursor": first["nextCursor"]})
        coverage = last["data"]["coverage"]
        assert coverage["scanComplete"] and not coverage["complete"] and not last["nextCursor"]
        assert coverage["unknownUsageRecordCount"] == coverage["unmappedUsageRecordCount"] == coverage["unusedSentinelCount"] == 1


def test_refresh_and_query_identity_invalidate_cursors(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        server = fake_server(fake, tmp_path)
        first = call(server, {"scan_budget": 1})
        for scope in ({"parent_event_id": 1}, {"start_event_id": 10, "end_event_id": 30}, {"kind": "texture"}):
            with pytest.raises(ToolError, match="INVALID_REQUEST"):
                call(server, dict(scope, cursor=first["nextCursor"]))
        refreshed = call(server, {"refresh": True, "kind": "buffer"})
        assert refreshed["data"]["scanId"] != first["data"]["scanId"]
        with pytest.raises(ToolError, match="INVALID_REQUEST"):
            call(server, {"cursor": first["nextCursor"]})


def test_failed_refresh_or_mixed_page_preserves_old_cache_and_cursor(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        server = fake_server(fake, tmp_path)
        first = call(server, {"scan_budget": 1})
        snapshot = json.dumps(fake._pass_resource_scan, sort_keys=True)
        original = fake._usage["1003"]
        fake._usage["1003"] = [{"eventId": "30", "usage": "ColorTarget"}]
        for params in ({"refresh": True}, {}):
            with pytest.raises(ToolError, match="REPLAY_ERROR"):
                call(server, params)
            assert json.dumps(fake._pass_resource_scan, sort_keys=True) == snapshot
        fake._usage["1003"] = original
        result = call(server, {"cursor": first["nextCursor"]})
        assert result["data"]["scanId"] == first["data"]["scanId"] and result["data"]["coverage"]["complete"]


def test_capture_binding_and_old_cursor_rejected_after_capture_change(tmp_path):
    with FakeRenderTestBridge(tmp_path) as fake:
        server = fake_server(fake, tmp_path)
        first = call(server, {"scan_budget": 1})
        fake.close_capture()
        fake.open_capture()
        with pytest.raises(ToolError, match="CAPTURE_CHANGED|CAPTURE_MISMATCH"):
            call(server)
        server = fake_server(fake, tmp_path)
        with pytest.raises(ToolError, match="INVALID_REQUEST"):
            call(server, {"cursor": first["nextCursor"]})
        assert call(server)["data"]["scanId"] != first["data"]["scanId"]


@pytest.mark.parametrize("wrong_session", [False, True])
def test_fixed_capture_scope_is_sent_and_response_identity_verified(tmp_path, wrong_session):
    directory = write_manifest(tmp_path, INSTANCE, capture_session_id=SESSION)
    requests = []
    def respond(request):
        requests.append(request)
        return json.dumps({"schema": "rendertest-agent-response/v1", "protocol": "rendertest-agent-ipc/v1",
            "id": request["id"], "ok": True, "result": {"data": {"resources": []}},
            "meta": {"instanceId": INSTANCE, "captureSessionId": "b" * 32 if wrong_session else SESSION}}).encode()
    server = create_server(BridgeClient(root=tmp_path, poll_interval=0.005, environ={
        "RENDERTEST_AGENT_FIXED_INSTANCE_ID": INSTANCE, "RENDERTEST_AGENT_CAPTURE_SESSION_ID": SESSION}))
    with ManualResponder(directory, respond):
        if wrong_session:
            with pytest.raises(ToolError, match="CAPTURE_CHANGED|CAPTURE_MISMATCH|INVALID_RESPONSE"):
                call(server)
        else:
            assert call(server)["captureSessionId"] == SESSION
    assert requests[0]["method"] == METHOD
    assert requests[0]["expected"] == {"instanceId": INSTANCE, "captureSessionId": SESSION}
