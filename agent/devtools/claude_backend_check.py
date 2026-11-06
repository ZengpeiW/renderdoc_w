"""Exercise the real Claude SDK/CLI through a localhost-only Messages API fixture.

No real model or capture is used. A fake RenderTest bridge supplies one texture artifact;
the real harness calls its MCP tool, persists the conversation, and resumes it in another
process. Credentials and Claude state are isolated under agent/.agent-data.
"""
from __future__ import annotations

import argparse
import ctypes
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from appserver_history_check import AppServer
from chat_panel_e2e import alive, process_tree
from fake_bridge import FakeRenderTestBridge

REPO = Path(__file__).resolve().parents[2]
FIRST = "CLAUDE_LOCAL_FIRST_QUESTION"
SECOND = "CLAUDE_LOCAL_SECOND_QUESTION"
ANSWER1 = "CLAUDE_LOCAL_FIRST_ANSWER"
ANSWER2 = "CLAUDE_LOCAL_SECOND_ANSWER"
KEY = "rendertest-local-fixture-key"


def supervise_process(process: subprocess.Popen) -> Any:
    """Mirror the Qt host's kill-on-close Job Object on Windows."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class Limits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                    ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("limits", Limits), ("io", ctypes.c_uint64 * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):
        error = ctypes.WinError(ctypes.get_last_error())
        if job:
            kernel.CloseHandle(job)
        process.kill()
        process.wait()
        raise error
    return lambda: kernel.CloseHandle(job)


class Fixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []


class Handler(BaseHTTPRequestHandler):
    server: Fixture

    def log_message(self, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:
        try:
            payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.headers.get("Content-Encoding") == "gzip":
                payload = gzip.decompress(payload)
            request = json.loads(payload)
            if self.path.split("?")[0].endswith("/messages/count_tokens"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens":100}')
                return
            if not self.path.split("?")[0].endswith("/messages"):
                raise ValueError("unexpected fixture endpoint " + self.path)
            if self.headers.get("x-api-key") != KEY:
                raise ValueError("fixture must use only its isolated test credential")
            self.server.requests.append(request)
            number = len(self.server.requests)
            if number > 3 or not request.get("stream"):
                raise ValueError("expected exactly three streamed model steps")
            names = [tool.get("name", "") for tool in request.get("tools", [])]
            if not names or any(not name.startswith("mcp__rendertest__") for name in names):
                raise ValueError("model received missing or foreign/builtin tools: " + repr(names))
            if number == 1:
                tool = "mcp__rendertest__export_texture"
                if tool not in names:
                    raise ValueError("texture export tool was not supplied")
                content = {"type": "tool_use", "id": "toolu_local_texture", "name": tool,
                           "input": {"resource_id": "1001", "event_id": 10}}
            else:
                content = {"type": "text", "text": ANSWER1 if number == 2 else ANSWER2}
            message = {"id": f"msg_local_{number}", "type": "message", "role": "assistant",
                       "model": request["model"], "content": [], "stop_reason": None,
                       "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 1}}
            start = dict(content, input={}) if content["type"] == "tool_use" else dict(content, text="")
            delta = ({"type": "input_json_delta", "partial_json": json.dumps(content["input"])}
                     if content["type"] == "tool_use" else {"type": "text_delta", "text": content["text"]})
            events = [
                {"type": "message_start", "message": message},
                {"type": "content_block_start", "index": 0, "content_block": start},
                {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use" if number == 1 else "end_turn",
                                                      "stop_sequence": None}, "usage": {"output_tokens": 10}},
                {"type": "message_stop"},
            ]
            body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (ValueError, KeyError, OSError) as exc:
            self.server.errors.append(str(exc))
            self.send_error(400)


class Backend(AppServer):
    def __init__(self, python: Path, claude: Path | None, home: Path, cwd: Path,
                 fixture: Fixture, bridge: FakeRenderTestBridge) -> None:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
                   "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                   "PROCESSOR_ARCHITECTURE"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update(CLAUDE_CONFIG_DIR=str(home), ANTHROPIC_API_KEY=KEY,
                   ANTHROPIC_BASE_URL=f"http://127.0.0.1:{fixture.server_port}",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1",
                   DISABLE_ERROR_REPORTING="1", ENABLE_TOOL_SEARCH="false")
        command = [str(python), "-m", "rendertest_mcp.claude_backend", "--mcp-python", str(python),
                   "--instance", bridge.instance_id, "--capture-session", bridge.capture_session_id,
                   "--agent-root", str(bridge.root)]
        if claude:
            command += ["--claude-path", str(claude)]
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW
                                        if os.name == "nt" else 0)
        # The bridge waits for initialize, so no SDK process starts before it enters the job.
        self.close_job = supervise_process(self.process)
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.notifications: list[dict[str, Any]] = []
        self.stderr: list[str] = []
        self.request_id = 0
        self.readers = [threading.Thread(target=self.read_stdout, daemon=True),
                        threading.Thread(target=self.read_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def close(self) -> None:
        children = process_tree(self.process.pid) if os.name == "nt" and self.process.poll() is None else {}
        try:
            super().close()
        finally:
            if self.close_job:
                self.close_job()
                self.close_job = None
        deadline = time.monotonic() + 3
        survivors = [pid for pid in children if alive(pid)]
        while survivors and time.monotonic() < deadline:
            time.sleep(0.1)
            survivors = [pid for pid in children if alive(pid)]
        if survivors:
            # Only descendants observed under this fixture's own backend process may be stopped.
            for pid in survivors:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            raise RuntimeError("Claude backend left child processes after EOF: " + repr({pid: children[pid] for pid in survivors}))

    def turn(self, thread_id: str, question: str, answer: str) -> None:
        result = self.request("turn/start", {"threadId": thread_id,
                              "input": [{"type": "text", "text": question, "text_elements": []}]})
        turn_id = result["turn"]["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            finished = next((entry["params"]["turn"] for entry in self.notifications
                             if entry.get("method") == "turn/completed" and
                             entry["params"].get("threadId") == thread_id and
                             entry["params"]["turn"].get("id") == turn_id), None)
            if finished:
                if finished.get("status") != "completed":
                    raise RuntimeError("Claude turn failed: " + json.dumps(finished))
                items = {entry["params"]["item"]["id"]: entry["params"]["item"].get("text")
                         for entry in self.notifications if entry.get("method") == "item/completed" and
                         entry["params"].get("turnId") == turn_id and
                         entry["params"].get("item", {}).get("type") == "agentMessage"}
                if list(items.values()) != [answer]:
                    raise RuntimeError("Claude rendered missing/duplicate answer items: " + repr(items))
                return
            self.receive(deadline - time.monotonic())
        raise RuntimeError("Claude turn completion timed out")


def check(python: Path, claude: Path | None) -> dict[str, Any]:
    base = REPO / "agent/.agent-data/claude-check"
    base.mkdir(parents=True, exist_ok=True)
    fixture = Fixture()
    worker = threading.Thread(target=fixture.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="run-", dir=base) as temporary:
            root = Path(temporary)
            home, cwd = root / "claude-home", root / "workspace"
            home.mkdir()
            cwd.mkdir()
            params = {"cwd": str(cwd), "model": "claude-sonnet-4-6", "sandbox": "read-only",
                      "approvalPolicy": "never", "config": {"mcp_servers.rendertest.enabled": True},
                      "developerInstructions": "Local protocol fixture. Use only the configured RenderTest tools."}
            policy = {"toolPolicy": "rendertest-only", "builtinTools": False,
                      "externalMcp": False, "approvals": "deny"}
            capture = root / "fixture.rdc"
            capture.write_bytes(b"Local fake capture identity; never passed to a replay API.")
            bridge = FakeRenderTestBridge(root / "bridge")
            bridge.capture_path = str(capture)
            with bridge:
                first = Backend(python, claude, home, cwd, fixture, bridge)
                try:
                    first.initialize()
                    response = first.request("thread/start", params)
                    thread_id = response["thread"]["id"]
                    if response.get("policy") != policy:
                        raise RuntimeError("backend did not confirm its tool policy")
                    first.turn(thread_id, FIRST, ANSWER1)
                    tools = [event["params"]["item"] for event in first.notifications
                             if event.get("method") == "item/completed" and
                             event["params"].get("item", {}).get("type") == "mcpToolCall"]
                    if len(tools) != 1 or tools[0].get("tool") != "export_texture":
                        raise RuntimeError("real harness did not emit exactly one completed MCP tool")
                    serialized = json.dumps(tools[0])
                    if "artifacts" not in serialized or "sha256" not in serialized or bridge.capture_session_id not in serialized:
                        raise RuntimeError("MCP tool completion lost artifact metadata or capture identity")
                finally:
                    first.close()
                second = Backend(python, claude, home, cwd, fixture, bridge)
                try:
                    second.initialize()
                    response = second.request("thread/resume", dict(params, threadId=thread_id, excludeTurns=True))
                    if response["thread"]["id"] != thread_id or response.get("policy") != policy:
                        raise RuntimeError("resume changed the native session identity or tool restrictions")
                    second.turn(thread_id, SECOND, ANSWER2)
                finally:
                    second.close()
                if fixture.errors or len(fixture.requests) != 3:
                    raise RuntimeError(f"fixture failed: {fixture.errors}; requests={len(fixture.requests)}")
                messages = json.dumps(fixture.requests[-1]["messages"])
                if not all(marker in messages for marker in (FIRST, ANSWER1, SECOND)):
                    raise RuntimeError("resumed request lost earlier conversation context")
                if first.process.returncode != 0 or second.process.returncode != 0:
                    raise RuntimeError("Claude backend did not exit cleanly on stdin EOF")
                return {"backend": "claude", "localModelRequests": 3, "mcpTextureArtifact": True,
                        "restoredConversation": True, "isolatedTools": True, "cleanExit": True}
    finally:
        fixture.shutdown()
        fixture.server_close()
        worker.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--claude", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.python.resolve(), args.claude), indent=2))
        print("PASS: real Claude harness, local MCP artifact, cross-process resume; localhost model fixture only")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("FAIL:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
