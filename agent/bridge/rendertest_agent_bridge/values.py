"""Convert RenderDoc replay objects into JSON-safe values."""

import math


def enum_name(value):
    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    text = str(value)
    return text.split(".", 1)[1] if "." in text else text


def enum_value(value):
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = None
    return {"name": enum_name(value), "value": number}


def flag_names(value):
    """Return the single-bit member names set in a RenderDoc IntFlag value."""
    if value is None:
        return []
    bits = flags_value(value)
    try:
        names = []
        for member in type(value):
            bit = int(member)
            if bit and not bit & (bit - 1) and bits & bit:
                names.append(member.name)
        if names or not bits:
            return names
    except TypeError:
        pass
    text = str(value)
    if "." in text:
        text = text.split(".", 1)[1]
    return [name for name in text.split("|") if name and name != "NoFlags" and not name.isdigit()]


def flags_value(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def resource_id(value):
    """Return a ResourceId as a decimal string, or None for the null id."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return str(number) if number else None


def number(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, int):
        return value
    try:
        return number(float(value))
    except (TypeError, ValueError):
        return str(value)


def numbers(values, count=None):
    items = list(values)
    if count is not None:
        items = items[:count]
    return [number(item) for item in items]


def text(value, limit=4096):
    value = "" if value is None else str(value)
    return value if len(value) <= limit else value[:limit] + "...(truncated)"


def resource_format(fmt):
    if fmt is None:
        return None
    return {
        "name": format_name(fmt),
        "type": enum_name(getattr(fmt, "type", None)),
        "compType": enum_name(getattr(fmt, "compType", None)),
        "compCount": int(getattr(fmt, "compCount", 0)),
        "compByteWidth": int(getattr(fmt, "compByteWidth", 0)),
    }


def format_name(fmt):
    if fmt is None:
        return None
    try:
        return fmt.Name()
    except Exception:
        return str(fmt)
