from __future__ import annotations

import base64
import hashlib
import json
import struct
import sys
import types
import zlib
from pathlib import Path
from typing import Any

import pytest

import rendertest_agent_bridge as bridge
from fake_renderdoc import FakeContext, FakeReplayManager, FileType, module
from rendertest_agent_bridge.ipc import AgentBridgeError
from rendertest_agent_bridge.lifecycle import CaptureState
from rendertest_agent_bridge.methods.buffers import decode_rows, parse_format
from rendertest_agent_bridge.paging import decode_cursor, encode_cursor, paginate
from rendertest_agent_bridge.replay import ReplayRunner
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.ipc_client import BridgeClient


@pytest.fixture
def fake_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    qrd = types.ModuleType("qrenderdoc")

    class CaptureViewer:
        def __init__(self) -> None:
            pass

    qrd.CaptureViewer = CaptureViewer
    monkeypatch.setitem(sys.modules, "qrenderdoc", qrd)
    monkeypatch.setitem(sys.modules, "renderdoc", module())


@pytest.fixture
def loaded(fake_modules: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RENDERTEST_AGENT_ROOT", str(tmp_path))
    ctx = FakeContext()
    bridge.start("1.47", ctx)
    ctx.load(9)
    api = BridgeClient(root=tmp_path, environ={}, poll_interval=0.005, default_timeout=10.0)
    yield ctx, api
    bridge.stop()


def call(api: BridgeClient, method: str, **params: Any) -> Any:
    return api.call(method, params)


def error_code(api: BridgeClient, method: str, **params: Any) -> str:
    with pytest.raises(RenderTestAgentError) as error:
        api.call(method, params)
    return error.value.code


def test_manifest_lists_p0_methods(loaded) -> None:
    _, api = loaded
    methods = call(api, "get_status").result["data"]["bridge"]["methods"]
    assert {"list_actions", "inspect_event", "export_texture", "get_debug_messages"} <= set(methods)
    assert "pixel_history" in methods
    assert "debug_vertex" in methods and "get_mesh_data" not in methods


def test_list_and_find_actions(loaded) -> None:
    _, api = loaded
    roots = call(api, "list_actions").result
    assert [item["eventId"] for item in roots["data"]["actions"]] == [1, 9]
    assert roots["data"]["actions"][0]["childCount"] == 2

    flat = call(api, "list_actions", include_markers=False).result["data"]["actions"]
    assert [item["eventId"] for item in flat] == [5, 8, 9]

    first = call(api, "list_actions", parent_event_id=1, limit=1).result
    assert [item["eventId"] for item in first["data"]["actions"]] == [5]
    assert first["data"]["parentPath"] == ["Frame"]
    second = call(api, "list_actions", parent_event_id=1, limit=1, cursor=first["nextCursor"])
    assert [item["eventId"] for item in second.result["data"]["actions"]] == [8]
    assert second.result["nextCursor"] is None
    assert error_code(api, "list_actions", cursor=first["nextCursor"]) == "INVALID_REQUEST"

    found = call(api, "find_actions", query="draw", flags=["draw"]).result["data"]
    assert [item["eventId"] for item in found["actions"]] == [5]
    assert found["actions"][0]["path"] == ["Frame"]
    assert found["actions"][0]["flags"] == ["Drawcall", "Indexed"]
    assert error_code(api, "list_actions", parent_event_id=3) == "INVALID_EVENT"


def test_read_tools_restore_the_replay_event(loaded) -> None:
    ctx, api = loaded
    controller = ctx.controller
    controller.frame_events.clear()

    response = call(api, "inspect_event", event_id=5)
    data = response.result["data"]
    assert data["action"]["name"] == "DrawIndexed(36)"
    assert set(data["pipeline"]["shaders"]) == {"vertex", "pixel"}
    assert data["pipeline"]["outputTargets"][0]["name"] == "SceneColor"
    assert data["pipeline"]["depthTarget"]["resourceId"] == "101"
    assert data["pipeline"]["bindings"]["pixel"]["readOnlyResources"][0]["name"] == "albedo"
    assert response.meta["eventIdBefore"] == 9
    assert response.meta["eventIdAfter"] == 9
    assert response.meta["replayEventRestored"] is True
    assert controller.frame_events == [5, 9]
    assert controller.current == 9

    status = call(api, "get_status").result["data"]["capture"]
    assert status["currentEventId"] == 9 and status["selectedEventId"] == 9

    nested = call(api, "inspect_event", event_id=3).result["data"]
    assert nested["isAction"] is False and nested["action"]["eventId"] == 5
    assert error_code(api, "inspect_event", event_id=999) == "INVALID_EVENT"


def test_pipeline_state_and_shaders(loaded) -> None:
    _, api = loaded
    graphics = call(api, "get_pipeline_state", event_id=5).result["data"]
    assert graphics["inputAssembly"]["topology"] == "TriangleList"
    assert graphics["inputAssembly"]["indexBuffer"]["byteStride"] == 2
    assert graphics["rasterizer"]["cullMode"] == "Back"
    assert graphics["outputTargets"][0]["blend"]["color"]["operation"] == "Add"
    assert graphics["bindings"]["pixel"]["samplers"][0]["maxLOD"] == "Infinity"

    compute = call(api, "get_pipeline_state", event_id=8, include_bindings=False).result["data"]
    assert set(compute["shaders"]) == {"compute"}
    assert compute["shaders"]["compute"]["threadGroupSize"] == [8, 8, 1]
    assert "outputTargets" not in compute and "bindings" not in compute

    shader = call(api, "get_shader", stage="pixel", event_id=5, include_disassembly=True)
    data = shader.result["data"]
    assert data["name"] == "MainPS"
    assert data["disassemblyTargets"] == ["DXIL", "HLSL"]
    assert data["disassembly"].startswith("; DXIL disassembly")
    assert data["reflection"]["constantBlocks"][0]["name"] == "Globals"
    assert data["reflection"]["outputSignature"][0]["semanticName"] == "SV_Target"
    assert error_code(api, "get_shader", stage="pixel", event_id=5, include_disassembly=True,
                      target="ISA") == "INVALID_REQUEST"
    assert error_code(api, "get_shader", stage="pixel", event_id=8) == "INVALID_EVENT"


def test_cbuffer_values(loaded) -> None:
    _, api = loaded
    data = call(api, "get_cbuffer", stage="pixel", event_id=5, name="globals").result["data"]
    assert data["name"] == "Globals"
    assert data["binding"]["resourceId"] == "200"
    tint, count = data["variables"]
    assert tint["value"] == [1.0, 0.5, 0.25, "NaN"]
    assert tint["byteOffset"] == 0 and tint["typeName"] == "float4"
    assert count["value"] == [7] and count["byteOffset"] == 16
    assert error_code(api, "get_cbuffer", stage="pixel", event_id=5, index=3) == "INVALID_REQUEST"


def test_resources_and_usage(loaded) -> None:
    _, api = loaded
    textures = call(api, "list_resources", kind="texture").result["data"]
    assert [item["name"] for item in textures["resources"]] == ["SceneColor", "SceneDepth"]
    assert textures["resources"][0]["mips"] == 2
    assert call(api, "list_resources", query="pso").result["data"]["resources"][0]["kind"] == \
        "pipeline"

    resource = call(api, "get_resource", resource_id="100").result["data"]
    assert resource["format"]["name"] == "R8G8B8A8_UNORM"
    assert resource["initialisationChunkCount"] == 2
    assert error_code(api, "get_resource", resource_id="999") == "INVALID_RESOURCE"

    usage = call(api, "get_resource_usage", resource_id="100").result["data"]["usages"]
    assert [(item["eventId"], item["access"]) for item in usage] == \
        [(5, "write"), (7, "write"), (8, "read")]
    assert usage[0]["actionName"] == "DrawIndexed(36)"


def test_texture_tools(loaded) -> None:
    ctx, api = loaded
    pixel = call(api, "pick_pixel", resource_id="100", x=2, y=1, event_id=5).result["data"]
    assert pixel["value"] == [0.5, 0.25, 0.0, 1.0]
    assert pixel["interpretedAs"] == "float"
    assert error_code(api, "pick_pixel", resource_id="100", x=4, y=0) == "INVALID_REQUEST"
    assert error_code(api, "pick_pixel", resource_id="200", x=0, y=0) == "INVALID_RESOURCE"
    assert error_code(api, "pick_pixel", resource_id="100", x=0, y=0, mip=2) == "INVALID_REQUEST"

    stats = call(api, "get_texture_stats", resource_id="100").result["data"]
    assert stats["max"]["value"] == [0.75, 0.75, 0.0, 1.0]

    exported = call(api, "export_texture", resource_id="100", event_id=5, mip=1).result
    artifact = exported["artifacts"][0]
    path = Path(artifact["path"])
    assert path.suffix == ".png" and path.is_file()
    assert (artifact["width"], artifact["height"]) == (2, 2)
    assert artifact["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact["source"]["mip"] == 1
    assert ctx.controller.current == 9


def test_pixel_history(loaded) -> None:
    ctx, api = loaded
    controller = ctx.controller
    controller.frame_events.clear()

    response = call(api, "pixel_history", resource_id="100", x=1, y=2, event_id=5)
    data = response.result["data"]
    assert data["total"] == 1 and data["order"] == "newest"
    event = data["events"][0]
    assert event["eventId"] == 5 and event["actionName"] == "DrawIndexed(36)"
    assert (event["fragmentCount"], event["passedCount"], event["passed"]) == (2, 1, True)
    assert event["failureCounts"] == {"depthTestFailed": 1}
    assert event["primitiveIds"] == [3] and event["changed"] is True
    assert event["preMod"] == {"color": [0.0, 0.0, 0.0, 1.0], "depth": None, "stencil": None}
    assert event["shaderOut"]["depth"] == 0.25
    assert event["postMod"]["color"] == [0.5, 0.5, 0.5, 1.0]
    assert "fragments" not in event
    summary = data["summary"]
    assert summary["lastPassedEventId"] == 5 and summary["lastChangedEventId"] == 5
    assert summary["eventCount"] == 1 and summary["fragmentCount"] == 2
    assert data["discardEventIds"] == []
    assert response.meta["replayEventRestored"] is True
    assert controller.history_calls == [(5, 1, 2, 0)]
    assert controller.current == 9

    fragments = call(api, "pixel_history", resource_id="100", x=1, y=2, event_id=5,
                     include_fragments=True).result["data"]["events"][0]["fragments"]
    assert [(item["fragIndex"], item["passed"]) for item in fragments] == [(0, True), (1, False)]
    assert fragments[1]["failures"] == ["depthTestFailed"] and fragments[1]["preMod"] is None
    assert len(controller.history_calls) == 1

    full = call(api, "pixel_history", resource_id="100", x=1, y=2, event_id=9, order="oldest",
                limit=1).result
    assert [item["eventId"] for item in full["data"]["events"]] == [5]
    assert full["data"]["summary"]["lastChangedEventId"] == 8
    assert full["data"]["summary"]["finalValue"]["color"] == [0.0, 0.0, 0.0, 1.0]
    assert full["data"]["discardEventIds"] == [7]
    assert not full["warnings"]
    rest = call(api, "pixel_history", resource_id="100", x=1, y=2, event_id=9, order="oldest",
                limit=1, cursor=full["nextCursor"]).result
    assert [item["eventId"] for item in rest["data"]["events"]] == [8]
    assert rest["data"]["events"][0]["directShaderWrite"] is True
    assert rest["nextCursor"] is None
    assert len(controller.history_calls) == 2
    assert error_code(api, "pixel_history", resource_id="100", x=1, y=2, event_id=9,
                      cursor=full["nextCursor"]) == "INVALID_REQUEST"

    assert error_code(api, "pixel_history", resource_id="100", x=4, y=0) == "INVALID_REQUEST"
    assert error_code(api, "pixel_history", resource_id="200", x=0, y=0) == "INVALID_RESOURCE"
    assert error_code(api, "pixel_history", resource_id="100", x=0, y=0,
                      order="sideways") == "INVALID_REQUEST"
    controller.pixel_history_supported = False
    assert error_code(api, "pixel_history", resource_id="100", x=0, y=0) == "UNSUPPORTED"


def test_pixel_history_warns_about_discards(loaded) -> None:
    ctx, api = loaded
    ctx.controller.PixelHistory = lambda *args: []
    result = call(api, "pixel_history", resource_id="100", x=0, y=0, event_id=9).result
    assert result["data"]["summary"]["lastPassedEventId"] is None
    assert result["data"]["events"] == []
    assert len(result["warnings"]) == 2 and "Discard events 7" in result["warnings"][1]


def test_debug_pixel(loaded) -> None:
    ctx, api = loaded
    controller = ctx.controller
    response = call(api, "debug_pixel", x=1, y=2, event_id=5, primitive=3)
    result = response.result
    data = result["data"]
    assert controller.debug_calls == [(5, 1, 2, 3)]
    assert len(controller.freed) == 1 and controller.current == 9
    assert response.meta["replayEventRestored"] is True
    assert data["debugged"] is True and data["steps"] == 3 and data["executionComplete"] is True
    assert data["shader"]["resourceId"] == "300" and data["shader"]["entryPoint"] == "main"
    assert data["inputs"][0]["name"] == "uv" and data["inputs"][0]["value"] == [0.5, 0.25]
    assert data["outputs"] == [{"name": "SV_Target0", "signatureIndex": 0, "type": "Float",
                                "value": [0.25, 0.5, 0.75, 1.0]}]
    assert [item["name"] for item in data["lastChanges"]] == ["o0"]
    assert [item["step"] for item in data["nanOrInfSteps"]] == [2]
    assert data["nanOrInfSteps"][0]["changes"][0]["value"] == [2.0, "NaN"]
    assert data["nanOrInfSteps"][0]["line"] == {"disassemblyLine": 5, "file": "shader.hlsl",
                                                "lineStart": 3, "lineEnd": 3}
    assert data["sampleSteps"] == 1

    artifact = result["artifacts"][0]
    lines = Path(artifact["path"]).read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0]["record"] == "header" and records[0]["files"] == ["shader.hlsl"]
    assert [item["step"] for item in records[1:-1]] == [1, 2, 3]
    assert records[1]["line"] == {"disassemblyLine": 1}
    assert records[-1] == {"record": "summary", "steps": 3, "executionComplete": True,
                           "traceComplete": True}
    assert artifact["sha256"] == hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
    assert not list(Path(artifact["path"]).parent.glob("*.part"))

    limited = call(api, "debug_pixel", x=1, y=2, event_id=5, max_steps=2).result
    assert limited["data"]["executionComplete"] is False and limited["data"]["steps"] == 2
    assert limited["data"]["outputs"][0]["value"] == [None, None, None, None]
    assert any("max_steps" in item for item in limited["warnings"])

    controller.debug_misses = True
    missed = call(api, "debug_pixel", x=3, y=3, event_id=5).result
    assert missed["data"]["debugged"] is False and missed["artifacts"] == []
    assert "pixel_history" in missed["warnings"][0]
    assert error_code(api, "debug_pixel", x=0, y=0, event_id=8) == "INVALID_EVENT"
    assert error_code(api, "debug_pixel", x=0, y=0, event_id=5, max_steps=0) == "INVALID_REQUEST"


def test_debug_vertex(loaded) -> None:
    ctx, api = loaded
    controller = ctx.controller
    response = call(api, "debug_vertex", vertex=4, event_id=5)
    result = response.result
    data = result["data"]
    assert controller.debug_calls == [(5, 4, 0, 14, 0)]
    assert len(controller.freed) == 1 and controller.current == 9
    assert response.meta["replayEventRestored"] is True
    assert data["vertex"] == 4 and data["instance"] == 0 and data["view"] == 0
    assert data["index"] == 14 and data["indexBufferValue"] == 14
    assert data["shader"]["resourceId"] == "302" and data["debugged"] is True
    assert data["outputs"] == [{"name": "SV_Position", "signatureIndex": 0, "type": "Float",
                                "value": [0.25, 0.5, 0.75, 1.0]}]
    artifact = result["artifacts"][0]
    assert artifact["source"]["stage"] == "vertex" and "debug-vertex-e5-v4-i0" in artifact["path"]
    header = json.loads(Path(artifact["path"]).read_text(encoding="utf-8").splitlines()[0])
    assert header["invocation"]["index"] == 14

    controller.debug_misses = True
    missed = call(api, "debug_vertex", vertex=0, event_id=5).result
    assert missed["data"]["debugged"] is False and missed["artifacts"] == []
    assert error_code(api, "debug_vertex", vertex=36, event_id=5) == "INVALID_REQUEST"
    assert error_code(api, "debug_vertex", vertex=0, instance=1, event_id=5) == "INVALID_REQUEST"
    assert error_code(api, "debug_vertex", vertex=0, event_id=8) == "INVALID_EVENT"
    assert error_code(api, "debug_vertex", event_id=5) == "INVALID_REQUEST"


def test_export_rgb_png_drops_alpha(loaded) -> None:
    ctx, api = loaded
    exported = call(api, "export_texture", resource_id="100", channels="rgb").result
    artifact = exported["artifacts"][0]
    path = Path(artifact["path"])
    data = path.read_bytes()
    width, height, depth, color_type = struct.unpack(">IIBB", data[16:26])
    assert (width, height, depth, color_type) == (4, 4, 8, 2)
    assert ctx.controller.saved[-1][0] == FileType.BMP
    assert not list(path.parent.glob("*.bmp"))

    idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
    rows = zlib.decompress(idat)
    assert rows[:7] == b"\x00" + bytes([30, 20, 10, 30, 20, 10])


def test_buffer_data_inline_and_artifact(loaded) -> None:
    _, api = loaded
    small = call(api, "get_buffer_data", resource_id="200", offset=16, size=32,
                 format="float4").result["data"]
    raw = base64.b64decode(small["base64"])
    assert struct.unpack("<8f", raw) == tuple(float(index) for index in range(4, 12))
    assert small["rows"] == [[[4.0, 5.0, 6.0, 7.0]], [[8.0, 9.0, 10.0, 11.0]]]
    assert small["hex"] == raw.hex()

    large = call(api, "get_buffer_data", resource_id="200", offset=0, size=100 * 1024).result
    assert "base64" not in large["data"]
    assert Path(large["artifacts"][0]["path"]).stat().st_size == 100 * 1024

    clamped = call(api, "get_buffer_data", resource_id="200", offset=128 * 1024 - 8, size=64)
    assert clamped.result["data"]["size"] == 8 and clamped.result["warnings"]
    assert error_code(api, "get_buffer_data", resource_id="100") == "INVALID_RESOURCE"
    assert error_code(api, "get_buffer_data", resource_id="200", format="vec4") == \
        "INVALID_REQUEST"


def test_set_event_and_messages(loaded) -> None:
    ctx, api = loaded
    moved = call(api, "set_event", event_id=5)
    assert moved.result["data"]["eventIdBefore"] == 9
    assert moved.result["data"]["eventIdAfter"] == 5
    assert moved.result["data"]["action"]["path"] == ["Frame"]
    assert ctx.current == 5
    assert call(api, "get_status").result["data"]["capture"]["currentEventId"] == 5

    context = call(api, "get_context").result["data"]
    assert set(context["pipeline"]["shaders"]) == {"vertex", "pixel"}
    assert error_code(api, "set_event", event_id=1000) == "INVALID_EVENT"

    messages = call(api, "get_debug_messages").result["data"]
    assert messages["total"] == 1
    assert messages["messages"][0]["description"] == "Bad blend state"


def test_requests_fail_cleanly_without_capture(loaded) -> None:
    ctx, api = loaded
    ctx.close()
    assert error_code(api, "list_actions") == "NO_CAPTURE"
    assert call(api, "get_context").result["data"]["pipeline"] is None


class Context:
    method = "inspect_event"
    params: dict[str, Any] = {}
    deadline_at = float("inf")

    def __init__(self, state: CaptureState) -> None:
        self.state = state.snapshot()

    def check_cancelled(self) -> None:
        pass


def runner_with_capture(event_id: int = 9) -> tuple[ReplayRunner, CaptureState, FakeContext]:
    state = CaptureState()
    state.capture_loaded("x.rdc", "D3D12", current=(event_id, None))
    ctx = FakeContext()
    ctx.controller.current = event_id
    runner = ReplayRunner(state, lambda message: None)
    runner.manager = ctx.manager
    return runner, state, ctx


def test_runner_reports_replay_that_never_ran() -> None:
    runner, state, ctx = runner_with_capture()
    ctx.manager.running = False
    with pytest.raises(AgentBridgeError) as error:
        runner.call(Context(state), lambda controller: 1)
    assert error.value.code == "CAPTURE_CHANGED"


def test_runner_restores_the_latest_ui_event() -> None:
    runner, state, ctx = runner_with_capture(9)
    controller = ctx.controller

    def read(controller_arg: Any) -> int:
        state.current_event_changed(5, None)
        return controller_arg.current

    value, meta = runner.at_event(Context(state), 8, read)
    assert value == 8
    assert meta["eventIdBefore"] == 9 and meta["eventIdAfter"] == 5
    assert controller.frame_events == [8, 5]


class LateNotifyManager(FakeReplayManager):
    """Mimics qrenderdoc notifying viewers only after its queued SetFrameEvent finished."""

    def __init__(self, controller: Any, notify: Any) -> None:
        FakeReplayManager.__init__(self, controller)
        self.notify = notify

    def BlockInvoke(self, callback: Any) -> None:
        FakeReplayManager.BlockInvoke(self, callback)
        if self.notify is not None:
            notify, self.notify = self.notify, None
            notify()


def test_runner_follows_ui_event_notified_after_restore() -> None:
    runner, state, ctx = runner_with_capture(9)
    controller = ctx.controller
    runner.manager = LateNotifyManager(controller, lambda: state.current_event_changed(5, None))

    _, meta = runner.at_event(Context(state), 8, lambda controller_arg: None)

    assert meta["eventIdAfter"] == 9
    assert controller.frame_events == [8, 9, 5]
    assert controller.current == 5


def test_runner_wraps_unexpected_errors() -> None:
    runner, state, ctx = runner_with_capture()

    def explode(controller: Any) -> None:
        raise ValueError("boom")

    with pytest.raises(AgentBridgeError) as error:
        runner.at_event(Context(state), 5, explode)
    assert error.value.code == "REPLAY_ERROR" and "boom" in error.value.message
    assert ctx.controller.current == 9


def test_cursor_round_trip_and_binding() -> None:
    cursor = encode_cursor("list_actions", "a" * 32, "key", 200)
    assert decode_cursor(cursor, "list_actions", "a" * 32, "key") == 200
    for kind, session, key in (("find_actions", "a" * 32, "key"), ("list_actions", "b" * 32, "key"),
                               ("list_actions", "a" * 32, "other")):
        with pytest.raises(AgentBridgeError):
            decode_cursor(cursor, kind, session, key)
    with pytest.raises(AgentBridgeError):
        decode_cursor("not-a-cursor", "list_actions", "a" * 32, "key")

    items = list(range(5))
    page, next_cursor, total = paginate(items, {"limit": 2}, "k", "s" * 32, "q")
    assert page == [0, 1] and total == 5
    page, next_cursor, _ = paginate(items, {"limit": 4, "cursor": next_cursor}, "k", "s" * 32, "q")
    assert page == [2, 3, 4] and next_cursor is None


def test_buffer_format_parser() -> None:
    fields, stride = parse_format("float3, uint ; half2")
    assert stride == 20
    assert [(name, count, offset) for name, _, count, offset in fields] == [
        ("float3", 3, 0), ("uint", 1, 12), ("half2", 2, 16)]
    data = struct.pack("<3fI2e", 1.0, 2.0, 3.0, 9, 0.5, 1.5)
    assert decode_rows(data, fields, stride) == [[[1.0, 2.0, 3.0], [9], [0.5, 1.5]]]
    with pytest.raises(AgentBridgeError):
        parse_format("float5")
