"""Tags settings and routing: which of a profile's tags receive which cards.

Three layers, resolved per key like :mod:`app.config.timezone`:

1. the profile's **own** overrides (``tag_settings.options``) — only the keys
   it has set;
2. else the **admin defaults**, ``server_config`` key ``tags_defaults`` (a JSON
   object written through ``PUT /api/tags/hardware/defaults``);
3. else the built-in :data:`BUILTIN_DEFAULTS`.

``routes`` merges per card kind the same way. A route is ``"all"`` (every tag
the profile owns), ``"none"``, or a list of the profile's own device ids —
ids of tags it does not own are dropped at routing time, never honoured.

``get_dynamic("server_config", …)`` is profile-independent; nothing here
passes ``profile=None`` to a ``user_config`` read (that silently reads the
admin's row).
"""

from __future__ import annotations

import copy
import json
from typing import Any, Iterable

from app.utils.logger import logger

DEFAULTS_KEY = "tags_defaults"

# Card kinds a profile can route. ``pinned_note`` and ``clear`` target one tag
# explicitly; ``resolved`` follows the card it resolves.
ROUTABLE_KINDS = (
    "notification", "task_outcome", "needs_input", "excerpt", "progress",
    "health", "indexing_problem", "calendar", "automation", "usage",
    "tag_diagnostics",
)
LAYOUTS = ("status",)
LANGUAGES_MAX = 16

BUILTIN_DEFAULTS: dict[str, Any] = {
    "layout": "status",
    "show_excerpts": False,
    "qr_links": False,
    "progress_cadence_s": 300,
    "language": "en",
    # "" = the profile's own Cremind timezone (Settings → Config).
    "timezone": "",
    "routes": {kind: ("none" if kind == "usage" else "all") for kind in ROUTABLE_KINDS},
}

OPTION_KEYS = tuple(BUILTIN_DEFAULTS)


class SettingsError(ValueError):
    """A rejected settings write. ``details`` maps field -> message."""

    def __init__(self, details: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in details.items()))
        self.details = details


def _route(value: Any, field: str, errors: dict[str, str]) -> Any:
    if value in ("all", "none"):
        return value
    if isinstance(value, (list, tuple)):
        ids = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > 64:
                errors[field] = "must be 'all', 'none' or a list of device ids"
                return None
            if item.strip() not in ids:
                ids.append(item.strip())
        return ids[:64]
    errors[field] = "must be 'all', 'none' or a list of device ids"
    return None


def normalize_options(raw: Any) -> dict[str, Any]:
    """Validate a (partial) options object. Unknown keys are rejected; a key
    whose value is ``None`` means "inherit" and is dropped."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SettingsError({"options": "must be an object"})
    errors: dict[str, str] = {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in OPTION_KEYS:
            errors[key] = "unknown setting"
            continue
        if value is None:
            continue
        if key == "layout":
            if value not in LAYOUTS:
                errors[key] = f"must be one of {', '.join(LAYOUTS)}"
                continue
            out[key] = value
        elif key in ("show_excerpts", "qr_links"):
            if not isinstance(value, bool):
                errors[key] = "must be true or false"
                continue
            out[key] = value
        elif key == "progress_cadence_s":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 60 <= value <= 3600:
                errors[key] = "must be a number of seconds between 60 and 3600"
                continue
            out[key] = int(value)
        elif key == "language":
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > LANGUAGES_MAX:
                errors[key] = "must be a language tag such as 'en' or 'vi'"
                continue
            out[key] = value.strip()
        elif key == "timezone":
            if not isinstance(value, str):
                errors[key] = "must be a timezone name, a UTC offset, or '' for the profile's own"
                continue
            text = value.strip()
            if text:
                from app.config.timezone import is_valid_timezone

                if not is_valid_timezone(text) or text.lower() == "auto":
                    errors[key] = "must be a timezone name, a UTC offset, or '' for the profile's own"
                    continue
            out[key] = text
        elif key == "routes":
            if not isinstance(value, dict):
                errors[key] = "must be an object of card kind -> route"
                continue
            routes: dict[str, Any] = {}
            for kind, route in value.items():
                if kind not in ROUTABLE_KINDS:
                    errors[f"routes.{kind}"] = "unknown card kind"
                    continue
                if route is None:
                    continue
                normalized = _route(route, f"routes.{kind}", errors)
                if normalized is not None:
                    routes[kind] = normalized
            out[key] = routes
    if errors:
        raise SettingsError(errors)
    return out


def merge_options(current: dict[str, Any] | None, patch: Any) -> dict[str, Any]:
    """Merge a partial options object into ``current`` (a stored override
    set). Keys given replace; ``null`` removes the override (inherit again);
    ``routes`` merges per card kind the same way (``"routes": null`` drops
    every route override). Validated exactly like a full write."""
    if not isinstance(patch, dict):
        raise SettingsError({"options": "must be an object"})
    validated = normalize_options(patch)
    out = copy.deepcopy(current or {})
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif key == "routes":
            routes = dict(out.get("routes") or {})
            for kind, route in value.items():
                if route is None:
                    routes.pop(kind, None)
            routes.update(validated.get("routes") or {})
            if routes:
                out["routes"] = routes
            else:
                out.pop("routes", None)
        else:
            out[key] = validated[key]
    return out


def read_admin_defaults() -> dict[str, Any]:
    """The admin's defaults (validated; a damaged value reads as none)."""
    try:
        from app.config.settings import get_dynamic

        raw = get_dynamic("server_config", DEFAULTS_KEY)
    except Exception:  # noqa: BLE001 — storage not wired yet
        return {}
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return normalize_options(value)
    except (ValueError, TypeError):
        logger.warning("[tags] ignoring a damaged tags_defaults value")
        return {}


def write_admin_defaults(raw: Any, config_storage) -> dict[str, Any]:
    """Validate and persist the admin defaults (whole object); return them."""
    value = normalize_options(raw)
    config_storage.set("server_config", DEFAULTS_KEY, json.dumps(value, sort_keys=True))
    return value


def patch_admin_defaults(patch: Any, config_storage) -> dict[str, Any]:
    """Merge a partial object into the admin defaults (see
    :func:`merge_options`); return the result."""
    return write_admin_defaults(merge_options(read_admin_defaults(), patch), config_storage)


def effective_options(own: dict[str, Any] | None, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge own overrides over admin defaults over the built-ins, per key."""
    if defaults is None:
        defaults = read_admin_defaults()
    out = copy.deepcopy(BUILTIN_DEFAULTS)
    for layer in (defaults or {}, own or {}):
        for key, value in layer.items():
            if key == "routes":
                out["routes"].update(value or {})
            else:
                out[key] = value
    return out


def resolved_timezone(profile: str, options: dict[str, Any]) -> str:
    """The IANA name / offset the companion should render times in."""
    tz = (options.get("timezone") or "").strip()
    if tz:
        return tz
    try:
        from app.config.timezone import resolve_tz_name

        return resolve_tz_name(profile)
    except Exception:  # noqa: BLE001
        return "UTC"


def route_targets(kind: str, options: dict[str, Any], owned: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tags among ``owned`` (the profile's own tag rows) that receive a
    card of ``kind``. Unknown kinds route nowhere."""
    if kind not in ROUTABLE_KINDS:
        return []
    route = (options.get("routes") or {}).get(kind, "all")
    tags = [d for d in owned if d.get("kind") == "tag"]
    if route == "all":
        return tags
    if route == "none" or not isinstance(route, list):
        return []
    wanted = set(route)
    return [d for d in tags if d.get("id") in wanted]
