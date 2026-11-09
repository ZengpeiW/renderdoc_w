from __future__ import annotations

import argparse
import copy
import json
import os
import struct
import sys
import threading
import time
import uuid
import zlib
from pathlib import Path
from types import SimpleNamespace
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
        self._gpu_duration_sample: dict[str, Any] | None = None
        self._shader_reference_scan: dict[str, Any] | None = None
        self._pass_resource_scan: dict[str, Any] | None = None
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

    def _method_get_api_calls(self, params: dict[str, Any]) -> BridgeResult:
        # Each fixture action has one setup call followed by its action call. API EIDs
        # intentionally differ from action EIDs so the transport tests cannot conflate them.
        owners = {event: event for event in self._actions}
        owners.update({event - 1: event for event in self._actions if event > 1})
        api_event = params.get("api_event_id")
        event = params.get("event_id", api_event if api_event is not None else self.current_event_id)
        if event not in owners or (api_event is not None and api_event not in owners):
            raise AgentBridgeError("INVALID_EVENT", "The API event does not exist.")
        owner = owners[event]
        if api_event is not None and owners[api_event] != owner:
            raise AgentBridgeError("INVALID_REQUEST", "The API event has a different action owner.")
        if api_event is None:
            calls = [{"apiEventId": eid, "actionEventId": owner, "chunkIndex": eid,
                      "name": "SetBindings" if eid != owner else self._actions[owner]["name"],
                      "available": True, "parameterCount": 3}
                     for eid in ([owner - 1, owner] if owner > 1 else [owner])]
            page, cursor = self._page(calls, params)
            return BridgeResult(_payload({"mode": "calls", "eventId": event,
                                           "actionEventId": owner, "calls": page,
                                           "total": len(calls)}, next_cursor=cursor), self._read_meta())
        tree = {"name": "parameters", "type": "Chunk", "typeName": "API call", "members": [
            {"name": "Resource", "type": "Resource", "typeName": "ResourceId", "value": "2001"},
            {"name": "Count", "type": "UnsignedInteger", "typeName": "uint32_t", "value": 3},
            {"name": "Region", "type": "Struct", "typeName": "Range", "members": [
                {"name": "Min", "type": "Float", "typeName": "float", "value": 0.0},
                {"name": "Max", "type": "Float", "typeName": "float", "value": 1.0},
            ]},
        ]}
        path = params.get("parameter_path", "")
        selected = tree
        try:
            indices = [] if not path else [int(index) for index in path[1:-1].split("][")]
            for index in indices:
                selected = selected.get("members", [])[index]
        except (ValueError, IndexError):
            raise AgentBridgeError("INVALID_REQUEST", "The parameter path does not exist.")

        def project(node, node_path, depth):
            members = node.get("members", [])
            value = {key: value for key, value in node.items() if key != "members"}
            value.update(canonicalPath=node_path, childCount=len(members),
                         membersTruncated=bool(members) and depth <= 0)
            if members and depth > 0:
                value["members"] = [project(child, node_path + f"[{index}]", depth - 1)
                                    for index, child in enumerate(members)]
            return value

        children = [project(child, path + f"[{index}]", params.get("max_depth", 0))
                    for index, child in enumerate(selected.get("members", []))]
        page, cursor = self._page(children, params)
        data = {"mode": "parameters", "apiEventId": api_event, "actionEventId": owner,
                "chunkIndex": api_event, "name": self._actions[owner]["name"], "available": True,
                "parameterPath": path, "canonicalPath": path, "selected": project(selected, path, 0),
                "parameters": page, "childCount": len(children)}
        return BridgeResult(_payload(data, next_cursor=cursor), self._read_meta())

    def _method_compare_events(self, params: dict[str, Any]) -> BridgeResult:
        before = self._event_id({"event_id": params["before_event_id"]})
        after = self._event_id({"event_id": params["after_event_id"]})
        left = self._pipeline(before, params.get("stages"))
        right = self._pipeline(after, params.get("stages"))
        left.pop("eventId")
        right.pop("eventId")
        differences = []
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                differences.append({"path": "/pipeline/" + key,
                                    "before": {"present": key in left, "value": left.get(key)},
                                    "after": {"present": key in right, "value": right.get(key)}})
        page, cursor = self._page(differences, params)
        data = {"beforeEventId": before, "afterEventId": after,
                "comparison": "different" if differences else "equal-in-scope",
                "differences": page, "totalDifferences": len(differences), "totalDifferencesExact": True,
                "coverage": {"complete": True, "fixture": True,
                             "cbufferSelections": params.get("cbuffers", [])}}
        return BridgeResult(_payload(data, next_cursor=cursor), self._read_meta(after))

    def _method_get_action_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        parent = params.get("parent_event_id")
        root = 1 if parent is None else self._event_id({"event_id": parent})
        nodes = [self._actions[root]]
        for node in nodes:
            nodes.extend(self._actions[event] for event in node["children"])
        def count(items):
            value = {kind: sum(kind in node["flags"] and not node["children"] for node in items)
                     for kind in ("draw", "dispatch", "copy", "clear", "resolve", "present",
                                  "meshDispatch", "rayDispatch", "genMips", "buildAccStruct")}
            value.update(actionCount=len(items), operationCount=sum(value.values()),
                         markerCount=sum("marker" in node["flags"] for node in items),
                         containerCount=0, unclassifiedLeafCount=0, ambiguousOperationCount=0,
                         excludedParentOperationCount=0)
            return value

        group_ids = [1] if parent is None else self._actions[root]["children"]
        groups, cursor = self._page([
            {"action": self._action_summary(event),
             "counts": count(nodes if event == root else [self._actions[event]])}
            for event in group_ids], params)
        top_n = params.get("top_n", 20)
        draws = sorted([dict(self._action_summary(node["eventId"]), numIndices=node["numIndices"],
                             numInstances=node["numInstances"], indexed=False,
                             indexOrVertexInvocations=node["numIndices"] * node["numInstances"])
                        for node in nodes if "draw" in node["flags"]],
                       key=lambda row: (-row["indexOrVertexInvocations"], row["eventId"]))
        dispatches = [dict(self._action_summary(node["eventId"]), dispatchDimension=node["dispatchDimensions"],
                           groupCount=node["dispatchDimensions"][0] * node["dispatchDimensions"][1] * node["dispatchDimensions"][2])
                      for node in nodes if "dispatch" in node["flags"]]

        def ranking(rows, metric, formula):
            return {"metric": metric, "formula": formula, "candidateCount": len(rows),
                    "rankableCount": len(rows), "unrankableCount": 0,
                    "returnedCount": min(top_n, len(rows)), "truncated": len(rows) > top_n,
                    "actions": rows[:top_n]}

        return _payload({"parentEventId": parent, "counts": count(nodes), "groups": groups,
                         "groupsTotal": len(group_ids), "topN": top_n,
                         "topDraws": ranking(draws, "indexOrVertexInvocations", "numIndices * numInstances"),
                         "topDispatches": ranking(dispatches, "groupCount", "x * y * z")},
                        next_cursor=cursor)

    def _method_list_gpu_counters(self, params: dict[str, Any]) -> dict[str, Any]:
        counters = [
            {"id": 1, "name": "GPU Duration", "category": "D3D12 Built-in",
             "description": "Time taken for this event during GPU replay.",
             "resultType": "Float", "resultByteWidth": 8, "unit": "Seconds",
             "uuid": [1, 2, 3, 4], "durationSamplingSupported": True},
            {"id": 13, "name": "CS Invocations", "category": "D3D12 Built-in",
             "description": "Number of compute shader invocations.",
             "resultType": "UInt", "resultByteWidth": 8, "unit": "Absolute",
             "uuid": [1, 2, 3, 13], "durationSamplingSupported": False},
        ]
        prefix = "gpu-catalog:" + self.capture_session_id + ":"
        cursor = params.get("cursor")
        if cursor is not None and (not cursor.startswith(prefix) or not cursor[len(prefix):].isdigit()):
            raise AgentBridgeError("INVALID_REQUEST", "The counter directory cursor is invalid or expired.")
        page, offset = self._page(counters, dict(params, cursor=cursor[len(prefix):] if cursor else None))
        return _payload({"counters": page, "total": len(counters), "duplicateCounterIds": 0,
                         "samplingPolicy": "Only EventGPUDuration with Float/8-byte/Seconds metadata is sampled."},
                        next_cursor=prefix + offset if offset else None, truncated=offset is not None)

    def _method_get_gpu_duration(self, params: dict[str, Any]) -> BridgeResult:
        refresh, cursor = params.get("refresh", False), params.get("cursor")
        if type(refresh) is not bool or (refresh and cursor is not None):
            raise AgentBridgeError("INVALID_REQUEST", "refresh cannot be combined with a cursor.")
        parent, sort_by = params.get("parent_event_id"), params.get("sort_by", "duration")
        if sort_by not in ("duration", "event_id"):
            raise AgentBridgeError("INVALID_REQUEST", "Unknown duration sort order.")
        if parent is not None:
            self._event_id({"event_id": parent}, required=True)
        sample = self._gpu_duration_sample
        if sample is not None and sample["session"] != self.capture_session_id:
            sample = None
        cached = sample is not None and not refresh
        if sample is None or refresh:
            if cursor is not None:
                raise AgentBridgeError("INVALID_REQUEST", "The duration sample cursor expired.")
            sample = {"session": self.capture_session_id, "id": uuid.uuid4().hex,
                      "sampledAtUtc": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
            self._gpu_duration_sample = sample
        prefix = "gpu-duration:%s:%s:%s:" % (sample["id"], parent, sort_by)
        if cursor is not None and (not cursor.startswith(prefix) or not cursor[len(prefix):].isdigit()):
            raise AgentBridgeError("INVALID_REQUEST", "The duration sample, scope or sort cursor does not match.")
        scope = list(self._actions) if parent is None else [parent]
        if parent is not None:
            for event_id in scope:
                scope.extend(self._actions[event_id]["children"])
        rows = []
        for event_id in scope:
            action = self._actions[event_id]
            seconds = {10: 0.001, 20: 0.0}.get(event_id)
            status = "valid" if seconds is not None else "invalid" if event_id == 30 else "missing"
            row = {"eventId": event_id, "name": action["name"], "flags": action["flags"],
                   "status": status, "durationSeconds": seconds,
                   "durationMs": seconds * 1000 if seconds is not None else None}
            if status != "valid":
                row["reason"] = "negative native duration" if status == "invalid" else "no native sample"
            if status == "invalid":
                row["rawDuration"] = -1.0
            rows.append(row)
        rows.sort(key=(lambda row: (row["status"] != "valid", -(row["durationSeconds"] or 0.0),
                                    row["eventId"])) if sort_by == "duration" else
                         (lambda row: row["eventId"]))
        page, offset = self._page(rows, dict(params, cursor=cursor[len(prefix):] if cursor else None))
        data = {"counterId": 1, "counterName": "EventGPUDuration", "sampleId": sample["id"],
                "sampledAtUtc": sample["sampledAtUtc"], "cached": cached,
                "samplingScope": "capture", "parentEventId": parent, "sortBy": sort_by,
                "scopeActionCount": len(rows), "validDurationCount": sum(r["status"] == "valid" for r in rows),
                "invalidDurationCount": sum(r["status"] == "invalid" for r in rows),
                "missingDurationCount": sum(r["status"] == "missing" for r in rows),
                "duplicateDurationCount": 0, "events": page,
                "diagnostics": {"rawResultCount": 3, "unknownEventResults": 0, "unknownEventIds": [],
                                "counterMismatchResults": 0, "malformedResults": 0, "duplicateResultCount": 0}}
        return BridgeResult(_payload(data, next_cursor=prefix + offset if offset else None,
                                     truncated=offset is not None,
                                     warnings=["Synthetic replay timing fixture; not application frame time."]),
                            self._read_meta(None if cached else self.current_event_id))

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

    def _method_find_shader_references(self, params: dict[str, Any]) -> BridgeResult:
        resource_id = params["resource_id"]
        resource = self._resources.get(resource_id)
        if resource is None or resource.get("kind") != "shader":
            raise AgentBridgeError("INVALID_RESOURCE", "An exact Shader ResourceId is required.")
        all_stages = ["vertex", "hull", "domain", "geometry", "pixel", "compute", "amplification", "mesh"]
        stages = [stage for stage in all_stages if params.get("stages") is None or stage in params["stages"]]
        parent = params.get("parent_event_id")
        if parent is not None:
            self._event_id({"event_id": parent}, required=True)
        cursor, refresh = params.get("cursor"), params.get("refresh", False)
        if refresh and cursor is not None:
            raise AgentBridgeError("INVALID_REQUEST", "refresh cannot be combined with a cursor.")
        scan = self._shader_reference_scan
        if scan is not None and scan["session"] != self.capture_session_id:
            scan = None
        if scan is None or refresh:
            if cursor is not None:
                raise AgentBridgeError("INVALID_REQUEST", "The shader reference scan expired.")
            scan = {"session": self.capture_session_id, "id": uuid.uuid4().hex, "bindings": {},
                    "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
            self._shader_reference_scan = scan
        prefix = "shader-references:%s:%s:%s:%s:%s:" % (
            self.capture_session_id, scan["id"], resource_id, parent, ",".join(stages))
        if cursor is not None and (not cursor.startswith(prefix) or not cursor[len(prefix):].isdigit()):
            raise AgentBridgeError("INVALID_REQUEST", "The shader reference scan or query cursor does not match.")
        offset = int(cursor[len(prefix):]) if cursor else 0
        scope = list(self._actions) if parent is None else [parent]
        if parent is not None:
            for event_id in scope:
                scope.extend(self._actions[event_id]["children"])
        candidates, unsupported, stage_excluded, excluded = [], [], 0, 0
        for event_id in scope:
            action = self._actions[event_id]
            flags = action["flags"]
            if action["children"]:
                excluded += 1
                continue
            if "rayDispatch" in flags or "dispatchRay" in flags:
                unsupported.append(event_id)
                continue
            if "meshDispatch" in flags:
                relevant = ["amplification", "mesh", "pixel"]
            elif "draw" in flags:
                relevant = ["vertex", "hull", "domain", "geometry", "pixel"]
            elif "dispatch" in flags:
                relevant = ["compute"]
            else:
                excluded += 1
                continue
            if not set(relevant).intersection(stages):
                stage_excluded += 1
                continue
            candidates.append((event_id, relevant))
        if offset > len(candidates):
            raise AgentBridgeError("INVALID_REQUEST", "The shader reference cursor is beyond the candidates.")
        rows, examined, reads, hits = [], 0, 0, 0
        limit, budget = params.get("limit", 200), params.get("scan_budget", 200)
        for event_id, relevant in candidates[offset:]:
            if examined >= budget or len(rows) >= limit:
                break
            examined += 1
            action = self._actions[event_id]
            if event_id in scan["bindings"]:
                bindings = scan["bindings"][event_id]
                hits += 1
            else:
                bindings = {stage: shader for stage, shader in action.get("shaders", {}).items() if stage in relevant}
                scan["bindings"][event_id] = bindings
                reads += 1
            matches = [{"stage": stage, "resourceId": resource_id, "entryPoint": "main", "entryPointKnown": True}
                       for stage in stages if bindings.get(stage) == resource_id]
            if matches:
                rows.append({"eventId": event_id, "name": action["name"], "flags": action["flags"],
                             "parentEventId": action["parentEventId"], "matches": matches})
        end = offset + examined
        complete = end == len(candidates)
        coverage = {"scopeActionCount": len(scope), "candidateEventCount": len(candidates),
                    "processedCandidateCount": end, "examinedThisPage": examined,
                    "nativeReadsThisPage": reads, "cacheHitsThisPage": hits,
                    "excludedActionCount": excluded, "stageExcludedActionCount": stage_excluded,
                    "unsupportedActionCount": len(unsupported), "unsupportedActionExamples": unsupported[:16],
                    "unsupportedStages": [], "scanComplete": complete,
                    "complete": complete and not unsupported}
        return BridgeResult(_payload({"resourceId": resource_id, "resourceName": resource["name"],
                                      "stages": stages, "parentEventId": parent, "scanId": scan["id"],
                                      "scanStartedAtUtc": scan["startedAt"], "references": rows, "coverage": coverage},
                                     next_cursor=prefix + str(end) if not complete else None,
                                     truncated=not coverage["complete"],
                                     warnings=["Synthetic bound references do not prove shader execution."]),
                            self._read_meta(self.current_event_id))

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
        event_id, shader_id = self._shader_id(params)
        index = params.get("index")
        name = params.get("name")
        if index not in (None, 0) or name not in (None, "MaterialConstants"):
            raise AgentBridgeError("INVALID_REQUEST", "The constant buffer does not exist.")
        from rendertest_agent_bridge.methods.cbuffer_members import validate_request, project_members
        request = validate_request(params)
        if request is not None:
            from rendertest_agent_bridge.methods.pipeline import convert_variable
            definitions = [("Tint", 0, [1.0, 0.5, 0.25, 1.0]), ("Exposure", 16, [1.0])]
            variables = [SimpleNamespace(name=label, type="Float", rows=1, columns=len(values),
                                          members=[], value=SimpleNamespace(f32v=values))
                         for label, _, values in definitions]
            block = SimpleNamespace(variables=[
                SimpleNamespace(name=label, byteOffset=offset,
                                type=SimpleNamespace(name="float" if len(values) == 1 else "float4",
                                                     elements=1, members=[], arrayByteStride=0))
                for label, offset, values in definitions])
            data, cursor, truncated, warnings = project_members(
                variables, block, request,
                (self.capture_session_id, event_id, params["stage"], shader_id, 0, "2001", 0, 32),
                convert_variable)
            data.update(eventId=event_id, stage=params["stage"], index=0, name="MaterialConstants")
            return BridgeResult(_payload(data, next_cursor=cursor, truncated=truncated, warnings=warnings),
                                self._read_meta(params.get("event_id")))
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

    def _method_get_shader_source(self, params: dict[str, Any]) -> BridgeResult:
        event, shader = self._shader_id(params)
        text = "// Embedded test source\nfloat4 main() : SV_Target { return 1; }\n"
        source = {"index": 0, "filename": "capture-only/shader.hlsl",
                  "bytes": len(text.encode("utf-8")), "available": True}
        data = {"eventId": event, "stage": params["stage"], "shader": {"resourceId": shader},
                "available": True, "fileCount": 1}
        artifacts = []
        if params.get("file_index") is None:
            data["files"] = [source]
        else:
            if params["file_index"] != 0:
                raise AgentBridgeError("INVALID_REQUEST", "Unknown embedded file index.")
            directory = artifact_directory(str(self.root), self.instance_id, self.capture_session_id, "replay")
            path = Path(directory) / f"shader-source-{uuid.uuid4().hex}.txt"
            path.write_text(text, encoding="utf-8", newline="")
            artifacts.append(describe_artifact(str(path), "shader-source", "text/plain; charset=utf-8",
                                               source={"event": event, "stage": params["stage"],
                                                       "shaderResourceId": shader, "fileIndex": 0,
                                                       "filename": source["filename"]}))
            data.update(file=source, sourceArtifact=str(path))
        return BridgeResult(_payload(data, artifacts=artifacts), self._read_meta(event))

    def _method_decompile_shader(self, params: dict[str, Any]) -> BridgeResult:
        event, shader = self._shader_id(params)
        requested_language = params.get("language", "auto")
        if requested_language not in ("auto", "hlsl", "glsl"):
            raise AgentBridgeError("INVALID_REQUEST", "Unknown output language.")
        language = "hlsl" if requested_language == "auto" else requested_language
        directory = artifact_directory(str(self.root), self.instance_id, self.capture_session_id, "replay")
        path = Path(directory) / f"shader-decompiled-{uuid.uuid4().hex}.txt"
        text = ("// Synthetic generated shader fixture, not original source\n" +
                ("float4 main() : SV_Target { return 1; }\n" if language == "hlsl"
                 else "#version 450\nvoid main() {}\n"))
        path.write_text(text, encoding="utf-8", newline="")
        artifact = describe_artifact(str(path), "shader-decompiled", "text/plain; charset=utf-8",
                                     source={"event": event, "stage": params["stage"],
                                             "shaderResourceId": shader, "generated": True})
        return BridgeResult(_payload(
            {"eventId": event, "stage": params["stage"], "shader": {"resourceId": shader},
             "available": True, "generated": True, "isOriginalSource": False,
             "inputEncoding": "DXIL", "outputLanguage": language, "requestedLanguage": requested_language,
             "decompiledArtifact": str(path), "readArtifactCompatible": True},
            artifacts=[artifact], warnings=["Synthetic generated shader; no release tool was run."]),
            self._read_meta(event))

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

    def _method_get_resource_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("kind", "all")
        resources = [dict(item, byteSize=item.get("byteSize", 64)) for item in self._resources.values()
                     if item["kind"] in ("texture", "buffer") and kind in ("all", item["kind"])]
        resources.sort(key=lambda item: (-item["byteSize"], int(item["resourceId"])))
        top = resources[:params.get("top_n", 20)]
        return _payload({"kind": kind, "resourceCount": len(resources),
                         "sizeBasis": "resource-description-bytes",
                         "knownTotalBytes": sum(item["byteSize"] for item in resources),
                         "totalBytesComplete": True, "largestResources": top,
                         "hasMoreLargest": len(top) < len(resources)})

    def _method_get_resource_usage(self, params: dict[str, Any]) -> dict[str, Any]:
        resource = self._resource(params)
        page, next_cursor = self._page(self._usage.get(resource["resourceId"], []), params)
        return _payload({"resourceId": resource["resourceId"], "usage": page}, next_cursor=next_cursor)

    def _method_get_pass_resources(self, params: dict[str, Any]) -> dict[str, Any]:
        parent, start, end = (params.get(key) for key in
                              ("parent_event_id", "start_event_id", "end_event_id"))
        kind, cursor, refresh = params.get("kind", "all"), params.get("cursor"), params.get("refresh", False)
        if ((parent is not None and (start is not None or end is not None)) or
                (start is None) != (end is None) or (start is not None and start > end) or
                (refresh and cursor is not None)):
            raise AgentBridgeError("INVALID_REQUEST", "Invalid scope or refresh/cursor combination.")
        # The fake's Frame marker is synthetic; each executable action owns a real setup API EID.
        real = {event for event, action in self._actions.items() if "marker" not in action["flags"]}
        owners = {event: event for event in real}
        owners.update({event - 1: event for event in real if event > 1})
        selected_actions = set(self._actions)
        scope_kind = "capture"
        if parent is not None:
            self._event_id({"event_id": parent}, required=True)
            selected_actions = {parent}
            pending_actions = [parent]
            for event in pending_actions:
                pending_actions.extend(self._actions[event]["children"])
            selected_actions.update(pending_actions)
            anchors = sorted(selected_actions.intersection(real))
            if not anchors:
                raise AgentBridgeError("UNSUPPORTED", "The subtree has no real API/action anchors.")
            start, end = anchors[0], anchors[-1]
            selected = {event for event, owner in owners.items() if owner in selected_actions and
                        start is not None and start <= event <= end}
            scope_kind = "actionSubtree"
        elif start is not None:
            if start not in owners or end not in owners:
                raise AgentBridgeError("INVALID_EVENT", "Range endpoints must be real API/action EIDs.")
            selected = {event for event in owners if start <= event <= end}
            selected_actions = {event for event in real if start <= event <= end}
            scope_kind = "eventRange"
        else:
            selected = set(owners)
        scope = {"kind": scope_kind, "parentEventId": parent, "startEventId": start,
                 "endEventId": end, "actionCount": len(selected_actions),
                 "selectedEventCount": len(selected),
                 "membership": "All native usage EIDs, including usages without an indexed action."}
        if parent is not None:
            scope["syntheticParent"] = parent not in real
            scope["membership"] = ("Exact subtree action/API EIDs, clipped to real action anchors; "
                                   "synthetic marker EIDs and setup before the first anchor are excluded.")
        elif scope_kind == "eventRange":
            scope["membership"] = "Inclusive raw usage EID interval; action ownership is metadata only."
        scan = self._pass_resource_scan
        if scan is not None and scan["session"] != self.capture_session_id:
            scan = None
        if scan is None or refresh:
            if cursor is not None:
                raise AgentBridgeError("INVALID_REQUEST", "The resource-usage scan expired.")
            scan = {"session": self.capture_session_id, "id": uuid.uuid4().hex, "usage": {},
                    "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
        prefix = "pass-resources:%s:%s:%s:%s:%s:%s:" % (
            self.capture_session_id, scan["id"], parent, start, end, kind)
        if cursor is not None and (not cursor.startswith(prefix) or not cursor[len(prefix):].isdigit()):
            raise AgentBridgeError("INVALID_REQUEST", "The resource-usage scan or scope cursor does not match.")
        offset = int(cursor[len(prefix):]) if cursor else 0
        candidates = sorted((key for key, resource in self._resources.items()
                             if resource["kind"] in ("texture", "buffer") and
                             (kind == "all" or resource["kind"] == kind)), key=int)
        if offset > len(candidates):
            raise AgentBridgeError("INVALID_REQUEST", "The resource cursor is beyond the candidate list.")
        aliases = {"ReadWriteResource": "CS_RWResource", "PixelShaderResource": "PS_Resource",
                   "ComputeShaderResource": "CS_Resource", "VertexConstantBuffer": "VS_Constants"}
        usage_names = ["Unused", "VertexBuffer", "IndexBuffer"]
        stages = ("VS", "HS", "DS", "GS", "PS", "CS", "TS", "MS", "All")
        usage_names += [stage + "_Constants" for stage in stages] + ["StreamOut"]
        usage_names += [stage + "_Resource" for stage in stages]
        usage_names += [stage + "_RWResource" for stage in stages]
        usage_names += ["InputTarget", "ColorTarget", "DepthStencilTarget", "Indirect", "Clear",
                        "Discard", "GenMips", "Resolve", "ResolveSrc", "ResolveDst", "Copy",
                        "CopySrc", "CopyDst", "Barrier", "CPUWrite"]

        def access(name):
            if name in ("Unused", "Discard", "Barrier"):
                return name.lower()
            if name.endswith("_RWResource") or name in ("DepthStencilTarget", "Copy", "Resolve", "GenMips"):
                return "readWrite"
            if name in ("ColorTarget", "StreamOut", "Clear", "ResolveDst", "CopyDst", "CPUWrite"):
                return "write"
            return "read" if name in usage_names else "unknown"

        def records_for(key):
            normalized = []
            for item in self._usage.get(key, []):
                event, name = item.get("eventId"), item.get("usage")
                if type(event) is not int or not 0 <= event <= 0xFFFFFFFF or not isinstance(name, str):
                    raise AgentBridgeError("REPLAY_ERROR", "Malformed native usage record.")
                name = aliases.get(name, name)
                row = {"eventId": event, "actionEventId": owners.get(event), "usage": name,
                       "usageValue": item.get("usageValue", usage_names.index(name) if name in usage_names else 9999),
                       "access": access(name)}
                if "viewId" in item:
                    row["viewId"] = item["viewId"]
                normalized.append(row)
            return sorted(normalized, key=lambda item: (item["eventId"], item["usageValue"]))

        def projection(key, records):
            sentinels = sum(row["eventId"] == 0 and row["usage"] == "Unused" for row in records)
            valid = [row for row in records if not (row["eventId"] == 0 and row["usage"] == "Unused")]
            bounded = [row for row in valid if start is None or start <= row["eventId"] <= end]
            inside = bounded if scope_kind != "actionSubtree" else [row for row in bounded if row["eventId"] in selected]
            diagnostics = {"unusedSentinelCount": sentinels, "malformedUsageRecordCount": 0,
                           "unmappedUsageRecordCount": sum(row["actionEventId"] is None for row in bounded),
                           "unknownUsageRecordCount": sum(row["access"] == "unknown" for row in inside)}
            if not inside:
                return None, diagnostics
            aggregate = {}
            for row in inside:
                token = (row["usage"], row["usageValue"], row["access"])
                entry = aggregate.setdefault(token, {"usage": row["usage"], "usageValue": row["usageValue"],
                    "access": row["access"], "count": 0, "firstEventId": row["eventId"], "lastEventId": row["eventId"]})
                entry["count"] += 1
                entry["lastEventId"] = row["eventId"]
            def neighbor(before):
                boundary = start if before else end
                if boundary is None:
                    return None
                outside = [row for row in valid if (row["eventId"] < boundary if before else row["eventId"] > boundary)]
                if not outside:
                    return None
                event = (max if before else min)(row["eventId"] for row in outside)
                group = [row for row in outside if row["eventId"] == event]
                return {"eventId": event, "recordCount": len(group), "usages": group[:16], "truncated": len(group) > 16}
            resource = self._resources[key]
            return {"resourceId": key, "name": resource["name"], "kind": resource["kind"],
                    "type": resource.get("type", resource["kind"].title()),
                    "usageRecordCount": len(inside), "eventCount": len({row["eventId"] for row in inside}),
                    "firstEventId": inside[0]["eventId"], "lastEventId": inside[-1]["eventId"],
                    "hasReadUsage": any(row["access"] in ("read", "readWrite") for row in inside),
                    "hasWriteUsage": any(row["access"] in ("write", "readWrite") for row in inside),
                    "usageTypes": sorted(aggregate.values(), key=lambda item: (item["usageValue"], item["usage"])),
                    "usageExamples": inside[:16],
                    "usageExamplesTruncated": len(inside) > 16,
                    "previousUsage": neighbor(True), "nextUsage": neighbor(False)}, diagnostics

        rows, pending, examined, reads, hits = [], {}, 0, 0, 0
        limit, budget = params.get("limit", 200), params.get("scan_budget", 200)
        for key in candidates[offset:]:
            if examined >= budget or len(rows) >= limit:
                break
            examined += 1
            if key in scan["usage"]:
                records = scan["usage"][key]
                hits += 1
            else:
                records = records_for(key)
                pending[key] = records
                reads += 1
            row, _ = projection(key, records)
            if row is not None:
                rows.append(row)
        consumed = offset + examined
        totals = {key: 0 for key in ("unknownUsageRecordCount", "unmappedUsageRecordCount",
                                   "malformedUsageRecordCount", "unusedSentinelCount")}
        merged = dict(scan["usage"], **pending)
        for key in candidates[:consumed]:
            if key not in merged:
                raise AgentBridgeError("INVALID_REQUEST", "The resource cursor skips unobserved candidates.")
            _, diagnostics = projection(key, merged[key])
            for name, count in diagnostics.items():
                totals[name] += count
        scan["usage"].update(pending)
        self._pass_resource_scan = scan
        scan_complete = consumed == len(candidates)
        complete = scan_complete and not totals["unknownUsageRecordCount"] and not totals["unmappedUsageRecordCount"]
        coverage = dict(totals, candidateResourceCount=len(candidates), processedResourceCount=consumed,
                        examinedThisPage=examined, nativeReadsThisPage=reads, cacheHitsThisPage=hits,
                        scanComplete=scan_complete, complete=complete)
        return _payload({"scope": scope, "kind": kind, "scanId": scan["id"],
                         "scanStartedAtUtc": scan["startedAt"], "resources": rows, "coverage": coverage},
                        next_cursor=prefix + str(consumed) if not scan_complete else None,
                        truncated=not complete or any(row["usageExamplesTruncated"] for row in rows),
                        warnings=["Synthetic resource usage describes potential roles, not proven producers or consumers."])

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

    def _method_export_overlay(self, params: dict[str, Any]) -> BridgeResult:
        from rendertest_agent_bridge.methods.overlay import OVERLAYS
        if params.get("overlay") not in OVERLAYS:
            raise AgentBridgeError("INVALID_REQUEST", "Unknown diagnostic overlay.")
        resource, event = self._texture_view(params)
        event = self.current_event_id if event is None else event
        directory = artifact_directory(str(self.root), self.instance_id, self.capture_session_id, "replay")
        path = Path(directory) / f"overlay-{uuid.uuid4().hex}.png"
        path.write_bytes(_png_bytes(4, 4, [(255, 0, 0, 255), (0, 255, 0, 255)] * 8))
        artifact = describe_artifact(str(path), "overlay", "image/png", source={
            "event_id": event, "resource_id": resource["resourceId"], "overlay": params["overlay"],
            "mip": 0, "slice": 0, "sample": 0,
        }, extra={"width": 4, "height": 4})
        return BridgeResult(_payload({"eventId": event, "overlay": params["overlay"],
                                       "texture": resource, "path": str(path),
                                       "representation": "composited-diagnostic",
                                       "legend": "Synthetic test image, not native GPU diagnostic data."},
                                      artifacts=[artifact]), self._read_meta(event))

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

    def _method_debug_compute(self, params: dict[str, Any]) -> BridgeResult:
        event_id = self._event_id(params)
        action = self._actions[event_id]
        shader = (action.get("shaders") or {}).get("compute")
        if shader is None or "dispatch" not in action["flags"]:
            raise AgentBridgeError("INVALID_EVENT", "The event is not a compute dispatch.")
        group, thread = params.get("group"), params.get("thread")
        dimensions, thread_size = action["dispatchDimensions"], [4, 4, 1]
        for field, coordinates, size in (("group", group, dimensions), ("thread", thread, thread_size)):
            if (not isinstance(coordinates, list) or len(coordinates) != 3 or
                    any(type(value) is not int or not 0 <= value < bound
                        for value, bound in zip(coordinates, size))):
                raise AgentBridgeError("INVALID_REQUEST", field + " is outside the compute dispatch.")
        global_thread = [group[index] * thread_size[index] + thread[index] for index in range(3)]
        directory = artifact_directory(str(self.root), self.instance_id, self.capture_session_id, "replay")
        path = Path(directory) / f"debug-compute-e{event_id}-{uuid.uuid4().hex}.jsonl"
        invocation = {"stage": "compute", "eventId": event_id, "group": group, "thread": thread,
                      "globalThread": global_thread, "dispatchDimensions": dimensions,
                      "threadGroupSize": thread_size, "threadGroupSizeSource": "shader_reflection"}
        header = {"record": "header", "schema": "rendertest-shader-debug-trace/v1",
                  "invocation": invocation, "shader": {"resourceId": shader, "entryPoint": "main"},
                  "files": []}
        summary = {"record": "summary", "steps": 1, "executionComplete": True, "traceComplete": True}
        path.write_text(json.dumps(header) + "\n" + json.dumps(summary) + "\n", encoding="utf-8")
        artifact = describe_artifact(str(path), "shader-debug-trace", "application/x-ndjson",
                                     source={"event_id": event_id, "stage": "compute",
                                             "group": group, "thread": thread, "globalThread": global_thread,
                                             "dispatchDimensions": dimensions, "threadGroupSize": thread_size,
                                             "threadGroupSizeSource": "shader_reflection"})
        return BridgeResult(_payload({
            "eventId": event_id, "stage": "compute", "group": group, "thread": thread,
            "globalThread": global_thread, "dispatchDimensions": dimensions, "threadGroupSize": thread_size,
            "threadGroupSizeSource": "shader_reflection",
            "shader": {"resourceId": shader, "entryPoint": "main"},
            "debugged": True, "steps": 1, "executionComplete": True, "traceComplete": True,
            "inputs": [], "outputs": [],
            "lastChanges": [{"name": "dispatchThreadId", "type": "UInt", "value": global_thread}],
            "nanOrInfSteps": [], "sampleSteps": 0, "debugBreakSteps": [],
            "traceArtifact": artifact["path"],
        }, artifacts=[artifact], warnings=["Synthetic single-invocation trace; not a UAV write log."]),
            self._read_meta(event_id))

    def _method_get_mesh_data(self, params: dict[str, Any]) -> BridgeResult:
        event_id = self._event_id(params)
        action = self._actions[event_id]
        if "draw" not in action["flags"]:
            raise AgentBridgeError("INVALID_EVENT", "The event is not a draw.",
                                   details={"event_id": event_id})
        if params.get("stage", "vs_out") != "vs_out":
            raise AgentBridgeError("INVALID_REQUEST", "The draw has no geometry stage.")
        count = action["numIndices"]
        positions = [[-1.0 + 2.0 * (row % 3), 1.0 - 2.0 * (row // 3 % 2), 0.0, 1.0]
                     for row in range(count)]
        rows = [{"vertex": row, "index": row, "values": [position, [row / max(1, count), 0.0]]}
                for row, position in enumerate(positions)]
        page, next_cursor = self._page(rows, params)
        return BridgeResult(
            _payload(
                {
                    "eventId": event_id,
                    "stage": "vs_out",
                    "instance": params.get("instance", 0),
                    "view": params.get("view", 0),
                    "topology": "TriangleList",
                    "indexed": False,
                    "rowCount": count,
                    "decodedRows": count,
                    "columns": [
                        {"name": "SV_Position", "semantic": "SV_Position",
                         "systemValue": "Position", "type": "Float", "components": 4,
                         "byteOffset": 0},
                        {"name": "TEXCOORD0", "semantic": "TEXCOORD0",
                         "systemValue": "NoBuiltin", "type": "Float", "components": 2,
                         "byteOffset": 16},
                    ],
                    "position": {"name": "SV_Position", "wNonPositiveRows": 0,
                                 "ndcMin": [-1.0, -1.0, 0.0], "ndcMax": [3.0, 1.0, 0.0]},
                    "rows": page,
                },
                next_cursor=next_cursor,
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
