"""Validate decompilation through MCP against an owned, hidden RenderTest process.

Example (no model or external service is used):
    python agent/devtools/decompile_live.py --launch E:/Captures/pragmata1.rdc --event 3820

The default matrix is vertex/pixel x auto/GLSL. auto prefers HLSL and reports any GLSL
fallback; explicit --language hlsl requires HLSL. Repeat --stage and --language to select
another matrix. Reports/artifacts remain in a unique agent/.agent-data/decompile-live-* folder.
The startup script closes the test process's config writer before testing. Existing user
windows are never attached to or closed, and UI.config/DefaultLayout.config are hash-checked.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = AGENT_DIR.parent
sys.path.insert(0, str(AGENT_DIR / "mcp_server" / "src"))

from mcp.types import TextContent  # noqa: E402
from rendertest_mcp.ipc_client import BridgeClient  # noqa: E402
from rendertest_mcp.protocol import RESULT_SCHEMA, TOOL_NAMES  # noqa: E402
from rendertest_mcp.server import create_server  # noqa: E402

STAGES = ("vertex", "hull", "domain", "geometry", "pixel", "compute", "amplification", "mesh")
HASH = re.compile(r"^[0-9a-f]{64}$")
MAIN = re.compile(r"\bmain\s*\(")


def structured(value: Any) -> dict[str, Any]:
    if isinstance(value, tuple):
        return value[1]
    if isinstance(value, dict):
        return value
    return json.loads(next(item.text for item in value if isinstance(item, TextContent)))


def digest(path: Path) -> str | None:
    if not path.exists():
        return None
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def error_text(error: BaseException) -> str:
    return type(error).__name__ + ": " + str(error)


class Check:
    def __init__(self, root: Path, instance: dict[str, Any]):
        self.instance = instance["instanceId"]
        self.session = instance["captureSessionId"]
        self.scope = (root / "artifacts" / self.instance / self.session).resolve()
        client = BridgeClient(root=root, environ={
            "RENDERTEST_AGENT_FIXED_INSTANCE_ID": self.instance,
            "RENDERTEST_AGENT_CAPTURE_SESSION_ID": self.session,
        })
        self.server = create_server(client)
        self.timings: list[dict[str, Any]] = []
        names = {item.name for item in asyncio.run(self.server.list_tools())}
        require(names == set(TOOL_NAMES), "Expected the current MCP catalog.")

    def call(self, name: str, **params: Any) -> dict[str, Any]:
        started = time.monotonic()
        try:
            response = structured(asyncio.run(self.server.call_tool(name, params)))
            require(response.get("schema") == RESULT_SCHEMA, name + ": invalid result schema")
            require(response.get("instanceId") == self.instance, name + ": instance changed")
            require(response.get("captureSessionId") == self.session, name + ": capture changed")
            return response
        finally:
            self.timings.append({"method": name, "params": params,
                                 "elapsedMs": round((time.monotonic() - started) * 1000, 1)})

    def ui_event(self) -> dict[str, Any]:
        context = self.call("get_context")["data"]
        return {name: context[name] for name in ("selectedEventId", "currentEventId")}

    def verify(self, result: dict[str, Any], shader: dict[str, Any], stage: str,
               language: str, event: int) -> dict[str, Any]:
        data = result["data"]
        require(result["effect"] == "artifact", "Decompilation must declare artifact effect.")
        require(data["available"] is True and data["generated"] is True,
                "Generated shader text is not available.")
        require(data["isOriginalSource"] is False, "Generated text was labelled original source.")
        require(data["eventId"] == event and data["stage"] == stage, "Shader selection changed.")
        require(data["shader"]["resourceId"] == shader["resourceId"], "Shader ResourceId changed.")
        require(data["entryPoint"] == shader["entryPoint"], "Bound shader entry point changed.")
        require(data["inputEncoding"] == shader["reflection"]["encoding"],
                "Input encoding does not match shader reflection.")
        require(data["inputEncoding"] in ("DXBC", "DXIL", "SPIRV"), "Unexpected input encoding.")
        require(type(data["inputBytes"]) is int and 0 < data["inputBytes"] <= 16 * 1024 * 1024,
                "Input byte count is invalid.")
        require(isinstance(data["inputSha256"], str) and HASH.fullmatch(data["inputSha256"]),
                "Input SHA-256 is invalid.")
        require(data["requestedLanguage"] == language, "Requested language changed.")
        actual_language = data["outputLanguage"]
        require(actual_language in ("hlsl", "glsl"), "Generated output language is invalid.")
        require(language == "auto" or actual_language == language,
                "Explicit output language was silently changed.")
        if language == "auto" and actual_language == "glsl":
            require(any("hlsl" in warning.lower() and "glsl" in warning.lower()
                        for warning in result["warnings"]), "Automatic GLSL fallback lacks a warning.")
        require(data["readArtifactCompatible"] is True, "Generated lines exceed the reader budget.")
        require(isinstance(data["decompiler"], dict) and data["decompiler"],
                "Missing converter provenance.")
        require(result["meta"]["replayEventRestored"] is True, "Replay EID was not restored.")
        require(len(result["artifacts"]) == 1, "Expected exactly one generated text artifact.")
        artifact = result["artifacts"][0]
        require(artifact["kind"] == "shader-decompiled", "Wrong generated artifact kind.")
        require(artifact["mediaType"].split(";", 1)[0] == "text/plain", "Artifact is not text.")
        require(artifact["path"] == data["decompiledArtifact"], "Artifact paths disagree.")
        path = Path(artifact["path"]).resolve(strict=True)
        require(path.is_relative_to(self.scope) and path.suffix == ".txt",
                "Artifact is outside the fixed instance/capture scope.")
        require(type(data["outputBytes"]) is int and 0 < data["outputBytes"] <= 16 * 1024 * 1024,
                "Output byte count is invalid.")
        require(artifact["bytes"] == data["outputBytes"] == path.stat().st_size,
                "Output byte counts disagree.")
        require(digest(path) == artifact["sha256"], "Artifact SHA-256 does not match its contents.")
        source = artifact["source"]
        expected = {"event_id": event, "stage": stage, "shaderResourceId": shader["resourceId"],
                    "entryPoint": shader["entryPoint"], "inputEncoding": data["inputEncoding"],
                    "inputBytes": data["inputBytes"], "inputSha256": data["inputSha256"],
                    "language": actual_language, "requestedLanguage": language,
                    "generated": True, "isOriginalSource": False,
                    "decompiler": data["decompiler"]}
        require(all(source.get(key) == value for key, value in expected.items()),
                "Artifact provenance disagrees with its result/reflection.")
        first_page = self.call("read_artifact", path=str(path), limit=12)["data"]
        main_page = self.call("read_artifact", path=str(path), search="main", limit=100,
                              max_bytes=128 * 1024)["data"]
        require(any(MAIN.search(item["text"]) for item in main_page["lines"]),
                "read_artifact could not find a generated main entry point.")
        return {"passed": True, "stage": stage, "requestedLanguage": language,
                "outputLanguage": actual_language, "reflection": shader,
                "translation": data, "artifact": artifact, "warnings": result["warnings"],
                "replay": result["meta"], "firstPage": first_page, "mainPage": main_page,
                "inputHashValidation": "metadata consistency; original bytecode is not exposed by get_shader"}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--launch", type=Path, required=True, metavar="CAPTURE.rdc")
    parser.add_argument("--exe", type=Path, default=REPO_DIR / "x64" / "Release" / "qrendertest.exe")
    parser.add_argument("--event", type=int, required=True, metavar="EID")
    parser.add_argument("--stage", choices=STAGES, action="append")
    parser.add_argument("--language", choices=("auto", "hlsl", "glsl"), action="append")
    parser.add_argument("--wait", type=float, default=240, help="Capture startup timeout in seconds.")
    args = parser.parse_args()
    if args.event < 0 or not 0 < args.wait <= 1800:
        parser.error("--event must be nonnegative and --wait must be in (0, 1800].")
    if os.name != "nt":
        parser.error("This hidden GUI validation runner requires Windows.")
    for name in ("launch", "exe"):
        value = getattr(args, name).resolve()
        if not value.is_file():
            parser.error("--%s is not an existing file: %s" % (name, value))
        setattr(args, name, value)
    args.stage = list(dict.fromkeys(args.stage or ("vertex", "pixel")))
    args.language = list(dict.fromkeys(args.language or ("auto", "glsl")))
    return args


def main() -> int:
    args = arguments()
    root = AGENT_DIR / ".agent-data" / ("decompile-live-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    report_path = root / "report.json"
    config = Path(os.environ["APPDATA"]) / "qrendertest" / "UI.config"
    configs = {path: digest(path) for path in (config, config.with_name("DefaultLayout.config"))}
    marker = root / "config-closed.txt"
    startup = root / "startup.py"
    startup.write_text("import pathlib\npyrenderdoc.Config().Close()\n"
                       "pathlib.Path(%r).write_text('closed')\n" % str(marker), encoding="utf-8")
    environment = dict(os.environ, RENDERTEST_AGENT_ROOT=str(root / "ipc"))
    environment.pop("RENDERTEST_AGENT_CHAT_AUTOTEST", None)
    environment.pop("RENDERTEST_AGENT_BRIDGE", None)
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = 0
    report: dict[str, Any] = {"passed": False, "root": str(root), "capture": str(args.launch),
                              "exe": str(args.exe), "eventId": args.event, "stages": args.stage,
                              "requestedLanguages": args.language, "artifacts": [], "errors": []}
    process = None
    check = None
    try:
        process = subprocess.Popen([str(args.exe), "--python", str(startup), str(args.launch)],
                                   env=environment, startupinfo=startup_info, cwd=str(args.exe.parent))
        report["pid"] = process.pid
        print("Started owned hidden RenderTest:", process.pid, "Report:", report_path, flush=True)
        discovery = BridgeClient(root=root / "ipc", environ={})
        deadline = time.monotonic() + args.wait
        instance = None
        while time.monotonic() < deadline:
            require(process.poll() is None, "Owned RenderTest exited while loading the capture.")
            candidates = [item for item in discovery.list_instances()
                          if item.get("pid") == process.pid and item.get("captureLoaded")]
            if len(candidates) == 1 and marker.is_file():
                instance = candidates[0]
                break
            time.sleep(0.5)
        require(instance is not None, "Owned RenderTest did not load the capture before the timeout.")
        report["instance"] = instance
        report["configWriterClosed"] = marker.read_text() == "closed"
        check = Check(root / "ipc", instance)
        report["toolCount"] = len(TOOL_NAMES)
        before = check.ui_event()
        report["uiEventBefore"] = before
        report["sources"] = {}
        for stage in args.stage:
            try:
                shader = check.call("get_shader", stage=stage, event_id=args.event)["data"]
                source = check.call("get_shader_source", stage=stage, event_id=args.event, limit=10)
                report["sources"][stage] = source
                # Source unavailability is expected for optimized captures and is not a failure.
                input_identity = None
                for language in args.language:
                    try:
                        result = check.call("decompile_shader", stage=stage, event_id=args.event,
                                            language=language)
                        entry = check.verify(result, shader, stage, language, args.event)
                        identity = (entry["translation"]["inputBytes"], entry["translation"]["inputSha256"])
                        require(input_identity is None or input_identity == identity,
                                "Input bytecode identity changed between output languages.")
                        input_identity = identity
                        report["artifacts"].append(entry)
                        print(json.dumps({"stage": stage, "requestedLanguage": language,
                                          "outputLanguage": entry["outputLanguage"],
                                          "bytes": entry["artifact"]["bytes"],
                                          "path": entry["artifact"]["path"]}, ensure_ascii=False), flush=True)
                    except Exception as error:
                        failure = {"stage": stage, "requestedLanguage": language,
                                   "error": error_text(error)}
                        report["errors"].append(failure)
                        print(json.dumps(failure, ensure_ascii=False), flush=True)
                    finally:
                        require(check.ui_event() == before, "A shader tool changed the GUI event.")
            except Exception as error:
                report["errors"].append({"stage": stage, "error": error_text(error)})
        after = check.ui_event()
        report["uiEventAfter"] = after
        report["uiEventPreserved"] = before == after
        require(before == after, "The GUI event was not preserved.")
        require(len(report["artifacts"]) == len(args.stage) * len(args.language),
                "Some requested shader/language combinations did not pass.")
    except BaseException as error:
        report["errors"].append({"error": error_text(error)})
    finally:
        if check is not None:
            report["calls"] = check.timings
        # Terminate only the exact Popen process handle we created. No process-name search,
        # taskkill or WM_CLOSE is sent to a user window; forced close also avoids layout writes.
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                report["ownedProcessExited"] = process.poll() is not None
            except BaseException as error:
                report["errors"].append({"cleanup": error_text(error)})
        report["configuration"] = {str(path): {"before": previous, "after": digest(path)}
                                    for path, previous in configs.items()}
        report["configPreserved"] = all(value["before"] == value["after"]
                                         for value in report["configuration"].values())
        if not report["configPreserved"]:
            report["errors"].append({"error": "UI.config or DefaultLayout.config changed."})
        report["passed"] = not report["errors"] and bool(report.get("ownedProcessExited"))
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Report:", report_path, "passed=" + str(report["passed"]).lower(), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
