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
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any

SCENARIOS = ("stream", "tool", "approvals", "interrupt", "hang", "crash-once", "logged-out",
             "config-error", "config-malformed", "status-error", "status-foreign",
             "status-page-foreign", "status-pages", "status-loop", "turn-timeout-once",
             "late-items", "late-before-start", "late-response", "command-status", "explicit-links",
             "login-success", "login-failure", "login-cancel", "model-pages", "model-error", "model-delayed",
             "model-empty", "model-loop",
             "login-not-found", "auth-recovered", "account-updated", "tool-artifacts", "tool-artifacts-rejected",
             "persistent", "resume-failure", "resume-config-error", "resume-foreign", "resume-unsafe", "resume-replay",
             "claude-policy-missing", "claude-policy-tools", "claude-policy-builtin", "claude-policy-external",
             "claude-policy-approvals", "claude-policy-type", "claude-resume-policy-missing", "claude-resume-policy-bad",
             "claude-disconnect-completed", "claude-disconnect-failed", "claude-disconnect-auth")
ANSWER = "Hello from the **fake** app-server.\n\n- EID 1513 draws the scene\n- uses `ResourceId::116618`"
SERVER_REQUESTS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
    "item/tool/requestUserInput",
)


class FakeAppServer:
    def __init__(self, scenario: str, log: Path | None, state_dir: Path | None, argv: list[str],
                 backend: str = "codex"):
        self.scenario = scenario
        self.backend = backend
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
        self.threads: dict[str, dict[str, Any]] = {}
        self.was_resumed = False
        self.initialized = False
        self.account_reads = 0
        self.capture_tools_enabled = False
        login_marker = self.state_dir / "logged-in" if self.state_dir else None
        self.logged_in = (not scenario.startswith("login-") and scenario != "auth-recovered") or bool(login_marker and login_marker.exists())
        self.login_id: str | None = None
        self.log({"event": "start", "pid": os.getpid(), "scenario": scenario, "argv": argv, "backend": backend})

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

    def send_batch(self, messages: list[dict[str, Any]]) -> None:
        """Keep a response and deliberately racing notification in the same pipe write."""
        with self.lock:
            sys.stdout.write("".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages))
            sys.stdout.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        thread = self.threads.get(params.get("threadId", ""))
        turn_id = params.get("turnId") or params.get("turn", {}).get("id")
        if thread and turn_id:
            turn = next((entry for entry in thread["turns"] if entry["id"] == turn_id), None)
            if turn and method == "item/completed":
                item = params["item"]
                turn["items"] = [entry for entry in turn["items"] if entry.get("id") != item.get("id")]
                turn["items"].append(item)
            if turn and method == "turn/completed":
                turn.update(params["turn"])
                self.persist_thread(thread)
        self.send({"method": method, "params": params})

    def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self.send({"id": request_id, "result": result})

    def crash_marker(self) -> Path | None:
        return self.state_dir / "crashed-once" if self.state_dir else None

    def spawn_cleanup_child(self) -> None:
        if os.name != "nt":
            return
        # Inherit the UI client's Windows job, but no stdio pipes. This child deliberately
        # outlives its fake parent unless process-tree cleanup closes the job immediately.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        self.log({"event": "cleanup-child", "pid": child.pid})

    def thread_path(self, thread_id: str) -> Path | None:
        if not self.state_dir or not re.fullmatch(r"thr_[0-9]+_[0-9]+", thread_id):
            return None
        return self.state_dir / "fake-threads" / f"{thread_id}.json"

    def persist_thread(self, thread: dict[str, Any]) -> None:
        path = self.thread_path(thread["id"])
        if not path or thread["ephemeral"] or not thread["turns"]:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(thread), encoding="utf-8")
        temporary.replace(path)

    def thread_result(self, thread: dict[str, Any], exclude_turns: bool = False) -> dict[str, Any]:
        public = dict(thread)
        if exclude_turns:
            public["turns"] = []
        if self.backend == "claude":
            result = {"thread": public, "model": thread["model"]}
            policy = {"toolPolicy": "rendertest-only", "builtinTools": False,
                      "externalMcp": False, "approvals": "deny"}
            if self.scenario in ("claude-policy-missing", "claude-resume-policy-missing"):
                # A Codex-looking sandbox is deliberately not a substitute for Claude's tool policy.
                return dict(result, sandbox={"type": "readOnly"}, approvalPolicy="never")
            bad_policy = {"claude-policy-tools": ("toolPolicy", "all"),
                          "claude-policy-builtin": ("builtinTools", True),
                          "claude-policy-external": ("externalMcp", True),
                          "claude-policy-approvals": ("approvals", "allow"),
                          "claude-policy-type": ("builtinTools", "false"),
                          "claude-resume-policy-bad": ("externalMcp", True)}
            if self.scenario in bad_policy:
                key, value = bad_policy[self.scenario]
                policy[key] = value
            return dict(result, policy=policy)
        return {"thread": public, "model": thread["model"],
                "sandbox": {"type": "readOnly", "networkAccess": False}, "approvalPolicy": "never"}

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
            if self.initialized:
                self.send({"id": request_id, "error": {"code": -32600, "message": "Already initialized"}})
                return
            self.initialized = True
            self.respond(request_id, {"userAgent": "fake-app-server/0", "codexHome": "fake",
                                      "platformFamily": "windows", "platformOs": "windows"})
        elif method == "account/read":
            self.account_reads += 1
            if self.scenario == "auth-recovered" and self.account_reads >= 2:
                self.logged_in = True
            if self.backend == "claude":
                self.respond(request_id, {
                    "account": None if self.scenario == "logged-out" or not self.logged_in else {"type": "external"},
                    "requiresAuth": True,
                    "loginMessage": "Claude authentication unavailable. Run claude auth login externally, then reconnect."})
                return
            if self.scenario == "logged-out" or not self.logged_in:
                self.respond(request_id, {"account": None, "requiresOpenaiAuth": True})
            else:
                self.respond(request_id, {"account": {"type": "chatgpt", "email": "fake@example.com",
                                                      "planType": "plus"},
                                          "requiresOpenaiAuth": True})
        elif method == "account/login/start":
            if self.backend == "claude":
                self.send({"id": request_id, "error": {"code": -32601, "message": "Claude uses external login"}})
                return
            if params.get("type") != "chatgpt":
                self.send({"id": request_id, "error": {"code": -32602, "message": "expected ChatGPT login"}})
                return
            self.login_id = f"login-{self.prefix}"
            login_id = self.login_id
            self.respond(request_id, {"type": "chatgpt", "loginId": login_id,
                                      "authUrl": f"https://example.invalid/rendertest-login/{login_id}"})
            if self.scenario not in ("login-cancel", "login-not-found"):
                threading.Timer(0.25, self.finish_login, args=(login_id,)).start()
        elif method == "account/login/cancel":
            login_id = params.get("loginId")
            matched = login_id == self.login_id
            if matched:
                self.login_id = None
            if self.scenario == "login-not-found":
                self.logged_in = True  # Another login operation already completed the account transition.
                self.respond(request_id, {"status": "notFound"})
                return
            # Completion can race cancellation. This must not restore a canceled login UI.
            if matched:
                self.log({"event": "late-login-completion", "loginId": login_id})
                self.send_batch([
                    {"id": request_id, "result": {"status": "canceled"}},
                    {"method": "account/login/completed", "params": {"loginId": login_id, "success": True,
                                                                       "error": None, "onboardingEntrypoint": None}},
                ])
            else:
                self.respond(request_id, {"status": "notFound"})
        elif method == "model/list":
            if self.scenario in ("model-error", "model-empty"):
                message = {"id": request_id}
                if self.scenario == "model-error":
                    message["error"] = {"code": -32000, "message": "model catalog unavailable"}
                else:
                    message["result"] = {"data": [], "nextCursor": None}
                # Leave enough time for the UI hook to type before completion, independent of
                # whether Python starts more quickly than the dialog's first 50 ms probe.
                timer = threading.Timer(0.2, self.send, args=(message,))
                timer.daemon = True
                timer.start()
                return
            models = [self.model("fake-model", "Fake model", ["low", "high"], "low", True),
                      self.model("fake-deep", "Fake deep model", ["medium", "ultra"], "medium", False)]
            if self.scenario == "model-delayed":
                self.log({"event": "model-list-delayed"})

                def finish_models() -> None:
                    self.log({"event": "model-list-response"})
                    self.respond(request_id, {"data": models, "nextCursor": None})

                timer = threading.Timer(2.0, finish_models)
                timer.daemon = True
                timer.start()
                return
            if self.scenario == "model-pages":
                second = bool(params.get("cursor"))
                self.respond(request_id, {"data": models[1:] if second else models[:1],
                                          "nextCursor": None if second else "models-page2"})
            elif self.scenario == "model-loop":
                self.respond(request_id, {"data": models[:1], "nextCursor": "models-loop"})
            else:
                self.respond(request_id, {"data": models, "nextCursor": None})
        elif method == "skills/extraRoots/set":
            self.respond(request_id, {})
        elif method == "config/read":
            if self.scenario in ("config-error", "resume-config-error"):
                self.send({"id": request_id, "error": {"code": -32000, "message": "config unavailable"}})
                return
            if self.scenario == "config-malformed":
                self.respond(request_id, {})
                return
            servers = {"rendertest": {"command": "python"}}
            if self.backend == "codex":
                servers["blender"] = {"command": "blender-mcp"}
            self.respond(request_id, {"config": {"mcp_servers": servers}})
        elif method == "thread/start":
            self.capture_tools_enabled = params.get("config", {}).get("mcp_servers.rendertest.enabled", True)
            self.thread_counter += 1
            self.thread_id = f"thr_{self.prefix}_{self.thread_counter}"
            thread = {"id": self.thread_id, "model": params.get("model") or "fake-model",
                      "ephemeral": params.get("ephemeral", False), "turns": [],
                      "modelProvider": "fake-provider", "historyMode": params.get("historyMode", "paginated")}
            self.threads[self.thread_id] = thread
            self.respond(request_id, self.thread_result(thread))
        elif method == "thread/resume":
            thread_id = params.get("threadId", "")
            path = self.thread_path(thread_id)
            if self.scenario == "resume-failure" or not path or not path.is_file():
                self.send({"id": request_id, "error": {"code": -32600, "message": "fake: saved thread unavailable"}})
                return
            thread = json.loads(path.read_text(encoding="utf-8"))
            if params.get("model"):
                thread["model"] = params["model"]
            self.thread_id = thread_id
            self.threads[thread_id] = thread
            self.was_resumed = True
            self.capture_tools_enabled = params.get("config", {}).get("mcp_servers.rendertest.enabled", True)
            response = self.thread_result(thread, params.get("excludeTurns", False) and self.scenario != "resume-replay")
            if self.scenario == "resume-unsafe":
                response["sandbox"] = {"type": "workspaceWrite", "networkAccess": False}
            self.respond(request_id, response)
            self.notify("thread/started", {"thread": response["thread"]})
        elif method == "mcpServerStatus/list":
            if self.scenario == "status-error":
                self.send({"id": request_id, "error": {"code": -32000, "message": "status unavailable"}})
                return
            primary = {"name": "rendertest",
                       "runtimeStatus": "connected" if self.capture_tools_enabled else "disabled",
                       "tools": {f"tool{i}": {} for i in range(22)} if self.capture_tools_enabled else {},
                       "toolsError": None}
            other = {"name": "blender", "runtimeStatus": "disabled", "tools": {}, "toolsError": None}
            if self.scenario in ("status-foreign", "status-page-foreign") or (self.was_resumed and self.scenario == "resume-foreign"):
                other["runtimeStatus"] = "connected"
            if self.scenario in ("status-page-foreign", "status-pages", "status-loop"):
                page = [other] if params.get("cursor") else [primary]
                cursor = "page2" if not params.get("cursor") or self.scenario == "status-loop" else None
                self.respond(request_id, {"data": page, "nextCursor": cursor})
            else:
                self.respond(request_id, {"data": [primary] if self.backend == "claude" else [primary, other],
                                          "nextCursor": None})
        elif method == "turn/start":
            self.turn_counter += 1
            turn_id = f"turn_{self.prefix}_{self.turn_counter}"
            thread = self.threads.get(params.get("threadId", ""))
            if thread is not None:
                thread["turns"].append({"id": turn_id, "status": "inProgress", "items": [
                    {"type": "userMessage", "id": f"{turn_id}_user", "content": params.get("input", [])}],
                    "error": None, "itemsView": "full"})
                self.persist_thread(thread)
            self.interrupted.clear()
            marker = self.state_dir / "timed-out-once" if self.state_dir else None
            if self.scenario == "turn-timeout-once" and marker and not marker.exists():
                marker.write_text("1", encoding="utf-8")
                self.notify("turn/started", {"threadId": params["threadId"],
                                             "turn": {"id": turn_id, "status": "inProgress"}})
                # The client must terminate this accepted remote turn, not merely time out locally.
                threading.Timer(2, lambda: self.respond(request_id, {
                    "turn": {"id": turn_id, "status": "inProgress"}})).start()
                return
            if self.scenario == "late-before-start" and self.turn_counter == 2:
                previous = f"turn_{self.prefix}_1"
                self.notify("turn/started", {"threadId": params["threadId"],
                                             "turn": {"id": previous, "status": "inProgress"}})
                self.notify("item/completed", {"threadId": params["threadId"], "turnId": previous,
                    "item": {"type": "agentMessage", "id": "stale-before-start", "text": "STALE ANSWER"}})
                self.complete(params["threadId"], previous, "completed")
                self.notify("item/completed", {"threadId": params["threadId"], "turnId": turn_id,
                    "item": {"type": "agentMessage", "id": "early-current", "text": "EARLY ITEM"}})
            if self.scenario == "late-response" and self.turn_counter == 1:
                threading.Timer(0.7, lambda: self.send({"id": request_id, "error": {
                    "code": -32000, "message": "late response from finished first turn"}})).start()
                threading.Thread(target=self.run_turn, args=(params["threadId"], turn_id), daemon=True).start()
                return
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

    @staticmethod
    def model(name: str, title: str, efforts: list[str], default: str, is_default: bool) -> dict[str, Any]:
        # Deliberately use distinct id/model to detect clients sending the display/catalog id.
        return {"id": f"catalog-{name}", "model": name, "displayName": title,
                "description": f"Test catalog entry for {name}", "hidden": False,
                "isDefault": is_default, "defaultReasoningEffort": default,
                "supportedReasoningEfforts": [{"reasoningEffort": effort, "description": effort.title()}
                                               for effort in efforts],
                "inputModalities": ["text"], "upgrade": None, "upgradeInfo": None,
                "availabilityNux": None, "modelSpecialty": None, "supportsPersonality": False,
                "multiAgentVersion": None, "additionalSpeedTiers": [], "serviceTiers": [],
                "defaultServiceTier": None, "availableAccessPrograms": None}

    def finish_login(self, login_id: str) -> None:
        if self.login_id != login_id:
            return
        success = self.scenario != "login-failure"
        if success:
            self.logged_in = True
            if self.state_dir:
                (self.state_dir / "logged-in").write_text("1", encoding="utf-8")
        self.login_id = None
        self.notify("account/login/completed", {"loginId": login_id, "success": success,
                    "error": None if success else "fake login rejected", "onboardingEntrypoint": None})
        if success:
            self.notify("account/updated", {"authMode": "chatgpt", "planType": "plus"})

    def artifact_result(self) -> dict[str, Any]:
        instance, session = f"fake-{self.prefix}", "0123456789abcdef0123456789abcdef"
        root = Path(os.environ["RENDERTEST_AGENT_ROOT"]).resolve()
        directory = root / "artifacts" / instance / session / "textures"
        directory.mkdir(parents=True, exist_ok=True)

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)) +
               chunk(b"IDAT", zlib.compress((b"\x00" + b"\x20\x80\xff\xff" * 2) * 2)) + chunk(b"IEND", b""))
        valid = directory / "valid.png"
        outside = root.parent / "outside-artifact.png"
        corrupt = directory / "not-a-png.png"
        missing = directory / "missing.png"
        valid.write_bytes(png)
        outside.write_bytes(png)
        corrupt.write_bytes(b"this is plain text, not an image")

        def describe(path: Path, data: bytes) -> dict[str, Any]:
            return {"kind": "image", "path": str(path), "mediaType": "image/png", "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(), "source": {"eventId": 1513}}

        artifacts = [describe(valid, png), describe(outside, png),
                     describe(corrupt, corrupt.read_bytes()), describe(missing, png)]
        if self.scenario == "tool-artifacts-rejected":
            artifacts[0], artifacts[1] = artifacts[1], artifacts[0]
        envelope = {"schema": "rendertest-mcp-result/v1", "ok": True, "instanceId": instance,
                    "captureSessionId": session, "meta": {"captureSessionId": session},
                    "data": {"preview": "RESULT_VISIBLE_MARKER", "padding": "X" * 6000},
                    "artifacts": artifacts}
        # export_texture sends a JSON text block; its artifacts occur beyond the 4 KiB display
        # truncation. The UI must extract metadata from the original result, not the preview.
        return {"content": [{"type": "text", "text": json.dumps(envelope)}], "isError": False}

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
        if self.scenario == "late-response" and self.turn_counter == 2:
            time.sleep(0.6)
        self.notify("turn/started", {"threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress"}})
        if self.scenario == "late-items":
            for stale_thread, stale_turn in (("old-thread", turn_id),
                                             (thread_id, f"turn_{self.prefix}_{self.turn_counter - 1}")):
                self.notify("item/completed", {"threadId": stale_thread, "turnId": stale_turn,
                    "item": {"type": "agentMessage", "id": "stale", "text": "STALE ANSWER"}})
        if self.scenario == "command-status":
            for kind in ("commandExecution", "fileChange"):
                for status in ("completed", "failed", "declined"):
                    self.notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                        "item": {"type": kind, "id": f"{kind}-{status}", "command": "echo hello",
                                 "status": status}})
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
                self.spawn_cleanup_child()
                sys.stderr.write("fake app-server: simulated crash\n")
                sys.stderr.flush()
                os._exit(3)

        if self.scenario == "tool" or self.scenario.startswith("tool-artifacts"):
            tool_id = self.item_id(turn_id, "tool")
            artifact = self.scenario.startswith("tool-artifacts")
            arguments = {"padding": "A" * 700, "z_marker": "ARGUMENT_END_MARKER"} if artifact else {}
            item = {"type": "mcpToolCall", "id": tool_id, "server": "rendertest",
                    "tool": "export_texture" if artifact else "get_context",
                    "status": "inProgress", "arguments": arguments}
            self.notify("item/started", {"threadId": thread_id, "turnId": turn_id, "item": item})
            self.notify("item/mcpToolCall/progress", {"threadId": thread_id, "turnId": turn_id,
                                                      "itemId": tool_id, "message": "reading context"})
            time.sleep(0.1)
            result = self.artifact_result() if artifact else {"content": [], "structuredContent": {"ok": True, "eventId": 1513}}
            done = dict(item, status="completed", durationMs=123, result=result)
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
            if self.scenario == "hang":
                self.spawn_cleanup_child()
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

        disconnect_marker = self.state_dir / "disconnected-once" if self.state_dir else None
        disconnect = self.scenario.startswith("claude-disconnect-") and not (
            disconnect_marker and disconnect_marker.exists())
        if disconnect and disconnect_marker:
            disconnect_marker.write_text("1", encoding="utf-8")
        failed_disconnect = disconnect and self.scenario != "claude-disconnect-completed"
        if failed_disconnect:
            self.complete(thread_id, turn_id, "failed", "fake Claude connection failed")
        text = ANSWER
        if self.scenario == "explicit-links":
            text += "\n[direct](rt://event/1513?source=forged) [EID 1513](rt://resource/116618)"
        if not failed_disconnect:
            self.stream_message(thread_id, turn_id, text)
            self.notify("thread/tokenUsage/updated", {"threadId": thread_id, "turnId": turn_id,
                                                      "tokenUsage": {"total": {"totalTokens": 4242},
                                                                     "last": {"totalTokens": 42},
                                                                     "modelContextWindow": 200000}})
            self.complete(thread_id, turn_id, "completed")
        if disconnect:
            requires_auth = self.scenario == "claude-disconnect-auth"
            self.log({"event": "backend-disconnected", "requiresAuth": requires_auth})
            self.notify("backend/disconnected", {"requiresAuth": requires_auth,
                "message": "Claude authentication expired. Run claude auth login externally."
                           if requires_auth else "Claude transport disconnected. Send again to reconnect."})
        if self.scenario == "account-updated":
            self.logged_in = False
            self.notify("account/updated", {"authMode": None, "planType": None})


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="stream")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--backend", choices=("codex", "claude"), default="codex")
    args, rest = parser.parse_known_args(argv)

    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    server = FakeAppServer(args.scenario, args.log, args.state_dir, rest, args.backend)
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
