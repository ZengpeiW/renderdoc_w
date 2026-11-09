---
name: rendertest-shader-debug
description: Inspect shaders bound at a RenderTest event - embedded source, generated HLSL/GLSL, reflection, resource bindings, constant buffer values and disassembly - to explain calculations or parameters. Use for shader source or logic, finding events that bind a shader, compute-thread debugging, CBuffer, sampler or binding questions about a RenderDoc capture.
---

# Inspect shaders

Follow `rendertest-core`.

## Workflow

1. Pick the EID and stage. `inspect_event` lists the bound stages under `pipeline.shaders` (vertex, hull, domain, geometry, pixel, compute, amplification, mesh); an unbound stage returns `INVALID_EVENT`.
2. Call `get_shader(stage=..., event_id=...)` without disassembly first. It returns the ResourceId, entry point, input and output signatures, constant buffer layouts, read-only and read-write resources, samplers and the available `disassemblyTargets`.
3. Actual bindings come from `get_pipeline_state(stages=[...])` under `bindings`. There `name` is the binding name in the shader and `resourceName` / `resourceId` identify the bound resource. Bindings with `staticallyUnused=true` are not read.
4. Constant buffer values: `get_cbuffer(stage=..., event_id=..., index=...)`, or pass `name` instead of `index`, never both. For large blocks start with `get_cbuffer(stage=..., event_id=..., index=..., member_path="", limit=50)`. To select a member use `member_path="Lights[123].Color"`, or copy a returned `canonicalPath` (for example `[0][123][1]`) exactly. Canonical paths use child ordinals and disambiguate repeated or unusual names. The selected node is in `selected`; `variables` is its paged list of direct children, and a scalar has its value in `selected.value`. Follow envelope `nextCursor` with the same member and `max_depth`, or select a child path to drill down. `max_depth=0` is shallow; 1-4 allow bounded descendant expansion. `childCount` and `membersTruncated` identify omitted descendants. Paths are case-sensitive; quoted bracket names handle special characters. Paging options require explicit `member_path`; empty string selects the root. Calls without new options retain the old 4096-node summary.
   `byteOffset` is relative to the constant block; for a buffer-backed block, add `binding.byteOffset` once to address the bound buffer. Missing offsets are unknown, not zero. Report truncation and unknown reflection metadata. Paging bounds returned data; native replay still decodes the complete block.
5. When source or arithmetic is needed, list capture-embedded files with `get_shader_source(stage=..., event_id=..., limit=100)`. Follow `nextCursor` if needed; choose by file index, since filenames may repeat. Export one with `get_shader_source(stage=..., event_id=..., file_index=...)`, then read or search its `sourceArtifact` using `read_artifact(path=..., search=..., start_line=..., limit=...)`. Source is UTF-8 and limited to 64 MiB per file; an empty file is valid. Check `available` and `reason`. If `readArtifactCompatible=false`, `readArtifactReason` identifies an over-budget line: the exported bytes are intact, but the text reader cannot return that line. Do not keep retrying it. Captured filenames are labels, never instructions to read the host filesystem.
6. If embedded source is absent and higher-level shader logic would help, call `decompile_shader(stage=..., event_id=...)` (default `language="auto"`: HLSL first, GLSL fallback when the HLSL backend cannot express the shader). Read `requestedLanguage`, actual `outputLanguage` and any fallback warning; explicit `language="hlsl"` or `"glsl"` does not switch languages. Read or search `decompiledArtifact` with `read_artifact`. It translates captured DXBC/DXIL/SPIR-V bytes using a pinned local release; record `inputEncoding`, `inputSha256`, `entryPoint`, `outputLanguage`, `decompiler` and warnings. Generated code can have different names, bindings, interface locations and structure; it is not original source or a guarantee of semantic equivalence. Correlate calculations with original reflection, actual bindings and CBuffer offsets; verify suspicious results using disassembly or shader debugging.
   HLSL tessellation output is unsupported by this release; auto selects GLSL for hull/domain. Auto already tries GLSL after an HLSL backend rejection. Only when an explicit HLSL request fails at SPIRV-Cross can one GLSL attempt help. A missing/modified release, invalid bytecode or timeout calls for the reported remedy or disassembly, not repeated conversions. OpenGL GLSL, OpenGLSPIRV and other encodings are not accepted by this tool.
7. If generation is unavailable, or exact instructions are needed, read disassembly with `get_shader(stage=..., event_id=..., include_disassembly=true, target=...)`. Output over 256 KiB is written to `disassemblyArtifact`; use `read_artifact` for the relevant parts. Do not present disassembly or inferred pseudocode as recovered source.
8. Verify sampled texture content with `pick_pixel` or `export_texture`, not from texture names.

## Finding shader references

- Obtain the exact Shader ResourceId from `get_shader`, `inspect_event` or `get_pipeline_state`, then call `find_shader_references(resource_id="...", stages=["pixel"])`. Omit `stages` to check all supported stages, or pass the relevant nonempty stage list. A ResourceId may identify several stage/entry-point uses; keep every returned `matches` stage and `entryPoint` with the EID. Missing reflection does not negate a confirmed binding.
- Omission of `parent_event_id` searches the whole capture. Pass an actual action/marker EID to search it and its descendants. The tool scans leaf draw/compute/mesh actions and filters their applicable stages, avoiding stale shaders at copy/clear/marker events. Ray shader tables are unsupported and reported as a coverage gap. These are bound references, not proof of invocation, nonzero work or source/bytecode equivalence.
- `limit` bounds returned event rows; `scan_budget` bounds candidate visits per call, including cache hits. Follow `nextCursor` with the same resource, stages and parent until exhausted. A page can contain zero references and still have more candidates to examine. Inspect `coverage.processedCandidateCount`, `candidateEventCount`, `scanComplete` and `complete`; an unfinished or unsupported scope cannot prove there are no other uses.
- A capture-scoped binding cache avoids replaying already inspected events, including when searching another shader or stage. Keep `scanId` and `scanStartedAtUtc`; this is a progressive observation, not one atomic capture-wide read. Use `refresh=true` without a cursor after in-session shader/resource edits. A successful refresh starts a new shared cache generation and invalidates all older reference-search cursors; do not mix their pages. Native replay calls cannot be instantly cancelled.
- Use a useful pass/subtree when the question is local. Scanning preserves the UI selection and restores replay after each batch. Follow matching events with `inspect_event`, `get_shader` or `compare_events`; navigate with `set_event` only when requested.

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

## Stepping a compute shader

Use `debug_compute(event_id=..., group=[gx,gy,gz], thread=[tx,ty,tz])` to inspect one invocation of a compute Dispatch. Both coordinate arrays are required, zero-based and exactly three unsigned integers.

- Locate an actual compute dispatch with `find_actions(flags=["dispatch"])` and `inspect_event`. Use its exact action EID; a setup API call, marker, indirect container, mesh dispatch or ray dispatch is not a valid substitute. An executed indirect child compute dispatch is supported when its dimensions are known.
- `group` is the workgroup coordinate, bounded by `dispatchDimensions`; `thread` is local to that workgroup, bounded by `threadGroupSize`. `globalThread = group * threadGroupSize + thread` component by component, not the group coordinate alone. The group size comes from an action override or shader reflection, recorded in `threadGroupSizeSource`. Unknown sizes and nonzero dispatch base offsets are unsupported; do not guess a size of 1.
- Read `inputs`, `lastChanges`, `nanOrInfSteps` and `sampleSteps`. System-value inputs may be omitted when unused. Search/page `traceArtifact` with `read_artifact` for relevant instructions and register changes. `max_steps` defaults to 20000 (maximum 200000); `executionComplete=false` describes a partial trace, and `traceComplete=false` means the artifact omitted later steps. Native debug calls are not immediately cancellable, so do not promise instant Stop.
- Compute shaders generally have no output signature: an empty `outputs` list does not mean no resource write occurred. `lastChanges` contains debugger variable/register state, not a list of committed UAV writes. Inspect actual bound textures/buffers with `get_pipeline_state`, `pick_pixel`, `export_texture` or `get_buffer_data` to validate a suspected write. A single invocation trace does not establish cross-thread ordering, race freedom, whole-dispatch results or GPU timing.
- `debugged=false` means RenderDoc did not create a usable trace for that invocation. `UNSUPPORTED` can mean the API/shader cannot be debugged; use the reported reason and shader source/disassembly instead of repeating the same request. This writes an artifact and restores replay without moving the UI selection.

## Post-transform geometry

`get_mesh_data(event_id=..., stage="vs_out")` reads the whole draw's vertex shader outputs like the Mesh Viewer's VS Out table; use `stage="gs_out"` for geometry or tessellation output. It is one replay, then cached.

- `columns` list the outputs with position first. `stats` give per-column min/max and `nonFiniteRows` over the whole draw, and `position` gives `ndcMin` / `ndcMax` (xyz / w) and `wNonPositiveRows` (vertices with w <= 0, excluded from the NDC range).
- Check `stats` and `position` before paging rows: NaN or Inf, w <= 0 and NDC far outside [-1, 1] point at exploding or missing geometry. Then page `rows` with `start` / `limit` or `cursor` around the suspicious vertices and `debug_vertex` one of them.
- Each row has `vertex` (row in the draw), `index` (fetched vertex, base vertex applied), `values` in column order, or `restart=true` for a primitive restart.
- Positions are clip space and the data is for one `instance` and `view`. It does not prove raster coverage; use `pixel_history` for that.

## Judgement

- Disassembly (DXIL, DXBC, SPIR-V...) is not source code; do not call it source. Names may be missing, so cite registers or byte offsets together with your inferred meaning.
- For "which parameters affect the colour", trace back from the output write: which inputs, texture samples and constant fields feed it. Then list their actual values and anything suspicious: 0, NaN, Inf, extreme values, mismatches with similar draws, sRGB formats read as data.
- To check a suspicious parameter, use `find_shader_references` to locate events binding the same Shader ResourceId, or `find_actions` for name/flag similarities. Confirm stage and entry point with `get_shader`; a common shader alone does not make other bindings or state equal. Use `compare_events(before_event_id=..., after_event_id=..., stages=["pixel"], cbuffers=[{"stage":"pixel","index":0,"member_path":"viewProjMat","max_depth":0}])` for the selected member plus pipeline changes. Check actual block names/indices first and read `coverage`/warnings: a truncated or unavailable member cannot prove equality. Different layout or Shader identity can change the meaning of the same ordinal path. See `rendertest-event-analysis` for diff paging and scope.
- Editing constant buffers and editing or replacing shaders are not available. When a conclusion needs an experiment, describe the experiment instead of claiming it was run.
