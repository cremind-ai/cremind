"""In-memory registry of browser/Electron tabs capable of HTTPS handoff.

Activation snapshots these authenticated clients into private transition
metadata.  It never advances the transport epoch until each snapshot member
has acknowledged that uploads settled and a fresh handoff exists.  A crashed
client therefore leaves the transition safely quiescing; the administrator can
cancel and retry rather than silently losing that tab.
"""
from __future__ import annotations

import re
import threading
import time

_TAB_ID = re.compile(r"^[A-Za-z0-9_-]{12,128}$")
_lock = threading.RLock()
# Browser sessions last 30 days by default. Keep a renderer registration for a
# little longer so a laptop sleep or discarded background tab is not mistaken
# for a dead client, while eventually removing entries left by crashed tabs.
STALE_AFTER_SECONDS = 31 * 24 * 60 * 60
# Match the durable ticket admission limits: every client accepted into a
# quiesce snapshot must be able to mint the one handoff activation requires.
MAX_CLIENTS_PER_PROFILE = 64
MAX_CLIENTS_TOTAL = 512
_clients: dict[str, tuple[str, float]] = {}


def _prune_stale(now: float) -> None:
    cutoff = now - STALE_AFTER_SECONDS
    for tab_id, (_profile, last_seen) in tuple(_clients.items()):
        if last_seen < cutoff:
            _clients.pop(tab_id, None)


def validate_tab_id(value: object) -> str:
    if not isinstance(value, str) or not _TAB_ID.fullmatch(value):
        raise ValueError("A valid HTTPS migration tab id is required.")
    return value


def register(tab_id: object, profile: str) -> str:
    tab_id = validate_tab_id(tab_id)
    if not profile:
        raise ValueError("An authenticated profile is required.")
    with _lock:
        now = time.monotonic()
        _prune_stale(now)
        existing = _clients.get(tab_id)
        owner = existing[0] if existing is not None else None
        if owner is not None and owner != profile:
            # Tab ids are visible on the credential-free same-origin readiness
            # channel.  Treat an existing id as profile-bound so another
            # authenticated profile cannot replace the durable quiesce owner
            # and acknowledge on the real tab's behalf.
            raise ValueError("This HTTPS migration tab id belongs to another profile.")
        if existing is None:
            profile_count = sum(item_profile == profile for item_profile, _ in _clients.values())
            if profile_count >= MAX_CLIENTS_PER_PROFILE:
                raise ValueError("This profile has too many HTTPS migration clients.")
            if len(_clients) >= MAX_CLIENTS_TOTAL:
                raise ValueError("This server has too many HTTPS migration clients.")
        _clients[tab_id] = (profile, now)
    return tab_id


def unregister(tab_id: object, profile: str) -> None:
    tab_id = validate_tab_id(tab_id)
    with _lock:
        existing = _clients.get(tab_id)
        if existing is not None and existing[0] == profile:
            _clients.pop(tab_id, None)


def snapshot() -> dict[str, str]:
    with _lock:
        _prune_stale(time.monotonic())
        return {tab_id: profile for tab_id, (profile, _last_seen) in _clients.items()}


def remove(tab_ids: set[str]) -> None:
    with _lock:
        for tab_id in tab_ids:
            _clients.pop(tab_id, None)


def clear() -> None:
    """Reset the process-local registry (used by isolated server tests)."""
    with _lock:
        _clients.clear()
