"""Filesystem watchdog manager for skill event folders.

Cremind always monitors **every** event-listener skill's ``events/`` tree, not
just the folders a conversation happens to be subscribed to. A skill's listener
daemon writes incoming events as ``*.md`` files into
``<skill_dir>/events/<event_type>/`` regardless of whether anyone is
subscribed, so without blanket monitoring those files accumulate as junk and
pollute the folder a real subscription later reads from.

A single shared watchdog ``Observer`` mounts **one recursive watch per profile**
rooted at that profile's skills directory (``profile_skills_dir(profile)``).
When a new ``*.md`` file appears anywhere under ``<skill>/events/`` the handler:

* deletes the file immediately (events are single-use — never replayed), and
* if the path is a well-formed ``<skill>/events/<event_type>/<file>.md`` AND a
  subscription exists for that ``(profile, skill, event_type)``, fans out one
  queue enqueue per subscriber conversation.

Rooting the watch at the profile skills dir (rather than per skill or per
event-type) means skill add / delete / reset / hot-reload need **no** watcher
re-arming: the recursive watch already covers new subfolders, and its OS
directory handle sits on the skills *root* — never on the ``<skill>/`` dir that
a delete/reset removes — so it never blocks ``_robust_rmtree`` on Windows.

Watchdog callbacks run on a watchdog thread; we bridge into the asyncio loop via
:func:`asyncio.run_coroutine_threadsafe` so the queue and runner stay on the
loop they were created on. The handler reads the immutable per-profile skill
index without taking the manager lock.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch

from app.events import queue as event_queue
from app.storage import get_event_subscription_storage
from app.utils.logger import logger


def _declares_events(metadata: object) -> bool:
    """True if a skill's parsed SKILL.md ``metadata`` declares any events."""
    if not isinstance(metadata, dict):
        return False
    events = metadata.get("events")
    if not isinstance(events, dict):
        return False
    items = events.get("event_type")
    return isinstance(items, list) and len(items) > 0


class _ProfileEventHandler(FileSystemEventHandler):
    """Handles event-file creation anywhere under one profile's skills tree."""

    def __init__(
        self,
        *,
        profile: str,
        skills_root: Path,
        manager: "EventManager",
        loop: asyncio.AbstractEventLoop,
    ):
        super().__init__()
        self._profile = profile
        self._skills_root = skills_root
        self._manager = manager
        self._loop = loop

    def on_created(self, event: FileSystemEvent) -> None:
        # Cheap rejects first — this handler fires for *every* file touched
        # anywhere under the skills root (listeners frequently rewrite
        # scripts/.listener_heartbeat, .listener.lock, .env, …). A single
        # endswith discards all of that before any Path work.
        if event.is_directory:
            return
        src = str(event.src_path)
        if not src.endswith(".md"):
            return
        try:
            rel = Path(src).relative_to(self._skills_root)
        except ValueError:
            return
        parts = rel.parts
        # Only files under some skill's events/ tree:
        #   parts == (<skill_dir>, "events", <event_type>, <file>.md)  → process
        #   parts == (<skill_dir>, "events", <file>.md) or deeper       → stray
        if len(parts) < 3 or parts[1] != "events":
            return  # e.g. <skill>/SKILL.md, <skill>/scripts/...

        path = Path(src)
        content = self._read_with_retry(path)
        # Consume the file: each event is single-use. Delete BEFORE dispatch so
        # a crash during fan-out never leaves the file to fire on the next
        # boot's sweep (watchdog only sees CREATE, never a replay).
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception(f"EventManager: failed to delete event file {path}")

        # Only well-formed <skill>/events/<event_type>/<file>.md is dispatched.
        # Strays (wrong depth) were just deleted — that satisfies "delete junk"
        # without ever enqueueing a garbage event_type.
        if len(parts) != 4 or content is None:
            return
        skill_dir, event_type = parts[0], parts[2]
        logger.info(
            f"EventManager: new {event_type} event for skill dir "
            f"'{skill_dir}' (profile={self._profile}) at {path.name}"
        )
        try:
            asyncio.run_coroutine_threadsafe(
                self._dispatch(skill_dir, event_type, content), self._loop,
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

    async def _dispatch(self, skill_dir: str, event_type: str, content: str) -> None:
        skill_name = self._manager.resolve_skill_name(self._profile, skill_dir)
        store = get_event_subscription_storage()
        try:
            subs = store.list_by_event(
                profile=self._profile,
                skill_name=skill_name,
                event_type=event_type,
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
    """Owns a single shared Observer with one recursive watch per profile."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observer: Optional[Observer] = None
        self._watches: Dict[str, ObservedWatch] = {}  # profile → watch handle
        # profile → {skill_dir_name → canonical skill tool_id}. The handler
        # reads this (lock-free) to map a path back to the subscription key.
        self._skill_index: Dict[str, Dict[str, str]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the asyncio loop and start the single shared Observer.

        Watches are armed per profile via :meth:`watch_profile`, called from
        server boot (after skills are synced) and on profile/skill changes.
        """
        self._loop = loop
        with self._lock:
            if self._observer is None:
                observer = Observer()
                observer.daemon = True
                observer.start()
                self._observer = observer

    def stop(self) -> None:
        with self._lock:
            observer = self._observer
            self._observer = None
            self._watches.clear()
            self._skill_index.clear()
        if observer is None:
            return
        try:
            observer.stop()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[events] observer stop raised: {e}", exc_info=True)
        try:
            observer.join(timeout=2)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[events] observer join raised: {e}", exc_info=True)

    # ── public API ─────────────────────────────────────────────────────

    def watch_profile(self, profile: str, registry) -> None:
        """Arm (or refresh) monitoring for a profile's skills tree.

        Idempotent. Always rebuilds the profile's skill index from the registry
        so fan-out can resolve a path back to the canonical subscription key.
        Schedules the recursive watch (and runs a one-time backlog sweep) the
        first time it's called with the loop running — at boot ``start()`` runs
        *after* the first ``initialize_profile_skills`` pass, so that early call
        only refreshes the index and server boot arms the watch explicitly.
        """
        if not profile:
            return
        # Rebuild the index outside the lock, then swap the reference atomically
        # so the handler thread always sees a consistent dict.
        index: Dict[str, str] = {}
        try:
            for tool in registry.owned_skills(profile):
                info = getattr(tool, "info", None)
                dir_path = getattr(info, "dir_path", None)
                if dir_path is None:
                    continue
                index[Path(dir_path).name] = tool.tool_id
        except Exception:  # noqa: BLE001
            logger.exception(
                f"EventManager: failed to build skill index for '{profile}'"
            )
        self._skill_index[profile] = index

        if self._loop is None or self._observer is None:
            return  # boot pre-start; server boot will arm the watch
        with self._lock:
            if profile in self._watches:
                return
            from app.skills.sync import profile_skills_dir

            skills_root = profile_skills_dir(profile)
            try:
                skills_root.mkdir(parents=True, exist_ok=True)
                handler = _ProfileEventHandler(
                    profile=profile,
                    skills_root=skills_root,
                    manager=self,
                    loop=self._loop,
                )
                watch = self._observer.schedule(
                    handler, str(skills_root), recursive=True,
                )
                self._watches[profile] = watch
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"EventManager: failed to schedule watch for '{profile}'"
                )
                return
        logger.info(
            f"EventManager: watching {skills_root} (profile={profile})"
        )
        self._sweep_backlog(profile, registry)

    def resolve_skill_name(self, profile: str, skill_dir: str) -> str:
        """Map a profile's on-disk skill dir name to its canonical tool_id.

        Falls back to ``<profile>__<slug(dir)>`` — correct for every built-in,
        whose on-disk dir name equals its SKILL.md name equals its slug.
        """
        name = self._skill_index.get(profile, {}).get(skill_dir)
        if name:
            return name
        from app.tools.ids import slugify

        return f"{profile}__{slugify(skill_dir)}"

    # ── helpers ────────────────────────────────────────────────────────

    def _sweep_backlog(self, profile: str, registry) -> None:
        """Delete pre-existing event ``*.md`` files for a profile (once, on arm).

        Delete-only: anything sitting in an ``events/`` tree at arm time was not
        processed in real time, so per the single-use/no-replay policy it's
        dropped. The recursive glob also clears depth-0 strays and nested junk.
        """
        deleted = 0
        try:
            skills = registry.owned_skills(profile)
        except Exception:  # noqa: BLE001
            logger.exception(
                f"EventManager: backlog sweep enumeration failed for '{profile}'"
            )
            return
        for tool in skills:
            info = getattr(tool, "info", None)
            if info is None or not _declares_events(getattr(info, "metadata", None)):
                continue
            events_dir = Path(info.dir_path) / "events"
            if not events_dir.is_dir():
                continue
            for md in events_dir.rglob("*.md"):
                try:
                    md.unlink()
                    deleted += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.debug(
                        f"EventManager: backlog sweep could not delete {md}",
                        exc_info=True,
                    )
        if deleted:
            logger.info(
                f"EventManager: swept {deleted} stale event file(s) for "
                f"profile '{profile}'"
            )


def wipe_event_folders_on_startup(profiles) -> int:
    """Delete every skill event ``*.md`` file for *profiles* at boot.

    A clean-slate sweep run once during startup — **before** listener
    subprocesses are spawned — so operation begins with no stale event files
    left over from a previous run. Delete-only: anything present at boot
    predates monitoring and is dropped (events are single-use, never replayed).
    The recursive glob also clears depth-0 strays and nested junk. Returns the
    number of files removed.
    """
    from app.skills.sync import profile_skills_dir

    deleted = 0
    for profile in profiles or ():
        skills_root = profile_skills_dir(profile)
        if not skills_root.is_dir():
            continue
        for events_dir in skills_root.glob("*/events"):
            if not events_dir.is_dir():
                continue
            for md in events_dir.rglob("*.md"):
                try:
                    md.unlink()
                    deleted += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.debug(
                        f"EventManager: startup wipe could not delete {md}",
                        exc_info=True,
                    )
    if deleted:
        logger.info(
            f"EventManager: startup wipe removed {deleted} stale event file(s)"
        )
    return deleted


_instance: Optional[EventManager] = None


def get_event_manager() -> EventManager:
    global _instance
    if _instance is None:
        _instance = EventManager()
    return _instance
