"""Artifact files written under ``<root>/artifacts/<instanceId>/<session>/<category>``."""

import hashlib
import os
import re
import struct
import uuid

from .paths import artifacts_root

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(value, label):
    value = str(value or "")
    if not _SAFE_COMPONENT.match(value):
        raise ValueError("Invalid artifact %s: %r" % (label, value))
    return value


def artifact_directory(root, instance_id, capture_session_id, category):
    path = os.path.join(
        artifacts_root(root),
        _safe_component(instance_id, "instance id"),
        _safe_component(capture_session_id, "capture session id"),
        _safe_component(category, "category"),
    )
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def describe_artifact(path, kind, media_type, source=None, extra=None):
    path = os.path.abspath(path)
    value = {
        "kind": kind,
        "path": path,
        "mediaType": media_type,
        "bytes": os.path.getsize(path),
        "sha256": file_sha256(path),
        "source": source or {},
    }
    if extra:
        value.update(extra)
    return value


def artifact_path(runtime, session, prefix, extension, category="replay"):
    directory = artifact_directory(runtime.root, runtime.instance_id, session or "nosession",
                                   category)
    name = _UNSAFE_NAME.sub("_", str(prefix)).strip("._") or "artifact"
    return os.path.join(directory, "%s-%s.%s" % (name[:96], uuid.uuid4().hex[:8], extension))


def describe(path, kind, media_type, source=None, **extra):
    return describe_artifact(path, kind, media_type, source=source, extra=extra)


def write_bytes(runtime, session, prefix, extension, data, kind, media_type, source=None,
                **extra):
    path = artifact_path(runtime, session, prefix, extension)
    with open(path, "wb") as stream:
        stream.write(data)
    return describe(path, kind, media_type, source=source, **extra)


def png_size(path):
    """Return ``(width, height)`` from a PNG header, or None if the file is not a PNG."""
    with open(path, "rb") as stream:
        header = stream.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", header[16:24])
