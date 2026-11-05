"""Print the Codex MCP configuration for the RenderTest agent.

Usage:
    python devtools/codex_config.py [--root DIR] [--format toml|args]

``toml`` prints a ``~/.codex/config.toml`` snippet. ``args`` prints the equivalent ``-c`` overrides
as a JSON list, for ``codex exec`` runs that should not touch the user's config.
Omit ``--root`` to use the default ``%LOCALAPPDATA%/RenderTest/agent`` shared with qrendertest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PYTHON = AGENT_DIR / "mcp_server" / ".venv" / "Scripts" / "python.exe"
SERVER_NAME = "rendertest"
TOOL_TIMEOUT_SECONDS = 180


def server_config(python: Path = DEFAULT_PYTHON, root: Path | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "command": Path(python).resolve().as_posix(),
        "args": ["-m", "rendertest_mcp"],
        "tool_timeout_sec": TOOL_TIMEOUT_SECONDS,
    }
    if root is not None:
        config["env"] = {"RENDERTEST_AGENT_ROOT": Path(root).resolve().as_posix()}
    return config


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML value: {value!r}")


def toml_snippet(config: dict[str, Any], name: str = SERVER_NAME) -> str:
    lines = [f"[mcp_servers.{name}]"]
    lines += [f"{key} = {_toml_value(value)}" for key, value in config.items() if key != "env"]
    if config.get("env"):
        lines += ["", f"[mcp_servers.{name}.env]"]
        lines += [f"{key} = {_toml_value(value)}" for key, value in config["env"].items()]
    return "\n".join(lines) + "\n"


def cli_overrides(config: dict[str, Any], name: str = SERVER_NAME) -> list[str]:
    overrides = []
    for key, value in config.items():
        if key == "env":
            overrides += [f"mcp_servers.{name}.env.{env_key}={_toml_value(env_value)}"
                          for env_key, env_value in value.items()]
        else:
            overrides.append(f"mcp_servers.{name}.{key}={_toml_value(value)}")
    args = []
    for override in overrides:
        args += ["-c", override]
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the Codex MCP config for RenderTest.")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--format", choices=("toml", "args"), default="toml")
    args = parser.parse_args(argv)
    if not args.python.exists():
        print(f"warning: {args.python} does not exist; create the venv first", file=sys.stderr)
    config = server_config(args.python, args.root)
    if args.format == "toml":
        sys.stdout.write(toml_snippet(config))
    else:
        print(json.dumps(cli_overrides(config), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
