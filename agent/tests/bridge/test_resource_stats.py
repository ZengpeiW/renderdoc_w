from __future__ import annotations

import copy
import json
import threading
import time
from types import SimpleNamespace as NS

import pytest

from fake_renderdoc import FakeController, FakeReplayManager, ResourceId, ResourceType, fmt
from rendertest_agent_bridge.indexes import SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError, RequestContext
from rendertest_agent_bridge.methods.resource_stats import get_resource_stats
from rendertest_agent_bridge.replay import ReplayRunner


class CountingController(FakeController):
    def __init__(self):
        super().__init__()
        self.enumerations = [0, 0, 0]

    def GetResources(self):
        self.enumerations[0] += 1
        return super().GetResources()

    def GetTextures(self):
        self.enumerations[1] += 1
        return super().GetTextures()

    def GetBuffers(self):
        self.enumerations[2] += 1
        return super().GetBuffers()


class Harness:
    def __init__(self):
        self.controller = CountingController()
        self.session = "a" * 32
        self.server = NS(_stop_event=threading.Event())
        self.runtime = NS(cache=SessionCache(), replay=ReplayRunner(self, lambda _line: None))
        self.runtime.replay.manager = FakeReplayManager(self.controller)

    def snapshot(self):
        return {"captureSessionId": self.session, "currentEventId": 9}

    def current_event_id(self):
        return 9

    def context(self, params):
        return RequestContext(self.server, "b" * 32, "get_resource_stats", params,
                              "read", time.time() + 10, self.snapshot())

    def call(self, **params):
        return get_resource_stats(self.runtime, self.context(params)).result


def texture(controller, key, size, name="R8G8B8A8_UNORM"):
    value = copy.copy(controller.textures[0])
    value.resourceId = ResourceId(key)
    value.byteSize = size
    value.format = fmt(name)
    return value


def groups(data, field, label):
    return {entry[label]: entry for entry in data[field]["groups"]}


def test_description_totals_scope_and_top_resources_are_locatable():
    harness = Harness()
    payload = harness.call()
    data = payload["data"]
    assert data["scope"] == "textures-and-buffers"
    assert data["resourceCount"] == data["knownSizeCount"] == 3
    assert data["knownTotalBytes"] == 80 + 64 + 128 * 1024
    assert data["unknownSizeCount"] == 0 and data["totalBytesComplete"] is True
    assert data["sizeBasis"] == "resource-description-bytes"
    assert all(word in data["sizeCaveat"] for word in ("resident", "peak", "unique", "aliased"))
    largest = data["largestResources"]
    assert [entry["resourceId"] for entry in largest] == ["200", "100", "101"]
    assert largest[0]["name"] == "Constants" and largest[0]["byteSize"] == 128 * 1024
    assert largest[1]["width"] == 4 and largest[1]["mips"] == 2
    assert largest[1]["format"] == "R8G8B8A8_UNORM" and largest[1]["textureType"] == "Texture2D"
    assert groups(data, "byKind", "kind")["texture"]["knownTotalBytes"] == 144
    assert groups(data, "byTextureFormat", "format")["D32_FLOAT"]["resourceCount"] == 1
    assert groups(data, "byTextureType", "textureType")["Texture2D"]["resourceCount"] == 2
    assert data["largestCandidates"] == data["returnedLargest"] == 3
    assert not data["hasMoreLargest"] and not payload["truncated"] and not payload["warnings"]
    assert harness.controller.current == 9 and harness.controller.frame_events == []


def test_filter_and_top_limit_do_not_change_full_aggregate_counts():
    harness = Harness()
    payload = harness.call(kind="texture", top_n=1)
    data = payload["data"]
    assert data["resourceCount"] == 2 and data["knownTotalBytes"] == 144
    assert data["largestCandidates"] == 2 and data["returnedLargest"] == 1
    assert data["hasMoreLargest"] and payload["truncated"]
    assert set(groups(data, "byKind", "kind")) == {"texture"}
    assert len(data["byTextureFormat"]["groups"]) == 2
    buffers = harness.call(kind="buffer")["data"]
    assert buffers["resourceCount"] == 1 and buffers["knownTotalBytes"] == 128 * 1024
    assert buffers["byTextureFormat"]["groups"] == buffers["byTextureType"]["groups"] == []
    assert harness.controller.enumerations == [1, 1, 1]


@pytest.mark.parametrize("params", [
    {"kind": True}, {"kind": None}, {"kind": "shader"}, {"kind": 1}, {"kind": "ALL"},
    {"top_n": True}, {"top_n": False}, {"top_n": None}, {"top_n": 0}, {"top_n": -1},
    {"top_n": 1001}, {"top_n": "20"}, {"top_n": 1.0},
])
def test_invalid_filters_fail_before_replay(params):
    harness = Harness()
    with pytest.raises(AgentBridgeError) as error:
        harness.call(**params)
    assert error.value.code == "INVALID_REQUEST"
    assert harness.controller.enumerations == [0, 0, 0]


def test_exact_decimal_ids_deduplicate_and_sort_numerically_without_float_rounding():
    harness = Harness()
    controller = harness.controller
    large = (1 << 53) + 1
    controller.buffers = []
    source = controller.textures[0]
    controller.textures = [texture(controller, key, large) for key in (large + 1, 10, large, 9)]
    controller.textures += [copy.copy(controller.textures[0])]
    controller.resources = [controller._resource(key, "same-name", ResourceType.Texture)
                            for key in (large + 1, 10, large, 9, 10)]
    controller.textures[0].width = source.width
    data = harness.call()["data"]
    assert data["resourceCount"] == 4 and data["knownTotalBytes"] == 4 * large
    assert [entry["resourceId"] for entry in data["largestResources"]] == [
        "9", "10", str(large), str(large + 1)]
    assert json.loads(json.dumps(data))["largestResources"][2]["byteSize"] == large


def test_missing_and_invalid_sizes_are_explicitly_excluded_while_real_zero_is_known():
    harness = Harness()
    controller = harness.controller
    controller.resources = []
    sizes = [None, -1, True, "99", 1.5, 1 << 64]
    controller.textures = [texture(controller, 100 + index, size) for index, size in enumerate(sizes)]
    missing = copy.copy(controller.textures[0])
    missing.resourceId = ResourceId(199)
    del missing.byteSize
    controller.textures.append(missing)
    controller.buffers = [NS(resourceId=ResourceId(200), length=0)]
    payload = harness.call()
    data = payload["data"]
    assert data["resourceCount"] == 8 and data["unknownSizeCount"] == 7
    assert data["knownSizeCount"] == 1 and data["knownTotalBytes"] == 0
    assert data["totalBytesComplete"] is False and payload["warnings"]
    assert data["largestCandidates"] == 1
    assert data["largestResources"][0]["resourceId"] == "200"
    assert data["largestResources"][0]["byteSize"] == 0
    assert all(item["byteSize"] is None for item in data["unknownSizeSamples"])
    assert {item["sizeIssue"] for item in data["unknownSizeSamples"]} == {
        "missing-size", "negative-size", "non-integer-size", "out-of-range-size"}
    texture_group = groups(data, "byKind", "kind")["texture"]
    assert texture_group["unknownSizeCount"] == 7 and texture_group["totalBytesComplete"] is False
    buffers = harness.call(kind="buffer")
    assert buffers["data"]["totalBytesComplete"] and buffers["warnings"] == []


def test_empty_resource_collection_is_a_valid_complete_zero_total():
    harness = Harness()
    harness.controller.textures = harness.controller.buffers = harness.controller.resources = []
    payload = harness.call(top_n=1000)
    data = payload["data"]
    assert data["resourceCount"] == data["knownTotalBytes"] == data["unknownSizeCount"] == 0
    assert data["totalBytesComplete"] and data["largestResources"] == []
    assert all(data[field]["totalGroups"] == 0 for field in ("byKind", "byTextureFormat", "byTextureType"))
    assert not payload["warnings"] and not payload["truncated"]


def test_uint64_max_texture_and_buffer_sizes_are_unavailable_sentinels():
    harness = Harness()
    harness.controller.textures[0].byteSize = (1 << 64) - 1
    harness.controller.buffers[0].length = (1 << 64) - 1
    payload = harness.call()
    data = payload["data"]
    assert data["resourceCount"] == 3
    assert data["knownSizeCount"] == 1 and data["unknownSizeCount"] == 2
    assert data["knownTotalBytes"] == 64 and data["totalBytesComplete"] is False
    assert data["largestCandidates"] == data["returnedLargest"] == 1
    assert [entry["resourceId"] for entry in data["largestResources"]] == ["101"]
    assert {entry["resourceId"] for entry in data["unknownSizeSamples"]} == {"100", "200"}
    assert all(entry["byteSize"] is None and entry["sizeIssue"] == "size-sentinel"
               for entry in data["unknownSizeSamples"])
    by_kind = groups(data, "byKind", "kind")
    assert by_kind["texture"]["knownTotalBytes"] == 64
    assert by_kind["buffer"]["knownTotalBytes"] == 0
    assert all(by_kind[kind]["unknownSizeCount"] == 1 and not by_kind[kind]["totalBytesComplete"]
               for kind in ("texture", "buffer"))
    assert payload["warnings"]


def test_session_change_rebuilds_index_and_old_context_cannot_read_new_capture():
    harness = Harness()
    assert harness.call()["data"]["resourceCount"] == 3
    old_context = harness.context({})
    harness.controller.resources = harness.controller.textures = harness.controller.buffers = []
    # Same session retains its immutable resource description snapshot.
    assert harness.call()["data"]["resourceCount"] == 3
    harness.session = "c" * 32
    with pytest.raises(AgentBridgeError) as error:
        get_resource_stats(harness.runtime, old_context)
    assert error.value.code == "CAPTURE_CHANGED"
    assert harness.call()["data"]["resourceCount"] == 0
    assert harness.controller.enumerations == [2, 2, 2]
    assert harness.controller.frame_events == []


def test_distribution_limits_and_truncated_labels_never_merge_different_formats():
    harness = Harness()
    controller = harness.controller
    formats = ["x" * 300 + "-A", "x" * 300 + "-B"] + ["format-%03d" % n for n in range(256)]
    controller.textures = [texture(controller, index + 1, 1000 - index, name)
                           for index, name in enumerate(formats)]
    controller.buffers = []
    controller.resources = [controller._resource(1, "name" * 300, ResourceType.Texture)]
    payload = harness.call(top_n=1)
    data = payload["data"]
    distribution = data["byTextureFormat"]
    assert distribution["totalGroups"] == 258 and distribution["returned"] == 256
    assert distribution["hasMore"] and distribution["labelsTruncated"] and payload["truncated"]
    first, second = distribution["groups"][:2]
    assert first["format"] == second["format"] and first["resourceCount"] == second["resourceCount"] == 1
    assert first["knownTotalBytes"] == 1000 and second["knownTotalBytes"] == 999
    assert data["resourceCount"] == 258 and data["knownTotalBytes"] == sum(range(743, 1001))
    assert data["largestResources"][0]["nameTruncated"]
    assert data["largestResources"][0]["formatTruncated"]


def test_unknown_size_diagnostics_are_bounded():
    harness = Harness()
    controller = harness.controller
    controller.textures = []
    controller.resources = []
    controller.buffers = [NS(resourceId=ResourceId(index + 1), length=-1) for index in range(100)]
    payload = harness.call()
    data = payload["data"]
    assert data["unknownSizeCount"] == 100 and len(data["unknownSizeSamples"]) == 20
    assert data["hasMoreUnknownSizeSamples"] and payload["truncated"]
    assert len(payload["warnings"]) == 1 and data["largestCandidates"] == 0


def test_conflicting_descriptions_count_a_resource_id_only_once_and_warn():
    harness = Harness()
    harness.controller.buffers.append(NS(resourceId=ResourceId(100), length=123))
    payload = harness.call()
    data = payload["data"]
    assert data["resourceCount"] == 3
    assert data["knownTotalBytes"] == 123 + 64 + 128 * 1024
    assert data["descriptionConflictCount"] == 1 and payload["warnings"]
    assert len([item for item in data["largestResources"] if item["resourceId"] == "100"]) == 1
