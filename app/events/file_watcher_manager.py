"""Filesystem watchdog manager for file-watcher subscriptions.

A single watchdog ``Observer`` is mounted per (profile, root_path, recursive)
key. Multiple subscriptions on the same root share one Observer; each
incoming watchdog event is filtered per-subscription (target_kind,
event_types, extensions) before being fanned out to the per-conversation
queue.

Watchdog callbacks run on a watchdog thread; we bridge into the asyncio
loop via :func:`asyncio.AbstractEventLoop.call_soon_threadsafe` so the queue
and runner stay on the loop they were created on.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from app.storage import get_file_watcher_storage
from app.utils.logger import logger


_WatcherKey = Tuple[str, str, bool]  # (profile, root_path_normcase, recursive)

# Hardcoded ignore patterns for common IDE/editor temp files. Applied first;
# matched events are dropped before debounce / per-subscription filtering.
_IGNORE_PATTERNS: Tuple[str, ...] = (
    "*.swp", "*.swx", "*.swo",
    "~$*",                       # MS Office lock files
    "*.tmp", "*.temp",
    ".DS_Store",
    "*.crdownload", "*.part",
)

# Per-(path, event_type) debounce window. Watchdog (especially on Windows)
# often emits multiple modified events in rapid succession for one save;
# 500 ms collapses them without losing meaningful change activity.
_DEBOUNCE_SECONDS: float = 0.5

# Window during which `created` + `modified` events for the same path are
# merged into one payload (event_type becomes "created, modified"). Triggered
# typically when a file is moved INTO a watched directory: watchdog emits a
# created and a modified back-to-back. Coalescing prevents the agent from
# being woken twice for the same interaction.
_COALESCE_SECONDS: float = 0.5
_COALESCEABLE: frozenset[str] = frozenset({"created", "modified"})


def _normalize_path(p: str) -> str:
    """Return an absolute, normcased, normpath form of ``p``.

    Used as the key for both the observer index and the debounce dict so
    Windows ``C:\\Foo`` and ``c:/foo`` collapse to one entry.
    """
    return os.path.normcase(os.path.abspath(os.path.normpath(p)))


def _matches_ignore(path: str) -> bool:
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(name, pat) for pat in _IGNORE_PATTERNS)


def _watchdog_event_type(event: FileSystemEvent) -> Optional[str]:
    """Map a watchdog event class onto our four event-type names."""
    if isinstance(event, (FileCreatedEvent, DirCreatedEvent)):
        return "created"
    if isinstance(event, (FileModifiedEvent, DirModifiedEvent)):
        return "modified"
    if isinstance(event, (FileDeletedEvent, DirDeletedEvent)):
        return "deleted"
    if isinstance(event, (FileMovedEvent, DirMovedEvent)):
        return "moved"
    return None


class _SharedHandler(FileSystemEventHandler):
    """Single handler per (profile, root, recursive) — fans out to all subs.

    The handler queries the DB on each event for the current subscription
    list rather than caching it in memory; this keeps deletes immediate
    and removes the need for cache-invalidation on add/remove.
    """

    def __init__(
        self,
        *,
        profile: str,
        root_path: str,
        recursive: bool,
        loop: asyncio.AbstractEventLoop,
    ):
        super().__init__()
        self._profile = profile
        # ``root_path`` is the verbatim value stored in the DB so that
        # :meth:`list_by_root` matches with ``WHERE root_path = ?`` exactly.
        # Path-equality dedupe across subscriptions happens at the index-key
        # level (normcased) instead of here.
        self._root_path = root_path
        self._recursive = recursive
        self._loop = loop
        self._debounce: Dict[Tuple[str, str], float] = {}
        self._debounce_lock = threading.Lock()
        # Coalesce buffer for {created, modified} bursts on the same path.
        # Touched only on the asyncio loop (via _coalesce_or_fan_out / _flush_key),
        # so no extra lock is needed. Key: (target_kind, path).
        self._pending: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._pending_timers: Dict[Tuple[str, str], asyncio.TimerHandle] = {}

    # watchdog dispatches to specific on_*; route them all through one path.
    def on_created(self, event: FileSystemEvent) -> None:
        self._dispatch(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._dispatch(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._dispatch(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._dispatch(event)

    def _dispatch(self, event: FileSystemEvent) -> None:
        event_type = _watchdog_event_type(event)
        if event_type is None:
            return

        src_path = str(event.src_path) if event.src_path else ""
        if not src_path:
            return

        logger.debug(
            f"FileWatcherManager: watchdog event "
            f"type={event_type} src={src_path} is_dir={event.is_directory} "
            f"profile={self._profile} root={self._root_path}"
        )

        if _matches_ignore(src_path):
            return

        # For moved events, also drop if the destination is an ignored pattern
        # — common for editors that write to a temp file and rename in place.
        dest_path = ""
        if isinstance(event, (FileMovedEvent, DirMovedEvent)):
            dest_path = str(event.dest_path) if event.dest_path else ""
            if dest_path and _matches_ignore(dest_path):
                return

        # Debounce identical (path, event_type) bursts.
        debounce_key = (src_path, event_type)
        now = time.monotonic()
        with self._debounce_lock:
            last = self._debounce.get(debounce_key)
            if last is not None and (now - last) < _DEBOUNCE_SECONDS:
                self._debounce[debounce_key] = now
                return
            self._debounce[debounce_key] = now
            # Cheap GC: trim entries older than 60s every dispatch.
            if len(self._debounce) > 256:
                cutoff = now - 60.0
                self._debounce = {
                    k: v for k, v in self._debounce.items() if v >= cutoff
                }

        is_directory = bool(getattr(event, "is_directory", False))
        target_kind = "folder" if is_directory else "file"

        ext = ""
        if not is_directory:
            ext = Path(src_path).suffix.lower()

        detected_at = datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )

        payload: Dict[str, Any] = {
            "event_type": event_type,
            "target_kind": target_kind,
            "path": src_path,
            "extension": ext,
            "detected_at": detected_at,
        }
        if event_type == "moved":
            payload["src_path"] = src_path
            payload["dest_path"] = dest_path

        try:
            asyncio.run_coroutine_threadsafe(
                self._coalesce_or_fan_out(payload), self._loop,
            )
        except Exception:  # noqa: BLE001
            logger.exception("FileWatcherManager: failed to schedule fan-out")

    @staticmethod
    def _passes_filter(sub: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        # event_types — comma-separated subset of {created, modified, deleted, moved}
        configured_events = {
            t.strip() for t in (sub.get("event_types") or "").split(",") if t.strip()
        }
        # payload["event_type"] may be a comma-separated set after coalescing
        # (e.g. "created, modified"); pass if any constituent type matches.
        payload_events = {
            t.strip() for t in payload["event_type"].split(",") if t.strip()
        }
        if configured_events and configured_events.isdisjoint(payload_events):
            return False

        # target_kind — "any" allows everything; otherwise must match exactly
        target_kind = (sub.get("target_kind") or "any").strip()
        if target_kind != "any" and target_kind != payload["target_kind"]:
            return False

        # extensions — only meaningful for files; folders bypass the filter
        if payload["target_kind"] == "file":
            exts_raw = (sub.get("extensions") or "").strip()
            if exts_raw:
                allowed = {
                    e.strip().lower() for e in exts_raw.split(",") if e.strip()
                }
                # Normalize: ensure each entry has a leading dot for fair match
                allowed = {e if e.startswith(".") else f".{e}" for e in allowed}
                if payload["extension"] not in allowed:
                    return False
        return True

    async def _coalesce_or_fan_out(self, payload: Dict[str, Any]) -> None:
        """Buffer created/modified pairs for the same path; pass others through.

        Watchdog emits both a created and a modified event when a file lands
        in a watched directory (move-in, atomic-write rename, etc.). Holding
        them for ``_COALESCE_SECONDS`` and merging into one payload avoids
        waking the reasoning agent twice for the same interaction.
        """
        event_type = payload["event_type"]
        key = (payload["target_kind"], payload["path"])

        if event_type not in _COALESCEABLE:
            # moved/deleted: flush any pending merge for this path first so
            # ordering is preserved, then dispatch the new event normally.
            await self._flush_key(key)
            await self._fan_out(payload)
            return

        existing = self._pending.get(key)
        if existing is None:
            self._pending[key] = dict(payload)

            def _fire() -> None:
                # Schedule the async flush on the loop; call_later requires a
                # plain callable.
                asyncio.create_task(self._flush_key(key))

            self._pending_timers[key] = self._loop.call_later(
                _COALESCE_SECONDS, _fire,
            )
            return

        # Merge: append this event_type to the existing payload's comma-list,
        # preserving insertion order and de-duping. Keep the original
        # detected_at — it represents when the activity started. Don't reset
        # the timer; a sustained burst should still flush within the original
        # window rather than starving the agent.
        existing_types = [
            t.strip() for t in existing["event_type"].split(",") if t.strip()
        ]
        if event_type not in existing_types:
            existing_types.append(event_type)
            existing["event_type"] = ", ".join(existing_types)

    async def _flush_key(self, key: Tuple[str, str]) -> None:
        """Drain a pending coalesced payload (if any) into ``_fan_out``."""
        payload = self._pending.pop(key, None)
        timer = self._pending_timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        if payload is not None:
            await self._fan_out(payload)

    async def _fan_out(self, payload: Dict[str, Any]) -> None:
        try:
            subs = get_file_watcher_storage().list_by_root(
                profile=self._profile,
                root_path=self._root_path,
                recursive=self._recursive,
            )
        except Exception:  # noqa: BLE001
            logger.exception("FileWatcherManager: subscription lookup failed")
            return
        matched = 0
        for sub in subs:
            if sub.get("paused"):
                # Retained but skipped; the shared per-root observer keeps
                # serving this root's other subscriptions.
                continue
            if not self._passes_filter(sub, payload):
                continue
            matched += 1
            try:
                from app.events import run_dispatcher
                await run_dispatcher.dispatch_file_watcher_event(
                    sub=sub, payload=dict(payload),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"FileWatcherManager: dispatch failed for subscription {sub.get('id')}"
                )
        if matched:
            logger.info(
                f"FileWatcherManager: dispatched {matched}/{len(subs)} subs "
                f"for {payload['event_type']} {payload['path']!r}"
            )
        else:
            logger.debug(
                f"FileWatcherManager: no subs matched filter for "
                f"{payload['event_type']} {payload['path']!r} "
                f"(checked {len(subs)} subs on this root)"
            )


class FileWatcherManager:
    """Owns one watchdog Observer per watched (profile, root, recursive)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observers: Dict[_WatcherKey, Observer] = {}
        self._handlers: Dict[_WatcherKey, _SharedHandler] = {}
        # Subscription ids that failed to arm at boot or after a path
        # disappeared — surfaced via admin SSE so the UI can flag them.
        self._unarmed_subscription_ids: set[str] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the asyncio loop and arm watchers for every existing subscription."""
        self._loop = loop
        existing: List[Dict[str, Any]] = []
        try:
            existing = get_file_watcher_storage().list_all()
        except Exception:  # noqa: BLE001
            logger.exception(
                "FileWatcherManager: failed to load subscriptions on start"
            )
        logger.info(
            f"FileWatcherManager: started; arming {len(existing)} "
            f"existing subscription(s)"
        )
        for sub in existing:
            try:
                self._arm_subscription_locked(sub)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"FileWatcherManager: failed to arm subscription "
                    f"{sub.get('id')!r} on start"
                )

    def stop(self) -> None:
        with self._lock:
            for obs in self._observers.values():
                try:
                    obs.stop()
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[file_watcher] observer stop raised: {e}", exc_info=True)
            for obs in self._observers.values():
                try:
                    obs.join(timeout=2)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[file_watcher] observer join raised: {e}", exc_info=True)
            self._observers.clear()
            self._handlers.clear()

    # ── public API ─────────────────────────────────────────────────────

    def arm(self, subscription: Dict[str, Any]) -> bool:
        """Ensure an Observer is running for this subscription's root.

        Idempotent: if another subscription already shares the same root +
        recursive flag, the existing observer absorbs this one (filter logic
        re-runs from the DB on each event). Returns True iff the observer
        is live for this root, False if the path is missing/unreadable.
        """
        # Defensive loop capture: ``start(loop)`` should always have run at
        # server boot, but if a tool call sneaks in before that (or after a
        # weird teardown), grab the current running loop instead of raising.
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
                logger.warning(
                    "FileWatcherManager.arm: start() had not been called; "
                    "captured current running loop as a fallback"
                )
            except RuntimeError:
                logger.error(
                    "FileWatcherManager.arm: no running loop and start() "
                    "was never called; subscription will remain unarmed"
                )
                return False
        logger.info(
            f"FileWatcherManager: arm requested for "
            f"sub_id={subscription.get('id')!r} "
            f"root={subscription.get('root_path')!r} "
            f"recursive={subscription.get('recursive')}"
        )
        return self._arm_subscription_locked(subscription)

    def disarm(self, subscription: Dict[str, Any]) -> None:
        """Tear down the observer for this subscription's root if no peers remain."""
        profile = subscription["profile"]
        root_path = subscription["root_path"]
        recursive = bool(subscription.get("recursive", True))
        try:
            remaining = get_file_watcher_storage().list_by_root(
                profile=profile, root_path=root_path, recursive=recursive,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "FileWatcherManager.disarm: subscription lookup failed"
            )
            return
        if remaining:
            return
        key = (profile, _normalize_path(root_path), recursive)
        with self._lock:
            observer = self._observers.pop(key, None)
            self._handlers.pop(key, None)
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=2)
        except Exception:  # noqa: BLE001
            logger.exception("FileWatcherManager: error stopping observer")
        logger.info(
            f"FileWatcherManager: stopped watching {root_path} "
            f"(no subscribers remain)"
        )

    def is_armed(self, subscription: Dict[str, Any]) -> bool:
        """True iff a live Observer covers this subscription's root."""
        profile = subscription["profile"]
        root_path = subscription["root_path"]
        recursive = bool(subscription.get("recursive", True))
        key = (profile, _normalize_path(root_path), recursive)
        with self._lock:
            return key in self._observers and subscription["id"] not in self._unarmed_subscription_ids

    # ── helpers ────────────────────────────────────────────────────────

    def _arm_subscription_locked(self, subscription: Dict[str, Any]) -> bool:
        profile = subscription["profile"]
        root_path = subscription["root_path"]
        recursive = bool(subscription.get("recursive", True))
        key = (profile, _normalize_path(root_path), recursive)

        with self._lock:
            self._unarmed_subscription_ids.discard(subscription["id"])
            if key in self._observers:
                return True

            # Observer.schedule fails if the path doesn't exist or isn't a dir.
            if not os.path.isdir(root_path):
                self._unarmed_subscription_ids.add(subscription["id"])
                logger.warning(
                    f"FileWatcherManager: cannot arm watcher for {root_path!r} — "
                    f"path is not a directory; subscription "
                    f"{subscription['id']} marked unarmed"
                )
                return False

            handler = _SharedHandler(
                profile=profile,
                # Pass the verbatim DB path so list_by_root matches exactly.
                # The dedupe key (above) is the normcased form.
                root_path=root_path,
                recursive=recursive,
                loop=self._loop,  # type: ignore[arg-type]
            )
            observer = Observer()
            observer.daemon = True
            try:
                observer.schedule(handler, root_path, recursive=recursive)
                observer.start()
            except (OSError, FileNotFoundError) as exc:
                self._unarmed_subscription_ids.add(subscription["id"])
                logger.warning(
                    f"FileWatcherManager: failed to start observer for "
                    f"{root_path!r}: {exc}"
                )
                return False
            self._observers[key] = observer
            self._handlers[key] = handler

        logger.info(
            f"FileWatcherManager: watching {root_path} "
            f"(profile={profile} recursive={recursive})"
        )
        return True


_instance: Optional[FileWatcherManager] = None


def get_file_watcher_manager() -> FileWatcherManager:
    global _instance
    if _instance is None:
        _instance = FileWatcherManager()
    return _instance
