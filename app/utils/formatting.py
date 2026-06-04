import json
from typing import Any


def dict_to_text(data: dict, indent: int = 0) -> str:
    """Convert a dict to a readable YAML-like indented text.

    Handles nested dicts, lists of dicts, lists of scalars,
    and mixed structures recursively.  String values that are
    valid JSON objects/arrays are parsed and formatted inline.
    """
    lines: list[str] = []
    for key, value in data.items():
        lines.append(_format_value(key, _maybe_parse_json(value), indent))
    return "\n".join(lines)


def _maybe_parse_json(value: Any) -> Any:
    """If *value* is a string that parses to a dict or list, return the parsed
    object so it can be formatted recursively.  Otherwise return *value* as-is."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (dict, list)):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return value


def _format_value(key: str, value: Any, indent: int) -> str:
    """Format a single key-value pair into YAML-like indented text."""
    prefix = "  " * indent
    if isinstance(value, dict):
        sub_lines = [f"{prefix}{key}:"]
        for k, v in value.items():
            sub_lines.append(_format_value(k, _maybe_parse_json(v), indent + 1))
        return "\n".join(sub_lines)
    elif isinstance(value, list):
        sub_lines = [f"{prefix}{key}:"]
        for item in value:
            if isinstance(item, dict):
                items = list(item.items())
                first_k, first_v = items[0]
                sub_lines.append(f"{prefix}  - {first_k}: {first_v}")
                for k, v in items[1:]:
                    sub_lines.append(f"{prefix}    {k}: {v}")
            else:
                sub_lines.append(f"{prefix}  - {item}")
        return "\n".join(sub_lines)
    else:
        return f"{prefix}{key}: {value}"
