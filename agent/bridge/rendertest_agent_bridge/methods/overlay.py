"""Bounded, composited diagnostic images from a temporary headless replay output."""

import math
import os
import struct
import zlib

from ..artifacts import artifact_path, describe
from ..ipc import AgentBridgeError
from ..replay import _check_replay_health
from ..values import enum_name, resource_id
from .common import (action_index, get_choice, get_int, invalid, renderdoc_module, result,
                     session_of)
from .textures import _prepare, _subresource, _texture_info, _type_cast


OVERLAYS = ("Drawcall", "Wireframe", "Depth", "Stencil", "BackfaceCull", "ViewportScissor",
            "QuadOverdrawDraw", "TriangleSizeDraw", "NaN", "Clipping")
TEXTURE_OVERLAYS = ("NaN", "Clipping")
MAX_SOURCE_TEXELS = 16 * 1024 * 1024  # Includes every mip, array slice and MSAA sample.
MAX_OUTPUT_PIXELS = 2 * 1024 * 1024  # RGB PNG stays below the panel's 8 MiB image budget.
MAX_DIMENSION = 4096
MAX_SOURCE_DIMENSION = 16384

LEGENDS = {
    "Drawcall": "Highlights rasterized draw coverage, including pixels rejected by tests.",
    "Wireframe": "Shows the selected draw's wireframe over the texture.",
    "Depth": "Green: depth passed; red: depth failed. Any passing fragment/sample can make a "
             "pixel green; this is a conservative diagnostic, not per-fragment history.",
    "Stencil": "Green: stencil passed; red: stencil failed. Any passing fragment/sample can "
               "make a pixel green; this is a conservative diagnostic, not per-fragment history.",
    "BackfaceCull": "Green: facing test passed; red: backface culled. Overlapping passing "
                    "fragments can hide failures.",
    "ViewportScissor": "Shows viewport and scissor rectangles for the selected draw.",
    "QuadOverdrawDraw": "RenderDoc heatmap of 2x2 rasterized quad overdraw for this draw, "
                        "including edge/helper work. Not GPU duration or exact per-pixel writes.",
    "TriangleSizeDraw": "RenderDoc triangle-area heatmap: low end >=16 square pixels; high "
                        "end <=0.125 square pixels. Not GPU duration.",
    "NaN": "Grayscale texture with NaN in red, infinity in green, and negative values in blue.",
    "Clipping": "Grayscale texture with values above range_max in green and below range_min "
                "in red. These are display thresholds, not rasterizer clipping.",
}


def _unsupported(message):
    return AgentBridgeError("UNSUPPORTED", message)


def _range_value(params, name, default):
    value = params.get(name, default)
    if type(value) not in (int, float):
        raise invalid("%s must be a finite number representable as a 32-bit float." % name)
    try:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError()
        value = struct.unpack("<f", struct.pack("<f", value))[0]
        if not math.isfinite(value):
            raise ValueError()
    except (OverflowError, ValueError, struct.error):
        raise invalid("%s must be a finite number representable as a 32-bit float." % name)
    return value


def _source_budget(texture):
    width, height = int(texture.width), int(texture.height)
    depth, arrays = max(1, int(texture.depth)), max(1, int(texture.arraysize))
    mips, samples = max(1, int(texture.mips)), max(1, int(texture.msSamp))
    if (width < 1 or height < 1 or max(width, height, depth, arrays) > MAX_SOURCE_DIMENSION
            or mips > 32 or samples > 64):
        raise _unsupported("Texture dimensions exceed the diagnostic overlay budget.")
    total = sum(max(1, width >> mip) * max(1, height >> mip) *
                max(1, depth >> mip) * arrays * samples for mip in range(mips))
    if total > MAX_SOURCE_TEXELS:
        raise _unsupported("The full source texture exceeds the %d stored-texel overlay budget "
                           "(all mips, slices and samples); reducing max_dimension only reduces "
                           "the exported image." % MAX_SOURCE_TEXELS)


def _dimensions(view, maximum):
    width, height = view["width"], view["height"]
    scale = min(1.0, float(maximum) / max(width, height),
                math.sqrt(float(MAX_OUTPUT_PIXELS) / (width * height)))
    return max(1, int(width * scale)), max(1, int(height * scale))


def _output_views(pipe):
    if not all(callable(getattr(pipe, name, None))
               for name in ("GetOutputTargets", "GetDepthTarget")):
        raise _unsupported("This replay cannot expose render-target attachments for overlays.")
    depth = pipe.GetDepthTarget()
    return list(pipe.GetOutputTargets()) + [depth], depth


def _depth_budget(resources, depth):
    depth_key = resource_id(depth.resource)
    if depth_key in resources.textures:
        # RenderOverlay can allocate/copy the depth target even for NaN/Clipping displays.
        _source_budget(resources.textures[depth_key])
    return depth_key


def _attachment(pipe, resources, key, view, overlay, warnings):
    descriptors, depth = _output_views(pipe)
    matches = [item for item in descriptors if resource_id(item.resource) == key]
    if not matches:
        raise invalid("Draw overlays require a texture bound as a color or depth target at this EID.")
    if len(matches) != 1:
        raise _unsupported("The texture is bound through multiple output views; overlay "
                           "subresource selection would be ambiguous.")
    target = matches[0]
    try:
        mip, first, count = int(target.firstMip), int(target.firstSlice), int(target.numSlices)
    except (AttributeError, TypeError, ValueError):
        raise _unsupported("This replay cannot identify the output attachment's subresource.")
    if count == 0xffffffff:
        count = max(1, int(resources.textures[key].arraysize)) - first
    if mip != view["mip"] or not first <= view["slice"] < first + count:
        raise invalid("Requested mip/slice is not part of the texture's bound output view.",
                      attachmentMip=mip, firstSlice=first, numSlices=count)
    depth_key = _depth_budget(resources, depth)
    if overlay in ("Depth", "Stencil") and depth_key is None:
        raise _unsupported("%s overlay requires a bound depth-stencil target." % overlay)
    if overlay == "Depth" and callable(getattr(pipe, "GetDepthTestState", None)):
        if not pipe.GetDepthTestState().depthEnable:
            warnings.append("Depth testing is disabled; the overlay is not evidence of a "
                            "depth comparison passing.")
    if overlay == "Stencil" and callable(getattr(pipe, "IsStencilTestEnabled", None)):
        if not pipe.IsStencilTestEnabled():
            warnings.append("Stencil testing is disabled; the overlay is not evidence of a "
                            "stencil comparison passing.")
    if overlay == "TriangleSizeDraw":
        topology = getattr(pipe, "GetPrimitiveTopology", lambda: None)()
        if not str(enum_name(topology) or "").startswith("Triangle"):
            raise _unsupported("TriangleSizeDraw requires a triangle-list or triangle-strip draw.")
    return {"mip": mip, "firstSlice": first, "numSlices": count,
            "depthTarget": depth_key}


def _capabilities(controller, rd, overlay, cast):
    for name in ("CreateOutput", "SetFrameEvent"):
        if not callable(getattr(controller, name, None)):
            raise _unsupported("This replay does not support headless overlay outputs (%s)." % name)
    for name in ("CreateHeadlessWindowingData", "TextureDisplay", "FloatVector", "Subresource"):
        if not callable(getattr(rd, name, None)):
            raise _unsupported("This RenderDoc build does not expose %s." % name)
    if (not hasattr(getattr(rd, "DebugOverlay", None), overlay)
            or not hasattr(getattr(rd, "ReplayOutputType", None), "Texture")
            or not hasattr(getattr(rd, "CompType", None), cast)):
        raise _unsupported("This RenderDoc build does not expose the requested overlay/type cast.")


def _render(context, controller, rd, key, resources, view, event_id, overlay,
            cast, low, high, width, height):
    _capabilities(controller, rd, overlay, cast)
    warnings = []
    attachment = None
    pipe = controller.GetPipelineState()
    if overlay in TEXTURE_OVERLAYS:
        # ReplayOutput still invokes RenderOverlay for a currently bound output texture. Native
        # drivers may copy the entire depth target before handling these display-only modes.
        # Other textures do not take that path and must not inherit unrelated depth limits.
        descriptors, depth = _output_views(pipe)
        if any(resource_id(item.resource) == key for item in descriptors):
            _depth_budget(resources, depth)
    else:
        attachment = _attachment(pipe, resources, key, view, overlay, warnings)
        if overlay == "QuadOverdrawDraw":
            if int(resources.textures[key].msSamp) > 1:
                raise _unsupported("QuadOverdrawDraw is not supported for MSAA targets by this tool.")
            properties = getattr(controller, "GetAPIProperties", lambda: None)()
            if enum_name(getattr(properties, "pipelineType", None)) == "Vulkan":
                state = getattr(controller, "GetVulkanPipelineState", lambda: None)()
                if state is None or int(state.multisample.rasterSamples) > 1:
                    raise _unsupported("QuadOverdrawDraw requires single-sample Vulkan rasterization.")
    output = None
    try:
        context.check_cancelled()
        output = controller.CreateOutput(rd.CreateHeadlessWindowingData(width, height),
                                         rd.ReplayOutputType.Texture)
        if output is None:
            raise _unsupported("RenderDoc could not create a headless texture output.")
        methods = ("SetTextureDisplay", "Display", "ReadbackOutputTexture", "GetDimensions",
                   "Shutdown", "GetDebugOverlayTexID")
        if not all(callable(getattr(output, name, None)) for name in methods):
            raise _unsupported("This RenderDoc output lacks the headless overlay/readback API.")
        if tuple(output.GetDimensions()) != (width, height):
            raise _unsupported("RenderDoc did not create the requested headless output dimensions.")
        display = rd.TextureDisplay()
        display.resourceId = resources.handle(key)
        display.overlay = getattr(rd.DebugOverlay, overlay)
        display.typeCast = getattr(rd.CompType, cast)
        display.subresource = _subresource(rd, view)
        display.rangeMin, display.rangeMax = low, high
        # RenderDoc's display scale is relative to base-level dimensions even for a smaller mip.
        texture = resources.textures[key]
        base_width, base_height = int(texture.width), int(texture.height)
        display.scale = min(float(width) / base_width, float(height) / base_height)
        display.xOffset = (width - base_width * display.scale) / 2.0
        display.yOffset = (height - base_height * display.scale) / 2.0
        display.red = display.green = display.blue = True
        display.alpha = False
        display.flipY = False
        display.rawOutput = False
        display.hdrMultiplier = -1.0
        display.linearDisplayAsGamma = True
        display.backgroundColor = rd.FloatVector(0.0, 0.0, 0.0, 1.0)
        output.SetTextureDisplay(display)
        context.check_cancelled()
        output.Display()
        _check_replay_health(controller)
        if (overlay not in TEXTURE_OVERLAYS
                and resource_id(output.GetDebugOverlayTexID()) is None):
            raise _unsupported("RenderDoc did not generate this diagnostic overlay at the selected EID.")
        context.check_cancelled()
        pixels = bytes(output.ReadbackOutputTexture())
        _check_replay_health(controller)
        if len(pixels) != width * height * 3:
            raise AgentBridgeError("REPLAY_ERROR", "Headless overlay readback returned an invalid "
                                   "RGB byte count.")
        transform = {"scale": display.scale, "xOffset": display.xOffset,
                     "yOffset": display.yOffset, "flipY": False,
                     "scaleBasis": "base-level-dimensions",
                     "sourcePixelScaleX": display.scale * base_width / view["width"],
                     "sourcePixelScaleY": display.scale * base_height / view["height"],
                     "rangeMin": low, "rangeMax": high, "typeCast": cast,
                     "channels": "rgb", "alpha": "discard", "linearDisplayAsGamma": True,
                     "background": [0.0, 0.0, 0.0, 1.0]}
        return pixels, attachment, transform, warnings
    finally:
        # Drivers reuse overlay textures across ReplayOutputs. Remove our output first, then
        # force a replay so existing GUI outputs regenerate their own overlays even at this EID.
        try:
            if output is not None and callable(getattr(output, "Shutdown", None)):
                output.Shutdown()
        finally:
            controller.SetFrameEvent(event_id, True)
            _check_replay_health(controller)


def _png(width, height, pixels):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    stride = width * 3
    scanlines = b"".join(b"\x00" + pixels[start:start + stride]
                         for start in range(0, len(pixels), stride))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(scanlines, 6)) + chunk(b"IEND", b""))


def export_overlay(runtime, context):
    params = context.params
    overlay = get_choice(params, "overlay", OVERLAYS, required=True)
    maximum = get_int(params, "max_dimension", 1024, minimum=1, maximum=MAX_DIMENSION)
    low, high = _range_value(params, "range_min", 0.0), _range_value(params, "range_max", 1.0)
    if not low < high:
        raise invalid("range_min must be below range_max after conversion to 32-bit floats.")
    cast = _type_cast(context)
    key, resources, event_id, view = _prepare(runtime, context)
    texture = resources.textures[key]
    _source_budget(texture)
    if int(texture.dimension) != 2:
        raise _unsupported("Diagnostic overlays currently support only 2D textures and 2D arrays.")
    if overlay not in TEXTURE_OVERLAYS:
        node = action_index(runtime, context).nodes.get(event_id)
        if node is not None and "MeshDispatch" in node["flags"]:
            raise _unsupported("Mesh-shader draw overlays are not supported by this tool yet.")
        if (node is None or "Drawcall" not in node["flags"] or node["children"]
                or "MultiAction" in node["flags"]):
            raise AgentBridgeError("INVALID_EVENT", "Draw overlays require the exact EID of a "
                                   "leaf raster draw, not a marker or API setup event.")
    width, height = _dimensions(view, maximum)
    value, meta = runtime.replay.at_event(
        context, event_id, lambda controller: _render(
            context, controller, renderdoc_module(), key, resources, view, event_id,
            overlay, cast, low, high, width, height))
    pixels, attachment, transform, warnings = value
    context.check_cancelled()
    source = {"resource_id": key, "event_id": event_id, "overlay": overlay,
              "mip": view["mip"], "slice": view["slice"], "sample": view["sample"],
              "type_cast": cast}
    path = artifact_path(runtime, session_of(context), "overlay-%s-%s-e%d" % (overlay, key, event_id),
                         "png")
    try:
        with open(path, "wb") as stream:
            stream.write(_png(width, height, pixels))
        context.check_cancelled()
        artifact = describe(path, "overlay", "image/png", source=source, width=width, height=height)
        context.check_cancelled()
    except BaseException as error:
        if os.path.exists(path):
            os.remove(path)
        if isinstance(error, (OSError, ValueError, struct.error, zlib.error)):
            raise AgentBridgeError("REPLAY_ERROR", "Could not write overlay artifact: %s" % error)
        raise
    if width < view["width"] or height < view["height"]:
        warnings.append("The source mip was downsampled to fit the output size/pixel budget; "
                        "small diagnostic details may not be visible.")
    warnings.append("This is an 8-bit RGB display visualization with RenderDoc's overlay colors, "
                    "not original texture values or exact visibility/performance measurements.")
    return result({"eventId": event_id, "texture": _texture_info(resources, key, view),
                   "overlay": overlay, "legend": LEGENDS[overlay], "format": "png",
                   "width": width, "height": height, "maxDimension": maximum,
                   "sourceWidth": view["width"], "sourceHeight": view["height"],
                   "attachment": attachment, "displayTransform": transform,
                   "path": artifact["path"]}, meta=meta, artifacts=[artifact], warnings=warnings)


HANDLERS = {"export_overlay": export_overlay}
