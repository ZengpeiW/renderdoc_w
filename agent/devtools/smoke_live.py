"""Live smoke test for the built-in bridge inside a running qrendertest.

Usage:
    python devtools/smoke_live.py [--root DIR] [--instance-id ID] [--expect-capture] [--wait 30]
    python devtools/smoke_live.py --all-p0 [--launch FILE.rdc] [--exe qrendertest.exe]

Without ``--all-p0`` it checks ping/get_status/get_context. With ``--all-p0`` it calls every P0
tool against the loaded capture and checks that read tools leave the UI event alone, that paging
has no duplicates, that ``set_event`` moves the UI and that exported PNGs match the texture size.
``--launch`` starts qrendertest with the capture first and closes it with WM_CLOSE afterwards.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = AGENT_DIR.parent
SOURCE_DIR = AGENT_DIR / "mcp_server" / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from rendertest_mcp.errors import RenderTestAgentError  # noqa: E402
from rendertest_mcp.ipc_client import BridgeClient  # noqa: E402

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
WM_CLOSE = 0x0010


def close_windows(pid: int) -> int:
    user32 = ctypes.windll.user32
    closed = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            closed.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return len(closed)


def png_size(path: Path) -> tuple[int, int] | None:
    header = path.read_bytes()[:24]
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", header[16:24])


class P0Smoke:
    def __init__(self, client: BridgeClient, instance_id: str):
        self.client = client
        self.instance_id = instance_id
        self.failures: list[str] = []
        self.timings: list[tuple[str, float, int]] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)
        print("FAIL:", message, file=sys.stderr)

    def call(self, method: str, params: dict[str, Any] | None = None, *, expect_error: str | None = None,
             timeout: float | None = None) -> dict[str, Any] | None:
        start = time.perf_counter()
        try:
            response = self.client.call(method, params or {}, instance_id=self.instance_id,
                                        timeout=timeout)
        except RenderTestAgentError as error:
            elapsed = (time.perf_counter() - start) * 1000
            self.timings.append((method, elapsed, 0))
            if expect_error == error.code or error.code == "UNSUPPORTED":
                print(f"  {method}: {error.code} ({elapsed:.0f} ms) as expected")
            else:
                self.fail(f"{method}({params}) -> {error.code}: {error}")
            return None
        elapsed = (time.perf_counter() - start) * 1000
        size = len(json.dumps(response.result, ensure_ascii=False).encode("utf-8"))
        self.timings.append((method, elapsed, size))
        if expect_error:
            self.fail(f"{method}({params}) succeeded but {expect_error} was expected")
        if size > MAX_RESPONSE_BYTES:
            self.fail(f"{method} response is {size} bytes")
        for warning in response.result.get("warnings") or []:
            print(f"  {method} warning: {warning}")
        print(f"  {method}: ok ({elapsed:.0f} ms, {size} bytes)")
        return response.result

    def ui_events(self) -> tuple[Any, Any]:
        data = self.call("get_context")["data"]
        return data.get("selectedEventId"), data.get("currentEventId")

    def page_all(self, method: str, params: dict[str, Any], key: str, id_key: str) -> list[dict]:
        items: list[dict] = []
        cursor = None
        while True:
            page_params = dict(params, limit=params.get("limit", 50))
            if cursor:
                page_params["cursor"] = cursor
            result = self.call(method, page_params)
            if result is None:
                return items
            items.extend(result["data"][key])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        ids = [item[id_key] for item in items]
        if len(ids) != len(set(ids)):
            self.fail(f"{method} paging returned duplicate {id_key}s")
        total = result["data"].get("total")
        if total is not None and total != len(items):
            self.fail(f"{method} paging returned {len(items)} items but total is {total}")
        return items

    def pick_draw(self, max_draws: int) -> tuple[int, dict] | None:
        draws = self.call("find_actions", {"query": "", "flags": ["draw"], "limit": 1000})
        if not draws or not draws["data"]["actions"]:
            self.fail("find_actions(flags=[draw]) found no draws")
            return None
        candidates = draws["data"]["actions"]
        print(f"  {draws['data']['total']} draws; probing up to {max_draws} for a colour target")
        step = max(1, len(candidates) // max_draws)
        for action in candidates[::step][:max_draws]:
            inspected = self.call("inspect_event", {"event_id": action["eventId"]})
            if inspected and inspected["data"]["pipeline"].get("outputTargets"):
                return action["eventId"], inspected["data"]
        self.fail("no probed draw had a bound colour target")
        return None

    def run(self, max_draws: int) -> None:
        status = self.call("get_status")
        if not status or not status["data"]["capture"]:
            self.fail("get_status reports no capture")
            return
        print("capture:", json.dumps(status["data"]["capture"], ensure_ascii=False))
        before = self.ui_events()

        print("[actions]")
        roots = self.page_all("list_actions", {"limit": 7}, "actions", "eventId")
        if not roots:
            self.fail("list_actions returned no root actions")
        parent = next((item for item in roots if item["childCount"]), None)
        if parent:
            self.page_all("list_actions", {"parent_event_id": parent["eventId"], "limit": 7},
                          "actions", "eventId")
        self.call("list_actions", {"parent_event_id": 10 ** 9}, expect_error="INVALID_EVENT")
        picked = self.pick_draw(max_draws)
        if picked is None:
            return
        event_id, inspected = picked
        pipeline = inspected["pipeline"]
        target = pipeline["outputTargets"][0]
        print(f"  draw EID {event_id}: {inspected['action']['name']}; RT {target['resourceId']} "
              f"{target.get('format')} {target.get('width')}x{target.get('height')}")

        print("[pipeline]")
        state = self.call("get_pipeline_state", {"event_id": event_id})
        stages = list(pipeline["shaders"])
        for stage in stages:
            self.call("get_shader", {"event_id": event_id, "stage": stage,
                                     "include_disassembly": stage == "pixel"}, timeout=120)
        cbuffer_stage = next((stage for stage in ("pixel", "vertex", "mesh", "compute") if stage in stages
                              and (state or {}).get("data", {}).get("bindings", {}).get(stage, {})
                              .get("constantBlocks")), None)
        if cbuffer_stage:
            self.call("get_cbuffer", {"event_id": event_id, "stage": cbuffer_stage, "index": 0})
        else:
            print("  no constant block bound; skipping get_cbuffer")
        self.call("get_shader", {"event_id": event_id, "stage": "compute"},
                  expect_error="INVALID_EVENT")

        print("[resources]")
        textures = self.call("list_resources", {"kind": "texture", "limit": 20})
        buffers = self.page_all("list_resources", {"kind": "buffer", "limit": 500}, "resources",
                                "resourceId")
        large_buffer = next((item["resourceId"] for item in buffers
                             if item.get("byteLength", 0) > 128 * 1024), None)
        rt_id = target["resourceId"]
        self.call("get_resource", {"resource_id": rt_id})
        self.call("get_resource", {"resource_id": "18446744073709551615"},
                  expect_error="INVALID_RESOURCE")
        usage = self.call("get_resource_usage", {"resource_id": rt_id, "limit": 50})
        if usage and not usage["data"]["usages"]:
            self.fail(f"render target {rt_id} has no recorded usage")
        if textures is not None and not textures["data"]["resources"]:
            self.fail("list_resources(kind=texture) returned nothing")

        print("[textures]")
        width, height = int(target.get("width") or 1), int(target.get("height") or 1)
        picked_pixel = self.call("pick_pixel", {"resource_id": rt_id, "event_id": event_id,
                                                "x": width // 2, "y": height // 2})
        if picked_pixel:
            print("  pixel:", json.dumps({key: value for key, value in picked_pixel["data"].items()
                                          if key not in ("texture",)}))
        self.call("pick_pixel", {"resource_id": rt_id, "event_id": event_id, "x": width, "y": 0},
                  expect_error="INVALID_REQUEST")
        if picked_pixel:
            self.check_pixel_history(rt_id, event_id, width // 2, height // 2,
                                     picked_pixel["data"]["value"])
        self.call("pixel_history", {"resource_id": rt_id, "event_id": event_id, "x": width,
                                    "y": 0}, expect_error="INVALID_REQUEST")
        if "pixel" in pipeline["shaders"]:
            self.check_debug_pixel(rt_id, event_id, width // 2, height // 2)
        if "vertex" in pipeline["shaders"]:
            self.check_debug_vertex(event_id, 0)
            self.call("debug_vertex", {"event_id": event_id,
                                       "vertex": int(inspected["action"]["numIndices"])},
                      expect_error="INVALID_REQUEST")
        stats = self.call("get_texture_stats", {"resource_id": rt_id, "event_id": event_id})
        if stats:
            print("  min:", json.dumps(stats["data"]["min"]), "max:", json.dumps(stats["data"]["max"]))
        exported = self.call("export_texture", {"resource_id": rt_id, "event_id": event_id},
                             timeout=120)
        if exported:
            artifact = exported["artifacts"][0]
            size = png_size(Path(artifact["path"]))
            if size != (width, height):
                self.fail(f"exported PNG is {size}, expected {(width, height)}")
            else:
                print(f"  PNG {artifact['path']} {size[0]}x{size[1]}")
        rgb = self.call("export_texture", {"resource_id": rt_id, "event_id": event_id,
                                           "channels": "rgb"}, timeout=120)
        if rgb:
            artifact = rgb["artifacts"][0]
            header = Path(artifact["path"]).read_bytes()[:26]
            if png_size(Path(artifact["path"])) != (width, height) or header[25] != 2:
                self.fail(f"rgb export is not a {width}x{height} RGB PNG: {header[16:26].hex()}")
            elif exported and artifact["sha256"] == exported["artifacts"][0]["sha256"]:
                self.fail("rgb export is identical to the rgba export")
            else:
                print(f"  RGB PNG {artifact['path']}")
        depth = pipeline.get("depthTarget")
        if depth and depth.get("resourceId"):
            self.call("export_texture", {"resource_id": depth["resourceId"], "event_id": event_id,
                                         "channels": "depth"}, timeout=120)

        print("[buffers]")
        buffer = self.first_buffer(state)
        if buffer:
            self.call("get_buffer_data", {"resource_id": buffer, "event_id": event_id,
                                          "size": 64, "format": "float4"})
            self.call("get_buffer_data", {"resource_id": buffer, "event_id": event_id,
                                          "size": 256 * 1024}, timeout=120)
        else:
            print("  no buffer bound at the draw; skipping get_buffer_data")
        if large_buffer:
            large = self.call("get_buffer_data", {"resource_id": large_buffer, "event_id": event_id,
                                                  "size": 128 * 1024}, timeout=120)
            if large and not large["artifacts"]:
                self.fail("a 128 KiB buffer read did not produce an artifact")
            elif large:
                print(f"  buffer artifact {large['artifacts'][0]['path']}")

        print("[messages]")
        self.page_all("get_debug_messages", {"limit": 200}, "messages", "index")

        after = self.ui_events()
        if after != before:
            self.fail(f"read tools changed the UI event: {before} -> {after}")
        else:
            print(f"  UI event unchanged by read tools: selected/current {after}")

        print("[set_event]")
        moved = self.call("set_event", {"event_id": event_id})
        if moved:
            print(f"  {moved['data']['eventIdBefore']} -> {moved['data']['eventIdAfter']}")
        selected, current = self.ui_events()
        if (selected, current) != (event_id, event_id):
            self.fail(f"after set_event the UI shows {selected}/{current}, expected {event_id}")
        if before[1] is not None:
            self.call("set_event", {"event_id": before[1]})

    def check_pixel_history(self, resource_id: str, event_id: int, x: int, y: int,
                            expected: list[Any] | None = None) -> dict | None:
        params = {"resource_id": resource_id, "event_id": event_id, "x": x, "y": y}
        first = self.call("pixel_history", dict(params, limit=1000), timeout=300)
        if first is None:
            return None
        data = first["data"]
        summary = data["summary"]
        print(f"  pixel_history {resource_id} @EID {event_id} ({x},{y}):",
              json.dumps(summary, ensure_ascii=False))
        for item in data["events"][:6]:
            print("   ", json.dumps({key: item[key] for key in (
                "eventId", "actionName", "fragmentCount", "passedCount", "failureCounts",
                "changed")}, ensure_ascii=False),
                  "post", json.dumps((item["postMod"] or {}).get("color")))
        events = [item["eventId"] for item in data["events"]]
        if events != sorted(set(events), reverse=True) or any(item > event_id for item in events):
            self.fail(f"pixel_history events are not newest-first up to {event_id}: {events[:10]}")
        if data["total"] != len(events):
            self.fail(f"pixel_history total {data['total']} != {len(events)} events")
        if 1 < data["total"] <= 40:
            oldest = self.page_all("pixel_history", dict(params, order="oldest", limit=1),
                                   "events", "eventId")
            if oldest != data["events"][::-1]:
                self.fail("pixel_history oldest-first paging does not mirror newest-first")
        detailed = self.call("pixel_history", dict(params, include_fragments=True, limit=1))
        if detailed and detailed["data"]["events"]:
            newest = detailed["data"]["events"][0]
            fragments = newest.get("fragments") or []
            if len(fragments) != min(newest["fragmentCount"], 64):
                self.fail(f"pixel_history returned {len(fragments)} fragments for EID "
                          f"{newest['eventId']} with {newest['fragmentCount']}")
            passed = [item for item in fragments if item["passed"]]
            print(f"  EID {newest['eventId']}: {len(fragments)} fragments, {len(passed)} passed; "
                  f"primitives {newest['primitiveIds']}")
        final = summary.get("finalValue")
        later_discard = any(item > (summary.get("lastEventId") or -1)
                            for item in data.get("discardEventIds") or [])
        if expected is not None and final is not None and not later_discard:
            if not all(abs(float(a) - float(b)) < 1e-3
                       for a, b in zip(final["color"], expected) if isinstance(a, (int, float))):
                self.fail(f"pixel_history final value {final['color']} != pick_pixel {expected}")
            else:
                print(f"  pixel_history final value matches pick_pixel {expected}")
        return data

    def check_debug_pixel(self, resource_id: str, event_id: int, x: int, y: int) -> None:
        history = self.call("pixel_history", {"resource_id": resource_id, "event_id": event_id,
                                              "x": x, "y": y, "limit": 1,
                                              "include_fragments": True}, timeout=300)
        events = (history or {}).get("data", {}).get("events") or []
        newest = events[0] if events and events[0]["eventId"] == event_id else None
        passed = [item for item in (newest or {}).get("fragments") or [] if item["passed"]]
        params: dict[str, Any] = {"event_id": event_id, "x": x, "y": y}
        if passed:
            params["primitive"] = passed[-1]["primitiveId"]
        before = self.ui_events()
        result = self.call("debug_pixel", params, timeout=300)
        if self.ui_events() != before:
            self.fail("debug_pixel changed the UI event")
        if result is None:
            return
        data = result["data"]
        print(f"  debug_pixel EID {event_id} ({x},{y}) primitive {params.get('primitive')}: "
              f"debugged={data['debugged']} steps={data.get('steps')} "
              f"complete={data.get('executionComplete')}")
        if not data["debugged"]:
            if passed:
                self.fail(f"debug_pixel found no fragment but pixel_history has {len(passed)}")
            return
        print("   shader:", json.dumps(data["shader"], ensure_ascii=False))
        print("   outputs:", json.dumps(data["outputs"], ensure_ascii=False))
        print(f"   inputs {len(data['inputs'])}, nanOrInf steps {len(data['nanOrInfSteps'])}, "
              f"sample steps {data['sampleSteps']}")
        artifact = result["artifacts"][0]
        lines = Path(artifact["path"]).read_text(encoding="utf-8").splitlines()
        summary = json.loads(lines[-1])
        if summary.get("record") != "summary" or summary.get("steps") != data["steps"]:
            self.fail(f"debug trace summary {summary} does not match {data['steps']} steps")
        if len(lines) != data["steps"] + 2 and data["traceComplete"]:
            self.fail(f"debug trace has {len(lines)} lines for {data['steps']} steps")
        print(f"   trace {artifact['path']} {artifact['bytes']} bytes")
        if not passed or not data["outputs"]:
            return
        target = next((item for item in data["outputs"] if item["signatureIndex"] == 0
                       or item["name"].upper().endswith("TARGET0")), data["outputs"][0])
        shader_out = (passed[-1].get("shaderOut") or {}).get("color") or []
        count = min(len(shader_out), len(target["value"]))
        if count and all(isinstance(target["value"][index], (int, float)) and
                         abs(float(target["value"][index]) - float(shader_out[index])) < 2e-3
                         for index in range(count)):
            print(f"   {target['name']} matches pixel_history shaderOut {shader_out[:count]}")
        else:
            print(f"   NOTE: {target['name']} {target['value']} vs pixel_history shaderOut "
                  f"{shader_out} (UNORM targets quantise the shader output)")

    def expected_vertex_index(self, event_id: int, vertex: int) -> int | None:
        action = self.call("inspect_event", {"event_id": event_id})["data"]["action"]
        if "Indexed" not in action["flags"]:
            return vertex
        state = self.call("get_pipeline_state", {"event_id": event_id,
                                                 "include_bindings": False})
        buffer = ((state or {}).get("data", {}).get("inputAssembly") or {}).get("indexBuffer")
        if not buffer:
            self.fail(f"EID {event_id} is indexed but has no index buffer")
            return None
        stride = buffer["byteStride"]
        offset = buffer["byteOffset"] + (action["indexOffset"] + vertex) * stride
        raw = self.call("get_buffer_data", {"resource_id": buffer["resourceId"],
                                            "offset": offset, "size": stride,
                                            "event_id": event_id})
        if raw is None:
            return None
        value = int.from_bytes(bytes.fromhex(raw["data"]["hex"]), "little")
        return max(0, value + action["baseVertex"])

    def check_debug_vertex(self, event_id: int, vertex: int, instance: int = 0) -> None:
        expected = self.expected_vertex_index(event_id, vertex)
        before = self.ui_events()
        result = self.call("debug_vertex", {"event_id": event_id, "vertex": vertex,
                                            "instance": instance}, timeout=300)
        if self.ui_events() != before:
            self.fail("debug_vertex changed the UI event")
        if result is None:
            return
        data = result["data"]
        print(f"  debug_vertex EID {event_id} vertex {vertex} instance {instance}: "
              f"index={data.get('index')} (raw {data.get('indexBufferValue')}, "
              f"get_buffer_data {expected}) debugged={data['debugged']} "
              f"steps={data.get('steps')} complete={data.get('executionComplete')}")
        if expected is not None and data.get("index") != expected:
            self.fail(f"debug_vertex index {data.get('index')} != index buffer {expected}")
        if not data["debugged"]:
            self.fail(f"debug_vertex could not debug vertex {vertex} at EID {event_id}")
            return
        print("   shader:", json.dumps(data["shader"], ensure_ascii=False))
        print("   inputs:", json.dumps([{key: item.get(key) for key in ("name", "value")}
                                        for item in data["inputs"]], ensure_ascii=False)[:1200])
        print("   outputs:", json.dumps(data["outputs"], ensure_ascii=False)[:1200])
        position = next((item for item in data["outputs"]
                         if item["name"].upper() in ("SV_POSITION", "GL_POSITION")
                         or item["name"].upper().endswith("POSITION")), None)
        if position is None:
            self.fail("debug_vertex outputs have no position")
        elif any(not isinstance(item, (int, float)) for item in position["value"]):
            self.fail(f"debug_vertex position is not finite: {position['value']}")
        elif position["value"][3] != 0:
            ndc = [round(item / position["value"][3], 4) for item in position["value"][:3]]
            print(f"   position NDC {ndc}")
        artifact = result["artifacts"][0]
        lines = Path(artifact["path"]).read_text(encoding="utf-8").splitlines()
        summary = json.loads(lines[-1])
        if summary.get("record") != "summary" or summary.get("steps") != data["steps"]:
            self.fail(f"vertex trace summary {summary} does not match {data['steps']} steps")
        if len(lines) != data["steps"] + 2 and data["traceComplete"]:
            self.fail(f"vertex trace has {len(lines)} lines for {data['steps']} steps")
        print(f"   trace {artifact['path']} {artifact['bytes']} bytes")

    def first_buffer(self, state: dict | None) -> str | None:
        data = (state or {}).get("data", {})
        assembly = data.get("inputAssembly") or {}
        for buffer in assembly.get("vertexBuffers") or []:
            return buffer["resourceId"]
        if assembly.get("indexBuffer"):
            return assembly["indexBuffer"]["resourceId"]
        for bindings in (data.get("bindings") or {}).values():
            for block in bindings.get("constantBlocks") or []:
                if block.get("resourceId"):
                    return block["resourceId"]
        return None

    def report(self) -> None:
        print("[timings]")
        slowest: dict[str, tuple[float, int]] = {}
        for method, elapsed, size in self.timings:
            if method not in slowest or elapsed > slowest[method][0]:
                slowest[method] = (elapsed, size)
        for method, (elapsed, size) in sorted(slowest.items()):
            print(f"  {method:<20} max {elapsed:8.0f} ms {size:>10} bytes")


def wait_for_instance(client: BridgeClient, args, pid: int | None) -> dict | None:
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        instances = [
            item
            for item in client.list_instances()
            if (args.instance_id is None or item["instanceId"] == args.instance_id)
            and (pid is None or item.get("pid") == pid)
        ]
        if instances and (not args.expect_capture or instances[0].get("captureLoaded")):
            return instances[0]
        time.sleep(0.5)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test a live RenderTest agent bridge.")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--expect-capture", action="store_true")
    parser.add_argument("--wait", type=float, default=30.0)
    parser.add_argument("--all-p0", action="store_true")
    parser.add_argument("--max-draws", type=int, default=40)
    parser.add_argument("--pixel-history", action="append", default=[],
                        metavar="RESOURCE:EID:X:Y",
                        help="Also print pixel_history for this texel (repeatable).")
    parser.add_argument("--debug-pixel", action="append", default=[],
                        metavar="RESOURCE:EID:X:Y",
                        help="Also run debug_pixel for this pixel (repeatable).")
    parser.add_argument("--debug-vertex", action="append", default=[],
                        metavar="EID:VERTEX[:INSTANCE]",
                        help="Also run debug_vertex for this vertex (repeatable).")
    parser.add_argument("--launch", type=Path, help="Start qrendertest with this capture first.")
    parser.add_argument("--exe", type=Path, default=REPO_DIR / "x64" / "Release" / "qrendertest.exe")
    args = parser.parse_args(argv)

    process = None
    if args.launch:
        args.root = (args.root or AGENT_DIR / ".agent-data" / "live").resolve()
        args.root.mkdir(parents=True, exist_ok=True)
        args.expect_capture = True
        args.wait = max(args.wait, 300.0)
        environ = dict(os.environ, RENDERTEST_AGENT_ROOT=str(args.root))
        environ.pop("RENDERTEST_AGENT_BRIDGE", None)
        process = subprocess.Popen([str(args.exe), str(args.launch.resolve())], env=environ)
        print(f"launched pid {process.pid}")

    client = BridgeClient(root=args.root)
    try:
        instance = wait_for_instance(client, args, process.pid if process else None)
        if instance is None:
            print(f"No matching online instance under {client.root} within {args.wait}s",
                  file=sys.stderr)
            return 1
        instance_id = instance["instanceId"]
        print("instance:", json.dumps(instance, ensure_ascii=False))
        if args.all_p0:
            smoke = P0Smoke(client, instance_id)
            smoke.run(args.max_draws)
            for spec in args.pixel_history:
                resource_id, event_id, x, y = spec.split(":")
                print(f"[pixel_history {spec}]")
                smoke.check_pixel_history(resource_id, int(event_id), int(x), int(y))
            for spec in args.debug_pixel:
                resource_id, event_id, x, y = spec.split(":")
                print(f"[debug_pixel {spec}]")
                smoke.check_debug_pixel(resource_id, int(event_id), int(x), int(y))
            for spec in args.debug_vertex:
                parts = [int(item) for item in spec.split(":")]
                print(f"[debug_vertex {spec}]")
                smoke.check_debug_vertex(*parts)
            smoke.report()
            print("PASS" if not smoke.failures else f"{len(smoke.failures)} check(s) failed")
            return 0 if not smoke.failures else 1
        try:
            for method in ("ping", "get_status", "get_context"):
                response = client.call(method, instance_id=instance_id)
                print(f"{method}:", json.dumps(response.result["data"], ensure_ascii=False))
        except RenderTestAgentError as error:
            print(f"bridge error: {error.code}: {error}", file=sys.stderr)
            return 1
        return 0
    finally:
        if process is not None:
            close_windows(process.pid)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
            print("qrendertest exit code:", process.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
