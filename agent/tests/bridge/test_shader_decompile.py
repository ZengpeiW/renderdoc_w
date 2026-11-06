from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import struct
import sys
from types import SimpleNamespace as NS

import pytest

import fake_renderdoc
from fake_renderdoc import FakeContext, ShaderStage, module
from rendertest_agent_bridge.indexes import SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError
from rendertest_agent_bridge.lifecycle import CaptureState
from rendertest_agent_bridge.methods import shader_decompile as decompile
from rendertest_agent_bridge import shader_decompiler
from rendertest_agent_bridge.replay import ReplayRunner


def container(chunk=b"DXIL", body=b"program"):
    payload = chunk + struct.pack("<I", len(body)) + body
    return b"DXBC" + b"\x00" * 16 + struct.pack("<IIII", 1, 36 + len(payload), 1, 36) + payload


def spirv():
    return struct.pack("<IIIII", 0x07230203, 0x00010000, 0, 1, 0)


class Context:
    method = "decompile_shader"
    deadline_at = float("inf")

    def __init__(self, state, params):
        self.state = state.snapshot()
        self.params = params
        self.cancelled = False

    def check_cancelled(self):
        if self.cancelled:
            raise AgentBridgeError("CANCELLED", "test cancellation")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "renderdoc", module())
    state = CaptureState()
    state.capture_loaded("fixture.rdc", "D3D12", current=(9, None))
    ctx = FakeContext()
    replay = ReplayRunner(state, lambda message: None)
    replay.manager = ctx.manager
    runtime = NS(root=str(tmp_path), instance_id="test-decompile", state=state, replay=replay,
                 cache=SessionCache(), ctx=ctx, calls=[], output="float4 main() { return 1; }\n",
                 provenance={"name": "test-release", "version": "1.2", "sha256": "a" * 64},
                 warnings=[], actual_language=None)
    original = fake_renderdoc.reflection
    runtime.reflection = original(ShaderStage.Pixel)
    runtime.reflection.rawBytes = container()
    monkeypatch.setattr(fake_renderdoc, "reflection", lambda stage: runtime.reflection
                        if stage == ShaderStage.Pixel else original(stage))

    def convert(raw, encoding, stage, entry, language, context):
        # External execution must happen only after replay state was restored.
        assert ctx.controller.current == 9
        runtime.calls.append((raw, encoding, stage, entry, language))
        provenance = dict(runtime.provenance)
        provenance["outputLanguage"] = (runtime.actual_language if runtime.actual_language is not None
                                        else "hlsl" if language == "auto" else language)
        runtime.last_provenance = provenance
        return runtime.output, provenance, runtime.warnings

    monkeypatch.setattr(shader_decompiler, "convert", convert)
    return runtime


def call(runtime, **params):
    values = dict(stage="pixel", event_id=5)
    values.update(params)
    return decompile.decompile_shader(runtime, Context(runtime.state, values))


def assert_error(runtime, code, **params):
    with pytest.raises(AgentBridgeError) as error:
        call(runtime, **params)
    assert error.value.code == code
    assert runtime.ctx.controller.current == 9
    assert not list(Path(runtime.root).rglob("*.txt"))
    return error.value


@pytest.mark.parametrize("encoding,raw", [("DXIL", container()), ("DXBC", container(b"SHEX")),
                                           ("DXBC", container(b"SHDR")), ("SPIRV", spirv())])
@pytest.mark.parametrize("language", ["auto", "hlsl", "glsl"])
def test_original_bytecode_snapshot_generated_metadata_artifact_and_event_restore(runtime, encoding, raw,
                                                                                 language):
    runtime.reflection.encoding = NS(name=encoding)
    runtime.reflection.rawBytes = bytearray(raw)
    runtime.output = "\ufeff// \u751f\u6210\u4ee3\u7801\nfloat4 main() { return 1; }\r\n"
    response = call(runtime, language=language)
    assert runtime.calls == [(raw, encoding, "pixel", "main", language)]
    assert type(runtime.calls[0][0]) is bytes
    payload, data = response.result, response.result["data"]
    assert data["available"] and data["generated"] and not data["isOriginalSource"]
    assert data["eventId"] == 5 and data["stage"] == "pixel"
    assert data["shader"] == {"resourceId": "300", "name": "MainPS"}
    assert data["entryPoint"] == "main" and data["inputEncoding"] == encoding
    assert data["inputBytes"] == len(raw)
    assert data["inputSha256"] == hashlib.sha256(raw).hexdigest()
    actual_language = "hlsl" if language == "auto" else language
    assert data["requestedLanguage"] == language and data["outputLanguage"] == actual_language
    assert data["decompiler"] == runtime.last_provenance
    assert data["readArtifactCompatible"] is True
    assert len(payload["artifacts"]) == 1 and payload["nextCursor"] is None
    artifact = payload["artifacts"][0]
    path = Path(artifact["path"])
    encoded = runtime.output.encode("utf-8")
    assert path.suffix == ".txt" and path.read_bytes() == encoded
    assert artifact["bytes"] == data["outputBytes"] == len(encoded)
    assert artifact["sha256"] == hashlib.sha256(encoded).hexdigest()
    assert artifact["kind"] == "shader-decompiled"
    assert artifact["mediaType"] == "text/plain; charset=utf-8"
    assert artifact["source"]["generated"] and not artifact["source"]["isOriginalSource"]
    assert artifact["source"]["language"] == actual_language
    assert artifact["source"]["requestedLanguage"] == language
    assert data["decompiledArtifact"] == str(path)
    assert all(term in " ".join(payload["warnings"]) for term in
               ("not embedded or original", "cannot be recovered reliably", "bindings", "equivalence"))
    assert response.meta["replayEventRestored"]
    assert response.meta["eventIdBefore"] == response.meta["eventIdAfter"] == 9


@pytest.mark.parametrize("actual_language", ["hlsl", "glsl"])
def test_default_auto_records_actual_language_and_preserves_fallback_warning(runtime, actual_language):
    runtime.actual_language = actual_language
    runtime.warnings = (["HLSL conversion failed; auto generated GLSL instead."]
                        if actual_language == "glsl" else [])
    payload = call(runtime).result
    assert runtime.calls[0][-1] == "auto"
    assert payload["data"]["requestedLanguage"] == "auto"
    assert payload["data"]["outputLanguage"] == actual_language
    assert payload["data"]["decompiler"]["outputLanguage"] == actual_language
    source = payload["artifacts"][0]["source"]
    assert source["requestedLanguage"] == "auto" and source["language"] == actual_language
    assert all(warning in payload["warnings"] for warning in runtime.warnings)


@pytest.mark.parametrize("language,actual", [("hlsl", "glsl"), ("glsl", "hlsl"),
                                            ("auto", "auto"), ("auto", "HLSL"),
                                            ("auto", False), ("auto", "")])
def test_inconsistent_or_invalid_actual_language_does_not_publish(runtime, language, actual):
    runtime.actual_language = actual
    assert_error(runtime, "INTERNAL_ERROR", language=language)


@pytest.mark.parametrize("provenance", [{}, {"outputLanguage": None}])
def test_missing_actual_output_language_does_not_publish(runtime, monkeypatch, provenance):
    monkeypatch.setattr(shader_decompiler, "convert", lambda *args: (runtime.output, provenance, []))
    assert_error(runtime, "INTERNAL_ERROR")


@pytest.mark.parametrize("params", [{"stage": True}, {"stage": "no-stage"}, {"event_id": True},
                                    {"language": None}, {"language": "HLSL"}, {"language": True}])
def test_bad_parameters_do_not_start_converter(runtime, params):
    assert_error(runtime, "INVALID_REQUEST", **params)
    assert runtime.calls == []


@pytest.mark.parametrize("encoding,raw", [
    ("GLSL", b"void main() {}"), ("OpenGLSPIRV", spirv()), ("Unknown", container()),
    ("DXIL", b""), ("DXBC", b"DXBC"), ("DXIL", container(b"SHDR")),
    ("DXBC", container(b"DXIL")), ("DXBC", container(b"STAT")),
    ("SPIRV", b"\x03\x02\x23\x07"), ("SPIRV", spirv() + b"x"),
    ("SPIRV", b"wrong" * 4),
])
def test_invalid_or_unsupported_encoding_is_not_silently_disassembled(runtime, encoding, raw):
    runtime.reflection.encoding = NS(name=encoding)
    runtime.reflection.rawBytes = raw
    assert_error(runtime, "UNSUPPORTED")
    assert runtime.calls == []


@pytest.mark.parametrize("field,value", [(24, 0), (28, 0), (28, 4097), (32, 0), (32, 10000), (40, 10000)])
def test_malformed_dxbc_chunk_directory_rejected(runtime, field, value):
    raw = bytearray(container())
    struct.pack_into("<I", raw, field, value)
    runtime.reflection.rawBytes = raw
    assert_error(runtime, "UNSUPPORTED")
    assert runtime.calls == []


def test_missing_bytecode_and_entry_metadata(runtime, monkeypatch):
    del runtime.reflection.rawBytes
    assert_error(runtime, "UNSUPPORTED")
    runtime.reflection.encoding = NS(name="SPIRV")
    runtime.reflection.rawBytes = spirv()
    monkeypatch.setattr(fake_renderdoc.FakePipe, "GetShaderEntryPoint", lambda *args: "")
    assert_error(runtime, "UNSUPPORTED")
    monkeypatch.setattr(fake_renderdoc.FakePipe, "GetShaderEntryPoint", lambda *args: "main\x00other")
    assert_error(runtime, "UNSUPPORTED")
    assert runtime.calls == []


def test_input_and_multibyte_output_limits_count_bytes(runtime, monkeypatch):
    monkeypatch.setattr(decompile, "MAX_SHADER_BYTES", 100)
    runtime.reflection.rawBytes = container(body=b"x" * 101)
    assert_error(runtime, "LIMIT_EXCEEDED")
    assert runtime.calls == []
    runtime.reflection.rawBytes = container()
    runtime.output = "\u00e9" * 60
    assert_error(runtime, "LIMIT_EXCEEDED")
    runtime.output = "x" * 101
    assert_error(runtime, "LIMIT_EXCEEDED")


@pytest.mark.parametrize("output", ["", " \r\n", b"void main() {}", "a\x00b", "bad\ud800"])
def test_invalid_generated_text_is_not_published(runtime, output):
    runtime.output = output
    assert_error(runtime, "REPLAY_ERROR")


def test_long_line_is_preserved_with_explicit_read_artifact_limit(runtime):
    runtime.output = "x" * (_source_limit := decompile._source.MAX_READ_ARTIFACT_PAGE_BYTES + 1) + "\n"
    payload = call(runtime).result
    data = payload["data"]
    assert data["readArtifactCompatible"] is False and data["readArtifactLine"] == 1
    assert data["outputBytes"] == _source_limit + 1
    assert "Generated shader text" in data["readArtifactReason"]
    assert "Embedded source" not in data["readArtifactReason"]
    assert Path(data["decompiledArtifact"]).read_bytes() == runtime.output.encode()


def test_capture_change_before_converter_cannot_start_process(runtime, monkeypatch):
    original = runtime.replay.at_event

    def changed(*args):
        value = original(*args)
        runtime.state.capture_loaded("other.rdc", "D3D12", current=(9, None))
        return value

    monkeypatch.setattr(runtime.replay, "at_event", changed)
    assert_error(runtime, "CAPTURE_CHANGED")
    assert runtime.calls == []


@pytest.mark.parametrize("outcome", ["capture", "cancel", "unsupported", "timeout"])
def test_capture_change_cancellation_and_converter_errors_do_not_publish(runtime, monkeypatch, outcome):
    def interrupted(raw, encoding, stage, entry, language, context):
        assert runtime.ctx.controller.current == 9
        if outcome == "capture":
            runtime.state.capture_loaded("other.rdc", "D3D12", current=(9, None))
        elif outcome == "cancel":
            context.cancelled = True
        else:
            raise AgentBridgeError(outcome.upper(), "test converter failure")
        return runtime.output, {}, []

    monkeypatch.setattr(shader_decompiler, "convert", interrupted)
    code = {"capture": "CAPTURE_CHANGED", "cancel": "CANCELLED"}.get(outcome, outcome.upper())
    assert_error(runtime, code)


def test_capture_change_during_artifact_write_removes_only_new_artifact(runtime, monkeypatch):
    original = decompile.write_bytes
    sentinel = Path(runtime.root) / "keep.bin"
    sentinel.write_bytes(b"keep")

    def changed(*args, **kwargs):
        artifact = original(*args, **kwargs)
        runtime.state.capture_loaded("other.rdc", "D3D12", current=(9, None))
        return artifact

    monkeypatch.setattr(decompile, "write_bytes", changed)
    assert_error(runtime, "CAPTURE_CHANGED")
    assert sentinel.read_bytes() == b"keep"


def test_provenance_and_warnings_bounded_and_json_serializable(runtime):
    runtime.provenance = {"name": object()}
    assert_error(runtime, "INTERNAL_ERROR")
    runtime.provenance = {"name": "x" * decompile.MAX_METADATA_BYTES}
    assert_error(runtime, "LIMIT_EXCEEDED")
    runtime.provenance = {}
    runtime.warnings = [object()]
    assert_error(runtime, "INTERNAL_ERROR")


def test_python36_syntax():
    ast.parse(Path(decompile.__file__).read_text(encoding="utf-8"), feature_version=(3, 6))
