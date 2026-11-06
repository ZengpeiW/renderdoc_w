"""Verify real Codex thread persistence using a localhost-only Responses SSE fixture.

No real model, account API, browser, credentials, or external HTTP endpoint is used. Two real
app-server processes share a fresh temporary CODEX_HOME and an empty working directory. The
first finishes a fixture turn; the second resumes its rollout and sends another turn. The local
HTTP fixture checks that the second request contains the first user/assistant exchange.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
from typing import Any

from appserver_schema_check import FEATURES, find_codex

REPO = Path(__file__).resolve().parents[2]
FIRST_QUESTION = "LOCAL_HISTORY_FIRST_QUESTION"
FIRST_ANSWER = "LOCAL_HISTORY_FIRST_ANSWER"
SECOND_QUESTION = "LOCAL_HISTORY_SECOND_QUESTION"
SECOND_ANSWER = "LOCAL_HISTORY_SECOND_ANSWER"


class ResponsesFixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), ResponsesHandler)
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []


class ResponsesHandler(BaseHTTPRequestHandler):
    server: ResponsesFixture

    def log_message(self, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != "/v1/responses":
            self.server.errors.append("unexpected local POST " + self.path)
            self.send_error(404)
            return
        if self.headers.get("Authorization"):
            self.server.errors.append("unexpected authorization header at the local fixture")
            self.send_error(400)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4 * 1024 * 1024:
                raise ValueError("unexpected request size")
            if self.headers.get("Content-Encoding") not in (None, "identity"):
                raise ValueError("unexpected request encoding " + self.headers["Content-Encoding"])
            request = json.loads(self.rfile.read(length))
            self.server.requests.append(request)
            index = len(self.server.requests)
            if index > 2 or request.get("stream") is not True:
                raise ValueError("expected exactly two streaming requests")
        except (ValueError, UnicodeError) as exc:
            self.server.errors.append(str(exc))
            self.send_error(400)
            return

        answer = FIRST_ANSWER if index == 1 else SECOND_ANSWER
        response_id, item_id = f"resp_local_{index}", f"msg_local_{index}"
        content = {"type": "output_text", "text": answer, "annotations": []}
        item = {"type": "message", "id": item_id, "role": "assistant", "status": "completed",
                "content": [content]}
        response = {"id": response_id, "object": "response", "created_at": int(time.time()),
                    "model": "history-probe-model", "status": "completed", "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}
        events = [
            {"type": "response.created", "response": dict(response, status="in_progress", output=[])},
            {"type": "response.output_item.added", "output_index": 0,
             "item": dict(item, status="in_progress", content=[])},
            {"type": "response.content_part.added", "item_id": item_id, "output_index": 0,
             "content_index": 0, "part": dict(content, text="")},
            {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0,
             "content_index": 0, "delta": answer},
            {"type": "response.output_text.done", "item_id": item_id, "output_index": 0,
             "content_index": 0, "text": answer},
            {"type": "response.content_part.done", "item_id": item_id, "output_index": 0,
             "content_index": 0, "part": content},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        body = "".join(f"event: {event['type']}\ndata: {json.dumps(dict(event, sequence_number=n))}\n\n"
                       for n, event in enumerate(events)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


class AppServer:
    def __init__(self, codex: Path, home: Path, cwd: Path, endpoint: str) -> None:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
                   "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                   "PROCESSOR_ARCHITECTURE"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update(CODEX_HOME=str(home), OTEL_SDK_DISABLED="true")
        provider = ('{name="Local history fixture",base_url=' + json.dumps(endpoint) +
                    ',wire_api="responses",requires_openai_auth=false,request_max_retries=0,'
                    'stream_max_retries=0}')
        overrides = ["mcp_servers={}", 'model_provider="history_fixture"',
                     'model="history-probe-model"', "model_providers.history_fixture=" + provider]
        overrides += [f"features.{feature}=false" for feature in FEATURES]
        command = [str(codex), "app-server"]
        for override in overrides:
            command += ["-c", override]
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW
                                        if os.name == "nt" else 0)
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.notifications: list[dict[str, Any]] = []
        self.stderr: list[str] = []
        self.request_id = 0
        self.readers = [threading.Thread(target=self.read_stdout, daemon=True),
                        threading.Thread(target=self.read_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                self.messages.put({"parseError": line[:200]})
        self.messages.put(None)

    def read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr.append(line)
            if len(self.stderr) > 64:
                del self.stderr[0]

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def receive(self, timeout: float) -> dict[str, Any]:
        try:
            message = self.messages.get(timeout=max(0.01, timeout))
        except queue.Empty as exc:
            raise RuntimeError("app-server timeout: " + "".join(self.stderr[-8:])) from exc
        if message is None or "parseError" in message:
            raise RuntimeError(f"app-server closed or invalid JSON: {message}; {''.join(self.stderr[-8:])}")
        if "method" in message:
            if "id" in message:
                self.send({"id": message["id"], "error": {"code": -32601,
                           "message": "history fixture does not grant tool requests"}})
                raise RuntimeError("unexpected server request " + message["method"])
            self.notifications.append(message)
        return message

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        self.send({"id": self.request_id, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            message = self.receive(deadline - time.monotonic())
            if message.get("id") == self.request_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message["result"]
        raise RuntimeError(method + " timed out")

    def initialize(self) -> None:
        self.request("initialize", {"clientInfo": {"name": "rendertest_history_check", "version": "1"},
                                     "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized", "params": {}})

    def turn(self, thread_id: str, question: str, answer: str) -> None:
        result = self.request("turn/start", {"threadId": thread_id,
                              "input": [{"type": "text", "text": question, "text_elements": []}]})
        turn_id = result["turn"]["id"]
        deadline = time.monotonic() + 30
        while True:
            finished = next((entry["params"]["turn"] for entry in self.notifications
                             if entry.get("method") == "turn/completed" and
                             entry["params"].get("threadId") == thread_id and
                             entry["params"]["turn"].get("id") == turn_id), None)
            if finished:
                if finished.get("status") != "completed":
                    raise RuntimeError(f"turn did not complete: {finished}; {''.join(self.stderr[-8:])}")
                texts = [entry["params"]["item"].get("text") for entry in self.notifications
                         if entry.get("method") == "item/completed" and
                         entry["params"].get("turnId") == turn_id and
                         entry["params"]["item"].get("type") == "agentMessage"]
                if texts != [answer]:
                    raise RuntimeError(f"mock response was not emitted as an agent message: {texts}")
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("turn completion timed out")
            self.receive(deadline - time.monotonic())

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=2)
        for stream in (self.process.stdout, self.process.stderr):
            if stream:
                stream.close()


def content_text(request: dict[str, Any], role: str) -> str:
    return "\n".join(part.get("text", "") for item in request.get("input", [])
                     if item.get("role") == role for part in item.get("content", [])
                     if isinstance(part, dict))


def check(codex: Path) -> dict[str, Any]:
    base = REPO / "agent/.agent-data/history-check"
    base.mkdir(parents=True, exist_ok=True)
    fixture = ResponsesFixture()
    worker = threading.Thread(target=fixture.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="run-", dir=base) as temporary:
            root = Path(temporary)
            home, cwd = root / "codex-home", root / "empty-workspace"
            home.mkdir()
            cwd.mkdir()
            endpoint = f"http://127.0.0.1:{fixture.server_port}/v1"
            params = {"cwd": str(cwd), "model": "history-probe-model", "modelProvider": "history_fixture",
                      "sandbox": "read-only", "approvalPolicy": "never", "config": {"mcp_servers": {}},
                      "developerInstructions": "This is a local history protocol fixture. No tools are needed."}
            first = AppServer(codex, home, cwd, endpoint)
            try:
                first.initialize()
                response = first.request("thread/start", dict(params, ephemeral=False))
                thread_id = response["thread"]["id"]
                rollout = Path(response["thread"]["path"])
                first.turn(thread_id, FIRST_QUESTION, FIRST_ANSWER)
            finally:
                first.close()
            if not rollout.is_relative_to(home) or not rollout.is_file():
                raise RuntimeError("completed thread has no rollout in the isolated CODEX_HOME")
            second = AppServer(codex, home, cwd, endpoint)
            try:
                second.initialize()
                response = second.request("thread/resume", dict(params, threadId=thread_id, excludeTurns=True))
                if (response["thread"]["id"] != thread_id or response["thread"]["turns"] != [] or
                        response.get("sandbox", {}).get("type") != "readOnly" or
                        response.get("approvalPolicy") != "never"):
                    raise RuntimeError("resume changed thread identity, replayed turns, or changed permissions")
                second.turn(thread_id, SECOND_QUESTION, SECOND_ANSWER)
            finally:
                second.close()
            if fixture.errors or len(fixture.requests) != 2:
                raise RuntimeError(f"HTTP fixture errors/requests: {fixture.errors}/{len(fixture.requests)}")
            first_user = content_text(fixture.requests[0], "user")
            second_user = content_text(fixture.requests[1], "user")
            second_assistant = content_text(fixture.requests[1], "assistant")
            if first_user.count(FIRST_QUESTION) != 1 or SECOND_QUESTION in first_user:
                raise RuntimeError("first request did not contain only its initial question")
            if (second_user.count(FIRST_QUESTION) != 1 or second_user.count(SECOND_QUESTION) != 1 or
                    second_assistant.count(FIRST_ANSWER) != 1):
                raise RuntimeError("resumed model input omitted or duplicated the first user/assistant exchange")
            if first.process.returncode != 0 or second.process.returncode != 0:
                raise RuntimeError("app-server did not shut down cleanly after the completed turn")
            return {"threadId": thread_id, "localRequests": 2, "restoredUserQuestion": True,
                    "restoredAssistantAnswer": True, "excludeTurns": True,
                    "processExitCodes": [first.process.returncode, second.process.returncode]}
    finally:
        fixture.shutdown()
        fixture.server_close()
        worker.join(timeout=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codex", type=Path)
    args = parser.parse_args(argv)
    try:
        result = check(find_codex(args.codex))
        print("PASS: real app-server restored a completed thread across processes using only localhost SSE")
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("FAIL:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
