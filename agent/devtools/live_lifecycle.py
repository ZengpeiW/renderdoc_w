"""Drive a full bridge lifecycle check against a real qrendertest (Windows).

Usage:
    python devtools/live_lifecycle.py --capture FILE.rdc [--exe x64/Release/qrendertest.exe]
                                      [--root DIR] [--step-ms 8000] [--timeout 600]

Launches qrendertest with ``ui_lifecycle_script.py``, polls the instance manifest while the
script loads, selects, closes and reloads the capture, checks get_context on the selected event,
closes the main window with WM_CLOSE and finally asserts that the bridge recorded a clean stop.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = AGENT_DIR.parent
SOURCE_DIR = AGENT_DIR / "mcp_server" / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from rendertest_mcp.ipc_client import BridgeClient  # noqa: E402
from rendertest_mcp.paths import instances_root  # noqa: E402

WM_CLOSE = 0x0010
SNAPSHOT_KEYS = ("captureLoaded", "captureSessionId", "selectedEventId", "currentEventId")


def close_windows(pid: int) -> int:
    user32 = ctypes.windll.user32
    closed = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            closed.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return len(closed)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_steps(path: Path) -> list[str]:
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def check(history: list[dict], steps: list[str], context: dict | None, final: dict | None) -> list[str]:
    failures = []
    selected = next((int(line.split()[1]) for line in steps if line.startswith("selected ")), None)
    errors = [line for line in steps if line.startswith("error ")]
    if errors:
        failures.append("ui script errors: " + "; ".join(errors))
    if "done" not in steps:
        failures.append("ui script did not reach 'done'")

    loaded = [item for item in history if item["captureLoaded"]]
    if not loaded or any(item["selectedEventId"] is None or item["currentEventId"] is None for item in loaded):
        failures.append("event ids missing while a capture was loaded")
    if selected is None or not any(item["selectedEventId"] == selected for item in loaded):
        failures.append(f"manifest never reported selected event {selected}")
    if context is None or (context.get("selectedAction") or {}).get("eventId") != selected:
        failures.append(f"get_context did not describe selected event {selected}: {context}")

    sessions = []
    closed_between = False
    for item in history:
        if item["captureLoaded"]:
            if not sessions or sessions[-1] != item["captureSessionId"]:
                if sessions and not closed_between:
                    failures.append("session id changed without a close")
                sessions.append(item["captureSessionId"])
                closed_between = False
        elif sessions:
            closed_between = True
    if len(sessions) < 2 or len(set(sessions)) != len(sessions):
        failures.append(f"expected a fresh session id after reload, saw {sessions}")

    if final is None or final.get("stopped") is not True or final.get("captureLoaded") is not False:
        failures.append(f"bridge did not record a clean stop: {final}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live lifecycle check for the RenderTest agent bridge.")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--exe", type=Path, default=REPO_DIR / "x64" / "Release" / "qrendertest.exe")
    parser.add_argument("--root", type=Path, default=AGENT_DIR / ".agent-data" / "live")
    parser.add_argument("--step-ms", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    steps_path = root / "smoke-steps.log"
    steps_path.unlink(missing_ok=True)

    environ = dict(os.environ)
    environ.update(
        RENDERTEST_AGENT_ROOT=str(root),
        RENDERTEST_AGENT_SMOKE_CAPTURE=str(args.capture.resolve()),
        RENDERTEST_AGENT_SMOKE_STEP_MS=str(args.step_ms),
    )
    environ.pop("RENDERTEST_AGENT_BRIDGE", None)
    script = AGENT_DIR / "devtools" / "ui_lifecycle_script.py"
    process = subprocess.Popen(
        [str(args.exe), str(args.capture.resolve()), "--ui-script", str(script)], env=environ
    )
    print(f"launched pid {process.pid}")

    client = BridgeClient(root=root)
    manifest_path: Path | None = None
    history: list[dict] = []
    context = None
    final = None
    closing = False
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if manifest_path is None:
            matches = sorted(instances_root(root).glob(f"{process.pid}-*/instance.json"))
            manifest_path = matches[0] if matches else None
        manifest = read_json(manifest_path) if manifest_path else None
        if manifest:
            final = manifest
            snapshot = {key: manifest.get(key) for key in SNAPSHOT_KEYS}
            if not history or history[-1] != snapshot:
                history.append(snapshot)
                print("state:", json.dumps(snapshot))

        steps = read_steps(steps_path)
        selected = next((int(line.split()[1]) for line in steps if line.startswith("selected ")), None)
        if context is None and manifest and selected is not None and manifest.get("selectedEventId") == selected:
            context = client.call("get_context", instance_id=manifest["instanceId"]).result["data"]
            print("get_context:", json.dumps(context, ensure_ascii=False))

        if process.poll() is not None:
            break
        if not closing and ("done" in steps or any(line.startswith("error ") for line in steps)):
            closing = close_windows(process.pid) > 0
            print("sent WM_CLOSE" if closing else "no window to close yet")
        time.sleep(0.25)
    else:
        process.kill()
        print("timed out; killed qrendertest", file=sys.stderr)

    process.wait(timeout=30)
    if manifest_path:
        final = read_json(manifest_path) or final
    steps = read_steps(steps_path)
    print("steps:", steps)
    print("exit code:", process.returncode)

    failures = check(history, steps, context, final)
    for failure in failures:
        print("FAIL:", failure, file=sys.stderr)
    print("PASS" if not failures else f"{len(failures)} check(s) failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
