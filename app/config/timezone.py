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

A stored value may be an **IANA name** (``Asia/Tokyo``) OR a whole-hour **UTC
offset** (``+07:00``, ``-05:00``) — the user picks one format on the Config page. The
literal value ``"auto"`` (the schema default) means "no explicit choice" — it
resolves via the env var / OS zone rather than inheriting the admin's value.

Resolution reads the *raw* ``user_config`` row via :func:`get_dynamic` (which
returns ``None`` when the profile has never set the key) rather than
:func:`get_user_config` — the latter masks "never set" with the TOML default,
which would defeat rung 2. All lookups degrade gracefully: an invalid IANA name
or an un-wired storage layer (early boot) falls through to the OS zone, never
raising.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config.settings import get_dynamic
from app.utils.logger import logger

_KEY = "system.timezone"
_AUTO = "auto"
_ENV = "CREMIND_TIMEZONE"
_ADMIN = "admin"

# A UTC-offset string: optional UTC/GMT prefix, sign, 1-2 digit hours, optional
# minutes (``:MM`` or ``MM``). E.g. ``+07:00``, ``-05:00``, ``UTC+7``, ``+0800``.
# Only WHOLE-HOUR offsets are accepted, so the minutes group must be ``00``.
_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def _os_local() -> tzinfo:
    """The process's OS-local zone as a concrete ``tzinfo`` (fixed offset)."""
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def _parse_offset(text: str) -> tzinfo | None:
    """Parse a WHOLE-HOUR UTC-offset string into a fixed-offset ``tzinfo``,
    else ``None``.

    Accepts ``+07:00`` / ``-05:00`` / ``UTC+07:00`` / ``+0800`` / ``Z``,
    range-checked to ``[-12:00, +14:00]``. A non-zero minutes component
    (e.g. ``+05:30``) is rejected — offsets are whole hours only. The resulting
    zone has no DST (a constant offset), which is the semantics of an
    offset-based schedule.
    """
    t = text.strip()
    if t.upper() == "Z":
        return timezone.utc
    # Bare "UTC"/"GMT" (no sign) are valid IANA names — let ZoneInfo own them so
    # they keep a ``.key``; only signed forms (UTC+07:00 / +07:00) are offsets.
    m = _OFFSET_RE.match(t)
    if not m:
        return None
    minutes = int(m.group(3) or 0)
    if minutes != 0:  # whole hours only — reject +05:30, +05:15, etc.
        return None
    hours = int(m.group(2))
    total = (1 if m.group(1) == "+" else -1) * hours * 60
    if not (-12 * 60 <= total <= 14 * 60):
        return None
    sign = "+" if total >= 0 else "-"
    return timezone(timedelta(minutes=total), name=f"UTC{sign}{abs(hours):02d}:00")


def _explicit_zone(text: str) -> tzinfo | None:
    """Parse an explicit zone string — a UTC offset OR an IANA name — else
    ``None``. Callers must have already excluded blank / ``"auto"``.
    """
    off = _parse_offset(text)
    if off is not None:
        return off
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError, ModuleNotFoundError):
        return None


def is_valid_timezone(text: Any) -> bool:
    """True if ``text`` is ``"auto"`` or a parseable IANA name / UTC offset.

    The shared validator behind both the resolver and ``Field.validate`` so the
    Settings UI/CLI accept exactly what the resolver can interpret.
    """
    t = (text or "").strip() if isinstance(text, str) else ""
    return t.lower() == _AUTO or _explicit_zone(t) is not None


def _safe_zone(name: Any) -> tzinfo | None:
    """Effective ``tzinfo`` for a stored value — IANA name OR UTC offset — else
    ``None``.

    ``None``/blank/``"auto"`` and any unparseable value degrade to ``None`` so
    the caller can fall through the resolution chain.
    """
    if not name or not isinstance(name, str):
        return None
    text = name.strip()
    if not text or text.lower() == _AUTO:
        return None
    zone = _explicit_zone(text)
    if zone is None:
        logger.warning(f"[timezone] ignoring invalid timezone {text!r}; falling back")
    return zone


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
