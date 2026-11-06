"""Verify deployed RenderTest skills with the real local Codex app-server, without a model turn.

Each case uses a fresh empty CODEX_HOME beneath agent/.agent-data. No account APIs, model turns,
browser flows, or user configuration writes are performed. Only initialize, extraRoots/set and
skills/list are sent. The third case tests the deployed .agents/skills fallback.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
from typing import Any

from appserver_schema_check import find_codex

REPO = Path(__file__).resolve().parents[2]
EXPECTED = {"rendertest-core", "rendertest-event-analysis", "rendertest-pixel-investigation",
            "rendertest-shader-debug"}


def probe(codex: Path, cwd: Path, home: Path, expected_root: Path,
          extra_roots: list[Path] | None) -> dict[str, Any]:
    home.mkdir(parents=True)
    # Inherit only process/OS plumbing, not credentials or Codex runtime settings.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
               "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
               "PROCESSOR_ARCHITECTURE"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["CODEX_HOME"] = str(home)
    command = [str(codex), "app-server", "-c", "mcp_servers={}"]
    for feature in ("apps", "plugins", "hooks", "shell_tool", "unified_exec", "multi_agent"):
        command += ["-c", f"features.{feature}=false"]
    messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
    stderr: list[str] = []
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except json.JSONDecodeError:
                messages.put({"parseError": line[:200]})
        messages.put(None)

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr.append(line)
            if len(stderr) > 64:
                del stderr[0]

    readers = [threading.Thread(target=read_stdout, daemon=True),
               threading.Thread(target=read_stderr, daemon=True)]
    for reader in readers:
        reader.start()
    request_id = 0

    def write(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal request_id
        request_id += 1
        write({"id": request_id, "method": method, "params": params})
        while True:
            try:
                message = messages.get(timeout=30)
            except queue.Empty as exc:
                raise RuntimeError(f"{method} timed out: {''.join(stderr[-5:])}") from exc
            if message is None:
                raise RuntimeError(f"app-server exited during {method}: {''.join(stderr[-5:])}")
            if "parseError" in message:
                raise RuntimeError(f"invalid app-server JSON: {message['parseError']}")
            if "method" in message and "id" in message:
                write({"id": message["id"], "error": {"code": -32601, "message": "probe is read-only"}})
            elif message.get("id") == request_id:
                if message.get("error"):
                    raise RuntimeError(f"{method}: {message['error']}")
                return message["result"]

    try:
        request("initialize", {"clientInfo": {"name": "rendertest_skills_check", "version": "1"},
                               "capabilities": {"experimentalApi": True}})
        write({"method": "initialized", "params": {}})
        if extra_roots is not None:
            request("skills/extraRoots/set", {"extraRoots": [str(path) for path in extra_roots]})
        result = request("skills/list", {"cwds": [str(cwd)], "forceReload": True})
        entries = result.get("data", [])
        if len(entries) != 1:
            raise RuntimeError(f"expected one cwd entry, got {len(entries)}")
        entry = entries[0]
        if entry.get("errors"):
            raise RuntimeError(f"skill errors: {entry['errors']}")
        skills = {skill["name"]: skill for skill in entry.get("skills", []) if skill["name"] in EXPECTED}
        if set(skills) != EXPECTED:
            raise RuntimeError(f"missing deployed skills: {sorted(EXPECTED - set(skills))}")
        paths = {}
        for name, skill in skills.items():
            path = Path(skill["path"]).resolve()
            expected = (expected_root / name / "SKILL.md").resolve()
            if path != expected or not skill.get("enabled"):
                raise RuntimeError(f"{name}: expected enabled deployed skill {expected}, got {skill}")
            paths[name] = str(path)
        return {"cwd": str(cwd), "extraRoots": [str(path) for path in extra_roots or []],
                "skills": paths, "errors": [], "pid": proc.pid}
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--deployment", type=Path,
                        default=REPO / "x64/Release/integrations/rendertest_agent")
    args = parser.parse_args(argv)
    deployment = args.deployment.resolve()
    data = REPO / "agent/.agent-data/skills-check"
    data.mkdir(parents=True, exist_ok=True)
    try:
        codex = find_codex(args.codex)
        with tempfile.TemporaryDirectory(prefix="run-", dir=data) as temporary, \
                tempfile.TemporaryDirectory(prefix="rendertest-external-cwd-") as outside:
            home_root, external = Path(temporary), Path(outside).resolve()
            if external.is_relative_to(REPO):
                raise RuntimeError("the external cwd must be outside the repository")
            cases = (
                ("deployed-extra-roots", deployment, deployment / "skills", [deployment / "skills"]),
                ("external-extra-roots", external, deployment / "skills", [deployment / "skills"]),
                ("deployed-fallback", deployment, deployment / ".agents/skills", None),
            )
            results = {}
            for name, cwd, expected_root, roots in cases:
                results[name] = probe(codex, cwd, home_root / name, expected_root, roots)
            print(json.dumps({"ok": True, "cases": results, "allProcessesExited": True}, ensure_ascii=False, indent=2))
            return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
