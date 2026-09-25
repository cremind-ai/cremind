"""Whether the agent may see the User Documents tool in this conversation.

Evaluated once per agent run, in ``ReasoningAgent.__init__``, next to the
other conversation-constant tool gates — so the ``tools=`` block and the
guidance built from it stay byte-stable for the whole run (prompt caching).
It reads only cheap state — the admin gate (a config value) and the
profile's settings row — and caches both for a few seconds: agent runs are
built on the event loop, and a busy channel can start several a second, so
the gate must not turn every run into database round trips.

Hidden when the admin has not allowed the feature, when the profile has
neither a local folder (on, with a root) nor Google Drive indexing on, and
when the conversation's origin is not one the profile allowed
(``options.allow_in`` — on the local row, which holds the per-profile
options even when only Drive is on):

- ``web_cli`` — the web UI, the CLI, and the profile's own automations
  (runs with no message origin); on by default;
- ``channels`` — a messaging channel conversation (Telegram, Zalo, …); off
  by default, because a channel in open mode answers whoever writes to it;
- ``rooms`` — a group room, Cremind's own or a platform group; off by
  default, because an answer there is read by other people.

Vector Embedding being off does not hide the tool: the index stays
searchable by keyword, and the tool says so in every result.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from app.utils.logger import logger

ORIGIN_WEB_CLI = "web_cli"
ORIGIN_CHANNELS = "channels"
ORIGIN_ROOMS = "rooms"

# A settings save invalidates the cache at once (see _ensure_listener); the
# TTL only bounds how stale a value can be if that notification is missed.
_CACHE_TTL_S = 5.0
_ADMIN_KEY = "__admin_allowed__"
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()
_listening = False


def origin_class(message_origin: dict[str, Any] | None) -> str:
    """Map a ReasoningAgent ``message_origin`` to an ``allow_in`` key."""
    source = (message_origin or {}).get("source")
    if source in ("group_chat", "channel_group"):
        return ORIGIN_ROOMS
    if source == "channel":
        return ORIGIN_CHANNELS
    return ORIGIN_WEB_CLI


def _drive_key(profile: str) -> str:
    return f"{profile}\x00drive"


def _invalidate(profile: str, _kind: str) -> None:
    # An admin-gate save notifies every profile, so the gate value goes too.
    with _lock:
        _cache.pop(profile, None)
        _cache.pop(_drive_key(profile), None)
        _cache.pop(_ADMIN_KEY, None)


def _ensure_listener() -> None:
    global _listening
    if _listening:
        return
    try:
        from app.userdocs import state as uds_state

        uds_state.add_settings_listener(_invalidate)
        _listening = True
    except Exception:  # noqa: BLE001 — the TTL still bounds staleness
        pass


def _cached(key: str, load) -> Any:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    value = load()
    with _lock:
        _cache[key] = (now, value)
    return value


def _admin_allowed() -> bool:
    from app.userdocs import settings as uds

    return bool(_cached(_ADMIN_KEY, lambda: uds.read_admin_policy().allowed))


def _source_row(profile: str) -> dict[str, Any] | None:
    from app.storage.userdocs_storage import get_userdocs_storage
    from app.userdocs import settings as uds

    return _cached(profile, lambda: get_userdocs_storage().get_source(profile, uds.SOURCE_LOCAL))


def _drive_row(profile: str) -> dict[str, Any] | None:
    from app.storage.userdocs_storage import get_userdocs_storage
    from app.userdocs import settings as uds

    return _cached(_drive_key(profile), lambda: get_userdocs_storage().get_source(profile, uds.SOURCE_DRIVE))


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def userdocs_tool_available(profile: str, origin: str | dict[str, Any] | None = None) -> bool:
    """True when the User Documents tool should be offered to ``profile``'s
    agent in a conversation of ``origin`` (an ``allow_in`` key, or the raw
    ``message_origin`` dict). Never raises: any failure hides the tool."""
    from app.userdocs import settings as uds

    _ensure_listener()
    try:
        if not _admin_allowed():
            return False
        row = _source_row(profile)
        local_on = bool(row and row.get("enabled") and row.get("root_path"))
        if not local_on:
            drive = _drive_row(profile)
            if not (drive and drive.get("enabled")):
                return False
        key = origin if isinstance(origin, str) else origin_class(origin)
        allow = uds.normalize_options((row or {}).get("options")).get("allow_in") or {}
        return bool(allow.get(key, uds.DEFAULT_ALLOW_IN.get(key, False)))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[userdocs] tool gate for {profile} failed: {exc}")
        return False


__all__ = [
    "ORIGIN_CHANNELS",
    "ORIGIN_ROOMS",
    "ORIGIN_WEB_CLI",
    "clear_cache",
    "origin_class",
    "userdocs_tool_available",
]
