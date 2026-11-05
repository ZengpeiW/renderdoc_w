---
name: rendertest-pixel-investigation
description: Investigate a specific pixel in a RenderTest capture - its value, which event last wrote it, and why it has that color. Use for wrong-color, black, NaN, missing-object or "who wrote this pixel" questions at given coordinates.
---

# Investigate a pixel

Follow `rendertest-core`.

## Target

- Texture: the ResourceId the user gave; otherwise choose from `outputTargets` / `depthTarget` of `inspect_event` at the target EID, and say which one you chose.
- Coordinates are pixels of that mip with the origin at the top left. Confirm mip, slice and sample (MSAA); coordinates outside the mip return `INVALID_REQUEST`.
- Target EID: the user's, otherwise the current event. `pick_pixel(event_id=...)` reads the value after that event executes.

## Last event that changed it

`pixel_history` is not available yet (planned for M4). Approximate it from resource usage:

1. Page through `get_resource_usage(resource_id=...)` and keep the events whose `access` is `write` or `readWrite` and whose `eventId` is at or before the target EID.
2. Read the target value with `pick_pixel`, then walk the writes backwards. Compare the value after each write with the value after the write before it. The latest write that changed the value is the last event that changed this pixel.
3. Bracket the change with a few reads before narrowing down; do not read every event blindly.
4. Report the limits: a write of an identical value, fragments rejected by depth or stencil and draws that never covered the pixel all look the same. A discard counts as a write and leaves undefined content.

## Cause

- For the writing event, check with `inspect_event` and `get_pipeline_state` whether the viewport and scissor cover the pixel and whether depth test, blend, cull and write masks make sense.
- Use `rendertest-shader-debug` for its pixel shader, input textures and constant buffers, and `pick_pixel` the inputs around the matching coordinates.
- NaN and Inf come back from `pick_pixel` as the strings "NaN" and "Inf". They are measured values; trace earlier writers to where they first appear.
- Use `export_texture(include_image=true)` when the surrounding image matters.

## Report

Give the texture (name and ResourceId), coordinates, mip/slice/sample, the value at the target EID, the last changing EID with its action name, and the cause with its evidence. Say that the writer was found by comparing values at write events, not from exact pixel history.
