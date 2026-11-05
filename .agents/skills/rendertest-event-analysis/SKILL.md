---
name: rendertest-event-analysis
description: Explain what a RenderDoc event, draw, dispatch or pass does in a RenderTest capture - its inputs, shaders, render targets and effect on the frame. Use when asked what an EID or pass is doing, where a pass is, or why an event produces its output.
---

# Explain an event

Follow `rendertest-core`.

## Locate

- Use the EID the user gave; otherwise use `currentEventId` and `action` from `get_context`.
- Find events by name or type with `find_actions(query=..., flags=[...])`, for example flags `["draw"]`, `["dispatch"]` or `["clear"]`. An empty query filters by type only.
- For the surrounding pass, read the `path` returned by `find_actions` or `inspect_event`, or expand markers with `list_actions(parent_event_id=...)`.
- The EID may be a marker or a non-action API event. `inspect_event` reports `isAction` and the owning `action`.

## Evidence

Start with `inspect_event(event_id=...)`: action parameters, shaders per stage, colour targets, depth target and main bindings. That plus one look at the main output usually answers "what does this event do". Add more only when the question needs it:

- Fixed-function state (input assembly, viewport and scissor, rasteriser, depth/stencil, blend): `get_pipeline_state`, narrowed with `stages` or `include_bindings=false`.
- Output content: `export_texture(resource_id=..., event_id=..., include_image=true)`. Use `channels="rgb"` when alpha is 0 or holds data, and `channels="depth"` for depth (stretched to its min/max, not linear depth).
- What this event changed: compare `get_texture_stats` or a few `pick_pixel` reads at this EID and at the previous write event of the same resource.
- Where a resource comes from and where it goes: `get_resource_usage(resource_id=...)` lists writers and readers.
- Shader arithmetic: see `rendertest-shader-debug`.

## Answer

- What the operation is (draw, dispatch, clear, copy...) and which pass it belongs to.
- Inputs: vertex and index buffers, main textures and constant buffers, with names and ResourceIds.
- Work: shaders per stage and the state that matters (depth test, blend, cull...).
- Outputs: which targets it writes and what that contributes. Describe images you actually viewed.
- Mark inferred purpose and its basis (shader reads and writes, resource flow, image content). Resource and marker names are clues only.
