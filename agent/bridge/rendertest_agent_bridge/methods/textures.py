from ..artifacts import artifact_path, describe, png_size
from ..ipc import AgentBridgeError
from ..values import enum_name, format_name, numbers
from .common import (action_index, get_choice, get_int, get_resource_key, invalid,
                     renderdoc_module, require_resource, resolve_event, resource_index, result,
                     session_of)

EXPORT_FORMATS = {
    "png": ("PNG", "png", "image/png"),
    "exr": ("EXR", "exr", "image/x-exr"),
    "dds": ("DDS", "dds", "image/vnd-ms.dds"),
}
CHANNELS = {"rgba": -1, "rgb": -1, "r": 0, "g": 1, "b": 2, "a": 3, "depth": 0, "stencil": 1}
INTEGER_COMP_TYPES = {"UInt": "uint", "SInt": "int"}


def _texture_view(context, resources, key):
    params = context.params
    texture = resources.textures[key]
    mip = get_int(params, "mip", 0)
    slice_index = get_int(params, "slice", 0)
    sample = get_int(params, "sample", 0)
    mips = max(1, int(texture.mips))
    if mip >= mips:
        raise invalid("mip %d is out of range; the texture has %d mips." % (mip, mips))
    depth = max(1, int(texture.depth) >> mip)
    slices = max(1, int(texture.arraysize), depth)
    if slice_index >= slices:
        raise invalid("slice %d is out of range; the texture has %d slices." % (slice_index, slices))
    samples = max(1, int(texture.msSamp))
    if sample >= samples:
        raise invalid("sample %d is out of range; the texture has %d samples." % (sample, samples))
    width = max(1, int(texture.width) >> mip)
    height = max(1, int(texture.height) >> mip)
    return {"mip": mip, "slice": slice_index, "sample": sample, "width": width,
            "height": height}


def _subresource(rd, view):
    subresource = rd.Subresource()
    subresource.mip = view["mip"]
    subresource.slice = view["slice"]
    subresource.sample = view["sample"]
    return subresource


def _prepare(runtime, context):
    key = get_resource_key(context.params)
    resources = resource_index(runtime, context)
    require_resource(resources, key, "texture")
    actions = action_index(runtime, context)
    event_id = resolve_event(context, actions)
    view = _texture_view(context, resources, key)
    return key, resources, event_id, view


def _interpret(texture, value):
    fmt = texture.format
    count = max(1, min(4, int(fmt.compCount)))
    comp_type = enum_name(fmt.compType)
    kind = INTEGER_COMP_TYPES.get(comp_type, "float")
    source = {"float": value.floatValue, "uint": value.uintValue, "int": value.intValue}[kind]
    return kind, numbers(source, count)


def _pixel(texture, value):
    kind, values = _interpret(texture, value)
    return {
        "interpretedAs": kind,
        "value": values,
        "float": numbers(value.floatValue),
        "uint": [int(item) for item in value.uintValue],
        "int": [int(item) for item in value.intValue],
    }


def _texture_info(resources, key, view):
    texture = resources.textures[key]
    info = {"resourceId": key, "name": resources.name(key), "format": format_name(texture.format),
            "components": int(texture.format.compCount)}
    info.update(view)
    return info


def pick_pixel(runtime, context):
    key, resources, event_id, view = _prepare(runtime, context)
    x = get_int(context.params, "x", required=True)
    y = get_int(context.params, "y", required=True)
    if x >= view["width"] or y >= view["height"]:
        raise invalid("(%d, %d) is outside the %dx%d mip." % (x, y, view["width"], view["height"]))
    handle = resources.handle(key)
    texture = resources.textures[key]

    def read(controller):
        rd = renderdoc_module()
        value = controller.PickPixel(handle, x, y, _subresource(rd, view), rd.CompType.Typeless)
        return _pixel(texture, value)

    pixel, meta = runtime.replay.at_event(context, event_id, read)
    data = {"eventId": event_id, "texture": _texture_info(resources, key, view), "x": x, "y": y}
    data.update(pixel)
    return result(data, meta=meta)


def get_texture_stats(runtime, context):
    key, resources, event_id, view = _prepare(runtime, context)
    handle = resources.handle(key)
    texture = resources.textures[key]

    def read(controller):
        rd = renderdoc_module()
        minimum, maximum = controller.GetMinMax(handle, _subresource(rd, view),
                                                rd.CompType.Typeless)
        return _pixel(texture, minimum), _pixel(texture, maximum)

    (minimum, maximum), meta = runtime.replay.at_event(context, event_id, read)
    return result({
        "eventId": event_id,
        "texture": _texture_info(resources, key, view),
        "min": minimum,
        "max": maximum,
    }, meta=meta)


def export_texture(runtime, context):
    params = context.params
    file_format = get_choice(params, "format", tuple(EXPORT_FORMATS), "png")
    channels = get_choice(params, "channels", tuple(CHANNELS), "rgba")
    key, resources, event_id, view = _prepare(runtime, context)
    handle = resources.handle(key)
    type_name, extension, media_type = EXPORT_FORMATS[file_format]
    path = artifact_path(runtime, session_of(context),
                         "texture-%s-e%d-m%d-s%d" % (key, event_id, view["mip"], view["slice"]),
                         extension)

    def save(controller):
        rd = renderdoc_module()
        options = rd.TextureSave()
        options.resourceId = handle
        options.destType = getattr(rd.FileType, type_name)
        options.typeCast = rd.CompType.Typeless
        options.mip = view["mip"]
        slice_mapping = options.slice
        slice_mapping.sliceIndex = view["slice"]
        options.slice = slice_mapping
        sample_mapping = options.sample
        sample_mapping.sampleIndex = view["sample"]
        options.sample = sample_mapping
        options.channelExtract = CHANNELS[channels]
        preserve = channels == "rgba" or file_format == "dds"
        options.alpha = rd.AlphaMapping.Preserve if preserve else rd.AlphaMapping.Discard
        if channels in ("depth", "stencil") and file_format == "png":
            minimum, maximum = controller.GetMinMax(handle, _subresource(rd, view),
                                                    rd.CompType.Typeless)
            channel = CHANNELS[channels]
            low = float(minimum.floatValue[channel])
            high = float(maximum.floatValue[channel])
            if high > low:
                mapping = options.comp
                mapping.blackPoint = low
                mapping.whitePoint = high
                options.comp = mapping
        status = controller.SaveTexture(options, path)
        try:
            failed = int(status.code) != 0
        except (AttributeError, TypeError, ValueError):
            failed = not status
        if failed:
            raise AgentBridgeError("REPLAY_ERROR", "SaveTexture failed: %s" % status)

    _, meta = runtime.replay.at_event(context, event_id, save)
    extra = {"width": view["width"], "height": view["height"],
             "source": {"event_id": event_id, "resource_id": key, "mip": view["mip"],
                        "slice": view["slice"], "sample": view["sample"],
                        "channels": channels}}
    if file_format == "png":
        size = png_size(path)
        if size is not None:
            extra["width"], extra["height"] = size
    artifact = describe(path, "texture", media_type, **extra)
    return result({"eventId": event_id, "texture": _texture_info(resources, key, view),
                   "format": file_format, "channels": channels, "path": artifact["path"]},
                  meta=meta, artifacts=[artifact])


HANDLERS = {
    "export_texture": export_texture,
    "pick_pixel": pick_pixel,
    "get_texture_stats": get_texture_stats,
}
