"""The one snapshot every surface reads: web UI, CLI, and (later) the agent tool.

A snapshot is the complete answer to "what is Documentation search doing for
this profile right now", so every frame on the progress stream can replace the
previous one outright. It is assembled from three layers:

1. the admin gate (:func:`app.documents.settings.read_admin_policy`),
2. the profile's saved settings (``document_sources``),
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

from app.documents import settings as uds

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


DriveSuspendHandler = Callable[[str], None]
_drive_suspend_handler: DriveSuspendHandler | None = None


def set_drive_suspend_handler(fn: DriveSuspendHandler | None) -> None:
    """Register what "stop Drive for this profile now" does (the engine's)."""
    global _drive_suspend_handler
    _drive_suspend_handler = fn


def suspend_drive(profile: str) -> bool:
    """Stop ``profile``'s Drive sync until its token is checked again — the
    Google unlink hook calls this for every Google skill it removes, so it
    decides nothing by itself: a still-linked Drive resumes on that check.

    Never blocks (the engine queues it) and is a no-op with no engine
    running; returns whether an engine took it."""
    if _drive_suspend_handler is None:
        return False
    _drive_suspend_handler(profile)
    return True


def notify_settings_changed(profile: str, kind: str) -> None:
    for fn in list(_settings_listeners):
        try:
            fn(profile, kind)
        except Exception as exc:  # noqa: BLE001 — one listener must not starve the rest
            from app.utils.logger import logger
            logger.warning(f"[documents] settings listener failed for {profile}/{kind}: {exc}")
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
    # ``root``: the folder the index was built from — the profile's working
    # directory, or its previous one while a move waits for confirmation.
    return {
        "enabled": bool(row.get("enabled")),
        "root": row.get("root_path"),
        "first_sync_confirmed": bool(row.get("first_sync_confirmed_at")),
    }


def _drive_default(row: dict[str, Any] | None, effective: bool, gate_reason: str | None) -> dict[str, Any]:
    """The Drive part of a snapshot when no engine reports one."""
    on = bool(row and row.get("enabled"))
    if on and not effective:
        reason = "admin_gate" if gate_reason == "admin_gate_off" else "embedding_off"
        return {"enabled": False, "state": "suspended", "reason": reason}
    return {"enabled": False, "state": "disabled", "reason": None}


def build_snapshot(profile: str, *, storage=None) -> dict[str, Any]:
    """Assemble the current snapshot for ``profile``. Synchronous and cheap
    (two indexed reads plus the engine's in-memory counters); API handlers
    call it through ``asyncio.to_thread``.

    The feature is on when either source is: a profile may index Google
    Drive with its local folder off. ``drive`` is the Drive half's own view
    (state, hold, counts, confirmation) — its holds never replace the
    top-level state, which belongs to the folder and the engine as a whole."""
    if storage is None:
        from app.storage.documents_storage import get_documents_storage
        storage = get_documents_storage()
    policy = uds.read_admin_policy()
    effective, gate_reason = uds.feature_effective(policy)

    local = storage.get_source(profile, uds.SOURCE_LOCAL)
    drive = storage.get_source(profile, uds.SOURCE_DRIVE)
    enabled = bool(local and local.get("enabled")) or bool(drive and drive.get("enabled"))

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
        "drive": _drive_default(drive, effective, gate_reason),
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
    from app.events.documents_bus import get_documents_stream_bus, publish_documents_changed
    if not get_documents_stream_bus().has_subscribers(profile):
        return  # nobody is watching — skip the reads
    try:
        publish_documents_changed(profile, build_snapshot(profile))
    except Exception:  # noqa: BLE001
        pass
