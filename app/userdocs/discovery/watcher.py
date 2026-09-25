"""Native change notifications for one local root, turned into settled path batches.

Raw filesystem events are a poor work queue. Saving one Word document produces
a temp file, a delete, a rename and several modify events; copying a 2 GB video
produces a modify event every few milliseconds for a minute; a program holding a
file open for writing makes it unreadable (Windows) or half-written. So events
pass through three stages before anyone indexes anything:

1. **Coalesce** — one pending entry per path, the latest event wins (a
   ``remove`` followed by a ``change`` is a change; a move is a remove of the
   source and a change of the destination). Paths the matcher prunes or skips
   never enter.
2. **Debounce** — nothing is looked at until ``debounce_s`` passes without a
   new event for that path.
3. **Stability check** — a changed file is ``stat``-ed, then again
   ``stability_s`` later; if its size or mtime moved, or it cannot be opened
   for reading (another program holds it), it is rechecked with backoff
   (2, 4, 8 … 60 s). After ``MAX_ATTEMPTS`` rechecks it is delivered anyway —
   the extractor then fails and retries on its own schedule, and the periodic
   scan covers anything a watcher never settled.

Only then does ``on_paths(changed, removed)`` run, from the watcher's own
thread, with root-relative POSIX paths:

- ``"docs/a.pdf"`` — a file; ``"docs/old/"`` — a directory (created, moved
  in, or removed/moved out — rescan or drop that subtree); ``"/"`` — rescan
  the whole root (a ``.cremindignore`` at the root changed, or the pending
  set overflowed).
- A removed path *without* a trailing slash may still have been a directory:
  Windows reports every deletion as a file deletion because the path no longer
  exists to ask. Callers treat a removed path as "that file, and anything
  under that prefix".

``on_paths`` should be quick (enqueue work, mark rows dirty); it runs on the
thread that settles the next batch.

The watcher is only an accelerator. Anything it misses — events dropped by the
OS, changes while stopped, pending batches discarded by :meth:`stop` at
shutdown — the next scan-diff finds. That is also why :meth:`start` raises
rather than limping on: a watch that cannot be placed (inotify's
``max_user_watches`` exhausted) must send the caller to polling.
"""

from __future__ import annotations

import errno
import os
import stat as stat_mod
import threading
import time
from typing import Any, Callable

from app.userdocs.discovery.hashing import fs_path, is_placeholder
from app.userdocs.discovery.ignore import IGNORE_FILE_NAME, SKIP, IgnoreMatcher, fold_case, norm_rel
from app.utils.logger import logger

OnPaths = Callable[[set[str], set[str]], None]

ROOT_RESCAN = "/"

_CHANGE = "change"
_REMOVE = "remove"

# The debouncer's own outcomes for one pending entry.
_DELIVER_CHANGE = "deliver_change"
_DELIVER_REMOVE = "deliver_remove"
_RECHECK = "recheck"
_BECOME_CHANGE = "become_change"
_DROP = "drop"

# A copy of 500k files must not grow memory without bound: past this many
# pending paths the watcher gives up on detail and asks for a full rescan.
_MAX_PENDING = 100_000
# Settle at most this many paths per pass (each costs a stat or two), and hand
# them over in batches of at most this many paths per list.
_MAX_PER_TICK = 5_000
_BATCH = 1_000
# Remembered delivered signatures, so a burst of duplicate modify events (or a
# metadata-only touch) does not re-deliver an unchanged file.
_DELIVERED_MEMORY = 20_000


class WatcherStartError(RuntimeError):
    """The native observer could not be started; fall back to polling.

    ``reason``: ``inotify_limit`` (ENOSPC/EMFILE from inotify — raise
    ``fs.inotify.max_user_watches``/``max_user_instances``) or ``unavailable``
    (anything else: root missing, unsupported filesystem, no watchdog).
    """

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


class _Pending:
    __slots__ = ("rel", "abs_path", "kind", "is_dir", "due", "seq", "phase", "sig", "attempts")

    def __init__(self, rel: str, abs_path: str):
        self.rel = rel
        self.abs_path = abs_path
        self.kind = _CHANGE
        self.is_dir = False
        self.due = 0.0
        self.seq = 0
        self.phase = 0        # 0: debounce elapsed, first stat next; 1: stability check
        self.sig: tuple[int, int] | None = None
        self.attempts = 0


class _Handler:
    """What watchdog calls. Not a ``FileSystemEventHandler`` subclass — the
    observer only needs ``dispatch`` — so watchdog stays a lazy import."""

    def __init__(self, watcher: "SourceWatcher"):
        self._watcher = watcher

    def dispatch(self, event: Any) -> None:
        try:
            self._watcher._on_event(event)
        except Exception as exc:  # noqa: BLE001 — an exception here kills watchdog's thread
            logger.warning(f"[userdocs] watcher: dropped an event after an error: {exc}")


def _to_str(p: Any) -> str:
    return os.fsdecode(p) if isinstance(p, bytes) else str(p)


def _stop_observer(observer: Any) -> None:
    try:
        observer.stop()
        observer.join()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[userdocs] watcher: observer stop: {exc}")


class SourceWatcher:
    BACKOFF_S: tuple[float, ...] = (2, 4, 8, 16, 32, 60)
    MAX_ATTEMPTS = 10

    def __init__(
        self,
        root_abs: str,
        matcher: IgnoreMatcher,
        on_paths: OnPaths,
        *,
        debounce_s: float = 2.0,
        stability_s: float = 1.0,
    ):
        self.root = os.path.abspath(root_abs)
        self.matcher = matcher
        self.on_paths = on_paths
        self.debounce_s = max(0.0, float(debounce_s))
        self.stability_s = max(0.0, float(stability_s))
        # Wall-clock time of the last raw event (0.0: none yet), for the
        # caller's "is the watcher still hearing anything" liveness check.
        self.last_event_at = 0.0

        # macOS reports /private/var/... for /var/..., so match either spelling.
        self._prefixes = sorted(
            {os.path.normcase(os.path.normpath(p)) for p in (self.root, os.path.realpath(self.root))},
            key=len, reverse=True,
        )
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._delivered: dict[str, tuple[int, int]] = {}
        self._overflow = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state_lock = threading.Lock()
        self._observer: Any = None
        self._thread: threading.Thread | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start watching. Idempotent. Raises :class:`WatcherStartError`."""
        with self._state_lock:
            if self._observer is not None:
                return
            try:
                from watchdog.observers import Observer
            except ImportError as exc:
                raise WatcherStartError("watchdog is not installed", "unavailable") from exc
            # A short queue timeout so the observer thread notices stop quickly.
            observer = Observer(timeout=0.25)
            try:
                observer.schedule(_Handler(self), self.root, recursive=True)
                observer.start()
            except Exception as exc:  # noqa: BLE001 — every failure means "poll instead"
                threading.Thread(target=_stop_observer, args=(observer,), daemon=True).start()
                code = getattr(exc, "errno", None)
                if code in (errno.ENOSPC, errno.EMFILE):
                    raise WatcherStartError(
                        f"inotify limit reached while watching {self.root}: {exc}", "inotify_limit",
                    ) from exc
                raise WatcherStartError(f"cannot watch {self.root}: {exc}", "unavailable") from exc
            self._stop.clear()
            self._wake.clear()
            thread = threading.Thread(target=self._run, name="userdocs-watch-settle", daemon=True)
            thread.start()
            self._observer, self._thread = observer, thread
        logger.debug(f"[userdocs] watcher started on {self.root}")

    def stop(self, timeout: float = 0.5) -> None:
        """Stop watching; returns within about ``timeout`` seconds. Idempotent.

        Pending, unsettled paths are discarded — the next scan finds them. The
        observer is stopped from a helper thread because watchdog joins its
        emitter threads without a timeout, and shutdown has a hard budget.
        """
        with self._state_lock:
            observer, thread = self._observer, self._thread
            self._observer = self._thread = None
        if observer is None and thread is None:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        self._stop.set()
        self._wake.set()
        if observer is not None:
            stopper = threading.Thread(target=_stop_observer, args=(observer,), daemon=True)
            stopper.start()
            stopper.join(max(0.0, deadline - time.monotonic()))
        if thread is not None:
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._pending.clear()
            self._overflow = False

    def is_alive(self) -> bool:
        """Running and every watchdog thread still up (an inotify emitter dies
        when the root is deleted or a new subfolder cannot get a watch)."""
        observer, thread = self._observer, self._thread
        if observer is None or thread is None or not thread.is_alive() or not observer.is_alive():
            return False
        try:
            return all(e.is_alive() for e in observer.emitters)
        except Exception:  # noqa: BLE001
            return True

    # ── event intake (watchdog's thread) ───────────────────────────────────

    def _on_event(self, event: Any) -> None:
        self.last_event_at = time.time()
        etype = event.event_type
        # Our own stability check opens files: inotify reports that as
        # opened/closed_no_write, which must not look like a change.
        if etype in ("opened", "closed_no_write"):
            return
        # A directory's mtime moves whenever an entry in it does; the entry
        # reports itself.
        if etype == "modified" and event.is_directory:
            return
        is_dir = bool(event.is_directory)
        if etype == "moved":
            self._note(_to_str(event.src_path), _REMOVE, is_dir)
            self._note(_to_str(event.dest_path), _CHANGE, is_dir)
        elif etype == "deleted":
            self._note(_to_str(event.src_path), _REMOVE, is_dir)
        else:  # created, modified, closed (after a write)
            self._note(_to_str(event.src_path), _CHANGE, is_dir)

    def _rel_of(self, abs_path: str) -> str | None:
        p = os.path.normpath(abs_path)
        np_ = os.path.normcase(p)
        for prefix in self._prefixes:
            if np_.startswith(prefix + os.sep):
                return norm_rel(p[len(prefix) + 1:].replace(os.sep, "/")) or None
        return None  # the root itself, or outside it

    def _note(self, abs_path: str, kind: str, is_dir: bool) -> None:
        rel = self._rel_of(abs_path)
        if rel is None:
            return
        parent, _, name = rel.rpartition("/")
        if name.lower() == IGNORE_FILE_NAME:
            # The rules for a whole subtree changed: forget cached decisions
            # and ask for that subtree to be rescanned.
            self.matcher.invalidate()
            parent_abs = os.path.dirname(abs_path)
            if parent and self.matcher.classify(parent, parent_abs, is_dir=True) == SKIP:
                return  # inside a pruned tree: its rules cannot matter
            self._queue(parent, parent_abs, _CHANGE, True)
            return
        bundle = self.matcher.bundle_root(rel)
        if bundle is not None:
            # Anything inside a bundle changes the bundle's single entry.
            self._queue(bundle, os.path.join(self.root, *bundle.split("/")), _CHANGE, False)
            return
        if self.matcher.classify(rel, abs_path, is_dir=is_dir) == SKIP:
            return
        if is_dir and self.matcher.is_bundle(name):
            is_dir = False  # a bundle is indexed as one file-like entry
        self._queue(rel, abs_path, kind, is_dir)

    def _queue(self, rel: str, abs_path: str, kind: str, is_dir: bool) -> None:
        key = fold_case(rel) + ("/" if is_dir else "")
        due = time.monotonic() + self.debounce_s
        with self._lock:
            p = self._pending.get(key)
            if p is None:
                if len(self._pending) >= _MAX_PENDING:
                    self._overflow = True
                    return
                p = self._pending[key] = _Pending(rel, abs_path)
            p.rel, p.abs_path, p.kind, p.is_dir = rel, abs_path, kind, is_dir
            p.due = due
            p.seq += 1
            p.phase = 0
            p.sig = None

    # ── settling (the watcher's own thread) ────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                wait_s = self._tick()
            except Exception as exc:  # noqa: BLE001 — never let the settle loop die
                logger.warning(f"[userdocs] watcher: settle pass failed: {exc}")
                wait_s = 1.0
            if self._stop.is_set():
                break
            # New events are always at least ``debounce_s`` from due, so
            # sleeping no longer than that never makes one late; stop() sets
            # ``_wake`` to cut the sleep short.
            self._wake.wait(max(0.02, min(wait_s, max(self.debounce_s, 0.05))))
            self._wake.clear()

    def _tick(self) -> float:
        now = time.monotonic()
        ready: list[tuple[str, int, str, str, str, bool, int, tuple[int, int] | None, int]] = []
        next_due = float("inf")
        with self._lock:
            overflow, self._overflow = self._overflow, False
            for key, p in self._pending.items():
                if p.due <= now and len(ready) < _MAX_PER_TICK:
                    ready.append((key, p.seq, p.rel, p.abs_path, p.kind, p.is_dir, p.phase, p.sig, p.attempts))
                elif p.due < next_due:
                    next_due = p.due

        changed: set[str] = {ROOT_RESCAN} if overflow else set()
        removed: set[str] = set()
        for key, seq, rel, abs_path, kind, is_dir, phase, sig, attempts in ready:
            if self._stop.is_set():
                return 0.0
            outcome, out_path, new_sig = self._examine(key, rel, abs_path, kind, is_dir, phase, sig, attempts)
            with self._lock:
                cur = self._pending.get(key)
                if cur is None or cur.seq != seq:
                    continue  # a newer event arrived meanwhile; it owns the entry now
                if outcome == _BECOME_CHANGE:
                    cur.kind, cur.phase, cur.sig, cur.due = _CHANGE, 0, None, time.monotonic()
                    next_due = min(next_due, cur.due)
                    continue
                if outcome == _RECHECK:
                    if phase == 1:
                        cur.attempts += 1
                        delay = self.BACKOFF_S[min(attempts, len(self.BACKOFF_S) - 1)]
                    else:
                        delay = self.stability_s
                    cur.phase, cur.sig, cur.due = 1, new_sig, time.monotonic() + delay
                    next_due = min(next_due, cur.due)
                    continue
                del self._pending[key]
                if outcome == _DELIVER_CHANGE:
                    changed.add(out_path)
                    if new_sig is not None:
                        self._remember(key, new_sig)
                elif outcome == _DELIVER_REMOVE:
                    removed.add(out_path)
                    self._delivered.pop(key, None)
        if changed or removed:
            self._deliver(changed, removed)
        return next_due - time.monotonic()

    def _remember(self, key: str, sig: tuple[int, int]) -> None:
        if len(self._delivered) >= _DELIVERED_MEMORY:
            self._delivered.clear()
        self._delivered[key] = sig

    def _examine(
        self, key: str, rel: str, abs_path: str, kind: str, is_dir: bool,
        phase: int, sig: tuple[int, int] | None, attempts: int,
    ) -> tuple[str, str, tuple[int, int] | None]:
        """Decide one due entry: ``(outcome, delivered path, signature)``."""
        dir_path = (rel + "/") if rel else ROOT_RESCAN
        if kind == _REMOVE:
            if os.path.lexists(fs_path(abs_path)):
                # It is back (a "created" event was lost or reordered): settle
                # it as a change rather than drop a live file from the index.
                return _BECOME_CHANGE, rel, None
            return _DELIVER_REMOVE, dir_path if is_dir else rel, None
        try:
            st = os.stat(fs_path(abs_path))
        except FileNotFoundError:
            return _DELIVER_REMOVE, dir_path if is_dir else rel, None
        except OSError:
            # Present but not stat-able right now (locked, flaky share): wait.
            if attempts >= self.MAX_ATTEMPTS:
                return _DROP, rel, None
            return _RECHECK, rel, sig
        if stat_mod.S_ISDIR(st.st_mode):
            if not rel or not self.matcher.is_bundle(rel.rpartition("/")[2]):
                return _DELIVER_CHANGE, dir_path, None
        elif not stat_mod.S_ISREG(st.st_mode):
            return _DROP, rel, None  # a FIFO or socket appeared: not a document
        new_sig = (int(st.st_size), int(st.st_mtime_ns))
        if phase == 0:
            if self._delivered.get(key) == new_sig:
                return _DROP, rel, None  # nothing changed since we last said so
            return _RECHECK, rel, new_sig
        if attempts >= self.MAX_ATTEMPTS or (new_sig == sig and not self._locked(abs_path, st)):
            return _DELIVER_CHANGE, rel, new_sig
        return _RECHECK, rel, new_sig

    @staticmethod
    def _locked(abs_path: str, st: os.stat_result) -> bool:
        """Another program holds the file open for writing (Windows sharing
        violation, surfaced as PermissionError). Never opens a placeholder or
        anything that is not a regular file."""
        if not stat_mod.S_ISREG(st.st_mode) or is_placeholder(os.path.basename(abs_path), st):
            return False
        try:
            with open(fs_path(abs_path), "rb"):
                return False
        except PermissionError:
            return True
        except OSError:
            return False

    def _deliver(self, changed: set[str], removed: set[str]) -> None:
        ch, rm = sorted(changed), sorted(removed)
        for i in range(0, max(len(ch), len(rm)), _BATCH):
            if self._stop.is_set():
                return
            try:
                self.on_paths(set(ch[i:i + _BATCH]), set(rm[i:i + _BATCH]))
            except Exception as exc:  # noqa: BLE001 — the caller's bug must not stop watching
                logger.warning(f"[userdocs] watcher: on_paths failed: {exc}")


__all__ = ["OnPaths", "ROOT_RESCAN", "SourceWatcher", "WatcherStartError"]
