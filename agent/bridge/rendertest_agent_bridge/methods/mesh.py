import math
import struct

from ..ipc import AgentBridgeError
from ..paging import decode_cursor, encode_cursor, query_key
from ..values import enum_name, number, resource_id
from .common import (action_index, get_choice, get_int, invalid, renderdoc_module, resolve_event,
                     result, session_of)

STAGES = ("vs_out", "gs_out")
DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
MAX_DECODED_ROWS = 262144
MAX_VERTEX_BYTES = 64 * 1024 * 1024
INDEX_CODES = {1: "B", 2: "H", 4: "I"}
WIDE_TYPES = {"Double": "d", "ULong": "Q", "SLong": "q", "GPUPointer": "Q"}
NARROW_TYPES = {"Float": "f", "Half": "f", "UInt": "I", "UShort": "I", "UByte": "I",
                "Bool": "I", "Enum": "I", "SInt": "i", "SShort": "i", "SByte": "i"}


def _align(offset, alignment):
    return (offset + alignment - 1) // alignment * alignment


def draw_indices(controller, pipe, node, first, count):
    """Index buffer values for vertices [first, first + count) of an indexed draw.

    Values are raw (no base vertex, see ``with_base``); primitive restart entries are None. The
    list is shorter than ``count`` when the bound index buffer ends early.
    """
    buffer = pipe.GetIBuffer()
    stride = int(buffer.byteStride)
    if stride not in INDEX_CODES:
        raise AgentBridgeError("INVALID_EVENT", "The indexed draw has no usable index buffer.",
                               details={"byteStride": stride})
    offset = (node["indexOffset"] + first) * stride
    size = int(buffer.byteSize)
    if size:
        count = max(0, min(count, (size - offset) // stride))
    if not count:
        return []
    data = bytes(controller.GetBufferData(buffer.resourceId, int(buffer.byteOffset) + offset,
                                          count * stride))
    count = len(data) // stride
    values = struct.unpack_from("<%d%s" % (count, INDEX_CODES[stride]), data)
    restart = None
    if pipe.IsRestartEnabled():
        restart = int(pipe.GetRestartIndex()) & ((1 << (8 * stride)) - 1)
    return [None if restart and value == restart else value for value in values]


def with_base(node, value):
    """Apply the draw's base vertex as qrenderdoc's Mesh Viewer does (clamped at 0)."""
    return None if value is None else max(0, value + node["baseVertex"])


def _columns(reflection, stream, aligned):
    """Post-transform columns laid out like qrenderdoc's Mesh Viewer, position first."""
    columns = []
    position = None
    for sig in reflection.outputSignature:
        if int(getattr(sig, "stream", 0)) != stream:
            continue
        builtin = enum_name(sig.systemValue)
        if builtin == "OutputIndices":
            continue
        var_type = enum_name(sig.varType)
        semantic = getattr(sig, "semanticIdxName", "") or sig.semanticName
        if builtin == "Position" and position is None:
            position = len(columns)
        columns.append({"name": sig.varName or semantic, "semantic": semantic,
                        "systemValue": builtin, "type": var_type,
                        "components": int(sig.compCount),
                        "code": WIDE_TYPES.get(var_type) or NARROW_TYPES.get(var_type),
                        "size": 8 if var_type in WIDE_TYPES else 4})
    if position:
        columns.insert(0, columns.pop(position))
    offset = 0
    for column in columns:
        count = column["components"]
        if aligned and count == 2:
            offset = _align(offset, 2 * column["size"])
        elif aligned and count > 2:
            offset = _align(offset, 4 * column["size"])
        column["byteOffset"] = offset
        offset += count * column["size"]
    return columns, offset


def _decode(columns, data, base):
    values = []
    for column in columns:
        start = base + column["byteOffset"]
        end = start + column["components"] * column["size"]
        if column["code"] is None or end > len(data):
            values.append(None)
        else:
            values.append(struct.unpack_from("<%d%s" % (column["components"], column["code"]),
                                             data, start))
    return values


def _finite(value):
    return not isinstance(value, float) or math.isfinite(value)


def _stats(columns, rows):
    stats = []
    for position, column in enumerate(columns):
        low = [None] * column["components"]
        high = [None] * column["components"]
        non_finite = 0
        for row in rows:
            values = row[2][position] if row[2] else None
            if values is None:
                continue
            if not all(_finite(item) for item in values):
                non_finite += 1
            for component, item in enumerate(values):
                if not _finite(item):
                    continue
                if low[component] is None or item < low[component]:
                    low[component] = item
                if high[component] is None or item > high[component]:
                    high[component] = item
        stats.append({"name": column["name"], "min": [number(item) for item in low],
                      "max": [number(item) for item in high], "nonFiniteRows": non_finite})
    return stats


def _position_stats(columns, rows):
    if not columns or columns[0]["systemValue"] != "Position" or columns[0]["components"] != 4:
        return None
    low = [None] * 3
    high = [None] * 3
    behind = 0
    for row in rows:
        values = row[2][0] if row[2] else None
        if values is None or not all(_finite(item) for item in values):
            continue
        if values[3] <= 0.0:
            behind += 1
            continue
        for component in range(3):
            item = values[component] / values[3]
            if low[component] is None or item < low[component]:
                low[component] = item
            if high[component] is None or item > high[component]:
                high[component] = item
    return {"name": columns[0]["name"], "wNonPositiveRows": behind,
            "ndcMin": [number(item) for item in low], "ndcMax": [number(item) for item in high]}


def _build(controller, rd, node, stage, instance, view):
    pipe = controller.GetPipelineState()
    if stage == "vs_out":
        reflection = pipe.GetShaderReflection(rd.ShaderStage.Vertex)
        stream = 0
        data_stage = rd.MeshDataStage.VSOut
        if reflection is None:
            raise AgentBridgeError("INVALID_EVENT", "No vertex shader is bound at event %d."
                                   % node["eventId"])
    else:
        reflection = (pipe.GetShaderReflection(rd.ShaderStage.Geometry) or
                      pipe.GetShaderReflection(rd.ShaderStage.Domain))
        stream = max(0, int(getattr(pipe, "GetRasterizedStream", lambda: 0)()))
        data_stage = rd.MeshDataStage.GSOut
        if reflection is None:
            raise invalid("Event %d has no geometry or domain shader; use stage=vs_out."
                          % node["eventId"])
    views = int(getattr(pipe, "MultiviewBroadcastCount", lambda: 1)() or 1)
    if view >= views:
        raise invalid("view must be below the multiview count %d." % views)
    aligned = stage == "vs_out" and bool(
        getattr(pipe, "HasAlignedPostVSData", lambda value: False)(rd.MeshDataStage.VSOut))
    columns, needed = _columns(reflection, stream, aligned)
    mesh = controller.GetPostVSData(instance, view, data_stage)
    stride = int(mesh.vertexByteStride)
    count = int(mesh.numIndices)
    info = {
        "topology": enum_name(mesh.topology),
        "indexed": bool(int(mesh.indexByteStride)),
        "rowCount": count,
        "vertexByteStride": stride,
        "unproject": bool(mesh.unproject),
        "nearPlane": number(float(mesh.nearPlane)),
        "farPlane": number(float(mesh.farPlane)),
        "status": str(getattr(mesh, "status", "") or ""),
        "columns": [{key: column[key] for key in ("name", "semantic", "systemValue", "type",
                                                    "components", "byteOffset")}
                    for column in columns],
    }
    warnings = []
    if info["status"]:
        warnings.append("RenderDoc reports: %s" % info["status"])
    vertex_buffer = mesh.vertexResourceId
    if resource_id(vertex_buffer) is None or count == 0:
        warnings.append("RenderDoc produced no post-transform data for this stage.")
        return info, [], warnings
    if stride and needed > stride:
        warnings.append("The output signature needs %d bytes per vertex but the stride is %d; "
                        "trailing columns are missing." % (needed, stride))
    decoded = min(count, MAX_DECODED_ROWS)
    if decoded < count:
        warnings.append("Only the first %d of %d rows were decoded; stats cover those rows."
                        % (decoded, count))

    original = list(range(decoded)) if stage == "vs_out" else [None] * decoded
    if stage == "vs_out" and "Indexed" in node["flags"]:
        original = [with_base(node, item)
                    for item in draw_indices(controller, pipe, node, 0, decoded)]
        if len(original) < decoded:
            warnings.append("The bound index buffer ends after %d indices." % len(original))
            original += [None] * (decoded - len(original))
    post = list(range(decoded))
    index_stride = int(mesh.indexByteStride)
    if index_stride:
        if resource_id(mesh.indexResourceId) is None or index_stride not in INDEX_CODES:
            raise AgentBridgeError("UNSUPPORTED", "RenderDoc did not provide the post-transform "
                                   "index buffer for this draw.")
        data = bytes(controller.GetBufferData(mesh.indexResourceId, int(mesh.indexByteOffset),
                                              decoded * index_stride))
        available = len(data) // index_stride
        values = struct.unpack_from("<%d%s" % (available, INDEX_CODES[index_stride]), data)
        restart = int(mesh.restartIndex) if bool(getattr(mesh, "allowRestart", False)) else None
        post = [None if restart is not None and value == restart else
                value + int(mesh.baseVertex) for value in values]
        post += [None] * (decoded - available)
    valid = [item for item in post if item is not None and item >= 0]
    vertex_data = b""
    if valid and stride:
        length = min((max(valid) + 1) * stride, MAX_VERTEX_BYTES)
        vertex_data = bytes(controller.GetBufferData(vertex_buffer, int(mesh.vertexByteOffset),
                                                     length))
    rows = []
    missing = 0
    for row in range(decoded):
        source = post[row]
        restart = stage == "vs_out" and original[row] is None and "Indexed" in node["flags"]
        if restart or source is None or source < 0 or (source + 1) * stride > len(vertex_data):
            if not restart:
                missing += 1
            rows.append((row, original[row], None, restart))
            continue
        rows.append((row, original[row], _decode(columns, vertex_data, source * stride), False))
    if missing:
        warnings.append("%d rows reference post-transform vertices that could not be read."
                        % missing)
    if stage == "vs_out" and any(pipe.GetShaderReflection(getattr(rd.ShaderStage, name))
                                 for name in ("Hull", "Domain", "Geometry")
                                 if hasattr(rd.ShaderStage, name)):
        warnings.append("Tessellation or a geometry shader follows; these are vertex shader "
                        "outputs, use stage=gs_out for the rasterized vertices.")
    info["decodedRows"] = decoded
    info["stats"] = _stats(columns, rows)
    info["position"] = _position_stats(columns, rows)
    return info, rows, warnings


def _row(item):
    vertex, index, values, restart = item
    row = {"vertex": vertex, "index": index}
    if restart:
        row["restart"] = True
    row["values"] = None if values is None else [
        None if value is None else [number(component) for component in value]
        for value in values]
    return row


def get_mesh_data(runtime, context):
    params = context.params
    stage = get_choice(params, "stage", STAGES, "vs_out")
    instance = get_int(params, "instance", 0)
    view = get_int(params, "view", 0)
    start = get_int(params, "start", 0)
    limit = get_int(params, "limit", DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
    actions = action_index(runtime, context)
    event_id = resolve_event(context, actions)
    node = actions.action_for(event_id)
    if node is None or "Drawcall" not in node["flags"]:
        if node is not None and "MeshDispatch" in node["flags"]:
            raise AgentBridgeError("UNSUPPORTED", "Mesh shader output is not supported yet.")
        raise AgentBridgeError("INVALID_EVENT", "Event %d is not a draw." % event_id,
                               details={"event_id": event_id})
    if instance >= max(1, node["numInstances"]):
        raise invalid("instance must be below the draw's instance count %d."
                      % node["numInstances"], numInstances=node["numInstances"])
    session = session_of(context)
    meta = {}

    def build():
        value, replay_meta = runtime.replay.at_event(
            context, event_id,
            lambda controller: _build(controller, renderdoc_module(), node, stage, instance, view))
        meta.update(replay_meta)
        return value

    cache_key = ("meshData", event_id, stage, instance, view)
    info, rows, warnings = runtime.cache.get(session, cache_key, build)
    key = query_key(cache_key)
    offset = decode_cursor(params.get("cursor"), "get_mesh_data", session, key) \
        if params.get("cursor") is not None else start
    page = rows[offset:offset + limit]
    end = offset + len(page)
    next_cursor = encode_cursor("get_mesh_data", session, key, end) if end < len(rows) else None
    data = {"eventId": event_id, "stage": stage, "instance": instance, "view": view}
    data.update(info)
    data["start"] = offset
    data["rows"] = [_row(item) for item in page]
    return result(data, meta=meta, warnings=list(warnings), next_cursor=next_cursor)


HANDLERS = {
    "get_mesh_data": get_mesh_data,
}
