"""Effective-timezone resolver for the Calendar & Schedule engine.

The scheduler stores schedules as naive local wall-clock (e.g. ``09:00``) and
bridges to epoch seconds. Which zone that ``09:00`` means used to be whatever
the *process* OS zone happened to be — the PC's zone locally, but ``UTC`` on a
Docker/VPS install (the image sets ``ENV TZ=UTC``). This module makes the zone
an explicit, user-configurable value instead.

The effective zone for a profile is resolved through this chain:

1. The profile's **own** ``system.timezone`` setting, if it has ever set one
   (a row exists in ``user_config``).
2. Else the **admin** profile's setting (inherited default). The admin value
   only fills in for profiles that have *never* chosen their own — it never
   overrides a profile that has.
3. Else the ``CREMIND_TIMEZONE`` environment variable (boot default; ideal for
   Docker/VPS / docker-compose).
4. Else auto-detect the OS local zone (so existing local installs behave
   exactly as before).

The literal value ``"auto"`` (the schema default) means "no explicit choice" —
it resolves via the env var / OS zone rather than inheriting the admin's value.

Resolution reads the *raw* ``user_config`` row via :func:`get_dynamic` (which
returns ``None`` when the profile has never set the key) rather than
:func:`get_user_config` — the latter masks "never set" with the TOML default,
which would defeat rung 2. All lookups degrade gracefully: an invalid IANA name
or an un-wired storage layer (early boot) falls through to the OS zone, never
raising.
"""

from __future__ import annotations

import os
from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config.settings import get_dynamic
from app.utils.logger import logger

_KEY = "system.timezone"
_AUTO = "auto"
_ENV = "CREMIND_TIMEZONE"
_ADMIN = "admin"


def _os_local() -> tzinfo:
    """The process's OS-local zone as a concrete ``tzinfo`` (fixed offset)."""
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def _safe_zone(name: Any) -> tzinfo | None:
    """``ZoneInfo(name)`` for a valid IANA string, else ``None``.

    ``None``/blank/``"auto"`` and any invalid name degrade to ``None`` so the
    caller can fall through the resolution chain.
    """
    if not name or not isinstance(name, str):
        return None
    text = name.strip()
    if not text or text.lower() == _AUTO:
        return None
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError, ModuleNotFoundError):
        logger.warning(f"[timezone] ignoring invalid timezone {text!r}; falling back")
        return None


def _env_or_os() -> tzinfo:
    """``CREMIND_TIMEZONE`` if set to a valid zone, else the OS-local zone."""
    return _safe_zone(os.environ.get(_ENV)) or _os_local()


def resolve_tzinfo(profile: str | None) -> tzinfo:
    """Return the effective ``tzinfo`` for ``profile`` (see module docstring)."""
    if profile:
        try:
            raw = get_dynamic("user_config", _KEY, profile=profile)
        except Exception as exc:  # noqa: BLE001 — never let tz resolution break a run
            logger.warning(f"[timezone] config read failed for {profile!r}: {exc}")
            raw = None
        if raw is not None:  # profile has explicitly set a value — keep its own
            return _safe_zone(raw) or _env_or_os()
        if profile != _ADMIN:  # never set — inherit the admin profile's zone
            try:
                admin_raw = get_dynamic("user_config", _KEY, profile=_ADMIN)
            except Exception:  # noqa: BLE001
                admin_raw = None
            admin_zone = _safe_zone(admin_raw)
            if admin_zone is not None:
                return admin_zone
    return _env_or_os()


def resolve_tz_name(profile: str | None) -> str:
    """A human/IANA label for the effective zone of ``profile`` (for logs/UI)."""
    tz = resolve_tzinfo(profile)
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    return datetime.now(tz).tzname() or "local"
