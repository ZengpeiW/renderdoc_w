"""Exercise the actual MCP parser before an analysis request reaches the Bridge."""

from __future__ import annotations

import asyncio
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError
import pytest

from rendertest_mcp.ipc_client import BridgeResponse
from rendertest_mcp.protocol import TOOL_NAMES
from rendertest_mcp.server import create_server


class RecordingBridge:
    def __init__(self, root: Path):
        self.root = root
        self.calls = []

    def call(self, method, params, **kwargs):
        self.calls.append((method, params, kwargs))
        return BridgeResponse("request", "test-instance", {"data": params},
                              {"captureSessionId": "a" * 32})


@pytest.fixture
def recording_server(tmp_path):
    bridge = RecordingBridge(tmp_path)
    return create_server(bridge), bridge


INTEGER_FIELDS = [
    ("get_shader_source", {"stage": "pixel"}, "event_id"),
    ("get_shader_source", {"stage": "pixel"}, "file_index"),
    ("get_shader_source", {"stage": "pixel"}, "limit"),
    ("get_action_stats", {}, "parent_event_id"),
    ("get_action_stats", {}, "top_n"),
    ("get_action_stats", {}, "limit"),
    ("get_resource_stats", {}, "top_n"),
]


@pytest.mark.parametrize("tool,base,field", INTEGER_FIELDS)
@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, "1"])
def test_analysis_integer_parameters_reject_coercion_before_bridge(
        recording_server, tool, base, field, value):
    server, bridge = recording_server
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool(tool, dict(base, **{field: value})))
    assert bridge.calls == []


@pytest.mark.parametrize("tool,base,field", INTEGER_FIELDS)
def test_analysis_integer_parameters_reject_negative_values(recording_server, tool, base, field):
    server, bridge = recording_server
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool(tool, dict(base, **{field: -1})))
    assert bridge.calls == []


@pytest.mark.parametrize("tool,base,field", [entry for entry in INTEGER_FIELDS
                                           if entry[2] in ("limit", "top_n")])
@pytest.mark.parametrize("value", [0, 1001, None])
def test_analysis_budgets_reject_out_of_range_or_null(recording_server, tool, base, field, value):
    server, bridge = recording_server
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool(tool, dict(base, **{field: value})))
    assert bridge.calls == []


@pytest.mark.parametrize("tool,args,expected", [
    ("get_shader_source", {"stage": "pixel"}, {"stage": "pixel", "limit": 100}),
    ("get_action_stats", {}, {"limit": 200, "top_n": 20}),
    ("get_resource_stats", {}, {"kind": "all", "top_n": 20}),
    ("get_shader_source", {"stage": "vertex", "event_id": 0, "file_index": 0, "limit": 1000},
     {"stage": "vertex", "event_id": 0, "file_index": 0, "limit": 1000}),
    ("get_shader_source", {"stage": "pixel", "event_id": None, "file_index": None},
     {"stage": "pixel", "limit": 100}),
    ("get_action_stats", {"parent_event_id": 0, "limit": 1, "top_n": 1000},
     {"parent_event_id": 0, "limit": 1, "top_n": 1000}),
    ("get_resource_stats", {"kind": "buffer", "top_n": 1}, {"kind": "buffer", "top_n": 1}),
])
def test_analysis_defaults_and_integer_boundaries_reach_bridge(recording_server, tool, args, expected):
    server, bridge = recording_server
    asyncio.run(server.call_tool(tool, args))
    assert len(bridge.calls) == 1
    method, params, options = bridge.calls[0]
    assert method == tool and params == expected and options["instance_id"] is None
    assert all(type(params[key]) is int for key in params
               if key in ("event_id", "file_index", "parent_event_id", "limit", "top_n"))


def test_analysis_tool_schemas_and_dynamic_whitelist_agree(recording_server):
    from rendertest_mcp.claude_backend import TOOL_NAMES as CLAUDE_TOOLS
    from rendertest_mcp.claude_events import TOOL_NAMES as CLAUDE_EVENT_TOOLS

    server, _ = recording_server
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == set(TOOL_NAMES) == set(CLAUDE_TOOLS) == set(CLAUDE_EVENT_TOOLS)
    for name, _, field in INTEGER_FIELDS:
        schema = tools[name].inputSchema["properties"][field]
        integer = next((part for part in schema.get("anyOf", []) if part.get("type") == "integer"),
                       schema)
        assert integer["type"] == "integer"
        assert integer["minimum"] == (1 if field in ("limit", "top_n") else 0)
        if field in ("limit", "top_n"):
            assert integer["maximum"] == 1000
    assert tools["get_shader_source"].annotations.readOnlyHint is False
    assert tools["get_action_stats"].annotations.readOnlyHint is True
    assert tools["get_resource_stats"].annotations.readOnlyHint is True
