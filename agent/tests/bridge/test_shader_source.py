from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

import fake_renderdoc
from fake_renderdoc import FakeContext, ShaderStage, module
from rendertest_agent_bridge.indexes import SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError
from rendertest_agent_bridge.lifecycle import CaptureState
from rendertest_agent_bridge.methods import shader_source
from rendertest_agent_bridge.replay import ReplayRunner
from rendertest_mcp.artifacts import ArtifactReader, MAX_PAGE_BYTES
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.ipc_client import BridgeClient, BridgeResponse


class Context:
    method = "get_shader_source"
    deadline_at = float("inf")

    def __init__(self, state, params):
        self.state = state.snapshot()
        self.params = params

    def check_cancelled(self):
        pass


@pytest.fixture
def source_runtime(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "renderdoc", module())
    state = CaptureState()
    state.capture_loaded("fixture.rdc", "D3D12", current=(9, None))
    ctx = FakeContext()
    replay = ReplayRunner(state, lambda message: None)
    replay.manager = ctx.manager
    runtime = NS(root=str(tmp_path), instance_id="test-source", state=state, replay=replay,
                 cache=SessionCache(), ctx=ctx)
    original = fake_renderdoc.reflection
    runtime.reflection = original(ShaderStage.Pixel)

    def reflection(stage):
        return runtime.reflection if stage == ShaderStage.Pixel else original(stage)

    monkeypatch.setattr(fake_renderdoc, "reflection", reflection)
    return runtime


def call(runtime, **params):
    values = dict(stage="pixel", event_id=5)
    values.update(params)
    return shader_source.get_shader_source(runtime, Context(runtime.state, values))


def sources(runtime, *files):
    runtime.reflection.debugInfo.files = list(files)


def source_error(runtime, code, **params):
    with pytest.raises(AgentBridgeError) as error:
        call(runtime, **params)
    assert error.value.code == code
    assert runtime.ctx.controller.current == 9


def test_source_directory_paginates_beyond_64_and_restores_event(source_runtime):
    runtime = source_runtime
    sources(runtime, *(NS(filename="same.hlsl", contents="source%d" % index)
                       for index in range(205)))
    indices = []
    cursor = None
    while True:
        response = call(runtime, cursor=cursor)
        payload = response.result
        data = payload["data"]
        assert data["fileCount"] == 205 and data["available"] is True
        assert data["shader"] == {"resourceId": "300", "name": "MainPS"}
        assert payload["artifacts"] == []
        assert response.meta["replayEventRestored"] is True
        assert len(data["files"]) <= 100
        indices.extend(item["index"] for item in data["files"])
        cursor = payload["nextCursor"]
        if cursor is None:
            break
    assert indices == list(range(205))
    assert runtime.ctx.controller.frame_events == [5, 9, 5, 9, 5, 9]
    assert not (Path(runtime.root) / "artifacts").exists()


@pytest.mark.parametrize("text", ["", "\ufeff// 中文\nfloat4 main() { return 1; }\n", "x\r\ny"])
def test_export_preserves_utf8_bom_empty_and_newlines(source_runtime, text):
    runtime = source_runtime
    sources(runtime, NS(filename="../../not-a-local-file.hlsl", contents=text))
    listed = call(runtime).result["data"]["files"][0]
    expected = text.encode("utf-8")
    assert listed["bytes"] == len(expected) and listed["available"] is True
    assert listed["readArtifactCompatible"] is True
    response = call(runtime, file_index=0)
    payload = response.result
    artifact = payload["artifacts"][0]
    path = Path(artifact["path"])
    assert path.read_bytes() == expected
    assert artifact["bytes"] == len(expected)
    assert artifact["sha256"] == hashlib.sha256(expected).hexdigest()
    assert artifact["source"] == {"event": 5, "stage": "pixel", "shaderResourceId": "300",
                                  "fileIndex": 0, "filename": "../../not-a-local-file.hlsl"}
    assert path.parent == Path(runtime.root) / "artifacts" / runtime.instance_id / \
        runtime.state.snapshot()["captureSessionId"] / "replay"
    assert payload["data"]["sourceArtifact"] == str(path)
    assert response.meta["eventIdBefore"] == response.meta["eventIdAfter"] == 9
    assert "contents" not in payload["data"] and payload["nextCursor"] is None
    assert payload["warnings"] == []


def test_duplicate_filenames_select_by_index_and_never_read_local_file(source_runtime, tmp_path):
    runtime = source_runtime
    local = tmp_path / "secret.hlsl"
    local.write_text("LOCAL CONTENT MUST NOT BE RETURNED")
    sources(runtime, NS(filename=str(local), contents="embedded first"),
            NS(filename=str(local), contents="embedded second"))
    for index, expected in enumerate(("embedded first", "embedded second")):
        payload = call(runtime, file_index=index).result
        assert Path(payload["artifacts"][0]["path"]).read_text() == expected


@pytest.mark.parametrize("value", [None, NS(), NS(files=None), NS(files=[])])
def test_missing_debug_sources_are_explicitly_unavailable(source_runtime, value):
    runtime = source_runtime
    runtime.reflection.debugInfo = value
    payload = call(runtime).result
    assert payload["data"]["available"] is False
    assert payload["data"]["fileCount"] == 0 and payload["data"]["files"] == []
    assert "no embedded source" in payload["data"]["reason"]
    assert payload["artifacts"] == []
    source_error(runtime, "INVALID_REQUEST", file_index=0)


@pytest.mark.parametrize("index", [True, False, -1, 1, "0", 0.5])
def test_invalid_source_indices_are_rejected(source_runtime, index):
    source_error(source_runtime, "INVALID_REQUEST", file_index=index)


def test_directory_cursor_is_bound_to_session_event_stage_and_shader(source_runtime, monkeypatch):
    runtime = source_runtime
    sources(runtime, NS(filename="a", contents="a"), NS(filename="b", contents="b"))
    cursor = call(runtime, limit=1).result["nextCursor"]
    original_shaders = fake_renderdoc.FakePipe._shaders
    monkeypatch.setattr(fake_renderdoc.FakePipe, "_shaders",
                        lambda pipe: {ShaderStage.Pixel: 300} if pipe.controller.current == 3
                        else original_shaders(pipe))
    source_error(runtime, "INVALID_REQUEST", cursor=cursor, event_id=3)
    source_error(runtime, "INVALID_REQUEST", cursor=cursor, stage="vertex")
    source_error(runtime, "INVALID_REQUEST", cursor=cursor, file_index=0)
    original = fake_renderdoc.FakePipe.GetShader
    monkeypatch.setattr(fake_renderdoc.FakePipe, "GetShader",
                        lambda pipe, stage: fake_renderdoc.ResourceId(302) if stage == ShaderStage.Pixel
                        else original(pipe, stage))
    source_error(runtime, "INVALID_REQUEST", cursor=cursor)
    monkeypatch.setattr(fake_renderdoc.FakePipe, "GetShader", original)
    old_session = runtime.state.snapshot()["captureSessionId"]
    # Even two session ids sharing the legacy cursor's first 16 characters stay distinct.
    runtime.state._session_id = old_session[:16] + ("b" if old_session[16] != "b" else "a") * 16
    source_error(runtime, "INVALID_REQUEST", cursor=cursor)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": True}, {"limit": 1001},
                                   {"cursor": "bad"}, {"stage": "not-a-stage"}])
def test_directory_rejects_invalid_paging_and_stage(source_runtime, params):
    source_error(source_runtime, "INVALID_REQUEST", **params)


def test_size_limit_is_in_utf8_bytes_and_exact_boundary_exports(source_runtime, monkeypatch):
    runtime = source_runtime
    monkeypatch.setattr(shader_source, "MAX_SOURCE_BYTES", 6)
    sources(runtime, NS(filename="ok", contents="中文"), NS(filename="large", contents="中文x"))
    files = call(runtime).result["data"]["files"]
    assert [(item["bytes"], item["available"]) for item in files] == [(6, True), (7, False)]
    assert Path(call(runtime, file_index=0).result["artifacts"][0]["path"]).read_bytes() == "中文".encode()
    source_error(runtime, "LIMIT_EXCEEDED", file_index=1)
    assert len(list((Path(runtime.root) / "artifacts").rglob("*.txt"))) == 1


@pytest.mark.parametrize("contents", [None, 123, b"\xff", "\ud800"])
def test_missing_or_undecodable_contents_do_not_fabricate_source(source_runtime, contents):
    runtime = source_runtime
    sources(runtime, NS(filename="missing.hlsl", contents=contents))
    info = call(runtime).result["data"]["files"][0]
    assert info["available"] is False and info["bytes"] is None and info["reason"]
    payload = call(runtime, file_index=0).result
    assert payload["data"]["available"] is False and payload["artifacts"] == []


def test_swig_decode_failure_is_reported_without_guessing_encoding(source_runtime):
    class BadSource:
        filename = "legacy.hlsl"

        @property
        def contents(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    runtime = source_runtime
    sources(runtime, BadSource())
    assert call(runtime).result["data"]["files"][0]["available"] is False
    payload = call(runtime, file_index=0).result
    assert payload["artifacts"] == [] and "UTF-8" in payload["data"]["reason"]
    assert runtime.ctx.controller.current == 9


def test_missing_debug_info_attribute_is_unavailable(source_runtime):
    del source_runtime.reflection.debugInfo
    assert call(source_runtime).result["data"]["available"] is False


def test_capture_change_before_export_does_not_write_artifact(source_runtime, monkeypatch):
    runtime = source_runtime
    original = runtime.replay.at_event

    def replace_capture(*args):
        value = original(*args)
        runtime.state.capture_loaded("different.rdc", "D3D12", current=(9, None))
        return value

    monkeypatch.setattr(runtime.replay, "at_event", replace_capture)
    source_error(runtime, "CAPTURE_CHANGED", file_index=0)
    assert not (Path(runtime.root) / "artifacts").exists()


def test_directory_response_budget_keeps_all_indices_reachable(source_runtime):
    runtime = source_runtime
    sources(runtime, *(NS(filename="\u4e2d" * 5000, contents="") for _ in range(30)))
    cursor, indices = None, []
    while True:
        payload = call(runtime, limit=1000, cursor=cursor).result
        assert len(json.dumps(payload).encode()) <= shader_source.MAX_DIRECTORY_BYTES + 2048
        indices.extend(item["index"] for item in payload["data"]["files"])
        assert all(item["filenameTruncated"] for item in payload["data"]["files"])
        cursor = payload["nextCursor"]
        if cursor is None:
            break
    assert indices == list(range(30))


def test_default_event_and_missing_bound_shader(source_runtime):
    runtime = source_runtime
    runtime.state.current_event_changed(5, None)
    runtime.ctx.controller.current = 5
    response = call(runtime, event_id=None)
    assert response.result["data"]["eventId"] == 5
    assert response.meta["eventIdAfter"] == 5
    runtime.state.current_event_changed(9, None)
    runtime.ctx.controller.current = 9
    source_error(runtime, "INVALID_EVENT", event_id=8)


def test_source_module_keeps_python36_syntax():
    ast.parse(Path(shader_source.__file__).read_text(), feature_version=(3, 6))


@pytest.mark.parametrize("contents", ["abc\x00tail", b"abc\x00tail", "\x00", "ok\n" * 40000 + "\x00"])
def test_nul_source_is_unavailable_and_never_written(source_runtime, contents):
    runtime = source_runtime
    sources(runtime, NS(filename="binary.hlsl", contents=contents))
    info = call(runtime).result["data"]["files"][0]
    assert info["available"] is False and info["readArtifactCompatible"] is False
    assert "NUL" in info["reason"]
    payload = call(runtime, file_index=0).result
    assert payload["data"]["available"] is False and payload["artifacts"] == []
    assert "sourceArtifact" not in payload["data"]
    assert not (Path(runtime.root) / "artifacts").exists()


def source_reader(runtime, payload):
    reader = ArtifactReader(BridgeClient(root=Path(runtime.root), environ={}))
    reader.register(BridgeResponse("shader-source-test", runtime.instance_id, payload,
                                  {"captureSessionId": runtime.state.snapshot()["captureSessionId"]}))
    return reader, payload["artifacts"][0]["path"]


@pytest.mark.parametrize("ending", ["", "\n", "\r\n", "\r"])
@pytest.mark.parametrize("character", ["x", '"', "\t", "中"])
def test_reader_compatibility_matches_actual_json_line_budget(source_runtime, ending, character):
    runtime = source_runtime
    assert shader_source.MAX_READ_ARTIFACT_PAGE_BYTES == MAX_PAGE_BYTES
    overhead = len(json.dumps({"line": 10, "text": ""}, ensure_ascii=False).encode()) + 2
    per_character = len(json.dumps(character, ensure_ascii=False).encode()) - 2
    count, remainder = divmod(MAX_PAGE_BYTES - overhead, per_character)
    line = character * count + "x" * remainder
    exact_text = "\n" * 9 + line + ending
    sources(runtime, NS(filename="boundary.hlsl", contents=exact_text))
    payload = call(runtime, file_index=0).result
    info = payload["data"]["file"]
    assert info["available"] and info["readArtifactCompatible"] and payload["warnings"] == []
    reader, path = source_reader(runtime, payload)
    assert reader.read(path, start_line=10, max_bytes=MAX_PAGE_BYTES)["data"]["lines"] == [
        {"line": 10, "text": line}]
    assert Path(path).read_bytes() == exact_text.encode()

    too_long = "\n" * 9 + line + "x" + ending
    sources(runtime, NS(filename="long.hlsl", contents=too_long))
    listed = call(runtime).result["data"]["files"][0]
    assert listed["available"] is True and listed["readArtifactCompatible"] is False
    assert listed["readArtifactLine"] == 10
    assert listed["readArtifactRequiredBytes"] == MAX_PAGE_BYTES + 1
    assert listed["readArtifactMaxBytes"] == MAX_PAGE_BYTES
    payload = call(runtime, file_index=0).result
    assert payload["data"]["file"] == listed and payload["warnings"]
    reader, path = source_reader(runtime, payload)
    assert Path(path).read_bytes() == too_long.encode()
    with pytest.raises(RenderTestAgentError) as error:
        reader.read(path, start_line=10, max_bytes=MAX_PAGE_BYTES)
    assert error.value.code == "LIMIT_EXCEEDED"


def test_large_line_keeps_other_source_lines_readable_and_original_bytes(source_runtime):
    runtime = source_runtime
    original = "x" * (MAX_PAGE_BYTES + 1) + '\nfloat4 main() { return 1; }\n'
    sources(runtime, NS(filename="generated.hlsl", contents=original))
    payload = call(runtime, file_index=0).result
    info = payload["data"]["file"]
    assert info["available"] is True and info["readArtifactCompatible"] is False
    assert info["readArtifactLine"] == 1 and "read_artifact" in info["readArtifactReason"]
    reader, path = source_reader(runtime, payload)
    assert Path(path).read_bytes() == original.encode()
    assert reader.read(path, search="main")["data"]["lines"] == [
        {"line": 2, "text": "float4 main() { return 1; }"}]


def test_line_detection_preserves_non_lf_separators_and_cr_across_chunk_boundary(source_runtime, monkeypatch):
    runtime = source_runtime
    monkeypatch.setattr(shader_source, "ENCODE_CHUNK_CHARS", 3)
    text = 'ab\r\nc\vd\fe\r\rf\n\r\n'
    sources(runtime, NS(filename="controls.hlsl", contents=text))
    payload = call(runtime, file_index=0).result
    assert payload["data"]["file"]["readArtifactCompatible"] is True
    reader, path = source_reader(runtime, payload)
    assert reader.read(path)["data"]["lines"] == [
        {"line": 1, "text": "ab"}, {"line": 2, "text": "c\vd\fe\r\rf"},
        {"line": 3, "text": ""}]
