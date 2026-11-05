from __future__ import annotations

import ast
import json
import time
import uuid
from pathlib import Path
from typing import Any

from rendertest_agent_bridge import constants as bridge_constants
from rendertest_agent_bridge.ipc import AgentBridgeError, BridgeResult, MailboxServer
from rendertest_mcp import protocol as mcp_protocol


def make_request(
    method: str = "ping",
    *,
    request_id: str | None = None,
    params: dict[str, Any] | None = None,
    expected: dict[str, Any] | None = None,
    created_ms: int | None = None,
    deadline_ms: int = 30_000,
) -> dict[str, Any]:
    return {
        "schema": "rendertest-agent-request/v1",
        "protocol": "rendertest-agent-ipc/v1",
        "id": request_id or uuid.uuid4().hex,
        "method": method,
        "params": params or {},
        "createdAtUnixMs": created_ms or int(time.time() * 1000),
        "deadlineMs": deadline_ms,
        "expected": expected or {},
    }


def write_request(server: MailboxServer, request: dict[str, Any], file_id: str | None = None) -> None:
    path = Path(server.request_dir) / f"{file_id or request['id']}.json"
    path.write_text(json.dumps(request), encoding="utf-8")


def read_response(server: MailboxServer, request_id: str) -> dict[str, Any]:
    return json.loads((Path(server.response_dir) / f"{request_id}.json").read_text(encoding="utf-8"))


def create_server(tmp_path: Path, instance_id: str, state: dict[str, Any], handler=None, **kwargs):
    return MailboxServer(
        tmp_path,
        instance_id,
        handler or (lambda context: {"method": context.method, "params": context.params}),
        lambda: state,
        **kwargs,
    )


def run_one(server: MailboxServer, request: dict[str, Any], file_id: str | None = None) -> dict[str, Any]:
    write_request(server, request, file_id)
    assert server.poll_once() == 1
    assert server.process_next()
    return read_response(server, file_id or request["id"])


def test_request_is_claimed_processed_and_removed(tmp_path: Path, instance_id: str) -> None:
    state = {"captureLoaded": False}
    server = create_server(tmp_path, instance_id, state)
    server.start(start_worker=False)
    request = make_request(params={"value": 1})

    response = run_one(server, request)

    assert response["ok"] is True
    assert response["result"] == {"method": "ping", "params": {"value": 1}}
    assert response["meta"]["instanceId"] == instance_id
    assert not list(Path(server.processing_dir).iterdir())
    manifest = json.loads(Path(server.manifest_path).read_text(encoding="utf-8"))
    assert manifest["instanceId"] == instance_id
    assert manifest["heartbeatUnixMs"] > 0


def test_request_file_name_must_match_request_id(tmp_path: Path, instance_id: str) -> None:
    server = create_server(tmp_path, instance_id, {"captureLoaded": False})
    server.start(start_worker=False)
    file_id = uuid.uuid4().hex

    response = run_one(server, make_request(), file_id)

    assert response["error"]["code"] == "INVALID_REQUEST"


def test_unknown_method_is_rejected(tmp_path: Path, instance_id: str) -> None:
    server = create_server(tmp_path, instance_id, {"captureLoaded": False}, methods=("ping",))
    server.start(start_worker=False)

    response = run_one(server, make_request("get_status"))

    assert response["error"]["code"] == "UNKNOWN_METHOD"


def test_capture_methods_require_capture(tmp_path: Path, instance_id: str) -> None:
    server = create_server(tmp_path, instance_id, {"captureLoaded": False})
    server.start(start_worker=False)

    status = run_one(server, make_request("get_status"))
    actions = run_one(server, make_request("list_actions"))

    assert status["ok"] is True
    assert actions["error"]["code"] == "NO_CAPTURE"


def test_expected_capture_session_must_match(tmp_path: Path, instance_id: str) -> None:
    state = {"captureLoaded": True, "captureSessionId": uuid.uuid4().hex}
    server = create_server(tmp_path, instance_id, state)
    server.start(start_worker=False)

    response = run_one(
        server, make_request("list_actions", expected={"captureSessionId": uuid.uuid4().hex})
    )

    assert response["error"]["code"] == "CAPTURE_MISMATCH"


def test_capture_change_during_request_is_reported(tmp_path: Path, instance_id: str) -> None:
    state = {"captureLoaded": True, "captureSessionId": uuid.uuid4().hex}

    def handler(_: Any) -> dict[str, bool]:
        state["captureSessionId"] = uuid.uuid4().hex
        return {"done": True}

    server = create_server(tmp_path, instance_id, state, handler)
    server.start(start_worker=False)

    response = run_one(server, make_request("list_actions"))

    assert response["error"]["code"] == "CAPTURE_CHANGED"
    assert response["error"]["retryable"] is True


def test_handler_errors_and_metadata_are_serialized(tmp_path: Path, instance_id: str) -> None:
    state = {"captureLoaded": True, "captureSessionId": uuid.uuid4().hex}

    def handler(context: Any) -> BridgeResult:
        if context.params.get("fail"):
            raise AgentBridgeError("INVALID_EVENT", "bad event", details={"event_id": 99})
        return BridgeResult({"ok": True}, {"eventIdBefore": 10, "eventIdAfter": 10})

    server = create_server(tmp_path, instance_id, state, handler)
    server.start(start_worker=False)

    success = run_one(server, make_request("list_actions"))
    failure = run_one(server, make_request("list_actions", params={"fail": True}))

    assert success["meta"]["eventIdBefore"] == 10
    assert failure["error"] == {
        "code": "INVALID_EVENT",
        "message": "bad event",
        "retryable": False,
        "details": {"event_id": 99},
    }


def test_full_queue_returns_busy(tmp_path: Path, instance_id: str) -> None:
    server = create_server(tmp_path, instance_id, {"captureLoaded": False}, queue_limit=1)
    server.start(start_worker=False)
    first = make_request()
    second = make_request()
    write_request(server, first)
    write_request(server, second)

    assert server.poll_once() == 1
    responses = list(Path(server.response_dir).glob("*.json"))
    assert len(responses) == 1
    busy = json.loads(responses[0].read_text(encoding="utf-8"))
    assert busy["error"]["code"] == "BRIDGE_BUSY"


def test_stale_processing_file_is_returned_as_restart(tmp_path: Path, instance_id: str) -> None:
    server = create_server(tmp_path, instance_id, {"captureLoaded": False})
    request_id = uuid.uuid4().hex
    processing = Path(server.processing_dir)
    processing.mkdir(parents=True)
    (processing / f"{request_id}.json").write_text("{}", encoding="utf-8")

    server.start(start_worker=False)

    assert read_response(server, request_id)["error"]["code"] == "BRIDGE_RESTARTED"
    assert not list(processing.iterdir())


def test_expired_request_times_out(tmp_path: Path, instance_id: str) -> None:
    server = create_server(tmp_path, instance_id, {"captureLoaded": False})
    server.start(start_worker=False)

    response = run_one(server, make_request(created_ms=1, deadline_ms=1))

    assert response["error"]["code"] == "TIMEOUT"


def test_stop_cancels_queued_requests_and_marks_manifest_stopped(
    tmp_path: Path, instance_id: str
) -> None:
    server = create_server(tmp_path, instance_id, {"captureLoaded": False})
    server.start(start_worker=False)
    request = make_request()
    write_request(server, request)
    assert server.poll_once() == 1

    server.stop()

    assert read_response(server, request["id"])["error"]["code"] == "CANCELLED"
    manifest = json.loads(Path(server.manifest_path).read_text(encoding="utf-8"))
    assert manifest["stopped"] is True


def test_bridge_and_mcp_protocol_constants_match() -> None:
    for name in (
        "PROTOCOL",
        "INSTANCE_SCHEMA",
        "REQUEST_SCHEMA",
        "RESPONSE_SCHEMA",
        "MAX_REQUEST_BYTES",
        "MAX_RESPONSE_BYTES",
        "MAX_DEADLINE_MS",
        "METHOD_SPECS",
    ):
        assert getattr(bridge_constants, name) == getattr(mcp_protocol, name)
    for code, retryable in bridge_constants.ERROR_RETRYABLE.items():
        assert mcp_protocol.ERROR_RETRYABLE[code] is retryable


def test_bridge_source_is_python36_compatible() -> None:
    bridge_root = Path(bridge_constants.__file__).resolve().parent
    forbidden_imports = {"contextvars", "dataclasses"}
    for path in bridge_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 6))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "__future__" or all(
                    alias.name != "annotations" for alias in node.names
                ), path
                assert node.module not in forbidden_imports, path
            elif isinstance(node, ast.Import):
                assert not forbidden_imports.intersection(alias.name for alias in node.names), path
