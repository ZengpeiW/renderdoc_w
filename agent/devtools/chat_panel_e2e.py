"""End-to-end checks for the qrendertest AI panel (M5a-M5c).

Usage:
    python devtools/chat_panel_e2e.py [--scenario all|A1|A2|...] [--exe qrendertest.exe]
    python devtools/chat_panel_e2e.py --real --capture FILE.rdc [--question TEXT] [--ask-pixel]

Fake mode points the panel at ``fake_app_server.py`` through RENDERTEST_AGENT_CODEX_COMMAND, drives
it with the RENDERTEST_AGENT_CHAT_AUTOTEST hook and asserts on both the transcript the panel writes
and the messages the fake received. Real mode uses the installed Codex CLI against a capture, then
checks the tool calls, the stop latency and that no codex or MCP process outlives qrendertest.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = AGENT_DIR.parent
DEFAULT_EXE = REPO_DIR / "x64" / "Release" / "qrendertest.exe"
DATA_DIR = AGENT_DIR / ".agent-data" / "chat-e2e"
FAKE = Path(__file__).resolve().parent / "fake_app_server.py"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_app_server import ANSWER  # noqa: E402
from smoke_live import close_windows  # noqa: E402

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
DEFAULT_REAL_QUESTION = "这个 pass 在做什么？请先用工具查看当前事件再回答。"
DEFAULT_REAL_EVENT = 1513


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def process_tree(root_pid: int) -> dict[int, str]:
    """Returns {pid: exe name} for every descendant of root_pid. Raises if it cannot enumerate."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        raise OSError("CreateToolhelp32Snapshot failed")
    entries: list[tuple[int, int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            entries.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not entries:
        raise OSError("the process snapshot is empty")

    tree: dict[int, str] = {}
    parents = {root_pid}
    while parents:
        children = {pid: name for pid, parent, name in entries
                    if parent in parents and pid not in tree and pid != root_pid}
        tree.update(children)
        parents = set(children)
    return tree


def alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Run:
    def __init__(self, name: str, exe: Path, root: Path):
        self.name = name
        self.exe = exe
        self.root = root
        self.transcript_path = root / "transcript.json"
        self.fake_log = root / "fake.jsonl"
        self.failures: list[str] = []
        self.transcript: dict[str, Any] = {}
        self.process: subprocess.Popen | None = None

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(message)

    def launch(self, autotest: dict[str, Any], env: dict[str, str], args: list[str] | None = None) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        autotest = dict(autotest, transcript=str(self.transcript_path))
        environ = dict(os.environ)
        environ.pop("RENDERTEST_AGENT_CODEX_COMMAND", None)
        environ.pop("RENDERTEST_MCP_PYTHON", None)
        environ["RENDERTEST_AGENT_ROOT"] = str(self.root / "agent-root")
        environ["RENDERTEST_AGENT_CHAT_AUTOTEST"] = json.dumps(autotest, ensure_ascii=False)
        environ.update(env)
        self.process = subprocess.Popen([str(self.exe), *(args or [])], env=environ)

    def wait_transcript(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        started = self.root / "agent-root" / "logs"
        start_deadline = time.time() + min(timeout, 600)
        while time.time() < deadline:
            if self.transcript_path.exists():
                self.transcript = json.loads(self.transcript_path.read_text(encoding="utf-8"))
                return True
            if self.process and self.process.poll() is not None:
                self.failures.append(f"qrendertest exited early with {self.process.returncode}")
                return False
            if time.time() > start_deadline and not list(started.glob("chat-*.log")):
                self.failures.append("the AI panel never started an app-server (autotest not running?)")
                return False
            time.sleep(0.2)
        self.failures.append(f"no transcript after {timeout:.0f}s")
        return False

    def close(self) -> None:
        if not self.process:
            return
        deadline = time.time() + 30
        while self.process.poll() is None and time.time() < deadline:
            close_windows(self.process.pid)
            time.sleep(0.5)
        if self.process.poll() is None:
            self.failures.append("qrendertest did not close; killing it")
            self.process.kill()
            self.process.wait()

    # helpers over the transcript and the fake log
    def items(self, kind: str) -> list[dict[str, Any]]:
        return [item for item in self.transcript.get("items", []) if item.get("kind") == kind]

    def results(self) -> list[dict[str, Any]]:
        return self.transcript.get("autotest", {}).get("results", [])

    def session(self) -> dict[str, Any]:
        return self.transcript.get("session", {})

    def received(self) -> list[dict[str, Any]]:
        return [entry["message"] for entry in read_jsonl(self.fake_log) if entry.get("event") == "recv"]

    def fake_pids(self) -> list[int]:
        return [entry["pid"] for entry in read_jsonl(self.fake_log) if entry.get("event") == "start"]

    def check_no_survivors(self, pids: list[int], within: float = 3.0) -> None:
        deadline = time.time() + within
        while time.time() < deadline and any(alive(pid) for pid in pids):
            time.sleep(0.1)
        survivors = [pid for pid in pids if alive(pid)]
        self.check(not survivors, f"processes still alive: {survivors}")


def fake_env(run: Run, scenario: str) -> dict[str, str]:
    command = f'"{sys.executable}" "{FAKE}" --scenario {scenario} --log "{run.fake_log}" --state-dir "{run.root}"'
    return {"RENDERTEST_AGENT_CODEX_COMMAND": command, "RENDERTEST_AGENT_ISOLATE": "1"}


def methods(messages: list[dict[str, Any]]) -> list[str]:
    return [message["method"] for message in messages if "method" in message]


def scenario_a1(run: Run) -> None:
    run.launch({"questions": ["hello"]}, fake_env(run, "stream"))
    if not run.wait_transcript(60):
        return
    received = run.received()
    order = methods(received)
    expected = ["initialize", "initialized", "account/read", "skills/extraRoots/set", "config/read",
                "thread/start", "mcpServerStatus/list", "turn/start"]
    session_order = [method for method in order if method != "model/list"]
    run.check(session_order[:len(expected)] == expected, f"request order {order}")
    start = next((m["params"] for m in received if m.get("method") == "thread/start"), {})
    instance = run.session().get("instanceId")
    run.check(bool(instance), "panel did not find the bridge instance id")
    run.check(start.get("sandbox") == "read-only" and start.get("approvalPolicy") == "never",
              f"thread/start sandbox/approval {start.get('sandbox')}/{start.get('approvalPolicy')}")
    run.check(start.get("ephemeral") is True, "thread/start is not ephemeral")
    run.check(bool(instance) and instance in start.get("developerInstructions", ""),
              "developerInstructions lack the instance id")
    run.check(start.get("config", {}).get("mcp_servers.blender.enabled") is False,
              f"other MCP servers not disabled: {start.get('config')}")
    run.check(start.get("config", {}).get("mcp_servers.rendertest.enabled") is False,
              "capture tools were enabled without a capture")
    argv = next((e["argv"] for e in read_jsonl(run.fake_log) if e.get("event") == "start"), [])
    joined = " ".join(argv)
    run.check("mcp_servers.rendertest.enabled=false" in joined, "unbound MCP was not disabled at launch")
    run.check("RENDERTEST_AGENT_FIXED_INSTANCE_ID" not in joined, "pure chat should not start a bound MCP")
    run.check("features.shell_tool=false" in joined, "launch args are not isolated")
    turn = next((m["params"] for m in received if m.get("method") == "turn/start"), {})
    text = (turn.get("input") or [{}])[0].get("text", "")
    run.check(text.startswith("<rendertest_context>") and text.endswith("hello"), f"turn text {text!r}")
    agents = run.items("agent")
    run.check(len(agents) == 1 and agents[0].get("text") == ANSWER, f"agent message {agents}")
    run.check(any(i.get("type") == "reasoning" for i in run.items("reasoning")), "no reasoning item")
    run.check([r.get("status") for r in run.results()] == ["completed"], f"results {run.results()}")
    run.check(run.transcript.get("totalTokens") == 4242, "token usage not applied")
    run.check(run.session().get("mcpStatus") == "disabled" and run.session().get("mcpTools") == 0,
              f"mcp status {run.session().get('mcpStatus')} {run.session().get('mcpTools')}")


def scenario_a2(run: Run) -> None:
    run.launch({"questions": ["which event?"]}, fake_env(run, "tool"))
    if not run.wait_transcript(60):
        return
    tools = run.items("tool")
    run.check(len(tools) == 1, f"tool items {tools}")
    if tools:
        tool = tools[0]
        run.check(tool.get("tool") == "get_context" and tool.get("server") == "rendertest", f"tool {tool}")
        run.check(tool.get("status") == "completed" and tool.get("durationMs") == 123, f"tool {tool}")
        run.check(not tool.get("progress"), f"completed tool retained progress text: {tool.get('progress')}")
        run.check('"eventId":1513' in tool.get("result", ""), f"result {tool.get('result')}")
    run.check([r.get("status") for r in run.results()] == ["completed"], f"results {run.results()}")


def scenario_a3(run: Run) -> None:
    run.launch({"questions": ["try to write"]}, fake_env(run, "approvals"))
    if not run.wait_transcript(60):
        return
    replies = {m["id"]: m for m in run.received() if "method" not in m and isinstance(m.get("id"), int)
               and m["id"] >= 1000}
    expected = {
        1000: {"result": {"decision": "decline"}},
        1001: {"result": {"decision": "decline"}},
        1002: {"result": {"permissions": {}, "scope": "turn"}},
        1003: {"result": {"action": "decline", "content": None, "_meta": None}},
    }
    for request_id, body in expected.items():
        reply = replies.get(request_id, {})
        run.check(reply.get("result") == body["result"], f"reply {request_id}: {reply}")
    run.check(replies.get(1004, {}).get("error", {}).get("code") == -32601,
              f"requestUserInput reply {replies.get(1004)}")
    run.check(len(run.session().get("serverRequests", [])) == 5, "server requests not recorded")
    run.check([r.get("status") for r in run.results()] == ["completed"], f"results {run.results()}")


def scenario_a4(run: Run) -> None:
    run.launch({"questions": ["slow question", "follow up"], "stopOnTool": "pixel_history",
                "stopQuestion": 0}, fake_env(run, "interrupt"))
    if not run.wait_transcript(60):
        return
    results = run.results()
    run.check([r.get("status") for r in results] == ["interrupted", "completed"], f"results {results}")
    latency = results[0].get("stopLatencyMs", 1e9) if results else 1e9
    run.check(latency < 2000, f"stop latency {latency}")
    run.check("turn/interrupt" in methods(run.received()), "turn/interrupt not sent")
    run.check(len(run.session().get("threadIds", [])) == 1, "graceful stop should keep the thread")


def scenario_a4_hang(run: Run) -> None:
    run.launch({"questions": ["slow question", "follow up"], "stopOnTool": "pixel_history",
                "stopQuestion": 0}, fake_env(run, "hang"))
    if not run.wait_transcript(60):
        return
    results = run.results()
    run.check([r.get("status") for r in results] == ["interrupted", "completed"], f"results {results}")
    latency = results[0].get("stopLatencyMs", 1e9) if results else 1e9
    run.check(latency < 2600, f"forced stop latency {latency}")
    pids = run.fake_pids()
    run.check(len(pids) == 2, f"expected a restarted app-server, pids {pids}")
    if pids:
        run.check(not alive(pids[0]), f"hung app-server {pids[0]} still alive")
    run.check(len(run.session().get("threadIds", [])) == 2, "no new thread after forced stop")


def scenario_a5(run: Run) -> None:
    run.launch({"questions": ["first", "second"]}, fake_env(run, "crash-once"))
    if not run.wait_transcript(60):
        return
    results = run.results()
    run.check([r.get("status") for r in results] == ["failed", "completed"], f"results {results}")
    notices = " ".join(i.get("text", "") for i in run.items("notice"))
    run.check("simulated crash" in notices, "stderr tail not shown after crash")
    run.check(len(run.session().get("threadIds", [])) == 2, "no new thread after crash")
    run.check(len(run.fake_pids()) == 2, f"app-server not restarted: {run.fake_pids()}")


def scenario_a6_logged_out(run: Run) -> None:
    run.launch({"questions": ["hello"]}, fake_env(run, "logged-out"))
    if not run.wait_transcript(60):
        return
    run.check(run.session().get("state") == "notLoggedIn", f"state {run.session().get('state')}")
    run.check([r.get("status") for r in run.results()] == ["failed"], f"results {run.results()}")
    run.check("thread/start" not in methods(run.received()), "thread started while logged out")
    run.check("Click Log in" in " ".join(i.get("text", "") for i in run.items("notice")),
              "no hint for the panel's login entry")
    run.check(run.session().get("loginVisible") is True and run.session().get("loginEnabled") is True,
              "logged-out panel has no usable login button")
    run.check(run.session().get("appServerRunning") is True,
              "logged-out panel discarded the initialized login process")


def scenario_a6_bad_codex(run: Run) -> None:
    missing = run.root / "missing" / "codex.exe"
    run.launch({"questions": ["hello"]}, {"RENDERTEST_AGENT_CODEX_COMMAND": str(missing)})
    if not run.wait_transcript(60):
        return
    run.check(run.session().get("state") == "error", f"state {run.session().get('state')}")
    run.check("does not exist" in " ".join(i.get("text", "") for i in run.items("notice")), "no guidance")


def scenario_a6_bad_python(run: Run) -> None:
    env = fake_env(run, "stream")
    env["RENDERTEST_MCP_PYTHON"] = str(run.root / "missing" / "python.exe")
    run.launch({"questions": ["hello"]}, env)
    if not run.wait_transcript(60):
        return
    run.check(run.session().get("state") == "ready", f"state {run.session().get('state')}")
    run.check(run.session().get("mcpStatus") == "disabled", "plain chat enabled capture MCP")
    run.check([r.get("status") for r in run.results()] == ["completed"], f"results {run.results()}")


def scenario_exit(run: Run) -> None:
    run.launch({"questions": ["never finishes"]}, fake_env(run, "hang"))
    deadline = time.time() + 60
    while time.time() < deadline and "turn/start" not in methods(run.received()):
        time.sleep(0.2)
    pids = run.fake_pids()
    run.check(bool(pids), "fake app-server never started a turn")
    run.close()
    run.check_no_survivors(pids)


PIXEL_CHIP = {
    "kind": "pixel",
    "label": "Pixel (256, 256) of Lumen.CardCaptureAlbedoAtlas",
    "selection": {"kind": "pixel", "resource_id": "116618", "x": 256, "y": 256, "origin": "top-left",
                  "mip": 0, "slice": 0, "sample": 0, "view": 0, "event_id": 1513},
}


def turn_texts(run: Run) -> list[str]:
    return [(m["params"].get("input") or [{}])[0].get("text", "")
            for m in run.received() if m.get("method") == "turn/start"]


def selections(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[len("selection: "):]) for line in text.splitlines()
            if line.startswith("selection: ")]


def scenario_b1_ask(run: Run) -> None:
    run.launch({"questions": ["", "follow up"],
                "asks": [{"source": "chip", "chip": PIXEL_CHIP, "question": "why is this pixel black?"}],
                "clickLinks": ["first-event", "rt://event/abc"]}, fake_env(run, "stream"))
    if not run.wait_transcript(60):
        return
    asks = run.transcript.get("autotest", {}).get("asks", [])
    run.check(len(asks) == 1 and asks[0].get("ok"), f"asks {asks}")
    if asks:
        run.check(asks[0].get("input") == "why is this pixel black?", f"prefill {asks[0].get('input')!r}")
        labels = [chip.get("label") for chip in asks[0].get("chips", [])]
        run.check(labels == [PIXEL_CHIP["label"]], f"chips after ask {labels}")
        run.check(asks[0].get("panelVisible") is True, "panel not visible after ask")
    texts = turn_texts(run)
    run.check(len(texts) == 2, f"turn texts {len(texts)}")
    if texts:
        run.check(selections(texts[0]) == [PIXEL_CHIP["selection"]], f"selection {selections(texts[0])}")
        run.check(texts[0].endswith("why is this pixel black?"), f"turn text {texts[0]!r}")
    if len(texts) > 1:
        run.check(selections(texts[1]) == [], "chips were not cleared after sending")
    users = run.items("user")
    run.check(len(users) == 2 and users[0].get("context") == [PIXEL_CHIP], f"user context {users[:1]}")
    run.check(len(users) == 2 and "context" not in users[1], f"second user context {users[1:]}")
    auto = run.transcript.get("autotest", {})
    links = auto.get("renderedLinks", [])
    bases = [link.split("?", 1)[0] for link in links]
    run.check("rt://event/1513" in bases and "rt://resource/116618" in bases, f"rendered links {links}")
    run.check(all(parse_qs(urlsplit(link).query).get("source") for link in links),
              f"references lack capture identity: {links}")
    clicks = auto.get("linkClicks", [])
    run.check(len(clicks) == 2, f"link clicks {clicks}")
    if len(clicks) == 2:
        run.check(clicks[0].get("link", "").startswith("rt://event/1513?") and clicks[0].get("ok") is False,
                  f"first-event click {clicks[0]}")
        run.check(clicks[1].get("ok") is False, f"malformed link click {clicks[1]}")
    notices = " ".join(i.get("text", "") for i in run.items("notice"))
    run.check("No capture is loaded" in notices and "Malformed link" in notices, f"link notices {notices!r}")
    run.check([r.get("status") for r in run.results()] == ["completed", "completed"], f"results {run.results()}")


def scenario_isolation_failure(run: Run) -> None:
    scenario = run.name.removeprefix("guard-")
    run.launch({"questions": ["hello"]}, fake_env(run, scenario))
    if not run.wait_transcript(60):
        return
    run.check([r.get("status") for r in run.results()] == ["failed"], f"results {run.results()}")
    run.check("turn/start" not in methods(run.received()), "a turn started despite unverifiable isolation")
    run.check(not run.session().get("appServerRunning"), "unsafe process is still running")
    if scenario == "status-page-foreign":
        pages = [m["params"].get("cursor") for m in run.received() if m.get("method") == "mcpServerStatus/list"]
        run.check(pages == [None, "page2"], f"did not inspect second status page: {pages}")


def scenario_status_pages(run: Run) -> None:
    run.launch({"questions": ["hello"]}, fake_env(run, "status-pages"))
    if not run.wait_transcript(60):
        return
    pages = [m["params"].get("cursor") for m in run.received() if m.get("method") == "mcpServerStatus/list"]
    run.check(pages == [None, "page2"], f"status pages {pages}")
    run.check([r.get("status") for r in run.results()] == ["completed"], f"results {run.results()}")


def scenario_start_timeout(run: Run) -> None:
    run.launch({"questions": ["first", "second"], "turnTimeoutMs": 250},
               fake_env(run, "turn-timeout-once"))
    if not run.wait_transcript(60):
        return
    run.check([r.get("status") for r in run.results()] == ["failed", "completed"], f"results {run.results()}")
    pids = run.fake_pids()
    run.check(len(pids) == 2, f"accepted remote turn was not terminated: {pids}")
    if pids:
        run.check(not alive(pids[0]), f"old process {pids[0]} still running")
    run.check(len(run.session().get("threadIds", [])) == 2, "no fresh thread after timeout")


def scenario_capture_change(run: Run) -> None:
    run.launch({"questions": ["first", "second"], "captureChangeAfterMs": 500}, fake_env(run, "hang"))
    if not run.wait_transcript(60):
        return
    run.check([r.get("status") for r in run.results()] == ["interrupted", "completed"], f"results {run.results()}")
    pids = run.fake_pids()
    run.check(len(pids) == 2 and not alive(pids[0]), f"capture change did not kill old process: {pids}")
    run.check(len(run.session().get("threadIds", [])) == 2, "capture change reused old thread")
    tools = run.items("tool")
    run.check(tools and tools[0].get("status") == "interrupted", f"pending tools {tools}")


def scenario_late_items(run: Run) -> None:
    run.launch({"questions": ["first", "second"]}, fake_env(run, "late-items"))
    if not run.wait_transcript(60):
        return
    run.check([i.get("text") for i in run.items("agent")] == [ANSWER, ANSWER], "stale notification contaminated transcript")


def scenario_late_pending(run: Run) -> None:
    scenario = run.name.removeprefix("guard-")
    run.launch({"questions": ["first", "second"]}, fake_env(run, scenario))
    if not run.wait_transcript(60):
        return
    expected = [ANSWER, "EARLY ITEM", ANSWER] if scenario == "late-before-start" else [ANSWER, ANSWER]
    run.check([i.get("text") for i in run.items("agent")] == expected,
              "late events claimed a pending turn, or a valid early item was discarded")
    run.check([r.get("status") for r in run.results()] == ["completed", "completed"], f"results {run.results()}")
    run.check(len(run.fake_pids()) == 1, "late response interrupted the next turn")


def scenario_disabled_bridge(run: Run) -> None:
    env = fake_env(run, "stream")
    env.update(RENDERTEST_AGENT_BRIDGE="0", RENDERTEST_AGENT_ISOLATE="0")
    run.launch({"questions": ["hello"]}, env)
    if not run.wait_transcript(60):
        return
    run.check(not run.session().get("instanceId"), "bridge-disabled process acquired an instance")
    run.check(run.session().get("mcpStatus") == "disabled", "unbound capture MCP was enabled")
    argv = next((e["argv"] for e in read_jsonl(run.fake_log) if e.get("event") == "start"), [])
    run.check("features.shell_tool=false" in argv and "features.unified_exec=false" in argv,
              "non-isolated mode enabled shell")
    run.check([r.get("status") for r in run.results()] == ["completed"], f"results {run.results()}")


def scenario_reference_scope(run: Run) -> None:
    run.launch({"questions": ["hello"], "captureChangeBeforeLinks": True, "clickLinks": ["first-event"]},
               fake_env(run, "explicit-links"))
    if not run.wait_transcript(60):
        return
    links = run.transcript.get("autotest", {}).get("renderedLinks", [])
    token = run.items("agent")[0].get("referenceToken") if run.items("agent") else None
    run.check(token and token != run.session().get("referenceToken"), "capture change retained old reference identity")
    run.check(links and all(parse_qs(urlsplit(link).query).get("source") == [token] for link in links),
              f"explicit or automatic reference escaped its message scope: {links}")
    run.check(all(not c.get("ok") for c in run.transcript.get("autotest", {}).get("linkClicks", [])),
              "stale reference navigated")


def scenario_command_status(run: Run) -> None:
    run.launch({"questions": ["hello"]}, fake_env(run, "command-status"))
    if not run.wait_transcript(60):
        return
    items = run.items("other")
    run.check(len(items) == 6 and all(f"({item['status']})" in item.get("text", "") for item in items),
              f"command/file status misrepresented: {items}")


def scenario_login(run: Run) -> None:
    scenario = run.name.removeprefix("C-")
    success = scenario == "login-success"
    settings: dict[str, Any] = {"questions": ["before login", "after login"] if success else ["before login"],
                                "autoLogin": True}
    if scenario == "login-cancel":
        settings["cancelLoginAfterMs"] = 50
    run.launch(settings, fake_env(run, scenario))
    if not run.wait_transcript(60):
        return
    received = run.received()
    login_requests = [m for m in received if m.get("method") == "account/login/start"]
    run.check(len(login_requests) == 1 and login_requests[0]["params"].get("type") == "chatgpt",
              f"login requests: {login_requests}")
    urls = run.session().get("authUrls", [])
    run.check(len(urls) == 1 and urls[0].startswith("https://example.invalid/rendertest-login/"),
              f"test browser URL was not captured: {urls}")
    expected_status = {"login-success": "completed", "login-failure": "failed", "login-cancel": "canceled"}[scenario]
    run.check(run.session().get("loginStatus") == expected_status, f"login state {run.session()}")
    expected_turns = ["failed", "completed"] if success else ["failed"]
    run.check([r.get("status") for r in run.results()] == expected_turns, f"results {run.results()}")
    if success:
        run.check(len(turn_texts(run)) == 1 and turn_texts(run)[0].endswith("after login"),
                  "failed pre-login question was sent or post-login question was lost")
        run.check(len([m for m in received if m.get("method") == "account/read"]) >= 2,
                  "account details were not refreshed after login")
    else:
        run.check("thread/start" not in methods(received) and "turn/start" not in methods(received),
                  "chat started without successful login")
    if scenario == "login-cancel":
        cancel = [m for m in received if m.get("method") == "account/login/cancel"]
        run.check(len(cancel) == 1 and bool(cancel[0]["params"].get("loginId")), f"cancel request {cancel}")
        late = [event for event in read_jsonl(run.fake_log) if event.get("event") == "late-login-completion"]
        run.check(len(late) == 1 and cancel and late[0].get("loginId") == cancel[0]["params"].get("loginId"),
                  "fixture did not send the canceled login's racing completion")
    if scenario == "login-failure":
        run.check("fake login rejected" in " ".join(i.get("text", "") for i in run.items("notice")),
                  "login error was not shown")


def scenario_auth_retry(run: Run) -> None:
    recovered = run.name in ("C-auth-recovered", "C-login-not-found")
    canceled = run.name == "C-login-cancel-retry"
    scenario = "login-cancel" if canceled else ("login-not-found" if run.name == "C-login-not-found" else
                                               "auth-recovered" if recovered else "logged-out")
    autotest: dict[str, Any] = {"questions": ["first attempt", "second attempt"]}
    if canceled or scenario == "login-not-found":
        autotest.update(autoLogin=True, cancelLoginAfterMs=50)
    run.launch(autotest, fake_env(run, scenario))
    if not run.wait_transcript(60):
        return
    received = run.received()
    run.check(methods(received).count("initialize") == 1, "existing app-server was initialized again")
    run.check(len(run.fake_pids()) == 1, "auth retry unnecessarily restarted the app-server")
    run.check(methods(received).count("account/read") >= 2, "auth retry did not refresh account state")
    expected = ["failed", "completed"] if recovered else ["failed", "failed"]
    run.check([r.get("status") for r in run.results()] == expected, f"results {run.results()}")
    run.check(len(turn_texts(run)) == (1 if recovered else 0), "pre-login prompt reached a model turn")
    if canceled:
        run.check(run.session().get("loginStatus") == "canceled", "late completion undid login cancellation")
    if scenario == "login-not-found":
        run.check(run.session().get("loginStatus") != "canceled", "notFound was reported as a canceled login")
        run.check(methods(received).count("account/login/cancel") == 1, "missing cancel race")


def scenario_account_updated(run: Run) -> None:
    run.launch({"questions": ["before account expires"]}, fake_env(run, "account-updated"))
    if not run.wait_transcript(60):
        return
    received = run.received()
    run.check(methods(received).count("initialize") == 1 and len(run.fake_pids()) == 1,
              "idle account transition unnecessarily restarted or reinitialized app-server")
    run.check(methods(received).count("account/read") >= 2, "account/updated did not refresh account state")
    run.check(run.session().get("state") == "notLoggedIn", f"account state {run.session().get('state')}")
    run.check(run.session().get("loginVisible") is True and run.session().get("loginEnabled") is True,
              "expired account has no usable login entry")
    run.check(run.session().get("appServerRunning") is True, "initialized login process was discarded")
    run.check(run.transcript.get("threadId") == "", "old account's thread remains active")
    run.check([r.get("status") for r in run.results()] == ["completed"], f"finished turn was changed: {run.results()}")


def scenario_model_settings(run: Run) -> None:
    catalog_error = run.name == "C-model-error"
    custom = run.name == "C-custom-model" or catalog_error
    selected_model = "custom-provider-model" if custom else "fake-deep"
    selected_effort = "custom-effort" if custom else "ultra"
    settings = {"codexPath": str(run.root / "missing-settings-codex.exe"),
                "model": selected_model, "reasoningEffort": selected_effort, "isolate": False}
    env = fake_env(run, "model-error" if catalog_error else "model-pages")
    # The test setting controls isolation; the explicit fake command still outranks codexPath.
    env.pop("RENDERTEST_AGENT_ISOLATE")
    autotest = {"questions": ["hello"], "settings": settings}
    if run.name == "C-model-settings":
        autotest["settingsScreenshot"] = str(run.root / "model-settings.png")
    run.launch(autotest, env)
    if not run.wait_transcript(60):
        return
    received = run.received()
    start = next((m["params"] for m in received if m.get("method") == "thread/start"), {})
    turn = next((m["params"] for m in received if m.get("method") == "turn/start"), {})
    run.check(start.get("model") == selected_model, f"thread model {start.get('model')}")
    run.check(turn.get("effort") == selected_effort, f"turn effort {turn.get('effort')}")
    run.check(run.session().get("selectedModel") == selected_model and
              run.session().get("selectedEffort") == selected_effort, "model/effort settings were not preserved")
    run.check([r.get("status") for r in run.results()] == ["completed"], f"results {run.results()}")
    argv = next((e["argv"] for e in read_jsonl(run.fake_log) if e.get("event") == "start"), [])
    run.check("features.shell_tool=false" in argv and "features.unified_exec=false" in argv,
              "settings isolation switch re-enabled shell tools")
    if not catalog_error:
        queries = [m["params"] for m in received if m.get("method") == "model/list"]
        run.check(any(q.get("cursor") == "models-page2" for q in queries), f"model pagination {queries}")
        models = run.session().get("models", [])
        run.check({m.get("model") for m in models} >= {"fake-model", "fake-deep"}, f"model catalog {models}")
        run.check(all(m.get("id") != m.get("model") for m in models), "fixture did not distinguish catalog id from model name")
    if run.name == "C-model-settings":
        dialog = run.transcript.get("autotest", {}).get("settingsDialog", {})
        run.check(dialog.get("model") == selected_model and dialog.get("effort") == selected_effort,
                  f"dialog used catalog id instead of model value: {dialog}")
        run.check(dialog.get("modelCount", 0) >= 3 and dialog.get("effortEditable") is False,
                  f"dialog did not use catalog-supported model/effort choices: {dialog}")


def scenario_tool_artifacts(run: Run) -> None:
    rejected = run.name == "C-tool-artifacts-rejected"
    run.launch({"questions": ["show the texture"], "clickLinks": ["first-tool", "first-artifact"]},
               fake_env(run, "tool-artifacts-rejected" if rejected else "tool-artifacts"))
    if not run.wait_transcript(60):
        return
    tools = run.items("tool")
    run.check(len(tools) == 1, f"tool cards {tools}")
    if tools:
        run.check("ARGUMENT_END_MARKER" in tools[0].get("arguments", ""), "expanded arguments were truncated to the old one-line limit")
        run.check(len(tools[0].get("result", "").encode("utf-8")) <= 4096, "tool result exceeded its display budget")
        run.check("valid.png" not in tools[0].get("result", ""), "fixture failed to put artifacts beyond display truncation")
        run.check(len(tools[0].get("artifacts", [])) == 4, "image metadata was lost to result truncation")
    auto = run.transcript.get("autotest", {})
    html = auto.get("renderedHtml", "")
    run.check("ARGUMENT_END_MARKER" in html and "RESULT_VISIBLE_MARKER" in html,
              "tool expansion did not show arguments and result preview")
    run.check(len(auto.get("expandedTools", [])) == 1, "tool card did not expand")
    run.check(html.count("<img ") == 1 and "agent-image://" in html, "PNG preview accepted unsafe files or omitted the valid image")
    run.check(html.count("Image unavailable") == 3, "unsafe or unreadable PNGs were not rejected")
    clicks = auto.get("linkClicks", [])
    run.check([click.get("ok") for click in clicks] == [True, not rejected], f"tool/artifact clicks {clicks}")
    if rejected:
        run.check(clicks and clicks[-1].get("link", "").startswith("agent-artifact://"),
                  "unsafe artifact was never clicked")


SCENARIOS: dict[str, Callable[[Run], None]] = {
    "A1": scenario_a1,
    "A2": scenario_a2,
    "A3": scenario_a3,
    "A4": scenario_a4,
    "A4-hang": scenario_a4_hang,
    "A5": scenario_a5,
    "A6-logged-out": scenario_a6_logged_out,
    "A6-bad-codex": scenario_a6_bad_codex,
    "A6-bad-python": scenario_a6_bad_python,
    "B1-ask": scenario_b1_ask,
    **{f"guard-{name}": scenario_isolation_failure for name in (
        "config-error", "config-malformed", "status-error", "status-foreign", "status-page-foreign", "status-loop")},
    "guard-status-pages": scenario_status_pages,
    "guard-start-timeout": scenario_start_timeout,
    "guard-capture-change": scenario_capture_change,
    "guard-late-items": scenario_late_items,
    "guard-late-before-start": scenario_late_pending,
    "guard-late-response": scenario_late_pending,
    "guard-disabled-bridge": scenario_disabled_bridge,
    "guard-reference-scope": scenario_reference_scope,
    "guard-command-status": scenario_command_status,
    "C-login-success": scenario_login,
    "C-login-failure": scenario_login,
    "C-login-cancel": scenario_login,
    "C-auth-retry": scenario_auth_retry,
    "C-auth-recovered": scenario_auth_retry,
    "C-login-cancel-retry": scenario_auth_retry,
    "C-login-not-found": scenario_auth_retry,
    "C-account-updated": scenario_account_updated,
    "C-model-settings": scenario_model_settings,
    "C-custom-model": scenario_model_settings,
    "C-model-error": scenario_model_settings,
    "C-tool-artifacts": scenario_tool_artifacts,
    "C-tool-artifacts-rejected": scenario_tool_artifacts,
    "exit": scenario_exit,
}


def run_real(args) -> int:
    run = Run("real", args.exe, DATA_DIR / "real")
    questions = [args.question, "请详细分析当前 capture 的每一个 pass，逐个调用工具检查。"]
    autotest = {"questions": questions, "waitForCapture": True, "stopQuestion": 1,
                "stopAfterMs": args.stop_after * 1000, "selectEvent": args.event,
                "captureTimeoutMs": int(args.timeout * 1000)}
    run.launch(autotest, {}, [str(args.capture.resolve())])
    ok = run.wait_transcript(args.timeout)
    tree = process_tree(run.process.pid) if run.process and run.process.poll() is None else {}
    print("process tree before close:", tree)
    names = {name.lower() for name in tree.values()}
    run.check("codex.exe" in names, f"codex.exe not under qrendertest: {tree}")
    run.check("python.exe" in names, f"MCP python not under qrendertest: {tree}")
    run.close()
    run.check_no_survivors(list(tree))
    if ok:
        tools = [item.get("tool") for item in run.items("tool")]
        results = run.results()
        print("tools:", tools)
        print("results:", results)
        for item in run.items("agent"):
            print("answer:", item.get("text", "")[:1500])
        run.check(run.session().get("mcpStatus") == "connected", f"mcp {run.session().get('mcpStatus')}")
        run.check(results[:1] and results[0].get("status") == "completed", f"Q1 {results[:1]}")
        run.check(any(tool in ("inspect_event", "get_context", "get_pipeline_state") for tool in tools),
                  f"no capture tools used: {tools}")
        run.check(all(item.get("server") == "rendertest" for item in run.items("tool")), "foreign tools used")
        run.check(not any(i.get("type") in ("commandExecution", "fileChange") for i in run.items("other")),
                  "shell or file items appeared")
        if len(results) > 1:
            run.check(results[1].get("status") == "interrupted", f"stop result {results[1]}")
            run.check(results[1].get("stopLatencyMs", 1e9) < 2000, f"stop latency {results[1]}")
        auto = run.transcript.get("autotest", {})
        run.check(auto.get("eventBefore") == args.event, f"event not selected: {auto.get('eventBefore')}")
        run.check(auto.get("eventBefore") == auto.get("eventAfter"),
                  f"UI event changed {auto.get('eventBefore')} -> {auto.get('eventAfter')}")
    report(run)
    return 1 if run.failures else 0


def report(run: Run) -> None:
    status = "FAIL" if run.failures else "ok"
    print(f"[{status}] {run.name}")
    for failure in run.failures:
        print(f"    - {failure}")


def run_real_ask(args) -> int:
    """Q2/Q3 through the Texture Viewer "Ask AI" pixel entry, then clicks the first EID link."""
    run = Run("real-ask", args.exe, DATA_DIR / "real-ask")
    rt0 = "116618"
    autotest = {
        "questions": ["这个像素为什么是这个值？是哪个事件写的？", "这个像素是当前 draw 的哪个图元画的？"],
        "asks": [{"source": "texturePixel", "resourceId": rt0, "x": 256, "y": 256},
                 {"source": "texturePixel", "resourceId": rt0, "x": 1, "y": 1}],
        "clickLinks": ["first-event"],
        "waitForCapture": True, "selectEvent": args.event, "captureTimeoutMs": int(args.timeout * 1000),
    }
    run.launch(autotest, {}, [str(args.capture.resolve())])
    ok = run.wait_transcript(args.timeout)
    tree = process_tree(run.process.pid) if run.process and run.process.poll() is None else {}
    run.close()
    run.check_no_survivors(list(tree))
    if ok:
        auto = run.transcript.get("autotest", {})
        asks = auto.get("asks", [])
        print("asks:", json.dumps(asks, ensure_ascii=False))
        run.check(len(asks) == 2 and all(a.get("ok") for a in asks), f"asks {asks}")
        texts = turn_texts_from_session(run)
        expected = [(256, 256), (1, 1)]
        for index, text in enumerate(texts[:2]):
            sel = selections(text)
            print(f"Q{index + 2} selection:", sel)
            run.check(len(sel) == 1 and sel[0].get("kind") == "pixel" and sel[0].get("resource_id") == rt0
                      and (sel[0].get("x"), sel[0].get("y")) == expected[index]
                      and sel[0].get("event_id") == args.event, f"Q{index + 2} selection {sel}")
        turns = split_turns(run)
        for index, (tools, answer) in enumerate(turns):
            print(f"Q{index + 2} tools:", tools)
            print(f"Q{index + 2} answer:", answer[:1500])
        results = run.results()
        print("results:", results)
        run.check([r.get("status") for r in results] == ["completed", "completed"], f"results {results}")
        if turns:
            tools, answer = turns[0]
            run.check("pixel_history" in tools, f"Q2 did not use pixel_history: {tools}")
            run.check("1459" in answer, "Q2 answer does not point at EID 1459")
        if len(turns) > 1:
            run.check("30161" in turns[1][1], "Q3 answer does not name primitive 30161")
        clicks = auto.get("linkClicks", [])
        print("links:", auto.get("renderedLinks"), "clicks:", clicks)
        run.check(len(clicks) == 1 and clicks[0].get("ok"), f"link click {clicks}")
        if clicks and clicks[0].get("link", "").startswith("rt://event/"):
            eid = int(urlsplit(clicks[0]["link"]).path.rsplit("/", 1)[1])
            run.check(clicks[0].get("eventAfter") == eid, f"link did not move the UI to {eid}: {clicks[0]}")
        run.check(auto.get("eventBefore") == auto.get("eventAfter") == args.event,
                  f"UI event changed before the click {auto.get('eventBefore')} -> {auto.get('eventAfter')}")
    report(run)
    return 1 if run.failures else 0


def turn_texts_from_session(run: Run) -> list[str]:
    return [text for text in run.session().get("turnTexts", []) if isinstance(text, str)]


def split_turns(run: Run) -> list[tuple[list[str], str]]:
    """Groups tool names and agent text per user message."""
    turns: list[tuple[list[str], str]] = []
    for item in run.transcript.get("items", []):
        kind = item.get("kind")
        if kind == "user":
            turns.append(([], ""))
        elif turns and kind == "tool":
            turns[-1][0].append(item.get("tool"))
        elif turns and kind == "agent":
            turns[-1] = (turns[-1][0], turns[-1][1] + item.get("text", "") + "\n")
    return turns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="all", help="all or one of: " + ", ".join(SCENARIOS))
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--ask-pixel", action="store_true", help="real mode: Q2/Q3 via the Ask AI pixel entry")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--question", default=DEFAULT_REAL_QUESTION)
    parser.add_argument("--event", type=int, default=DEFAULT_REAL_EVENT, help="event selected before asking")
    parser.add_argument("--stop-after", type=int, default=20, help="seconds before stopping the 2nd real question")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)

    if args.real:
        if not args.capture:
            parser.error("--real needs --capture")
        return run_real_ask(args) if args.ask_pixel else run_real(args)

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    failed = 0
    for name in names:
        run = Run(name, args.exe, DATA_DIR / name)
        try:
            SCENARIOS[name](run)
        finally:
            run.close()
        report(run)
        failed += bool(run.failures)
    print(f"{len(names) - failed}/{len(names)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
