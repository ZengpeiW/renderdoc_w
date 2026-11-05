from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
for path in (
    AGENT_DIR / "mcp_server" / "src",
    AGENT_DIR / "bridge",
    AGENT_DIR / "devtools",
    AGENT_DIR / "tests",
):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)


@pytest.fixture
def instance_id() -> str:
    return f"test-{uuid.uuid4().hex[:12]}"
