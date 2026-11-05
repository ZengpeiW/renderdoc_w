from __future__ import annotations

from typing import Any

PROTOCOL = "rendertest-agent-ipc/v1"
INSTANCE_SCHEMA = "rendertest-agent-instance/v1"
REQUEST_SCHEMA = "rendertest-agent-request/v1"
RESPONSE_SCHEMA = "rendertest-agent-response/v1"
RESULT_SCHEMA = "rendertest-mcp-result/v1"

CLIENT_NAME = "rendertest-mcp"
CLIENT_VERSION = "0.1.0"

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DEADLINE_MS = 10 * 60 * 1000
DEFAULT_TIMEOUT_SECONDS = 30.0
LONG_TIMEOUT_SECONDS = 120.0
INSTANCE_STALE_SECONDS = 5.0

EFFECT_READ = "read"
EFFECT_UI = "ui"
EFFECT_ARTIFACT = "artifact"

ERROR_RETRYABLE = {
    "INVALID_REQUEST": False,
    "UNKNOWN_METHOD": False,
    "NO_CAPTURE": False,
    "CAPTURE_MISMATCH": False,
    "CAPTURE_CHANGED": True,
    "INVALID_EVENT": False,
    "INVALID_RESOURCE": False,
    "UNSUPPORTED": False,
    "LIMIT_EXCEEDED": False,
    "BRIDGE_BUSY": True,
    "TIMEOUT": True,
    "CANCELLED": True,
    "BRIDGE_RESTARTED": True,
    "REPLAY_ERROR": False,
    "INTERNAL_ERROR": False,
    "BRIDGE_UNAVAILABLE": True,
    "INSTANCE_AMBIGUOUS": False,
    "INVALID_RESPONSE": False,
}


def _method(effect: str, requires_capture: bool = True) -> dict[str, Any]:
    return {"effect": effect, "requiresCapture": requires_capture}


METHOD_SPECS: dict[str, dict[str, Any]] = {
    "ping": _method(EFFECT_READ, False),
    "get_status": _method(EFFECT_READ, False),
    "get_context": _method(EFFECT_READ, False),
    "list_actions": _method(EFFECT_READ),
    "find_actions": _method(EFFECT_READ),
    "inspect_event": _method(EFFECT_READ),
    "set_event": _method(EFFECT_UI),
    "get_pipeline_state": _method(EFFECT_READ),
    "get_shader": _method(EFFECT_READ),
    "get_cbuffer": _method(EFFECT_READ),
    "list_resources": _method(EFFECT_READ),
    "get_resource": _method(EFFECT_READ),
    "get_resource_usage": _method(EFFECT_READ),
    "export_texture": _method(EFFECT_ARTIFACT),
    "pick_pixel": _method(EFFECT_READ),
    "get_texture_stats": _method(EFFECT_READ),
    "get_buffer_data": _method(EFFECT_READ),
    "get_debug_messages": _method(EFFECT_READ),
    "pixel_history": _method(EFFECT_READ),
    "debug_pixel": _method(EFFECT_ARTIFACT),
    "debug_vertex": _method(EFFECT_ARTIFACT),
    "get_mesh_data": _method(EFFECT_READ),
}

UNBOUND_METHODS = frozenset({"ping", "get_status", "get_context"})

P0_TOOL_NAMES = (
    "list_instances",
    "get_status",
    "get_context",
    "list_actions",
    "find_actions",
    "inspect_event",
    "set_event",
    "get_pipeline_state",
    "get_shader",
    "get_cbuffer",
    "list_resources",
    "get_resource",
    "get_resource_usage",
    "export_texture",
    "pick_pixel",
    "get_texture_stats",
    "get_buffer_data",
    "get_debug_messages",
)

P1_TOOL_NAMES = ("pixel_history", "debug_pixel")

TOOL_NAMES = P0_TOOL_NAMES + P1_TOOL_NAMES
