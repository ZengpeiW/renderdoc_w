"""End-to-end checks for the qrendertest AI panel (M5a).

Usage:
    python devtools/chat_panel_e2e.py [--scenario all|A1|A2|...] [--exe qrendertest.exe]
    python devtools/chat_panel_e2e.py --real --capture FILE.rdc [--question TEXT]

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
    return {"RENDERTEST_AGENT_CODEX_COMMAND": command}


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
    run.check(order[:len(expected)] == expected, f"request order {order}")
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
    run.check("mcp_servers.rendertest.enabled" not in start.get("config", {}), "rendertest disabled")
    argv = next((e["argv"] for e in read_jsonl(run.fake_log) if e.get("event") == "start"), [])
    joined = " ".join(argv)
    run.check(f'RENDERTEST_AGENT_INSTANCE_ID="{instance}"' in joined, "launch args lack the instance id")
    run.check("features.shell_tool=false" in joined, "launch args are not isolated")
    turn = next((m["params"] for m in received if m.get("method") == "turn/start"), {})
    text = (turn.get("input") or [{}])[0].get("text", "")
    run.check(text.startswith("<rendertest_context>") and text.endswith("hello"), f"turn text {text!r}")
    agents = run.items("agent")
    run.check(len(agents) == 1 and agents[0].get("text") == ANSWER, f"agent message {agents}")
    run.check(any(i.get("type") == "reasoning" for i in run.items("reasoning")), "no reasoning item")
    run.check([r.get("status") for r in run.results()] == ["completed"], f"results {run.results()}")
    run.check(run.transcript.get("totalTokens") == 4242, "token usage not applied")
    run.check(run.session().get("mcpStatus") == "connected" and run.session().get("mcpTools") == 22,
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
        run.check(tool.get("progress") == "reading context", f"progress {tool.get('progress')}")
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
    run.check("codex login" in " ".join(i.get("text", "") for i in run.items("notice")), "no login hint")


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
    run.check(run.session().get("state") == "error", f"state {run.session().get('state')}")
    run.check("RENDERTEST_MCP_PYTHON" in " ".join(i.get("text", "") for i in run.items("notice")),
              "no MCP python guidance")
    run.check(not run.fake_log.exists(), "app-server started without an MCP python")


def scenario_exit(run: Run) -> None:
    run.launch({"questions": ["never finishes"]}, fake_env(run, "hang"))
    deadline = time.time() + 60
    while time.time() < deadline and "turn/start" not in methods(run.received()):
        time.sleep(0.2)
    pids = run.fake_pids()
    run.check(bool(pids), "fake app-server never started a turn")
    run.close()
    run.check_no_survivors(pids)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="all", help="all or one of: " + ", ".join(SCENARIOS))
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--question", default=DEFAULT_REAL_QUESTION)
    parser.add_argument("--event", type=int, default=DEFAULT_REAL_EVENT, help="event selected before asking")
    parser.add_argument("--stop-after", type=int, default=20, help="seconds before stopping the 2nd real question")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)

    if args.real:
        if not args.capture:
            parser.error("--real needs --capture")
        return run_real(args)

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
