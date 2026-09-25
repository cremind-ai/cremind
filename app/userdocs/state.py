"""The one snapshot every surface reads: web UI, CLI, and (later) the agent tool.

A snapshot is the complete answer to "what is User Document Search doing for
this profile right now", so every frame on the progress stream can replace the
previous one outright. It is assembled from three layers:

1. the admin gate (:func:`app.userdocs.settings.read_admin_policy`),
2. the profile's saved settings (``userdoc_sources``),
3. the sync engine's live runtime state, when an engine is running — supplied
   through :func:`set_runtime_provider`, so this module has no import-time
   dependency on the engine.

The top-level ``state``/``reason`` pair follows the precedence table in the
design: an admin-level suspension outranks anything the profile did, which
outranks what the engine reports.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from app.userdocs import settings as uds

SNAPSHOT_VERSION = 1

# Distinguishes this server process's frames from a previous one's, so a
# client that reconnects after a restart does not discard fresh frames as
# "older" because their sequence number restarted at 1.
BOOT_ID = uuid.uuid4().hex[:12]

_seq_lock = threading.Lock()
_seq = 0


def _next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


RuntimeProvider = Callable[[str], dict[str, Any] | None]
_runtime_provider: RuntimeProvider | None = None


def set_runtime_provider(fn: RuntimeProvider | None) -> None:
    """Register the sync engine's per-profile runtime snapshot function."""
    global _runtime_provider
    _runtime_provider = fn


SettingsListener = Callable[[str, str], None]
_settings_listeners: list[SettingsListener] = []


def add_settings_listener(fn: SettingsListener) -> None:
    """Call ``fn(profile, kind)`` after a profile's source settings are saved
    (enable, disable, root change, excludes, options). The sync engine uses it
    to start, stop or re-scope its watcher without the API importing it."""
    if fn not in _settings_listeners:
        _settings_listeners.append(fn)


PurgeHandler = Callable[[str, str], None]
_purge_handler: PurgeHandler | None = None


def set_purge_handler(fn: PurgeHandler | None) -> None:
    """Register what "delete this source's index" does. The sync engine owns
    the index file and the vector collection, so it supplies the handler."""
    global _purge_handler
    _purge_handler = fn


def request_purge(profile: str, kind: str) -> bool:
    """Ask the engine to delete ``profile``'s index for source ``kind``.

    Returns False when no engine is registered — which only happens before any
    index can exist, so there is nothing to delete.
    """
    if _purge_handler is None:
        return False
    _purge_handler(profile, kind)
    return True


def notify_settings_changed(profile: str, kind: str) -> None:
    for fn in list(_settings_listeners):
        try:
            fn(profile, kind)
        except Exception as exc:  # noqa: BLE001 — one listener must not starve the rest
            from app.utils.logger import logger
            logger.warning(f"[userdocs] settings listener failed for {profile}/{kind}: {exc}")
    publish_snapshot(profile)


def _runtime(profile: str) -> dict[str, Any] | None:
    if _runtime_provider is None:
        return None
    try:
        return _runtime_provider(profile)
    except Exception:  # noqa: BLE001 — a broken engine must not break the stream
        return None


def _source_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "enabled": bool(row.get("enabled")),
        "root_mode": row.get("root_mode"),
        "root": row.get("root_path"),
        "first_sync_confirmed": bool(row.get("first_sync_confirmed_at")),
    }


def build_snapshot(profile: str, *, storage=None) -> dict[str, Any]:
    """Assemble the current snapshot for ``profile``. Synchronous and cheap
    (two indexed reads plus the engine's in-memory counters); API handlers
    call it through ``asyncio.to_thread``."""
    if storage is None:
        from app.storage.userdocs_storage import get_userdocs_storage
        storage = get_userdocs_storage()
    policy = uds.read_admin_policy()
    effective, gate_reason = uds.feature_effective(policy)

    local = storage.get_source(profile, uds.SOURCE_LOCAL)
    drive = storage.get_source(profile, uds.SOURCE_DRIVE)
    enabled = bool(local and local.get("enabled"))

    snap: dict[str, Any] = {
        "v": SNAPSHOT_VERSION,
        "boot": BOOT_ID,
        "seq": _next_seq(),
        "ts": time.time() * 1000,
        "allowed": policy.allowed,
        "enabled": enabled,
        "state": "disabled",
        "reason": None,
        # How the agent tool behaves right now: hidden | normal | lexical_only
        "tool_mode": "hidden",
        "sources": {
            uds.SOURCE_LOCAL: _source_view(local),
            uds.SOURCE_DRIVE: _source_view(drive),
        },
    }

    if not policy.allowed:
        snap.update(state="suspended", reason="admin_gate")
        return snap
    if not enabled:
        return snap
    if gate_reason == "embedding_disabled":
        # The index is kept and stays searchable by keyword; sync stops.
        snap.update(state="suspended", reason="embedding_off", tool_mode="lexical_only")
        return snap

    snap.update(state="idle", tool_mode="normal")
    runtime = _runtime(profile)
    if runtime:
        # The engine owns every field it reports; top-level state/reason from
        # the engine replace the "idle" default.
        snap.update(runtime)
    return snap


def publish_snapshot(profile: str) -> None:
    """Build and publish ``profile``'s snapshot. Safe from any thread."""
    from app.events.userdocs_bus import get_userdocs_stream_bus, publish_userdocs_changed
    if not get_userdocs_stream_bus().has_subscribers(profile):
        return  # nobody is watching — skip the reads
    try:
        publish_userdocs_changed(profile, build_snapshot(profile))
    except Exception:  # noqa: BLE001
        pass
