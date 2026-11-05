from __future__ import annotations

import enum
import json
import os
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

import rendertest_agent_bridge as bridge
from rendertest_agent_bridge.paths import instances_root
from rendertest_agent_bridge.runtime import AgentBridgeRuntime, prune_instances
from rendertest_mcp.errors import RenderTestAgentError
from rendertest_mcp.ipc_client import BridgeClient


class GraphicsAPI(enum.IntEnum):
    D3D11 = 0
    D3D12 = 1
    Vulkan = 3


class FakeAction:
    def __init__(self, event_id: int, name: str) -> None:
        self.eventId = event_id
        self.actionId = event_id // 10
        self.flags = 2
        self._name = name

    def GetName(self, structured_file: Any) -> str:
        assert structured_file == "sdfile"
        return self._name


class FakeContext:
    def __init__(self) -> None:
        self.viewers: list[Any] = []
        self.loaded = False
        self.selected = 0
        self.current = 0
        self.fail_add_viewer = False
        self.actions = {10: FakeAction(10, "DrawIndexed(36)"), 20: FakeAction(20, "Dispatch(8, 8, 1)")}

    def AddCaptureViewer(self, viewer: Any) -> None:
        if self.fail_add_viewer:
            raise RuntimeError("viewer rejected")
        self.viewers.append(viewer)
        if self.loaded:
            viewer.OnCaptureLoaded()
            viewer.OnSelectedEventChanged(self.selected)
            viewer.OnEventChanged(self.current)

    def RemoveCaptureViewer(self, viewer: Any) -> None:
        self.viewers.remove(viewer)

    def GetCaptureFilename(self) -> str:
        return "E:/Captures/sample.rdc"

    def APIProps(self) -> Any:
        return types.SimpleNamespace(pipelineType=GraphicsAPI.D3D12)

    def GetAction(self, event_id: int) -> FakeAction | None:
        return self.actions.get(event_id)

    def GetStructuredFile(self) -> str:
        return "sdfile"

    def CurSelectedEvent(self) -> int:
        return self.selected

    def CurEvent(self) -> int:
        return self.current

    def load(self, event_id: int = 10) -> None:
        # mirrors CaptureContext::LoadCapture: viewers are excluded from the initial event change
        self.loaded = True
        self.selected = self.current = event_id
        for viewer in list(self.viewers):
            viewer.OnCaptureLoaded()

    def select(self, event_id: int) -> None:
        self.selected = self.current = event_id
        for viewer in list(self.viewers):
            viewer.OnSelectedEventChanged(event_id)
            viewer.OnEventChanged(event_id)

    def close(self) -> None:
        self.loaded = False
        for viewer in list(self.viewers):
            viewer.OnCaptureClosed()


@pytest.fixture
def fake_qrenderdoc(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("qrenderdoc")

    class CaptureViewer:
        def __init__(self) -> None:
            pass

    module.CaptureViewer = CaptureViewer
    monkeypatch.setitem(sys.modules, "qrenderdoc", module)


@pytest.fixture
def agent_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RENDERTEST_AGENT_ROOT", str(tmp_path))
    yield tmp_path
    bridge.stop()


def client(root: Path) -> BridgeClient:
    return BridgeClient(root=root, environ={}, poll_interval=0.005, default_timeout=5.0)


def manifest(root: Path) -> dict[str, Any]:
    instance_dir = Path(instances_root(str(root))) / bridge.instance_id()
    return json.loads((instance_dir / "instance.json").read_text(encoding="utf-8"))


def test_start_without_capture_serves_status(fake_qrenderdoc: None, agent_root: Path) -> None:
    ctx = FakeContext()
    bridge.start("1.47", ctx)

    assert len(ctx.viewers) == 1
    data = manifest(agent_root)
    assert data["captureLoaded"] is False
    assert data["renderdocVersion"] == "1.47"
    assert data["methods"] == ["get_context", "get_status", "ping"]

    api = client(agent_root)
    assert api.call("ping").result["data"]["pong"] is True
    status = api.call("get_status").result["data"]
    assert status["bridge"]["instanceId"] == bridge.instance_id()
    assert status["capture"] is None

    with pytest.raises(RenderTestAgentError) as error:
        api.call("list_actions", {})
    assert error.value.code == "UNKNOWN_METHOD"


def test_capture_lifecycle_updates_manifest_and_session(fake_qrenderdoc: None, agent_root: Path) -> None:
    ctx = FakeContext()
    bridge.start("1.47", ctx)
    api = client(agent_root)

    ctx.load(10)
    data = manifest(agent_root)
    assert data["captureLoaded"] is True
    assert data["api"] == "D3D12"
    assert data["currentEventId"] == 10
    assert data["selectedEventId"] == 10
    first_session = data["captureSessionId"]
    assert len(first_session) == 32

    status = api.call("get_status").result["data"]["capture"]
    assert status["captureName"] == "sample.rdc"
    assert status["captureSessionId"] == first_session
    assert api.call("get_context").result["data"]["action"]["name"] == "DrawIndexed(36)"

    ctx.select(20)
    context = api.call("get_context").result["data"]
    assert context["currentEventId"] == 20
    assert context["action"]["name"] == "Dispatch(8, 8, 1)"

    ctx.close()
    assert manifest(agent_root)["captureLoaded"] is False
    assert api.call("get_context").result["data"]["captureLoaded"] is False

    ctx.load(10)
    assert manifest(agent_root)["captureSessionId"] not in ("", first_session)


def test_viewer_registered_after_load_receives_current_state(fake_qrenderdoc: None, agent_root: Path) -> None:
    ctx = FakeContext()
    ctx.loaded = True
    ctx.selected = ctx.current = 20
    bridge.start("1.47", ctx)

    data = manifest(agent_root)
    assert data["captureLoaded"] is True
    assert data["selectedEventId"] == 20


def test_stop_marks_instance_stopped_and_removes_viewer(fake_qrenderdoc: None, agent_root: Path) -> None:
    ctx = FakeContext()
    bridge.start("1.47", ctx)
    instance_id = bridge.instance_id()

    bridge.stop()

    assert ctx.viewers == []
    assert bridge.instance_id() == ""
    data = json.loads((Path(instances_root(str(agent_root))) / instance_id / "instance.json").read_text())
    assert data["stopped"] is True
    assert client(agent_root).list_instances() == []


def test_failed_start_rolls_back(fake_qrenderdoc: None, agent_root: Path) -> None:
    ctx = FakeContext()
    ctx.fail_add_viewer = True

    with pytest.raises(RuntimeError):
        bridge.start("1.47", ctx)

    assert bridge.instance_id() == ""
    manifests = list(Path(instances_root(str(agent_root))).glob("*/instance.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["stopped"] is True


def test_runtime_log_is_written(tmp_path: Path) -> None:
    runtime = AgentBridgeRuntime(renderdoc_version="1.47", root=str(tmp_path), instance_id="1-abc")
    runtime.start()
    assert runtime.stop() is True

    log = (tmp_path / "logs" / "bridge-1-abc.log").read_text(encoding="utf-8")
    assert "started" in log and "stopped" in log


def test_prune_removes_only_stale_instances(tmp_path: Path) -> None:
    base = Path(instances_root(str(tmp_path)))
    now = time.time()

    def make(name: str, heartbeat: float, stopped: bool) -> None:
        directory = base / name
        directory.mkdir(parents=True)
        (directory / "instance.json").write_text(
            json.dumps({"heartbeatUnixMs": int(heartbeat * 1000), "stopped": stopped})
        )

    make("old-stopped", now - 2 * 3600, True)
    make("recent-stopped", now - 60, True)
    make("silent", now - 8 * 24 * 3600, False)
    make("alive", now, False)
    make("self", now - 8 * 24 * 3600, True)

    removed = prune_instances(str(tmp_path), "self", now=now)

    assert sorted(removed) == ["old-stopped", "silent"]
    assert sorted(os.listdir(base)) == ["alive", "recent-stopped", "self"]
