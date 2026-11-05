import hashlib
import os
import re

from .paths import artifacts_root

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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
