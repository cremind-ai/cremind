"""The root guard: an absent folder is a reason to wait, never a reason to delete.

A scan that finds nothing where 10,000 files used to be has two explanations.
The user deleted them — or the folder is not there right now: the USB disk is
unplugged, the network share did not mount, the Docker bind mount is missing
after a compose edit, the root was renamed. The second is far more common, and
acting on it as if it were the first would wipe an index that took days and
real money (captions) to build.

So every scan asks the guard first, and the guard only ever *holds*:

- :meth:`RootGuard.check_root` — is the root there at all, and is it the same
  folder? Existence alone proves little:
  :func:`app.config.settings.get_user_working_directory` ``makedirs`` the
  working directory, so an unmounted root reappears as an empty folder. Hence
  "empty while the index had files" and "a different device" are checked too.
- :meth:`RootGuard.check_bulk` — the root is there, but a large share of the
  indexed files vanished at once (or a burst of delete events arrived). The
  engine hides those rows and asks the user before purging.

The thresholds are class attributes so they can be calibrated on real trees
without touching the logic.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.documents import deploy_env
from app.documents.discovery.walker import fit_i63

REASON_ROOT_MISSING = "root_missing"
REASON_ROOT_EMPTY = "root_empty"
REASON_DEVICE_CHANGED = "device_changed"
REASON_BIND_MISSING = "bind_missing"
REASON_MASS_DELETE = "mass_delete"


@dataclass
class GuardVerdict:
    ok: bool
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _is_nonempty(path: str) -> bool:
    with os.scandir(path) as it:
        return next(it, None) is not None


class RootGuard:
    # An empty root is only suspicious when the index expected something.
    EMPTY_ROOT_MIN_MANIFEST = 20
    # Mass deletion: at least this many files *and* this share of the index.
    MASS_DELETE_MIN_FILES = 200
    MASS_DELETE_MIN_RATIO = 0.5
    # ... or more than this many delete events inside the window.
    DELETE_BURST_EVENTS = 500
    DELETE_BURST_WINDOW_S = 10.0
    # A device change holds the source only if it also lost this share of files.
    DEVICE_CHANGE_MISSING_RATIO = 0.2

    def check_root(
        self,
        root_abs: str,
        *,
        manifest_count: int,
        root_identity: dict | None,
        docker_bind_expected: bool = False,
    ) -> GuardVerdict:
        """Is the root usable right now?

        ``ok=False`` with ``root_missing`` (absent, not a directory, or not
        listable), ``bind_missing`` (a container whose compose file promised a
        bind mount at the root, and nothing is mounted there) or ``root_empty``
        (no entries at all while the manifest holds at least
        ``EMPTY_ROOT_MIN_MANIFEST`` files).

        ``device_changed`` is *advisory* and comes back with ``ok=True``: the
        root now lives on a different ``st_dev`` than ``root_identity``
        recorded. A reboot can renumber devices, so this alone is no reason to
        stop; the caller holds only when the scan also finds more than
        ``DEVICE_CHANGE_MISSING_RATIO`` of the files missing — see
        :meth:`device_change_holds`.
        """
        if not os.path.isdir(root_abs):
            return GuardVerdict(False, REASON_ROOT_MISSING, {"exists": os.path.exists(root_abs)})

        if docker_bind_expected:
            status = deploy_env.docker_root_status(root_abs)
            if status.get("root_mounted") is False:
                return GuardVerdict(False, REASON_BIND_MISSING, status)

        try:
            nonempty = _is_nonempty(root_abs)
            st = os.stat(root_abs)
        except OSError as exc:
            return GuardVerdict(False, REASON_ROOT_MISSING, {"exists": True, "error": str(exc)})

        seen_nonempty = bool((root_identity or {}).get("seen_nonempty"))
        if not nonempty and manifest_count >= self.EMPTY_ROOT_MIN_MANIFEST:
            return GuardVerdict(
                False, REASON_ROOT_EMPTY,
                {"manifest_count": manifest_count, "seen_nonempty": seen_nonempty},
            )

        old_dev = (root_identity or {}).get("dev")
        new_dev = fit_i63(st.st_dev)
        if old_dev is not None and int(old_dev) != new_dev:
            return GuardVerdict(True, REASON_DEVICE_CHANGED, {"old_dev": int(old_dev), "new_dev": new_dev})
        return GuardVerdict(True)

    def device_change_holds(self, missing: int, total: int) -> bool:
        """After a ``device_changed`` verdict: does the scan's loss confirm it?"""
        return total > 0 and missing / total > self.DEVICE_CHANGE_MISSING_RATIO

    def check_bulk(self, missing: int, total: int, *, delete_events_in_window: int = 0) -> str | None:
        """``mass_delete`` when deletions are too many to apply unasked, else ``None``.

        ``missing``/``total``: files a scan found gone / files in the manifest.
        ``delete_events_in_window``: watcher deletions in the last
        ``DELETE_BURST_WINDOW_S`` seconds (see :class:`DeleteBurst`).
        """
        if delete_events_in_window > self.DELETE_BURST_EVENTS:
            return REASON_MASS_DELETE
        if (
            missing >= self.MASS_DELETE_MIN_FILES
            and total > 0
            and missing / total >= self.MASS_DELETE_MIN_RATIO
        ):
            return REASON_MASS_DELETE
        return None

    @staticmethod
    def identity_of(root_abs: str) -> dict[str, Any]:
        """What "the same root" means, recorded after a good scan.

        ``{dev, ino, realpath, fstype, is_mountpoint, seen_nonempty}``;
        ``seen_nonempty`` is whether it has entries *now* — the caller ORs it
        with the stored value. ``dev``/``ino`` are ``None`` when the root cannot
        be stat'ed; ``fstype`` is ``None`` off Linux.
        """
        try:
            st = os.stat(root_abs)
            dev, ino = fit_i63(st.st_dev), fit_i63(st.st_ino)
        except OSError:
            dev = ino = None
        try:
            nonempty = _is_nonempty(root_abs)
        except OSError:
            nonempty = False
        mount = deploy_env.mount_for_path(root_abs)
        return {
            "dev": dev,
            "ino": ino,
            "realpath": os.path.realpath(root_abs),
            "fstype": mount.fstype if mount else None,
            "is_mountpoint": os.path.ismount(root_abs),
            "seen_nonempty": nonempty,
        }


class DeleteBurst:
    """Counts delete events in a sliding window, for :meth:`RootGuard.check_bulk`.

    The watcher's caller records each batch of removals; a burst of more than
    ``RootGuard.DELETE_BURST_EVENTS`` inside the window means "someone just
    emptied a folder tree (or it just unmounted)" and the deletes are held.
    Thread-safe.
    """

    def __init__(self, window_s: float = RootGuard.DELETE_BURST_WINDOW_S):
        self.window_s = float(window_s)
        self._events: deque[tuple[float, int]] = deque()
        self._total = 0
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        horizon = now - self.window_s
        while self._events and self._events[0][0] < horizon:
            self._total -= self._events.popleft()[1]

    def record(self, n: int = 1, now: float | None = None) -> int:
        """Add ``n`` deletions; return the count now inside the window."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if n > 0:
                self._events.append((now, n))
                self._total += n
            self._trim(now)
            return self._total

    def count(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._trim(now)
            return self._total


__all__ = [
    "DeleteBurst",
    "GuardVerdict",
    "REASON_BIND_MISSING",
    "REASON_DEVICE_CHANGED",
    "REASON_MASS_DELETE",
    "REASON_ROOT_EMPTY",
    "REASON_ROOT_MISSING",
    "RootGuard",
]
