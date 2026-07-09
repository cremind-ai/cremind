"""cremind-connect discovery for Google Calendar (web connect flow).

The backend Google Calendar connect reuses the same cremind-connect endpoints the
gcalendar *skill* uses (see
``app/skills/builtin/gcalendar/scripts/app/google/discovery.py``):

- ``GET {base}/.well-known/cremind-connect`` — providers, per-resource scopes, relay.
- ``GET {base}/credentials/google``          — OAuth ``clientId`` + ``clientSecret``.

so the OAuth client is managed centrally (rotatable) and the calendar consent asks
for least-privilege calendar scopes. ``CREMIND_CONNECT_URL`` overrides the base;
``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET`` env vars override the fetched
credentials (handy for local dev). Synchronous (httpx.Client) so the sync calendar
provider + the OAuth handlers can call it directly; results are cached briefly.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict

import httpx

from app.utils.logger import logger

DEFAULT_BASE = "https://connect.cremind.io"
# Used when cremind-connect predates per-resource scopes or is unreachable.
CALENDAR_SCOPES_FALLBACK = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
]

_TTL = 300.0
_cache: Dict[str, Any] = {"doc": None, "doc_at": 0.0, "creds": None, "creds_at": 0.0}


class DiscoveryError(RuntimeError):
    pass


def cremind_connect_url() -> str:
    return (os.environ.get("CREMIND_CONNECT_URL", "") or DEFAULT_BASE).strip().rstrip("/")


def _get_json(url: str, timeout: float = 15.0) -> Dict[str, Any]:
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, headers={"accept": "application/json", "user-agent": "cremind/1.0"})
        resp.raise_for_status()
        return resp.json()


def document(*, force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if _cache["doc"] is not None and not force and (now - _cache["doc_at"]) < _TTL:
        return _cache["doc"]
    base = cremind_connect_url()
    try:
        doc = _get_json(f"{base}/.well-known/cremind-connect")
    except Exception as exc:  # noqa: BLE001
        raise DiscoveryError(f"failed to fetch discovery doc from {base}: {exc}") from exc
    _cache["doc"] = doc
    _cache["doc_at"] = now
    return doc


def credentials(*, force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if _cache["creds"] is not None and not force and (now - _cache["creds_at"]) < _TTL:
        return _cache["creds"]
    base = cremind_connect_url()
    try:
        doc = _get_json(f"{base}/credentials/google")
    except Exception as exc:  # noqa: BLE001
        raise DiscoveryError(f"failed to fetch credentials from {base}: {exc}") from exc
    _cache["creds"] = doc
    _cache["creds_at"] = now
    return doc


def _provider(doc: Dict[str, Any]) -> Dict[str, Any]:
    for p in doc.get("providers", []):
        if p.get("provider") == "google":
            return p
    return {}


def calendar_scopes() -> list[str]:
    try:
        prov = _provider(document())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[google_discovery] scopes lookup fell back: {exc}")
        return list(CALENDAR_SCOPES_FALLBACK)
    for r in prov.get("resources", []):
        if r.get("resource") == "calendar":
            scopes = r.get("scopes") or []
            if scopes:
                return list(scopes)
    return list(prov.get("scopes") or CALENDAR_SCOPES_FALLBACK)


def google_client() -> Dict[str, Any]:
    """Return ``{client_id, client_secret, scopes}`` for the Google OAuth client.

    Env overrides win; otherwise the values come from cremind-connect. Raises
    :class:`DiscoveryError` when no client id can be resolved (the connect
    endpoint surfaces this as "unavailable").
    """
    env_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    env_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

    client_id = env_id
    client_secret = env_secret
    if not client_id or not client_secret:
        creds = credentials()
        client_id = client_id or creds.get("clientId", "")
        client_secret = client_secret or creds.get("clientSecret", "")
    if not client_id:
        # authClientId in the well-known doc is the last resort.
        try:
            client_id = _provider(document()).get("authClientId", "")
        except Exception:  # noqa: BLE001
            client_id = ""
    if not client_id:
        raise DiscoveryError("no Google client id available from cremind-connect")
    return {"client_id": client_id, "client_secret": client_secret, "scopes": calendar_scopes()}


def reset_cache() -> None:
    """Drop the cached discovery doc/credentials (tests / forced refresh)."""
    _cache.update({"doc": None, "doc_at": 0.0, "creds": None, "creds_at": 0.0})
