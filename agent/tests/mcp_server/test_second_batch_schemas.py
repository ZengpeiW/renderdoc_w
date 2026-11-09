"""Second-batch MCP parsing, image output and fixed-capture boundary tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from fake_bridge import _png_bytes
from helpers import ManualResponder, write_manifest
from rendertest_mcp.ipc_client import BridgeClient, BridgeResponse
from rendertest_mcp.protocol import LONG_TIMEOUT_SECONDS, TOOL_NAMES
from rendertest_mcp.server import create_server

OVERLAY_ARGS = {"resource_id": "9007199254740993", "overlay": "Wireframe"}
INSTANCE = "overlay-test"
SESSION = "a" * 32


class RecordingBridge:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.artifacts = []

    def call(self, method, params, **options):
        self.calls.append((method, params, options))
        return BridgeResponse("test", INSTANCE, {"data": params, "artifacts": self.artifacts},
                              {"captureSessionId": SESSION})

    def validate_artifact_scope(self, instance, session):
        assert instance == INSTANCE and session == SESSION


@pytest.fixture
def recording(tmp_path):
    bridge = RecordingBridge(tmp_path)
    return create_server(bridge), bridge


def call(server, name, arguments):
    return asyncio.run(server.call_tool(name, arguments))


def envelope(response):
    if isinstance(response, tuple):
        return response[1]
    return json.loads(next(item.text for item in response if isinstance(item, TextContent)))


def test_legacy_cbuffer_payload_is_unchanged_and_new_options_are_optional(recording):
    server, bridge = recording
    call(server, "get_cbuffer", {"stage": "pixel", "index": 0})
    assert bridge.calls[-1][1] == {"stage": "pixel", "index": 0}
    call(server, "get_cbuffer", {"stage": "pixel", "name": "Globals", "member_path": None,
                                 "cursor": None, "limit": None, "max_depth": None})
    assert bridge.calls[-1][1] == {"stage": "pixel", "name": "Globals"}
    call(server, "get_cbuffer", {"stage": "pixel", "index": 0, "member_path": "",
                                 "limit": 1000, "max_depth": 0})
    assert bridge.calls[-1][1] == {"stage": "pixel", "index": 0, "member_path": "",
                                  "limit": 1000, "max_depth": 0}
    call(server, "get_cbuffer", {"stage": "pixel", "index": 0, "max_depth": 4,
                                 "member_path": "Lights[12].Color", "cursor": "opaque"})
    assert bridge.calls[-1][1]["member_path"] == "Lights[12].Color"
    assert bridge.calls[-1][1]["cursor"] == "opaque" and bridge.calls[-1][1]["max_depth"] == 4


@pytest.mark.parametrize("path", ["[1]", "[0][123][1]", '["name"]',
                                  '["with.dots[0]"]', "null", "true", "{}", "[]"])
def test_cbuffer_member_path_remains_literal_through_fastmcp(recording, path):
    server, bridge = recording
    call(server, "get_cbuffer", {"stage": "pixel", "index": 0, "member_path": path})
    assert bridge.calls[-1][1]["member_path"] == path
    tool = next(item for item in asyncio.run(server.list_tools()) if item.name == "get_cbuffer")
    field = tool.inputSchema["properties"]["member_path"]
    assert {part["type"] for part in field["anyOf"]} == {"string", "null"}
    assert field["default"] is None


@pytest.mark.parametrize("value", [[1], ["name"], {"name": 1}, {}, True, 1])
def test_cbuffer_literal_path_adapter_does_not_coerce_non_strings(recording, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "get_cbuffer", {"stage": "pixel", "index": 0, "member_path": value})
    assert bridge.calls == []


@pytest.mark.parametrize("field", ["limit", "max_depth"])
@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, "1", -1])
def test_new_cbuffer_integer_options_are_strict_before_bridge(recording, field, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "get_cbuffer", {"stage": "pixel", "index": 0, field: value})
    assert bridge.calls == []


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 1001}, {"max_depth": 5}, {"member_path": True},
    {"member_path": []}, {"member_path": "x" * 4097}, {"cursor": "x" * 1025},
])
def test_cbuffer_member_and_page_bounds(recording, params):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "get_cbuffer", dict(stage="pixel", index=0, **params))
    assert bridge.calls == []


@pytest.mark.parametrize("selectors", [{}, {"index": 0, "name": "Globals"}])
def test_cbuffer_member_keeps_exactly_one_buffer_selector(recording, selectors):
    server, bridge = recording
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        call(server, "get_cbuffer", dict(stage="pixel", member_path="", **selectors))
    assert bridge.calls == []


@pytest.mark.parametrize("options", [{"cursor": "opaque"}, {"limit": 1}, {"max_depth": 0}])
def test_cbuffer_page_options_require_explicit_member_path(recording, options):
    server, bridge = recording
    with pytest.raises(ToolError, match="member_path"):
        call(server, "get_cbuffer", dict(stage="pixel", index=0, **options))
    assert bridge.calls == []


def test_overlay_defaults_and_precision_reach_bridge_with_long_timeout(recording):
    server, bridge = recording
    response = call(server, "export_overlay", OVERLAY_ARGS)
    method, params, options = bridge.calls[-1]
    assert method == "export_overlay"
    assert params == dict(OVERLAY_ARGS, mip=0, slice=0, sample=0, type_cast="Typeless",
                          range_min=0.0, range_max=1.0, max_dimension=1024)
    assert options["timeout"] == LONG_TIMEOUT_SECONDS and options["instance_id"] is None
    assert envelope(response)["effect"] == "artifact"
    assert not any(isinstance(item, ImageContent) for item in response)
    call(server, "export_overlay", dict(OVERLAY_ARGS, event_id=0, mip=2, slice=3, sample=4,
                                        type_cast="UInt", range_min=-1, range_max=2,
                                        max_dimension=4096, instance_id="selected-window"))
    assert bridge.calls[-1][1]["event_id"] == 0 and bridge.calls[-1][1]["sample"] == 4
    assert bridge.calls[-1][1]["range_min"] == -1.0
    assert bridge.calls[-1][2]["instance_id"] == "selected-window"


@pytest.mark.parametrize("field", ["event_id", "mip", "slice", "sample", "max_dimension"])
@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, "1", -1])
def test_overlay_integer_parameters_reject_coercion(recording, field, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "export_overlay", dict(OVERLAY_ARGS, **{field: value}))
    assert bridge.calls == []


@pytest.mark.parametrize("params", [
    {"max_dimension": 0}, {"max_dimension": 4097}, {"max_dimension": None},
    {"mip": None}, {"slice": None}, {"sample": None}, {"include_image": 1},
    {"include_image": "false"}, {"include_image": None}, {"overlay": "ClearBeforeDraw"},
    {"overlay": "QuadOverdrawPass"}, {"overlay": "wireframe"}, {"overlay": None},
    {"resource_id": 9007199254740993}, {"resource_id": "1.0"}, {"resource_id": "../100"},
    {"type_cast": "not-a-cast"},
])
def test_overlay_selector_and_budget_bounds(recording, params):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "export_overlay", dict(OVERLAY_ARGS, **params))
    assert bridge.calls == []


@pytest.mark.parametrize("arguments", [{"resource_id": "100"}, {"overlay": "Wireframe"}, {}])
def test_overlay_requires_explicit_resource_and_diagnostic(recording, arguments):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "export_overlay", arguments)
    assert bridge.calls == []


@pytest.mark.parametrize("field", ["range_min", "range_max"])
@pytest.mark.parametrize("value", [True, False, "0", None, float("nan"), float("inf"), -float("inf")])
def test_overlay_range_is_finite_and_rejects_boolean_or_string(recording, field, value):
    server, bridge = recording
    with pytest.raises(ToolError):
        call(server, "export_overlay", dict(OVERLAY_ARGS, **{field: value}))
    assert bridge.calls == []


@pytest.mark.parametrize("low,high", [(0, 0), (1, 1), (2, 1)])
def test_overlay_range_order_is_checked_before_bridge(recording, low, high):
    server, bridge = recording
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        call(server, "export_overlay", dict(OVERLAY_ARGS, range_min=low, range_max=high))
    assert bridge.calls == []


def make_image(root, session=SESSION):
    path = root / "artifacts" / INSTANCE / session / "replay" / "overlay.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _png_bytes(1, 1, [(255, 0, 0, 255)])
    path.write_bytes(body)
    return {"kind": "overlay", "path": str(path), "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(), "mediaType": "image/png",
            "source": dict(OVERLAY_ARGS, event_id=5, mip=0, slice=0, sample=0)}


def test_overlay_image_is_opt_in_and_preserves_artifact_metadata(recording):
    server, bridge = recording
    artifact = make_image(bridge.root)
    bridge.artifacts = [artifact]
    default = call(server, "export_overlay", OVERLAY_ARGS)
    assert not any(isinstance(item, ImageContent) for item in default)
    attached = call(server, "export_overlay", dict(OVERLAY_ARGS, include_image=True))
    assert sum(isinstance(item, ImageContent) for item in attached) == 1
    assert envelope(attached)["artifacts"] == [artifact]
    assert "include_image" not in bridge.calls[-1][1]
    # Shared helper keeps existing texture attachment behaviour.
    texture = call(server, "export_texture", {"resource_id": "100", "include_image": True})
    assert any(isinstance(item, ImageContent) for item in texture)


def test_overlay_image_attachment_is_bounded(recording):
    server, bridge = recording
    artifact = make_image(bridge.root)
    artifact["bytes"] = 8 * 1024 * 1024 + 1
    bridge.artifacts = [artifact]
    result = call(server, "export_overlay", dict(OVERLAY_ARGS, include_image=True))
    assert envelope(result)["artifacts"] == [artifact]
    assert not any(isinstance(item, ImageContent) for item in result)


@pytest.mark.parametrize("wrong_session", [False, True])
def test_overlay_image_uses_actual_fixed_capture_validation(tmp_path, wrong_session):
    directory = write_manifest(tmp_path, INSTANCE, capture_session_id=SESSION)
    artifact = make_image(tmp_path, "b" * 32 if wrong_session else SESSION)
    requests = []

    def respond(request):
        requests.append(request)
        return json.dumps({"schema": "rendertest-agent-response/v1", "protocol": "rendertest-agent-ipc/v1",
                           "id": request["id"], "ok": True,
                           "result": {"data": {}, "artifacts": [artifact]},
                           "meta": {"instanceId": INSTANCE, "captureSessionId": SESSION}}).encode()

    client = BridgeClient(root=tmp_path, environ={"RENDERTEST_AGENT_FIXED_INSTANCE_ID": INSTANCE,
                          "RENDERTEST_AGENT_CAPTURE_SESSION_ID": SESSION}, poll_interval=0.005)
    server = create_server(client)
    with ManualResponder(directory, respond):
        if wrong_session:
            with pytest.raises(ToolError, match="INVALID_RESPONSE"):
                call(server, "export_overlay", dict(OVERLAY_ARGS, include_image=True))
        else:
            result = call(server, "export_overlay", dict(OVERLAY_ARGS, include_image=True))
            assert any(isinstance(item, ImageContent) for item in result)
            assert envelope(result)["captureSessionId"] == SESSION
    assert requests[0]["expected"] == {"instanceId": INSTANCE, "captureSessionId": SESSION}


def test_second_batch_does_not_create_extra_cbuffer_tool_and_declares_effects(recording):
    server, _ = recording
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == set(TOOL_NAMES) and len(tools) == 31
    assert "get_cbuffer_member" not in tools
    cbuffer = tools["get_cbuffer"]
    assert cbuffer.annotations.readOnlyHint is True
    assert {"member_path", "cursor", "limit", "max_depth"} <= set(cbuffer.inputSchema["properties"])
    assert tools["export_overlay"].inputSchema["required"] == ["resource_id", "overlay"]
    assert tools["export_overlay"].annotations.readOnlyHint is False
    assert tools["export_overlay"].annotations.destructiveHint is False
