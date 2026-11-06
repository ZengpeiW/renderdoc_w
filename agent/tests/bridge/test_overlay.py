from __future__ import annotations

import ast
import enum
import hashlib
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from fake_renderdoc import (FakeController, FakePipe, FakeReplayManager, ResourceId, descriptor,
                           module)
from rendertest_agent_bridge.indexes import SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError, RequestContext
from rendertest_agent_bridge.methods import overlay
from rendertest_agent_bridge.replay import ReplayRunner


class Output:
    def __init__(self, controller, width, height):
        self.controller = controller
        self.width, self.height = width, height
        self.closed = False
        self.display = None

    def _phase(self, name):
        self.controller.calls.append(name)
        if self.controller.fail == name:
            raise RuntimeError("simulated " + name)

    def SetTextureDisplay(self, display):
        self._phase("configure")
        self.display = display

    def GetDimensions(self):
        return self.controller.dimensions or (self.width, self.height)

    def Display(self):
        self._phase("display")
        self.controller.overlay_owner = "temporary"
        if self.controller.during_display:
            self.controller.during_display()

    def GetDebugOverlayTexID(self):
        self._phase("overlay-id")
        return ResourceId(self.controller.overlay_id)

    def ReadbackOutputTexture(self):
        self._phase("readback")
        # Distinct rows/channels catch RGB order and upside-down PNG mistakes.
        if self.controller.pixels is not None:
            return self.controller.pixels
        return bytes([20, 40, 60]) * (self.width * self.height)

    def Shutdown(self):
        self.closed = True
        self._phase("shutdown")


class Controller(FakeController):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.outputs = []
        self.pipe = FakePipe(self)
        self.targets = [descriptor(100)]
        self.depth = descriptor(101)
        self.pipe.GetOutputTargets = lambda: self.targets
        self.pipe.GetDepthTarget = lambda: self.depth
        self.fail = None
        self.overlay_id = 700
        self.overlay_owner = "gui"
        self.during_display = None
        self.dimensions = None
        self.pixels = None
        self.fatal = 0

    def GetPipelineState(self):
        return self.pipe

    def GetFatalErrorStatus(self):
        return NS(code=self.fatal)

    def SetFrameEvent(self, event_id, force):
        self.calls.append(("event", event_id, force))
        super().SetFrameEvent(event_id, force)
        if force:
            assert all(item.closed for item in self.outputs)
            self.overlay_owner = "gui"

    def CreateOutput(self, window, kind):
        assert kind == "texture"
        self.calls.append("create")
        if self.fail == "create":
            raise RuntimeError("simulated create")
        if self.fail == "null-output":
            return None
        output = Output(self, *window)
        self.outputs.append(output)
        return output


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.controller = Controller()
        self.session = "a" * 32
        self.current = 9
        self.server = NS(_stop_event=threading.Event())
        self.runtime = NS(cache=SessionCache(), root=str(tmp_path), instance_id="overlay-test",
                          replay=ReplayRunner(self, lambda _line: None))
        self.runtime.replay.manager = FakeReplayManager(self.controller)
        self.rd = module()
        self.rd.DebugOverlay = enum.IntEnum("DebugOverlay", {name: i for i, name in
                                                             enumerate(overlay.OVERLAYS, 1)})
        self.rd.ReplayOutputType = NS(Texture="texture")
        self.rd.CreateHeadlessWindowingData = lambda width, height: (width, height)
        self.rd.TextureDisplay = NS
        self.rd.FloatVector = lambda *args: args
        monkeypatch.setitem(sys.modules, "renderdoc", self.rd)

    def snapshot(self):
        return {"captureSessionId": self.session, "currentEventId": self.current}

    def current_event_id(self):
        return self.current

    def context(self, params):
        return RequestContext(self.server, "b" * 32, "export_overlay", params, "read",
                              time.time() + 30, self.snapshot())

    def call(self, **params):
        arguments = {"resource_id": "100", "overlay": "Drawcall", "event_id": 5}
        arguments.update(params)
        return overlay.export_overlay(self.runtime, self.context(arguments))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def assert_error(harness, code, **params):
    with pytest.raises(AgentBridgeError) as error:
        harness.call(**params)
    assert error.value.code == code
    assert harness.controller.current == harness.current
    assert all(item.closed for item in harness.controller.outputs)
    assert not list(Path(harness.runtime.root).rglob("*.png"))
    return error.value


def png_pixels(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks = {}
    cursor = 8
    while cursor < len(data):
        size = struct.unpack_from(">I", data, cursor)[0]
        kind = data[cursor + 4:cursor + 8]
        body = data[cursor + 8:cursor + 8 + size]
        crc = struct.unpack_from(">I", data, cursor + 8 + size)[0]
        assert crc == zlib.crc32(kind + body) & 0xffffffff
        chunks[kind] = body
        cursor += size + 12
    width, height, bits, kind, compression, filter_type, interlace = struct.unpack(
        ">IIBBBBB", chunks[b"IHDR"])
    assert (bits, kind, compression, filter_type, interlace) == (8, 2, 0, 0, 0)
    decoded = zlib.decompress(chunks[b"IDAT"])
    stride = width * 3 + 1
    assert all(decoded[row * stride] == 0 for row in range(height))
    return width, height, b"".join(decoded[row * stride + 1:(row + 1) * stride]
                                 for row in range(height))


def test_composited_png_artifact_is_rgb_and_restores_replay_and_gui_overlay(harness):
    harness.controller.pixels = bytes(range(48))
    response = harness.call()
    payload, meta = response.result, response.meta
    data, artifact = payload["data"], payload["artifacts"][0]
    path = Path(artifact["path"])
    raw = path.read_bytes()
    assert png_pixels(raw) == (4, 4, bytes(range(48)))
    assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()
    assert artifact["bytes"] == len(raw)
    assert artifact["mediaType"] == "image/png" and artifact["kind"] == "overlay"
    assert path.is_relative_to(Path(harness.runtime.root) / "artifacts" / "overlay-test" /
                               harness.session)
    assert artifact["source"] == {"resource_id": "100", "event_id": 5, "overlay": "Drawcall",
                                  "mip": 0, "slice": 0, "sample": 0, "type_cast": "Typeless"}
    assert data["path"] == str(path) and data["attachment"]["depthTarget"] == "101"
    assert meta["replayEventRestored"] and meta["eventIdAfter"] == 9
    assert harness.controller.current == 9 and harness.controller.overlay_owner == "gui"
    calls = harness.controller.calls
    assert calls.index("shutdown") < calls.index(("event", 5, True)) < calls.index(("event", 9, False))
    assert not harness.controller.saved  # SaveTexture cannot export the composited heatmap.
    assert "not original texture" in payload["warnings"][-1]


@pytest.mark.parametrize("name", overlay.OVERLAYS)
def test_each_supported_overlay_uses_the_native_enum(harness, name):
    response = harness.call(overlay=name)
    assert harness.controller.outputs[0].display.overlay == getattr(harness.rd.DebugOverlay, name)
    assert response.result["data"]["legend"]
    assert all(item.closed for item in harness.controller.outputs)


def test_same_ui_eid_is_force_restored_and_default_event_is_resolved(harness):
    harness.current = harness.controller.current = 5
    harness.call(event_id=None)
    assert ("event", 5, True) in harness.controller.calls
    assert harness.controller.overlay_owner == "gui"


def test_mip_scale_is_based_on_full_texture_dimensions_and_cast_is_forwarded(harness):
    harness.controller.targets[0].firstMip = 1
    response = harness.call(mip=1, type_cast="UInt", range_min=2, range_max=12, max_dimension=1)
    data = response.result["data"]
    display = harness.controller.outputs[0].display
    assert (data["width"], data["height"], data["sourceWidth"], data["sourceHeight"]) == (1, 1, 2, 2)
    assert display.scale == 0.25 and display.subresource.mip == 1
    assert display.typeCast == harness.rd.CompType.UInt
    assert display.rangeMin == 2 and display.rangeMax == 12
    assert data["displayTransform"]["sourcePixelScaleX"] == 0.5
    assert display.linearDisplayAsGamma and not display.alpha and not display.flipY
    assert display.backgroundColor == (0, 0, 0, 1)


def test_array_slice_and_msaa_sample_are_preserved_for_supported_draw_overlays(harness):
    texture = harness.controller.textures[0]
    texture.arraysize, texture.msSamp = 4, 4
    target = harness.controller.targets[0]
    target.firstSlice, target.numSlices = 1, 2
    response = harness.call(slice=2, sample=3)
    display = harness.controller.outputs[0].display
    assert (display.subresource.slice, display.subresource.sample) == (2, 3)
    assert response.result["data"]["attachment"]["firstSlice"] == 1


@pytest.mark.parametrize("name", ["NaN", "Clipping"])
def test_display_only_overlays_allow_non_output_texture_and_non_draw_event(harness, name):
    harness.controller.targets = []
    harness.controller.depth = descriptor(0)
    harness.controller.overlay_id = 0
    response = harness.call(overlay=name, event_id=8, mip=1)
    assert response.result["data"]["attachment"] is None
    assert "overlay-id" not in harness.controller.calls


@pytest.mark.parametrize("name", ["NaN", "Clipping"])
def test_display_only_current_output_checks_full_bound_depth_budget_before_creation(harness, name):
    depth = harness.controller.textures[1]
    depth.width = depth.height = 2048
    depth.arraysize = 5
    error = assert_error(harness, "UNSUPPORTED", overlay=name, max_dimension=1)
    assert "stored-texel" in error.message
    assert "create" not in harness.controller.calls


@pytest.mark.parametrize("name", ["NaN", "Clipping"])
def test_display_only_non_output_texture_ignores_unrelated_oversized_depth(harness, name):
    depth = harness.controller.textures[1]
    depth.width = depth.height = 2048
    depth.arraysize = 5
    harness.controller.targets = []
    harness.controller.overlay_id = 0
    response = harness.call(overlay=name, event_id=8, mip=1)
    assert response.result["data"]["attachment"] is None
    assert harness.controller.outputs[0].closed


@pytest.mark.parametrize("name", ["NaN", "Clipping"])
def test_display_only_current_output_does_not_require_draw_or_matching_bound_mip(harness, name):
    response = harness.call(overlay=name, event_id=8, mip=1)
    assert response.result["data"]["attachment"] is None
    assert harness.controller.outputs[0].display.subresource.mip == 1


@pytest.mark.parametrize("params", [
    {"overlay": "ClearBeforeDraw"}, {"overlay": "ClearBeforePass"},
    {"overlay": "QuadOverdrawPass"}, {"overlay": "TriangleSizePass"},
    {"overlay": "NoOverlay"}, {"overlay": None}, {"max_dimension": 0},
    {"max_dimension": 4097}, {"max_dimension": True}, {"range_min": float("nan")},
    {"range_max": float("inf")}, {"range_min": True}, {"range_max": "1"},
    {"range_max": 1e40}, {"range_max": 10 ** 1000}, {"range_min": 1, "range_max": 1},
    {"range_min": 1, "range_max": 1 + 1e-9}, {"type_cast": "unsupported"},
    {"sample": 0xffffffff}, {"mip": 99}, {"slice": 1},
])
def test_invalid_parameters_never_create_an_output(harness, params):
    assert_error(harness, "INVALID_REQUEST", **params)
    assert not harness.controller.outputs


@pytest.mark.parametrize("event_id", [1, 3, 8, 9, 999])
def test_draw_overlay_requires_exact_leaf_draw_event(harness, event_id):
    assert_error(harness, "INVALID_EVENT", event_id=event_id)
    assert not harness.controller.outputs


def test_wrong_attachment_or_wrong_bound_subresource_is_rejected(harness):
    assert_error(harness, "INVALID_REQUEST", mip=1)
    harness.controller.targets = []
    assert_error(harness, "INVALID_REQUEST")
    assert not harness.controller.outputs


def test_ambiguous_duplicate_output_views_are_not_silently_selected(harness):
    harness.controller.targets.append(descriptor(100))
    assert_error(harness, "UNSUPPORTED")
    assert not harness.controller.outputs


@pytest.mark.parametrize("name", ["Depth", "Stencil"])
def test_depth_and_stencil_require_depth_attachment(harness, name):
    harness.controller.depth = descriptor(0)
    assert_error(harness, "UNSUPPORTED", overlay=name)


def test_disabled_tests_produce_explicit_caveats(harness):
    harness.controller.pipe.GetDepthTestState = lambda: NS(depthEnable=False)
    assert "Depth testing is disabled" in harness.call(overlay="Depth").result["warnings"][0]
    assert "Stencil testing is disabled" in harness.call(overlay="Stencil").result["warnings"][0]


def test_quad_overdraw_rejects_msaa_and_vulkan_multisample_rasterization(harness):
    harness.controller.textures[0].msSamp = 4
    assert_error(harness, "UNSUPPORTED", overlay="QuadOverdrawDraw")
    harness.controller.textures[0].msSamp = 1
    harness.controller.GetAPIProperties = lambda: NS(pipelineType=NS(name="Vulkan"))
    harness.controller.GetVulkanPipelineState = lambda: NS(multisample=NS(rasterSamples=4))
    assert_error(harness, "UNSUPPORTED", overlay="QuadOverdrawDraw")


def test_triangle_size_rejects_non_triangular_topology(harness):
    harness.controller.pipe.GetPrimitiveTopology = lambda: NS(name="LineList")
    assert_error(harness, "UNSUPPORTED", overlay="TriangleSizeDraw")


@pytest.mark.parametrize("phase", ["create", "configure", "display", "readback", "shutdown"])
def test_native_failures_always_release_output_and_restore_event(harness, phase):
    harness.controller.fail = phase
    assert_error(harness, "REPLAY_ERROR")
    assert ("event", 5, True) in harness.controller.calls
    assert harness.controller.overlay_owner == "gui"


@pytest.mark.parametrize("failure", ["null-output", "null-overlay", "wrong-size", "short-pixels"])
def test_no_fake_success_when_output_or_overlay_is_unavailable(harness, failure):
    code = "UNSUPPORTED"
    if failure == "null-output":
        harness.controller.fail = failure
    elif failure == "null-overlay":
        harness.controller.overlay_id = 0
    elif failure == "wrong-size":
        harness.controller.dimensions = (8, 8)
    else:
        harness.controller.pixels = b"\x00"
        code = "REPLAY_ERROR"
    assert_error(harness, code)
    assert ("event", 5, True) in harness.controller.calls


@pytest.mark.parametrize("name", ["CreateHeadlessWindowingData", "TextureDisplay", "DebugOverlay"])
def test_missing_native_capability_is_explicit(harness, name):
    delattr(harness.rd, name)
    assert_error(harness, "UNSUPPORTED")
    assert not harness.controller.outputs


def test_cancellation_during_native_display_still_restores_and_writes_no_artifact(harness):
    harness.controller.during_display = harness.server._stop_event.set
    assert_error(harness, "CANCELLED")
    assert ("event", 5, True) in harness.controller.calls


def test_fatal_native_error_does_not_return_an_image(harness):
    harness.controller.during_display = lambda: setattr(harness.controller, "fatal", 1)
    assert_error(harness, "REPLAY_ERROR")


@pytest.mark.parametrize("change", [
    {"dimension": 3}, {"width": 20000}, {"mips": 100}, {"msSamp": 128},
    {"width": 4096, "height": 4096, "mips": 2},
    {"width": 2048, "height": 2048, "arraysize": 5, "mips": 1},
])
def test_source_budget_includes_all_storage_not_only_export_size(harness, change):
    for key, value in change.items():
        setattr(harness.controller.textures[0], key, value)
    assert_error(harness, "UNSUPPORTED", max_dimension=1)
    assert not harness.controller.outputs


def test_bound_depth_allocation_is_also_bounded(harness):
    harness.controller.textures[1].width = 20000
    assert_error(harness, "UNSUPPORTED")
    assert not harness.controller.outputs


def test_source_exact_limit_is_accepted_and_output_has_independent_pixel_budget(harness):
    texture = harness.controller.textures[0]
    texture.width = texture.height = 4096
    texture.mips = 1
    data = harness.call(max_dimension=4096).result["data"]
    assert data["width"] == data["height"] == 1448
    assert data["width"] * data["height"] <= overlay.MAX_OUTPUT_PIXELS
    assert data["sourceWidth"] == 4096 and data["displayTransform"]["scale"] == 1448 / 4096


@pytest.mark.parametrize("phase", ["encode", "describe"])
def test_artifact_failure_removes_partial_png_after_native_cleanup(harness, monkeypatch, phase):
    def fail(*args, **kwargs):
        assert all(item.closed for item in harness.controller.outputs)
        raise OSError("simulated artifact failure")
    monkeypatch.setattr(overlay, "_png" if phase == "encode" else "describe", fail)
    with pytest.raises(AgentBridgeError) as error:
        harness.call()
    assert error.value.code == "REPLAY_ERROR"
    assert not list(Path(harness.runtime.root).rglob("*.png"))
    assert harness.controller.current == 9


def test_overlay_implementation_remains_python36_compatible():
    path = Path(overlay.__file__)
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 6))
