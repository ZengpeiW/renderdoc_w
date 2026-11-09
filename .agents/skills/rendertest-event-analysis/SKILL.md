---
name: rendertest-event-analysis
description: Explain a RenderDoc event, draw, dispatch or pass in RenderTest, or summarize frame/pass operations and texture/buffer footprint. Use for EID/pass purpose, inputs and outputs, raw API parameters, differences between events, event counts, workload rankings or resource-size questions.
---

# Explain an event

Follow `rendertest-core`.

## Locate

- Use the EID the user gave; otherwise use `currentEventId` and `action` from `get_context`.
- Find events by name or type with `find_actions(query=..., flags=[...])`, for example flags `["draw"]`, `["dispatch"]` or `["clear"]`. An empty query filters by type only.
- For the surrounding pass, read the `path` returned by `find_actions` or `inspect_event`, or expand markers with `list_actions(parent_event_id=...)`.
- The EID may be a marker or a non-action API event. `inspect_event` reports `isAction` and the owning `action`.

## Recorded API calls and parameters

- For exact clear/copy/barrier/draw parameters, call `get_api_calls(event_id=...)`. It lists the API calls belonging to that action; a non-action EID resolves to its owner. Page `calls` with `nextCursor`.
- Choose a returned `apiEventId` and call `get_api_calls(api_event_id=...)` for its root `parameters`. This reads that exact API call, not its owner draw. If you also pass `event_id`, it must have the same owner.
- Copy a returned `canonicalPath` such as `[2][0][1]` into `parameter_path` to select a field or array element. Paths are child ordinals, not names; hidden fields and duplicate names retain their positions. `selected.value` holds a scalar; `parameters` pages direct children. `max_depth=0` is shallow; 1-4 expand descendants within response limits. Keep the same target/path/depth when paging.
- Read `available`/`reason` before explaining a chunk. RenderDoc serialized fields can contain internal/return data; `flags` and `displayValue` help interpret them. ResourceIds and large integers retain exact string representations. Raw Buffer payloads are omitted, and truncated strings are previews. `childCount`, `membersTruncated`, `nextCursor` and warnings identify missing data.

## Compare two events

- Call `compare_events(before_event_id=..., after_event_id=...)` for Shader identities, used bindings, input assembly, output targets and exposed raster/depth/stencil/blend state. Narrow `stages` or use `include_bindings=false` when appropriate. The tool compares exact replay EIDs, including non-action API events, and restores replay afterwards.
- Read `coverage.scope`: only viewport/scissor 0 and constant-block array element 0 are represented; graphics sections depend on the owning action. Lists match by position. Resource contents, shader text and unselected constant values are excluded. Binding limits or unavailable reflection can leave coverage incomplete.
- Add selected values with `cbuffers=[{"stage":"pixel","name":"SceneInfo","member_path":"viewProjMat","max_depth":0}]`. Each selection takes exactly one of `name` or `index`; use `get_shader`/`get_cbuffer` to find it. At most 8 selections; select a deep member instead of expanding an entire large block. Layout and binding changes are evidence too: equal child ordinals across different layouts do not establish the same meaning.
- `differences` contains JSON Pointer `path`s and before/after values with explicit `present` flags. Missing and null differ. `comparison="different"` means an observed change; `equal-in-scope` requires complete coverage; `indeterminate` means no observed change but missing evidence. Report incomplete coverage even if some differences were found. `totalDifferencesExact=false` means a lower bound because input coverage is incomplete or the difference budget was reached; `coverage.differencesComplete=false` specifically identifies omitted diff rows.
- Page differences using the same selection and `nextCursor`. Pages belong to a cached snapshot, with only the two most recent comparisons retained. A request without a cursor reads a fresh snapshot and invalidates older cursors for that query; if a snapshot expires, restart without a cursor. Never merge pages from different snapshots or captures. Use texture/pixel tools for changes to rendered contents.

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
- Diagnostic coverage: `export_overlay(resource_id=..., event_id=..., overlay="Wireframe", include_image=true)` visualizes one exact raster draw. Choose its bound color/depth target and matching mip/slice. `Drawcall`, `Depth`, `Stencil`, `BackfaceCull` and `ViewportScissor` answer different coverage/state questions; use the returned legend. `QuadOverdrawDraw` and `TriangleSizeDraw` are heatmaps, not GPU timings or exact pixel-write counts. Overdraw on MSAA and mesh-shader draw overlays are currently unsupported. There is no pass-wide or clear-before mode. Do not scan the entire frame with overlays for an ordinary event question.
- What this event changed: compare `get_texture_stats` or a few `pick_pixel` reads at this EID and at the previous write event of the same resource. For one pixel, `pixel_history` at this EID shows whether this event's fragments passed and what they wrote.
- Where a resource comes from and where it goes: `get_resource_usage(resource_id=...)` lists writers and readers.
- Compute invocation arithmetic: `debug_compute(event_id=..., group=[...], thread=[...])` traces one ordinary compute Dispatch thread. Check exact dispatch/group/local-thread dimensions first; see `rendertest-shader-debug` for coordinate semantics, trace limits and how to verify UAV contents. A trace is not a GPU timing measurement.
- Shader arithmetic: see `rendertest-shader-debug`.

## Answer

- What the operation is (draw, dispatch, clear, copy...) and which pass it belongs to.
- Inputs: vertex and index buffers, main textures and constant buffers, with names and ResourceIds.
- Work: shaders per stage and the state that matters (depth test, blend, cull...).
- Outputs: which targets it writes and what that contributes. Describe images you actually viewed.
- Mark inferred purpose and its basis (shader reads and writes, resource flow, image content). Resource and marker names are clues only.
