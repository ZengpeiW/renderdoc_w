"""Check the Codex app-server protocol and feature switches used by the AI panel.

This runs only local ``generate-ts --experimental`` and ``features list`` commands, with a
temporary empty CODEX_HOME. It never starts a model turn, logs in, or changes the user's config.
Use --out to retain fresh bindings (the destination must be empty), or --check-dir and
--features-file to check an existing snapshot without executing Codex.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Iterable


METHODS = {
    "ClientRequest.ts": (
        "initialize", "account/read", "account/login/start", "account/login/cancel", "model/list",
        "skills/extraRoots/set", "skills/list", "config/read", "thread/start", "thread/resume", "mcpServerStatus/list",
        "turn/start", "turn/interrupt",
    ),
    "ClientNotification.ts": ("initialized",),
    "ServerNotification.ts": (
        "account/login/completed", "account/updated", "turn/started", "turn/completed",
        "item/started", "item/completed", "item/agentMessage/delta",
        "item/reasoning/summaryTextDelta", "item/mcpToolCall/progress",
        "thread/tokenUsage/updated", "error",
    ),
    "ServerRequest.ts": (
        "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
        "item/permissions/requestApproval", "mcpServer/elicitation/request",
        "item/tool/requestUserInput", "execCommandApproval", "applyPatchApproval",
    ),
}

# These are consumed fields, not a copy of the complete schema. Additional fields are compatible.
FIELDS = {
    "InitializeParams.ts": ("clientInfo", "capabilities"),
    "InitializeCapabilities.ts": ("experimentalApi",),
    "ClientInfo.ts": ("name", "title", "version"),
    "InitializeResponse.ts": ("userAgent", "codexHome", "platformFamily", "platformOs"),
    "v2/GetAccountParams.ts": ("refreshToken",),
    "v2/GetAccountResponse.ts": ("account", "requiresOpenaiAuth"),
    "v2/Account.ts": ("type", "email", "planType"),
    "v2/LoginAccountParams.ts": ("type",),
    "v2/LoginAccountResponse.ts": ("type", "loginId", "authUrl"),
    "v2/CancelLoginAccountParams.ts": ("loginId",),
    "v2/CancelLoginAccountResponse.ts": ("status",),
    "v2/AccountLoginCompletedNotification.ts": ("loginId", "success", "error"),
    "v2/AccountUpdatedNotification.ts": ("authMode", "planType"),
    "v2/ModelListParams.ts": ("cursor", "limit", "includeHidden"),
    "v2/ModelListResponse.ts": ("data", "nextCursor"),
    "v2/Model.ts": ("id", "model", "displayName", "description", "hidden", "isDefault",
                    "supportedReasoningEfforts", "defaultReasoningEffort"),
    "v2/ReasoningEffortOption.ts": ("reasoningEffort", "description"),
    "v2/SkillsExtraRootsSetParams.ts": ("extraRoots",),
    "v2/SkillsListParams.ts": ("cwds", "forceReload"),
    "v2/SkillsListResponse.ts": ("data",),
    "v2/SkillsListEntry.ts": ("cwd", "skills", "errors"),
    "v2/SkillMetadata.ts": ("name", "path", "enabled"),
    "v2/ConfigReadParams.ts": ("cwd",),
    "v2/ConfigReadResponse.ts": ("config",),
    "v2/ThreadStartParams.ts": ("cwd", "sandbox", "approvalPolicy", "ephemeral", "serviceName",
                                "model", "developerInstructions", "config"),
    "v2/ThreadStartResponse.ts": ("thread", "model", "sandbox", "approvalPolicy"),
    "v2/ThreadResumeParams.ts": ("threadId", "cwd", "model", "approvalPolicy", "sandbox", "config",
                                 "developerInstructions", "excludeTurns"),
    "v2/ThreadResumeResponse.ts": ("thread", "model", "sandbox", "approvalPolicy"),
    "v2/Thread.ts": ("id", "ephemeral", "historyMode", "turns"),
    "v2/ListMcpServerStatusParams.ts": ("threadId", "detail", "limit", "cursor"),
    "v2/ListMcpServerStatusResponse.ts": ("data", "nextCursor"),
    "v2/McpServerStatus.ts": ("name", "runtimeStatus", "tools", "toolsError"),
    "v2/TurnStartParams.ts": ("threadId", "input", "effort"),
    "v2/UserInput.ts": ("type", "text", "text_elements"),
    "v2/TurnStartResponse.ts": ("turn",),
    "v2/TurnInterruptParams.ts": ("threadId", "turnId"),
    "v2/Turn.ts": ("id", "status", "error"),
    "v2/TurnCompletedNotification.ts": ("threadId", "turn"),
    "v2/ThreadItem.ts": ("type", "id", "text", "summary", "server", "tool", "arguments",
                         "status", "durationMs", "result", "error"),
    "v2/McpToolCallResult.ts": ("content", "structuredContent"),
    "v2/AgentMessageDeltaNotification.ts": ("threadId", "turnId", "itemId", "delta"),
    "v2/ReasoningSummaryTextDeltaNotification.ts": ("threadId", "turnId", "itemId", "delta"),
    "v2/ErrorNotification.ts": ("error", "willRetry"),
    "v2/CommandExecutionRequestApprovalResponse.ts": ("decision",),
    "v2/FileChangeRequestApprovalResponse.ts": ("decision",),
    "v2/PermissionsRequestApprovalResponse.ts": ("permissions", "scope"),
    "v2/McpServerElicitationRequestResponse.ts": ("action", "content", "_meta"),
    "ExecCommandApprovalResponse.ts": ("decision",),
    "ApplyPatchApprovalResponse.ts": ("decision",),
}

LITERALS = {
    "v2/LoginAccountParams.ts": ("chatgpt",),
    "v2/LoginAccountResponse.ts": ("chatgpt",),
    "v2/CancelLoginAccountStatus.ts": ("canceled", "notFound"),
    "v2/SandboxMode.ts": ("read-only",),
    "v2/SandboxPolicy.ts": ("readOnly",),
    "v2/AskForApproval.ts": ("never",),
    "v2/McpServerStatusDetail.ts": ("toolsAndAuthOnly",),
    "v2/McpServerConnectionStatus.ts": ("starting", "connected", "disabled"),
    "v2/CommandExecutionApprovalDecision.ts": ("decline",),
    "v2/FileChangeApprovalDecision.ts": ("decline",),
}

FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "computer_use", "browser_use",
    "browser_use_external", "image_generation", "multi_agent", "goals", "hooks",
)


def source_text(path: Path) -> str:
    """Exclude comments so mentioning an old field in migration notes cannot pass the check."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def check_schema(root: Path) -> list[str]:
    errors: list[str] = []
    sources: dict[str, str] = {}
    for name in sorted(set(METHODS) | set(FIELDS) | set(LITERALS)):
        try:
            sources[name] = source_text(root / name)
        except OSError as exc:
            errors.append(f"{name}: cannot read schema ({exc.strerror})")
    for name, methods in METHODS.items():
        if name not in sources:
            continue
        actual = set(re.findall(r'(?:"method"|\bmethod)\s*:\s*"([^\"]+)"', sources[name]))
        errors.extend(f"{name}: missing method {method}" for method in methods if method not in actual)
    for name, fields in FIELDS.items():
        if name not in sources:
            continue
        for field in fields:
            pattern = rf'(?<![\w])(?:"{re.escape(field)}"|{re.escape(field)})\??\s*:'
            if not re.search(pattern, sources[name]):
                errors.append(f"{name}: missing field {field}")
    for name, literals in LITERALS.items():
        if name not in sources:
            continue
        for value in literals:
            if json.dumps(value) not in sources[name]:
                errors.append(f"{name}: missing enum/variant {value}")
    return errors


def check_features(text: str) -> list[str]:
    rows = re.findall(r"(?m)^(\S+)\s+(stable|experimental|under development|deprecated|removed)\s+(?:true|false)\s*$", text)
    features = dict(rows)
    errors = []
    for name in FEATURES:
        if name not in features:
            errors.append(f"features list: missing {name}")
        elif features[name] in ("removed", "deprecated"):
            errors.append(f"features list: {name} is {features[name]}; isolation needs revalidation")
    return errors


def find_codex(explicit: Path | None) -> Path:
    if explicit:
        if not explicit.is_file():
            raise ValueError(f"Codex executable does not exist: {explicit}")
        return explicit.resolve()
    vendor = Path(os.environ.get("APPDATA", "")) / (
        "npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/"
        "vendor/x86_64-pc-windows-msvc/bin/codex.exe")
    if vendor.is_file():
        return vendor.resolve()
    found = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if not found:
        raise ValueError("Codex was not found; provide --codex with the native executable path")
    return Path(found).resolve()


def run_local(command: Iterable[str], env: dict[str, str]) -> str:
    result = subprocess.run(list(command), env=env, text=True, encoding="utf-8", errors="replace",
                            capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise ValueError(f"Local Codex command failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codex", type=Path, help="native codex executable (no shell shim)")
    parser.add_argument("--out", type=Path, help="retain generated TS in this empty directory")
    parser.add_argument("--check-dir", type=Path, help="check an existing TS snapshot instead of generating")
    parser.add_argument("--features-file", type=Path, help="features list output, required with --check-dir")
    args = parser.parse_args(argv)
    if args.check_dir and (args.out or not args.features_file):
        parser.error("--check-dir requires --features-file and cannot be combined with --out")
    if args.features_file and not args.check_dir:
        parser.error("--features-file requires --check-dir")
    try:
        with tempfile.TemporaryDirectory(prefix="rendertest-appserver-schema-") as temporary:
            root = args.check_dir or args.out or Path(temporary) / "ts"
            version = "existing schema snapshot"
            if args.check_dir:
                features = args.features_file.read_text(encoding="utf-8")
            else:
                if root.exists() and any(root.iterdir()):
                    raise ValueError(f"Output directory must be empty to prevent stale schemas: {root}")
                root.mkdir(parents=True, exist_ok=True)
                home = Path(temporary) / "codex-home"
                home.mkdir()
                env = dict(os.environ, CODEX_HOME=str(home))
                codex = str(find_codex(args.codex))
                version = run_local([codex, "--version"], env).strip()
                run_local([codex, "app-server", "generate-ts", "--experimental", "--out", str(root.resolve())], env)
                features = run_local([codex, "features", "list"], env)
                if args.out:
                    (root / "features.txt").write_text(features, encoding="utf-8")
                    (root / "codex-version.txt").write_text(version + "\n", encoding="utf-8")
            errors = check_schema(root) + check_features(features)
            print(version)
            for error in errors:
                print(f"FAIL: {error}")
            if errors:
                return 1
            print(f"PASS: {sum(map(len, METHODS.values()))} methods, "
                  f"{sum(map(len, FIELDS.values()))} fields and {len(FEATURES)} feature switches")
            return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
