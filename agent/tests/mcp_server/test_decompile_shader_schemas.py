"""Decompilation MCP validation, effect and artifact-reader integration contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from helpers import ManualResponder, write_manifest
from rendertest_agent_bridge.constants import METHOD_SPECS as BRIDGE_SPECS
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.ipc_client import BridgeClient, BridgeResponse
from rendertest_mcp.protocol import LONG_TIMEOUT_SECONDS, METHOD_SPECS, TOOL_NAMES
from rendertest_mcp.server import create_server

INSTANCE = "decompile-test"
SESSION = "d" * 32


class RecordingBridge:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.error = None
        self.response = None

    def call(self, method, params, **options):
        self.calls.append((method, params, options))
        if self.error is not None:
            raise self.error
        return BridgeResponse("request", INSTANCE, self.response or {"data": params},
                              {"captureSessionId": SESSION})


@pytest.fixture
def recording(tmp_path):
    bridge = RecordingBridge(tmp_path)
    return create_server(bridge), bridge


def call(server, name, arguments):
    return asyncio.run(server.call_tool(name, arguments))


def envelope(result):
    return result[1]


def test_default_and_explicit_decompilation_payloads_keep_instance_out_of_params(recording):
    server, bridge = recording
    response = call(server, "decompile_shader", {"stage": "pixel"})
    assert bridge.calls[-1] == ("decompile_shader", {"stage": "pixel", "language": "auto"},
                                {"instance_id": None, "timeout": LONG_TIMEOUT_SECONDS})
    assert envelope(response)["effect"] == "artifact"
    assert envelope(response)["captureSessionId"] == SESSION
    call(server, "decompile_shader", {"stage": "compute", "language": "glsl", "event_id": 0,
                                       "instance_id": "specific-window"})
    assert bridge.calls[-1] == ("decompile_shader",
                                {"stage": "compute", "event_id": 0, "language": "glsl"},
                                {"instance_id": "specific-window", "timeout": 120.0})
    call(server, "decompile_shader", {"stage": "vertex", "event_id": None})
    assert "event_id" not in bridge.calls[-1][1]
    call(server, "decompile_shader", {"stage": "pixel", "language": "hlsl"})
    assert bridge.calls[-1][1]["language"] == "hlsl"


@pytest.mark.parametrize("output_language", ["hlsl", "glsl"])
def test_auto_preserves_actual_language_and_conversion_warnings(recording, output_language):
    server, bridge = recording
    warnings = ["HLSL backend unsupported; generated GLSL instead."] if output_language == "glsl" else []
    bridge.response = {"data": {"requestedLanguage": "auto", "outputLanguage": output_language,
                                 "generated": True, "isOriginalSource": False}, "warnings": warnings}
    result = envelope(call(server, "decompile_shader", {"stage": "pixel"}))
    assert bridge.calls[-1][1]["language"] == "auto"
    assert result["data"] == bridge.response["data"]
    assert result["warnings"] == warnings


@pytest.mark.parametrize("arguments", [
    {}, {"stage": None}, {"stage": "fragment"}, {"stage": "Pixel"}, {"stage": True},
    {"stage": "pixel", "language": "HLSL"}, {"stage": "pixel", "language": "spirv"},
    {"stage": "pixel", "language": None}, {"stage": "pixel", "language": 1},
    {"stage": "pixel", "event_id": True}, {"stage": "pixel", "event_id": False},
    {"stage": "pixel", "event_id": -1}, {"stage": "pixel", "event_id": 1.0},
    {"stage": "pixel", "event_id": 1.5}, {"stage": "pixel", "event_id": "1"},
    {"stage": "pixel", "instance_id": "../other"},
])
def test_invalid_decompile_selectors_are_rejected_before_bridge(recording, arguments):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "decompile_shader", arguments)
    assert bridge.calls == []


def test_decompile_registration_is_capture_scoped_artifact_with_no_tool_path_parameter(recording):
    server, _ = recording
    tools = {item.name: item for item in asyncio.run(server.list_tools())}
    assert len(tools) == 35 and set(tools) == set(TOOL_NAMES)
    assert METHOD_SPECS["decompile_shader"] == BRIDGE_SPECS["decompile_shader"] == {
        "effect": "artifact", "requiresCapture": True}
    tool = tools["decompile_shader"]
    assert tool.inputSchema["required"] == ["stage"]
    assert set(tool.inputSchema["properties"]) == {"stage", "event_id", "language", "instance_id"}
    assert tool.inputSchema["properties"]["language"]["enum"] == ["auto", "hlsl", "glsl"]
    assert tool.inputSchema["properties"]["language"]["default"] == "auto"
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is False
    assert tool.annotations.openWorldHint is False
    assert "not the original source" in tool.description
    assert "read_artifact" in tool.description


@pytest.mark.parametrize("code", ["UNSUPPORTED", "TIMEOUT", "CANCELLED", "CAPTURE_CHANGED"])
def test_converter_failure_preserves_code_and_details_without_success_artifacts(recording, code):
    server, bridge = recording
    bridge.error = RenderTestAgentError(code, "Conversion did not complete.",
                                       details={"step": "spirv-cross", "reason": "unsupported opcode"})
    with pytest.raises(ToolError) as raised:
        call(server, "decompile_shader", {"stage": "pixel"})
    assert '"code": "' + code + '"' in str(raised.value)
    assert '"reason": "unsupported opcode"' in str(raised.value)
    assert len(bridge.calls) == 1


@pytest.mark.parametrize("wrong_session", [False, True])
def test_decompiled_text_artifact_is_readable_only_in_its_bound_capture(tmp_path, wrong_session):
    directory = write_manifest(tmp_path, INSTANCE, capture_session_id=SESSION)
    artifact_session = "e" * 32 if wrong_session else SESSION
    path = tmp_path / "artifacts" / INSTANCE / artifact_session / "replay" / "decompiled-hlsl.txt"
    path.parent.mkdir(parents=True)
    body = b"// Generated HLSL, not original source.\nfloat4 main() { return 1.0; }\n"
    path.write_bytes(body)
    artifact = {"kind": "shader", "path": str(path), "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(), "mediaType": "text/plain",
                "source": {"event_id": 42, "stage": "pixel", "language": "hlsl"}}
    requests = []

    def respond(request):
        requests.append(request)
        return json.dumps({
            "schema": "rendertest-agent-response/v1", "protocol": "rendertest-agent-ipc/v1",
            "id": request["id"], "ok": True,
            "result": {"data": {"requestedLanguage": "auto", "outputLanguage": "hlsl"},
                       "artifacts": [artifact]},
            "meta": {"instanceId": INSTANCE, "captureSessionId": SESSION},
        }).encode()

    client = BridgeClient(root=tmp_path, poll_interval=0.005, environ={
        "RENDERTEST_AGENT_FIXED_INSTANCE_ID": INSTANCE,
        "RENDERTEST_AGENT_CAPTURE_SESSION_ID": SESSION,
    })
    server = create_server(client)
    with ManualResponder(directory, respond):
        if wrong_session:
            with pytest.raises(ToolError, match="INVALID_RESPONSE"):
                call(server, "decompile_shader", {"stage": "pixel", "event_id": 42})
        else:
            result = envelope(call(server, "decompile_shader", {"stage": "pixel", "event_id": 42}))
            assert result["artifacts"] == [artifact]
            text = envelope(call(server, "read_artifact", {"path": str(path), "search": "main"}))
            assert text["data"]["lines"] == [{"line": 2, "text": "float4 main() { return 1.0; }"}]
            path.write_bytes(body.replace(b"1.0", b"2.0"))
            with pytest.raises(ToolError, match="SHA-256"):
                call(server, "read_artifact", {"path": str(path)})
    assert len(requests) == 1
    assert requests[0]["expected"] == {"instanceId": INSTANCE, "captureSessionId": SESSION}
