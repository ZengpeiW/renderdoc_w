from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from helpers import write_manifest
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.instances import discover_instances, select_instance
from rendertest_mcp.ipc_client import BridgeClient


def instances_root(root: Path) -> Path:
    return root / "ipc-v1" / "instances"


def test_stale_and_stopped_instances_are_offline(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    write_manifest(tmp_path, "online", heartbeat_ms=now_ms)
    write_manifest(tmp_path, "stale", heartbeat_ms=now_ms - 10_000)
    write_manifest(tmp_path, "stopped", stopped=True)

    instances = {item.instance_id: item for item in discover_instances(instances_root(tmp_path))}

    assert instances["online"].online is True
    assert instances["stale"].offline_reason == "stale-heartbeat"
    assert instances["stopped"].offline_reason == "stopped"


def test_explicit_instance_must_be_online(tmp_path: Path) -> None:
    write_manifest(tmp_path, "online")
    instances = discover_instances(instances_root(tmp_path))

    assert select_instance(instances, instance_id="online", effect="read").instance_id == "online"
    with pytest.raises(RenderTestAgentError) as error:
        select_instance(instances, instance_id="missing", effect="read")
    assert error.value.code == "BRIDGE_UNAVAILABLE"


def test_multiple_read_instances_choose_most_recent_activity(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    write_manifest(tmp_path, "older", activity_ms=now_ms - 1000)
    write_manifest(tmp_path, "newer", activity_ms=now_ms)

    selected = select_instance(discover_instances(instances_root(tmp_path)), instance_id=None, effect="read")

    assert selected.instance_id == "newer"


def test_multiple_instances_require_explicit_ui_target(tmp_path: Path) -> None:
    write_manifest(tmp_path, "first")
    write_manifest(tmp_path, "second")

    with pytest.raises(RenderTestAgentError) as error:
        select_instance(discover_instances(instances_root(tmp_path)), instance_id=None, effect="ui")

    assert error.value.code == "INSTANCE_AMBIGUOUS"


def test_environment_instance_overrides_recent_activity(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    write_manifest(tmp_path, "selected", activity_ms=now_ms - 1000)
    write_manifest(tmp_path, "recent", activity_ms=now_ms)
    client = BridgeClient(root=tmp_path, environ={"RENDERTEST_AGENT_INSTANCE_ID": "selected"})

    assert client.default_instance_id == "selected"
    assert [item["instanceId"] for item in client.list_instances()] == ["recent", "selected"]


def test_invalid_manifest_is_ignored(tmp_path: Path) -> None:
    directory = write_manifest(tmp_path, f"invalid-{uuid.uuid4().hex[:8]}")
    (directory / "instance.json").write_text("{}", encoding="utf-8")

    assert discover_instances(instances_root(tmp_path)) == []
