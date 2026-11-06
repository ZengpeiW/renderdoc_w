"""Install the pinned upstream Windows release; never build or execute downloaded programs.

Python 3.10+; standard library only. Run with --verify-only to check an existing installation.
--archive accepts a previously downloaded copy, subject to the same fixed size/hash checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile


AGENT_ROOT = Path(__file__).resolve().parents[1]
RELEASE = runpy.run_path(str(AGENT_ROOT / "bridge" / "rendertest_agent_bridge" /
                            "decompiler_release.py"))["RELEASE"]
DEFAULT_OUTPUT = (AGENT_ROOT.parent / "x64" / "Release" / "integrations" /
                  "rendertest_agent" / "tools" / "shader_decompiler")
DEFAULT_CACHE = AGENT_ROOT / ".agent-data" / "shader-decompiler-downloads"
MANIFEST = "manifest.json"
IGNORED_ARCHIVE_FILES = {"HLSLDecompiler.exe", "HLSLDecompiler.bat"}
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_NOTICE_BYTES = 512 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
CHUNK_BYTES = 128 * 1024


class InstallError(RuntimeError):
    pass


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return (stat.S_ISLNK(info.st_mode) or
            bool(getattr(info, "st_file_attributes", 0) & 0x400))  # Windows reparse points.


def _safe_path(path: Path) -> Path:
    """Reject existing symlinks/junctions before resolving or changing any directory."""
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if os.path.lexists(item) and _is_link(item):
            raise InstallError("Symlinks and reparse points are not allowed: %s" % item)
    return path


def _verify_file(path: Path, expected: dict, limit: int) -> None:
    path = _safe_path(path)
    if not path.is_file():
        raise InstallError("Missing regular file: %s" % path)
    size = path.stat().st_size
    if size > limit or size != expected["size"]:
        raise InstallError("Size mismatch for %s (expected %d, got %d)." %
                           (path.name, expected["size"], size))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        count = 0
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            count += len(chunk)
            if count > expected["size"]:
                raise InstallError("File grew during verification: %s" % path.name)
            digest.update(chunk)
    if count != size or digest.hexdigest() != expected["sha256"]:
        raise InstallError("SHA256 mismatch for %s." % path.name)


def _download(expected: dict, destination: Path, limit: int) -> Path:
    destination = _safe_path(destination)
    if destination.exists():
        _verify_file(destination, expected, limit)
        return destination
    if not 0 < expected["size"] <= limit or not expected["url"].startswith("https://"):
        raise InstallError("Invalid pinned download specification.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
    temporary = Path(name)
    try:
        request = urllib.request.Request(expected["url"], headers={
            "User-Agent": "RenderTest-shader-decompiler-installer", "Accept-Encoding": "identity"})
        with os.fdopen(descriptor, "wb") as target:
            with urllib.request.urlopen(request, timeout=30) as source:
                if not source.geturl().startswith("https://"):
                    raise InstallError("Refusing a non-HTTPS download redirect.")
                length = source.headers.get("Content-Length")
                if length is not None and int(length) != expected["size"]:
                    raise InstallError("Download Content-Length does not match the pinned release.")
                count = 0
                digest = hashlib.sha256()
                while True:
                    chunk = source.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > min(limit, expected["size"]):
                        raise InstallError("Download exceeds its pinned size limit.")
                    digest.update(chunk)
                    target.write(chunk)
                if count != expected["size"] or digest.hexdigest() != expected["sha256"]:
                    raise InstallError("Downloaded size/SHA256 does not match the pinned release.")
        _safe_path(destination)
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def _archive_entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > 64 or sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
        raise InstallError("Archive exceeds entry count or expanded-size limits.")
    selected = {}
    seen = set()
    allowed = set(RELEASE["files"]) | IGNORED_ARCHIVE_FILES
    for item in entries:
        name = item.filename
        # This release is flat. Validate every entry, including excluded wrappers, before writes.
        if (not name or item.orig_filename != name or name in (".", "..") or
                any(char in name for char in ("/", "\\", ":", "\x00")) or name.endswith((".", " "))):
            raise InstallError("Unsafe archive path: %r" % name)
        folded = name.casefold()
        if folded in seen:
            raise InstallError("Duplicate archive entry: %s" % name)
        seen.add(folded)
        mode = stat.S_IFMT(item.external_attr >> 16)
        if (item.is_dir() or mode not in (0, stat.S_IFREG) or item.flag_bits & 1 or
                item.file_size > MAX_FILE_BYTES):
            raise InstallError("Unsupported archive entry type, encryption or size: %s" % name)
        if name not in allowed:
            raise InstallError("Unexpected archive member: %s" % name)
        if name in RELEASE["files"]:
            if item.file_size != RELEASE["files"][name]["size"]:
                raise InstallError("Archive member size mismatch: %s" % name)
            selected[name] = item
    if set(selected) != set(RELEASE["files"]):
        raise InstallError("Archive is missing required component files.")
    return selected


def _extract(archive_path: Path, target: Path) -> None:
    _verify_file(archive_path, RELEASE, MAX_ARCHIVE_BYTES)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            selected = _archive_entries(archive)
            for name, item in selected.items():
                expected = RELEASE["files"][name]
                with archive.open(item) as source, (target / name).open("xb") as output:
                    count = 0
                    while True:
                        chunk = source.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > expected["size"]:
                            raise InstallError("Expanded file exceeds its pinned size: %s" % name)
                        output.write(chunk)
                _verify_file(target / name, expected, MAX_FILE_BYTES)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
        if isinstance(error, InstallError):
            raise
        raise InstallError("Could not read release ZIP: %s" % error) from error


def _manifest(notices: dict) -> dict:
    return {"schemaVersion": 1,
            "release": {key: RELEASE[key] for key in ("version", "url", "sha256", "size")},
            "files": RELEASE["files"], "components": RELEASE["components"], "notices": notices}


def verify_install(output: Path) -> dict:
    output = _safe_path(output)
    manifest_path = _safe_path(output / MANIFEST)
    if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise InstallError("Installation manifest is missing or oversized: %s" % manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise InstallError("Invalid installation manifest.") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("notices"), dict):
        raise InstallError("Invalid installation manifest structure.")
    if manifest != _manifest(manifest["notices"]):
        raise InstallError("Installation manifest does not describe the pinned release.")
    if set(manifest["notices"]) != set(RELEASE.get("notices", {})):
        raise InstallError("Installation notice inventory does not match the pinned release.")
    expected_names = set(RELEASE["files"]) | {MANIFEST}
    if RELEASE.get("notices"):
        expected_names.add("licenses")
    if {entry.name for entry in output.iterdir()} != expected_names:
        raise InstallError("Installation contains missing or unexpected files/directories.")
    for name, expected in RELEASE["files"].items():
        _verify_file(output / name, expected, MAX_FILE_BYTES)
    included = set()
    for name, expected in RELEASE.get("notices", {}).items():
        recorded = manifest["notices"][name]
        if (not isinstance(recorded, dict) or type(recorded.get("installed")) is not bool or
                recorded != dict(expected, installed=recorded.get("installed"))):
            raise InstallError("Invalid notice metadata: %s" % name)
        if recorded["installed"]:
            included.add(name)
            _verify_file(output / "licenses" / name, expected, MAX_NOTICE_BYTES)
    if RELEASE.get("notices"):
        directory = _safe_path(output / "licenses")
        if not directory.is_dir() or {entry.name for entry in directory.iterdir()} != included:
            raise InstallError("Installed notice files do not match their manifest.")
    return manifest


def _cleanup_staging(staging: Path, parent: Path) -> None:
    if not staging.exists():
        return
    # Only recursively remove the exact staging directory we created within the chosen output parent.
    checked = _safe_path(staging)
    if checked.parent.resolve() != parent.resolve() or not checked.name.startswith(".shader-decompiler-"):
        raise InstallError("Refusing to remove a staging directory outside the output parent.")
    shutil.rmtree(checked)


def _notice_warnings(manifest: dict) -> list[str]:
    if any(not item["installed"] for item in manifest["notices"].values()):
        return ["Some upstream license notices were unavailable at installation."]
    return []


def install(output: Path, archive: Path | None = None, cache: Path = DEFAULT_CACHE) -> dict:
    output, cache = _safe_path(output), _safe_path(cache)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        manifest = verify_install(output)
        return {"status": "already-installed", "output": str(output), "manifest": manifest,
                "warnings": _notice_warnings(manifest)}
    if archive is None:
        archive = _download(RELEASE, cache / "HLSLDecompiler-v0.6.zip", MAX_ARCHIVE_BYTES)
    else:
        archive = _safe_path(archive)
        _verify_file(archive, RELEASE, MAX_ARCHIVE_BYTES)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".shader-decompiler-", dir=output.parent))
    warnings = []
    try:
        _extract(archive, staging)
        notices = {}
        if RELEASE.get("notices"):
            (staging / "licenses").mkdir()
        for name, expected in RELEASE.get("notices", {}).items():
            included = False
            try:
                source = _download(expected, cache / "notices" / name, MAX_NOTICE_BYTES)
                shutil.copyfile(source, staging / "licenses" / name)
                _verify_file(staging / "licenses" / name, expected, MAX_NOTICE_BYTES)
                included = True
            except (InstallError, OSError, ValueError, urllib.error.URLError) as error:
                (staging / "licenses" / name).unlink(missing_ok=True)
                warnings.append("Notice %s unavailable: %s" % (name, error))
            notices[name] = dict(expected, installed=included)
        (staging / MANIFEST).write_text(json.dumps(_manifest(notices), indent=2, sort_keys=True) +
                                        "\n", encoding="utf-8")
        manifest = verify_install(staging)
        # Never replace an existing installation or an unrelated nonempty directory.
        _safe_path(output)
        if output.exists():
            output.rmdir()  # Only succeeds for the initially empty directory.
        if staging.parent.resolve() != output.parent.resolve():
            raise InstallError("Staging and installation paths have different parents.")
        staging.rename(output)
        return {"status": "installed", "output": str(output), "manifest": manifest,
                "warnings": warnings}
    finally:
        _cleanup_staging(staging, output.parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--archive", type=Path, help="Use a local copy of the exact pinned ZIP.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--verify-only", action="store_true", help="Verify installed hashes; no downloads.")
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            manifest = verify_install(args.output)
            response = {"status": "verified", "output": str(_safe_path(args.output)),
                        "manifest": manifest, "warnings": _notice_warnings(manifest)}
        else:
            response = install(args.output, args.archive, args.cache_dir)
        print(json.dumps(response, indent=2))
        return 0
    except (InstallError, OSError, ValueError, urllib.error.URLError) as error:
        print("Shader decompiler installation failed: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
