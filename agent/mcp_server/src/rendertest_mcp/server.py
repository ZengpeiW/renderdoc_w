from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .errors import RenderTestAgentError
from .ipc_client import BridgeClient, BridgeResponse
from .protocol import (EFFECT_ARTIFACT, EFFECT_READ, EFFECT_UI, LONG_TIMEOUT_SECONDS, METHOD_SPECS,
                       RESULT_SCHEMA)

Stage = Literal["vertex", "hull", "domain", "geometry", "pixel", "compute", "amplification", "mesh"]
ActionFlag = Literal["draw", "dispatch", "marker", "clear", "copy", "resolve", "present"]
ResourceKind = Literal["texture", "buffer", "shader", "pipeline", "sampler", "other"]
TextureFormat = Literal["png", "exr", "dds"]
TextureChannels = Literal["rgba", "rgb", "r", "g", "b", "a", "depth", "stencil"]
HistoryOrder = Literal["newest", "oldest"]

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

_LONG_RUNNING_METHODS = frozenset({"export_texture", "get_buffer_data", "pixel_history"})
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


def create_server(client: BridgeClient | None = None) -> FastMCP:
    bridge = client or BridgeClient()
    server = FastMCP(
        name="rendertest",
        instructions=(
            "Inspect the capture currently open in RenderTest. Tools are read-only unless their "
            "effect is ui or artifact. ResourceId values are exact decimal strings."
        ),
    )

    def tool(**options: Any):
        def register(function: Any) -> Any:
            annotations = _ANNOTATIONS[tool_effect(function.__name__)]
            return server.tool(annotations=annotations, **options)(function)

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
        return _envelope(response, METHOD_SPECS[method]["effect"])

    @tool()
    async def list_instances(include_offline: bool = False) -> dict[str, Any]:
        """List RenderTest instances that expose the agent bridge."""
        instances = await anyio.to_thread.run_sync(
            partial(bridge.list_instances, include_offline=include_offline)
        )
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
    ) -> dict[str, Any]:
        """Read one constant buffer by reflected index or name."""
        if (index is None) == (name is None):
            raise _tool_error(
                RenderTestAgentError("INVALID_REQUEST", "Specify exactly one of index or name.")
            )
        return await invoke(
            "get_cbuffer",
            _clean_params(stage=stage, event_id=event_id, index=index, name=name),
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
        content: list[Any] = [result]
        if include_image:
            for artifact in result["artifacts"]:
                path = Path(artifact["path"])
                image_format = _IMAGE_FORMATS.get(path.suffix.lower())
                if image_format and artifact.get("bytes", 0) <= 8 * 1024 * 1024:
                    content.append(Image(path=path, format=image_format))
                    break
        return content

    @tool()
    async def pick_pixel(
        resource_id: ResourceId,
        x: NonNegativeInt,
        y: NonNegativeInt,
        event_id: OptionalEventId = None,
        mip: NonNegativeInt = 0,
        slice: NonNegativeInt = 0,
        sample: NonNegativeInt = 0,
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
                order=order,
                include_fragments=include_fragments,
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
