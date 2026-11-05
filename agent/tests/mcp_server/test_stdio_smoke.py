from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent

from fake_bridge import FakeRenderTestBridge
from rendertest_mcp.protocol import TOOL_NAMES

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "mcp_server" / "src"


def test_stdio_server_round_trip(tmp_path: Path) -> None:
    async def run(instance_id: str) -> None:
        python_path = os.pathsep.join(filter(None, [str(SOURCE_ROOT), os.environ.get("PYTHONPATH")]))
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "rendertest_mcp"],
            env={"RENDERTEST_AGENT_ROOT": str(tmp_path), "PYTHONPATH": python_path},
        )
        with anyio.fail_after(60):
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == set(TOOL_NAMES)

                    status = await session.call_tool("get_status", {})
                    assert not status.isError
                    assert status.structuredContent["instanceId"] == instance_id
                    assert status.structuredContent["data"]["capture"]["captureLoaded"] is True

                    texture = await session.call_tool(
                        "export_texture", {"resource_id": "1001", "include_image": True}
                    )
                    assert not texture.isError
                    assert any(isinstance(item, ImageContent) for item in texture.content)

                    invalid = await session.call_tool("get_cbuffer", {"stage": "pixel"})
                    assert invalid.isError

    with FakeRenderTestBridge(tmp_path) as bridge:
        anyio.run(run, bridge.instance_id)
