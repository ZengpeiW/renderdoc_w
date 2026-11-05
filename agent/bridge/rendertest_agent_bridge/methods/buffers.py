import base64
import re
import struct

from ..artifacts import write_bytes
from ..values import numbers
from .common import (action_index, get_int, get_resource_key, get_str, invalid, require_resource,
                     resolve_event, resource_index, result, session_of)

INLINE_BYTES = 64 * 1024
HEX_BYTES = 1024
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 1024
ELEMENTS = {
    "float": ("f", 4), "half": ("e", 2), "double": ("d", 8),
    "int": ("i", 4), "uint": ("I", 4), "short": ("h", 2), "ushort": ("H", 2),
    "byte": ("b", 1), "sbyte": ("b", 1), "ubyte": ("B", 1), "bool": ("I", 4),
    "long": ("q", 8), "ulong": ("Q", 8),
}
FIELD_PATTERN = re.compile(r"^([a-z]+)([1-4])?$")


def parse_format(text):
    """Parse ``"float4, uint"`` style layouts into ``[(name, code, count, offset)]``."""
    fields = []
    offset = 0
    for token in re.split(r"[,;\s]+", text.strip().lower()):
        if not token:
            continue
        match = FIELD_PATTERN.match(token)
        if not match or match.group(1) not in ELEMENTS:
            raise invalid("Unsupported buffer format element %s." % token,
                          allowed=sorted(ELEMENTS))
        code, size = ELEMENTS[match.group(1)]
        count = int(match.group(2) or 1)
        fields.append((token, code, count, offset))
        offset += size * count
    if not fields:
        raise invalid("The buffer format is empty.")
    return fields, offset


def decode_rows(data, fields, stride):
    rows = []
    for start in range(0, len(data) - stride + 1, stride):
        if len(rows) >= MAX_ROWS:
            break
        row = []
        for name, code, count, offset in fields:
            values = struct.unpack_from("<%d%s" % (count, code), data, start + offset)
            if name.startswith("bool"):
                values = [bool(value) for value in values]
            row.append(numbers(values))
        rows.append(row)
    return rows


def get_buffer_data(runtime, context):
    params = context.params
    key = get_resource_key(params)
    offset = get_int(params, "offset", 0)
    size = get_int(params, "size", 256, minimum=1, maximum=MAX_BYTES)
    layout = get_str(params, "format")
    fields = stride = None
    if layout:
        fields, stride = parse_format(layout)
    resources = resource_index(runtime, context)
    handle = require_resource(resources, key, "buffer")
    actions = action_index(runtime, context)
    event_id = resolve_event(context, actions)
    length = int(resources.buffers[key].length)
    if offset >= length:
        raise invalid("offset %d is beyond the %d byte buffer." % (offset, length))
    warnings = []
    if offset + size > length:
        size = length - offset
        warnings.append("The range was clamped to the end of the buffer.")

    data, meta = runtime.replay.at_event(
        context, event_id, lambda controller: bytes(controller.GetBufferData(handle, offset, size)))
    info = {"eventId": event_id, "resource": resources.ref(key), "byteLength": length,
            "offset": offset, "size": len(data)}
    artifacts = []
    if len(data) <= INLINE_BYTES:
        info["base64"] = base64.b64encode(data).decode("ascii")
        if len(data) <= HEX_BYTES:
            info["hex"] = data.hex()
    else:
        artifact = write_bytes(runtime, session_of(context), "buffer-%s-e%d-o%d" %
                               (key, event_id, offset), "bin", data, "buffer",
                               "application/octet-stream",
                               source={"event_id": event_id, "resource_id": key,
                                       "offset": offset, "size": len(data)})
        artifacts.append(artifact)
        info["path"] = artifact["path"]
    truncated = False
    if fields:
        rows = decode_rows(data, fields, stride)
        info["layout"] = [{"name": name, "offset": field_offset, "components": count}
                          for name, _, count, field_offset in fields]
        info["stride"] = stride
        info["rows"] = rows
        truncated = len(data) // stride > MAX_ROWS
        if truncated:
            warnings.append("Only the first %d rows were decoded." % MAX_ROWS)
    return result(info, meta=meta, artifacts=artifacts, warnings=warnings, truncated=truncated)


HANDLERS = {"get_buffer_data": get_buffer_data}
