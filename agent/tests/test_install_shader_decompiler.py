from __future__ import annotations

import ast
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

import pytest

import install_shader_decompiler as installer


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def release(tmp_path, monkeypatch):
    fixed = copy.deepcopy(installer.RELEASE)
    fixed["notices"] = {}
    contents = {name: ("binary:" + name).encode() for name in fixed["files"]}
    for name, data in contents.items():
        fixed["files"][name].update(size=len(data), sha256=digest(data))
    monkeypatch.setattr(installer, "RELEASE", fixed)
    return fixed, contents


def archive_file(tmp_path, release, extra=(), content_changes=None):
    fixed, contents = release
    contents = dict(contents, **(content_changes or {}))
    path = tmp_path / "release.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in list(contents.items()) + list(extra):
            archive.writestr(name, data)
    raw = path.read_bytes()
    fixed.update(size=len(raw), sha256=digest(raw))
    return path


def install_fixture(tmp_path, release):
    archive = archive_file(tmp_path, release)
    output = tmp_path / "tools" / "shader_decompiler"
    response = installer.install(output, archive, tmp_path / "cache")
    return output, archive, response


def test_installs_only_component_allowlist_and_verifies_repeat_without_download(tmp_path, release,
                                                                             monkeypatch):
    archive = archive_file(tmp_path, release, extra=[("HLSLDecompiler.exe", b"wrapper"),
                                                    ("HLSLDecompiler.bat", b"batch")])
    output = tmp_path / "tools"
    result = installer.install(output, archive, tmp_path / "cache")
    assert result["status"] == "installed"
    assert set(item.name for item in output.iterdir()) == set(release[0]["files"]) | {"manifest.json"}
    for name, content in release[1].items():
        assert (output / name).read_bytes() == content
    manifest = installer.verify_install(output)
    assert manifest["release"]["sha256"] == release[0]["sha256"]
    assert manifest["components"] == release[0]["components"]
    dates = {item.name: item.stat().st_mtime_ns for item in output.iterdir()}
    monkeypatch.setattr(installer, "_download", lambda *args: pytest.fail("unexpected download"))
    again = installer.install(output)
    assert again["status"] == "already-installed"
    assert {item.name: item.stat().st_mtime_ns for item in output.iterdir()} == dates


def test_archive_hash_mismatch_changes_no_existing_output(tmp_path, release):
    archive = archive_file(tmp_path, release)
    archive.write_bytes(archive.read_bytes()[:-1] + b"x")
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(installer.InstallError, match="SHA256"):
        installer.install(output, archive)
    assert list(output.iterdir()) == []
    assert not list(tmp_path.glob(".shader-decompiler-*"))


def test_file_hash_is_checked_independently_from_archive_hash(tmp_path, release):
    name = next(iter(release[1]))
    archive = archive_file(tmp_path, release, content_changes={name: b"x" * len(release[1][name])})
    with pytest.raises(installer.InstallError, match="SHA256"):
        installer.install(tmp_path / "output", archive)
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".shader-decompiler-*"))


@pytest.mark.parametrize("name", ["../escape.dll", "/absolute.dll", "C:escape.dll", "dir/file.dll",
                                  "dir\\file.dll", "dxil-spirv.exe.", "dxil-spirv.exe ",
                                  "unexpected.dll"])
def test_rejects_unsafe_and_unlisted_archive_names_before_extraction(tmp_path, release, name):
    archive = archive_file(tmp_path, release, extra=[(name, b"unsafe")])
    with pytest.raises(installer.InstallError, match="archive path|Unexpected archive"):
        installer.install(tmp_path / "output", archive)
    assert not (tmp_path / "escape.dll").exists()
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".shader-decompiler-*"))


@pytest.mark.parametrize("name", ["dxil-spirv.exe", "DXIL-SPIRV.EXE"])
def test_rejects_duplicates_including_case_collisions(tmp_path, release, name):
    with pytest.warns(UserWarning) if name == "dxil-spirv.exe" else _no_warning():
        archive = archive_file(tmp_path, release, extra=[(name, b"duplicate")])
    with pytest.raises(installer.InstallError, match="Duplicate"):
        installer.install(tmp_path / "output", archive)


class _no_warning:
    def __enter__(self):
        pass

    def __exit__(self, *args):
        pass


def test_rejects_archive_symlink_even_for_an_excluded_wrapper(tmp_path, release):
    link = zipfile.ZipInfo("HLSLDecompiler.exe")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = archive_file(tmp_path, release, extra=[(link, b"outside")])
    with pytest.raises(installer.InstallError, match="entry type"):
        installer.install(tmp_path / "output", archive)


def test_missing_component_and_member_size_mismatch_are_rejected(tmp_path, release):
    name = next(iter(release[1]))
    release[1].pop(name)
    archive = archive_file(tmp_path, release)
    with pytest.raises(installer.InstallError, match="missing required"):
        installer.install(tmp_path / "output", archive)
    release[1][name] = b"too-short"
    archive = archive_file(tmp_path, release)
    with pytest.raises(installer.InstallError, match="size mismatch"):
        installer.install(tmp_path / "output", archive)


def test_expanded_size_budget_is_checked_before_any_extraction(tmp_path, release, monkeypatch):
    archive = archive_file(tmp_path, release)
    monkeypatch.setattr(installer, "MAX_EXPANDED_BYTES", 1)
    with pytest.raises(installer.InstallError, match="expanded-size"):
        installer.install(tmp_path / "output", archive)
    assert not list(tmp_path.glob(".shader-decompiler-*"))


@pytest.mark.parametrize("change", ["binary", "extra", "manifest", "missing"])
def test_existing_installation_tampering_is_rejected_without_overwrite(tmp_path, release, change):
    output, archive, _ = install_fixture(tmp_path, release)
    name = next(iter(release[1]))
    if change == "binary":
        (output / name).write_bytes(b"x" * len(release[1][name]))
    elif change == "extra":
        (output / "unexpected.dll").write_bytes(b"x")
    elif change == "manifest":
        path = output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["release"]["version"] = "tampered"
        path.write_text(json.dumps(manifest))
    else:
        (output / name).unlink()
    before = {item.name: item.read_bytes() for item in output.iterdir()}
    with pytest.raises(installer.InstallError):
        installer.install(output, archive)
    assert {item.name: item.read_bytes() for item in output.iterdir()} == before


def test_non_install_directory_is_preserved(tmp_path, release):
    archive = archive_file(tmp_path, release)
    output = tmp_path / "output"
    output.mkdir()
    (output / "important.txt").write_text("keep")
    with pytest.raises(installer.InstallError, match="manifest"):
        installer.install(output, archive)
    assert (output / "important.txt").read_text() == "keep"


def test_notice_install_hashes_and_optional_network_failure(tmp_path, release, monkeypatch, capsys):
    release[0]["notices"] = {
        "LICENSE": {"url": "https://example.test/LICENSE", "size": 7,
                    "sha256": digest(b"license"), "component": "test"},
        "NOTICE": {"url": "https://example.test/NOTICE", "size": 6,
                   "sha256": digest(b"notice"), "component": "test"},
    }
    cache = tmp_path / "cache" / "notices"
    cache.mkdir(parents=True)
    (cache / "LICENSE").write_bytes(b"license")
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(
        OSError("offline")))
    archive = archive_file(tmp_path, release)
    output = tmp_path / "output"
    result = installer.install(output, archive, cache.parent)
    assert result["status"] == "installed"
    assert result["manifest"]["notices"]["LICENSE"]["installed"]
    assert not result["manifest"]["notices"]["NOTICE"]["installed"]
    assert "NOTICE unavailable" in result["warnings"][0]
    assert (output / "licenses" / "LICENSE").read_bytes() == b"license"
    installer.verify_install(output)
    installed_warnings = installer.install(output, archive, cache.parent)["warnings"]
    assert installed_warnings
    assert installer.main(["--output", str(output), "--verify-only"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "verified" and verified["warnings"] == installed_warnings
    (output / "licenses" / "LICENSE").write_bytes(b"changed")
    with pytest.raises(installer.InstallError, match="SHA256"):
        installer.verify_install(output)


class Response(io.BytesIO):
    def __init__(self, data, length=None, url="https://example.test/release.zip"):
        super().__init__(data)
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.url = url

    def geturl(self):
        return self.url


def test_download_is_bounded_pinned_and_cache_is_reverified(tmp_path, monkeypatch):
    expected = {"url": "https://example.test/release.zip", "size": 4, "sha256": digest(b"data")}
    path = tmp_path / "cache" / "release.zip"
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: Response(b"data", 4))
    assert installer._download(expected, path, 4) == path
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
    installer._download(expected, path, 4)
    path.write_bytes(b"fake")
    with pytest.raises(installer.InstallError, match="SHA256"):
        installer._download(expected, path, 4)


@pytest.mark.parametrize("body,length,url", [
    (b"dataextra", None, "https://example.test/release.zip"),
    (b"fake", 4, "https://example.test/release.zip"),
    (b"d", None, "https://example.test/release.zip"),
    (b"data", 1000, "https://example.test/release.zip"),
    (b"data", 4, "http://example.test/release.zip"),
])
def test_invalid_downloads_are_not_cached_and_partial_files_are_removed(tmp_path, monkeypatch,
                                                                       body, length, url):
    expected = {"url": "https://example.test/release.zip", "size": 4, "sha256": digest(b"data")}
    monkeypatch.setattr(installer.urllib.request, "urlopen",
                        lambda *a, **k: Response(body, length, url))
    with pytest.raises(installer.InstallError):
        installer._download(expected, tmp_path / "release.zip", 4)
    assert list(tmp_path.iterdir()) == []


def test_reparse_point_attributes_are_detected():
    class Reparse:
        def lstat(self):
            class Info:
                st_mode = stat.S_IFDIR
                st_file_attributes = 0x400
            return Info()
    assert installer._is_link(Reparse())


def test_symlink_output_ancestor_is_rejected(tmp_path, release):
    archive = archive_file(tmp_path, release)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links requires Windows developer mode or privilege")
    with pytest.raises(installer.InstallError, match="reparse"):
        installer.install(link / "output", archive)
    assert list(real.iterdir()) == []


def test_cleanup_refuses_an_unowned_directory(tmp_path):
    other = tmp_path / "keep"
    other.mkdir()
    (other / "important").write_text("keep")
    with pytest.raises(installer.InstallError, match="Refusing to remove"):
        installer._cleanup_staging(other, tmp_path)
    assert (other / "important").read_text() == "keep"


def test_verify_only_cli_and_release_python36_compatibility(tmp_path, release, capsys):
    output, _, _ = install_fixture(tmp_path, release)
    assert installer.main(["--output", str(output), "--verify-only"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "verified" and response["warnings"] == []
    path = installer.AGENT_ROOT / "bridge" / "rendertest_agent_bridge" / "decompiler_release.py"
    ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 6))
