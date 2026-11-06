"""Read source embedded in shader reflection, never files named by the capture."""

import json

from ..artifacts import write_bytes
from ..ipc import AgentBridgeError
from ..paging import decode_cursor, encode_cursor, query_key
from .common import (STAGE_NAMES, action_index, get_choice, get_int, invalid, renderdoc_module,
                     resolve_event, resource_index, result, session_of)
from .pipeline import _bound_shader

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_DIRECTORY_BYTES = 256 * 1024
MAX_FILENAME_CHARS = 4096
ENCODE_CHUNK_CHARS = 64 * 1024
# Keep aligned with the MCP ArtifactReader's maximum JSON line-page byte budget.
MAX_READ_ARTIFACT_PAGE_BYTES = 128 * 1024


def _line_read_issue(number, encoded_json_bytes, ends_cr):
    # ArtifactReader removes one trailing LF and then one CR. JSON encodes CR as two bytes.
    text_bytes = encoded_json_bytes - (2 if ends_cr else 0)
    empty_entry = json.dumps({"line": number, "text": ""}, ensure_ascii=False).encode("utf-8")
    required = text_bytes + len(empty_entry) + 2  # enclosing JSON array brackets
    if required <= MAX_READ_ARTIFACT_PAGE_BYTES:
        return None
    return {"readArtifactCompatible": False,
            "readArtifactReason": "Embedded source is exportable, but line %d exceeds "
                                  "read_artifact's maximum JSON page byte budget." % number,
            "readArtifactLine": number, "readArtifactRequiredBytes": required,
            "readArtifactMaxBytes": MAX_READ_ARTIFACT_PAGE_BYTES}


def _read_artifact_info(contents, context):
    number, json_bytes, has_text, ends_cr = 1, 0, False, False
    for offset in range(0, len(contents), ENCODE_CHUNK_CHARS):
        context.check_cancelled()
        # Use LF, not splitlines(): ArtifactReader iterates binary lines and preserves VT/FF.
        parts = contents[offset:offset + ENCODE_CHUNK_CHARS].split("\n")
        for index, part in enumerate(parts):
            if part:
                # Bound temporary JSON/UTF-8 copies even for a multi-megabyte source line.
                json_bytes += len(json.dumps(part, ensure_ascii=False).encode("utf-8")) - 2
                has_text, ends_cr = True, part.endswith("\r")
            if index < len(parts) - 1:
                issue = _line_read_issue(number, json_bytes, ends_cr)
                if issue:
                    return issue
                number, json_bytes, has_text, ends_cr = number + 1, 0, False, False
    if has_text:
        issue = _line_read_issue(number, json_bytes, ends_cr)
        if issue:
            return issue
    return {"readArtifactCompatible": True}


def _file_info(source, index, context, retain=False):
    filename = str(getattr(source, "filename", ""))
    info = {"index": index, "filename": filename[:MAX_FILENAME_CHARS], "bytes": None,
            "available": False, "readArtifactCompatible": False}
    if len(filename) > MAX_FILENAME_CHARS:
        info["filenameTruncated"] = True
    try:
        contents = getattr(source, "contents", None)
        if isinstance(contents, bytes):
            contents = contents.decode("utf-8")
        if not isinstance(contents, str):
            info["reason"] = "The embedded source contents are unavailable."
            return info, None
        # Counting chunks avoids allocating another giant copy merely to list a source file.
        # Do not guess an encoding or silently replace invalid source characters.
        chunks = []
        byte_count = 0
        for offset in range(0, len(contents), ENCODE_CHUNK_CHARS):
            context.check_cancelled()
            chunk = contents[offset:offset + ENCODE_CHUNK_CHARS]
            if "\x00" in chunk:
                info["reason"] = "The embedded source contains NUL bytes and is not readable " \
                                 "as a UTF-8 text artifact."
                return info, None
            encoded = chunk.encode("utf-8")
            byte_count += len(encoded)
            if retain and byte_count <= MAX_SOURCE_BYTES:
                chunks.append(encoded)
            elif retain:
                chunks = []
        info["bytes"] = byte_count
    except (AttributeError, UnicodeError):
        info["reason"] = "The embedded source could not be decoded as UTF-8 text."
        return info, None
    if byte_count > MAX_SOURCE_BYTES:
        info["reason"] = "The embedded source exceeds the 64 MiB UTF-8 size limit."
        return info, None
    info["available"] = True
    info.update(_read_artifact_info(contents, context))
    return info, b"".join(chunks) if retain else None


def get_shader_source(runtime, context):
    params = context.params
    stage_name = get_choice(params, "stage", STAGE_NAMES, required=True)
    file_index = get_int(params, "file_index")
    limit = get_int(params, "limit", 100, minimum=1, maximum=1000)
    cursor = params.get("cursor")
    if file_index is not None and cursor is not None:
        raise invalid("cursor is only valid when listing embedded source files.")
    event_id = resolve_event(context, action_index(runtime, context))
    resources = resource_index(runtime, context)
    session = session_of(context)

    def read(controller):
        pipe = controller.GetPipelineState()
        _, _, shader_key, reflection = _bound_shader(
            pipe, renderdoc_module(), stage_name, event_id)
        debug_info = getattr(reflection, "debugInfo", None)
        files = list(getattr(debug_info, "files", None) or [])
        data = {"eventId": event_id, "stage": stage_name,
                "shader": {"resourceId": shader_key, "name": resources.name(shader_key)},
                "available": bool(files), "fileCount": len(files)}
        if file_index is not None:
            if file_index >= len(files):
                raise invalid("Embedded source file_index %d is out of range." % file_index,
                              fileCount=len(files))
            info, encoded = _file_info(files[file_index], file_index, context, retain=True)
            data.update(file=info, available=info["available"])
            if not info["available"]:
                data["reason"] = info["reason"]
                if info["bytes"] is not None and info["bytes"] > MAX_SOURCE_BYTES:
                    raise AgentBridgeError("LIMIT_EXCEEDED", info["reason"],
                                           details={"fileIndex": file_index,
                                                    "bytes": info["bytes"],
                                                    "maxBytes": MAX_SOURCE_BYTES})
            return data, encoded, None

        # Include the full session in the query key, as well as the cursor's session prefix.
        key = query_key(session, event_id, stage_name, shader_key, "embeddedSourceFiles")
        offset = decode_cursor(cursor, "get_shader_source", session, key)
        if offset > len(files):
            raise invalid("The source directory cursor is beyond the file list.")
        page = []
        page_bytes = 0
        for index in range(offset, min(offset + limit, len(files))):
            context.check_cancelled()
            info, _ = _file_info(files[index], index, context)
            info_bytes = len(json.dumps(info, ensure_ascii=True).encode("utf-8"))
            if page and page_bytes + info_bytes > MAX_DIRECTORY_BYTES:
                break
            page.append(info)
            page_bytes += info_bytes
        end = offset + len(page)
        next_cursor = (encode_cursor("get_shader_source", session, key, end)
                       if end < len(files) else None)
        data["files"] = page
        if not files:
            data["reason"] = "The shader has no embedded source files; original source is unavailable."
        return data, None, next_cursor

    (data, encoded, next_cursor), meta = runtime.replay.at_event(context, event_id, read)
    artifacts = []
    warnings = []
    if encoded is not None:
        context.check_cancelled()
        if session_of(context) != (runtime.state.snapshot().get("captureSessionId") or ""):
            raise AgentBridgeError("CAPTURE_CHANGED", "The capture changed before source export.")
        info = data["file"]
        source = {"event": event_id, "stage": stage_name,
                  "shaderResourceId": data["shader"]["resourceId"],
                  "fileIndex": info["index"], "filename": info["filename"]}
        if info.get("filenameTruncated"):
            source["filenameTruncated"] = True
        artifact = write_bytes(runtime, session,
                               "shader-source-%s-%s-%d" % (data["shader"]["resourceId"],
                                                           stage_name, file_index),
                               "txt", encoded, "shader-source", "text/plain; charset=utf-8",
                               source=source)
        artifacts.append(artifact)
        data["sourceArtifact"] = artifact["path"]
        if not info["readArtifactCompatible"]:
            warnings.append(info["readArtifactReason"] +
                            " The artifact preserves the complete source bytes; any shorter lines "
                            "can still be read by start_line or search.")
    return result(data, meta=meta, artifacts=artifacts, next_cursor=next_cursor, warnings=warnings)


HANDLERS = {"get_shader_source": get_shader_source}
