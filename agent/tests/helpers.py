from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable


def write_manifest(
    root: Path,
    instance_id: str,
    *,
    heartbeat_ms: int | None = None,
    activity_ms: int | None = None,
    stopped: bool = False,
    capture_session_id: str = "",
) -> Path:
    directory = root / "ipc-v1" / "instances" / instance_id
    for name in ("requests", "processing", "responses"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    manifest = {
        "schema": "rendertest-agent-instance/v1",
        "protocol": "rendertest-agent-ipc/v1",
        "instanceId": instance_id,
        "pid": 1,
        "heartbeatUnixMs": heartbeat_ms or now_ms,
        "lastActivityUnixMs": activity_ms or now_ms,
        "captureLoaded": bool(capture_session_id),
        "captureSessionId": capture_session_id,
        "stopped": stopped,
    }
    (directory / "instance.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


class ManualResponder:
    def __init__(self, directory: Path, build_response: Callable[[dict[str, Any]], bytes]) -> None:
        self.directory = directory
        self.build_response = build_response
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> ManualResponder:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop_event.set()
        self.thread.join(2.0)

    def _run(self) -> None:
        requests = self.directory / "requests"
        responses = self.directory / "responses"
        while not self.stop_event.is_set():
            for path in requests.glob("*.json"):
                try:
                    request = json.loads(path.read_text(encoding="utf-8"))
                    path.unlink()
                except (FileNotFoundError, json.JSONDecodeError, PermissionError):
                    continue
                temporary = responses / f"{request['id']}.json.tmp"
                temporary.write_bytes(self.build_response(request))
                temporary.replace(responses / f"{request['id']}.json")
            time.sleep(0.005)
