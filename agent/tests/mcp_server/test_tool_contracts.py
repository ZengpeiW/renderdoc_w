from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import ImageContent, TextContent

from fake_bridge import FakeRenderTestBridge
from rendertest_mcp.ipc_client import BridgeClient
from rendertest_mcp.protocol import TOOL_NAMES
from rendertest_mcp.server import create_server


def call_tool(server: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    return asyncio.run(server.call_tool(name, arguments or {}))


def structured(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple):
        return result[1]
    if isinstance(result, dict):
        return result
    text = next(item for item in result if isinstance(item, TextContent))
    return json.loads(text.text)


def test_server_exposes_exact_p0_tools() -> None:
    server = create_server(BridgeClient(root=Path.cwd(), environ={}))
    tools = asyncio.run(server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == set(TOOL_NAMES)
    assert by_name["set_event"].inputSchema["required"] == ["event_id"]
    assert by_name["get_resource"].inputSchema["properties"]["resource_id"]["pattern"] == r"^[0-9]{1,20}$"


def test_tool_annotations_follow_effects() -> None:
    server = create_server(BridgeClient(root=Path.cwd(), environ={}))
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    read_only = {name for name, tool in tools.items() if tool.annotations.readOnlyHint}
    assert read_only == set(TOOL_NAMES) - {"set_event", "export_texture"}
    for tool in tools.values():
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False


def test_all_p0_tools_work_against_fake_bridge(tmp_path: Path) -> None:
    with FakeRenderTestBridge(tmp_path) as bridge:
        server = create_server(BridgeClient(root=tmp_path, environ={}, poll_interval=0.005))
        calls = {
            "list_instances": {},
            "get_status": {},
            "get_context": {},
            "list_actions": {"parent_event_id": 1, "limit": 2},
            "find_actions": {"query": "draw", "flags": ["draw"]},
            "inspect_event": {"event_id": 30},
            "set_event": {"event_id": 20},
            "get_pipeline_state": {"event_id": 20},
            "get_shader": {"event_id": 30, "stage": "pixel", "include_disassembly": True},
            "get_cbuffer": {"event_id": 30, "stage": "pixel", "index": 0},
            "list_resources": {"kind": "texture"},
            "get_resource": {"resource_id": "1001"},
            "get_resource_usage": {"resource_id": "1001"},
            "export_texture": {"resource_id": "1001", "event_id": 30},
            "pick_pixel": {"resource_id": "1001", "x": 1, "y": 2},
            "get_texture_stats": {"resource_id": "1001"},
            "pixel_history": {"resource_id": "1001", "x": 1, "y": 2, "event_id": 30},
            "get_buffer_data": {"resource_id": "2001", "offset": 4, "size": 4},
            "get_debug_messages": {},
        }
        results = {name: structured(call_tool(server, name, arguments)) for name, arguments in calls.items()}

    assert set(results) == set(TOOL_NAMES)
    assert results["list_instances"]["data"]["instances"][0]["instanceId"] == bridge.instance_id
    assert results["list_actions"]["nextCursor"] == "2"
    assert results["set_event"]["effect"] == "ui"
    assert results["set_event"]["data"]["eventIdAfter"] == 20
    assert results["get_buffer_data"]["data"]["hex"] == "04050607"
    assert results["pixel_history"]["data"]["summary"]["lastPassedEventId"] == 10
    artifact = results["export_texture"]["artifacts"][0]
    assert Path(artifact["path"]).read_bytes().startswith(b"\x89PNG")


def test_export_texture_can_attach_image(tmp_path: Path) -> None:
    with FakeRenderTestBridge(tmp_path):
        server = create_server(BridgeClient(root=tmp_path, environ={}, poll_interval=0.005))
        result = call_tool(
            server,
            "export_texture",
            {"resource_id": "1001", "include_image": True},
        )

    assert any(isinstance(item, ImageContent) for item in result)


def test_cbuffer_requires_exactly_one_selector() -> None:
    server = create_server(BridgeClient(root=Path.cwd(), environ={}))

    with pytest.raises(Exception) as error:
        call_tool(server, "get_cbuffer", {"stage": "pixel"})

    assert "INVALID_REQUEST" in str(error.value)
