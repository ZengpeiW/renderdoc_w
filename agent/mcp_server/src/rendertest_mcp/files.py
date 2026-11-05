from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import RenderTestAgentError


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())

    last_error: PermissionError | None = None
    for _ in range(50):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as error:
            last_error = error
            time.sleep(0.01)
    temporary.unlink(missing_ok=True)
    assert last_error is not None
    raise last_error


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, json_bytes(value))


def read_json_file(path: Path, max_bytes: int) -> Any:
    last_error: PermissionError | None = None
    for _ in range(20):
        try:
            size = path.stat().st_size
            if size > max_bytes:
                raise RenderTestAgentError(
                    "INVALID_RESPONSE",
                    "The JSON file is too large.",
                    details={"path": str(path), "bytes": size, "maxBytes": max_bytes},
                )
            with path.open("rb") as stream:
                data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise RenderTestAgentError(
                    "INVALID_RESPONSE",
                    "The JSON file is too large.",
                    details={"path": str(path), "maxBytes": max_bytes},
                )
            return json.loads(data.decode("utf-8"))
        except PermissionError as error:
            last_error = error
            time.sleep(0.01)
    assert last_error is not None
    raise last_error
