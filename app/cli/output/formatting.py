"""Field-extraction helpers for rendering loosely-typed dicts.

Mirrors the `stringField` / `boolField` helpers in `cli/cmd/tools.go`.
"""

from __future__ import annotations

from typing import Any


def string_field(d: dict[str, Any], key: str) -> str:
    """Return `d[key]` as a string, falling back to "" if missing."""
    if key not in d:
        return ""
    v = d[key]
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def bool_field(d: dict[str, Any], key: str, fallback: bool = False) -> str:
    """Return `d[key]` as "yes"/"no", falling back to `fallback` if missing
    or non-boolean.
    """
    if key in d and isinstance(d[key], bool):
        return "yes" if d[key] else "no"
    return "yes" if fallback else "no"


def epoch_seconds_field(value: Any) -> str:
    """Render an epoch-SECONDS timestamp as local ``YYYY-MM-DD HH:MM``, else "".

    Subscription tables store seconds; event runs store milliseconds (see
    ``_fmt_ts`` in the event-runs command) — do not mix the two.
    """
    if not value:
        return ""
    try:
        from datetime import datetime

        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
