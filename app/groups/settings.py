"""A group's settings blob: validation and defaults.

Stored as JSON on ``group_chats.settings`` so adding a knob needs no migration.
:func:`normalize_settings` is strict — it raises :class:`ValueError` with a
message the API turns into a 400 — because these values decide how a room
behaves. A malformed cap that was quietly dropped would look exactly like "the
room ignored my setting", which is a confusing way to find out about a typo.

The shape::

    {
      "max_agent_hops": 6,
      "max_agent_posts_per_minute": 30,
      "web_sender_name": "Operator",
      "smart_routing": true
    }
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.groups.constants import (
    DEFAULT_MAX_AGENT_HOPS,
    DEFAULT_MAX_AGENT_POSTS_PER_MINUTE,
    DEFAULT_ROUTING_ENABLED,
    DEFAULT_WEB_SENDER_NAME,
    ROUTING_SETTING_KEY,
)

_MAX_HOPS_CEILING = 100
_MAX_POSTS_CEILING = 600


def default_settings() -> Dict[str, Any]:
    return {
        "max_agent_hops": DEFAULT_MAX_AGENT_HOPS,
        "max_agent_posts_per_minute": DEFAULT_MAX_AGENT_POSTS_PER_MINUTE,
        "web_sender_name": DEFAULT_WEB_SENDER_NAME,
        ROUTING_SETTING_KEY: DEFAULT_ROUTING_ENABLED,
    }


def normalize_settings(raw: Any) -> Dict[str, Any]:
    """Validate and fill in a settings blob. Raises ``ValueError`` when unusable."""
    if raw is None:
        return default_settings()
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")

    out = default_settings()

    if raw.get("max_agent_hops") is not None:
        out["max_agent_hops"] = _positive_int(
            raw["max_agent_hops"], "max_agent_hops", _MAX_HOPS_CEILING,
        )

    if raw.get("max_agent_posts_per_minute") is not None:
        out["max_agent_posts_per_minute"] = _positive_int(
            raw["max_agent_posts_per_minute"],
            "max_agent_posts_per_minute",
            _MAX_POSTS_CEILING,
        )

    if raw.get("web_sender_name") is not None:
        name = str(raw["web_sender_name"]).strip()
        out["web_sender_name"] = name or DEFAULT_WEB_SENDER_NAME

    # ``is not None`` rather than ``in raw``: this one defaults to ON, and the UI
    # sends the whole blob back with unset fields as null — ``bool(None)`` would
    # read a blank field as "turn routing off".
    if raw.get(ROUTING_SETTING_KEY) is not None:
        out[ROUTING_SETTING_KEY] = bool(raw[ROUTING_SETTING_KEY])

    return out


def _positive_int(value: Any, field: str, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a whole number") from None
    if number < 0:
        raise ValueError(f"{field} cannot be negative")
    if number > ceiling:
        raise ValueError(f"{field} must be {ceiling} or less")
    return number


def max_agent_hops(settings: Optional[Dict[str, Any]]) -> int:
    value = (settings or {}).get("max_agent_hops")
    return DEFAULT_MAX_AGENT_HOPS if value is None else int(value)


def max_agent_posts_per_minute(settings: Optional[Dict[str, Any]]) -> int:
    value = (settings or {}).get("max_agent_posts_per_minute")
    return (
        DEFAULT_MAX_AGENT_POSTS_PER_MINUTE if value is None else int(value)
    )


def routing_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    """Whether this room asks a cheap model who should start a turn.

    Absent falls back to the default (on) like the numeric caps do: a blob
    without the key was stored before the knob existed, not by someone who
    declined it. This is the single definition —
    :func:`app.groups.routing.routing_enabled` delegates here.
    """
    value = (settings or {}).get(ROUTING_SETTING_KEY)
    return DEFAULT_ROUTING_ENABLED if value is None else bool(value)


def web_sender_name(settings: Optional[Dict[str, Any]]) -> str:
    name = str((settings or {}).get("web_sender_name") or "").strip()
    return name or DEFAULT_WEB_SENDER_NAME
