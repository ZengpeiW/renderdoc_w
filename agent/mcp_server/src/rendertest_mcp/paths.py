from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def agent_root(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get("RENDERTEST_AGENT_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    local_root = (environ.get("LOCALAPPDATA") or environ.get("XDG_DATA_HOME") or "").strip()
    if local_root:
        base = Path(local_root)
    else:
        base = Path.home() / ".local" / "share"
    return (base / "RenderTest" / "agent").resolve()


def instances_root(root: Path) -> Path:
    return root / "ipc-v1" / "instances"


def artifacts_root(root: Path) -> Path:
    return root / "artifacts"
