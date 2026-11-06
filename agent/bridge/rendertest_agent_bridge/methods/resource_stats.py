"""Texture/buffer description statistics without changing the replay event."""

import heapq

from ..indexes import texture_summary
from ..values import enum_name, format_name
from .common import get_choice, get_int, resource_index, result

KINDS = ("all", "texture", "buffer")
GROUP_LIMIT = 256
LABEL_LIMIT = 256
NAME_LIMIT = 512
UNKNOWN_SAMPLE_LIMIT = 20
MAX_RESOURCE_BYTES = (1 << 64) - 1


def _bounded(value, limit):
    text = str(value) if value is not None else ""
    return text[:limit], len(text) > limit


def _totals():
    return {"resourceCount": 0, "knownSizeCount": 0, "unknownSizeCount": 0,
            "knownTotalBytes": 0, "totalBytesComplete": True}


def _add(totals, size):
    totals["resourceCount"] += 1
    if size is None:
        totals["unknownSizeCount"] += 1
        totals["totalBytesComplete"] = False
    else:
        totals["knownSizeCount"] += 1
        totals["knownTotalBytes"] += size


def _size(description, attribute):
    try:
        value = getattr(description, attribute, None)
    except Exception:
        return None, "unreadable-size"
    if value is None:
        return None, "missing-size"
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "non-integer-size"
    if value < 0:
        return None, "negative-size"
    if value > MAX_RESOURCE_BYTES:
        return None, "out-of-range-size"
    if value == MAX_RESOURCE_BYTES:
        # Native allocation-size queries can use UINT64_MAX to report failure. It cannot
        # describe a real texture/buffer allocation and must not dominate totals or rankings.
        return None, "size-sentinel"
    return value, None


def _category(description, attribute, convert):
    try:
        value = convert(getattr(description, attribute, None))
        return str(value) if value else "<unknown>"
    except Exception:
        return "<unknown>"


def _distribution(groups, field):
    # Aggregate by the full category, not its bounded display label.
    ordered = sorted(groups, key=lambda key: (-groups[key]["knownTotalBytes"], key))
    entries = []
    labels_truncated = False
    for key in ordered[:GROUP_LIMIT]:
        entry = dict(groups[key])
        entry[field], entry["labelTruncated"] = _bounded(key, LABEL_LIMIT)
        labels_truncated = labels_truncated or entry["labelTruncated"]
        entries.append(entry)
    return {"groups": entries, "totalGroups": len(groups), "returned": len(entries),
            "limit": GROUP_LIMIT, "hasMore": len(groups) > len(entries),
            "labelsTruncated": labels_truncated}


def _reference(resources, key, kind):
    name, clipped = _bounded(resources.name(key), NAME_LIMIT)
    return {"resourceId": key, "name": name, "nameTruncated": clipped, "kind": kind}


def get_resource_stats(runtime, context):
    kind = get_choice(context.params, "kind", KINDS, "all", required=True)
    top_n = get_int(context.params, "top_n", 20, minimum=1, maximum=1000, required=True)
    resources = resource_index(runtime, context)
    totals = _totals()
    kinds, formats, types = {}, {}, {}
    candidates = []
    unknown = []
    conflicts = 0
    # ResourceIndex.items is keyed by exact decimal ID. Its order can contain duplicate
    # descriptions, so do not count or sum by that list, resource names, or float IDs.
    keys = sorted(resources.items, key=int)
    for offset, key in enumerate(keys):
        if offset % 256 == 0:
            context.check_cancelled()
        item_kind = resources.items[key]["kind"]
        if item_kind not in ("texture", "buffer") or kind not in ("all", item_kind):
            continue
        texture = resources.textures.get(key)
        buffer = resources.buffers.get(key)
        if texture is not None and buffer is not None:
            # Keep ResourceIndex's classification and count the ID only once.
            conflicts += 1
        description = texture if item_kind == "texture" else buffer
        size, issue = _size(description, "byteSize" if item_kind == "texture" else "length")
        _add(totals, size)
        _add(kinds.setdefault(item_kind, _totals()), size)
        if item_kind == "texture":
            format_key = _category(texture, "format", format_name)
            type_key = _category(texture, "type", enum_name)
            _add(formats.setdefault(format_key, _totals()), size)
            _add(types.setdefault(type_key, _totals()), size)
        if size is not None:
            candidates.append((size, key, item_kind))
        elif len(unknown) < UNKNOWN_SAMPLE_LIMIT:
            sample = _reference(resources, key, item_kind)
            sample.update({"byteSize": None, "sizeIssue": issue})
            unknown.append(sample)

    largest = []
    for size, key, item_kind in heapq.nsmallest(
            top_n, candidates, key=lambda value: (-value[0], int(value[1]))):
        entry = _reference(resources, key, item_kind)
        entry["byteSize"] = size
        if item_kind == "texture":
            entry.update(texture_summary(resources.textures[key]))
            entry["format"], entry["formatTruncated"] = _bounded(entry["format"], LABEL_LIMIT)
            entry["textureType"], entry["textureTypeTruncated"] = _bounded(
                entry["textureType"], LABEL_LIMIT)
        largest.append(entry)

    data = dict(totals)
    data.update({
        "kind": kind, "scope": "textures-and-buffers",
        "sizeBasis": "resource-description-bytes",
        "sizeCaveat": "Description sizes are not resident, peak, or unique allocation memory; "
                      "aliased resources may refer to the same allocation.",
        "byKind": _distribution(kinds, "kind"),
        "byTextureFormat": _distribution(formats, "format"),
        "byTextureType": _distribution(types, "textureType"),
        "topN": top_n, "largestCandidates": len(candidates),
        "largestResources": largest, "returnedLargest": len(largest),
        "hasMoreLargest": len(candidates) > len(largest),
        "unknownSizeSamples": unknown, "unknownSizeSampleLimit": UNKNOWN_SAMPLE_LIMIT,
        "hasMoreUnknownSizeSamples": totals["unknownSizeCount"] > len(unknown),
        "descriptionConflictCount": conflicts,
    })
    warnings = []
    if totals["unknownSizeCount"]:
        warnings.append("%d resources have unknown or invalid description sizes; "
                        "knownTotalBytes and largestResources exclude them." %
                        totals["unknownSizeCount"])
    if conflicts:
        warnings.append("%d ResourceIds have both texture and buffer descriptions; "
                        "each is counted once using the resource index's kind." % conflicts)
    distributions = (data["byKind"], data["byTextureFormat"], data["byTextureType"])
    clipped = any(entry.get("nameTruncated") or entry.get("formatTruncated") or
                  entry.get("textureTypeTruncated") for entry in largest + unknown)
    truncated = (data["hasMoreLargest"] or data["hasMoreUnknownSizeSamples"] or clipped or
                 any(group["hasMore"] or group["labelsTruncated"] for group in distributions))
    context.check_cancelled()
    return result(data, warnings=warnings, truncated=truncated)


HANDLERS = {"get_resource_stats": get_resource_stats}
