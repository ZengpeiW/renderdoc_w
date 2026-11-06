from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from fake_bridge import FakeRenderTestBridge
from helpers import ManualResponder, write_manifest
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.ipc_client import BridgeClient


def fixed_client(root: Path, instance_id: str, session_id: str) -> BridgeClient:
    return BridgeClient(
        root=root,
        environ={
            "RENDERTEST_AGENT_FIXED_INSTANCE_ID": instance_id,
            "RENDERTEST_AGENT_CAPTURE_SESSION_ID": session_id,
        },
        poll_interval=0.005,
        default_timeout=1,
    )


def response(request: dict, instance_id: str, session_id: str, result: dict | None = None) -> bytes:
    return json.dumps(
        {
            "schema": "rendertest-agent-response/v1",
            "protocol": "rendertest-agent-ipc/v1",
            "id": request["id"],
            "ok": True,
            "result": result or {},
            "meta": {"instanceId": instance_id, "captureSessionId": session_id},
        }
    ).encode()


@pytest.mark.parametrize("session", [None, "", "not-a-session", "a" * 31, "a" * 33, "A" * 32])
def test_fixed_mode_rejects_missing_or_invalid_session(tmp_path: Path, session: str | None) -> None:
    environ = {"RENDERTEST_AGENT_FIXED_INSTANCE_ID": "panel-instance"}
    if session is not None:
        environ["RENDERTEST_AGENT_CAPTURE_SESSION_ID"] = session
    with pytest.raises(RenderTestAgentError) as error:
        BridgeClient(root=tmp_path, environ=environ)
    assert error.value.code == "INVALID_REQUEST"
    assert not (tmp_path / "ipc-v1").exists()


def test_fixed_mode_rejects_invalid_instance(tmp_path: Path) -> None:
    with pytest.raises(RenderTestAgentError) as error:
        fixed_client(tmp_path, "../another-instance", uuid.uuid4().hex)
    assert error.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("method", ["ping", "get_status", "get_context", "inspect_event"])
@pytest.mark.parametrize("bind_capture", [None, False])
def test_first_request_is_bound_even_for_status_or_explicit_unbound_call(
    tmp_path: Path, method: str, bind_capture: bool | None
) -> None:
    instance_id, session = "panel-instance", uuid.uuid4().hex
    directory = write_manifest(tmp_path, instance_id, capture_session_id=session)
    requests = []

    def build(request: dict) -> bytes:
        requests.append(request)
        return response(request, instance_id, session)

    bridge_client = fixed_client(tmp_path, instance_id, session)
    with ManualResponder(directory, build):
        bridge_client.call(method, bind_capture=bind_capture)
    assert requests[0]["expected"] == {"instanceId": instance_id, "captureSessionId": session}
    assert bridge_client.pinned_capture_session(instance_id) == session


def test_first_request_rejects_capture_changed_before_any_tool_call(tmp_path: Path) -> None:
    with FakeRenderTestBridge(tmp_path) as bridge:
        original_session = bridge.capture_session_id
        bridge_client = fixed_client(tmp_path, bridge.instance_id, original_session)
        bridge.open_capture()
        with pytest.raises(RenderTestAgentError) as error:
            bridge_client.call("get_context")
        assert error.value.code == "CAPTURE_MISMATCH"
        assert bridge_client.pinned_capture_session(bridge.instance_id) == original_session


@pytest.mark.parametrize("method", ["ping", "get_status", "get_context", "inspect_event"])
def test_status_cannot_rebind_fixed_session_after_capture_switch(tmp_path: Path, method: str) -> None:
    with FakeRenderTestBridge(tmp_path) as bridge:
        original_session = bridge.capture_session_id
        bridge_client = fixed_client(tmp_path, bridge.instance_id, original_session)
        bridge_client.call("get_context")
        bridge.open_capture()
        with pytest.raises(RenderTestAgentError) as error:
            bridge_client.call(method, bind_capture=False)
        assert error.value.code == "CAPTURE_MISMATCH"
        assert bridge_client.pinned_capture_session(bridge.instance_id) == original_session
        with pytest.raises(RenderTestAgentError) as followup:
            bridge_client.call("inspect_event")
        assert followup.value.code == "CAPTURE_MISMATCH"


def test_fixed_instance_cannot_be_overridden_or_fall_back(tmp_path: Path) -> None:
    session = uuid.uuid4().hex
    other_directory = write_manifest(tmp_path, "another-window", capture_session_id=session)
    bridge_client = fixed_client(tmp_path, "missing-panel-window", session)
    with pytest.raises(RenderTestAgentError) as override:
        bridge_client.call("get_context", instance_id="another-window")
    assert override.value.code == "INVALID_REQUEST"
    with pytest.raises(RenderTestAgentError) as fallback:
        bridge_client.call("get_context")
    assert fallback.value.code == "BRIDGE_UNAVAILABLE"
    assert not list((other_directory / "requests").iterdir())


def test_fixed_instance_wins_over_legacy_default_and_only_lists_itself(tmp_path: Path) -> None:
    with FakeRenderTestBridge(tmp_path, instance_id="panel-window") as panel:
        with FakeRenderTestBridge(tmp_path, instance_id="another-window"):
            bridge_client = BridgeClient(
                root=tmp_path,
                environ={
                    "RENDERTEST_AGENT_INSTANCE_ID": "another-window",
                    "RENDERTEST_AGENT_FIXED_INSTANCE_ID": panel.instance_id,
                    "RENDERTEST_AGENT_CAPTURE_SESSION_ID": panel.capture_session_id,
                },
                poll_interval=0.005,
            )
            assert bridge_client.call("get_context").instance_id == panel.instance_id
            assert [item["instanceId"] for item in bridge_client.list_instances()] == [panel.instance_id]
            assert [item["instanceId"] for item in bridge_client.list_instances(include_offline=True)] == [
                panel.instance_id
            ]
            panel.open_capture()
            with pytest.raises(RenderTestAgentError) as error:
                bridge_client.list_instances()
            assert error.value.code == "CAPTURE_MISMATCH"


def test_cli_retains_explicit_instance_override_and_session_refresh(tmp_path: Path) -> None:
    with FakeRenderTestBridge(tmp_path, instance_id="default-window") as default:
        with FakeRenderTestBridge(tmp_path, instance_id="explicit-window") as explicit:
            bridge_client = BridgeClient(
                root=tmp_path,
                environ={"RENDERTEST_AGENT_INSTANCE_ID": default.instance_id},
                poll_interval=0.005,
            )
            assert bridge_client.call("get_context").instance_id == default.instance_id
            selected = bridge_client.call("get_context", instance_id=explicit.instance_id)
            assert selected.instance_id == explicit.instance_id
            explicit.open_capture()
            bridge_client.call("get_context", instance_id=explicit.instance_id)
            assert bridge_client.pinned_capture_session(explicit.instance_id) == explicit.capture_session_id
            assert len(bridge_client.list_instances()) == 2


def test_success_response_cannot_replace_fixed_session(tmp_path: Path) -> None:
    original_session, other_session = uuid.uuid4().hex, uuid.uuid4().hex
    directory = write_manifest(tmp_path, "panel-window", capture_session_id=original_session)
    bridge_client = fixed_client(tmp_path, "panel-window", original_session)
    with ManualResponder(directory, lambda request: response(request, "panel-window", other_session)):
        with pytest.raises(RenderTestAgentError) as error:
            bridge_client.call("get_context")
    assert error.value.code == "CAPTURE_MISMATCH"
    assert bridge_client.pinned_capture_session("panel-window") == original_session


def test_artifact_scope_is_fixed_but_cli_keeps_existing_behavior(tmp_path: Path) -> None:
    session = uuid.uuid4().hex
    bridge_client = fixed_client(tmp_path, "panel-window", session)
    bridge_client.validate_artifact_scope("panel-window", session)
    with pytest.raises(RenderTestAgentError) as instance_error:
        bridge_client.validate_artifact_scope("another-window", session)
    assert instance_error.value.code == "INVALID_REQUEST"
    with pytest.raises(RenderTestAgentError) as session_error:
        bridge_client.validate_artifact_scope("panel-window", uuid.uuid4().hex)
    assert session_error.value.code == "CAPTURE_MISMATCH"
    BridgeClient(root=tmp_path, environ={}).validate_artifact_scope("another-window", uuid.uuid4().hex)


def test_fixed_response_cannot_return_artifact_from_old_session(tmp_path: Path) -> None:
    session, old_session = uuid.uuid4().hex, uuid.uuid4().hex
    instance_id = "panel-window"
    directory = write_manifest(tmp_path, instance_id, capture_session_id=session)
    artifact = tmp_path / "artifacts" / instance_id / old_session / "trace.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"old session data")
    result = {
        "artifacts": [{"path": str(artifact), "bytes": artifact.stat().st_size, "sha256": "0" * 64}]
    }
    with ManualResponder(directory, lambda request: response(request, instance_id, session, result)):
        with pytest.raises(RenderTestAgentError) as error:
            fixed_client(tmp_path, instance_id, session).call("get_context")
    assert error.value.code == "INVALID_RESPONSE"


def test_fixed_mode_accepts_artifact_from_its_bound_session(tmp_path: Path) -> None:
    with FakeRenderTestBridge(tmp_path) as bridge:
        bridge_client = fixed_client(tmp_path, bridge.instance_id, bridge.capture_session_id)
        exported = bridge_client.call("export_texture", {"resource_id": "1001"})
        assert len(exported.result["artifacts"]) == 1
        artifact = Path(exported.result["artifacts"][0]["path"])
        assert artifact.is_relative_to(tmp_path / "artifacts" / bridge.instance_id / bridge.capture_session_id)
