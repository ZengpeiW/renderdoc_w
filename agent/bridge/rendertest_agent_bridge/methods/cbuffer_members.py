"""Bounded projections of the native constant-buffer variable tree.

This module deliberately does not read bytes or resolve shader bindings. The caller supplies the
original GetCBufferVariableContents result, reflection and the existing value converter while still
inside the replay callback. Numeric path components address direct children, including array
elements; emitted ordinal paths remain unambiguous when native names are duplicated or decorated.
"""

import json
import re

from ..ipc import AgentBridgeError
from ..paging import decode_cursor, encode_cursor, query_key
from .common import get_int, invalid

MAX_PATH_LENGTH = 4096
MAX_PATH_COMPONENTS = 64
MAX_DEPTH = 4
MAX_NODES = 4096
MAX_PAYLOAD_BYTES = 256 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_INDEX = re.compile(r"(?:0|[1-9][0-9]*)")


def _parse_path(path):
    if not isinstance(path, str) or len(path) > MAX_PATH_LENGTH:
        raise invalid("member_path must be a string of at most %d characters." % MAX_PATH_LENGTH)
    tokens = []
    position = 0
    decoder = json.JSONDecoder()
    while position < len(path):
        if len(tokens) >= MAX_PATH_COMPONENTS:
            raise invalid("member_path has more than %d components." % MAX_PATH_COMPONENTS)
        if path[position] == "[":
            position += 1
            if position < len(path) and path[position] == '"':
                try:
                    token, position = decoder.raw_decode(path, position)
                except ValueError:
                    raise invalid("member_path contains an invalid JSON-quoted name.")
            else:
                match = _INDEX.match(path, position)
                if match is None:
                    raise invalid("member_path brackets require an unsigned index or JSON string.")
                token = int(match.group(0))
                position = match.end()
            if position >= len(path) or path[position] != "]":
                raise invalid("member_path contains an unterminated bracket component.")
            position += 1
        else:
            match = _IDENTIFIER.match(path, position)
            if match is None:
                raise invalid("member_path requires an identifier or bracket component.")
            token = match.group(0)
            position = match.end()
        tokens.append(token)
        if position < len(path):
            if path[position] == ".":
                position += 1
                if position >= len(path) or _IDENTIFIER.match(path, position) is None:
                    raise invalid("A dot in member_path must be followed by an identifier.")
            elif path[position] != "[":
                raise invalid("member_path components must be separated by a dot or bracket.")
    return tokens


def validate_request(params):
    """Return None for the unchanged legacy call, or a validated projection request."""
    path = params.get("member_path")
    if path is None:
        if any(params.get(name) is not None for name in ("cursor", "limit", "max_depth")):
            raise invalid("Specify member_path (an empty string selects the root) for child paging.")
        return None
    tokens = _parse_path(path)
    limit = get_int(params, "limit", minimum=1, maximum=1000)
    depth = get_int(params, "max_depth", minimum=0, maximum=MAX_DEPTH)
    cursor = params.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 1024):
        raise invalid("The cursor is invalid.")
    return {"path": path, "tokens": tokens, "limit": 200 if limit is None else limit,
            "depth": 0 if depth is None else depth, "cursor": cursor}


def _check(callback):
    if callback is not None:
        callback()


def _unique_named(items, name, check_cancelled):
    found = None
    for position, item in enumerate(items):
        if position % 256 == 0:
            _check(check_cancelled)
        if item.name == name:
            if found is not None:
                raise invalid("The member name %s is ambiguous; use its numeric child index." % name)
            found = position
    return found


def _reflection_member(items, name, check_cancelled):
    # Missing/ambiguous reflection must not attach somebody else's type or byte offset.
    match = None
    for position, item in enumerate(items):
        if position % 256 == 0:
            _check(check_cancelled)
        if item.name == name:
            if match is not None:
                return None
            match = item
    return match


def _children(reference, variables):
    return variables if reference is None else reference["variable"].members


def _child(reference, variables, block, position, check_cancelled):
    variable = _children(reference, variables)[position]
    parent_path = "" if reference is None else reference["path"]
    child_type = None
    offset = None
    element = False
    if reference is None:
        constant = _reflection_member(block.variables, variable.name, check_cancelled)
        if constant is not None:
            child_type, offset = constant.type, int(constant.byteOffset)
    else:
        constant_type = reference["type"]
        if constant_type is not None:
            is_array = int(constant_type.elements) > 1 and not reference["element"]
            if is_array:
                element = True
                child_type = constant_type
                if reference["offset"] is not None:
                    offset = reference["offset"] + position * int(constant_type.arrayByteStride)
            else:
                constant = _reflection_member(constant_type.members, variable.name, check_cancelled)
                if constant is not None:
                    child_type = constant.type
                    if reference["offset"] is not None:
                        offset = reference["offset"] + int(constant.byteOffset)
    return {"variable": variable, "type": child_type, "offset": offset, "element": element,
            "path": parent_path + "[%d]" % position}


def _resolve(variables, block, tokens, check_cancelled):
    reference = None
    for token in tokens:
        _check(check_cancelled)
        children = _children(reference, variables)
        if isinstance(token, str):
            position = _unique_named(children, token, check_cancelled)
            if position is None:
                raise invalid("No constant-buffer member named %s at this path." % token)
        else:
            position = token
            if position >= len(children):
                raise invalid("Constant-buffer child index %d is out of range." % position,
                              childCount=len(children))
        reference = _child(reference, variables, block, position, check_cancelled)
    return reference


def _shallow(reference, convert_variable):
    item = convert_variable(reference["variable"], [1], reference["type"], reference["offset"],
                            reference["element"])
    item.pop("members", None)
    item.update(canonicalPath=reference["path"],
                childCount=len(reference["variable"].members))
    return item


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


class _Budget(object):
    def __init__(self, initial_bytes, initial_nodes=0):
        self.nodes = MAX_NODES - initial_nodes
        # Leave space for envelope metadata and for list/object separators added by recursion.
        self.bytes = MAX_PAYLOAD_BYTES - initial_bytes - 4096

    def take(self, item):
        size = _json_bytes(item) + 32
        if self.nodes <= 0 or size > self.bytes:
            return False
        self.nodes -= 1
        self.bytes -= size
        return True


def _project(reference, variables, block, depth, budget, convert_variable, check_cancelled):
    _check(check_cancelled)
    item = _shallow(reference, convert_variable)
    count = item["childCount"]
    item["membersTruncated"] = count > 0
    if not budget.take(item):
        return None
    if count and depth > 0:
        members = []
        for position in range(count):
            child = _project(_child(reference, variables, block, position, check_cancelled), variables, block,
                             depth - 1, budget, convert_variable, check_cancelled)
            if child is None:
                break
            members.append(child)
        item["members"] = members
        item["membersTruncated"] = len(members) < count
    return item


def _incomplete(item):
    return item.get("membersTruncated", False) or any(
        _incomplete(child) for child in item.get("members", []))


def project_members(variables, block, request, scope, convert_variable, check_cancelled=None):
    """Return (payload, next_cursor, truncated, warnings) for one selected member/root.

    ``scope`` starts with the full capture session and includes event, stage, shader, reflected block
    index and descriptor resource/offset/size. ``byteOffset`` values remain block-relative, matching
    the legacy converter; descriptor.binding.byteOffset is separate and is never added twice.
    """
    _check(check_cancelled)
    selected = _resolve(variables, block, request["tokens"], check_cancelled)
    canonical = "" if selected is None else selected["path"]
    children = _children(selected, variables)
    selected_item = None if selected is None else _shallow(selected, convert_variable)
    payload = {"memberPath": request["path"], "canonicalPath": canonical,
               "selected": selected_item, "variables": [], "childCount": len(children),
               "byteOffsetBasis": "block"}
    key = query_key(scope, canonical, request["depth"])
    offset = decode_cursor(request["cursor"], "get_cbuffer_members", scope[0], key)
    if offset > len(children):
        raise invalid("The member cursor is beyond the child list.")
    budget = _Budget(_json_bytes(payload), 0 if selected is None else 1)
    if budget.bytes < 0:
        raise AgentBridgeError("LIMIT_EXCEEDED", "The selected member metadata exceeds the page limit.")
    end = offset
    for position in range(offset, min(len(children), offset + request["limit"])):
        child = _project(_child(selected, variables, block, position, check_cancelled), variables, block,
                         request["depth"], budget, convert_variable, check_cancelled)
        if child is None:
            if not payload["variables"]:
                raise AgentBridgeError("LIMIT_EXCEEDED", "A member exceeds the 256 KiB page limit.",
                                       details={"canonicalPath": canonical + "[%d]" % position})
            break
        payload["variables"].append(child)
        end = position + 1
    next_cursor = (encode_cursor("get_cbuffer_members", scope[0], key, end)
                   if end < len(children) else None)
    truncated = next_cursor is not None or any(_incomplete(item) for item in payload["variables"])
    warnings = []
    if truncated:
        warnings.append("Some members are omitted by paging, expansion depth or response budgets. "
                        "Use nextCursor for siblings, or query a returned canonicalPath to page its children.")
    return payload, next_cursor, truncated, warnings
