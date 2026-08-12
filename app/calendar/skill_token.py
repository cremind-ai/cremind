"""Read-only access to the gcalendar skill's OAuth token, for backend Calendar use.

Linking a Google account used to have to happen twice: once in chat (the
``gcalendar`` skill's ``link``) and again on the Calendar & Schedule page
(:mod:`app.calendar.google_auth`). Two grants meant two accounts could drift
apart. This module makes the skill's link the one that counts: when it is
usable, the calendar reads and writes through it, and the page shows the linked
address instead of another Connect button.

The skill owns its token file (``<profile>/skills/gcalendar/scripts/
.google_token.json``): it mints it during ``link`` and rewrites it on every
refresh, and its listener does the same from a separate process. This module
**never writes that file** — it reads the credentials, refreshes in memory when
the access token has expired, and lets the skill remain the single writer. Two
writers would race, and a half-written token file breaks linking for the user.

Mirrors :mod:`app.drive.skill_token`, which does the same for the ``gdrive``
skill. The two are deliberately kept separate for now: that one carries Drive's
grant provenance and scope-migration rules, and folding both onto one helper
would entangle them for no functional gain. If a third reader appears, extract
the common core (locate / read / refresh-in-memory) then.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.utils.logger import logger

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

SKILL_DIR_NAME = "gcalendar"
_TOKEN_REL = Path("scripts") / ".google_token.json"

# What the calendar needs to be able to do. The skill requests ``calendar.events``
# (plus openid/email, which is how we know the address); a bring-your-own-client
# user may hold the broader ``calendar`` scope instead, which is a superset.
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
BROAD_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"

# In-memory access tokens keyed by profile: {token, expiry, account}. Refreshing
# without persisting means a backend restart re-refreshes once — cheap, and it
# keeps the token file single-writer. ``account`` pins the entry to the account it
# was minted for, so a re-link to a different address can never serve the old one.
_access_cache: Dict[str, Dict[str, Any]] = {}


def _as_float(value: Any) -> float:
    """Coerce a token-file number, tolerating whatever the file actually holds."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def skill_dir(profile: str) -> Optional[Path]:
    from app.skills.sync import profile_skills_dir

    path = profile_skills_dir(profile) / SKILL_DIR_NAME
    return path if path.is_dir() else None


def token_path(profile: str) -> Optional[Path]:
    base = skill_dir(profile)
    if base is None:
        return None
    path = base / _TOKEN_REL
    return path if path.is_file() else None


def read_token(profile: str) -> Optional[Dict[str, Any]]:
    """The linked account's credentials, or None when not linked/unreadable.

    A parse failure is not necessarily corruption: the skill replaces this file
    atomically, so a read can land mid-swap. Callers treat None as "not linked
    right now" and try again on the next request.
    """
    path = token_path(profile)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug(f"[calendar] unreadable gcalendar token for profile={profile}: {exc}")
        return None
    return data if isinstance(data, dict) else None


def has_calendar_scope(data: Optional[Dict[str, Any]]) -> bool:
    """Whether the linked grant can act on calendar events.

    Membership, never list equality: Google records granted scopes in its own
    forms (``email`` comes back as ``.../auth/userinfo.email``), so comparing the
    whole list against what we expect would reject every real token.
    """
    granted = set((data or {}).get("scopes") or [])
    return bool(granted & {CALENDAR_EVENTS_SCOPE, BROAD_CALENDAR_SCOPE})


def skill_enabled(profile: str) -> bool:
    """Whether this profile has the gcalendar skill switched on.

    A disabled skill must not keep steering the calendar — that is what makes the
    page's own Connect button meaningful again. Skills have no ``profile_tools``
    row until a profile enables them, and no row means disabled.
    """
    if not profile:
        return False
    try:
        from app.storage.tool_storage import get_tool_storage
        from app.tools.ids import slugify

        # The registry ids a skill as "<profile>__<slugify(SKILL.md name)>", and
        # gcalendar's manifest name matches its directory name.
        tool_id = f"{profile}__{slugify(SKILL_DIR_NAME)}"
        return bool(get_tool_storage().list_profile_tools(profile).get(tool_id))
    except Exception as exc:  # noqa: BLE001 - storage not ready proves nothing
        logger.debug(f"[calendar] gcalendar enabled lookup failed for profile={profile}: {exc}")
        return False


def is_effective(profile: str) -> bool:
    """Whether the skill's link is the credential the calendar should use.

    Ordered cheapest-first: a missing file costs one ``stat`` and no query, which
    is what most profiles pay.
    """
    data = read_token(profile)
    if data is None or not has_calendar_scope(data):
        return False
    return skill_enabled(profile)


def status(profile: str) -> Dict[str, Any]:
    """``{linked, enabled, effective, email, scopes, account_key}``.

    ``linked`` describes the token file, ``effective`` adds the gates that decide
    whether the calendar actually uses it — a linked-but-disabled skill is worth
    reporting differently from an unlinked one.
    """
    data = read_token(profile)
    if data is None:
        return {
            "linked": False, "enabled": False, "effective": False,
            "email": None, "scopes": [], "account_key": None,
        }
    enabled = skill_enabled(profile)
    return {
        "linked": True,
        "enabled": enabled,
        "effective": enabled and has_calendar_scope(data),
        "email": data.get("email"),
        "scopes": data.get("scopes") or [],
        "account_key": data.get("account_key"),
    }


def _account(data: Dict[str, Any]) -> str:
    """An identity for the linked account, to pin cached tokens to it.

    The address alone would not notice a re-link of the *same* address (a fresh
    grant, new refresh token), so the refresh token's tail joins it. Never logged.
    """
    return f"{data.get('email') or ''}|{str(data.get('refresh_token') or '')[-12:]}"


def _refresh(profile: str, data: Dict[str, Any]) -> Optional[str]:
    """Mint a new access token in memory. Returns None on any failure."""
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": str(data.get("refresh_token") or ""),
        "client_id": str(data.get("client_id") or ""),
        "client_secret": str(data.get("client_secret") or ""),
    }
    if not payload["refresh_token"]:
        logger.warning(
            f"[calendar] gcalendar token for profile={profile} has no refresh token; "
            "re-link the skill (uv run scripts/__main__.py link)"
        )
        return None
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(TOKEN_ENDPOINT, data=payload)
            resp.raise_for_status()
            tok = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[calendar] gcalendar token refresh failed for profile={profile}: {exc}")
        return None
    access = tok.get("access_token")
    if not access:
        logger.warning(f"[calendar] gcalendar refresh returned no access token for profile={profile}")
        return None
    _access_cache[profile] = {
        "token": str(access),
        "expiry": time.time() + int(tok.get("expires_in", 3600)),
        "account": _account(data),
    }
    return str(access)


def access_token(profile: str) -> Optional[str]:
    """A usable access token from the skill's link, or None.

    Never raises: the caller (:func:`app.calendar.google_auth.get_access_token`)
    treats None as "not connected", which degrades the calendar to internal
    instead of failing a request.

    The file's own token is preferred over anything cached in memory. The skill
    refreshes on every run and writes the result, so the file is the fresher of
    the two, and after a re-link it is the *only* one describing the new account.
    """
    data = read_token(profile)
    if not data:
        return None
    stored = data.get("access_token")
    if stored and time.time() < _as_float(data.get("expiry")) - 60:
        return str(stored)
    cached = _access_cache.get(profile)
    if (
        cached
        and cached.get("account") == _account(data)
        and time.time() < _as_float(cached.get("expiry")) - 60
    ):
        return str(cached["token"])
    return _refresh(profile, data)
