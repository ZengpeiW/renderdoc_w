"""Check live Claude inference, scoped RenderTest tools, and cross-process native resume.

This uses the caller's existing Claude credentials and endpoint, and makes two real model
turns. It never opens a GUI or writes the panel's conversations directory. Each turn requests
only get_context and inspect_event. Its UUID session ledger and native Claude history are
retained for inspection; only processes created by this check are stopped. An independent
report records the prompts, answers, tool evidence, and session id (never credentials).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

from claude_backend_check import Backend as SupervisedBackend, supervise_process

REPO = Path(__file__).resolve().parents[2]
POLICY = {"toolPolicy": "rendertest-only", "builtinTools": False,
          "externalMcp": False, "approvals": "deny"}
TOOLS = {"get_context", "inspect_event"}


def safe_text(value: Any) -> str:
    text = str(value)
    for key, secret in os.environ.items():
        if re.search(r"TOKEN|SECRET|PASSWORD|API.?KEY", key, re.I) and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token)\s*[=:]\s*)[^\s,;]+",
                  r"\1[redacted]", text)


def validate_scope(args: argparse.Namespace) -> dict[str, Any]:
    """Read only the explicitly selected manifest; never choose a latest instance."""
    path = args.agent_root / "ipc-v1" / "instances" / args.instance / "instance.json"
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("The explicitly selected bridge manifest is missing or too large.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("instanceId") != args.instance or
            manifest.get("captureSessionId") != args.capture_session or
            manifest.get("captureLoaded") is not True or manifest.get("stopped") is True):
        raise ValueError("The selected instance no longer has the requested capture session.")
    heartbeat = manifest.get("heartbeatUnixMs")
    if type(heartbeat) is not int or abs(time.time() * 1000 - heartbeat) > 5000:
        raise ValueError("The selected bridge heartbeat is stale.")
    capture = Path(str(manifest.get("capturePath", "")))
    if not capture.is_absolute() or str(capture).startswith("\\\\") or not capture.is_file():
        raise ValueError("Native resume requires a saved, locally accessible capture file.")
    return {key: manifest.get(key) for key in
            ("instanceId", "captureSessionId", "capturePath", "currentEventId", "api")}


class LiveBackend(SupervisedBackend):
    """Reuse the existing newline transport and Windows job/process-tree cleanup."""

    def __init__(self, args: argparse.Namespace, cwd: Path):
        self.timeout = args.timeout
        command = [str(args.python), "-m", "rendertest_mcp.claude_backend", "--mcp-python",
                   str(args.python), "--agent-root", str(args.agent_root), "--instance",
                   args.instance, "--capture-session", args.capture_session,
                   "--skills-root", str(args.skills_root)]
        if args.claude:
            command += ["--claude-path", str(args.claude)]
        # Keep auth/endpoint/home exactly as supplied by the caller. Do not copy credentials
        # into a fixture home, override an endpoint, or dump the process environment.
        self.process = subprocess.Popen(command, cwd=cwd, env=os.environ.copy(),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.close_job = supervise_process(self.process)
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.notifications: list[dict[str, Any]] = []
        self.stderr: list[str] = []
        self.request_id = 0
        self.readers = [threading.Thread(target=self.read_stdout, daemon=True),
                        threading.Thread(target=self.read_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr.append(safe_text(line))
            del self.stderr[:-16]

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        self.send({"id": self.request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message = self.receive(deadline - time.monotonic())
            if message.get("id") == self.request_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {safe_text(message['error'])}")
                return message["result"]
        raise RuntimeError(method + " timed out")

    def live_turn(self, thread_id: str, prompt: str, effort: str | None) -> dict[str, Any]:
        params = {"threadId": thread_id, "input": [{"type": "text", "text": prompt,
                                                   "text_elements": []}]}
        if effort:
            params["effort"] = effort
        turn_id = self.request("turn/start", params)["turn"]["id"]
        deadline = time.monotonic() + self.timeout
        while True:
            relevant = [event.get("params", {}) for event in self.notifications
                        if event.get("params", {}).get("threadId") == thread_id and
                        event.get("params", {}).get("turnId") == turn_id]
            started = {item["id"]: item for event in relevant
                       if (item := event.get("item", {})).get("type") == "mcpToolCall"}
            if len(started) > 3 or any(item.get("server") != "rendertest" or
                                       item.get("tool") not in TOOLS for item in started.values()):
                raise RuntimeError("The live turn exceeded its three-call/read-only tool budget.")
            finished = next((event["params"]["turn"] for event in self.notifications
                             if event.get("method") == "turn/completed" and
                             event["params"].get("threadId") == thread_id and
                             event["params"].get("turn", {}).get("id") == turn_id), None)
            if finished:
                if finished.get("status") != "completed":
                    raise RuntimeError("Live Claude turn did not complete: " + safe_text(finished))
                items = {event["params"]["item"]["id"]: event["params"]["item"]
                         for event in self.notifications if event.get("method") == "item/completed"
                         and event.get("params", {}).get("threadId") == thread_id
                         and event.get("params", {}).get("turnId") == turn_id}
                return {"turnId": turn_id, "prompt": prompt,
                        "answer": "\n".join(item.get("text", "") for item in items.values()
                                              if item.get("type") == "agentMessage"),
                        "tools": [item for item in items.values() if item.get("type") == "mcpToolCall"]}
            if time.monotonic() >= deadline:
                raise RuntimeError("Live turn timed out; closing its supervised backend.")
            self.receive(deadline - time.monotonic())


def response_json(answer: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    objects = []
    for position, character in enumerate(answer):
        if character == "{":
            try:
                value, _ = decoder.raw_decode(answer[position:])
                if isinstance(value, dict) and "memory_marker" in value:
                    objects.append(value)
            except ValueError:
                pass
    if len(objects) != 1:
        raise ValueError("Expected exactly one answer JSON object containing memory_marker.")
    return objects[0]


def tool_envelope(item: dict[str, Any]) -> dict[str, Any]:
    result = item.get("result", {})
    if item.get("status") != "completed" or result.get("isError"):
        raise ValueError("The model's RenderTest tool call failed: " + safe_text(item))
    value = result.get("structuredContent")
    if not isinstance(value, dict):
        candidates = []
        for part in result.get("content", []):
            if part.get("type") == "text":
                try:
                    candidate = json.loads(part.get("text", ""))
                    if isinstance(candidate, dict) and "captureSessionId" in candidate:
                        candidates.append(candidate)
                except ValueError:
                    pass
        if len(candidates) != 1:
            raise ValueError("Tool completion lacks an unambiguous structured RenderTest result.")
        value = candidates[0]
    return value


def validate_turn(turn: dict[str, Any], args: argparse.Namespace, marker: str,
                  previous_tag: str | None = None) -> dict[str, Any]:
    tools = turn["tools"]
    if not 2 <= len(tools) <= 3 or {item.get("tool") for item in tools} != TOOLS:
        raise ValueError("Each turn must actually complete get_context and inspect_event (2-3 calls).")
    evidence = {}
    for item in tools:
        if item.get("server") != "rendertest":
            raise ValueError("A tool completion came from a foreign MCP server.")
        envelope = tool_envelope(item)
        if (envelope.get("instanceId") != args.instance or
                envelope.get("captureSessionId") != args.capture_session or
                envelope.get("effect") != "read"):
            raise ValueError("Tool evidence has the wrong instance, capture session, or effect.")
        if item["tool"] == "inspect_event" and item.get("arguments", {}).get("event_id") != args.event:
            raise ValueError("inspect_event did not explicitly target the requested EID.")
        evidence[item["tool"]] = envelope["data"]
    action = evidence["inspect_event"]["action"]
    answer = response_json(turn["answer"])
    expected = {"memory_marker": marker, "instance_id": args.instance,
                "capture_session_id": args.capture_session, "event_id": args.event,
                "current_event_id": evidence["get_context"]["currentEventId"],
                "action_name": action["name"]}
    if action.get("eventId") != args.event or any(answer.get(key) != value for key, value in expected.items()):
        raise ValueError("The answer did not accurately recall the marker or quote the live tool readings.")
    tag = answer.get("reply_tag", "")
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9]{12,32}", tag) or tag in marker:
        raise ValueError("The first assistant-generated reply_tag must be 12-32 alphanumeric characters.")
    if previous_tag is not None and tag != previous_tag:
        raise ValueError("The resumed model did not recall its own previous reply_tag.")
    return answer


def prompts(args: argparse.Namespace, marker: str) -> tuple[str, str]:
    instructions = (
        f"Use only get_context(instance_id={json.dumps(args.instance)}) once, then "
        f"inspect_event(event_id={args.event}, instance_id={json.dumps(args.instance)}) once. "
        "These two read-only calls are the entire task; do not investigate shaders, list tools, "
        "change the UI, or call any other tool. Reply with one JSON object and no prose. "
        "Keys: memory_marker, reply_tag, instance_id, capture_session_id, current_event_id, "
        "event_id, action_name. Copy instance_id and capture_session_id from the tool envelope; "
        "current_event_id from get_context.data.currentEventId; event_id and action_name from "
        "inspect_event.data.action.eventId and .name. Preserve exact strings and numeric types. "
    )
    first = ("This is a short live capture and conversation-memory check. Remember this exact "
             f"memory_marker for later: {marker}. Invent a new random 12-character alphanumeric "
             "reply_tag of your own, distinct from the marker, and remember it too. " + instructions)
    second = ("Continue our previous conversation after the app process was restarted. Recall the "
              "exact memory_marker I gave you and the exact reply_tag you invented in your previous "
              "answer. Neither value is supplied in this request. Put those remembered values in "
              "the corresponding JSON keys. Independently refresh the live capture facts now. " + instructions)
    return first, second


def check(args: argparse.Namespace) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    directory = args.output_root / run_id
    directory.mkdir(parents=True, exist_ok=False)
    report_path = directory / "report.json"
    report: dict[str, Any] = {"runId": run_id, "backend": "claude", "passed": False,
                              "requestedModel": args.model or "default",
                              "turns": [], "processExitCodes": [], "report": str(report_path)}
    print("Live check report:", report_path, flush=True)
    try:
        report["capture"] = validate_scope(args)
        marker = "RTLIVE_" + uuid.uuid4().hex
        first_prompt, second_prompt = prompts(args, marker)
        assert marker not in second_prompt
        params: dict[str, Any] = {"sandbox": "read-only", "approvalPolicy": "never",
                                 "config": {"mcp_servers.rendertest.enabled": True},
                                 "ephemeral": False,
                                 "developerInstructions": json.dumps(report["capture"])}
        if args.model:
            params["model"] = args.model
        if args.effort:
            params["effort"] = args.effort
        thread_id, previous_tag = "", None
        for index, prompt in enumerate((first_prompt, second_prompt)):
            validate_scope(args)
            backend = LiveBackend(args, directory)
            try:
                backend.initialize()
                account = backend.request("account/read", {"refreshToken": False})
                if account.get("requiresAuth") or not account.get("account"):
                    raise ValueError("Claude authentication is unavailable to the panel backend.")
                operation = "thread/start" if index == 0 else "thread/resume"
                print(f"Round {index + 1}: {operation}", flush=True)
                response = backend.request(operation, params if index == 0 else
                                           dict(params, threadId=thread_id, excludeTurns=True))
                if response.get("policy") != POLICY:
                    raise ValueError("Claude did not confirm the required tool restrictions.")
                if index == 0:
                    thread_id = response["thread"]["id"]
                    if str(uuid.UUID(thread_id)) != thread_id:
                        raise ValueError("Native session id is not a canonical UUID.")
                    report.update(threadId=thread_id, model=response.get("model"),
                                  ledger=str(args.agent_root / "claude-sessions" / (thread_id + ".json")))
                elif (response["thread"]["id"] != thread_id or response.get("resumeState") != "restored"):
                    raise ValueError("The second process did not restore the original native Claude session.")
                turn = backend.live_turn(thread_id, prompt, args.effort)
                report["turns"].append(turn)
                answer = validate_turn(turn, args, marker, previous_tag)
                previous_tag = answer["reply_tag"]
                print(f"Round {index + 1}: verified {len(turn['tools'])} real tools and answer", flush=True)
            finally:
                backend.close()
                report["processExitCodes"].append(backend.process.returncode)
            if backend.process.returncode != 0:
                raise RuntimeError("The supervised Claude bridge did not exit cleanly on stdin EOF.")
        report.update(passed=True, authenticated=True, nativeResume=True, recalledUserMarker=True,
                      recalledAssistantTag=True, liveToolReadings=True,
                      cleanup="Both supervised process trees stopped. Unique native session and ledger retained; panel conversations untouched.")
        return report
    except BaseException as exc:
        report["error"] = safe_text(exc)
        raise
    finally:
        report_path.write_text(safe_text(json.dumps(report, indent=2, ensure_ascii=False)), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", required=True, help="Exact online RenderTest instance id")
    parser.add_argument("--capture-session", required=True, help="Exact 32-character capture session id")
    parser.add_argument("--agent-root", type=Path, required=True, help="Real RenderTest Bridge root")
    parser.add_argument("--event", type=int, required=True, help="Explicit EID for both inspect_event calls")
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="Python with rendertest_mcp[claude] installed")
    parser.add_argument("--claude", type=Path, help="Native Claude executable; omitted uses SDK discovery")
    parser.add_argument("--model", help="Explicit model; omitted follows the existing Claude default")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--timeout", type=float, default=300, help="Timeout seconds per RPC/turn (default: 300)")
    parser.add_argument("--skills-root", type=Path, default=REPO / ".agents/skills")
    parser.add_argument("--output-root", type=Path, default=REPO / "agent/.agent-data/claude-live-check",
                        help="Independent evidence-report directory; does not store GUI conversation records")
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.instance) or
            not re.fullmatch(r"[0-9a-f]{32}", args.capture_session) or args.event < 0 or
            not 1 <= args.timeout <= 3600):
        parser.error("Invalid instance, capture session, EID, or timeout.")
    for key in ("agent_root", "python", "skills_root", "output_root", "claude"):
        value = getattr(args, key)
        if value is not None:
            setattr(args, key, value.expanduser().resolve())
    try:
        result = check(args)
        print(json.dumps({key: value for key, value in result.items() if key != "turns"}, indent=2))
        print("PASS: live Claude, real scoped RenderTest tools, and cross-process native conversation recall")
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        print("FAIL:", safe_text(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
