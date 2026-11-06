"""Bounded text reads of artifacts returned to this MCP process by the bridge."""

from __future__ import annotations

import codecs
import hashlib
import io
import json
import os
import re
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RenderTestAgentError
from .ipc_client import BridgeClient, BridgeResponse
from .protocol import EFFECT_READ, RESULT_SCHEMA

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_PAGE_BYTES = 128 * 1024
DEFAULT_PAGE_BYTES = 64 * 1024
MAX_REGISTERED_ARTIFACTS = 1024
TEXT_SUFFIXES = frozenset({".txt", ".jsonl", ".json", ".csv", ".log"})
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _Artifact:
    path: Path
    instance_id: str
    session_id: str
    size: int
    sha256: str


class ArtifactReader:
    def __init__(self, bridge: BridgeClient) -> None:
        self.bridge = bridge
        self.root = bridge.root / "artifacts"
        self._registered: OrderedDict[str, _Artifact] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(str(path))

    def _path(self, value: str, instance: str, session: str) -> Path:
        if not _COMPONENT.fullmatch(instance) or not _COMPONENT.fullmatch(session):
            raise RenderTestAgentError("INVALID_RESPONSE", "Artifact scope is missing or invalid.")
        path = Path(value)
        scope = self.root / instance / session
        if not path.is_absolute() or ".." in path.parts or path.suffix.lower() not in TEXT_SUFFIXES:
            raise RenderTestAgentError("INVALID_REQUEST", "Expected an absolute text artifact path.")
        try:
            path.relative_to(scope)
            # Reject links and Windows junctions, including the artifacts directory itself.
            relative = path.relative_to(self.root)
            component = self.root
            for part in (None, *relative.parts):
                if part is not None:
                    component = component / part
                info = component.lstat()
                if stat.S_ISLNK(info.st_mode) or (
                    getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ):
                    raise ValueError("linked artifact path")
            resolved = path.resolve(strict=True)
            resolved.relative_to(scope)
            if not stat.S_ISREG(resolved.stat().st_mode):
                raise ValueError("not a regular file")
        except (OSError, ValueError, RuntimeError) as error:
            raise RenderTestAgentError(
                "INVALID_REQUEST", "Artifact must be a regular file inside its instance and capture directory."
            ) from error
        return resolved

    def register(self, response: BridgeResponse) -> None:
        """Only bridge responses can grant read access; user-supplied metadata never does."""
        result = response.result
        if not isinstance(result, dict):
            return
        artifacts = result.get("artifacts", [])
        if not isinstance(artifacts, list):
            return
        instance = response.instance_id
        session = response.meta.get("captureSessionId") or ""
        for metadata in artifacts:
            if not isinstance(metadata, dict):
                continue
            size = metadata.get("bytes")
            digest = metadata.get("sha256")
            if (type(size) is not int or size < 0 or size > MAX_ARTIFACT_BYTES
                    or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                    or not isinstance(metadata.get("path"), str)):
                continue
            try:
                self.bridge.validate_artifact_scope(instance, session)
                path = self._path(metadata["path"], instance, session)
            except RenderTestAgentError:
                # Binary exports and invalid/oversized metadata grant no local read access.
                continue
            artifact = _Artifact(path, instance, session, size, digest)
            with self._lock:
                key = self._key(path)
                self._registered[key] = artifact
                self._registered.move_to_end(key)
                while len(self._registered) > MAX_REGISTERED_ARTIFACTS:
                    self._registered.popitem(last=False)

    def _read_verified(self, artifact: _Artifact) -> bytes:
        self.bridge.validate_artifact_scope(artifact.instance_id, artifact.session_id)
        path = self._path(str(artifact.path), artifact.instance_id, artifact.session_id)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            before = path.stat()
            if before.st_size != artifact.size or before.st_size > MAX_ARTIFACT_BYTES:
                raise RenderTestAgentError("INVALID_RESPONSE", "Artifact size no longer matches its metadata.")
            with os.fdopen(os.open(path, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                    raise RenderTestAgentError("INVALID_RESPONSE", "Artifact changed while opening it.")
                self._path(str(path), artifact.instance_id, artifact.session_id)
                data = stream.read(MAX_ARTIFACT_BYTES + 1)
        except OSError as error:
            raise RenderTestAgentError("INVALID_REQUEST", "Artifact cannot be opened.") from error
        if len(data) != artifact.size or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise RenderTestAgentError("INVALID_RESPONSE", "Artifact contents no longer match its SHA-256 metadata.")
        self.bridge.validate_artifact_scope(artifact.instance_id, artifact.session_id)
        if b"\x00" in data:
            raise RenderTestAgentError("UNSUPPORTED", "Artifact contains binary data, not UTF-8 text.")
        try:
            decoder = codecs.getincrementaldecoder("utf-8")()
            for offset in range(0, len(data), 65536):
                decoder.decode(data[offset:offset + 65536], final=False)
            decoder.decode(b"", final=True)
        except UnicodeError as error:
            raise RenderTestAgentError("UNSUPPORTED", "Artifact is not UTF-8 text.") from error
        return data

    def read(self, path: str, *, start_line: int = 1, limit: int = 100,
             search: str | None = None, max_bytes: int = DEFAULT_PAGE_BYTES) -> dict[str, Any]:
        if (type(start_line) is not int or start_line < 1 or type(limit) is not int
                or not 1 <= limit <= 1000 or type(max_bytes) is not int
                or not 256 <= max_bytes <= MAX_PAGE_BYTES
                or (search is not None and (not isinstance(search, str) or not 1 <= len(search) <= 256))):
            raise RenderTestAgentError("INVALID_REQUEST", "Invalid artifact page or search limits.")
        requested = Path(path)
        if not requested.is_absolute() or ".." in requested.parts:
            raise RenderTestAgentError("INVALID_REQUEST", "Expected a registered absolute artifact path.")
        with self._lock:
            artifact = self._registered.get(self._key(requested))
            if artifact:
                self._registered.move_to_end(self._key(requested))
        if artifact is None:
            raise RenderTestAgentError(
                "INVALID_REQUEST", "This text artifact was not returned by a tool in this MCP process. "
                "Run the producing tool again to obtain a registered path."
            )
        data = self._read_verified(artifact)
        needle = search.encode("utf-8") if search is not None else None
        lines: list[dict[str, Any]] = []
        used = 2  # JSON array brackets; the budget includes escaped line text and line numbers.
        next_line = None
        for number, raw in enumerate(io.BytesIO(data), 1):
            if number < start_line or (needle is not None and needle not in raw):
                continue
            if len(lines) >= limit:
                next_line = number
                break
            entry = {"line": number, "text": raw.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8")}
            size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + (2 if lines else 0)
            if used + size > max_bytes:
                if not lines:
                    raise RenderTestAgentError(
                        "LIMIT_EXCEEDED", "A matching line exceeds the page byte budget. "
                        "Increase max_bytes, skip this line, or search for other lines.",
                        details={"line": number, "maxBytes": max_bytes, "requiredBytes": used + size},
                    )
                next_line = number
                break
            lines.append(entry)
            used += size
        return {
            "schema": RESULT_SCHEMA,
            "instanceId": artifact.instance_id,
            "captureSessionId": artifact.session_id,
            "effect": EFFECT_READ,
            "data": {"path": str(artifact.path), "sha256": artifact.sha256,
                     "bytes": artifact.size, "startLine": start_line, "lines": lines,
                     "nextLine": next_line, "search": search},
            "artifacts": [], "warnings": [], "truncated": next_line is not None,
            "nextCursor": None, "meta": {"captureSessionId": artifact.session_id},
        }
