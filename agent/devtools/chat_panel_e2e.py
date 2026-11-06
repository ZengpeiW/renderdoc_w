"""End-to-end checks for the qrendertest AI panel (M5a-M5d and Claude harness).

Usage:
    python devtools/chat_panel_e2e.py [--scenario all|M5d|model-settings|claude|A1|A2|...] [--exe qrendertest.exe]
    python devtools/chat_panel_e2e.py --history-capture --capture FILE.rdc [--event EID]
    python devtools/chat_panel_e2e.py --real --capture FILE.rdc [--question TEXT] [--ask-pixel]

Fake mode points the panel at ``fake_app_server.py`` through the selected backend's command override, drives
it with the RENDERTEST_AGENT_CHAT_AUTOTEST hook and asserts on both the transcript the panel writes
and the messages the fake received. Real mode uses the installed Codex CLI against a capture, then
checks the tool calls, the stop latency and that no codex or MCP process outlives qrendertest.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
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
        self.log_offset = 0

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(message)

    def launch(self, autotest: dict[str, Any], env: dict[str, str], args: list[str] | None = None,
               *, preserve: bool = False) -> None:
        if self.process and self.process.poll() is None:
            raise RuntimeError("close the previous qrendertest before relaunching")
        # Never recursively remove a caller-supplied location outside this test workspace.
        allowed = (AGENT_DIR / ".agent-data").resolve()
        target = self.root.resolve()
        if target == allowed or allowed not in target.parents:
            raise ValueError(f"test root must be below {allowed}: {target}")
        if self.root.exists() and not preserve:
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.transcript_path.unlink(missing_ok=True)
        self.transcript = {}
        self.log_offset = len(read_jsonl(self.fake_log))
        autotest = dict(autotest, transcript=str(self.transcript_path))
        # Make test backend/model selection independent of the developer's persisted settings
        # and environment. Individual scenarios explicitly supply the backend under test.
        autotest["settings"] = dict({"backend": "codex"}, **autotest.get("settings", {}))
        environ = dict(os.environ)
        for name in ("RENDERTEST_AGENT_CODEX_COMMAND", "RENDERTEST_AGENT_CLAUDE_COMMAND",
                     "RENDERTEST_AGENT_BACKEND", "RENDERTEST_MCP_PYTHON", "RENDERTEST_AGENT_MODEL",
                     "RENDERTEST_AGENT_REASONING_EFFORT", "RENDERTEST_AGENT_ISOLATE"):
            environ.pop(name, None)
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
        return [entry["message"] for entry in read_jsonl(self.fake_log)[self.log_offset:]
                if entry.get("event") == "recv"]

    def fake_pids(self) -> list[int]:
        return [entry["pid"] for entry in read_jsonl(self.fake_log)[self.log_offset:]
                if entry.get("event") == "start"]

    def cleanup_child_pids(self) -> list[int]:
        return [entry["pid"] for entry in read_jsonl(self.fake_log)[self.log_offset:]
                if entry.get("event") == "cleanup-child"]

    def history_records(self) -> list[dict[str, Any]]:
        records = []
        for path in (self.root / "agent-root" / "conversations").glob("*.json"):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass
        return records

    def check_no_survivors(self, pids: list[int], within: float = 3.0) -> None:
        deadline = time.time() + within
        while time.time() < deadline and any(alive(pid) for pid in pids):
            time.sleep(0.1)
        survivors = [pid for pid in pids if alive(pid)]
        self.check(not survivors, f"processes still alive: {survivors}")


def fake_env(run: Run, scenario: str) -> dict[str, str]:
    command = f'"{sys.executable}" "{FAKE}" --scenario {scenario} --log "{run.fake_log}" --state-dir "{run.root}"'
    return {"RENDERTEST_AGENT_CODEX_COMMAND": command, "RENDERTEST_AGENT_ISOLATE": "1"}


def claude_env(run: Run, scenario: str) -> dict[str, str]:
    command = subprocess.list2cmdline([sys.executable, str(FAKE), "--backend", "claude", "--scenario", scenario,
                                      "--log", str(run.fake_log), "--state-dir", str(run.root)])
    return {"RENDERTEST_AGENT_BACKEND": "claude", "RENDERTEST_AGENT_CLAUDE_COMMAND": command,
            # The selected harness must ignore an unusable Codex command and enforce its own
            # restrictions even if the optional Codex isolation switch is explicitly disabled.
            "RENDERTEST_AGENT_CODEX_COMMAND": str(run.root / "must-not-launch-codex.exe"),
            "RENDERTEST_AGENT_ISOLATE": "0"}


def claude_settings() -> dict[str, Any]:
    return {"backend": "claude", "claudePath": sys.executable, "mcpPython": sys.executable,
            "claudeModel": "fake-deep", "claudeReasoningEffort": "ultra",
            "model": "codex-independent-model", "reasoningEffort": "codex-independent-effort",
            "isolate": False}


def check_claude_launch(run: Run, *, discovery: bool = False) -> None:
    starts = [entry for entry in read_jsonl(run.fake_log)[run.log_offset:] if entry.get("event") == "start"]
    run.check(bool(starts) and all(entry.get("backend") == "claude" for entry in starts),
              "Claude command override did not launch the intended fake harness")
    for entry in starts:
        argv = entry.get("argv", [])
        for flag in ("--claude-path", "--mcp-python"):
            value = argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else ""
            run.check(os.path.normcase(os.path.normpath(value)) == os.path.normcase(os.path.normpath(sys.executable)),
                      f"Claude launch lost {flag}: {argv}")
        run.check("-c" not in argv and not any(value.startswith(("features.", "mcp_servers.")) for value in argv),
                  f"Claude process received Codex-specific command-line overrides: {argv}")
        run.check("--instance" not in argv and "--capture-session" not in argv,
                  "plain-chat/catalog Claude process acquired a capture scope")
        if not discovery:
            root = argv[argv.index("--agent-root") + 1] if "--agent-root" in argv else ""
            run.check(os.path.normcase(os.path.normpath(root)) ==
                      os.path.normcase(os.path.normpath(str(run.root / "agent-root"))),
                      "Claude process did not receive the isolated test agent root")
            run.check("--skills-root" in argv, "Claude process did not receive deployed RenderTest skills")
    run.check(run.session().get("backend") == "claude", f"selected backend {run.session().get('backend')}")


def check_claude_params(run: Run, *, resume: bool = False) -> None:
    session_method = "thread/resume" if resume else "thread/start"
    sessions = [message.get("params", {}) for message in run.received() if message.get("method") == session_method]
    run.check(len(sessions) == 1, f"Claude {session_method} request count {len(sessions)}")
    for params in sessions:
        run.check(params.get("model") == "fake-deep", "Claude session used Codex's independent model setting")
        run.check(params.get("config") == {"mcp_servers.rendertest.enabled": False},
                  f"Claude plain-chat session inherited another MCP configuration: {params.get('config')}")
    turns = [message.get("params", {}) for message in run.received() if message.get("method") == "turn/start"]
    run.check(all(params.get("effort") == "ultra" for params in turns),
              "Claude turn used Codex's independent reasoning effort")
    run.check(run.session().get("selectedModel") == "fake-deep" and
              run.session().get("selectedEffort") == "ultra" and run.session().get("isolate") is True,
              "Claude launch did not apply independent model/effort and forced isolation")
    run.check(not any(message.get("method", "").startswith("account/login/") for message in run.received()),
              "Claude received a Codex browser login operation")


def methods(messages: list[dict[str, Any]]) -> list[str]:
    return [message["method"] for message in messages if "method" in message]


def check_thread_rebound(run: Run, trigger: str) -> None:
    ids = run.session().get("threadIds", [])
    run.check(len(ids) == 2 and ids[0] == ids[1], f"{trigger} did not resume its original thread: {ids}")
    run.check(methods(run.received()).count("thread/start") == 1 and
              methods(run.received()).count("thread/resume") == 1,
              f"{trigger} created a replacement thread instead of resuming")


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
    run.check(start.get("ephemeral") is False, "thread/start is not persistent")
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
    check_thread_rebound(run, "forced stop")


def scenario_a5(run: Run) -> None:
    retry = run.name != "A5-crash-child"
    run.launch({"questions": ["first", "second"] if retry else ["first"]}, fake_env(run, "crash-once"))
    if not run.wait_transcript(60):
        return
    results = run.results()
    run.check([r.get("status") for r in results] == (["failed", "completed"] if retry else ["failed"]),
              f"results {results}")
    notices = " ".join(i.get("text", "") for i in run.items("notice"))
    run.check("simulated crash" in notices, "stderr tail not shown after crash")
    if retry:
        check_thread_rebound(run, "crash")
        run.check(len(run.fake_pids()) == 2, f"app-server not restarted: {run.fake_pids()}")
    else:
        run.check(len(run.fake_pids()) == 1 and run.process is not None and run.process.poll() is None,
                  "crash cleanup fixture retried or closed the UI before checking its child")
        run.check(run.session().get("state") == "error" and not run.session().get("appServerRunning"),
                  "crashed backend was not marked stopped")
    if os.name == "nt":
        children = run.cleanup_child_pids()
        run.check(bool(children), "crash fixture did not create a surviving child")
        # A5-crash-child does not retry, so cleanup cannot be deferred to the next Start/Kill.
        run.check_no_survivors(children)


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
    while time.time() < deadline and ("turn/start" not in methods(run.received()) or
                                     (os.name == "nt" and not run.cleanup_child_pids())):
        time.sleep(0.2)
    pids = run.fake_pids()
    run.check(bool(pids), "fake app-server never started a turn")
    run.close()
    children = run.cleanup_child_pids()
    if os.name == "nt":
        run.check(bool(children), "exit fixture did not create a child process")
    run.check_no_survivors(pids + children)


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
    check_thread_rebound(run, "timeout")


def scenario_capture_change(run: Run) -> None:
    run.launch({"questions": ["first", "second"], "captureChangeAfterMs": 500,
                "newChatBeforeQuestion": [1]}, fake_env(run, "hang"))
    if not run.wait_transcript(60):
        return
    run.check([r.get("status") for r in run.results()] == ["interrupted", "completed"], f"results {run.results()}")
    pids = run.fake_pids()
    run.check(len(pids) == 2 and not alive(pids[0]), f"capture change did not kill old process: {pids}")
    ids = run.session().get("threadIds", [])
    run.check(len(ids) == 2 and ids[0] != ids[1], "capture change reused old thread")
    tools = [item for record in run.history_records()
             for item in record.get("conversation", {}).get("items", []) if item.get("kind") == "tool"]
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
    claude = run.name == "E-claude-tool-artifacts"
    autotest = {"questions": ["show the texture"], "clickLinks": ["first-tool", "first-artifact"]}
    if claude:
        autotest["settings"] = claude_settings()
    run.launch(autotest, claude_env(run, "tool-artifacts") if claude else
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
    if claude:
        check_claude_launch(run)
        check_claude_params(run)
        run.check([result.get("status") for result in run.results()] == ["completed"],
                  f"Claude artifact turn did not complete: {run.results()}")


def scenario_first_settings(run: Run) -> None:
    case = run.name.removeprefix("C-first-settings")
    scenario = {"": "model-pages", "-manual": "model-delayed", "-known-manual": "model-delayed", "-error": "model-error",
                "-cancel": "model-delayed", "-empty": "model-empty", "-loop": "model-loop"}[case]
    screenshot = run.root / "first-settings.png"
    autotest: dict[str, Any] = {"questions": [], "settingsScreenshot": str(screenshot),
                                "settings": {"model": "", "reasoningEffort": "",
                                             "codexPath": sys.executable,
                                             "mcpPython": str(run.root / "missing-mcp-python.exe")}}
    if not case:
        autotest["settingsSelectModel"] = "fake-deep"
    elif case in ("-manual", "-known-manual"):
        autotest.update(settingsTypeModel="fake-deep" if case == "-known-manual" else "manual-before-catalog",
                        settingsTypeEffort="manual-effort",
                        settingsAccept=True)
    elif case == "-cancel":
        autotest["settingsCloseAfterMs"] = 200
    elif case in ("-error", "-empty"):
        autotest.update(settingsTypeModel="manual-without-catalog", settingsTypeEffort="manual-effort",
                        settingsAccept=True)
    if autotest.get("settingsAccept"):
        # Saving still validates executable fields; discovery alone must not need MCP Python.
        autotest["settings"]["mcpPython"] = sys.executable
    run.launch(autotest, fake_env(run, scenario))
    if not run.wait_transcript(60):
        return
    received = run.received()
    order = methods(received)
    run.check(set(order) <= {"initialize", "initialized", "model/list"},
              f"opening Settings started unrelated account/thread/turn APIs: {order}")
    run.check(order.count("initialize") == 1 and order.count("initialized") == 1,
              f"Settings discovery initialization sequence {order}")
    run.check(not run.results() and not run.items("user") and not run.transcript.get("threadId"),
              "first-open Settings created a chat conversation")
    run.check(run.session().get("appServerRunning") is False,
              "Settings discovery was bound to the live chat app-server")
    pids = run.fake_pids()
    run.check(len(pids) == 1, f"Settings discovery process count {pids}")
    run.check_no_survivors(pids)
    starts = [entry for entry in read_jsonl(run.fake_log) if entry.get("event") == "start"]
    joined = " ".join(starts[0].get("argv", [])) if starts else ""
    run.check("RENDERTEST_AGENT_FIXED_INSTANCE_ID" not in joined and
              "RENDERTEST_AGENT_CAPTURE_SESSION_ID" not in joined,
              "Settings discovery inherited a capture scope")
    dialog = run.transcript.get("autotest", {}).get("settingsDialog", {})
    run.check(not dialog.get("timedOut"), f"Settings discovery did not finish: {dialog}")
    if case != "-cancel":
        run.check(order.count("model/list") >= 1, "Settings did not request the model catalog")
        run.check(dialog.get("screenshotSaved") is True and screenshot.is_file(),
                  "Settings screenshot was not saved")
    if not case:
        queries = [message["params"].get("cursor") for message in received if message.get("method") == "model/list"]
        run.check(queries == [None, "models-page2"], f"first-open model pagination {queries}")
        run.check(dialog.get("catalogState") == "ready" and dialog.get("modelCount") == 3,
                  f"first-open Settings did not populate both models plus the default: {dialog}")
        run.check(dialog.get("model") == "fake-deep", "Settings selected catalog id instead of the model value")
        run.check(dialog.get("effortOptions") == ["", "medium", "ultra"] and
                  dialog.get("effortEditable") is False and dialog.get("effort") == "",
                  f"newly discovered model did not control its supported reasoning efforts: {dialog}")
    elif case in ("-manual", "-known-manual"):
        run.check(dialog.get("catalogState") == "ready" and dialog.get("typedWhileLoading") is True,
                  f"manual-model fixture did not edit during discovery: {dialog}")
        events = [entry.get("event") for entry in read_jsonl(run.fake_log)]
        run.check("model-list-delayed" in events and "model-list-response" in events,
                  "delayed model response was not exercised")
        expected_model = "fake-deep" if case == "-known-manual" else "manual-before-catalog"
        run.check(dialog.get("model") == expected_model and dialog.get("effort") == "manual-effort",
                  "async discovery replaced the user-entered model/effort")
        if case == "-known-manual":
            run.check(dialog.get("accepted") is False and dialog.get("appliedModel") == "" and
                      dialog.get("appliedEffort") == "" and "manual-effort" in dialog.get("validationError", ""),
                      "unsupported effort was silently changed or saved instead of showing its validation error")
        else:
            run.check(dialog.get("appliedModel") == expected_model and
                      dialog.get("appliedEffort") == "manual-effort" and dialog.get("accepted") is True,
                      "custom model/effort were not saved")
    elif case == "-cancel":
        run.check(dialog.get("accepted") is False and dialog.get("catalogState") == "loading",
                  f"Settings was not canceled while discovery was pending: {dialog}")
        run.check(order.count("model/list") == 1 and any(entry.get("event") == "model-list-delayed"
                  for entry in read_jsonl(run.fake_log)), "Settings canceled before the catalog request was pending")
        run.check(not any(entry.get("event") == "model-list-response" for entry in read_jsonl(run.fake_log)),
                  "delayed discovery survived cancel and returned a result")
    else:
        run.check(dialog.get("catalogState") in (("empty", "ready") if case == "-empty" else ("error",)) and
                  bool(dialog.get("catalogMessage")), f"empty/failed catalog has no visible status: {dialog}")
        if case == "-error":
            run.check("model catalog unavailable" in dialog.get("catalogMessage", ""),
                      "model discovery error was not displayed")
        if case == "-loop":
            run.check(2 <= order.count("model/list") <= 32,
                      "model pagination loop was not stopped within its request bound")
        else:
            run.check(dialog.get("model") == dialog.get("appliedModel") == "manual-without-catalog" and
                      dialog.get("effort") == dialog.get("appliedEffort") == "manual-effort" and
                      dialog.get("effortEditable") is True and dialog.get("accepted") is True,
                      "manual model/effort could not be saved without a catalog")


def seed_history(run: Run, backend: str = "codex") -> dict[str, Any] | None:
    """Finish and close one real UI process; retain only this test's local fake/history files."""
    settings = claude_settings() if backend == "claude" else {"backend": "codex"}
    env = claude_env(run, "persistent") if backend == "claude" else fake_env(run, "persistent")
    run.launch({"questions": ["saved first question"], "settings": settings}, env)
    if not run.wait_transcript(60):
        return None
    run.check([r.get("status") for r in run.results()] == ["completed"], f"seed results {run.results()}")
    saved = {"historyId": run.session().get("historyId"), "threadId": run.transcript.get("threadId"),
             "instanceId": run.session().get("instanceId")}
    pids = run.fake_pids()
    run.close()
    run.check_no_survivors(pids)
    run.check(bool(saved["historyId"]) and bool(saved["threadId"]), f"seed has no durable identity: {saved}")
    records = run.history_records()
    record = next((entry for entry in records if entry.get("metadata", {}).get("id") == saved["historyId"]), None)
    run.check(record is not None, "completed conversation was not saved to disk")
    if record:
        run.check(record.get("version") == 2 and record.get("backend") == backend,
                  f"history did not preserve its version/backend: {record.get('version')}/{record.get('backend')}")
        run.check(record["metadata"].get("captureIdentity", {}).get("kind") == "none",
                  "plain chat was not saved with an explicit no-capture identity")
        run.check(record["metadata"].get("threadId") == saved["threadId"], "saved thread id differs from live binding")
    return saved if saved["historyId"] and record else None


def check_resume_config(run: Run, saved: dict[str, Any], *, capture_tools: bool = False) -> None:
    received = run.received()
    order = methods(received)
    resumes = [entry.get("params", {}) for entry in received if entry.get("method") == "thread/resume"]
    run.check(len(resumes) == 1, f"resume requests {resumes}")
    run.check("thread/start" not in order, "history restore silently created a different thread")
    if not resumes:
        return
    params = resumes[0]
    run.check(params.get("threadId") == saved["threadId"] and params.get("excludeTurns") is True,
              f"resume did not request the saved thread without server history: {params}")
    run.check(params.get("sandbox") == "read-only" and params.get("approvalPolicy") == "never",
              "resume inherited unsafe sandbox or approval policy")
    run.check(bool(params.get("cwd")), "resume omitted the current integration cwd")
    config = params.get("config", {})
    run.check(config.get("mcp_servers.blender.enabled") is False and
              config.get("mcp_servers.rendertest.enabled") is capture_tools,
              f"resume failed to reapply MCP isolation/capture binding: {config}")
    instance = run.session().get("instanceId")
    run.check(instance and instance != saved["instanceId"] and instance in params.get("developerInstructions", ""),
              "resumed process did not use its current instance instructions")
    run.check("config/read" in order and order.index("config/read") < order.index("thread/resume"),
              "resume skipped the current effective config check")


def scenario_history_new_chat(run: Run) -> None:
    run.launch({"questions": ["first chat", "second chat"], "newChatBeforeQuestion": [1]},
               fake_env(run, "persistent"))
    if not run.wait_transcript(60):
        return
    run.check([r.get("status") for r in run.results()] == ["completed", "completed"], f"results {run.results()}")
    ids = run.session().get("threadIds", [])
    run.check(len(ids) == 2 and ids[0] != ids[1], f"New chat reused the previous thread: {ids}")
    run.check(len(run.items("user")) == 1 and run.items("user")[0].get("text") == "second chat",
              "New chat did not clear the displayed conversation")
    records = run.history_records()
    run.check(len(records) == 2 and {entry.get("metadata", {}).get("threadId") for entry in records} == set(ids),
              "New chat discarded or overwrote the previous saved conversation")
    texts = {item.get("text") for record in records for item in record.get("conversation", {}).get("items", [])
             if item.get("kind") == "user"}
    run.check(texts == {"first chat", "second chat"}, f"saved questions {texts}")


def scenario_history_restore(run: Run) -> None:
    saved = seed_history(run)
    if not saved:
        return
    if run.name == "D-legacy-restore":
        history_path = run.root / "agent-root" / "conversations" / f"{saved['historyId']}.json"
        legacy = json.loads(history_path.read_text(encoding="utf-8"))
        legacy["version"] = 1
        legacy.pop("backend", None)
        history_path.write_text(json.dumps(legacy), encoding="utf-8")
    replay = run.name == "D-resume-replay"
    run.launch({"loadHistoryId": "latest", "questions": ["continued second question"],
                "settings": {"model": "fake-deep", "reasoningEffort": "ultra"},
                "clickLinks": ["first-event"]}, fake_env(run, "resume-replay" if replay else "persistent"),
               preserve=True)
    if not run.wait_transcript(60):
        return
    check_resume_config(run, saved)
    run.check(run.session().get("historyId") == saved["historyId"], "resume changed the saved conversation id")
    run.check(run.session().get("historyLoaded") is True and run.session().get("historyReadOnly") is False,
              f"matching plain-chat history cannot continue: {run.session().get('historyError')}")
    run.check(run.transcript.get("threadId") == saved["threadId"], "resume changed the conversation thread")
    run.check([r.get("status") for r in run.results()] == ["completed"], f"results {run.results()}")
    run.check([item.get("text") for item in run.items("user")] == ["saved first question", "continued second question"],
              "restoring history duplicated, discarded or reordered user messages")
    agents = run.items("agent")
    run.check(len(agents) == 2 and all(item.get("text") == ANSWER for item in agents),
              "server-resumed items duplicated or replaced the saved local transcript")
    run.check(agents and not agents[0].get("referenceToken"), "loaded capture reference retained live navigation authority")
    run.check(len(agents) == 2 and bool(agents[1].get("referenceToken")), "new message lost its current reference identity")
    clicks = run.transcript.get("autotest", {}).get("linkClicks", [])
    run.check(len(clicks) == 1 and clicks[0].get("ok") is False, "restored stale reference was allowed to navigate")
    order = methods(run.received())
    run.check("thread/resume" in order and "mcpServerStatus/list" in order and "turn/start" in order and
              order.index("thread/resume") < order.index("mcpServerStatus/list") < order.index("turn/start"),
              "resumed MCP runtime status was not checked before sending the new turn")
    statuses = [m["params"] for m in run.received() if m.get("method") == "mcpServerStatus/list"]
    run.check(statuses and all(p.get("threadId") == saved["threadId"] for p in statuses),
              "MCP status was checked for a different thread")
    resume = next((m["params"] for m in run.received() if m.get("method") == "thread/resume"), {})
    turn = next((m["params"] for m in run.received() if m.get("method") == "turn/start"), {})
    run.check(resume.get("model") == "fake-deep" and turn.get("effort") == "ultra", "resume ignored current model/effort settings")
    if run.name == "D-legacy-restore":
        upgraded = next((entry for entry in run.history_records()
                         if entry.get("metadata", {}).get("id") == saved["historyId"]), {})
        run.check(upgraded.get("version") == 2 and upgraded.get("backend") == "codex",
                  "continuing v1 history did not upgrade it with its original Codex backend")


def scenario_history_view(run: Run) -> None:
    saved = seed_history(run)
    if not saved:
        return
    bad = run.name == "D-bad-history"
    invalid_id = "abcdefab-1234-4123-8123-abcdefabcdef"
    if bad:
        (run.root / "agent-root" / "conversations" / f"{invalid_id}.json").write_text("{broken", encoding="utf-8")
    run.launch({"loadHistoryId": "latest", "questions": []}, fake_env(run, "persistent"), preserve=True)
    if not run.wait_transcript(60):
        return
    run.check(not run.fake_pids() and not run.received(), "viewing local history unnecessarily started app-server")
    run.check(run.session().get("historyId") == saved["historyId"] and run.session().get("historyLoaded") is True,
              "saved local history was not loaded")
    run.check([item.get("text") for item in run.items("agent")] == [ANSWER], "local transcript was not preserved")
    run.check(all(not item.get("referenceToken") for item in run.items("agent")), "loaded references remained navigable")
    if bad:
        entries = run.session().get("historyEntries", [])
        run.check(any(entry.get("id") == saved["historyId"] for entry in entries) and
                  all(entry.get("id") != invalid_id for entry in entries), "bad history blocked or polluted the history list")


def scenario_history_rejected(run: Run) -> None:
    saved = seed_history(run)
    if not saved:
        return
    mismatch = run.name == "D-capture-mismatch"
    history_path = run.root / "agent-root" / "conversations" / f"{saved['historyId']}.json"
    if mismatch:
        record = json.loads(history_path.read_text(encoding="utf-8"))
        record["metadata"]["captureIdentity"] = {
            "schema": "rendertest-agent-capture-identity/v1", "kind": "local-file", "verified": True,
            "canonicalPath": str((run.root / "different-capture.rdc").resolve()), "bytes": "1",
            "modifiedUnixMs": "1700000000000", "sha256": "a" * 64}
        record["metadata"]["mode"] = "capture"
        history_path.write_text(json.dumps(record), encoding="utf-8")
    before = history_path.read_bytes()
    scenario = "persistent" if mismatch else run.name.removeprefix("D-")
    run.launch({"loadHistoryId": "latest", "questions": ["must not run"]}, fake_env(run, scenario), preserve=True)
    if not run.wait_transcript(60):
        return
    run.check(run.session().get("historyReadOnly") is True and bool(run.session().get("historyError")),
              "unavailable/unsafe history did not retain a read-only explanation")
    run.check(run.session().get("historyId") == saved["historyId"], "resume rejection discarded the selected history")
    run.check([item.get("text") for item in run.items("agent")] == [ANSWER], "resume rejection discarded saved answers")
    order = methods(run.received())
    run.check("turn/start" not in order and "thread/start" not in order, "rejected history started a model or replacement thread")
    if mismatch:
        run.check(not run.fake_pids() and "thread/resume" not in order, "capture mismatch started app-server")
        run.check([result.get("status") for result in run.results()] == ["blocked"], f"mismatch results {run.results()}")
    else:
        if run.name == "D-resume-config-error":
            run.check("config/read" in order and "thread/resume" not in order,
                      "failed config verification did not stop resume")
        else:
            check_resume_config(run, saved)
        run.check(not run.session().get("appServerRunning"), "failed resume left an unsafe process running")
        run.check([result.get("status") for result in run.results()] == ["failed"], f"rejected resume results {run.results()}")
    run.check(history_path.read_bytes() == before, "read-only rejection rewrote the original saved history")


def scenario_claude_stream(run: Run) -> None:
    run.launch({"questions": ["Claude first question", "Claude follow up"], "settings": claude_settings()},
               claude_env(run, "stream"))
    if not run.wait_transcript(60):
        return
    check_claude_launch(run)
    check_claude_params(run)
    run.check([result.get("status") for result in run.results()] == ["completed", "completed"],
              f"Claude follow-up results {run.results()}")
    run.check([item.get("text") for item in run.items("agent")] == [ANSWER, ANSWER],
              "Claude streaming failed to reduce to exactly two complete assistant messages")
    run.check(len(run.items("reasoning")) == 2, "Claude reasoning items were lost")
    order = methods(run.received())
    run.check(order.count("thread/start") == 1 and order.count("turn/start") == 2 and
              "thread/resume" not in order, "Claude follow-up did not reuse the live session")
    run.check(run.session().get("accountType") == "external" and not run.session().get("loginVisible"),
              "Claude external account was rendered as Codex browser authentication")
    records = run.history_records()
    run.check(len(records) == 1 and records[0].get("version") == 2 and records[0].get("backend") == "claude",
              "Claude conversation was persisted without its backend identity")


def scenario_claude_stop(run: Run) -> None:
    hang = run.name.endswith("-hang")
    run.launch({"questions": ["Claude slow question", "Claude after stop"], "settings": claude_settings(),
                "stopOnTool": "pixel_history", "stopQuestion": 0}, claude_env(run, "hang" if hang else "interrupt"))
    if not run.wait_transcript(60):
        return
    check_claude_launch(run)
    run.check([result.get("status") for result in run.results()] == ["interrupted", "completed"],
              f"Claude stop/follow-up results {run.results()}")
    latency = run.results()[0].get("stopLatencyMs", 1e9) if run.results() else 1e9
    run.check(latency < (2600 if hang else 2000), f"Claude stop latency {latency}")
    run.check("turn/interrupt" in methods(run.received()), "Claude interrupt was not requested")
    if hang:
        pids = run.fake_pids()
        run.check(len(pids) == 2 and not alive(pids[0]), "unresponsive Claude harness was not killed before restart")
        check_thread_rebound(run, "Claude forced stop")
    else:
        run.check(len(run.fake_pids()) == 1 and len(run.session().get("threadIds", [])) == 1,
                  "graceful Claude interruption replaced its process/session")


def scenario_claude_resume(run: Run) -> None:
    saved = seed_history(run, "claude")
    if not saved:
        return
    run.launch({"loadHistoryId": saved["historyId"], "questions": ["Claude resumed question"],
                "settings": claude_settings(), "clickLinks": ["first-event"]},
               claude_env(run, "persistent"), preserve=True)
    if not run.wait_transcript(60):
        return
    check_claude_launch(run)
    check_claude_params(run, resume=True)
    order = methods(run.received())
    run.check("thread/start" not in order and order.count("thread/resume") == 1 and order.count("turn/start") == 1,
              "Claude history restore created a replacement session")
    resume = next((message["params"] for message in run.received() if message.get("method") == "thread/resume"), {})
    run.check(resume.get("threadId") == saved["threadId"] and resume.get("excludeTurns") is True,
              "Claude resume lost native session identity or duplicated server history")
    run.check(run.session().get("historyId") == saved["historyId"] and
              run.session().get("historyReadOnly") is False and run.session().get("historyLoaded") is True,
              f"Claude history could not continue: {run.session().get('historyError')}")
    run.check([item.get("text") for item in run.items("user")] == ["saved first question", "Claude resumed question"] and
              [item.get("text") for item in run.items("agent")] == [ANSWER, ANSWER],
              "Claude resumed transcript lost or duplicated messages")
    run.check([result.get("status") for result in run.results()] == ["completed"], f"Claude resume results {run.results()}")
    clicks = run.transcript.get("autotest", {}).get("linkClicks", [])
    run.check(len(clicks) == 1 and clicks[0].get("ok") is False, "Claude loaded reference retained navigation authority")
    records = run.history_records()
    run.check(len(records) == 1 and records[0].get("backend") == "claude" and
              records[0].get("metadata", {}).get("threadId") == saved["threadId"], "Claude resume changed persisted identity")


def scenario_claude_cross_backend(run: Run) -> None:
    saved_backend = "codex" if run.name.endswith("from-codex") else "claude"
    selected_backend = "claude" if saved_backend == "codex" else "codex"
    saved = seed_history(run, saved_backend)
    if not saved:
        return
    path = run.root / "agent-root" / "conversations" / f"{saved['historyId']}.json"
    before = path.read_bytes()
    run.launch({"loadHistoryId": saved["historyId"], "questions": ["must not cross backends"],
                "settings": claude_settings() if selected_backend == "claude" else {"backend": "codex"}},
               claude_env(run, "persistent") if selected_backend == "claude" else fake_env(run, "persistent"),
               preserve=True)
    if not run.wait_transcript(60):
        return
    run.check(run.session().get("backend") == selected_backend and run.session().get("historyReadOnly") is True,
              f"cross-backend history is editable: {run.session()}")
    run.check(bool(run.session().get("historyError")) and run.session().get("sendEnabled") is False,
              "cross-backend refusal lacks visible explanation or send guard")
    run.check(not run.fake_pids() and not run.received(), "cross-backend history started a harness or tried native resume")
    run.check([result.get("status") for result in run.results()] == ["blocked"], f"cross-backend results {run.results()}")
    run.check([item.get("text") for item in run.items("agent")] == [ANSWER], "cross-backend refusal hid saved answers")
    run.check(path.read_bytes() == before, "cross-backend refusal modified the original record")


def scenario_claude_policy(run: Run) -> None:
    scenario = run.name.removeprefix("E-")
    resume = "resume-policy" in scenario
    saved = seed_history(run, "claude") if resume else None
    if resume and not saved:
        return
    autotest = {"questions": ["must not run without Claude restrictions"], "settings": claude_settings()}
    before = None
    if saved:
        autotest["loadHistoryId"] = saved["historyId"]
        path = run.root / "agent-root" / "conversations" / f"{saved['historyId']}.json"
        before = path.read_bytes()
    run.launch(autotest, claude_env(run, scenario), preserve=resume)
    if not run.wait_transcript(60):
        return
    check_claude_launch(run)
    order = methods(run.received())
    run.check(("thread/resume" if resume else "thread/start") in order, "Claude policy fixture was not reached")
    run.check("turn/start" not in order and "mcpServerStatus/list" not in order,
              "unconfirmed Claude policy was used to send a turn or inspect tools")
    run.check(run.session().get("state") == "error" and not run.session().get("appServerRunning"),
              "rejected Claude policy left its harness usable")
    run.check([result.get("status") for result in run.results()] == ["failed"], f"Claude policy rejection {run.results()}")
    notices = " ".join(item.get("text", "") for item in run.items("notice"))
    run.check("tool restrictions" in notices, "Claude policy failure lacks an explanation")
    if before is not None:
        run.check(run.session().get("historyReadOnly") is True and path.read_bytes() == before,
                  "Claude resume policy refusal rewrote or unlocked the saved history")


def scenario_claude_external_login(run: Run) -> None:
    run.launch({"questions": ["Claude auth check"], "settings": claude_settings(), "autoLogin": True},
               claude_env(run, "logged-out"))
    if not run.wait_transcript(60):
        return
    check_claude_launch(run)
    run.check(run.session().get("state") == "notLoggedIn" and run.session().get("loginVisible") is False,
              "Claude external authentication exposed the Codex browser login UI")
    order = methods(run.received())
    run.check("account/read" in order and not any(method.startswith("account/login/") for method in order) and
              "thread/start" not in order and "turn/start" not in order, f"external-auth request sequence {order}")
    notices = " ".join(item.get("text", "") for item in run.items("notice"))
    run.check("claude auth login" in notices and "Click Log in" not in notices,
              "Claude external authentication guidance was replaced by Codex login instructions")
    run.check([result.get("status") for result in run.results()] == ["failed"], f"external-auth results {run.results()}")


def scenario_claude_first_settings(run: Run) -> None:
    screenshot = run.root / "claude-first-settings.png"
    run.launch({"questions": [], "settings": claude_settings(), "settingsScreenshot": str(screenshot),
                "settingsAccept": True}, claude_env(run, "model-pages"))
    if not run.wait_transcript(60):
        return
    check_claude_launch(run, discovery=True)
    order = methods(run.received())
    run.check(set(order) <= {"initialize", "initialized", "model/list"} and order.count("initialize") == 1 and
              order.count("model/list") == 2, f"Claude settings-only catalog sequence {order}")
    run.check(not run.results() and not run.items("user") and not run.transcript.get("threadId"),
              "Claude model discovery created a conversation")
    dialog = run.transcript.get("autotest", {}).get("settingsDialog", {})
    run.check(dialog.get("catalogState") == "ready" and dialog.get("modelCount") == 3 and
              not dialog.get("timedOut"), f"Claude first model catalog {dialog}")
    run.check(dialog.get("model") == dialog.get("appliedModel") == "fake-deep" and
              dialog.get("effort") == dialog.get("appliedEffort") == "ultra" and
              dialog.get("appliedBackend") == "claude" and dialog.get("accepted") is True,
              f"Claude Settings did not read/save its independent model/effort: {dialog}")
    run.check(dialog.get("savedCodexModel") == "codex-independent-model" and
              dialog.get("savedCodexEffort") == "codex-independent-effort" and
              dialog.get("savedClaudeModel") == "fake-deep" and dialog.get("savedClaudeEffort") == "ultra",
              "saving Claude model settings changed the independent Codex settings")
    run.check(dialog.get("effortOptions") == ["", "medium", "ultra"] and dialog.get("effortEditable") is False,
              "Claude catalog efforts were not applied to its settings control")
    run.check(dialog.get("screenshotSaved") is True and screenshot.is_file(), "Claude Settings screenshot missing")
    run.check_no_survivors(run.fake_pids())


def scenario_claude_disconnected(run: Run) -> None:
    scenario = run.name.removeprefix("E-")
    auth = scenario.endswith("-auth")
    completed = scenario.endswith("-completed")
    questions = ["before Claude disconnect"] if auth else ["before Claude disconnect", "after reconnect"]
    run.launch({"questions": questions, "settings": claude_settings()}, claude_env(run, scenario))
    if not run.wait_transcript(60):
        return
    check_claude_launch(run)
    events = [entry for entry in read_jsonl(run.fake_log)[run.log_offset:]
              if entry.get("event") == "backend-disconnected"]
    run.check(len(events) == 1 and events[0].get("requiresAuth") is auth,
              "backend/disconnected notification fixture was not exercised exactly once")
    order = methods(run.received())
    run.check(not any(method.startswith("account/login/") for method in order) and
              run.session().get("loginVisible") is False, "Claude disconnect exposed Codex browser login")
    if auth:
        run.check(run.session().get("state") == "notLoggedIn" and not run.session().get("appServerRunning"),
                  "Claude auth disconnect retained a connected/usable harness")
        run.check([result.get("status") for result in run.results()] == ["failed"], f"auth disconnect results {run.results()}")
        notices = " ".join(item.get("text", "") for item in run.items("notice"))
        run.check("claude auth login" in notices and "Click Log in" not in notices,
                  "auth disconnect did not show external authentication instructions")
        run.check_no_survivors(run.fake_pids())
    else:
        expected = ["completed" if completed else "failed", "completed"]
        run.check([result.get("status") for result in run.results()] == expected, f"disconnect/reconnect results {run.results()}")
        pids = run.fake_pids()
        run.check(len(pids) == 2 and not alive(pids[0]), "next question reused the disconnected harness")
        check_thread_rebound(run, "Claude backend disconnect")
        run.check(len(run.items("agent")) == (2 if completed else 1) and
                  all(item.get("text") == ANSWER for item in run.items("agent")),
                  "disconnect/reconnect duplicated or lost assistant messages")
        run.check(run.session().get("state") == "ready" and run.session().get("appServerRunning") is True,
                  "Claude did not recover after the disconnection")


def scenario_claude_account_updated(run: Run) -> None:
    run.launch({"questions": ["before Claude account expires"], "settings": claude_settings()},
               claude_env(run, "account-updated"))
    if not run.wait_transcript(60):
        return
    check_claude_launch(run)
    order = methods(run.received())
    run.check(order.count("account/read") == 2, "Claude account update did not recheck external authentication")
    run.check(run.session().get("state") == "notLoggedIn" and run.session().get("loginVisible") is False and
              not run.session().get("accountType"), "expired Claude account retained ready state or showed Codex login")
    run.check(not any(method.startswith("account/login/") for method in order), "Claude auth update initiated OAuth")
    notices = " ".join(item.get("text", "") for item in run.items("notice"))
    run.check("claude auth login" in notices and "Codex is no longer logged in" not in notices,
              "Claude account update did not use external-login guidance")
    run.check([result.get("status") for result in run.results()] == ["completed"],
              "post-turn account expiry changed the completed turn result")


SCENARIOS: dict[str, Callable[[Run], None]] = {
    "A1": scenario_a1,
    "A2": scenario_a2,
    "A3": scenario_a3,
    "A4": scenario_a4,
    "A4-hang": scenario_a4_hang,
    "A5": scenario_a5,
    "A5-crash-child": scenario_a5,
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
    "C-first-settings": scenario_first_settings,
    "C-first-settings-manual": scenario_first_settings,
    "C-first-settings-known-manual": scenario_first_settings,
    "C-first-settings-error": scenario_first_settings,
    "C-first-settings-cancel": scenario_first_settings,
    "C-first-settings-empty": scenario_first_settings,
    "C-first-settings-loop": scenario_first_settings,
    "D-new-chat": scenario_history_new_chat,
    "D-restart-restore": scenario_history_restore,
    "D-legacy-restore": scenario_history_restore,
    "D-resume-replay": scenario_history_restore,
    "D-history-view": scenario_history_view,
    "D-bad-history": scenario_history_view,
    "D-capture-mismatch": scenario_history_rejected,
    "D-resume-failure": scenario_history_rejected,
    "D-resume-config-error": scenario_history_rejected,
    "D-resume-foreign": scenario_history_rejected,
    "D-resume-unsafe": scenario_history_rejected,
    "E-claude-stream": scenario_claude_stream,
    "E-claude-tool-artifacts": scenario_tool_artifacts,
    "E-claude-stop": scenario_claude_stop,
    "E-claude-stop-hang": scenario_claude_stop,
    "E-claude-resume": scenario_claude_resume,
    "E-claude-cross-to-codex": scenario_claude_cross_backend,
    "E-claude-cross-from-codex": scenario_claude_cross_backend,
    **{f"E-claude-policy-{case}": scenario_claude_policy
       for case in ("missing", "tools", "builtin", "external", "approvals", "type")},
    "E-claude-resume-policy-missing": scenario_claude_policy,
    "E-claude-resume-policy-bad": scenario_claude_policy,
    "E-claude-external-login": scenario_claude_external_login,
    "E-claude-first-settings": scenario_claude_first_settings,
    "E-claude-disconnect-completed": scenario_claude_disconnected,
    "E-claude-disconnect-failed": scenario_claude_disconnected,
    "E-claude-disconnect-auth": scenario_claude_disconnected,
    "E-claude-account-updated": scenario_claude_account_updated,
    "exit": scenario_exit,
}


def scenario_history_capture(run: Run, args: argparse.Namespace) -> None:
    """Exercise a real capture/bridge/UI with the persistent fake; never invoke a real model."""
    capture = args.capture.resolve()
    digest = hashlib.sha256()
    with capture.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    expected_digest = digest.hexdigest()
    stat = capture.stat()
    common = {"waitForCapture": True, "selectEvent": args.event,
              "captureTimeoutMs": int(args.timeout * 1000)}
    first_png, restored_png, history_png = (run.root / name for name in
                                          ("panel-first.png", "panel-restored.png", "history.png"))
    run.launch(dict(common, questions=["saved capture question"], screenshot=str(first_png)),
               fake_env(run, "persistent"), [str(capture)])
    if not run.wait_transcript(args.timeout):
        return
    run.check([result.get("status") for result in run.results()] == ["completed"], f"first results {run.results()}")
    run.check(run.session().get("captureTools") is True, "first capture conversation disabled capture tools")
    saved = {"historyId": run.session().get("historyId"), "threadId": run.transcript.get("threadId"),
             "instanceId": run.session().get("instanceId"), "captureSessionId": run.session().get("captureSessionId")}
    run.check(all(saved.values()), f"first capture conversation has incomplete identity: {saved}")
    (run.root / "first-transcript.json").write_text(json.dumps(run.transcript, indent=2), encoding="utf-8")
    pids = run.fake_pids()
    run.close()
    run.check_no_survivors(pids)
    record = next((entry for entry in run.history_records()
                   if entry.get("metadata", {}).get("id") == saved["historyId"]), None)
    run.check(record is not None, "capture conversation did not persist")
    if not record or not all(saved.values()):
        return
    identity = record["metadata"].get("captureIdentity", {})
    run.check(identity.get("kind") == "local-file" and identity.get("verified") is True,
              f"capture identity was not verified: {identity}")
    run.check(identity.get("sha256") == expected_digest and identity.get("bytes") == str(stat.st_size),
              "saved capture identity does not contain the independently checked full-file SHA-256/size")
    run.check(identity.get("modifiedUnixMs") == str(stat.st_mtime_ns // 1_000_000),
              "saved capture identity has a different modification time")
    run.check(os.path.normcase(os.path.normpath(identity.get("canonicalPath", ""))) ==
              os.path.normcase(os.path.normpath(str(capture))), "saved capture identity has a different canonical path")

    run.launch(dict(common, loadHistoryId="latest", questions=["continued capture question"],
                    clickLinks=["first-event"], screenshot=str(restored_png), historyScreenshot=str(history_png)),
               fake_env(run, "persistent"), [str(capture)], preserve=True)
    if not run.wait_transcript(args.timeout):
        return
    check_resume_config(run, saved, capture_tools=True)
    session = run.session()
    run.check(session.get("historyId") == saved["historyId"] and run.transcript.get("threadId") == saved["threadId"],
              "capture restore changed the history/thread id")
    run.check(session.get("historyLoaded") is True and session.get("historyReadOnly") is False,
              f"same capture could not resume: {session.get('historyError')}")
    run.check(session.get("captureTools") is True and session.get("mcpStatus") == "connected",
              "resumed capture tools were not enabled and verified")
    run.check(session.get("captureSessionId") and session["captureSessionId"] != saved["captureSessionId"],
              "reopened capture reused the old bridge capture session")
    run.check([result.get("status") for result in run.results()] == ["completed"], f"resumed results {run.results()}")
    run.check([item.get("text") for item in run.items("user")] == ["saved capture question", "continued capture question"],
              "capture restore duplicated or lost the two user questions")
    agents = run.items("agent")
    run.check(len(agents) == 2 and all(item.get("text") == ANSWER for item in agents),
              "capture restore duplicated or lost the two assistant answers")
    run.check(agents and not agents[0].get("referenceToken") and
              len(agents) == 2 and agents[1].get("referenceToken") == session.get("referenceToken"),
              "old capture links retained authority or new answers lack the new reference identity")
    texts = turn_texts(run)
    run.check(len(texts) == 1 and f"instanceId: {session.get('instanceId')}" in texts[0] and
              f"captureSessionId: {session.get('captureSessionId')}" in texts[0] and
              "captureToolsAvailable: true" in texts[0] and texts[0].endswith("continued capture question"),
              "resumed turn did not send the current instance/capture-session context")
    order = methods(run.received())
    run.check("thread/resume" in order and "mcpServerStatus/list" in order and "turn/start" in order and
              order.index("thread/resume") < order.index("mcpServerStatus/list") < order.index("turn/start"),
              "capture resume did not verify MCP runtime status before sending")
    auto = run.transcript.get("autotest", {})
    clicks = auto.get("linkClicks", [])
    run.check(len(clicks) == 1 and clicks[0].get("link", "").startswith("rt://event/1513") and
              clicks[0].get("ok") is False, "loaded old event link navigated the reopened capture")
    run.check(auto.get("eventBefore") == auto.get("eventAfter") == args.event,
              "old-link click changed the selected event")
    dialog = auto.get("historyDialog", {})
    run.check(dialog.get("count", 0) >= 1 and dialog.get("screenshotSaved") is True,
              f"history dialog did not list the saved capture conversation: {dialog}")
    run.check(all(path.is_file() and path.stat().st_size > 0 for path in (first_png, restored_png, history_png)),
              "panel/history screenshots were not saved")
    pids = run.fake_pids()
    run.close()
    run.check_no_survivors(pids)


def run_history_capture(args: argparse.Namespace) -> int:
    run = Run("history-capture", args.exe, DATA_DIR / "history-capture")
    try:
        scenario_history_capture(run, args)
    finally:
        run.close()
    report(run)
    return 1 if run.failures else 0


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
    parser.add_argument("--scenario", default="all", help="all, M5d, model-settings, claude, or one of: " + ", ".join(SCENARIOS))
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--history-capture", action="store_true",
                        help="real capture, fake app-server: verify history restore across two UI processes")
    parser.add_argument("--ask-pixel", action="store_true", help="real mode: Q2/Q3 via the Ask AI pixel entry")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--question", default=DEFAULT_REAL_QUESTION)
    parser.add_argument("--event", type=int, default=DEFAULT_REAL_EVENT, help="event selected before asking")
    parser.add_argument("--stop-after", type=int, default=20, help="seconds before stopping the 2nd real question")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)

    if args.history_capture:
        if args.real or not args.capture or not args.capture.is_file():
            parser.error("--history-capture requires an existing --capture and cannot use --real")
        return run_history_capture(args)

    if args.real:
        if not args.capture:
            parser.error("--real needs --capture")
        return run_real_ask(args) if args.ask_pixel else run_real(args)

    if args.scenario == "all":
        names = list(SCENARIOS)
    elif args.scenario == "M5d":
        names = [name for name in SCENARIOS if name.startswith("D-")]
    elif args.scenario == "model-settings":
        names = [name for name in SCENARIOS if name.startswith("C-first-settings")]
    elif args.scenario == "claude":
        names = [name for name in SCENARIOS if name.startswith("E-claude-")]
    elif args.scenario in SCENARIOS:
        names = [args.scenario]
    else:
        parser.error("unknown scenario " + args.scenario)
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
