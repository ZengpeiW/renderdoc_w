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

Use `pixel_history(resource_id=..., x=..., y=..., event_id=...)`. It replays the frame up to the target EID and returns one entry per event that touched the texel, newest first.

- `summary.lastChangedEventId` is the last event that passed and changed the value; `lastPassedEventId` is the last event with a fragment that passed all tests; `finalValue` is the value after the last recorded event.
- Each event gives `fragmentCount`, `passedCount`, `failureCounts` (for example `depthTestFailed`, `shaderDiscarded`, `scissorClipped`, `backfaceCulled`), `primitiveIds` of passing fragments and `preMod` / `shaderOut` / `postMod` values (`color`, `depth`, `stencil`; `null` when unavailable). Colour values use the format's interpretation (`interpretedAs`).
- `changed=false` with passing fragments means the event wrote the same value (or blending kept it). An event that only failed tests did not write the pixel; its `failureCounts` say why.
- Add `include_fragments=true` (with a small `limit`) only when per-fragment detail matters, such as which primitive won the depth test.
- Pixel history skips `Discard` events. Its `discardEventIds` and warnings list them; content after a discard is undefined, not rendered.
- An empty history means nothing tracked wrote the pixel up to the target EID: the value predates the frame, came from a discard, or from a write pixel history does not record. Check `get_resource_usage` for the remaining writes and compare `pick_pixel` before and after them.
- If `pixel_history` returns `UNSUPPORTED`, fall back to that same comparison over the write events from `get_resource_usage`, and say the result is approximate.

## Cause

- For the writing event, check with `inspect_event` and `get_pipeline_state` whether the viewport and scissor cover the pixel and whether depth test, blend, cull and write masks match what `pixel_history` reports.
- Use `rendertest-shader-debug` for its pixel shader, input textures and constant buffers, and `pick_pixel` the inputs around the matching coordinates. Shader stepping (`debug_pixel`) is not available yet (planned for M4).
- NaN and Inf come back as the strings "NaN" and "Inf". They are measured values; trace earlier writers to where they first appear.
- Use `export_texture(include_image=true)` when the surrounding image matters.

## Report

Give the texture (name and ResourceId), coordinates, mip/slice/sample, the value at the target EID, the last changing EID with its action name, passed/failed fragment counts and failure reasons, and the cause with its evidence. Say whether the writer came from `pixel_history` or from the usage comparison fallback.
