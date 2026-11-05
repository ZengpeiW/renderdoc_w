from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RenderTestAgentError
from .files import read_json_file
from .protocol import EFFECT_UI, INSTANCE_SCHEMA, PROTOCOL


@dataclass(frozen=True)
class InstanceInfo:
    instance_id: str
    directory: Path
    manifest: dict[str, Any]
    heartbeat_unix_ms: int
    last_activity_unix_ms: int
    online: bool
    offline_reason: str = ""

    @property
    def capture_session_id(self) -> str:
        return str(self.manifest.get("captureSessionId") or "")

    def summary(self) -> dict[str, Any]:
        return {
            "instanceId": self.instance_id,
            "online": self.online,
            "offlineReason": self.offline_reason,
            "pid": self.manifest.get("pid"),
            "bridgeVersion": self.manifest.get("bridgeVersion", ""),
            "renderdocVersion": self.manifest.get("renderdocVersion", ""),
            "heartbeatAt": self.manifest.get("heartbeatAt", ""),
            "lastActivityAt": self.manifest.get("lastActivityAt", ""),
            "captureLoaded": bool(self.manifest.get("captureLoaded")),
            "capturePath": self.manifest.get("capturePath", ""),
            "captureSessionId": self.capture_session_id,
            "api": self.manifest.get("api", ""),
            "selectedEventId": self.manifest.get("selectedEventId"),
            "currentEventId": self.manifest.get("currentEventId"),
        }


def _int_field(manifest: dict[str, Any], key: str) -> int | None:
    value = manifest.get(key)
    return value if type(value) is int and value > 0 else None


def read_instance(directory: Path, *, now: float | None = None,
                  stale_after: float = 5.0) -> InstanceInfo | None:
    manifest_path = directory / "instance.json"
    try:
        manifest = read_json_file(manifest_path, 1024 * 1024)
    except (OSError, ValueError, RenderTestAgentError):
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("schema") != INSTANCE_SCHEMA or manifest.get("protocol") != PROTOCOL:
        return None
    if manifest.get("instanceId") != directory.name:
        return None

    heartbeat = _int_field(manifest, "heartbeatUnixMs")
    activity = _int_field(manifest, "lastActivityUnixMs")
    if heartbeat is None or activity is None:
        return None

    current_ms = int((time.time() if now is None else now) * 1000)
    reason = ""
    if manifest.get("stopped") is True:
        reason = "stopped"
    elif current_ms - heartbeat > int(stale_after * 1000):
        reason = "stale-heartbeat"

    return InstanceInfo(
        instance_id=directory.name,
        directory=directory,
        manifest=manifest,
        heartbeat_unix_ms=heartbeat,
        last_activity_unix_ms=activity,
        online=not reason,
        offline_reason=reason,
    )


def discover_instances(root: Path, *, now: float | None = None,
                       stale_after: float = 5.0) -> list[InstanceInfo]:
    try:
        directories = [path for path in root.iterdir() if path.is_dir()]
    except FileNotFoundError:
        return []
    instances = [
        instance
        for directory in directories
        if (instance := read_instance(directory, now=now, stale_after=stale_after)) is not None
    ]
    return sorted(instances, key=lambda item: item.last_activity_unix_ms, reverse=True)


def select_instance(instances: list[InstanceInfo], *, instance_id: str | None,
                    effect: str) -> InstanceInfo:
    online = [instance for instance in instances if instance.online]
    if instance_id:
        for instance in online:
            if instance.instance_id == instance_id:
                return instance
        raise RenderTestAgentError(
            "BRIDGE_UNAVAILABLE",
            "The requested RenderTest instance is not online.",
            details={"instanceId": instance_id},
        )
    if not online:
        raise RenderTestAgentError("BRIDGE_UNAVAILABLE", "No online RenderTest instance was found.")
    if len(online) == 1:
        return online[0]
    if effect == EFFECT_UI:
        raise RenderTestAgentError(
            "INSTANCE_AMBIGUOUS",
            "Multiple RenderTest instances are online; specify instance_id for UI changes.",
            details={"instanceIds": [instance.instance_id for instance in online]},
        )
    return max(online, key=lambda item: item.last_activity_unix_ms)
