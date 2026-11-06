"""Export explicitly generated shader text from a bounded original-bytecode snapshot."""

import hashlib
import json
import os
import struct

from ..artifacts import write_bytes
from ..ipc import AgentBridgeError
from ..values import enum_name
from . import shader_source as _source
from .common import (STAGE_NAMES, action_index, get_choice, renderdoc_module, resolve_event,
                     resource_index, result, session_of)
from .pipeline import _bound_shader

MAX_SHADER_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
SUPPORTED_ENCODINGS = ("DXBC", "DXIL", "SPIRV")


def _unsupported(message):
    return AgentBridgeError("UNSUPPORTED", message)


def _session_guard(runtime, context, session):
    context.check_cancelled()
    current = runtime.state.snapshot()
    if not current.get("captureLoaded") or (current.get("captureSessionId") or "") != session:
        raise AgentBridgeError("CAPTURE_CHANGED", "The capture changed during shader decompilation.")


def _validate_bytecode(raw, encoding, entry_point, context):
    if encoding not in SUPPORTED_ENCODINGS:
        raise _unsupported("Shader encoding %s is not supported for decompilation." % encoding)
    if not raw:
        raise _unsupported("The bound shader has no original bytecode to decompile.")
    if encoding == "SPIRV":
        if (len(raw) < 20 or len(raw) % 4 or raw[:4] != b"\x03\x02\x23\x07"):
            raise _unsupported("SPIRV reflection does not contain a valid little-endian SPIR-V header.")
        if not entry_point:
            raise _unsupported("SPIR-V decompilation requires the bound shader entry point.")
        return
    # DXIL also uses a DXBC container. Its outer magic alone does not distinguish the formats.
    if len(raw) < 32 or raw[:4] != b"DXBC":
        raise _unsupported("%s reflection does not contain a DXBC shader container." % encoding)
    total, count = struct.unpack_from("<II", raw, 24)
    table_end = 32 + count * 4
    if total != len(raw) or count < 1 or count > 4096 or table_end > len(raw):
        raise _unsupported("The shader container size or chunk directory is invalid.")
    chunks, spans = set(), []
    for index in range(count):
        if index % 256 == 0:
            context.check_cancelled()
        offset = struct.unpack_from("<I", raw, 32 + index * 4)[0]
        if offset < table_end or offset + 8 > len(raw):
            raise _unsupported("A shader container chunk is outside its bytecode.")
        size = struct.unpack_from("<I", raw, offset + 4)[0]
        end = offset + 8 + size
        if end > len(raw):
            raise _unsupported("A shader container chunk is truncated.")
        chunks.add(raw[offset:offset + 4])
        spans.append((offset, end))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise _unsupported("The shader container contains overlapping chunks.")
    if encoding == "DXIL" and b"DXIL" not in chunks:
        raise _unsupported("DXIL reflection has no DXIL program chunk.")
    if encoding == "DXBC" and (b"DXIL" in chunks or not chunks.intersection((b"SHDR", b"SHEX"))):
        raise _unsupported("DXBC reflection has no matching legacy shader program chunk.")


def _encode_output(text, context):
    if not isinstance(text, str):
        raise AgentBridgeError("REPLAY_ERROR", "The decompiler returned no generated shader text.")
    if len(text) > MAX_SHADER_BYTES:
        raise AgentBridgeError("LIMIT_EXCEEDED", "Generated shader text exceeds 16 MiB.")
    if not text.strip():
        raise AgentBridgeError("REPLAY_ERROR", "The decompiler returned no generated shader text.")
    chunks, size = [], 0
    try:
        for offset in range(0, len(text), _source.ENCODE_CHUNK_CHARS):
            context.check_cancelled()
            chunk = text[offset:offset + _source.ENCODE_CHUNK_CHARS]
            if "\x00" in chunk:
                raise AgentBridgeError("REPLAY_ERROR", "The decompiler output contains NUL bytes.")
            encoded = chunk.encode("utf-8")
            size += len(encoded)
            if size > MAX_SHADER_BYTES:
                raise AgentBridgeError("LIMIT_EXCEEDED", "Generated UTF-8 shader text exceeds 16 MiB.",
                                       details={"maxBytes": MAX_SHADER_BYTES})
            chunks.append(encoded)
    except UnicodeError:
        raise AgentBridgeError("REPLAY_ERROR", "The decompiler output is not valid UTF-8 text.")
    return b"".join(chunks)


def decompile_shader(runtime, context):
    params = context.params
    stage_name = get_choice(params, "stage", STAGE_NAMES, required=True)
    language = get_choice(params, "language", ("auto", "hlsl", "glsl"), "auto", required=True)
    event_id = resolve_event(context, action_index(runtime, context))
    resources = resource_index(runtime, context)
    session = session_of(context)

    def snapshot(controller):
        pipe = controller.GetPipelineState()
        stage, _, key, reflection = _bound_shader(pipe, renderdoc_module(), stage_name, event_id)
        raw = getattr(reflection, "rawBytes", None)
        if raw is None:
            raise _unsupported("The bound shader does not expose its original bytecode.")
        if len(raw) > MAX_SHADER_BYTES:
            raise AgentBridgeError("LIMIT_EXCEEDED", "Original shader bytecode exceeds 16 MiB.",
                                   details={"bytes": len(raw), "maxBytes": MAX_SHADER_BYTES})
        # Only owned Python values leave this callback. The process runs after at_event restores EID.
        raw = bytes(raw)
        entry_point = pipe.GetShaderEntryPoint(stage)
        if not isinstance(entry_point, str) or len(entry_point) > 1024 or "\x00" in entry_point:
            raise _unsupported("The shader entry point metadata is invalid.")
        return raw, {"eventId": event_id, "stage": stage_name,
                     "shader": {"resourceId": key, "name": resources.name(key)},
                     "entryPoint": entry_point, "inputEncoding": enum_name(reflection.encoding)}

    (raw, data), meta = runtime.replay.at_event(context, event_id, snapshot)
    _session_guard(runtime, context, session)
    _validate_bytecode(raw, data["inputEncoding"], data["entryPoint"], context)
    input_hash = hashlib.sha256(raw).hexdigest()
    from ..shader_decompiler import convert
    text, provenance, warnings = convert(raw, data["inputEncoding"], stage_name,
                                         data["entryPoint"], language, context)
    _session_guard(runtime, context, session)
    if not isinstance(provenance, dict) or not isinstance(warnings, list) or any(
            not isinstance(warning, str) for warning in warnings):
        raise AgentBridgeError("INTERNAL_ERROR", "The decompiler returned invalid provenance metadata.")
    output_language = provenance.get("outputLanguage")
    if output_language not in ("hlsl", "glsl"):
        raise AgentBridgeError("INTERNAL_ERROR", "The decompiler did not identify its actual output language.")
    if language != "auto" and output_language != language:
        raise AgentBridgeError("INTERNAL_ERROR", "The decompiler output language differs from the explicit request.")
    try:
        metadata_size = len(json.dumps([provenance, warnings], ensure_ascii=True).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        raise AgentBridgeError("INTERNAL_ERROR", "The decompiler provenance is not JSON metadata.")
    if metadata_size > MAX_METADATA_BYTES:
        raise AgentBridgeError("LIMIT_EXCEEDED", "Decompiler provenance/log metadata exceeds 64 KiB.")
    encoded = _encode_output(text, context)
    readability = _source._read_artifact_info(text, context)
    if "readArtifactReason" in readability:
        readability["readArtifactReason"] = readability["readArtifactReason"].replace(
            "Embedded source", "Generated shader text")
    data.update(available=True, generated=True, isOriginalSource=False,
                inputBytes=len(raw), inputSha256=input_hash, requestedLanguage=language,
                outputLanguage=output_language,
                outputBytes=len(encoded), decompiler=provenance)
    data.update(readability)
    warnings = list(warnings) + [
        "This artifact is generated shader text, not embedded or original source code.",
        "Original names, comments, macros and source structure cannot be recovered reliably "
        "from optimized bytecode; generated text is not a guarantee of semantic equivalence.",
        "Resource bindings and interface locations may be remapped by translation. Use the "
        "captured shader reflection and pipeline bindings as the authoritative mapping.",
    ]
    if not readability["readArtifactCompatible"]:
        warnings.append(readability["readArtifactReason"])
    _session_guard(runtime, context, session)
    source = {"event_id": event_id, "stage": stage_name,
              "shaderResourceId": data["shader"]["resourceId"], "entryPoint": data["entryPoint"],
              "inputEncoding": data["inputEncoding"], "inputBytes": len(raw),
              "inputSha256": input_hash, "language": output_language, "requestedLanguage": language,
              "generated": True, "isOriginalSource": False, "decompiler": provenance}
    artifact = write_bytes(runtime, session,
                           "shader-decompiled-%s-%s" % (data["shader"]["resourceId"], stage_name),
                           "txt", encoded, "shader-decompiled", "text/plain; charset=utf-8",
                           source=source)
    try:
        _session_guard(runtime, context, session)
    except BaseException:
        # This is our uniquely generated artifact, never a capture-supplied output filename.
        try:
            os.remove(artifact["path"])
        except OSError:
            pass
        raise
    data["decompiledArtifact"] = artifact["path"]
    return result(data, meta=meta, artifacts=[artifact], warnings=warnings)


HANDLERS = {"decompile_shader": decompile_shader}
