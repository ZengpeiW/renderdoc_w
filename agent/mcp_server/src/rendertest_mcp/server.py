from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import ArtifactReader, DEFAULT_PAGE_BYTES, MAX_PAGE_BYTES
from .errors import RenderTestAgentError
from .ipc_client import BridgeClient, BridgeResponse
from .protocol import (EFFECT_ARTIFACT, EFFECT_READ, EFFECT_UI, LONG_TIMEOUT_SECONDS, METHOD_SPECS,
                       RESULT_SCHEMA)

Stage = Literal["vertex", "hull", "domain", "geometry", "pixel", "compute", "amplification", "mesh"]
ActionFlag = Literal["draw", "dispatch", "marker", "clear", "copy", "resolve", "present"]
ResourceKind = Literal["texture", "buffer", "shader", "pipeline", "sampler", "other"]
ResourceStatsKind = Literal["all", "texture", "buffer"]
TextureFormat = Literal["png", "exr", "dds"]
TextureChannels = Literal["rgba", "rgb", "r", "g", "b", "a", "depth", "stencil"]
TextureTypeCast = Literal["Typeless", "Float", "UNorm", "SNorm", "UInt", "SInt", "UScaled",
                          "SScaled", "Depth", "UNormSRGB"]
DiagnosticOverlay = Literal["Drawcall", "Wireframe", "Depth", "Stencil", "BackfaceCull",
                            "ViewportScissor", "QuadOverdrawDraw", "TriangleSizeDraw", "NaN",
                            "Clipping"]
ShaderLanguage = Literal["auto", "hlsl", "glsl"]
FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
HistoryOrder = Literal["newest", "oldest"]
MeshStage = Literal["vs_out", "gs_out"]
MaxSteps = Annotated[int, Field(ge=1, le=200000, description="Shader steps to execute at most.")]

EventId = Annotated[int, Field(ge=0, description="RenderDoc event ID (EID).")]
OptionalEventId = Annotated[
    int | None,
    Field(ge=0, description="RenderDoc EID. Omit to use the current replay event."),
]
ResourceId = Annotated[
    str, Field(pattern=r"^[0-9]{1,20}$", description="Exact decimal RenderDoc ResourceId.")
]
InstanceId = Annotated[
    str | None,
    Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        description="RenderTest instance ID. Omit when only one instance is online.",
    ),
]
Cursor = Annotated[str | None, Field(max_length=1024, description="Opaque pagination cursor.")]
Limit = Annotated[int, Field(ge=1, le=1000, description="Maximum number of items to return.")]
NonNegativeInt = Annotated[int, Field(ge=0)]
BufferSize = Annotated[int, Field(ge=1, le=16 * 1024 * 1024)]

# Keep existing tools' coercion behavior unchanged; new selectors and budgets must not
# silently turn JSON booleans, floats or strings into an event, file index or limit.
StrictOptionalEventId = Annotated[
    int | None,
    Field(strict=True, ge=0, description="RenderDoc EID. Omit to use the current replay event."),
]
StrictOptionalParentId = Annotated[
    int | None,
    Field(strict=True, ge=0,
          description="Action or marker EID selecting a subtree. Omit to summarize the whole frame."),
]
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictEventId = Annotated[int, Field(strict=True, ge=0, le=0xFFFFFFFF)]
StrictNullableEventId = Annotated[int | None, Field(strict=True, ge=0, le=0xFFFFFFFF)]
ComputeCoordinate = Annotated[
    list[Annotated[int, Field(strict=True, ge=0, le=0xFFFFFFFF)]],
    Field(strict=True, min_length=3, max_length=3,
          description="Three zero-based integer coordinates: x, y, z."),
]
StrictMaxSteps = Annotated[int, Field(strict=True, ge=1, le=200000)]
StrictLimit = Annotated[
    int, Field(strict=True, ge=1, le=1000, description="Maximum number of items to return.")
]
StrictDepth = Annotated[int, Field(strict=True, ge=0, le=4)]


class ComparisonCBuffer(BaseModel):
    """One bounded constant-block selection for an event comparison."""

    model_config = ConfigDict(extra="forbid")
    stage: Stage
    index: StrictNonNegativeInt | None = None
    name: Annotated[str | None, Field(strict=True, min_length=1, max_length=256)] = None
    member_path: Annotated[str, Field(strict=True, max_length=4096)] = ""
    max_depth: StrictDepth = 1
    member_limit: StrictLimit = 200

    @model_validator(mode="after")
    def require_one_selector(self) -> ComparisonCBuffer:
        if (self.index is None) == (self.name is None):
            raise ValueError("INVALID_REQUEST: Each CBuffer requires exactly one of index or name.")
        return self

_LONG_RUNNING_METHODS = frozenset({"export_texture", "export_overlay", "get_buffer_data", "pixel_history",
                                   "debug_pixel", "debug_vertex", "debug_compute", "get_mesh_data",
                                   "decompile_shader", "get_gpu_duration"})
_IMAGE_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}
_ANNOTATIONS = {
    EFFECT_READ: ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                                 openWorldHint=False),
    EFFECT_UI: ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                               openWorldHint=False),
    EFFECT_ARTIFACT: ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                     idempotentHint=False, openWorldHint=False),
}


def tool_effect(name: str) -> str:
    return METHOD_SPECS.get(name, {"effect": EFFECT_READ})["effect"]


def _clean_params(**params: Any) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


class _LiteralFieldMetadata(FuncMetadata):
    literal_fields: tuple[str, ...] = ()

    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        # FastMCP 1.x JSON-decodes some string inputs before Pydantic validation.
        # Preserve path strings such as [1], ["name"] and null, and prevent strict
        # compute coordinate arrays from accepting a JSON-encoded string instead.
        # Counter selectors/cursors must not turn the string null into a fresh full sample.
        other = {key: value for key, value in data.items() if key not in self.literal_fields}
        parsed = super().pre_parse_json(other)
        for key in self.literal_fields:
            if key in data:
                parsed[key] = data[key]
        return parsed


def _tool_error(error: RenderTestAgentError) -> ToolError:
    return ToolError(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True))


def _envelope(response: BridgeResponse, effect: str) -> dict[str, Any]:
    result = response.result
    if isinstance(result, dict) and "data" in result:
        data = result["data"]
        artifacts = result.get("artifacts", [])
        warnings = result.get("warnings", [])
        next_cursor = result.get("nextCursor")
        truncated = bool(result.get("truncated", response.meta.get("truncated", False)))
    else:
        data = result
        artifacts = []
        warnings = []
        next_cursor = None
        truncated = bool(response.meta.get("truncated", False))
    return {
        "schema": RESULT_SCHEMA,
        "instanceId": response.instance_id,
        "captureSessionId": response.meta.get("captureSessionId") or None,
        "effect": effect,
        "data": data,
        "artifacts": artifacts,
        "warnings": warnings,
        "truncated": truncated,
        "nextCursor": next_cursor,
        "meta": response.meta,
    }


def _with_image(result: dict[str, Any], include_image: bool) -> list[Any]:
    content: list[Any] = [result]
    if include_image:
        for artifact in result["artifacts"]:
            path = Path(artifact["path"])
            image_format = _IMAGE_FORMATS.get(path.suffix.lower())
            if image_format and artifact.get("bytes", 0) <= 8 * 1024 * 1024:
                content.append(Image(path=path, format=image_format))
                break
    return content


def create_server(client: BridgeClient | None = None) -> FastMCP:
    bridge = client or BridgeClient()
    artifacts = ArtifactReader(bridge)
    server = FastMCP(
        name="rendertest",
        instructions=(
            "Inspect the capture currently open in RenderTest. Tools are read-only unless their "
            "effect is ui or artifact. ResourceId values are exact decimal strings. "
            "Use read_artifact on returned text artifact paths to read shader disassembly and "
            "debug traces, including when shell tools are unavailable."
        ),
    )

    def tool(**options: Any):
        def register(function: Any) -> Any:
            annotations = _ANNOTATIONS[tool_effect(function.__name__)]
            registered = server.tool(annotations=annotations, **options)(function)
            literal_fields = {"get_cbuffer": ("member_path",),
                              "get_api_calls": ("parameter_path",),
                              "debug_compute": ("group", "thread"),
                              "list_gpu_counters": ("cursor",),
                              "get_gpu_duration": ("parent_event_id", "cursor")}.get(function.__name__)
            if literal_fields:
                # FastMCP exposes no per-argument pre-parser option. Keep this adapter
                # local to these fields, retaining generated input/output schemas.
                entry = server._tool_manager.get_tool(function.__name__)
                metadata = entry.fn_metadata
                entry.fn_metadata = _LiteralFieldMetadata(literal_fields=literal_fields, **{
                    key: getattr(metadata, key) for key in type(metadata).model_fields
                })
            return registered

        return register

    async def invoke(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        instance_id: str | None = None,
    ) -> dict[str, Any]:
        timeout = LONG_TIMEOUT_SECONDS if method in _LONG_RUNNING_METHODS else None
        try:
            response = await anyio.to_thread.run_sync(
                partial(
                    bridge.call,
                    method,
                    params or {},
                    instance_id=instance_id,
                    timeout=timeout,
                )
            )
        except RenderTestAgentError as error:
            raise _tool_error(error) from error
        await anyio.to_thread.run_sync(artifacts.register, response)
        return _envelope(response, METHOD_SPECS[method]["effect"])

    @tool()
    async def read_artifact(
        path: Annotated[str, Field(min_length=1, max_length=4096)],
        start_line: Annotated[int, Field(ge=1)] = 1,
        limit: Limit = 100,
        search: Annotated[str | None, Field(min_length=1, max_length=256)] = None,
        max_bytes: Annotated[int, Field(ge=256, le=MAX_PAGE_BYTES)] = DEFAULT_PAGE_BYTES,
    ) -> dict[str, Any]:
        """Read UTF-8 text from an artifact returned by a tool in this MCP process.

        Supports .txt/.jsonl/.json/.csv/.log files up to 64 MiB. Lines are 1-based; pass
        data.nextLine as start_line to continue. search is a case-sensitive literal substring,
        not a regex. max_bytes bounds JSON line entries; an oversized first matching line
        returns LIMIT_EXCEEDED, so skip it or search for other lines. File contents must still
        match the returned size and SHA-256. Restarting MCP requires regenerating the artifact.
        """
        try:
            return await anyio.to_thread.run_sync(partial(
                artifacts.read, path, start_line=start_line, limit=limit,
                search=search, max_bytes=max_bytes,
            ))
        except RenderTestAgentError as error:
            raise _tool_error(error) from error

    @tool()
    async def list_instances(include_offline: bool = False) -> dict[str, Any]:
        """List RenderTest instances that expose the agent bridge."""
        try:
            instances = await anyio.to_thread.run_sync(
                partial(bridge.list_instances, include_offline=include_offline)
            )
        except RenderTestAgentError as error:
            raise _tool_error(error) from error
        return {
            "schema": RESULT_SCHEMA,
            "instanceId": None,
            "captureSessionId": None,
            "effect": "read",
            "data": {"instances": instances},
            "artifacts": [],
            "warnings": [],
            "truncated": False,
            "nextCursor": None,
            "meta": {},
        }

    @tool()
    async def get_status(instance_id: InstanceId = None) -> dict[str, Any]:
        """Read bridge status and the currently loaded capture."""
        return await invoke("get_status", instance_id=instance_id)

    @tool()
    async def get_context(instance_id: InstanceId = None) -> dict[str, Any]:
        """Read the current and selected EID with a compact pipeline summary."""
        return await invoke("get_context", instance_id=instance_id)

    @tool()
    async def list_actions(
        parent_event_id: OptionalEventId = None,
        cursor: Cursor = None,
        limit: Limit = 200,
        include_markers: bool = True,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """List RenderDoc actions below a parent action, with pagination."""
        return await invoke(
            "list_actions",
            _clean_params(
                parent_event_id=parent_event_id,
                cursor=cursor,
                limit=limit,
                include_markers=include_markers,
            ),
            instance_id=instance_id,
        )

    @tool()
    async def find_actions(
        query: Annotated[str, Field(min_length=1, max_length=256)],
        flags: list[ActionFlag] | None = None,
        cursor: Cursor = None,
        limit: Limit = 200,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Search actions by name and action type."""
        return await invoke(
            "find_actions",
            _clean_params(query=query, flags=flags, cursor=cursor, limit=limit),
            instance_id=instance_id,
        )

    @tool()
    async def inspect_event(
        event_id: OptionalEventId = None, instance_id: InstanceId = None
    ) -> dict[str, Any]:
        """Summarize an action, its shaders, outputs and main bound resources."""
        return await invoke(
            "inspect_event", _clean_params(event_id=event_id), instance_id=instance_id
        )

    @tool()
    async def get_api_calls(
        event_id: StrictNullableEventId = None,
        api_event_id: StrictNullableEventId = None,
        parameter_path: Annotated[
            str | None, Field(strict=True, max_length=4096, pattern=r"^(?:\[[0-9]+\]){0,64}$")
        ] = None,
        cursor: Cursor = None,
        limit: StrictLimit = 100,
        max_depth: StrictDepth = 0,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Page the captured API calls belonging to an action, or inspect one call's parameters.

        Omit api_event_id to list calls owned by event_id (default: current UI event). Pass
        an exact returned apiEventId to inspect that call; an explicit event_id must have
        the same owner. parameter_path is an ordinal path such as [0][2]; an empty string
        selects the parameter root. max_depth=0 returns direct leaf values and metadata for
        nested structures, which can be expanded using their canonicalPath. NoChunk and
        unavailable data are reported explicitly. These are RenderDoc serialized fields,
        which may include internal/return fields, not a guaranteed verbatim API transcript.
        Large raw buffer contents are unavailable. This does not change the UI event.
        """
        if api_event_id is None and parameter_path is not None:
            raise _tool_error(RenderTestAgentError(
                "INVALID_REQUEST", "parameter_path requires api_event_id."))
        return await invoke(
            "get_api_calls",
            _clean_params(event_id=event_id, api_event_id=api_event_id,
                          parameter_path=parameter_path, cursor=cursor, limit=limit,
                          max_depth=max_depth),
            instance_id=instance_id,
        )

    @tool()
    async def compare_events(
        before_event_id: StrictEventId,
        after_event_id: StrictEventId,
        stages: list[Stage] | None = None,
        include_bindings: Annotated[bool, Field(strict=True)] = True,
        binding_limit: Annotated[int, Field(strict=True, ge=1, le=256)] = 64,
        cbuffers: Annotated[list[ComparisonCBuffer] | None, Field(max_length=8)] = None,
        cursor: Cursor = None,
        limit: StrictLimit = 200,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Compare two EIDs in the current capture and page their structured differences.

        Compares shaders, selected pipeline state and optionally used bindings. Up to eight
        CBuffer selections may name one stage and exactly one index/name, plus an optional
        member_path, max_depth (default 1) and member_limit (default 200). Empty member_path
        selects the block root. JSON Pointer difference paths distinguish missing from null.
        Read coverage and warnings: viewport/scissor 0, used descriptors and bounded CBuffer
        members are a limited scope. equal-in-scope does not mean identical rendered output;
        unavailable/truncated input can make a zero-difference comparison indeterminate.
        Both EIDs are read within one capture session and the UI event is restored.
        """
        selections = ([item.model_dump(exclude_none=True) for item in cbuffers]
                      if cbuffers is not None else None)
        return await invoke(
            "compare_events",
            _clean_params(before_event_id=before_event_id, after_event_id=after_event_id,
                          stages=stages, include_bindings=include_bindings,
                          binding_limit=binding_limit, cbuffers=selections,
                          cursor=cursor, limit=limit),
            instance_id=instance_id,
        )

    @tool()
    async def get_action_stats(
        parent_event_id: StrictOptionalParentId = None,
        cursor: Cursor = None,
        limit: StrictLimit = 200,
        top_n: StrictLimit = 20,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Count operational leaf actions in the frame or one action subtree.

        Returns counts, paged immediate-child groups, and bounded Draw/Dispatch rankings.
        Containers and markers are counted separately to avoid counting multi-draw parents
        twice. Draw rankings use numIndices * numInstances; compute rankings use group counts.
        These are not triangle counts, measured shader invocations, or GPU timings. nextCursor
        pages groups only; global counts and Top N rankings always cover the whole scope.
        """
        return await invoke(
            "get_action_stats",
            _clean_params(parent_event_id=parent_event_id, cursor=cursor, limit=limit, top_n=top_n),
            instance_id=instance_id,
        )

    @tool()
    async def list_gpu_counters(
        cursor: Cursor = None,
        limit: StrictLimit = 200,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """List the GPU counters available for this capture and replay implementation.

        Returns native descriptions, value types, byte widths, units and stable UUIDs.
        This only discovers capabilities; it does not sample counters or guarantee that a
        later sample succeeds. Only the built-in EventGPUDuration can currently be sampled,
        through get_gpu_duration; vendor counters are described but cannot be requested.
        Follow nextCursor to inspect the rest of the bounded directory.
        """
        return await invoke("list_gpu_counters", _clean_params(cursor=cursor, limit=limit),
                            instance_id=instance_id)

    @tool()
    async def get_gpu_duration(
        parent_event_id: StrictNullableEventId = None,
        cursor: Cursor = None,
        limit: StrictLimit = 200,
        sort_by: Literal["duration", "event_id"] = "duration",
        refresh: Annotated[bool, Field(strict=True)] = False,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Read replay GPU durations for the whole capture or one action subtree.

        Samples only native EventGPUDuration, in seconds. The first request and refresh=true
        replay the whole capture; parent_event_id, sorting and paging only filter results,
        not native sampling cost. The cached sampleId is reused across scopes and pages.
        Duration sorting ranks valid measured events first, greatest duration first; missing
        or invalid measurements remain unavailable, never zero. A native zero does not prove
        no GPU work. These measurements are replay timings, not original application frame
        time; summing events is not a reliable frame time or elapsed pass time.
        Check valid, invalid and missing counts and warnings. refresh cannot accompany cursor; successful refresh
        invalidates old cursors. Native fetch is not immediately cancellable; the 120-second
        MCP wait is not a hard native timeout. UI selection is preserved and replay restored.
        """
        if refresh and cursor is not None:
            raise _tool_error(RenderTestAgentError(
                "INVALID_REQUEST", "refresh cannot be combined with a continuation cursor."))
        return await invoke(
            "get_gpu_duration",
            _clean_params(parent_event_id=parent_event_id, cursor=cursor, limit=limit,
                          sort_by=sort_by, refresh=refresh),
            instance_id=instance_id,
        )

    @tool()
    async def set_event(event_id: EventId, instance_id: InstanceId = None) -> dict[str, Any]:
        """Change the selected EID in the RenderTest UI."""
        return await invoke("set_event", {"event_id": event_id}, instance_id=instance_id)

    @tool()
    async def get_pipeline_state(
        event_id: OptionalEventId = None,
        stages: list[Stage] | None = None,
        include_bindings: bool = True,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Read graphics or compute pipeline state at an EID."""
        return await invoke(
            "get_pipeline_state",
            _clean_params(event_id=event_id, stages=stages, include_bindings=include_bindings),
            instance_id=instance_id,
        )

    @tool()
    async def get_shader(
        stage: Stage,
        event_id: OptionalEventId = None,
        include_disassembly: bool = False,
        target: Annotated[str | None, Field(max_length=128)] = None,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Read shader identity, reflection, bindings and optional disassembly."""
        return await invoke(
            "get_shader",
            _clean_params(
                stage=stage,
                event_id=event_id,
                include_disassembly=include_disassembly,
                target=target,
            ),
            instance_id=instance_id,
        )

    @tool()
    async def get_cbuffer(
        stage: Stage,
        event_id: OptionalEventId = None,
        index: Annotated[int | None, Field(ge=0)] = None,
        name: Annotated[str | None, Field(min_length=1, max_length=256)] = None,
        instance_id: InstanceId = None,
        member_path: Annotated[str | None, Field(strict=True, max_length=4096)] = None,
        cursor: Cursor = None,
        limit: StrictLimit | None = None,
        max_depth: Annotated[int | None, Field(strict=True, ge=0, le=4)] = None,
    ) -> dict[str, Any]:
        """Read one constant buffer by reflected index or name.

        Omit the member/page options for the legacy full-buffer result. Use member_path=""
        for the root or a member path to drill into a large buffer, with bounded child pages.
        Page options require member_path (use "" for the root); max_depth defaults to 0.
        Follow nextCursor and check truncated/warnings; this never edits buffer contents.
        """
        if (index is None) == (name is None):
            raise _tool_error(
                RenderTestAgentError("INVALID_REQUEST", "Specify exactly one of index or name.")
            )
        if member_path is None and any(option is not None for option in (cursor, limit, max_depth)):
            raise _tool_error(RenderTestAgentError(
                "INVALID_REQUEST", "Paging options require member_path; use an empty string for the root."))
        return await invoke(
            "get_cbuffer",
            _clean_params(stage=stage, event_id=event_id, index=index, name=name,
                          member_path=member_path, cursor=cursor, limit=limit, max_depth=max_depth),
            instance_id=instance_id,
        )

    @tool()
    async def get_shader_source(
        stage: Stage,
        event_id: StrictOptionalEventId = None,
        file_index: StrictNonNegativeInt | None = None,
        cursor: Cursor = None,
        limit: StrictLimit = 100,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """List embedded shader source files, or export one file as a UTF-8 text artifact.

        Omit file_index to page the complete source directory; select its numeric index to
        export one file (up to 64 MiB), then use read_artifact for line paging or search.
        readArtifactCompatible/readArtifactReason flag lines exceeding the reader's budget.
        Files come only from the capture's shader reflection, never from filesystem paths.
        Reports unavailable when no source was embedded; this does not decompile bytecode.
        """
        return await invoke(
            "get_shader_source",
            _clean_params(stage=stage, event_id=event_id, file_index=file_index,
                          cursor=cursor, limit=limit),
            instance_id=instance_id,
        )

    @tool()
    async def decompile_shader(
        stage: Stage,
        event_id: StrictOptionalEventId = None,
        language: ShaderLanguage = "auto",
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Generate HLSL or GLSL from the shader bytecode bound at an event.

        auto tries HLSL first and falls back to GLSL with a warning only when SPIRV-Cross
        cannot generate HLSL. Explicit hlsl/glsl requests never switch languages. Check
        requestedLanguage and outputLanguage in the result.
        The generated code is not the original source and is not guaranteed to recompile
        or behave equivalently. Prefer get_shader_source when embedded source is available.
        Unsupported formats, stages or converter features return an error with details;
        a failed conversion is never substituted with disassembly. Read a successful text
        artifact with read_artifact for line paging or search. Does not replace the shader.
        """
        return await invoke(
            "decompile_shader",
            _clean_params(stage=stage, event_id=event_id, language=language),
            instance_id=instance_id,
        )

    @tool()
    async def list_resources(
        kind: ResourceKind | None = None,
        query: Annotated[str | None, Field(max_length=256)] = None,
        cursor: Cursor = None,
        limit: Limit = 200,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """List capture resources with optional type and name filters."""
        return await invoke(
            "list_resources",
            _clean_params(kind=kind, query=query, cursor=cursor, limit=limit),
            instance_id=instance_id,
        )

    @tool()
    async def get_resource(resource_id: ResourceId, instance_id: InstanceId = None) -> dict[str, Any]:
        """Read detailed metadata for one resource."""
        return await invoke("get_resource", {"resource_id": resource_id}, instance_id=instance_id)

    @tool()
    async def get_resource_stats(
        kind: ResourceStatsKind = "all",
        top_n: StrictLimit = 20,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Summarize texture/buffer counts, descriptor bytes, distributions and largest resources.

        all includes textures and buffers only. Sizes are texture.byteSize / buffer.length,
        not measured resident, peak, or unique-allocation GPU memory. Unknown sizes remain
        explicit and are excluded from byte sums and size rankings. This is distinct from
        get_texture_stats, which reads one texture view's pixel min/max values.
        """
        return await invoke("get_resource_stats", {"kind": kind, "top_n": top_n},
                            instance_id=instance_id)

    @tool()
    async def get_resource_usage(
        resource_id: ResourceId,
        cursor: Cursor = None,
        limit: Limit = 200,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """List how a resource is read or written across the capture."""
        return await invoke(
            "get_resource_usage",
            _clean_params(resource_id=resource_id, cursor=cursor, limit=limit),
            instance_id=instance_id,
        )

    @tool(structured_output=False)
    async def export_texture(
        resource_id: ResourceId,
        event_id: OptionalEventId = None,
        mip: NonNegativeInt = 0,
        slice: NonNegativeInt = 0,
        sample: NonNegativeInt = 0,
        format: TextureFormat = "png",
        channels: TextureChannels = "rgba",
        include_image: bool = False,
        instance_id: InstanceId = None,
    ) -> list[Any]:
        """Export a texture view to an artifact file and optionally attach the image."""
        result = await invoke(
            "export_texture",
            _clean_params(
                resource_id=resource_id,
                event_id=event_id,
                mip=mip,
                slice=slice,
                sample=sample,
                format=format,
                channels=channels,
            ),
            instance_id=instance_id,
        )
        return _with_image(result, include_image)

    @tool(structured_output=False)
    async def export_overlay(
        resource_id: ResourceId,
        overlay: DiagnosticOverlay,
        event_id: StrictOptionalEventId = None,
        mip: StrictNonNegativeInt = 0,
        slice: StrictNonNegativeInt = 0,
        sample: StrictNonNegativeInt = 0,
        type_cast: TextureTypeCast = "Typeless",
        range_min: FiniteNumber = 0.0,
        range_max: FiniteNumber = 1.0,
        max_dimension: Annotated[int, Field(strict=True, ge=1, le=4096)] = 1024,
        include_image: Annotated[bool, Field(strict=True)] = False,
        instance_id: InstanceId = None,
    ) -> list[Any]:
        """Export a diagnostic overlay for one event and texture view as a PNG artifact.

        Optionally attach the image for visual inspection. Diagnostic colours are not raw
        texture values or GPU timings. Unsupported API/overlay combinations return an error;
        the GUI event and texture display are preserved. range_min must be below range_max.
        """
        if range_min >= range_max:
            raise _tool_error(RenderTestAgentError(
                "INVALID_REQUEST", "range_min must be less than range_max."))
        result = await invoke(
            "export_overlay",
            _clean_params(resource_id=resource_id, overlay=overlay, event_id=event_id,
                          mip=mip, slice=slice, sample=sample, type_cast=type_cast,
                          range_min=range_min, range_max=range_max, max_dimension=max_dimension),
            instance_id=instance_id,
        )
        return _with_image(result, include_image)

    @tool()
    async def pick_pixel(
        resource_id: ResourceId,
        x: NonNegativeInt,
        y: NonNegativeInt,
        event_id: OptionalEventId = None,
        mip: NonNegativeInt = 0,
        slice: NonNegativeInt = 0,
        sample: NonNegativeInt = 0,
        type_cast: TextureTypeCast = "Typeless",
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Read one texel from a texture view."""
        return await invoke(
            "pick_pixel",
            _clean_params(
                resource_id=resource_id,
                x=x,
                y=y,
                event_id=event_id,
                mip=mip,
                slice=slice,
                sample=sample,
                type_cast=type_cast,
            ),
            instance_id=instance_id,
        )

    @tool()
    async def get_texture_stats(
        resource_id: ResourceId,
        event_id: OptionalEventId = None,
        mip: NonNegativeInt = 0,
        slice: NonNegativeInt = 0,
        sample: NonNegativeInt = 0,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Read min and max values for a texture view."""
        return await invoke(
            "get_texture_stats",
            _clean_params(
                resource_id=resource_id,
                event_id=event_id,
                mip=mip,
                slice=slice,
                sample=sample,
            ),
            instance_id=instance_id,
        )

    @tool()
    async def pixel_history(
        resource_id: ResourceId,
        x: NonNegativeInt,
        y: NonNegativeInt,
        event_id: OptionalEventId = None,
        mip: NonNegativeInt = 0,
        slice: NonNegativeInt = 0,
        sample: NonNegativeInt = 0,
        type_cast: TextureTypeCast = "Typeless",
        order: HistoryOrder = "newest",
        include_fragments: bool = False,
        cursor: Cursor = None,
        limit: Limit = 50,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """List the events that modified one texel up to an EID (top-left origin).

        Each event has its fragment counts, failure reasons (depth/stencil/discard/...), and the
        value before the event, from the shader and after it. The summary names the last passing
        and last changing events. include_fragments adds up to 64 per-fragment entries per event.
        This replays the frame and can take seconds on large captures.
        """
        return await invoke(
            "pixel_history",
            _clean_params(
                resource_id=resource_id,
                x=x,
                y=y,
                event_id=event_id,
                mip=mip,
                slice=slice,
                sample=sample,
                type_cast=type_cast,
                order=order,
                include_fragments=include_fragments,
                cursor=cursor,
                limit=limit,
            ),
            instance_id=instance_id,
        )

    @tool()
    async def debug_pixel(
        x: NonNegativeInt,
        y: NonNegativeInt,
        event_id: OptionalEventId = None,
        sample: NonNegativeInt | None = None,
        primitive: NonNegativeInt | None = None,
        view: NonNegativeInt | None = None,
        max_steps: MaxSteps = 20000,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Run the pixel shader of a draw at one pixel in RenderDoc's shader debugger.

        Coordinates are framebuffer pixels with a top-left origin. Returns the shader, its inputs,
        the final values of its outputs (SV_Target etc.), the last changed variables and steps that
        generated NaN/Inf. The full step-by-step trace is written to a JSONL artifact. primitive
        picks a fragment when several overlap (see pixel_history primitiveIds); omit it for the
        fragment RenderDoc chooses.
        """
        return await invoke(
            "debug_pixel",
            _clean_params(
                x=x,
                y=y,
                event_id=event_id,
                sample=sample,
                primitive=primitive,
                view=view,
                max_steps=max_steps,
            ),
            instance_id=instance_id,
        )

    @tool()
    async def debug_vertex(
        vertex: NonNegativeInt,
        event_id: OptionalEventId = None,
        instance: NonNegativeInt = 0,
        view: NonNegativeInt = 0,
        max_steps: MaxSteps = 20000,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Run the vertex shader of a draw for one vertex in RenderDoc's shader debugger.

        vertex is the 0-based position in the draw (the VTX column of the Mesh Viewer, below
        numIndices); for indexed draws the bridge reads the index buffer and reports index (the
        fetched vertex, base vertex applied). Returns the shader, its inputs, the final outputs
        (SV_Position etc.), the last changed variables and steps that generated NaN/Inf. The
        full step-by-step trace is written to a JSONL artifact.
        """
        return await invoke(
            "debug_vertex",
            _clean_params(
                vertex=vertex,
                event_id=event_id,
                instance=instance,
                view=view,
                max_steps=max_steps,
            ),
            instance_id=instance_id,
        )

    @tool()
    async def debug_compute(
        group: ComputeCoordinate,
        thread: ComputeCoordinate,
        event_id: StrictNullableEventId = None,
        max_steps: StrictMaxSteps = 20000,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Debug one compute invocation of a dispatch in RenderDoc's shader debugger.

        group is the zero-based workgroup coordinate; thread is the local coordinate within
        that group, not a global dispatch-thread ID. Each is [x, y, z]. Select an actual
        dispatch EID; coordinates must fit its group counts and effective thread-group size.
        Returns debugger inputs, observed changes and completion metadata, with the detailed
        trace in a JSONL artifact readable through read_artifact. Debugger output variables
        are not a complete log of UAV writes or proof of final resource contents. Inspect
        executionComplete, traceComplete, truncation and warnings before drawing conclusions.
        Unsupported shaders/APIs return an explicit error. The UI event is preserved.
        """
        return await invoke(
            "debug_compute",
            _clean_params(group=group, thread=thread, event_id=event_id, max_steps=max_steps),
            instance_id=instance_id,
        )

    @tool()
    async def get_mesh_data(
        event_id: OptionalEventId = None,
        stage: MeshStage = "vs_out",
        instance: NonNegativeInt = 0,
        view: NonNegativeInt = 0,
        start: NonNegativeInt = 0,
        cursor: Cursor = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 50,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Read a draw's post-transform vertices, like the VS Out / GS Out tables of the Mesh Viewer.

        Returns the output columns (position first), per-column min/max and non-finite row counts,
        the position's NDC range and rows with clip-space positions and other outputs. vertex is
        the row in the draw; index is the vertex fetched for it (base vertex applied). stage=gs_out
        reads geometry/tessellation output. Stats cover the whole draw; rows are paged with
        start/limit or cursor.
        """
        return await invoke(
            "get_mesh_data",
            _clean_params(
                event_id=event_id,
                stage=stage,
                instance=instance,
                view=view,
                start=start,
                cursor=cursor,
                limit=limit,
            ),
            instance_id=instance_id,
        )
    @tool()
    async def get_buffer_data(
        resource_id: ResourceId,
        offset: NonNegativeInt = 0,
        size: BufferSize = 256,
        event_id: OptionalEventId = None,
        format: Annotated[str | None, Field(max_length=256)] = None,
        instance_id: InstanceId = None,
    ) -> dict[str, Any]:
        """Read a bounded buffer byte range inline or as an artifact."""
        return await invoke(
            "get_buffer_data",
            _clean_params(
                resource_id=resource_id,
                offset=offset,
                size=size,
                event_id=event_id,
                format=format,
            ),
            instance_id=instance_id,
        )

    @tool()
    async def get_debug_messages(
        cursor: Cursor = None, limit: Limit = 200, instance_id: InstanceId = None
    ) -> dict[str, Any]:
        """Read API validation and driver debug messages from the capture."""
        return await invoke(
            "get_debug_messages", _clean_params(cursor=cursor, limit=limit), instance_id=instance_id
        )

    return server


def main() -> None:
    create_server().run(transport="stdio")
