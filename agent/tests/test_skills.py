"""The repository Skills must only reference tools and parameters the MCP server exposes."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from rendertest_mcp.ipc_client import BridgeClient
from rendertest_mcp.server import create_server

SKILLS_DIR = Path(__file__).resolve().parents[2] / ".agents" / "skills"
EXPECTED_SKILLS = {
    "rendertest-core",
    "rendertest-event-analysis",
    "rendertest-shader-debug",
    "rendertest-pixel-investigation",
}
FUTURE_TOOLS = {"debug_vertex", "get_mesh_data"}

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_CALL = re.compile(r"`([a-z][a-z0-9_]+)\(([^`]*)\)`")
_TOKEN = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
_KEYWORD = re.compile(r"([a-z_][a-z0-9_]*)\s*=")


def _tool_schemas() -> dict[str, set[str]]:
    server = create_server(BridgeClient(root=Path.cwd(), environ={}))
    tools = asyncio.run(server.list_tools())
    return {tool.name: set(tool.inputSchema.get("properties", {})) for tool in tools}


def _skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def test_expected_skills_exist() -> None:
    assert {path.parent.name for path in _skill_files()} == EXPECTED_SKILLS


@pytest.mark.parametrize("path", _skill_files(), ids=lambda path: path.parent.name)
def test_skill_frontmatter(path: Path) -> None:
    match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    assert match, "SKILL.md must start with YAML frontmatter"
    fields = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
    assert fields["name"].strip() == path.parent.name
    description = fields["description"].strip()
    assert 40 <= len(description) <= 1024


@pytest.mark.parametrize("path", _skill_files(), ids=lambda path: path.parent.name)
def test_skill_tool_references(path: Path) -> None:
    schemas = _tool_schemas()
    text = path.read_text(encoding="utf-8")
    known_tokens = set(schemas) | FUTURE_TOOLS | {name for props in schemas.values() for name in props}

    for tool, arguments in _CALL.findall(text):
        assert tool in schemas or tool in FUTURE_TOOLS, f"unknown tool {tool}"
        if tool in schemas:
            for keyword in _KEYWORD.findall(arguments):
                assert keyword in schemas[tool], f"{tool} has no parameter {keyword}"

    for token in _TOKEN.findall(text):
        if token.startswith(("get_", "list_", "find_", "set_", "export_", "pick_", "inspect_",
                             "debug_", "pixel_")):
            assert token in known_tokens, f"unknown tool or parameter {token}"


def test_future_tools_are_flagged_as_unavailable() -> None:
    schemas = _tool_schemas()
    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        for tool in FUTURE_TOOLS - set(schemas):
            if f"`{tool}" in text:
                assert "M4" in text, f"{path.parent.name} mentions {tool} without saying it is M4"
