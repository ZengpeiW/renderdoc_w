"""Structural action statistics, not GPU timings or a count of executed primitives."""

import heapq

from ..ipc import AgentBridgeError
from ..paging import paginate, query_key
from .common import action_index, get_int, result, session_of


MAX_NAME_CHARS = 256
MAX_PATH_NAMES = 64
OPERATION_FLAGS = (
    ("draw", ("Drawcall",)),
    ("dispatch", ("Dispatch",)),
    ("meshDispatch", ("MeshDispatch",)),
    ("rayDispatch", ("DispatchRay",)),
    ("copy", ("Copy",)),
    ("resolve", ("Resolve",)),
    ("clear", ("Clear", "ClearColor", "ClearDepthStencil")),
    ("present", ("Present",)),
    ("genMips", ("GenMips",)),
    ("buildAccStruct", ("BuildAccStruct",)),
)
MARKERS = ("SetMarker", "PushMarker", "PopMarker")
CONTAINERS = ("MultiAction", "CmdList", "PassBoundary", "BeginPass", "EndPass",
              "CommandBufferBoundary")
COUNTERS = ("actionCount", "operationCount", "markerCount", "containerCount",
            "unclassifiedLeafCount", "ambiguousOperationCount", "excludedParentOperationCount")
COUNTING_POLICY = {
    "scope": "The selected action and all descendants; null parent selects every root subtree.",
    "operations": "Count only leaf action records with one recognized operation category. "
                  "Markers and structural/container records are excluded even when childless. "
                  "A node with children never contributes its own operation flags. This does "
                  "not prove that an excluded parent performed no work.",
    "categories": "Operation categories are mutually exclusive. MeshDispatch takes precedence "
                  "over Drawcall/Dispatch; DispatchRay takes precedence over Dispatch. Other "
                  "conflicting categories are excluded and counted as ambiguousOperationCount.",
    "structure": "actionCount includes every action record. markerCount counts each "
                 "SetMarker/PushMarker/PopMarker record once, including a selected marker. "
                 "containerCount counts non-marker structural records or non-marker parents. "
                 "These are action-tree statistics, not counts of GPU executions.",
    "groups": "Each group is one immediate child subtree, identified by EID, not marker name. "
              "For a selected action, groups exclude the selected node itself; scope counts "
              "include it. nextCursor paginates only groups, never either top-N list.",
    "rankings": "Rank only counted regular draws and compute dispatches, using recorded "
                "ActionDescription metadata; mesh/ray dispatches are counted but not ranked. "
                "Zero values remain zero. Missing/negative dimensions or counts are unrankable. "
                "Ties are ordered by ascending EID. These estimates are not triangles, unique "
                "vertex shader invocations, shader thread counts, or GPU duration.",
    "strings": "Action names are limited to 256 characters. parentPath retains at most the "
               "last 64 ancestor/scope names. Truncation is reported explicitly.",
}


def _counts():
    return dict((name, 0) for name in COUNTERS + tuple(name for name, _ in OPERATION_FLAGS))


def _classify(node):
    flags = set(node["flags"])
    categories = set(name for name, names in OPERATION_FLAGS if flags.intersection(names))
    if "meshDispatch" in categories:
        categories.discard("draw")
        categories.discard("dispatch")
    if "rayDispatch" in categories:
        categories.discard("dispatch")
    marker = bool(flags.intersection(MARKERS))
    structural = bool(node["children"]) or bool(flags.intersection(CONTAINERS))
    counted = not marker and not structural
    category = next(iter(categories)) if counted and len(categories) == 1 else None
    counters = ["actionCount"]
    if marker:
        counters.append("markerCount")
    elif structural:
        counters.append("containerCount")
    if categories and (marker or structural):
        counters.append("excludedParentOperationCount")
    elif counted and len(categories) > 1:
        counters.append("ambiguousOperationCount")
    elif category:
        counters.extend(("operationCount", category))
    elif counted:
        counters.append("unclassifiedLeafCount")
    return counters, category


def _summary(actions, node):
    value = actions.summary(node)
    value["nameTruncated"] = len(value["name"]) > MAX_NAME_CHARS
    value["name"] = value["name"][:MAX_NAME_CHARS]
    return value


def _draw_metric(node):
    indices, instances = node.get("numIndices"), node.get("numInstances")
    if type(indices) is not int or type(instances) is not int or indices < 0 or instances < 0:
        return None
    return indices * instances


def _dispatch_metric(node):
    dims = node.get("dispatchDimension", [])
    if len(dims) != 3 or any(type(value) is not int or value < 0 for value in dims):
        return None
    return dims[0] * dims[1] * dims[2]


class _TopActions(object):
    def __init__(self, limit, metric, formula, measure):
        self.limit = limit
        self.metric = metric
        self.formula = formula
        self.measure = measure
        self.candidates = 0
        self.rankable = 0
        self.heap = []

    def add(self, node):
        self.candidates += 1
        value = self.measure(node)
        if value is None:
            return
        self.rankable += 1
        key = (value, -node["eventId"])
        if len(self.heap) < self.limit:
            heapq.heappush(self.heap, key)
        elif key > self.heap[0]:
            heapq.heapreplace(self.heap, key)

    def describe(self, actions):
        rows = []
        for value, negative_id in sorted(self.heap, reverse=True):
            node = actions.nodes[-negative_id]
            row = _summary(actions, node)
            row[self.metric] = value
            if self.metric == "indexOrVertexInvocations":
                row.update(numIndices=node["numIndices"], numInstances=node["numInstances"],
                           indexed="Indexed" in node["flags"])
            else:
                row["dispatchDimension"] = list(node["dispatchDimension"])
            rows.append(row)
        return {"metric": self.metric, "formula": self.formula, "candidateCount": self.candidates,
                "rankableCount": self.rankable, "unrankableCount": self.candidates - self.rankable,
                "returnedCount": len(rows), "truncated": len(rows) < self.rankable, "actions": rows}


def get_action_stats(runtime, context):
    params = context.params
    parent = get_int(params, "parent_event_id")
    top_n = get_int(params, "top_n", 20, minimum=1, maximum=1000, required=True)
    # Validate paging before traversing a potentially large tree. Include the full session in
    # the query key because the common cursor's separate session field is a shortened prefix.
    session = session_of(context)
    key = query_key("action-stats-v1", session, parent, top_n)
    paginate([], params, "get_action_stats", session, key)
    actions = action_index(runtime, context)
    if parent is not None and parent not in actions.nodes:
        raise AgentBridgeError("INVALID_EVENT", "Event %d is not an action." % parent,
                               details={"event_id": parent})

    totals = _counts()
    draws = _TopActions(top_n, "indexOrVertexInvocations", "numIndices * numInstances", _draw_metric)
    dispatches = _TopActions(top_n, "groupCount",
                            "dispatchDimension[0] * dispatchDimension[1] * dispatchDimension[2]",
                            _dispatch_metric)
    visited = 0

    def add(node, group_counts=None):
        counters, category = _classify(node)
        for name in counters:
            totals[name] += 1
            if group_counts is not None:
                group_counts[name] += 1
        if category == "draw":
            draws.add(node)
        elif category == "dispatch":
            dispatches.add(node)

    group_ids = actions.roots if parent is None else actions.nodes[parent]["children"]
    page_ids, next_cursor, total_groups = paginate(group_ids, params, "get_action_stats", session, key)
    page_set = set(page_ids)
    if parent is not None:
        add(actions.nodes[parent])
    groups = []
    for event_id in group_ids:
        counts = _counts() if event_id in page_set else None
        stack = [event_id]
        while stack:
            if visited % 1024 == 0:
                context.check_cancelled()
            visited += 1
            node = actions.nodes[stack.pop()]
            add(node, counts)
            stack.extend(reversed(node["children"]))
        if counts is not None:
            groups.append({"action": _summary(actions, actions.nodes[event_id]), "counts": counts})
    context.check_cancelled()
    top_draws, top_dispatches = draws.describe(actions), dispatches.describe(actions)
    path = [] if parent is None else actions.path(parent) + [actions.nodes[parent]["name"]]
    path_truncated = len(path) > MAX_PATH_NAMES or any(len(name) > MAX_NAME_CHARS for name in path)
    path = [name[:MAX_NAME_CHARS] for name in path[-MAX_PATH_NAMES:]]
    scope = _summary(actions, actions.nodes[parent]) if parent is not None else None
    returned_actions = [group["action"] for group in groups] + top_draws["actions"] + top_dispatches["actions"]
    if scope:
        returned_actions.append(scope)
    strings_truncated = path_truncated or any(row["nameTruncated"] for row in returned_actions)
    warnings = []
    if totals["excludedParentOperationCount"]:
        warnings.append("%d operation-flagged parent/container or marker records were excluded "
                        "to avoid counting both parents and descendants. This does not establish "
                        "that the excluded records performed no work."
                        % totals["excludedParentOperationCount"])
    if totals["ambiguousOperationCount"]:
        warnings.append("%d leaf records have conflicting operation categories and were excluded "
                        "from operationCount and rankings." % totals["ambiguousOperationCount"])
    if draws.candidates != draws.rankable or dispatches.candidates != dispatches.rankable:
        warnings.append("Some draw/dispatch records have invalid or missing workload metadata; "
                        "they remain counted but are excluded from rankings.")
    if strings_truncated:
        warnings.append("Some action names or parentPath entries were truncated for the response budget.")
    return result({"parentEventId": parent, "parentPath": path, "parentPathTruncated": path_truncated,
                   "scopeAction": scope, "counts": totals, "groupsTotal": total_groups, "groups": groups,
                   "topN": top_n, "topDraws": top_draws, "topDispatches": top_dispatches,
                   "countingPolicy": dict(COUNTING_POLICY)}, warnings=warnings, next_cursor=next_cursor,
                  truncated=strings_truncated or top_draws["truncated"] or top_dispatches["truncated"])


HANDLERS = {"get_action_stats": get_action_stats}
