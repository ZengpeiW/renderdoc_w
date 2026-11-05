---
name: rendertest-shader-debug
description: Inspect the shaders bound at a RenderTest event - reflection, resource bindings, constant buffer values and disassembly - to explain a calculation or find parameters that affect a result. Use for shader logic, CBuffer, sampler or binding questions about a RenderDoc capture.
---

# Inspect shaders

Follow `rendertest-core`.

## Workflow

1. Pick the EID and stage. `inspect_event` lists the bound stages under `pipeline.shaders` (vertex, hull, domain, geometry, pixel, compute, amplification, mesh); an unbound stage returns `INVALID_EVENT`.
2. Call `get_shader(stage=..., event_id=...)` without disassembly first. It returns the ResourceId, entry point, input and output signatures, constant buffer layouts, read-only and read-write resources, samplers and the available `disassemblyTargets`.
3. Actual bindings come from `get_pipeline_state(stages=[...])` under `bindings`. There `name` is the binding name in the shader and `resourceName` / `resourceId` identify the bound resource. Bindings with `staticallyUnused=true` are not read.
4. Constant buffer values: `get_cbuffer(stage=..., event_id=..., index=...)`, or pass `name` instead of `index`, never both. Results carry field names when reflection has them, types, byte offsets and values; mention truncated results.
5. Read disassembly only when you need the arithmetic: `get_shader(include_disassembly=true, target=...)`. Output over 256 KiB is written to `disassemblyArtifact`; read only the relevant parts.
6. Verify sampled texture content with `pick_pixel` or `export_texture`, not from texture names.

## Judgement

- Disassembly (DXIL, DXBC, SPIR-V...) is not source code; do not call it source. Names may be missing, so cite registers or byte offsets together with your inferred meaning.
- For "which parameters affect the colour", trace back from the output write: which inputs, texture samples and constant fields feed it. Then list their actual values and anything suspicious: 0, NaN, Inf, extreme values, mismatches with similar draws, sRGB formats read as data.
- To check a suspicious parameter, find other draws using the same shader with `find_actions` and compare their `get_cbuffer` values.
- Editing constant buffers, editing or replacing shaders and shader stepping are not available (`debug_pixel` is planned for M4). When a conclusion needs an experiment, describe the experiment instead of claiming it was run.
