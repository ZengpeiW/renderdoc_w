"""Codex end-to-end acceptance for the RenderTest agent (M3).

Usage:
    python devtools/codex_e2e.py --launch FILE.rdc [--exe qrendertest.exe] [--codex codex]
    python devtools/codex_e2e.py --instance-id ID        # use an already running qrendertest

Picks a draw with a colour target, moves the UI there with ``set_event`` and derives a pixel and
its last-changing write event from the bridge. It then asks ``codex exec`` the three acceptance
questions, with the rendertest MCP server injected through ``-c`` overrides, and checks the JSONL
event stream: rendertest tools were called, ``set_event`` was not, the UI event did not move and
the answer cites the real EID / ResourceId. Transcripts and answers are written to
``<root>/e2e/<timestamp>/`` for manual review of fact-vs-inference wording.

This sends capture data (names, values, paths) to the model provider configured for Codex.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = AGENT_DIR.parent
SOURCE_DIR = AGENT_DIR / "mcp_server" / "src"
for path in (SOURCE_DIR, AGENT_DIR / "devtools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from codex_config import cli_overrides, server_config  # noqa: E402
from rendertest_mcp.ipc_client import BridgeClient  # noqa: E402

SERVER = "rendertest"
MAX_PIXEL_PICKS = 64
WARNING_PREFIXES = ("Codex is ignoring", "Ignoring unrecognized", "warning:")


@dataclass
class Question:
    key: str
    prompt: str
    expected_tools: tuple[frozenset[str], ...]
    expected_mentions: tuple[str, ...]


@dataclass
class Transcript:
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rendertest_tools(self) -> list[str]:
        return [call["tool"] for call in self.tool_calls if call.get("server") == SERVER]

    @property
    def answer(self) -> str:
        return self.messages[-1] if self.messages else ""


def parse_events(lines: Iterable[str]) -> Transcript:
    """Collect MCP tool calls, shell commands and agent messages from ``codex exec --json``."""
    transcript = Transcript()
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type", "")
        if kind == "error" or kind == "turn.failed":
            transcript.errors.append(json.dumps(event, ensure_ascii=False))
            continue
        if kind != "item.completed":
            continue
        item = event.get("item") or {}
        item_type = item.get("type") or item.get("item_type") or ""
        if item_type == "mcp_tool_call":
            transcript.tool_calls.append({
                "server": item.get("server"),
                "tool": item.get("tool"),
                "arguments": item.get("arguments"),
                "status": item.get("status"),
                "error": item.get("error"),
            })
        elif item_type == "command_execution":
            transcript.commands.append(str(item.get("command", "")))
        elif item_type in ("agent_message", "assistant_message"):
            transcript.messages.append(str(item.get("text", "")))
        elif item_type == "error":
            message = str(item.get("message", ""))
            target = transcript.warnings if message.startswith(WARNING_PREFIXES) else transcript.errors
            if message not in target:
                target.append(message)
    return transcript


def evaluate(question: Question, transcript: Transcript, ui_before: Any, ui_after: Any) -> list[str]:
    failures = []
    tools = set(transcript.rendertest_tools)
    if not tools:
        failures.append("no rendertest MCP tool was called")
    for group in question.expected_tools:
        if not tools & group:
            failures.append(f"none of {sorted(group)} was called (called {sorted(tools)})")
    if "set_event" in tools:
        failures.append("set_event was called for a read-only question")
    if ui_before != ui_after:
        failures.append(f"UI event moved: {ui_before} -> {ui_after}")
    if not transcript.answer:
        failures.append("no final answer")
    for mention in question.expected_mentions:
        if mention not in transcript.answer:
            failures.append(f"answer does not mention {mention}")
    failed_calls = [call for call in transcript.tool_calls if call.get("status") == "failed"]
    if failed_calls and len(failed_calls) == len(transcript.tool_calls):
        failures.append("every MCP tool call failed")
    return failures


class Probe:
    """Bridge-side ground truth for the questions."""

    def __init__(self, client: BridgeClient, instance_id: str):
        self.client = client
        self.instance_id = instance_id

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        response = self.client.call(method, params, instance_id=self.instance_id, timeout=120)
        return response.result

    def ui(self) -> tuple[Any, Any]:
        data = self.call("get_context")["data"]
        return data.get("selectedEventId"), data.get("currentEventId")

    def pick_draw(self, max_draws: int = 40) -> tuple[int, dict[str, Any]]:
        draws = self.call("find_actions", query="", flags=["draw"], limit=1000)["data"]["actions"]
        step = max(1, len(draws) // max_draws)
        for action in draws[::step][:max_draws]:
            data = self.call("inspect_event", event_id=action["eventId"])["data"]
            if data["pipeline"].get("outputTargets") and "pixel" in data["pipeline"]["shaders"]:
                return action["eventId"], data
        raise RuntimeError("no draw with a colour target and a pixel shader")

    def value(self, resource_id: str, x: int, y: int, event_id: int) -> list[Any]:
        return self.call("pick_pixel", resource_id=resource_id, x=x, y=y,
                         event_id=event_id)["data"]["value"]

    def pick_pixel_location(self, resource_id: str, width: int, height: int, event_id: int,
                            writes: list[int]) -> tuple[int, int]:
        """Prefer a pixel that this draw actually changed, so question 2 has a clear answer."""
        previous = max((eid for eid in writes if eid < event_id), default=None)
        for fy in (0.5, 0.25, 0.75):
            for fx in (0.5, 0.25, 0.75):
                x, y = int(width * fx), int(height * fy)
                after = self.value(resource_id, x, y, event_id)
                if previous is None or after != self.value(resource_id, x, y, previous):
                    return x, y
        return width // 2, height // 2

    def writes(self, resource_id: str) -> list[int]:
        events, cursor = set(), None
        while True:
            params: dict[str, Any] = {"resource_id": resource_id, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = self.call("get_resource_usage", **params)
            events.update(item["eventId"] for item in result["data"]["usages"]
                          if item["access"] in ("write", "readWrite"))
            cursor = result.get("nextCursor")
            if not cursor:
                return sorted(events)

    def last_change(self, resource_id: str, x: int, y: int, event_id: int,
                    writes: list[int]) -> int | None:
        candidates = [eid for eid in writes if eid <= event_id][-MAX_PIXEL_PICKS:]
        values = [self.value(resource_id, x, y, eid) for eid in candidates]
        for position in range(len(candidates) - 1, 0, -1):
            if values[position] != values[position - 1]:
                return candidates[position]
        return candidates[0] if candidates else None


def run_codex(codex: str, overrides: list[str], prompt: str, output: Path,
              timeout: float) -> Transcript:
    command = [codex, "exec", "--json", "--ephemeral", "-s", "read-only",
               "-c", 'approval_policy="never"', "-C", str(REPO_DIR), *overrides,
               "-o", str(output.with_suffix(".answer.md")), "-"]
    process = subprocess.run(command, input=prompt.encode("utf-8"), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=timeout)
    output.write_bytes(process.stdout)
    output.with_suffix(".stderr.txt").write_bytes(process.stderr)
    transcript = parse_events(process.stdout.decode("utf-8", "replace").splitlines())
    answer_path = output.with_suffix(".answer.md")
    if answer_path.exists() and not transcript.messages:
        transcript.messages.append(answer_path.read_text(encoding="utf-8"))
    if process.returncode != 0:
        transcript.errors.append(f"codex exec exited with {process.returncode}")
    return transcript


def build_questions(event_id: int, target: dict[str, Any], x: int, y: int,
                    writer: int | None) -> list[Question]:
    rt = target["resourceId"]
    return [
        Question(
            "q1-explain-event",
            "RenderTest 里打开着一个 capture。解释当前 EID 在做什么，输入和输出是什么？",
            (frozenset({"get_context", "inspect_event", "get_pipeline_state"}),),
            (str(event_id),),
        ),
        Question(
            "q2-pixel-writer",
            f"RenderTest 里打开着一个 capture。在当前 EID 的第一个颜色 RT 的 x={x}, y={y} 位置，"
            "截至当前 EID，最后是哪个 Draw 改变了这个像素？",
            (frozenset({"pick_pixel"}), frozenset({"get_resource_usage", "pixel_history"})),
            tuple(item for item in (rt, str(writer) if writer else None) if item),
        ),
        Question(
            "q3-shader-cbuffer",
            "RenderTest 里打开着一个 capture。检查当前 Draw 的 Pixel Shader 和 CBuffer，"
            "找出可能影响颜色的参数。",
            (frozenset({"get_shader"}), frozenset({"get_cbuffer"})),
            (str(event_id),),
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex end-to-end acceptance for RenderTest.")
    parser.add_argument("--launch", type=Path)
    parser.add_argument("--exe", type=Path, default=REPO_DIR / "x64" / "Release" / "qrendertest.exe")
    parser.add_argument("--instance-id")
    parser.add_argument("--root", type=Path, default=AGENT_DIR / ".agent-data" / "e2e")
    parser.add_argument("--codex", default=shutil.which("codex") or "codex")
    parser.add_argument("--only", action="append", help="Run only questions whose key starts with this.")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute the ground truth and questions without calling Codex.")
    args = parser.parse_args(argv)
    if not args.launch and not args.instance_id:
        parser.error("pass --launch FILE.rdc or --instance-id ID")

    from smoke_live import close_windows  # noqa: E402

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    process = None
    if args.launch:
        environ = dict(os.environ, RENDERTEST_AGENT_ROOT=str(root))
        environ.pop("RENDERTEST_AGENT_BRIDGE", None)
        process = subprocess.Popen([str(args.exe), str(args.launch.resolve())], env=environ)
        print(f"launched pid {process.pid}")

    client = BridgeClient(root=root)
    try:
        instance_id = args.instance_id
        deadline = time.monotonic() + 300
        while instance_id is None and time.monotonic() < deadline:
            for item in client.list_instances():
                if item.get("pid") == process.pid and item.get("captureLoaded"):
                    instance_id = item["instanceId"]
            time.sleep(0.5)
        if instance_id is None:
            print("qrendertest did not come online with a capture", file=sys.stderr)
            return 1

        probe = Probe(client, instance_id)
        event_id, inspected = probe.pick_draw()
        target = inspected["pipeline"]["outputTargets"][0]
        probe.call("set_event", event_id=event_id)
        writes = probe.writes(target["resourceId"])
        x, y = probe.pick_pixel_location(target["resourceId"], int(target["width"]),
                                         int(target["height"]), event_id, writes)
        writer = probe.last_change(target["resourceId"], x, y, event_id, writes)
        truth = {"instanceId": instance_id, "eventId": event_id,
                 "action": inspected["action"]["name"], "renderTarget": target,
                 "pixel": [x, y], "pixelValue": probe.value(target["resourceId"], x, y, event_id),
                 "lastChangingWrite": writer, "writeEvents": len(writes)}
        print("ground truth:", json.dumps(truth, ensure_ascii=False))

        run_dir = root / time.strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True)
        (run_dir / "ground_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
        overrides = cli_overrides(server_config(root=root)) + [
            "-c", f'mcp_servers.{SERVER}.env.RENDERTEST_AGENT_INSTANCE_ID="{instance_id}"']
        if args.dry_run:
            for question in build_questions(event_id, target, x, y, writer):
                print(f"[{question.key}] {question.prompt} -> expects {question.expected_mentions}")
            print("codex overrides:", json.dumps(overrides, ensure_ascii=False))
            return 0

        all_failures = 0
        for question in build_questions(event_id, target, x, y, writer):
            if args.only and not any(question.key.startswith(prefix) for prefix in args.only):
                continue
            print(f"[{question.key}] {question.prompt}")
            ui_before = probe.ui()
            started = time.monotonic()
            transcript = run_codex(args.codex, overrides, question.prompt,
                                   run_dir / f"{question.key}.jsonl", args.timeout)
            ui_after = probe.ui()
            failures = evaluate(question, transcript, ui_before, ui_after) + transcript.errors
            print(f"  {time.monotonic() - started:.0f}s; tools: {transcript.rendertest_tools}")
            for warning in transcript.warnings:
                print("  warning:", warning.splitlines()[0])
            if transcript.commands:
                print(f"  shell commands: {len(transcript.commands)}")
            for failure in failures:
                print("  FAIL:", failure, file=sys.stderr)
            print("  PASS" if not failures else f"  {len(failures)} check(s) failed")
            all_failures += len(failures)
        print(f"transcripts: {run_dir}")
        print("PASS" if not all_failures else f"{all_failures} check(s) failed")
        return 0 if not all_failures else 1
    finally:
        if process is not None:
            close_windows(process.pid)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
