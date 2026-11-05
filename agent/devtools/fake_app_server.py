"""Scripted stand-in for ``codex app-server`` used to test the qrendertest AI panel.

Usage (normally via RENDERTEST_AGENT_CODEX_COMMAND):
    python devtools/fake_app_server.py --scenario stream --log received.jsonl [codex -c args...]

It speaks the same newline-delimited JSON as the real app-server and appends every message it
receives (plus a few events of its own) to ``--log`` so tests can assert on the client side of the
protocol. Unknown arguments, such as the ``-c`` overrides the panel passes, are recorded and
otherwise ignored.

Scenarios:
    stream        streamed agent message (A1)
    tool          one mcpToolCall from started to completed (A2)
    approvals     sends every approval-style server request before answering (A3)
    interrupt     first turn runs until turn/interrupt, then completes as interrupted (A4)
    hang          first turn ignores turn/interrupt so the panel has to kill it (A4)
    crash-once    exits mid-turn the first time, streams normally afterwards (A5)
    logged-out    account/read returns no account (A6)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

SCENARIOS = ("stream", "tool", "approvals", "interrupt", "hang", "crash-once", "logged-out")
ANSWER = "Hello from the **fake** app-server.\n\n- EID 1513 draws the scene\n- uses `ResourceId::116618`"
SERVER_REQUESTS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
    "item/tool/requestUserInput",
)


class FakeAppServer:
    def __init__(self, scenario: str, log: Path | None, state_dir: Path | None, argv: list[str]):
        self.scenario = scenario
        self.log_path = log
        self.state_dir = state_dir
        self.lock = threading.Lock()
        self.thread_counter = 0
        self.turn_counter = 0
        self.prefix = f"{os.getpid()}"
        self.interrupted = threading.Event()
        self.server_replies: dict[int, threading.Event] = {}
        self.next_server_id = 1000
        self.thread_id = ""
        self.log({"event": "start", "pid": os.getpid(), "scenario": scenario, "argv": argv})

    def log(self, entry: dict[str, Any]) -> None:
        if not self.log_path:
            return
        entry = dict(entry, t=time.time())
        with self.lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False) + "\n"
        with self.lock:
            sys.stdout.write(data)
            sys.stdout.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self.send({"id": request_id, "result": result})

    def crash_marker(self) -> Path | None:
        return self.state_dir / "crashed-once" if self.state_dir else None

    def handle(self, message: dict[str, Any]) -> None:
        self.log({"event": "recv", "message": message})
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method is None and request_id is not None:
            event = self.server_replies.get(request_id)
            if event:
                event.set()
            return
        if request_id is None:
            return

        if method == "initialize":
            self.respond(request_id, {"userAgent": "fake-app-server/0", "codexHome": "fake",
                                      "platformFamily": "windows", "platformOs": "windows"})
        elif method == "account/read":
            if self.scenario == "logged-out":
                self.respond(request_id, {"account": None, "requiresOpenaiAuth": True})
            else:
                self.respond(request_id, {"account": {"type": "chatgpt", "email": "fake@example.com",
                                                      "planType": "plus"},
                                          "requiresOpenaiAuth": True})
        elif method == "skills/extraRoots/set":
            self.respond(request_id, {})
        elif method == "config/read":
            self.respond(request_id, {"config": {"mcp_servers": {
                "rendertest": {"command": "python"}, "blender": {"command": "blender-mcp"}}}})
        elif method == "thread/start":
            self.thread_counter += 1
            self.thread_id = f"thr_{self.prefix}_{self.thread_counter}"
            self.respond(request_id, {"thread": {"id": self.thread_id}, "model": "fake-model",
                                      "sandbox": {"type": "readOnly", "networkAccess": False},
                                      "approvalPolicy": "never"})
        elif method == "mcpServerStatus/list":
            self.respond(request_id, {"data": [
                {"name": "rendertest", "runtimeStatus": "connected",
                 "tools": {f"tool{i}": {} for i in range(22)}, "toolsError": None},
                {"name": "blender", "runtimeStatus": "disabled", "tools": {}, "toolsError": None},
            ], "nextCursor": None})
        elif method == "turn/start":
            self.turn_counter += 1
            turn_id = f"turn_{self.prefix}_{self.turn_counter}"
            self.interrupted.clear()
            self.respond(request_id, {"turn": {"id": turn_id, "status": "inProgress"}})
            threading.Thread(target=self.run_turn, args=(params.get("threadId"), turn_id),
                             daemon=True).start()
        elif method == "turn/interrupt":
            if self.scenario == "hang":
                self.log({"event": "ignoring-interrupt"})
                return
            self.respond(request_id, {})
            self.interrupted.set()
        else:
            self.send({"id": request_id, "error": {"code": -32601, "message": f"fake: {method}"}})

    def item_id(self, turn_id: str, name: str) -> str:
        return f"{turn_id}_{name}"

    def complete(self, thread_id: str, turn_id: str, status: str, error: str | None = None) -> None:
        turn: dict[str, Any] = {"id": turn_id, "status": status}
        if error:
            turn["error"] = {"message": error}
        self.notify("turn/completed", {"threadId": thread_id, "turn": turn})

    def stream_message(self, thread_id: str, turn_id: str, text: str, delay: float = 0.01) -> None:
        item_id = self.item_id(turn_id, "msg")
        self.notify("item/started", {"threadId": thread_id, "turnId": turn_id,
                                     "item": {"type": "agentMessage", "id": item_id, "text": ""}})
        for start in range(0, len(text), 7):
            self.notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id,
                                                    "itemId": item_id, "delta": text[start:start + 7]})
            time.sleep(delay)
        self.notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                                       "item": {"type": "agentMessage", "id": item_id, "text": text}})

    def run_turn(self, thread_id: str, turn_id: str) -> None:
        self.notify("turn/started", {"threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress"}})
        reasoning_id = self.item_id(turn_id, "reasoning")
        self.notify("item/started", {"threadId": thread_id, "turnId": turn_id,
                                     "item": {"type": "reasoning", "id": reasoning_id, "summary": [],
                                              "content": []}})
        for word in ("**Checking** ", "the ", "capture"):
            self.notify("item/reasoning/summaryTextDelta", {"threadId": thread_id, "turnId": turn_id,
                                                            "itemId": reasoning_id, "delta": word,
                                                            "summaryIndex": 0})
        self.notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                                       "item": {"type": "reasoning", "id": reasoning_id,
                                                "summary": ["**Checking** the capture"], "content": []}})

        if self.scenario == "crash-once":
            marker = self.crash_marker()
            if marker and not marker.exists():
                marker.write_text("1", encoding="utf-8")
                self.log({"event": "crash"})
                sys.stderr.write("fake app-server: simulated crash\n")
                sys.stderr.flush()
                os._exit(3)

        if self.scenario == "tool":
            tool_id = self.item_id(turn_id, "tool")
            item = {"type": "mcpToolCall", "id": tool_id, "server": "rendertest", "tool": "get_context",
                    "status": "inProgress", "arguments": {}}
            self.notify("item/started", {"threadId": thread_id, "turnId": turn_id, "item": item})
            self.notify("item/mcpToolCall/progress", {"threadId": thread_id, "turnId": turn_id,
                                                      "itemId": tool_id, "message": "reading context"})
            time.sleep(0.1)
            done = dict(item, status="completed", durationMs=123,
                        result={"content": [], "structuredContent": {"ok": True, "eventId": 1513}})
            self.notify("item/completed", {"threadId": thread_id, "turnId": turn_id, "item": done})

        if self.scenario == "approvals":
            for method in SERVER_REQUESTS:
                request_id = self.next_server_id
                self.next_server_id += 1
                event = threading.Event()
                self.server_replies[request_id] = event
                self.send({"id": request_id, "method": method,
                           "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "x"}})
                if not event.wait(5):
                    self.log({"event": "no-reply", "method": method})
                    self.complete(thread_id, turn_id, "failed", f"no reply to {method}")
                    return

        stall_marker = self.state_dir / "stalled-once" if self.state_dir else None
        if self.scenario in ("interrupt", "hang") and not (stall_marker and stall_marker.exists()):
            if stall_marker:
                stall_marker.write_text("1", encoding="utf-8")
            tool_id = self.item_id(turn_id, "tool")
            item = {"type": "mcpToolCall", "id": tool_id, "server": "rendertest", "tool": "pixel_history",
                    "status": "inProgress", "arguments": {"x": 256, "y": 256}}
            self.notify("item/started", {"threadId": thread_id, "turnId": turn_id, "item": item})
            deadline = time.time() + 60
            while time.time() < deadline:
                if self.interrupted.wait(0.05):
                    self.complete(thread_id, turn_id, "interrupted")
                    return
            self.complete(thread_id, turn_id, "failed", "fake: nobody stopped the turn")
            return

        self.stream_message(thread_id, turn_id, ANSWER)
        self.notify("thread/tokenUsage/updated", {"threadId": thread_id, "turnId": turn_id,
                                                  "tokenUsage": {"total": {"totalTokens": 4242},
                                                                 "last": {"totalTokens": 42},
                                                                 "modelContextWindow": 200000}})
        self.complete(thread_id, turn_id, "completed")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="stream")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--state-dir", type=Path)
    args, rest = parser.parse_known_args(argv)

    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    server = FakeAppServer(args.scenario, args.log, args.state_dir, rest)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            server.log({"event": "bad-json", "line": line[:200]})
            continue
        server.handle(message)
    server.log({"event": "stdin-closed"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
