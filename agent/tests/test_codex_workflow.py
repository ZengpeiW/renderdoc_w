from __future__ import annotations

import json
import tomllib
from pathlib import Path

from codex_config import cli_overrides, server_config, toml_snippet
from codex_e2e import Question, Transcript, build_questions, evaluate, parse_events


def test_toml_snippet_round_trips(tmp_path: Path) -> None:
    config = server_config(Path("C:/venv/python.exe"), tmp_path)
    parsed = tomllib.loads(toml_snippet(config))["mcp_servers"]["rendertest"]
    assert parsed["args"] == ["-m", "rendertest_mcp"]
    assert parsed["command"].endswith("python.exe")
    assert parsed["env"]["RENDERTEST_AGENT_ROOT"] == tmp_path.resolve().as_posix()


def test_cli_overrides_are_valid_toml_values(tmp_path: Path) -> None:
    overrides = cli_overrides(server_config(Path("C:/venv/python.exe"), tmp_path))
    assert overrides[0::2] == ["-c"] * (len(overrides) // 2)
    for override in overrides[1::2]:
        key, value = override.split("=", 1)
        assert key.startswith("mcp_servers.rendertest.")
        tomllib.loads(f"value = {value}")


def _event(item: dict) -> str:
    return json.dumps({"type": "item.completed", "item": item})


def test_parse_events_collects_tools_commands_and_answer() -> None:
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        _event({"id": "1", "type": "mcp_tool_call", "server": "rendertest", "tool": "get_context",
                "status": "completed"}),
        _event({"id": "2", "type": "mcp_tool_call", "server": "other", "tool": "search",
                "status": "completed"}),
        _event({"id": "3", "type": "command_execution", "command": "rg foo"}),
        _event({"id": "4", "type": "agent_message", "text": "EID 1513 draws"}),
        _event({"id": "5", "type": "error", "message": "Codex is ignoring 1 unrecognized setting"}),
        _event({"id": "6", "type": "error", "message": "Codex is ignoring 1 unrecognized setting"}),
        _event({"id": "7", "type": "error", "message": "MCP server failed to start"}),
        "not json",
    ]
    transcript = parse_events(lines)
    assert transcript.rendertest_tools == ["get_context"]
    assert transcript.commands == ["rg foo"]
    assert transcript.answer == "EID 1513 draws"
    assert transcript.warnings == ["Codex is ignoring 1 unrecognized setting"]
    assert transcript.errors == ["MCP server failed to start"]


def test_evaluate_flags_ui_changes_and_missing_evidence() -> None:
    question = Question("q", "?", (frozenset({"inspect_event"}),), ("1513",))
    good = Transcript(tool_calls=[{"server": "rendertest", "tool": "inspect_event",
                                   "status": "completed"}], messages=["EID 1513 ..."])
    assert evaluate(question, good, (1513, 1513), (1513, 1513)) == []

    bad = Transcript(tool_calls=[{"server": "rendertest", "tool": "set_event",
                                  "status": "completed"}], messages=["it draws"])
    failures = evaluate(question, bad, (1513, 1513), (3, 3))
    assert any("inspect_event" in failure for failure in failures)
    assert any("set_event" in failure for failure in failures)
    assert any("UI event moved" in failure for failure in failures)
    assert any("1513" in failure for failure in failures)


def test_questions_cite_ground_truth() -> None:
    target = {"resourceId": "116618", "width": 512, "height": 512}
    questions = build_questions(1513, target, 10, 20, 1400)
    assert [question.key[:2] for question in questions] == ["q1", "q2", "q3"]
    assert "x=10, y=20" in questions[1].prompt
    assert questions[1].expected_mentions == ("116618", "1400")
