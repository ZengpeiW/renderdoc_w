from __future__ import annotations

import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RenderTestAgentError
from .files import atomic_write_bytes, json_bytes, read_json_file
from .instances import InstanceInfo, discover_instances, read_instance, select_instance
from .paths import agent_root, artifacts_root, instances_root
from .protocol import (
    CLIENT_NAME,
    CLIENT_VERSION,
    DEFAULT_TIMEOUT_SECONDS,
    INSTANCE_STALE_SECONDS,
    MAX_DEADLINE_MS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    METHOD_SPECS,
    PROTOCOL,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    UNBOUND_METHODS,
)

_SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class BridgeResponse:
    request_id: str
    instance_id: str
    result: Any
    meta: dict[str, Any]


class BridgeClient:
    def __init__(
        self,
        *,
        root: Path | str | None = None,
        environ: Mapping[str, str] | None = None,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        response_grace: float = 1.0,
        poll_interval: float = 0.02,
        stale_after: float = INSTANCE_STALE_SECONDS,
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self.root = Path(root).resolve() if root is not None else agent_root(self.environ)
        self.instances_root = instances_root(self.root)
        self.artifacts_root = artifacts_root(self.root)
        self.default_timeout = default_timeout
        self.response_grace = response_grace
        self.poll_interval = poll_interval
        self.stale_after = stale_after
        self.default_instance_id = self.environ.get("RENDERTEST_AGENT_INSTANCE_ID", "").strip()
        self._capture_sessions: dict[str, str] = {}
        self._lock = threading.Lock()

    def list_instances(self, *, include_offline: bool = False) -> list[dict[str, Any]]:
        instances = discover_instances(self.instances_root, stale_after=self.stale_after)
        return [item.summary() for item in instances if include_offline or item.online]

    def pinned_capture_session(self, instance_id: str) -> str | None:
        return self._capture_sessions.get(instance_id)

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        instance_id: str | None = None,
        timeout: float | None = None,
        bind_capture: bool | None = None,
    ) -> BridgeResponse:
        spec = METHOD_SPECS.get(method)
        if spec is None:
            raise RenderTestAgentError("UNKNOWN_METHOD", f"Unknown bridge method: {method}")
        timeout = self.default_timeout if timeout is None else float(timeout)
        deadline_ms = int(timeout * 1000)
        if deadline_ms < 1 or deadline_ms > MAX_DEADLINE_MS:
            raise RenderTestAgentError("INVALID_REQUEST", "The request timeout is out of range.")

        with self._lock:
            instances = discover_instances(self.instances_root, stale_after=self.stale_after)
            instance = select_instance(
                instances,
                instance_id=instance_id or self.default_instance_id or None,
                effect=spec["effect"],
            )
            should_bind = method not in UNBOUND_METHODS if bind_capture is None else bind_capture
            expected: dict[str, str] = {"instanceId": instance.instance_id}
            pinned_session = self._capture_sessions.get(instance.instance_id)
            if should_bind and pinned_session:
                expected["captureSessionId"] = pinned_session

            request_id = uuid.uuid4().hex
            created = time.time()
            request = {
                "schema": REQUEST_SCHEMA,
                "protocol": PROTOCOL,
                "id": request_id,
                "method": method,
                "params": params or {},
                "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created)),
                "createdAtUnixMs": int(created * 1000),
                "deadlineMs": deadline_ms,
                "expected": expected,
                "client": {"name": CLIENT_NAME, "version": CLIENT_VERSION, "pid": os.getpid()},
            }
            data = json_bytes(request)
            if len(data) > MAX_REQUEST_BYTES:
                raise RenderTestAgentError("LIMIT_EXCEEDED", "The request is too large.")

            request_path = instance.directory / "requests" / f"{request_id}.json"
            response_path = instance.directory / "responses" / f"{request_id}.json"
            atomic_write_bytes(request_path, data)
            response = self._wait_for_response(
                instance, request_id, request_path, response_path, created + timeout
            )
            self._update_capture_session(instance.instance_id, method, response)
            return response

    def _wait_for_response(
        self,
        instance: InstanceInfo,
        request_id: str,
        request_path: Path,
        response_path: Path,
        deadline: float,
    ) -> BridgeResponse:
        final_deadline = deadline + self.response_grace
        next_liveness_check = time.time() + 0.5
        while time.time() <= final_deadline:
            if response_path.exists():
                return self._consume_response(instance, request_id, response_path)
            now = time.time()
            if now >= next_liveness_check:
                current = read_instance(instance.directory, stale_after=self.stale_after)
                if current is None or not current.online:
                    claimed = not self._remove_unclaimed(request_path)
                    raise RenderTestAgentError(
                        "BRIDGE_UNAVAILABLE",
                        "The RenderTest instance stopped responding.",
                        details={"instanceId": instance.instance_id, "requestClaimed": claimed},
                    )
                next_liveness_check = now + 0.5
            time.sleep(self.poll_interval)

        claimed = not self._remove_unclaimed(request_path)
        raise RenderTestAgentError(
            "TIMEOUT",
            "Timed out waiting for the RenderTest bridge.",
            details={"instanceId": instance.instance_id, "requestClaimed": claimed},
        )

    @staticmethod
    def _remove_unclaimed(request_path: Path) -> bool:
        try:
            request_path.unlink()
            return True
        except FileNotFoundError:
            return False

    def _consume_response(
        self, instance: InstanceInfo, request_id: str, response_path: Path
    ) -> BridgeResponse:
        try:
            response = read_json_file(response_path, MAX_RESPONSE_BYTES)
        except (UnicodeDecodeError, ValueError) as error:
            raise RenderTestAgentError(
                "INVALID_RESPONSE", "The bridge response is not valid JSON."
            ) from error
        finally:
            response_path.unlink(missing_ok=True)

        if not isinstance(response, dict):
            raise RenderTestAgentError("INVALID_RESPONSE", "The bridge response must be an object.")
        if response.get("schema") != RESPONSE_SCHEMA or response.get("protocol") != PROTOCOL:
            raise RenderTestAgentError("INVALID_RESPONSE", "The bridge response schema is invalid.")
        if response.get("id") != request_id:
            raise RenderTestAgentError(
                "INVALID_RESPONSE",
                "The bridge response id does not match the request.",
                details={"expected": request_id, "actual": response.get("id")},
            )
        meta = response.get("meta")
        if not isinstance(meta, dict) or meta.get("instanceId") != instance.instance_id:
            raise RenderTestAgentError(
                "INVALID_RESPONSE", "The bridge response belongs to another instance."
            )
        ok = response.get("ok")
        if ok is False:
            error = response.get("error")
            if not isinstance(error, dict):
                raise RenderTestAgentError("INVALID_RESPONSE", "The bridge error is invalid.")
            code = error.get("code")
            message = error.get("message")
            retryable = error.get("retryable")
            details = error.get("details", {})
            if not isinstance(code, str) or not isinstance(message, str) or type(retryable) is not bool:
                raise RenderTestAgentError("INVALID_RESPONSE", "The bridge error is invalid.")
            if not isinstance(details, dict):
                raise RenderTestAgentError("INVALID_RESPONSE", "The bridge error details are invalid.")
            raise RenderTestAgentError(code, message, retryable=retryable, details=details)
        if ok is not True or "result" not in response:
            raise RenderTestAgentError("INVALID_RESPONSE", "The bridge success response is invalid.")

        result = response["result"]
        self._validate_artifacts(instance.instance_id, result)
        return BridgeResponse(request_id, instance.instance_id, result, meta)

    def _validate_artifacts(self, instance_id: str, result: Any) -> None:
        if not isinstance(result, dict) or "artifacts" not in result:
            return
        artifacts = result["artifacts"]
        if not isinstance(artifacts, list):
            raise RenderTestAgentError("INVALID_RESPONSE", "Bridge artifacts must be a list.")
        allowed_root = (self.artifacts_root / instance_id).resolve()
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
                raise RenderTestAgentError("INVALID_RESPONSE", "A bridge artifact is invalid.")
            path = Path(artifact["path"])
            if not path.is_absolute():
                raise RenderTestAgentError("INVALID_RESPONSE", "Artifact paths must be absolute.")
            resolved = path.resolve()
            if not resolved.is_relative_to(allowed_root) or not resolved.is_file():
                raise RenderTestAgentError(
                    "INVALID_RESPONSE",
                    "A bridge artifact is outside its instance artifact directory.",
                    details={"path": artifact["path"]},
                )
            expected_bytes = artifact.get("bytes")
            if type(expected_bytes) is not int or resolved.stat().st_size != expected_bytes:
                raise RenderTestAgentError("INVALID_RESPONSE", "Artifact size does not match.")
            digest = artifact.get("sha256")
            if not isinstance(digest, str) or not _SHA256_PATTERN.match(digest):
                raise RenderTestAgentError("INVALID_RESPONSE", "Artifact SHA-256 is invalid.")

    def _update_capture_session(
        self, instance_id: str, method: str, response: BridgeResponse
    ) -> None:
        session = response.meta.get("captureSessionId")
        if isinstance(session, str) and _SESSION_PATTERN.match(session):
            self._capture_sessions[instance_id] = session
        elif method in UNBOUND_METHODS:
            self._capture_sessions.pop(instance_id, None)
