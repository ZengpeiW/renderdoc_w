from __future__ import annotations

import ast
import enum
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from rendertest_agent_bridge.indexes import SessionCache
from rendertest_agent_bridge.ipc import AgentBridgeError, json_bytes
from rendertest_agent_bridge.methods.action_stats import get_action_stats
from rendertest_agent_bridge.paging import encode_cursor


class Flags(enum.IntFlag):
    NoFlags = 0
    Clear = 0x1
    Drawcall = 0x2
    Dispatch = 0x4
    MeshDispatch = 0x8
    CmdList = 0x10
    SetMarker = 0x20
    PushMarker = 0x40
    PopMarker = 0x80
    Present = 0x100
    MultiAction = 0x200
    Copy = 0x400
    Resolve = 0x800
    GenMips = 0x1000
    PassBoundary = 0x2000
    DispatchRay = 0x4000
    BuildAccStruct = 0x8000
    Indexed = 0x10000
    ClearColor = 0x100000
    ClearDepthStencil = 0x200000
    BeginPass = 0x400000
    EndPass = 0x800000
    CommandBufferBoundary = 0x1000000


def action(eid, flags, name=None, children=None, indices=0, instances=1, dims=(0, 0, 0)):
    name = name if name is not None else "Action %d" % eid
    return NS(eventId=eid, actionId=eid, flags=flags, customName=name,
              GetName=lambda _: name, events=[NS(eventId=eid)], children=children or [],
              numIndices=indices, numInstances=instances, baseVertex=0, indexOffset=0,
              vertexOffset=0, instanceOffset=0, drawIndex=0, dispatchDimension=dims,
              dispatchThreadsDimension=(1024, 1024, 1024), copySource=None,
              copyDestination=None, outputs=[], depthOut=None)


class Controller:
    def __init__(self, roots):
        self.roots = roots
        self.builds = 0

    def GetStructuredFile(self):
        return None

    def GetRootActions(self):
        self.builds += 1
        return self.roots

    def SetFrameEvent(self, *_):
        raise AssertionError("Statistics must not move the replay event")


class Context:
    def __init__(self, params=None, session="a" * 32, cancel_after=None):
        self.params = params or {}
        self.state = {"captureSessionId": session, "currentEventId": 777}
        self.checks = 0
        self.cancel_after = cancel_after

    def check_cancelled(self):
        self.checks += 1
        if self.cancel_after is not None and self.checks >= self.cancel_after:
            raise AgentBridgeError("CANCELLED", "Cancelled test request")


class Runtime:
    def __init__(self, roots):
        self.controller = Controller(roots)
        self.cache = SessionCache()
        self.replay = NS(call=self.call)

    def call(self, context, callback):
        context.check_cancelled()
        return callback(self.controller)


def stats(runtime, session="a" * 32, **params):
    context = Context(params, session)
    response = get_action_stats(runtime, context)
    assert context.state["currentEventId"] == 777
    return response.result


def nested_tree():
    first = action(4, Flags.Drawcall | Flags.Indexed, indices=12, instances=2)
    second = action(5, Flags.Drawcall, indices=24)
    multi = action(3, Flags.MultiAction | Flags.Drawcall, children=[first, second], indices=9999)
    left = action(2, Flags.PushMarker, "Same pass", [multi])
    zero_draw = action(7, Flags.Drawcall, indices=100, instances=0)
    right = action(6, Flags.PushMarker, "Same pass", [zero_draw])
    return [action(1, Flags.PushMarker, "Frame", [left, right])]


def test_nested_markers_multi_draw_and_same_name_groups():
    runtime = Runtime(nested_tree())
    response = stats(runtime, parent_event_id=1, top_n=1)
    data, counts = response["data"], response["data"]["counts"]
    assert counts["actionCount"] == 7
    assert counts["markerCount"] == 3
    assert counts["containerCount"] == 1
    assert counts["draw"] == counts["operationCount"] == 3
    assert counts["excludedParentOperationCount"] == 1
    assert "does not establish" in " ".join(response["warnings"])
    assert [group["action"]["eventId"] for group in data["groups"]] == [2, 6]
    assert [group["action"]["name"] for group in data["groups"]] == ["Same pass"] * 2
    assert [group["counts"]["draw"] for group in data["groups"]] == [2, 1]
    assert data["parentPath"] == ["Frame"]
    assert data["scopeAction"]["eventId"] == 1
    assert data["topDraws"]["candidateCount"] == 3
    assert data["topDraws"]["returnedCount"] == 1
    assert data["topDraws"]["truncated"] is True
    winner = data["topDraws"]["actions"][0]
    assert winner["eventId"] == 4  # Equal products break ties by EID, never tree-name grouping.
    assert winner["indexed"] is True
    assert winner["indexOrVertexInvocations"] == 24
    assert response["nextCursor"] is None  # Top-N truncation never produces a groups cursor.


def test_single_draw_scope_counts_self_and_rejects_non_action_api_event():
    root = action(5, Flags.Drawcall, indices=9, instances=3)
    root.events.insert(0, NS(eventId=3))
    runtime = Runtime([root])
    data = stats(runtime, parent_event_id=5)["data"]
    assert data["counts"]["draw"] == 1
    assert data["counts"]["actionCount"] == 1
    assert data["groups"] == [] and data["groupsTotal"] == 0
    assert data["topDraws"]["actions"][0]["indexOrVertexInvocations"] == 27
    for parent in (0, 3, 1000):
        with pytest.raises(AgentBridgeError) as error:
            stats(runtime, parent_event_id=parent)
        assert error.value.code == "INVALID_EVENT"


def test_operation_categories_are_mutually_exclusive_and_structures_are_not_operations():
    values = [Flags.Drawcall, Flags.Dispatch, Flags.MeshDispatch | Flags.Drawcall | Flags.Dispatch,
              Flags.DispatchRay | Flags.Dispatch, Flags.Copy, Flags.Resolve,
              Flags.Clear | Flags.ClearColor | Flags.ClearDepthStencil, Flags.Present,
              Flags.GenMips, Flags.BuildAccStruct, Flags.NoFlags,
              Flags.SetMarker, Flags.PushMarker, Flags.PopMarker,
              Flags.MultiAction | Flags.Drawcall, Flags.CmdList,
              Flags.PassBoundary | Flags.BeginPass, Flags.EndPass,
              Flags.CommandBufferBoundary, Flags.Drawcall | Flags.Copy]
    data = stats(Runtime([action(eid, flags, indices=5, dims=(2, 3, 4))
                          for eid, flags in enumerate(values, 1)]))["data"]
    counts = data["counts"]
    names = ("draw", "dispatch", "meshDispatch", "rayDispatch", "copy", "resolve", "clear",
             "present", "genMips", "buildAccStruct")
    assert all(counts[name] == 1 for name in names)
    assert counts["operationCount"] == sum(counts[name] for name in names) == 10
    assert counts["actionCount"] == len(values)
    assert counts["markerCount"] == 3 and counts["containerCount"] == 5
    assert counts["unclassifiedLeafCount"] == 1 and counts["ambiguousOperationCount"] == 1
    assert counts["excludedParentOperationCount"] == 1
    assert data["topDraws"]["candidateCount"] == data["topDispatches"]["candidateCount"] == 1
    assert data["topDispatches"]["actions"][0]["eventId"] == 2


def test_operation_flag_on_unmarked_parent_is_conservatively_excluded():
    child = action(2, Flags.Drawcall, indices=2)
    response = stats(Runtime([action(1, Flags.Drawcall, children=[child], indices=99)]))
    counts = response["data"]["counts"]
    assert counts["draw"] == 1 and counts["containerCount"] == 1
    assert counts["excludedParentOperationCount"] == 1
    assert response["warnings"]


def test_rankings_use_recorded_products_and_report_unrankable_candidates():
    roots = [action(1, Flags.Drawcall, indices=19),
             action(2, Flags.Drawcall, indices=2, instances=10),
             action(3, Flags.Drawcall, indices=100, instances=0),
             action(4, Flags.Drawcall, indices=-1),
             action(5, Flags.Dispatch, dims=(2, 3, 4)),
             action(6, Flags.Dispatch, dims=(3, 2, 5)),
             action(7, Flags.Dispatch, dims=(1000, 0, 1000)),
             action(8, Flags.Dispatch, dims=(-1, 2, 3)),
             action(9, Flags.Dispatch, dims=(3, 4)),
             action(10, Flags.Dispatch, dims=())]
    response = stats(Runtime(roots), top_n=2)
    data = response["data"]
    assert data["counts"]["draw"] == 4 and data["counts"]["dispatch"] == 6
    draws, dispatches = data["topDraws"], data["topDispatches"]
    assert [row["eventId"] for row in draws["actions"]] == [2, 1]
    assert draws["candidateCount"] == 4 and draws["rankableCount"] == 3
    assert draws["unrankableCount"] == 1 and draws["truncated"] is True
    assert [row["eventId"] for row in dispatches["actions"]] == [6, 5]
    assert [row["groupCount"] for row in dispatches["actions"]] == [30, 24]
    assert dispatches["candidateCount"] == 6 and dispatches["rankableCount"] == 3
    assert dispatches["unrankableCount"] == 3
    assert "invalid or missing" in " ".join(response["warnings"])
    unbounded = stats(Runtime(roots))["data"]
    assert unbounded["topDraws"]["actions"][-1]["indexOrVertexInvocations"] == 0
    assert unbounded["topDispatches"]["actions"][-1]["groupCount"] == 0


def test_empty_frame_and_empty_marker_scope():
    response = stats(Runtime([]))
    data = response["data"]
    assert all(value == 0 for value in data["counts"].values())
    assert data["groups"] == [] and data["scopeAction"] is None
    assert data["topDraws"]["actions"] == data["topDispatches"]["actions"] == []
    assert response["nextCursor"] is None and response["truncated"] is False
    data = stats(Runtime([action(1, Flags.PushMarker)]), parent_event_id=1)["data"]
    assert data["counts"]["actionCount"] == data["counts"]["markerCount"] == 1
    assert data["counts"]["operationCount"] == 0 and data["groups"] == []


def test_groups_paging_binds_scope_top_n_and_complete_session_but_not_page_size():
    roots = [action(eid, Flags.Drawcall, indices=eid) for eid in (1, 2, 3)]
    runtime = Runtime(roots)
    first = stats(runtime, limit=1, top_n=1)
    second = stats(runtime, limit=2, top_n=1, cursor=first["nextCursor"])
    assert [group["action"]["eventId"] for group in first["data"]["groups"]] == [1]
    assert [group["action"]["eventId"] for group in second["data"]["groups"]] == [2, 3]
    assert first["data"]["counts"] == second["data"]["counts"]
    assert first["data"]["topDraws"] == second["data"]["topDraws"]
    assert first["data"]["groupsTotal"] == second["data"]["groupsTotal"] == 3
    assert second["nextCursor"] is None
    for params in ({"parent_event_id": 1, "top_n": 1}, {"top_n": 2},
                   {"top_n": 1, "session": "a" * 16 + "b" * 16}):
        with pytest.raises(AgentBridgeError) as error:
            stats(runtime, cursor=first["nextCursor"], **params)
        assert error.value.code == "INVALID_REQUEST"
    with pytest.raises(AgentBridgeError) as error:
        stats(runtime, cursor=encode_cursor("list_actions", "a" * 32, "x", 0))
    assert error.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("field,value", [(field, value) for field in ("top_n", "limit")
                                         for value in (0, 1001, True, 1.5, None)])
def test_invalid_limits(field, value):
    with pytest.raises(AgentBridgeError) as error:
        stats(Runtime([]), **{field: value})
    assert error.value.code == "INVALID_REQUEST"


def test_action_index_cache_is_reused_then_invalidated_for_new_capture_session():
    runtime = Runtime([action(1, Flags.Drawcall, indices=3)])
    assert stats(runtime)["data"]["counts"]["draw"] == 1
    runtime.controller.roots = [action(2, Flags.Dispatch, dims=(1, 2, 3))]
    assert stats(runtime)["data"]["counts"]["draw"] == 1
    assert runtime.controller.builds == 1
    changed = stats(runtime, session="b" * 32)["data"]
    assert changed["counts"]["draw"] == 0 and changed["counts"]["dispatch"] == 1
    assert changed["groups"][0]["action"]["eventId"] == 2
    assert runtime.controller.builds == 2


def test_deep_trees_do_not_recurse_and_requests_can_be_cancelled():
    root = action(1501, Flags.Drawcall, indices=7)
    for eid in range(1500, 0, -1):
        root = action(eid, Flags.PushMarker, children=[root])
    runtime = Runtime([root])
    data = stats(runtime)["data"]
    assert data["counts"]["markerCount"] == 1500 and data["counts"]["draw"] == 1
    with pytest.raises(AgentBridgeError) as error:
        get_action_stats(runtime, Context(cancel_after=3))
    assert error.value.code == "CANCELLED"


def test_names_paths_and_maximum_pages_stay_inside_response_budget():
    long_name = "\U0001f600" * 10000
    roots = [action(eid, Flags.Drawcall if eid <= 1000 else Flags.Dispatch, long_name,
                    indices=eid, dims=(1, 2, 3)) for eid in range(1, 2001)]
    response = stats(Runtime(roots), limit=1000, top_n=1000)
    assert len(json_bytes(response)) < 8 * 1024 * 1024
    assert response["truncated"] is True and response["warnings"]
    assert all(len(group["action"]["name"]) == 256 and group["action"]["nameTruncated"]
               for group in response["data"]["groups"])
    root = action(70, Flags.Drawcall, long_name, indices=1)
    for eid in range(69, 0, -1):
        root = action(eid, Flags.PushMarker, long_name, [root])
    data = stats(Runtime([root]), parent_event_id=70)["data"]
    assert data["parentPathTruncated"] is True and len(data["parentPath"]) == 64
    assert all(len(name) == 256 for name in data["parentPath"])


def test_new_bridge_module_keeps_python_36_syntax():
    path = Path(__file__).resolve().parents[2] / "bridge" / "rendertest_agent_bridge" / "methods" / "action_stats.py"
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 6))
