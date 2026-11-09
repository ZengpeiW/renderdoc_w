"""Per-capture indexes built on the replay thread and cached for the capture session."""

import threading

from .values import enum_name, flag_names, flags_value, format_name, resource_id

MARKER_FLAGS = ("SetMarker", "PushMarker", "PopMarker")


class SessionCache(object):
    """Values keyed by name, dropped as soon as a different capture session asks for them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._session = ""
        self._values = {}

    def get(self, session, key, build):
        with self._lock:
            if session != self._session:
                self._session = session
                self._values = {}
            if key in self._values:
                return self._values[key]
        value = build()
        with self._lock:
            if session == self._session:
                self._values[key] = value
        return value

    def clear(self):
        with self._lock:
            self._session = ""
            self._values = {}


def _dims(value):
    try:
        return [int(item) for item in value]
    except TypeError:
        return []


class ActionIndex(object):
    """Flattened action tree plus a map from every API event to its owning action."""

    def __init__(self):
        self.nodes = {}
        self.order = []
        self.roots = []
        self.owner = {}
        # Keep only primitive API-event metadata here, never native structured-data objects.
        self.api_events = {}
        self.action_api_events = {}
        self.last_event_id = 0

    @classmethod
    def build(cls, controller):
        index = cls()
        structured = controller.GetStructuredFile()
        stack = [(action, None) for action in reversed(list(controller.GetRootActions()))]
        while stack:
            action, parent = stack.pop()
            children = list(action.children)
            node = cls._describe(action, structured, parent, children)
            event_id = node["eventId"]
            api_events = []
            for event in action.events:
                api_event_id = int(event.eventId)
                chunk_index = getattr(event, "chunkIndex", None)
                record = {"apiEventId": api_event_id, "actionEventId": event_id,
                          "chunkIndex": None if chunk_index is None else int(chunk_index)}
                api_events.append(record)
                index.api_events[api_event_id] = record
                index.owner[api_event_id] = event_id
            index.action_api_events[event_id] = api_events
            index.owner[event_id] = event_id
            index.nodes[event_id] = node
            index.order.append(event_id)
            index.last_event_id = max(index.last_event_id, event_id)
            if parent is None:
                index.roots.append(event_id)
            for child in reversed(children):
                stack.append((child, event_id))
        return index

    @staticmethod
    def _describe(action, structured, parent, children):
        try:
            name = action.GetName(structured)
        except Exception:
            name = getattr(action, "customName", "") or ""
        flags = action.flags
        events = list(action.events)
        node = {
            "eventId": int(action.eventId),
            "actionId": int(action.actionId),
            "name": str(name),
            "flags": flag_names(flags),
            "flagsValue": flags_value(flags),
            "parentEventId": parent,
            "children": [int(child.eventId) for child in children],
            "firstEventId": int(events[0].eventId) if events else int(action.eventId),
            "numIndices": int(action.numIndices),
            "numInstances": int(action.numInstances),
            "baseVertex": int(action.baseVertex),
            "indexOffset": int(action.indexOffset),
            "vertexOffset": int(action.vertexOffset),
            "instanceOffset": int(action.instanceOffset),
            "drawIndex": int(action.drawIndex),
            "dispatchDimension": _dims(action.dispatchDimension),
            "dispatchThreadsDimension": _dims(action.dispatchThreadsDimension),
            "copySource": resource_id(action.copySource),
            "copyDestination": resource_id(action.copyDestination),
            "outputs": [item for item in (resource_id(rid) for rid in action.outputs) if item],
            "depthOut": resource_id(action.depthOut),
        }
        return node

    def has_event(self, event_id):
        return event_id in self.owner

    def action_for(self, event_id):
        owner = self.owner.get(event_id)
        return self.nodes.get(owner) if owner is not None else None

    def is_marker(self, node):
        return any(flag in MARKER_FLAGS for flag in node["flags"])

    def path(self, event_id):
        names = []
        node = self.nodes.get(event_id)
        parent = node["parentEventId"] if node else None
        while parent is not None:
            parent_node = self.nodes[parent]
            names.append(parent_node["name"])
            parent = parent_node["parentEventId"]
        names.reverse()
        return names

    def summary(self, node, with_path=False):
        item = {
            "eventId": node["eventId"],
            "actionId": node["actionId"],
            "name": node["name"],
            "flags": list(node["flags"]),
            "flagsValue": node["flagsValue"],
            "parentEventId": node["parentEventId"],
            "childCount": len(node["children"]),
        }
        if with_path:
            item["path"] = self.path(node["eventId"])
        return item

    def children(self, parent_event_id, include_markers):
        ids = self.roots if parent_event_id is None else self.nodes[parent_event_id]["children"]
        if include_markers:
            return [self.nodes[item] for item in ids]
        result = []
        stack = list(reversed(ids))
        while stack:
            node = self.nodes[stack.pop()]
            if self.is_marker(node):
                stack.extend(reversed(node["children"]))
            else:
                result.append(node)
        return result


def resource_kind(type_name):
    if type_name in ("Texture", "SwapchainImage"):
        return "texture"
    if type_name == "Buffer":
        return "buffer"
    if type_name == "Shader":
        return "shader"
    if type_name in ("PipelineState", "StateObject", "ShaderBinding"):
        return "pipeline"
    if type_name == "Sampler":
        return "sampler"
    return "other"


def texture_summary(texture):
    return {
        "width": int(texture.width),
        "height": int(texture.height),
        "depth": int(texture.depth),
        "mips": int(texture.mips),
        "arraySize": int(texture.arraysize),
        "samples": int(texture.msSamp),
        "format": format_name(texture.format),
        "dimension": int(texture.dimension),
        "textureType": enum_name(texture.type),
        "cubemap": bool(texture.cubemap),
    }


class ResourceIndex(object):
    """Resource descriptions keyed by decimal ResourceId.

    The description objects are kept so their ``resourceId`` members can be passed back to the
    replay controller; Python cannot build a ResourceId from an integer.
    """

    def __init__(self):
        self.items = {}
        self.order = []
        self.descriptions = {}
        self.textures = {}
        self.buffers = {}

    @classmethod
    def build(cls, controller):
        index = cls()
        for description in controller.GetResources():
            key = resource_id(description.resourceId)
            if key is None:
                continue
            type_name = enum_name(description.type)
            index.items[key] = {
                "resourceId": key,
                "name": str(description.name),
                "type": type_name,
                "kind": resource_kind(type_name),
                "autogeneratedName": bool(description.autogeneratedName),
            }
            index.descriptions[key] = description
            index.order.append(key)
        for texture in controller.GetTextures():
            key = resource_id(texture.resourceId)
            if key is None:
                continue
            index.textures[key] = texture
            index._ensure(key, texture, "Texture")["kind"] = "texture"
        for buffer in controller.GetBuffers():
            key = resource_id(buffer.resourceId)
            if key is None:
                continue
            index.buffers[key] = buffer
            index._ensure(key, buffer, "Buffer")["kind"] = "buffer"
        index.order.sort(key=int)
        return index

    def _ensure(self, key, description, type_name):
        item = self.items.get(key)
        if item is None:
            item = {"resourceId": key, "name": "%s %s" % (type_name, key), "type": type_name,
                    "kind": resource_kind(type_name), "autogeneratedName": True}
            self.items[key] = item
            self.descriptions[key] = description
            self.order.append(key)
        return item

    def name(self, key):
        item = self.items.get(key) if key else None
        return item["name"] if item else None

    def handle(self, key):
        """Return a live ResourceId for ``key``, or None when the capture has no such resource."""
        description = self.descriptions.get(key)
        return description.resourceId if description is not None else None

    def summary(self, key):
        item = dict(self.items[key])
        if key in self.textures:
            item.update(texture_summary(self.textures[key]))
        elif key in self.buffers:
            item["byteLength"] = int(self.buffers[key].length)
        return item

    def ref(self, key):
        if key is None:
            return None
        return {"resourceId": key, "name": self.name(key)}
