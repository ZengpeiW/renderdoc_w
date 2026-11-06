from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fake_bridge import FakeRenderTestBridge
from rendertest_mcp import artifacts as artifact_module
from rendertest_mcp.artifacts import ArtifactReader
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.ipc_client import BridgeClient, BridgeResponse
from rendertest_mcp.protocol import LOCAL_TOOL_NAMES, METHOD_SPECS
from rendertest_mcp.server import create_server

INSTANCE = "test-instance"
SESSION = "a" * 32


def response(path: Path, data: bytes, instance: str = INSTANCE, session: str = SESSION,
             **metadata: Any) -> BridgeResponse:
    artifact = {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    artifact.update(metadata)
    return BridgeResponse("request-1", instance, {"data": {}, "artifacts": [artifact]},
                          {"captureSessionId": session})


def registered(tmp_path: Path, data: bytes = b"one\ntwo\nthree\n", *, suffix: str = ".txt",
               fixed: bool = False) -> tuple[ArtifactReader, Path]:
    env = {"RENDERTEST_AGENT_FIXED_INSTANCE_ID": INSTANCE,
           "RENDERTEST_AGENT_CAPTURE_SESSION_ID": SESSION} if fixed else {}
    reader = ArtifactReader(BridgeClient(root=tmp_path, environ=env))
    path = tmp_path / "artifacts" / INSTANCE / SESSION / "replay" / ("test" + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    reader.register(response(path, data))
    return reader, path


def assert_error(code: str, function: Any, *args: Any, **kwargs: Any) -> None:
    with pytest.raises(RenderTestAgentError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def test_line_pages_have_no_gaps_or_duplicates(tmp_path: Path) -> None:
    reader, path = registered(tmp_path, "α\r\n二\nlast".encode())
    first = reader.read(str(path), limit=2)
    assert first["data"]["lines"] == [{"line": 1, "text": "α"}, {"line": 2, "text": "二"}]
    assert first["truncated"] is True
    last = reader.read(str(path), start_line=first["data"]["nextLine"])
    assert last["data"]["lines"] == [{"line": 3, "text": "last"}]
    assert last["data"]["nextLine"] is None and not last["truncated"]
    assert reader.read(str(path), start_line=10)["data"]["lines"] == []


def test_literal_search_keeps_original_line_numbers_and_paginates(tmp_path: Path) -> None:
    reader, path = registered(tmp_path, b"a.*\nalpha\na.*\nA.*\na.*\n")
    first = reader.read(str(path), search="a.*", limit=2)
    assert [line["line"] for line in first["data"]["lines"]] == [1, 3]
    assert first["data"]["nextLine"] == 5
    second = reader.read(str(path), search="a.*", start_line=5)
    assert second["data"]["lines"] == [{"line": 5, "text": "a.*"}]
    assert second["data"]["nextLine"] is None


def test_json_escaped_byte_budget_and_long_nonmatching_lines(tmp_path: Path) -> None:
    reader, path = registered(tmp_path, b'x' * 300000 + b'\nneedle "quoted"\n' + b'y' * 300000)
    assert_error("LIMIT_EXCEEDED", reader.read, str(path), max_bytes=256)
    found = reader.read(str(path), search="needle", max_bytes=256)
    assert found["data"]["lines"] == [{"line": 2, "text": 'needle "quoted"'}]
    assert found["data"]["nextLine"] is None
    reader, path = registered(tmp_path, (b'"' * 70 + b'\n') * 3)
    page = reader.read(str(path), max_bytes=256)
    assert len(json.dumps(page["data"]["lines"], ensure_ascii=False).encode()) <= 256
    assert page["data"]["nextLine"] == 2


def test_empty_file_is_valid(tmp_path: Path) -> None:
    reader, path = registered(tmp_path, b"")
    assert reader.read(str(path))["data"]["lines"] == []


@pytest.mark.parametrize("suffix", [".txt", ".json", ".jsonl", ".csv", ".log"])
def test_text_suffixes(tmp_path: Path, suffix: str) -> None:
    reader, path = registered(tmp_path, b"{}\n", suffix=suffix)
    assert reader.read(str(path))["data"]["lines"][0]["text"] == "{}"


@pytest.mark.parametrize("data", [b"nul\x00byte", b"\xff", b"ok\n\xff"])
def test_rejects_binary_and_invalid_utf8_even_after_first_page(tmp_path: Path, data: bytes) -> None:
    reader, path = registered(tmp_path, data)
    assert_error("UNSUPPORTED", reader.read, str(path), limit=1)


@pytest.mark.parametrize("data", [b"ONE\ntwo\nthree\n", b"short"])
def test_rejects_modified_content_and_size(tmp_path: Path, data: bytes) -> None:
    reader, path = registered(tmp_path)
    path.write_bytes(data)
    assert_error("INVALID_RESPONSE", reader.read, str(path))


def test_validly_formatted_wrong_digest_is_not_trusted(tmp_path: Path) -> None:
    reader, path = registered(tmp_path)
    reader.register(response(path, path.read_bytes(), sha256="0" * 64))
    assert_error("INVALID_RESPONSE", reader.read, str(path))


def test_unregistered_file_and_new_process_are_denied(tmp_path: Path) -> None:
    reader, path = registered(tmp_path)
    unknown = path.with_name("other.txt")
    unknown.write_text("secret")
    assert_error("INVALID_REQUEST", reader.read, str(unknown))
    fresh = ArtifactReader(BridgeClient(root=tmp_path, environ={}))
    assert_error("INVALID_REQUEST", fresh.read, str(path))


def test_outside_scope_paths_never_register(tmp_path: Path) -> None:
    reader, path = registered(tmp_path)
    candidates = [tmp_path / "outside.txt",
                  tmp_path / "artifacts" / "other-instance" / SESSION / "file.txt",
                  tmp_path / "artifacts" / INSTANCE / ("b" * 32) / "file.txt"]
    for candidate in candidates:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"outside")
        reader.register(response(candidate, b"outside"))
        assert_error("INVALID_REQUEST", reader.read, str(candidate))
    assert_error("INVALID_REQUEST", reader.read, str(path.parent / ".." / "replay" / path.name))
    assert_error("INVALID_REQUEST", reader.read, path.name)


def test_binary_suffix_and_size_limits_do_not_register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, path = registered(tmp_path, b"binary", suffix=".bin")
    assert_error("INVALID_REQUEST", reader.read, str(path))
    monkeypatch.setattr(artifact_module, "MAX_ARTIFACT_BYTES", 8)
    reader, path = registered(tmp_path, b"12345678")
    assert reader.read(str(path))["data"]["bytes"] == 8
    reader, path = registered(tmp_path, b"123456789")
    assert_error("INVALID_REQUEST", reader.read, str(path))


def test_registry_eviction_requires_regeneration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_module, "MAX_REGISTERED_ARTIFACTS", 1)
    reader, path = registered(tmp_path)
    newer = path.with_name("new.txt")
    newer.write_bytes(b"new")
    reader.register(response(newer, b"new"))
    assert_error("INVALID_REQUEST", reader.read, str(path))
    assert reader.read(str(newer))["data"]["bytes"] == 3


def test_fixed_scope_rejects_artifacts_from_other_instances_or_captures(tmp_path: Path) -> None:
    reader, path = registered(tmp_path, fixed=True)
    assert reader.read(str(path))["captureSessionId"] == SESSION
    for instance, session in (("other-instance", SESSION), (INSTANCE, "b" * 32)):
        other = tmp_path / "artifacts" / instance / session / "file.txt"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"other")
        reader.register(response(other, b"other", instance, session))
        assert_error("INVALID_REQUEST", reader.read, str(other))


def test_scope_is_revalidated_at_read_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, path = registered(tmp_path)
    def reject(instance: str, session: str) -> None:
        raise RenderTestAgentError("CAPTURE_MISMATCH", "test capture changed")
    monkeypatch.setattr(reader.bridge, "validate_artifact_scope", reject)
    assert_error("CAPTURE_MISMATCH", reader.read, str(path))


def test_symlink_replacement_is_denied(tmp_path: Path) -> None:
    reader, path = registered(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(path.read_bytes())  # Even matching content must not allow an outside path.
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links.")
    assert_error("INVALID_REQUEST", reader.read, str(path))


def test_directory_symlink_outside_scope_is_denied(tmp_path: Path) -> None:
    reader, path = registered(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = path.parent / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links.")
    target = linked / "file.txt"
    (outside / "file.txt").write_bytes(b"hidden")
    reader.register(response(target, b"hidden"))
    assert_error("INVALID_REQUEST", reader.read, str(target))


def test_windows_reparse_directory_is_rejected_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, path = registered(tmp_path)
    original = Path.lstat
    def reparse_info(candidate: Path) -> Any:
        if candidate == path.parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(candidate)
    monkeypatch.setattr(Path, "lstat", reparse_info)
    assert_error("INVALID_REQUEST", reader.read, str(path))


def test_deleted_artifact_is_a_structured_error(tmp_path: Path) -> None:
    reader, path = registered(tmp_path)
    path.unlink()
    assert_error("INVALID_REQUEST", reader.read, str(path))


@pytest.mark.parametrize("kwargs", [{"start_line": 0}, {"limit": 1001}, {"max_bytes": 255},
                                    {"max_bytes": 128 * 1024 + 1}, {"search": ""}])
def test_page_argument_limits(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    reader, path = registered(tmp_path)
    assert_error("INVALID_REQUEST", reader.read, str(path), **kwargs)


def test_local_tool_reads_real_bridge_artifact_without_mailbox_call(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    with FakeRenderTestBridge(tmp_path):
        client = BridgeClient(root=tmp_path, environ={}, poll_interval=0.005)
        server = create_server(client)
        result = asyncio.run(server.call_tool("debug_pixel", {"x": 1, "y": 2, "event_id": 30}))[1]
        def no_bridge(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("read_artifact must not create a bridge request")
        monkeypatch.setattr(client, "call", no_bridge)
        page = asyncio.run(server.call_tool("read_artifact", {
            "path": result["artifacts"][0]["path"], "search": "summary",
        }))[1]
        assert page["data"]["lines"] == [{"line": 2, "text": '{"record":"summary","steps":1}'}]
        assert page["effect"] == "read"
        assert not set(LOCAL_TOOL_NAMES) & set(METHOD_SPECS)


@pytest.mark.parametrize("name", ["pick_pixel", "pixel_history"])
def test_pixel_type_cast_reaches_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    client = BridgeClient(root=tmp_path, environ={})
    received = []
    def record(method: str, params: dict[str, Any], **kwargs: Any) -> BridgeResponse:
        received.append((method, params))
        return BridgeResponse("id", INSTANCE, {"data": {}}, {"captureSessionId": SESSION})
    monkeypatch.setattr(client, "call", record)
    server = create_server(client)
    asyncio.run(server.call_tool(name, {"resource_id": "1001", "x": 1, "y": 2, "type_cast": "UInt"}))
    assert received[0][0] == name and received[0][1]["type_cast"] == "UInt"
