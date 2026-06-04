"""Filesystem watcher for the agent skills directory.

Uses the ``watchdog`` library to monitor ``{CREMIND_SYSTEM_DIR}/skills/``
for directory and file changes.  When a relevant change is detected (new
skill added, SKILL.md modified, skill directory deleted) it debounces
for a short window and then re-scans the entire directory, invoking the
caller-supplied ``on_change`` callback with the fresh skills dict.
"""

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.skills.scanner import SkillInfo, scan_skills
from app.utils.logger import logger

# Debounce window in seconds — multiple rapid filesystem events are
# collapsed into a single re-scan.
_DEBOUNCE_SECONDS = 1.0


class _SkillEventHandler(FileSystemEventHandler):
    """Handler that triggers a debounced re-scan on relevant events."""

    def __init__(self, skills_dir: Path, on_change: Callable[[dict[str, SkillInfo]], None]):
        super().__init__()
        self._skills_dir = skills_dir
        self._on_change = on_change
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _schedule_rescan(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(_DEBOUNCE_SECONDS, self._do_rescan)
            self._timer.daemon = True
            self._timer.start()

    def _do_rescan(self) -> None:
        try:
            skills = scan_skills(self._skills_dir)
            logger.info(f"Skills watcher re-scan complete: {len(skills)} skill(s) found")
            self._on_change(skills)
        except Exception:
            logger.exception("Error during skills re-scan")

    def _is_relevant(self, event: FileSystemEvent) -> bool:
        """Only react to SKILL.md files or directory-level changes."""
        src = Path(str(event.src_path))
        if event.is_directory:
            # Directory created/deleted directly under skills_dir
            return src.parent == self._skills_dir
        # File event — only care about SKILL.md
        return src.name.lower() == "skill.md"

    def on_created(self, event: FileSystemEvent) -> None:
        if self._is_relevant(event):
            logger.debug(f"Skills watcher: created {event.src_path}")
            self._schedule_rescan()

    def on_modified(self, event: FileSystemEvent) -> None:
        if self._is_relevant(event):
            logger.debug(f"Skills watcher: modified {event.src_path}")
            self._schedule_rescan()

    def on_deleted(self, event: FileSystemEvent) -> None:
        if self._is_relevant(event):
            logger.debug(f"Skills watcher: deleted {event.src_path}")
            self._schedule_rescan()

    def on_moved(self, event: FileSystemEvent) -> None:
        if self._is_relevant(event):
            logger.debug(f"Skills watcher: moved {event.src_path}")
            self._schedule_rescan()

    def cancel_pending(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


class SkillsWatcher:
    """Watch the skills directory for changes and invoke a callback."""

    def __init__(self, skills_dir: Path, on_change: Callable[[dict[str, SkillInfo]], None]):
        self._skills_dir = skills_dir
        self._handler = _SkillEventHandler(skills_dir, on_change)
        self._observer = Observer()
        self._observer.daemon = True

    def start(self) -> None:
        self._observer.schedule(self._handler, str(self._skills_dir), recursive=True)
        self._observer.start()
        logger.info(f"Skills watcher started on {self._skills_dir}")

    def stop(self) -> None:
        self._handler.cancel_pending()
        self._observer.stop()
        self._observer.join(timeout=5)
        logger.info("Skills watcher stopped")
