---
name: rendertest-event-analysis
description: Explain a RenderDoc event, draw, dispatch or pass in RenderTest, or summarize frame/pass operations and texture/buffer footprint. Use for EID/pass purpose, inputs and outputs, event counts, workload rankings or resource-size questions.
---

# Explain an event

Follow `rendertest-core`.

## Locate

- Use the EID the user gave; otherwise use `currentEventId` and `action` from `get_context`.
- Find events by name or type with `find_actions(query=..., flags=[...])`, for example flags `["draw"]`, `["dispatch"]` or `["clear"]`. An empty query filters by type only.
- For the surrounding pass, read the `path` returned by `find_actions` or `inspect_event`, or expand markers with `list_actions(parent_event_id=...)`.
- The EID may be a marker or a non-action API event. `inspect_event` reports `isAction` and the owning `action`.

## Frame and pass summaries

- For a frame overview, call `get_action_stats(top_n=20)` without `parent_event_id`; for a pass, pass its exact marker EID. The scope includes that action and its descendants. `groups` are immediate child subtrees, keyed by EID; identical names are not merged. Page with `cursor` when all groups matter. Scope totals and rankings cover the entire scope on every page.
- `counts` separates operational leaves, markers and containers. Parent/container operation flags are excluded to avoid double counting; mention `excludedParentOperationCount` and warnings if present. Mesh and ray dispatches are separate categories. A single draw can be the scope even when it has no groups.
- `topDraws` ranks recorded `numIndices * numInstances` as index/vertex invocations; this is not a triangle count or unique vertex shader executions. `topDispatches` ranks ordinary compute dispatch group counts. Neither ranking measures GPU cost, and invalid metadata is reported as unrankable. Follow a candidate with `inspect_event` before explaining it.
- For texture/buffer size totals or largest resources, use `get_resource_stats(kind="all", top_n=20)` (`kind="texture"` or `kind="buffer"` narrows it). `all` covers textures and buffers only. Check `byKind`, `byTextureFormat`, `byTextureType` and `largestResources`; use exact ResourceIds to inspect candidates. Report known description bytes and unknown sizes, never call this resident/peak VRAM or a sum of unique allocations.

## Evidence

Start with `inspect_event(event_id=...)`: action parameters, shaders per stage, colour targets, depth target and main bindings. That plus one look at the main output usually answers "what does this event do". Add more only when the question needs it:

- Fixed-function state (input assembly, viewport and scissor, rasteriser, depth/stencil, blend): `get_pipeline_state`, narrowed with `stages` or `include_bindings=false`.
- Where a draw's geometry lands: `get_mesh_data(event_id=..., limit=...)` gives the post-transform columns, the NDC range of the positions and counts of NaN/Inf or w <= 0 vertices without reading every row.
- Output content: `export_texture(resource_id=..., event_id=..., include_image=true)`. Use `channels="rgb"` when alpha is 0 or holds data, and `channels="depth"` for depth (stretched to its min/max, not linear depth).
- What this event changed: compare `get_texture_stats` or a few `pick_pixel` reads at this EID and at the previous write event of the same resource. For one pixel, `pixel_history` at this EID shows whether this event's fragments passed and what they wrote.
- Where a resource comes from and where it goes: `get_resource_usage(resource_id=...)` lists writers and readers.
- Shader arithmetic: see `rendertest-shader-debug`.

## Answer

- What the operation is (draw, dispatch, clear, copy...) and which pass it belongs to.
- Inputs: vertex and index buffers, main textures and constant buffers, with names and ResourceIds.
- Work: shaders per stage and the state that matters (depth test, blend, cull...).
- Outputs: which targets it writes and what that contributes. Describe images you actually viewed.
- Mark inferred purpose and its basis (shader reads and writes, resource flow, image content). Resource and marker names are clues only.
