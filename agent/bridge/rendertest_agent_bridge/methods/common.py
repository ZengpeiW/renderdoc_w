import re

from ..indexes import ActionIndex, ResourceIndex
from ..ipc import AgentBridgeError, BridgeResult

RESOURCE_ID_PATTERN = re.compile(r"^[0-9]{1,20}$")

STAGES = (
    ("vertex", ("Vertex",)),
    ("hull", ("Hull", "Tess_Control")),
    ("domain", ("Domain", "Tess_Eval")),
    ("geometry", ("Geometry",)),
    ("pixel", ("Pixel", "Fragment")),
    ("compute", ("Compute",)),
    ("amplification", ("Amplification", "Task")),
    ("mesh", ("Mesh",)),
)
STAGE_NAMES = tuple(name for name, _ in STAGES)


def payload(data, artifacts=None, warnings=None, next_cursor=None, truncated=False):
    return {"data": data, "artifacts": artifacts or [], "warnings": warnings or [],
            "nextCursor": next_cursor, "truncated": bool(truncated)}


def renderdoc_module():
    import renderdoc
    return renderdoc


def result(data, meta=None, **kwargs):
    return BridgeResult(payload(data, **kwargs), meta or {})


def invalid(message, **details):
    return AgentBridgeError("INVALID_REQUEST", message, details=details)


def get_int(params, name, default=None, minimum=0, maximum=None, required=False):
    value = params.get(name, default)
    if value is None:
        if required:
            raise invalid("%s is required." % name)
        return None
    if type(value) is not int:
        raise invalid("%s must be an integer." % name)
    if minimum is not None and value < minimum:
        raise invalid("%s must be at least %d." % (name, minimum))
    if maximum is not None and value > maximum:
        raise invalid("%s must be at most %d." % (name, maximum))
    return value


def get_bool(params, name, default):
    value = params.get(name, default)
    if type(value) is not bool:
        raise invalid("%s must be a boolean." % name)
    return value


def get_str(params, name, default=None, max_length=256, required=False):
    value = params.get(name, default)
    if value is None:
        if required:
            raise invalid("%s is required." % name)
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise invalid("%s must be a string of at most %d characters." % (name, max_length))
    return value


def get_choice(params, name, choices, default=None, required=False):
    value = get_str(params, name, default, required=required)
    if value is not None and value not in choices:
        raise invalid("%s must be one of %s." % (name, ", ".join(choices)), allowed=list(choices))
    return value


def get_choices(params, name, choices):
    value = params.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or any(item not in choices for item in value):
        raise invalid("%s must be a list drawn from %s." % (name, ", ".join(choices)),
                      allowed=list(choices))
    return value


def get_resource_key(params, name="resource_id"):
    value = params.get(name)
    if not isinstance(value, str) or not RESOURCE_ID_PATTERN.match(value):
        raise invalid("%s must be a decimal ResourceId string." % name)
    return value


def stage_enum(rd, name):
    for stage_name, attributes in STAGES:
        if stage_name == name:
            for attribute in attributes:
                value = getattr(rd.ShaderStage, attribute, None)
                if value is not None:
                    return value
    raise invalid("Unknown shader stage %s." % name, allowed=list(STAGE_NAMES))


def session_of(context):
    return context.state.get("captureSessionId") or ""


def action_index(runtime, context):
    session = session_of(context)
    return runtime.replay.call(
        context, lambda controller: runtime.cache.get(
            session, "actions", lambda: ActionIndex.build(controller)))


def resource_index(runtime, context):
    session = session_of(context)
    return runtime.replay.call(
        context, lambda controller: runtime.cache.get(
            session, "resources", lambda: ResourceIndex.build(controller)))


def resolve_event(context, actions, name="event_id"):
    """Return the requested EID, defaulting to the UI's current event, validated against actions."""
    event_id = get_int(context.params, name)
    if event_id is None:
        event_id = context.state.get("currentEventId")
        if event_id is None:
            raise AgentBridgeError("INVALID_EVENT", "The UI has no current event.")
    if not actions.has_event(event_id):
        raise AgentBridgeError("INVALID_EVENT",
                               "Event %d does not exist in the loaded capture." % event_id,
                               details={"event_id": event_id, "lastEventId": actions.last_event_id})
    return event_id


def require_resource(resources, key, kind=None):
    if key not in resources.items:
        raise AgentBridgeError("INVALID_RESOURCE",
                               "Resource %s does not exist in the loaded capture." % key,
                               details={"resource_id": key})
    if kind == "texture" and key not in resources.textures:
        raise AgentBridgeError("INVALID_RESOURCE", "Resource %s is not a texture." % key,
                               details={"resource_id": key,
                                        "type": resources.items[key]["type"]})
    if kind == "buffer" and key not in resources.buffers:
        raise AgentBridgeError("INVALID_RESOURCE", "Resource %s is not a buffer." % key,
                               details={"resource_id": key,
                                        "type": resources.items[key]["type"]})
    return resources.handle(key)
