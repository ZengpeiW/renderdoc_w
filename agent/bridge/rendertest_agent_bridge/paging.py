"""Opaque, capture-bound pagination cursors."""

import base64
import hashlib
import json

from .ipc import AgentBridgeError

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def query_key(*parts):
    data = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(data.encode("utf-8")).hexdigest()[:16]


def encode_cursor(kind, session, key, offset):
    data = json.dumps({"k": kind, "s": session[:16], "q": key, "o": offset},
                      separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(data.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor, kind, session, key):
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
        raise AgentBridgeError("INVALID_REQUEST", "The cursor is invalid.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        offset = value["o"]
        valid = (value["k"] == kind and value["s"] == session[:16] and value["q"] == key and
                 type(offset) is int and offset >= 0)
    except Exception:
        valid = False
    if not valid:
        raise AgentBridgeError("INVALID_REQUEST",
                               "The cursor belongs to another capture, query or tool.")
    return offset


def paginate(items, params, kind, session, key):
    """Return ``(page, next_cursor, total)`` for a list using the request's cursor/limit."""
    limit = params.get("limit", DEFAULT_LIMIT)
    if type(limit) is not int or limit < 1 or limit > MAX_LIMIT:
        raise AgentBridgeError("INVALID_REQUEST", "limit must be between 1 and %d." % MAX_LIMIT)
    offset = decode_cursor(params.get("cursor"), kind, session, key)
    page = items[offset:offset + limit]
    end = offset + len(page)
    next_cursor = encode_cursor(kind, session, key, end) if end < len(items) else None
    return page, next_cursor, len(items)
