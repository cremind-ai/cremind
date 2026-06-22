"""Backend-native Google OAuth for per-profile Calendar access.

Unlike the gmail/gcalendar *skills* (subprocesses that hold a single shared file
token and never expose creds to the backend), this runs the OAuth Authorization
Code + PKCE flow **in the backend** and stores per-profile access/refresh tokens
in the ``auth_tokens`` table (``app/utils/client_storage.py``) — so the per-profile
:class:`GoogleCalendarProvider` can call the Calendar REST API directly.

The OAuth *client* (id/secret) + calendar scopes still come from cremind-connect
(:mod:`app.calendar.google_discovery`), honoring "like the Google Suite skills".

Flow:
1. ``build_authorize_url(profile)`` mints a PKCE verifier + ``state``, stashes them
   in an in-process pending map, and returns the Google consent URL.
2. Google redirects to ``/api/oauth/google-calendar/callback`` (app/api/oauth_callback.py),
   which calls ``complete_callback(state, code)`` to exchange + persist tokens.
3. ``get_access_token(profile)`` returns a valid access token, refreshing on demand.

Synchronous (httpx.Client): the one-time connect/callback briefly block, and the
provider's per-load calls match the codebase's sync-storage-in-async-handler style.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

from app.config.settings import BaseConfig
from app.calendar import google_discovery
from app.utils.client_storage import (
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    get_auth_client_storage,
)
from app.utils.logger import logger

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/api/oauth/google-calendar/callback"

# Least-privilege: ONLY calendar event access. ``calendar.events`` covers
# view/create/edit/delete of events without Gmail, profile, or full
# calendar-management ("delete all calendars") powers. Deliberately NO
# openid/email/profile — so the consent screen shows only "Calendar events".
# (Trade-off: we can't read the account email; status shows just "Connected".)
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# auth_tokens row coordinates for the per-profile Google Calendar tokens.
AGENT_NAME = "google_calendar"
AGENT_TYPE = "google"
META_KIND = "google_meta"  # JSON {email, expiry, scopes}

# Pending consent flows keyed by OAuth ``state``: {profile, verifier, redirect_uri, ts}.
_pending: Dict[str, Dict[str, Any]] = {}
_PENDING_TTL = 600.0


class GoogleAuthError(RuntimeError):
    pass


# ── redirect / PKCE helpers ─────────────────────────────────────────────────

def redirect_uri() -> Optional[str]:
    """Browser-facing callback, derived from APP_URL (same basis the skills use).

    None when APP_URL is the unusable listen-all default; the connect endpoint
    then reports "unavailable".
    """
    base = (BaseConfig.APP_URL or "").strip().rstrip("/")
    if not base or "://0.0.0.0" in base:
        return None
    return base + CALLBACK_PATH


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    return verifier, challenge


def _prune() -> None:
    now = time.time()
    for state in [s for s, p in _pending.items() if now - p.get("ts", 0) > _PENDING_TTL]:
        _pending.pop(state, None)


def _jwt_email(id_token: str) -> Optional[str]:
    """Pull ``email`` from a Google id_token (no verification — self-owned)."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("email")
    except Exception:  # noqa: BLE001
        return None


def _post_token(data: Dict[str, str]) -> Dict[str, Any]:
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(TOKEN_ENDPOINT, data=data)
        resp.raise_for_status()
        return resp.json()


def _load_meta(storage, profile: str) -> Dict[str, Any]:
    raw = storage.get_token(AGENT_NAME, profile, agent_type=AGENT_TYPE, token_kind=META_KIND)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


# ── public API ───────────────────────────────────────────────────────────────

def build_authorize_url(profile: str) -> Optional[str]:
    """Return the Google consent URL for ``profile``, or None if unavailable
    (no redirect URI, or cremind-connect can't supply a client)."""
    ru = redirect_uri()
    if not ru:
        logger.warning("[google_auth] no usable redirect URI (APP_URL); cannot connect")
        return None
    try:
        client = google_discovery.google_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[google_auth] cremind-connect client unavailable: {exc}")
        return None

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    _pending[state] = {"profile": profile, "verifier": verifier, "redirect_uri": ru, "ts": time.time()}
    _prune()
    params = {
        "client_id": client["client_id"],
        "redirect_uri": ru,
        "response_type": "code",
        # Fixed minimal calendar scope — NOT the broad cremind-connect provider
        # scopes. No include_granted_scopes (it would re-request previously
        # granted Gmail/profile scopes from the shared client).
        "scope": " ".join(CALENDAR_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def complete_callback(state: str, code: str) -> Dict[str, Any]:
    """Exchange ``code`` for tokens and persist them per-profile.

    Called by the callback route. Raises :class:`GoogleAuthError` on unknown
    state or exchange failure.
    """
    pend = _pending.pop(state, None)
    if not pend:
        raise GoogleAuthError("unknown or expired OAuth state")
    client = google_discovery.google_client()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pend["redirect_uri"],
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "code_verifier": pend["verifier"],
    }
    try:
        tok = _post_token(data)
    except Exception as exc:  # noqa: BLE001
        raise GoogleAuthError(f"token exchange failed: {exc}") from exc

    access = tok.get("access_token")
    if not access:
        raise GoogleAuthError("token response had no access_token")
    refresh = tok.get("refresh_token")
    expires_in = int(tok.get("expires_in", 3600))
    # No openid/email scope, so no id_token email — left as None (UI shows
    # "Connected"). _jwt_email tolerates the empty id_token gracefully.
    email = _jwt_email(tok.get("id_token", "") or "")
    profile = pend["profile"]

    storage = get_auth_client_storage()
    storage.save_token(AGENT_NAME, profile, access, agent_type=AGENT_TYPE, token_kind=ACCESS_TOKEN)
    if refresh:
        storage.save_token(AGENT_NAME, profile, refresh, agent_type=AGENT_TYPE, token_kind=REFRESH_TOKEN)
    meta = {"email": email, "expiry": time.time() + expires_in, "scopes": CALENDAR_SCOPES}
    storage.save_token(AGENT_NAME, profile, json.dumps(meta), agent_type=AGENT_TYPE, token_kind=META_KIND)
    logger.info(f"[google_auth] connected Google Calendar for profile={profile} email={email}")
    return {"profile": profile, "email": email}


def get_access_token(profile: str) -> Optional[str]:
    """Return a valid access token for ``profile``, refreshing when expired."""
    storage = get_auth_client_storage()
    access = storage.get_token(AGENT_NAME, profile, agent_type=AGENT_TYPE, token_kind=ACCESS_TOKEN)
    if not access:
        return None
    meta = _load_meta(storage, profile)
    if time.time() < float(meta.get("expiry", 0)) - 60:
        return access
    # Expired (or unknown expiry): refresh if we can.
    refresh = storage.get_token(AGENT_NAME, profile, agent_type=AGENT_TYPE, token_kind=REFRESH_TOKEN)
    if not refresh:
        return access  # stale; the caller will see a 401 and can prompt reconnect
    try:
        client = google_discovery.google_client()
        tok = _post_token({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[google_auth] refresh failed for profile={profile}: {exc}")
        return access
    new_access = tok.get("access_token")
    if not new_access:
        return access
    expires_in = int(tok.get("expires_in", 3600))
    storage.save_token(AGENT_NAME, profile, new_access, agent_type=AGENT_TYPE, token_kind=ACCESS_TOKEN)
    meta["expiry"] = time.time() + expires_in
    storage.save_token(AGENT_NAME, profile, json.dumps(meta), agent_type=AGENT_TYPE, token_kind=META_KIND)
    return new_access


def status(profile: str) -> Dict[str, Any]:
    """``{connected, email}`` for ``profile``."""
    try:
        storage = get_auth_client_storage()
        access = storage.get_token(AGENT_NAME, profile, agent_type=AGENT_TYPE, token_kind=ACCESS_TOKEN)
        meta = _load_meta(storage, profile)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[google_auth] status lookup failed: {exc}")
        return {"connected": False, "email": None}
    return {"connected": bool(access), "email": meta.get("email")}


def disconnect(profile: str) -> None:
    storage = get_auth_client_storage()
    for kind in (ACCESS_TOKEN, REFRESH_TOKEN, META_KIND):
        try:
            storage.delete_token(AGENT_NAME, profile, agent_type=AGENT_TYPE, token_kind=kind)
        except Exception:  # noqa: BLE001
            pass
    logger.info(f"[google_auth] disconnected Google Calendar for profile={profile}")
