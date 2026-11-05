"""Artifact files written under ``<root>/artifacts/<instanceId>/<session>/<category>``."""

import hashlib
import os
import re
import struct
import uuid
import zlib

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


def _png_chunk(kind, body):
    return (struct.pack(">I", len(body)) + kind + body +
            struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff))


def bmp_to_png(bmp_path, png_path):
    """Convert an uncompressed 24/32-bit BMP written by RenderDoc into an opaque RGB PNG."""
    with open(bmp_path, "rb") as stream:
        data = stream.read()
    if data[:2] != b"BM" or len(data) < 54:
        raise ValueError("Not a BMP file: %s" % bmp_path)
    pixel_offset = struct.unpack("<I", data[10:14])[0]
    width, height = struct.unpack("<ii", data[18:26])
    bits, compression = struct.unpack("<H", data[28:30])[0], struct.unpack("<I", data[30:34])[0]
    if bits not in (24, 32) or compression not in (0, 3) or width <= 0 or height == 0:
        raise ValueError("Unsupported BMP layout: %d bpp, compression %d" % (bits, compression))
    bottom_up = height > 0
    height = abs(height)
    channels = bits // 8
    stride = (width * channels + 3) & ~3
    rows = []
    for row in range(height):
        source = height - 1 - row if bottom_up else row
        start = pixel_offset + source * stride
        line = bytearray(data[start:start + width * channels])
        rgb = bytearray(width * 3)
        rgb[0::3] = line[2::channels]
        rgb[1::3] = line[1::channels]
        rgb[2::3] = line[0::channels]
        rows.append(b"\x00" + bytes(rgb))
    png = (b"\x89PNG\r\n\x1a\n" +
           _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
           _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) +
           _png_chunk(b"IEND", b""))
    with open(png_path, "wb") as stream:
        stream.write(png)
    return width, height
