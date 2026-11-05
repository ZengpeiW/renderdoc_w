from __future__ import annotations

import argparse
import copy
import os
import struct
import sys
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parents[1]
BRIDGE_DIR = AGENT_DIR / "bridge"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

from rendertest_agent_bridge.artifacts import artifact_directory, describe_artifact  # noqa: E402
from rendertest_agent_bridge.constants import P0_METHODS, P1_METHODS  # noqa: E402
from rendertest_agent_bridge.ipc import AgentBridgeError, BridgeResult, MailboxServer  # noqa: E402
from rendertest_agent_bridge.paths import agent_root  # noqa: E402


def _png_bytes(width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(pixels[y * width + x])

    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(
        b"IDAT", zlib.compress(bytes(rows))
    ) + chunk(b"IEND", b"")


def _payload(
    data: Any,
    *,
    artifacts: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    next_cursor: str | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "data": data,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "nextCursor": next_cursor,
        "truncated": truncated,
    }


class FakeRenderTestBridge:
    def __init__(
        self,
        root: Path | str,
        *,
        instance_id: str | None = None,
        capture_loaded: bool = True,
        poll_interval: float = 0.005,
    ) -> None:
        self.root = Path(root).resolve()
        self.instance_id = instance_id or f"fake-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval
        self.capture_loaded = capture_loaded
        self.capture_path = "fake://rendertest/sample.rdc"
        self.capture_session_id = uuid.uuid4().hex if capture_loaded else ""
        self.current_event_id = 10 if capture_loaded else None
        self.selected_event_id = 10 if capture_loaded else None
        self._actions = self._build_actions()
        self._resources = self._build_resources()
        self._usage = self._build_usage()
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self.server = MailboxServer(
            self.root,
            self.instance_id,
            self.handle,
            self.state,
            methods=("ping",) + P0_METHODS + P1_METHODS,
            renderdoc_version="1.47-fake",
        )

    def start(self, *, poll: bool = True) -> FakeRenderTestBridge:
        self.server.start(start_worker=True)
        if poll:
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_main, name="FakeRenderTestBridgePoller", daemon=True
            )
            self._poll_thread.start()
        return self

    def stop(self) -> None:
        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(2.0)
            self._poll_thread = None
        self.server.stop()

    def __enter__(self) -> FakeRenderTestBridge:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _poll_main(self) -> None:
        while not self._poll_stop.is_set():
            self.server.poll_once()
            time.sleep(self.poll_interval)

    def state(self) -> dict[str, Any]:
        return {
            "captureLoaded": self.capture_loaded,
            "capturePath": self.capture_path if self.capture_loaded else "",
            "captureSessionId": self.capture_session_id,
            "api": "D3D12" if self.capture_loaded else "",
            "selectedEventId": self.selected_event_id,
            "currentEventId": self.current_event_id,
        }

    def open_capture(self) -> None:
        self.capture_loaded = True
        self.capture_session_id = uuid.uuid4().hex
        self.current_event_id = 10
        self.selected_event_id = 10
        self.server.notify_activity()

    def close_capture(self) -> None:
        self.capture_loaded = False
        self.capture_session_id = ""
        self.current_event_id = None
        self.selected_event_id = None
        self.server.notify_activity()

    def handle(self, context: Any) -> Any:
        method = getattr(self, f"_method_{context.method}", None)
        if method is None:
            raise AgentBridgeError("UNKNOWN_METHOD", f"Fake bridge does not implement {context.method}.")
        return method(context.params)

    @staticmethod
    def _build_actions() -> dict[int, dict[str, Any]]:
        return {
            1: {
                "eventId": 1,
                "name": "Frame",
                "flags": ["marker"],
                "parentEventId": None,
                "children": [10, 20, 30],
            },
            10: {
                "eventId": 10,
                "name": "GBuffer Draw",
                "flags": ["draw"],
                "parentEventId": 1,
                "children": [],
                "numIndices": 36,
                "numInstances": 1,
                "shaders": {"vertex": "3001", "pixel": "3002"},
                "inputs": ["2001"],
                "outputs": ["1001"],
                "depth": "1002",
            },
            20: {
                "eventId": 20,
                "name": "Lighting CS",
                "flags": ["dispatch"],
                "parentEventId": 1,
                "children": [],
                "dispatchDimensions": [1, 1, 1],
                "shaders": {"compute": "3003"},
                "inputs": ["1001", "1002"],
                "outputs": ["1001"],
            },
            30: {
                "eventId": 30,
                "name": "Tonemap",
                "flags": ["draw"],
                "parentEventId": 1,
                "children": [],
                "numIndices": 3,
                "numInstances": 1,
                "shaders": {"vertex": "3001", "pixel": "3004"},
                "inputs": ["1001"],
                "outputs": ["1003"],
            },
        }

    @staticmethod
    def _build_resources() -> dict[str, dict[str, Any]]:
        def texture(resource_id: str, name: str, fmt: str) -> dict[str, Any]:
            return {
                "resourceId": resource_id,
                "name": name,
                "kind": "texture",
                "format": fmt,
                "width": 4,
                "height": 4,
                "depth": 1,
                "mips": 1,
                "arraySize": 1,
                "samples": 1,
            }

        return {
            "1001": texture("1001", "SceneColor", "R8G8B8A8_UNORM"),
            "1002": texture("1002", "SceneDepth", "D32_FLOAT"),
            "1003": texture("1003", "Backbuffer", "R8G8B8A8_UNORM"),
            "2001": {
                "resourceId": "2001",
                "name": "ObjectConstants",
                "kind": "buffer",
                "byteSize": 256,
            },
            "3001": {"resourceId": "3001", "name": "FullscreenVS", "kind": "shader", "stage": "vertex"},
            "3002": {"resourceId": "3002", "name": "GBufferPS", "kind": "shader", "stage": "pixel"},
            "3003": {"resourceId": "3003", "name": "LightingCS", "kind": "shader", "stage": "compute"},
            "3004": {"resourceId": "3004", "name": "TonemapPS", "kind": "shader", "stage": "pixel"},
        }

    @staticmethod
    def _build_usage() -> dict[str, list[dict[str, Any]]]:
        return {
            "1001": [
                {"eventId": 10, "usage": "ColorTarget", "access": "write"},
                {"eventId": 20, "usage": "ReadWriteResource", "access": "read-write"},
                {"eventId": 30, "usage": "PixelShaderResource", "access": "read"},
            ],
            "1002": [
                {"eventId": 10, "usage": "DepthStencilTarget", "access": "write"},
                {"eventId": 20, "usage": "ComputeShaderResource", "access": "read"},
            ],
            "1003": [{"eventId": 30, "usage": "ColorTarget", "access": "write"}],
            "2001": [{"eventId": 10, "usage": "VertexConstantBuffer", "access": "read"}],
        }

    def _event_id(self, params: dict[str, Any], *, required: bool = False) -> int:
        value = params.get("event_id")
        if value is None and not required:
            value = self.current_event_id
        if type(value) is not int or value not in self._actions:
            raise AgentBridgeError("INVALID_EVENT", "The event does not exist.", details={"event_id": value})
        return value

    def _resource(self, params: dict[str, Any], kind: str | None = None) -> dict[str, Any]:
        resource_id = params.get("resource_id")
        resource = self._resources.get(resource_id) if isinstance(resource_id, str) else None
        if resource is None or (kind and resource["kind"] != kind):
            raise AgentBridgeError(
                "INVALID_RESOURCE",
                "The resource does not exist or has the wrong type.",
                details={"resource_id": resource_id, "kind": kind},
            )
        return resource

    @staticmethod
    def _page(items: list[Any], params: dict[str, Any]) -> tuple[list[Any], str | None]:
        cursor = params.get("cursor") or "0"
        if not isinstance(cursor, str) or not cursor.isdigit():
            raise AgentBridgeError("INVALID_REQUEST", "The cursor is invalid.")
        offset = int(cursor)
        limit = params.get("limit", 200)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise AgentBridgeError("INVALID_REQUEST", "The limit is invalid.")
        page = items[offset : offset + limit]
        next_offset = offset + len(page)
        return page, str(next_offset) if next_offset < len(items) else None

    def _read_meta(self, event_id: int | None = None) -> dict[str, Any]:
        return {
            "eventIdBefore": self.current_event_id,
            "eventIdAfter": self.current_event_id,
            "replayEventRestored": event_id is not None,
        }

    def _action_summary(self, event_id: int) -> dict[str, Any]:
        action = copy.deepcopy(self._actions[event_id])
        action["childCount"] = len(action.pop("children"))
        return action

    def _pipeline(self, event_id: int, stages: list[str] | None = None) -> dict[str, Any]:
        action = self._actions[event_id]
        shaders = {
            stage: {"resourceId": resource_id, "name": self._resources[resource_id]["name"]}
            for stage, resource_id in action.get("shaders", {}).items()
            if stages is None or stage in stages
        }
        return {
            "eventId": event_id,
            "type": "compute" if "compute" in action.get("shaders", {}) else "graphics",
            "shaders": shaders,
            "renderTargets": action.get("outputs", []),
            "depthTarget": action.get("depth"),
            "viewport": {"x": 0, "y": 0, "width": 4, "height": 4, "minDepth": 0, "maxDepth": 1},
        }

    def _method_ping(self, _: dict[str, Any]) -> dict[str, Any]:
        return _payload({"pong": True})

    def _method_get_status(self, _: dict[str, Any]) -> dict[str, Any]:
        return _payload(
            {
                "bridge": {
                    "instanceId": self.instance_id,
                    "version": self.server.bridge_version,
                    "methods": list(self.server.methods),
                },
                "capture": self.state() if self.capture_loaded else None,
            }
        )

    def _method_get_context(self, _: dict[str, Any]) -> dict[str, Any]:
        if not self.capture_loaded:
            return _payload({"captureLoaded": False, "action": None, "pipeline": None})
        event_id = self.current_event_id
        assert event_id is not None
        return _payload(
            {
                "captureLoaded": True,
                "currentEventId": self.current_event_id,
                "selectedEventId": self.selected_event_id,
                "action": self._action_summary(event_id),
                "pipeline": self._pipeline(event_id),
            }
        )

    def _method_list_actions(self, params: dict[str, Any]) -> BridgeResult:
        parent = params.get("parent_event_id")
        ids = [1] if parent is None else self._actions[self._event_id(params | {"event_id": parent})]["children"]
        if params.get("include_markers") is False:
            ids = [event_id for event_id in ids if "marker" not in self._actions[event_id]["flags"]]
        items, next_cursor = self._page([self._action_summary(event_id) for event_id in ids], params)
        return BridgeResult(_payload({"actions": items}, next_cursor=next_cursor), self._read_meta())

    def _method_find_actions(self, params: dict[str, Any]) -> BridgeResult:
        query = str(params.get("query", "")).casefold()
        flags = set(params.get("flags") or [])
        matches = [
            self._action_summary(event_id)
            for event_id, action in sorted(self._actions.items())
            if query in action["name"].casefold() and (not flags or flags.intersection(action["flags"]))
        ]
        items, next_cursor = self._page(matches, params)
        return BridgeResult(_payload({"actions": items}, next_cursor=next_cursor), self._read_meta())

    def _method_inspect_event(self, params: dict[str, Any]) -> BridgeResult:
        event_id = self._event_id(params)
        action = self._actions[event_id]
        return BridgeResult(
            _payload(
                {
                    "action": self._action_summary(event_id),
                    "pipeline": self._pipeline(event_id),
                    "inputs": [self._resources[item] for item in action.get("inputs", [])],
                    "outputs": [self._resources[item] for item in action.get("outputs", [])],
                }
            ),
            self._read_meta(params.get("event_id")),
        )

    def _method_set_event(self, params: dict[str, Any]) -> BridgeResult:
        event_id = self._event_id(params, required=True)
        before = self.current_event_id
        self.current_event_id = event_id
        self.selected_event_id = event_id
        self.server.notify_activity()
        return BridgeResult(
            _payload({"eventIdBefore": before, "eventIdAfter": event_id}),
            {"eventIdBefore": before, "eventIdAfter": event_id, "replayEventRestored": False},
        )

    def _method_get_pipeline_state(self, params: dict[str, Any]) -> BridgeResult:
        event_id = self._event_id(params)
        return BridgeResult(
            _payload(self._pipeline(event_id, params.get("stages"))),
            self._read_meta(params.get("event_id")),
        )

    def _shader_id(self, params: dict[str, Any]) -> tuple[int, str]:
        event_id = self._event_id(params)
        stage = params.get("stage")
        shader_id = self._actions[event_id].get("shaders", {}).get(stage)
        if shader_id is None:
            raise AgentBridgeError(
                "INVALID_REQUEST",
                "The requested stage is not bound at this event.",
                details={"event_id": event_id, "stage": stage},
            )
        return event_id, shader_id

    def _method_get_shader(self, params: dict[str, Any]) -> BridgeResult:
        event_id, shader_id = self._shader_id(params)
        data: dict[str, Any] = {
            "eventId": event_id,
            "stage": params["stage"],
            "shader": self._resources[shader_id],
            "entryPoint": "main",
            "reflection": {
                "constantBlocks": [{"index": 0, "name": "MaterialConstants", "byteSize": 32}],
                "readOnlyResources": [{"name": "SceneColor", "bindPoint": 0}],
            },
        }
        if params.get("include_disassembly"):
            data["disassembly"] = {"target": params.get("target") or "DXIL", "text": "ret\n"}
        return BridgeResult(_payload(data), self._read_meta(params.get("event_id")))

    def _method_get_cbuffer(self, params: dict[str, Any]) -> BridgeResult:
        event_id, _ = self._shader_id(params)
        index = params.get("index")
        name = params.get("name")
        if index not in (None, 0) or name not in (None, "MaterialConstants"):
            raise AgentBridgeError("INVALID_REQUEST", "The constant buffer does not exist.")
        return BridgeResult(
            _payload(
                {
                    "eventId": event_id,
                    "stage": params["stage"],
                    "index": 0,
                    "name": "MaterialConstants",
                    "variables": [
                        {"name": "Tint", "offset": 0, "type": "float4", "value": [1.0, 0.5, 0.25, 1.0]},
                        {"name": "Exposure", "offset": 16, "type": "float", "value": 1.0},
                    ],
                }
            ),
            self._read_meta(params.get("event_id")),
        )

    def _method_list_resources(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("kind")
        query = str(params.get("query") or "").casefold()
        items = [
            resource
            for _, resource in sorted(self._resources.items(), key=lambda item: int(item[0]))
            if (kind is None or resource["kind"] == kind) and query in resource["name"].casefold()
        ]
        page, next_cursor = self._page(items, params)
        return _payload({"resources": page}, next_cursor=next_cursor)

    def _method_get_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        return _payload(self._resource(params))

    def _method_get_resource_usage(self, params: dict[str, Any]) -> dict[str, Any]:
        resource = self._resource(params)
        page, next_cursor = self._page(self._usage.get(resource["resourceId"], []), params)
        return _payload({"resourceId": resource["resourceId"], "usage": page}, next_cursor=next_cursor)

    def _texture_pixels(self, resource_id: str) -> list[tuple[int, int, int, int]]:
        seed = int(resource_id) % 255
        return [((x * 64 + seed) % 256, (y * 64 + seed) % 256, seed, 255) for y in range(4) for x in range(4)]

    def _texture_view(self, params: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
        resource = self._resource(params, "texture")
        if any(params.get(key, 0) != 0 for key in ("mip", "slice", "sample")):
            raise AgentBridgeError("INVALID_REQUEST", "The fake texture only has mip, slice and sample 0.")
        event_id = self._event_id(params) if params.get("event_id") is not None else None
        return resource, event_id

    def _method_export_texture(self, params: dict[str, Any]) -> BridgeResult:
        resource, event_id = self._texture_view(params)
        if params.get("format", "png") != "png":
            raise AgentBridgeError("UNSUPPORTED", "The fake bridge exports PNG only.")
        directory = artifact_directory(
            str(self.root), self.instance_id, self.capture_session_id, "textures"
        )
        path = Path(directory) / f"{resource['resourceId']}-eid{event_id or self.current_event_id}.png"
        path.write_bytes(_png_bytes(4, 4, self._texture_pixels(resource["resourceId"])))
        artifact = describe_artifact(
            str(path),
            "texture",
            "image/png",
            source={
                "event_id": event_id or self.current_event_id,
                "resource_id": resource["resourceId"],
                "mip": 0,
                "slice": 0,
                "sample": 0,
            },
            extra={"width": 4, "height": 4},
        )
        return BridgeResult(
            _payload({"resource": resource}, artifacts=[artifact]), self._read_meta(event_id)
        )

    def _method_pick_pixel(self, params: dict[str, Any]) -> BridgeResult:
        resource, event_id = self._texture_view(params)
        x = params.get("x")
        y = params.get("y")
        if type(x) is not int or type(y) is not int or not (0 <= x < 4 and 0 <= y < 4):
            raise AgentBridgeError("INVALID_REQUEST", "The pixel coordinate is outside the texture.")
        value = self._texture_pixels(resource["resourceId"])[y * 4 + x]
        return BridgeResult(
            _payload(
                {
                    "resourceId": resource["resourceId"],
                    "x": x,
                    "y": y,
                    "raw": list(value),
                    "normalized": [channel / 255.0 for channel in value],
                }
            ),
            self._read_meta(event_id),
        )

    def _method_get_texture_stats(self, params: dict[str, Any]) -> BridgeResult:
        resource, event_id = self._texture_view(params)
        pixels = self._texture_pixels(resource["resourceId"])
        return BridgeResult(
            _payload(
                {
                    "resourceId": resource["resourceId"],
                    "min": [min(pixel[index] for pixel in pixels) for index in range(4)],
                    "max": [max(pixel[index] for pixel in pixels) for index in range(4)],
                }
            ),
            self._read_meta(event_id),
        )

    def _method_pixel_history(self, params: dict[str, Any]) -> BridgeResult:
        resource, _ = self._texture_view(params)
        event_id = self._event_id(params)
        x = params.get("x")
        y = params.get("y")
        if type(x) is not int or type(y) is not int or not (0 <= x < 4 and 0 <= y < 4):
            raise AgentBridgeError("INVALID_REQUEST", "The pixel coordinate is outside the texture.")
        final = [channel / 255.0 for channel in self._texture_pixels(resource["resourceId"])[y * 4 + x]]
        cleared = {"color": [0.0, 0.0, 0.0, 0.0], "depth": None, "stencil": None}
        written = {"color": final, "depth": None, "stencil": None}
        entries = [
            {
                "eventId": usage["eventId"],
                "actionName": self._actions[usage["eventId"]]["name"],
                "fragmentCount": 1,
                "passedCount": 1,
                "passed": True,
                "failureCounts": {},
                "primitiveIds": [0],
                "directShaderWrite": False,
                "unboundPS": False,
                "changed": True,
                "preMod": cleared,
                "shaderOut": written,
                "postMod": written,
            }
            for usage in self._usage.get(resource["resourceId"], [])
            if usage["access"] == "write" and usage["eventId"] <= event_id
        ]
        last = entries[-1]["eventId"] if entries else None
        page, next_cursor = self._page(list(reversed(entries)), params)
        return BridgeResult(
            _payload(
                {
                    "eventId": event_id,
                    "texture": resource,
                    "x": x,
                    "y": y,
                    "order": "newest",
                    "total": len(entries),
                    "summary": {
                        "eventCount": len(entries),
                        "lastPassedEventId": last,
                        "lastChangedEventId": last,
                        "finalValue": entries[-1]["postMod"] if entries else None,
                    },
                    "events": page,
                },
                next_cursor=next_cursor,
            ),
            self._read_meta(event_id),
        )

    def _method_get_buffer_data(self, params: dict[str, Any]) -> BridgeResult:
        return self._buffer_data(params)

    def _method_debug_pixel(self, params: dict[str, Any]) -> BridgeResult:
        event_id = self._event_id(params)
        action = self._actions[event_id]
        shader = (action.get("shaders") or {}).get("pixel")
        if shader is None:
            raise AgentBridgeError("INVALID_EVENT", "No pixel shader is bound at this event.",
                                   details={"event_id": event_id, "stage": "pixel"})
        x, y = params.get("x"), params.get("y")
        directory = artifact_directory(
            str(self.root), self.instance_id, self.capture_session_id, "replay"
        )
        path = Path(directory) / f"debug-pixel-e{event_id}-x{x}-y{y}.jsonl"
        path.write_text('{"record":"header"}\n{"record":"summary","steps":1}\n', encoding="utf-8")
        artifact = describe_artifact(str(path), "shader-debug-trace", "application/x-ndjson",
                                     source={"event_id": event_id, "x": x, "y": y})
        output = {"name": "SV_Target0", "signatureIndex": 0, "type": "Float",
                  "value": [0.25, 0.5, 0.75, 1.0]}
        return BridgeResult(
            _payload(
                {
                    "eventId": event_id,
                    "x": x,
                    "y": y,
                    "shader": {"resourceId": shader, "entryPoint": "main"},
                    "debugged": True,
                    "steps": 1,
                    "executionComplete": True,
                    "traceComplete": True,
                    "inputs": [],
                    "outputs": [output],
                    "lastChanges": [],
                    "nanOrInfSteps": [],
                    "sampleSteps": 0,
                    "debugBreakSteps": [],
                    "traceArtifact": artifact["path"],
                },
                artifacts=[artifact],
            ),
            self._read_meta(event_id),
        )

    def _method_debug_vertex(self, params: dict[str, Any]) -> BridgeResult:
        event_id = self._event_id(params)
        action = self._actions[event_id]
        shader = (action.get("shaders") or {}).get("vertex")
        if shader is None:
            raise AgentBridgeError("INVALID_EVENT", "No vertex shader is bound at this event.",
                                   details={"event_id": event_id, "stage": "vertex"})
        vertex = params.get("vertex")
        if type(vertex) is not int or not 0 <= vertex < action.get("numIndices", 0):
            raise AgentBridgeError("INVALID_REQUEST", "vertex is outside the draw.")
        instance = params.get("instance", 0)
        directory = artifact_directory(
            str(self.root), self.instance_id, self.capture_session_id, "replay"
        )
        path = Path(directory) / f"debug-vertex-e{event_id}-v{vertex}-i{instance}.jsonl"
        path.write_text('{"record":"header"}\n{"record":"summary","steps":1}\n', encoding="utf-8")
        artifact = describe_artifact(str(path), "shader-debug-trace", "application/x-ndjson",
                                     source={"event_id": event_id, "vertex": vertex,
                                             "instance": instance, "stage": "vertex"})
        output = {"name": "SV_Position", "signatureIndex": 0, "type": "Float",
                  "value": [-1.0, 1.0, 0.0, 1.0]}
        return BridgeResult(
            _payload(
                {
                    "eventId": event_id,
                    "vertex": vertex,
                    "instance": instance,
                    "view": params.get("view", 0),
                    "index": vertex,
                    "indexBufferValue": None,
                    "shader": {"resourceId": shader, "entryPoint": "main"},
                    "debugged": True,
                    "steps": 1,
                    "executionComplete": True,
                    "traceComplete": True,
                    "inputs": [],
                    "outputs": [output],
                    "lastChanges": [],
                    "nanOrInfSteps": [],
                    "sampleSteps": 0,
                    "debugBreakSteps": [],
                    "traceArtifact": artifact["path"],
                },
                artifacts=[artifact],
            ),
            self._read_meta(event_id),
        )

    def _buffer_data(self, params: dict[str, Any]) -> BridgeResult:
        resource = self._resource(params, "buffer")
        offset = params.get("offset", 0)
        size = params.get("size", 256)
        if type(offset) is not int or type(size) is not int or offset < 0 or size < 1:
            raise AgentBridgeError("INVALID_REQUEST", "The buffer range is invalid.")
        if offset + size > resource["byteSize"]:
            raise AgentBridgeError("INVALID_REQUEST", "The buffer range is outside the resource.")
        data = bytes((offset + index) % 256 for index in range(size))
        event_id = self._event_id(params) if params.get("event_id") is not None else None
        return BridgeResult(
            _payload(
                {
                    "resourceId": resource["resourceId"],
                    "offset": offset,
                    "size": size,
                    "hex": data.hex(),
                }
            ),
            self._read_meta(event_id),
        )

    def _method_get_debug_messages(self, params: dict[str, Any]) -> dict[str, Any]:
        messages = [
            {
                "eventId": 20,
                "severity": "medium",
                "category": "execution",
                "message": "Fake bridge diagnostic for protocol testing.",
            }
        ]
        page, next_cursor = self._page(messages, params)
        return _payload({"messages": page}, next_cursor=next_cursor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a RenderTest agent bridge simulator.")
    parser.add_argument("--root", type=Path, default=Path(agent_root()))
    parser.add_argument("--instance-id")
    parser.add_argument("--no-capture", action="store_true")
    args = parser.parse_args(argv)

    bridge = FakeRenderTestBridge(
        args.root, instance_id=args.instance_id, capture_loaded=not args.no_capture
    ).start()
    print(f"Fake RenderTest bridge {bridge.instance_id} is running under {bridge.root}")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
