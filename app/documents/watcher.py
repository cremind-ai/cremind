"""Watch a documents directory and forward `.md` events to the sync service.

Modeled on :class:`app.skills.watcher.SkillsWatcher`. One :class:`DocumentWatcher`
runs per scope (the shared root and each profile's ``documents`` directory).
Events are debounced -- multiple rapid filesystem events on the same path
collapse into a single ``apply_event`` call -- and only ``.md`` files are
relevant.
"""

from __future__ import annotations

import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.documents.sync import DocumentSyncService
from app.utils.logger import logger

_DEBOUNCE_SECONDS = 1.0


class _MdEventHandler(FileSystemEventHandler):
    def __init__(self, scope: str, sync_service: DocumentSyncService):
        super().__init__()
        self._scope = scope
        self._sync = sync_service
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _is_relevant(self, path_str: str, is_directory: bool) -> bool:
        if is_directory:
            return False
        return path_str.lower().endswith(".md")

    def _schedule(self, path_str: str) -> None:
        with self._lock:
            existing = self._timers.pop(path_str, None)
            if existing is not None:
                existing.cancel()

            def _fire() -> None:
                with self._lock:
                    self._timers.pop(path_str, None)
                try:
                    self._sync.apply_event(self._scope, Path(path_str))
                except Exception:  # noqa: BLE001
                    logger.exception(
                        f"[documents] apply_event failed for {self._scope}/{path_str}"
                    )

            timer = threading.Timer(_DEBOUNCE_SECONDS, _fire)
            timer.daemon = True
            timer.start()
            self._timers[path_str] = timer

    def on_created(self, event: FileSystemEvent) -> None:
        if self._is_relevant(str(event.src_path), event.is_directory):
            self._schedule(str(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if self._is_relevant(str(event.src_path), event.is_directory):
            self._schedule(str(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if self._is_relevant(str(event.src_path), event.is_directory):
            self._schedule(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        # ``moved`` fires when a file is renamed; the old path becomes a
        # delete and the new path becomes a create. Re-route both.
        if self._is_relevant(str(event.src_path), event.is_directory):
            self._schedule(str(event.src_path))
        dest = getattr(event, "dest_path", None)
        if dest and self._is_relevant(str(dest), event.is_directory):
            self._schedule(str(dest))

    def cancel_pending(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()


class DocumentWatcher:
    """Watch one scope's documents directory and forward events."""

    def __init__(self, scope: str, directory: Path, sync_service: DocumentSyncService):
        self._scope = scope
        self._directory = directory
        self._handler = _MdEventHandler(scope, sync_service)
        self._observer = Observer()
        self._observer.daemon = True

    def start(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        self._observer.schedule(self._handler, str(self._directory), recursive=True)
        self._observer.start()
        logger.info(
            f"[documents] watcher started for scope={self._scope!r} at {self._directory}"
        )

    def stop(self) -> None:
        self._handler.cancel_pending()
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception:  # noqa: BLE001
            logger.exception(f"[documents] watcher stop failed (scope={self._scope!r})")
        logger.info(f"[documents] watcher stopped for scope={self._scope!r}")
