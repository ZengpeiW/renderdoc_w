from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from helpers import ManualResponder, write_manifest
from fake_bridge import FakeRenderTestBridge
from rendertest_mcp import ipc_client
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.ipc_client import BridgeClient


def client(root: Path, **kwargs) -> BridgeClient:
    return BridgeClient(root=root, environ={}, poll_interval=0.005, **kwargs)


def test_end_to_end_call_through_fake_bridge(tmp_path: Path) -> None:
    with FakeRenderTestBridge(tmp_path) as bridge:
        response = client(tmp_path).call("get_status")

    assert response.instance_id == bridge.instance_id
    assert response.result["data"]["bridge"]["instanceId"] == bridge.instance_id
    assert response.meta["captureSessionId"] == bridge.capture_session_id
    assert not list((tmp_path / "ipc-v1" / "instances" / bridge.instance_id / "requests").iterdir())
    assert not list((tmp_path / "ipc-v1" / "instances" / bridge.instance_id / "responses").iterdir())


def test_response_id_must_match(tmp_path: Path, instance_id: str) -> None:
    directory = write_manifest(tmp_path, instance_id)

    def build(_: dict) -> bytes:
        return json.dumps(
            {
                "schema": "rendertest-agent-response/v1",
                "protocol": "rendertest-agent-ipc/v1",
                "id": uuid.uuid4().hex,
                "ok": True,
                "result": {},
                "meta": {"instanceId": instance_id},
            }
        ).encode()

    with ManualResponder(directory, build), pytest.raises(RenderTestAgentError) as error:
        client(tmp_path, default_timeout=1).call("get_status")

    assert error.value.code == "INVALID_RESPONSE"


def test_oversized_response_is_rejected(
    tmp_path: Path, instance_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = write_manifest(tmp_path, instance_id)
    monkeypatch.setattr(ipc_client, "MAX_RESPONSE_BYTES", 128)

    with ManualResponder(directory, lambda _: b"{" + b" " * 256 + b"}"), pytest.raises(
        RenderTestAgentError
    ) as error:
        client(tmp_path, default_timeout=1).call("get_status")

    assert error.value.code == "INVALID_RESPONSE"
    assert not list((directory / "responses").iterdir())


def test_timeout_removes_unclaimed_request(tmp_path: Path, instance_id: str) -> None:
    directory = write_manifest(tmp_path, instance_id)

    with pytest.raises(RenderTestAgentError) as error:
        client(tmp_path, default_timeout=0.05, response_grace=0).call("get_status")

    assert error.value.code == "TIMEOUT"
    assert error.value.details["requestClaimed"] is False
    assert not list((directory / "requests").iterdir())


def test_stale_bridge_during_wait_is_unavailable(tmp_path: Path, instance_id: str) -> None:
    directory = write_manifest(tmp_path, instance_id)

    with pytest.raises(RenderTestAgentError) as error:
        client(tmp_path, default_timeout=2, stale_after=0.1).call("get_status")

    assert error.value.code == "BRIDGE_UNAVAILABLE"
    assert not list((directory / "requests").iterdir())


def test_capture_session_is_pinned_and_refreshed(tmp_path: Path) -> None:
    with FakeRenderTestBridge(tmp_path) as bridge:
        bridge_client = client(tmp_path)
        first_session = bridge.capture_session_id
        bridge_client.call("get_status")
        assert bridge_client.pinned_capture_session(bridge.instance_id) == first_session
        bridge_client.call("inspect_event")

        bridge.open_capture()
        with pytest.raises(RenderTestAgentError) as error:
            bridge_client.call("inspect_event")
        assert error.value.code == "CAPTURE_MISMATCH"

        bridge_client.call("get_status")
        assert bridge_client.pinned_capture_session(bridge.instance_id) == bridge.capture_session_id
        assert bridge_client.call("inspect_event").result["data"]["action"]["eventId"] == 10


def test_no_capture_status_is_available_but_capture_tool_is_not(tmp_path: Path) -> None:
    with FakeRenderTestBridge(tmp_path, capture_loaded=False):
        bridge_client = client(tmp_path)
        status = bridge_client.call("get_status")
        with pytest.raises(RenderTestAgentError) as error:
            bridge_client.call("list_actions")

    assert status.result["data"]["capture"] is None
    assert error.value.code == "NO_CAPTURE"


def test_artifact_outside_instance_directory_is_rejected(tmp_path: Path, instance_id: str) -> None:
    directory = write_manifest(tmp_path, instance_id)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")

    def build(request: dict) -> bytes:
        return json.dumps(
            {
                "schema": "rendertest-agent-response/v1",
                "protocol": "rendertest-agent-ipc/v1",
                "id": request["id"],
                "ok": True,
                "result": {
                    "data": {},
                    "artifacts": [
                        {
                            "path": str(outside),
                            "bytes": outside.stat().st_size,
                            "sha256": "0" * 64,
                        }
                    ],
                },
                "meta": {"instanceId": instance_id},
            }
        ).encode()

    with ManualResponder(directory, build), pytest.raises(RenderTestAgentError) as error:
        client(tmp_path, default_timeout=1).call("get_status")

    assert error.value.code == "INVALID_RESPONSE"
