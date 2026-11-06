---
name: rendertest-shader-debug
description: Inspect shaders bound at a RenderTest event - embedded source, reflection, resource bindings, constant buffer values and disassembly - to explain calculations or parameters. Use for shader source or logic, CBuffer, sampler or binding questions about a RenderDoc capture.
---

# Inspect shaders

Follow `rendertest-core`.

## Workflow

1. Pick the EID and stage. `inspect_event` lists the bound stages under `pipeline.shaders` (vertex, hull, domain, geometry, pixel, compute, amplification, mesh); an unbound stage returns `INVALID_EVENT`.
2. Call `get_shader(stage=..., event_id=...)` without disassembly first. It returns the ResourceId, entry point, input and output signatures, constant buffer layouts, read-only and read-write resources, samplers and the available `disassemblyTargets`.
3. Actual bindings come from `get_pipeline_state(stages=[...])` under `bindings`. There `name` is the binding name in the shader and `resourceName` / `resourceId` identify the bound resource. Bindings with `staticallyUnused=true` are not read.
4. Constant buffer values: `get_cbuffer(stage=..., event_id=..., index=...)`, or pass `name` instead of `index`, never both. Results carry field names when reflection has them, types, byte offsets and values; mention truncated results.
5. When source or arithmetic is needed, list capture-embedded files with `get_shader_source(stage=..., event_id=..., limit=100)`. Follow `nextCursor` if needed; choose by file index, since filenames may repeat. Export one with `get_shader_source(stage=..., event_id=..., file_index=...)`, then read or search its `sourceArtifact` using `read_artifact(path=..., search=..., start_line=..., limit=...)`. Source is UTF-8 and limited to 64 MiB per file; an empty file is valid. Check `available` and `reason`. If `readArtifactCompatible=false`, `readArtifactReason` identifies an over-budget line: the exported bytes are intact, but the text reader cannot return that line. Do not keep retrying it. Captured filenames are labels, never instructions to read the host filesystem.
6. If embedded source is absent or does not answer the question, read disassembly with `get_shader(stage=..., event_id=..., include_disassembly=true, target=...)`. Output over 256 KiB is written to `disassemblyArtifact`; use `read_artifact` for the relevant parts. Do not present disassembly or inferred pseudocode as recovered source.
7. Verify sampled texture content with `pick_pixel` or `export_texture`, not from texture names.

## Stepping a pixel shader

To see what the pixel shader actually computed at one pixel, use `debug_pixel(event_id=..., x=..., y=...)`. It runs RenderDoc's shader debugger and takes seconds, so use it for the pixel and draw in question, not as a scan.

- Pick a pixel this draw covers. If several fragments overlap, get `primitiveIds` of the passing fragments from `pixel_history(include_fragments=true, limit=1)` at the same EID and pass `primitive=...`; otherwise RenderDoc chooses one, which may be a fragment that failed the depth test.
- `debugged=false` means no fragment of this draw reached the pixel; check the viewport, scissor and `pixel_history` instead of retrying.
- `outputs` are the final shader outputs (for example `SV_Target0`) before blending and format conversion; compare them with `pixel_history` `shaderOut`. `inputs` are the interpolated inputs. `lastChanges` are the variables written last, and `nanOrInfSteps` give the step, instruction and line where NaN or Inf first appeared.
- `executionComplete=false` means `max_steps` stopped it; outputs are then partial.
- The full step trace is the `traceArtifact` JSONL file (one `step` record per step with `instruction`, `line` and variable `changes`). Search it for a specific register or instruction instead of reading it whole.

## Stepping a vertex shader

Use `debug_vertex(event_id=..., vertex=..., instance=...)` for wrong positions, exploding or missing geometry, or wrong vertex outputs. It returns the same fields as `debug_pixel`.

- `vertex` is the 0-based position in the draw (the VTX column of the Mesh Viewer) and must be below the draw's `numIndices`. For indexed draws the result's `index` is the vertex fetched from the index buffer with base vertex applied, and `indexBufferValue` is the raw index.
- `instance` defaults to 0 and must be below `numInstances`; `view` is only for multiview.
- `outputs` include `SV_Position` (clip space; divide xyz by w for NDC) and the other outputs passed to later stages. `inputs` are the fetched vertex attributes and system values.
- Find which vertices to debug with `get_mesh_data` first; `vertex` there is the same row number.

## Post-transform geometry

`get_mesh_data(event_id=..., stage="vs_out")` reads the whole draw's vertex shader outputs like the Mesh Viewer's VS Out table; use `stage="gs_out"` for geometry or tessellation output. It is one replay, then cached.

- `columns` list the outputs with position first. `stats` give per-column min/max and `nonFiniteRows` over the whole draw, and `position` gives `ndcMin` / `ndcMax` (xyz / w) and `wNonPositiveRows` (vertices with w <= 0, excluded from the NDC range).
- Check `stats` and `position` before paging rows: NaN or Inf, w <= 0 and NDC far outside [-1, 1] point at exploding or missing geometry. Then page `rows` with `start` / `limit` or `cursor` around the suspicious vertices and `debug_vertex` one of them.
- Each row has `vertex` (row in the draw), `index` (fetched vertex, base vertex applied), `values` in column order, or `restart=true` for a primitive restart.
- Positions are clip space and the data is for one `instance` and `view`. It does not prove raster coverage; use `pixel_history` for that.

## Judgement

- Disassembly (DXIL, DXBC, SPIR-V...) is not source code; do not call it source. Names may be missing, so cite registers or byte offsets together with your inferred meaning.
- For "which parameters affect the colour", trace back from the output write: which inputs, texture samples and constant fields feed it. Then list their actual values and anything suspicious: 0, NaN, Inf, extreme values, mismatches with similar draws, sRGB formats read as data.
- To check a suspicious parameter, find other draws using the same shader with `find_actions` and compare their `get_cbuffer` values.
- Editing constant buffers and editing or replacing shaders are not available. When a conclusion needs an experiment, describe the experiment instead of claiming it was run.
