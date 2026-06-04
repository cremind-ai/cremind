"""Filesystem watchdog manager for skill event subscriptions.

A single watchdog ``Observer`` is mounted per (profile, source_dir, event_type)
folder. When a new ``*.md`` file is created inside, the manager reads it and
fans out one queue enqueue per subscriber conversation.

Watchdog callbacks run on a watchdog thread; we bridge into the asyncio loop
via :func:`asyncio.AbstractEventLoop.call_soon_threadsafe` so the queue and
runner stay on the loop they were created on.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.events import queue as event_queue
from app.storage import get_event_subscription_storage
from app.utils.logger import logger


_WatcherKey = Tuple[str, str, str]  # (profile, source_dir_str, event_type)


class _EventFileHandler(FileSystemEventHandler):
    def __init__(
        self,
        *,
        profile: str,
        skill_name: str,
        event_type: str,
        events_dir: Path,
        loop: asyncio.AbstractEventLoop,
    ):
        super().__init__()
        self._profile = profile
        self._skill_name = skill_name
        self._event_type = event_type
        self._events_dir = events_dir
        self._loop = loop

    def _is_relevant(self, event: FileSystemEvent) -> bool:
        if event.is_directory:
            return False
        path = Path(str(event.src_path))
        if path.suffix.lower() != ".md":
            return False
        # Only files directly inside the watched dir, not nested.
        try:
            return path.parent.resolve() == self._events_dir.resolve()
        except OSError:
            return False

    def on_created(self, event: FileSystemEvent) -> None:
        if not self._is_relevant(event):
            return
        path = Path(str(event.src_path))
        # Defensive read: the listener may still be writing the file when we
        # see CREATE on Windows. Tiny sleep + retry-once is good enough.
        content = self._read_with_retry(path)
        if content is None:
            return
        logger.info(
            f"EventManager: new {self._event_type} event for skill "
            f"'{self._skill_name}' (profile={self._profile}) at {path.name}"
        )
        # Consume the file: each event is single-use. Delete BEFORE fan-out
        # so a crash during dispatch never leaves the same file to fire on
        # the next boot's replay (watchdog only sees CREATE, never replay).
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception(
                f"EventManager: failed to delete event file {path}"
            )
        try:
            asyncio.run_coroutine_threadsafe(
                self._fan_out(content), self._loop,
            )
        except Exception:  # noqa: BLE001
            logger.exception("EventManager: failed to schedule fan-out")

    def _read_with_retry(self, path: Path) -> Optional[str]:
        for delay in (0.0, 0.05, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except OSError:
                continue
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            logger.warning(f"EventManager: could not read event file {path}")
            return None

    async def _fan_out(self, content: str) -> None:
        store = get_event_subscription_storage()
        try:
            subs = store.list_by_event(
                profile=self._profile,
                skill_name=self._skill_name,
                event_type=self._event_type,
            )
        except Exception:  # noqa: BLE001
            logger.exception("EventManager: subscription lookup failed")
            return
        for sub in subs:
            try:
                await event_queue.enqueue(
                    conversation_id=sub["conversation_id"],
                    profile=sub["profile"],
                    skill_name=sub["skill_name"],
                    event_type=sub["event_type"],
                    action=sub["action"],
                    file_content=content,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"EventManager: enqueue failed for conversation "
                    f"{sub['conversation_id']}"
                )


class EventManager:
    """Owns one Observer per watched events folder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observers: Dict[_WatcherKey, Observer] = {}
        self._handlers: Dict[_WatcherKey, _EventFileHandler] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Cache of (profile, skill_name) → source_dir to avoid repeated DB lookups.
        self._source_dir_cache: Dict[Tuple[str, str], str] = {}

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the asyncio loop and arm watchers for every existing subscription."""
        self._loop = loop
        try:
            for row in get_event_subscription_storage().distinct_event_folders():
                source_dir = self._resolve_source_dir(
                    row["profile"], row["skill_name"],
                )
                if not source_dir:
                    logger.warning(
                        f"EventManager: cannot resolve skill source for "
                        f"profile={row['profile']} skill_name={row['skill_name']}; "
                        f"skipping watcher"
                    )
                    continue
                self.ensure_watcher(
                    profile=row["profile"],
                    skill_name=row["skill_name"],
                    source_dir=source_dir,
                    event_type=row["event_type"],
                )
        except Exception:  # noqa: BLE001
            logger.exception("EventManager: failed to replay subscriptions on start")

    def stop(self) -> None:
        with self._lock:
            for obs in self._observers.values():
                try:
                    obs.stop()
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[events] observer stop raised: {e}", exc_info=True)
            for obs in self._observers.values():
                try:
                    obs.join(timeout=2)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[events] observer join raised: {e}", exc_info=True)
            self._observers.clear()
            self._handlers.clear()

    # ── public API ─────────────────────────────────────────────────────

    def ensure_watcher(
        self,
        *,
        profile: str,
        skill_name: str,
        source_dir: str,
        event_type: str,
    ) -> Path:
        """Ensure a watcher is running for this (profile, skill, event_type) folder.

        Idempotent. Creates the events dir if it doesn't exist. Returns the
        watched directory path.
        """
        if self._loop is None:
            raise RuntimeError("EventManager.start() must be called before ensure_watcher")
        events_dir = Path(source_dir) / "events" / event_type
        events_dir.mkdir(parents=True, exist_ok=True)
        key: _WatcherKey = (profile, str(events_dir), event_type)
        with self._lock:
            if key in self._observers:
                return events_dir
            handler = _EventFileHandler(
                profile=profile,
                skill_name=skill_name,
                event_type=event_type,
                events_dir=events_dir,
                loop=self._loop,
            )
            observer = Observer()
            observer.daemon = True
            observer.schedule(handler, str(events_dir), recursive=False)
            observer.start()
            self._observers[key] = observer
            self._handlers[key] = handler
            self._source_dir_cache[(profile, skill_name)] = source_dir
        logger.info(
            f"EventManager: watching {events_dir} for skill '{skill_name}' "
            f"(event_type={event_type}, profile={profile})"
        )
        return events_dir

    def release_watcher(
        self,
        *,
        profile: str,
        skill_name: str,
        event_type: str,
    ) -> None:
        """Tear down the watcher if no subscriptions remain for that folder."""
        try:
            remaining = get_event_subscription_storage().list_by_event(
                profile=profile,
                skill_name=skill_name,
                event_type=event_type,
            )
        except Exception:  # noqa: BLE001
            logger.exception("EventManager.release_watcher: subscription lookup failed")
            return
        if remaining:
            return
        source_dir = self._source_dir_cache.get((profile, skill_name)) \
            or self._resolve_source_dir(profile, skill_name)
        if not source_dir:
            return
        events_dir = Path(source_dir) / "events" / event_type
        key: _WatcherKey = (profile, str(events_dir), event_type)
        with self._lock:
            observer = self._observers.pop(key, None)
            self._handlers.pop(key, None)
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=2)
        except Exception:  # noqa: BLE001
            logger.exception("EventManager: error stopping observer")
        logger.info(
            f"EventManager: stopped watching {events_dir} (no subscribers remain)"
        )

    # ── helpers ────────────────────────────────────────────────────────

    def _resolve_source_dir(self, profile: str, skill_name: str) -> Optional[str]:
        """Look up the on-disk skill directory for (profile, skill_name).

        Skill tool_ids are stored as ``<profile>__<slug>`` (see
        :mod:`app.tools.registry`); fall back to bare slug / verbatim input
        for resilience.
        """
        from app.tools.builtin.register_skill_event import _resolve_skill_source

        return _resolve_skill_source(skill_name, profile)


_instance: Optional[EventManager] = None


def get_event_manager() -> EventManager:
    global _instance
    if _instance is None:
        _instance = EventManager()
    return _instance
