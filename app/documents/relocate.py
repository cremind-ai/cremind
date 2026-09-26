"""Move the document trees of an older install to where this build keeps them.

Two renames changed where things live on disk, and nothing moved them:

====================================  ===========================================
Before                                Now (under ``<SYS>`` = the system dir)
====================================  ===========================================
``<SYS>/<profile NAME>/documents``    ``storage/cremind_documents/profiles/<uuid>``
  (Cremind manual pages a profile       (keyed by ``profiles.id``; see
  authored)                             :mod:`app.cremind_documents.paths`)
``<SYS>/documents``                   ``storage/cremind_documents/shared``
  (mirror of the bundled manual)        (re-seeded every boot; the old one is
                                        simply retired)
``storage/userdocs/<uuid>``           ``storage/documents/<uuid>``
  (Documentation search indexes:        (index.db, its -wal/-shm, .bak files —
  index.db + sidecars)                  moved intact: no re-extraction, no
                                        re-captioning, same epoch)
``storage/userdocs/tmp``              dropped (extractor scratch)
``<SYS>/cli/documents``               retired (the long-gone ``cli`` scope)
====================================  ===========================================

Name-keyed paths are why this is careful rather than a ``mv``: a profile is a
directory at ``<SYS>/<name>``, and names are free text. A profile named
``storage`` has its manual pages at ``<SYS>/storage/documents`` — the very
directory the indexes move INTO — so those pages are moved out first, and the
index directories already there are recognised and left alone. A profile named
``documents`` owns ``<SYS>/documents``, so the old shared mirror is only
deleted when no profile has that name. A profile named ``cli`` owns
``<SYS>/cli``, which the old boot code deleted wholesale.

Guarantees
----------
- **One mover per installation.** An exclusive lock file under
  ``<SYS>/storage`` (stale ones — a dead pid on this host, or no heartbeat for
  :data:`LOCK_STALE_AFTER_S` — are taken over).
- **Resumable.** A durable journal (JSON under ``<SYS>/storage``, replaced
  atomically) records each step. Every step is idempotent and re-derives what
  is left from the filesystem, so a crash anywhere just means the next run
  finishes the job; the journal is what lets a finished-but-unrecorded step be
  verified and closed instead of redone.
- **Never overwrites.** A destination that already holds different content is
  a conflict: BOTH sides are kept, the step is recorded as a conflict with an
  actionable message, logged, and reported by :func:`pending_errors` until a
  later run finds it resolved (a step whose old copy is gone — deleted by the
  person, or with its profile or index — is closed, never reported forever).
- **Never races a fresh index.** While an index has not moved (the run was
  skipped or failed), the engine uses the old directory in place
  (:func:`app.documents.index.index_dir`) instead of creating an empty one at
  the new place — so the next run still finds nothing in the way and moves
  it whole, rather than recording a conflict and leaving it stranded.
- **Manual pages move once.** The name-keyed ``<SYS>/<name>/documents``
  folders are moved for the profiles that existed at the first run (and
  those a restore brings back), then never again: a name outlives its
  profile, and a later profile of that name must not inherit the old one's
  pages (``_authored_candidates``).
- **Verified.** A moved index must still say it belongs to its uuid
  (``meta.profile_uid``) and the destination must hold the same files with the
  same sizes (and hashes for small files) before a step is recorded as done.
- **Cross-device safe.** ``os.rename`` first; across devices, copy to a
  temporary sibling, verify, rename into place, then remove the source.

Order at boot (``app/server.py``): after storage is up (the profile rows are
the map from names to uuids), BEFORE the Cremind manual service and its
watchers, the Documentation search engine, its indexing workers, research
recovery and GC. A backup restore runs it right after it copies file trees
(``app/backup/engine.py``), and the boot that follows runs it again — a no-op
by then, or the finish of whatever the restore could not move.

Pure standard library at module level: the offline CLI's restore path imports
this with the server stopped.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
import time
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from app.utils.logger import logger

JOURNAL_FILE = "document-relocation.json"
LOCK_FILE = "document-relocation.lock"
JOURNAL_VERSION = 1

# A lock whose holder has shown no sign of life for this long is taken over.
# The holder touches it between steps and during long copies.
LOCK_STALE_AFTER_S = 15 * 60
# How long a second process waits for the first before giving up this round.
LOCK_WAIT_S = 30.0
# The retirements run on the boot path itself (not in a worker thread) and are
# retried on every boot, so they never wait long for another holder.
RETIRE_LOCK_WAIT_S = 2.0
# Files up to this size are compared by hash, larger ones by size (an index
# can be gigabytes; its bytes are not re-read just to prove a rename).
HASH_MAX_BYTES = 8 * 1024 * 1024
# The temporary sibling a cross-device copy lands in before it is renamed into
# place. Anything wearing it at the destination is a leftover of an
# interrupted copy and is removed on the next run.
TMP_SUFFIX = ".cremind-relocating"

STATE_MOVING = "moving"
STATE_DONE = "done"
STATE_CONFLICT = "conflict"
STATE_ERROR = "error"
STATE_KEPT = "kept"          # deliberately left in place (a profile owns it)
STATE_PENDING = "pending"    # waiting on something else (the vector store)
_PROBLEM_STATES = (STATE_CONFLICT, STATE_ERROR)

# The journal step recording which profiles (uuid → name) existed when the
# Cremind manual pages were first moved: the only ones ever moved from the
# name-keyed folders (see ``_authored_candidates``).
AUTHORED_BASELINE_STEP = "authored_baseline"

_INDEX_DB = "index.db"
_LEGACY_INDEX_PARTS = ("storage", "userdocs")
_INDEX_PARTS = ("storage", "documents")
_MANUAL_ROOT_PARTS = ("storage", "cremind_documents")
_AUTHORED_PARTS = _MANUAL_ROOT_PARTS + ("profiles",)
_SHARED_PARTS = _MANUAL_ROOT_PARTS + ("shared",)

# Serialises runs inside one process (the lock file serialises processes).
_process_lock = threading.Lock()


# ── Results ────────────────────────────────────────────────────────────────


@dataclass
class RelocationReport:
    ran: bool = False
    busy: bool = False
    moved_authored_files: int = 0
    moved_indexes: int = 0
    removed: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    # Left in place on purpose, with a warning (not a problem to resolve).
    kept: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran, "busy": self.busy,
            "moved_authored_files": self.moved_authored_files,
            "moved_indexes": self.moved_indexes,
            "removed": list(self.removed), "errors": list(self.errors),
            "kept": list(self.kept),
        }


# ── Paths ──────────────────────────────────────────────────────────────────


def _sys(system_dir: str | Path | None) -> Path:
    if system_dir is not None:
        return Path(system_dir)
    from app.config.settings import BaseConfig

    return Path(BaseConfig.CREMIND_SYSTEM_DIR)


def _rel(base: Path, p: Path) -> str:
    try:
        return p.relative_to(base).as_posix()
    except ValueError:
        return str(p)


def journal_path(system_dir: str | Path | None = None) -> Path:
    return _sys(system_dir) / "storage" / JOURNAL_FILE


def lock_path(system_dir: str | Path | None = None) -> Path:
    return _sys(system_dir) / "storage" / LOCK_FILE


def _safe_segment(name: str) -> bool:
    """A profile name or uuid usable as ONE path segment."""
    s = str(name or "")
    return bool(s) and s not in (".", "..") and not any(c in s for c in ("/", "\\", os.sep, "\0"))


# ── Journal ────────────────────────────────────────────────────────────────


class Journal:
    """The durable record of every step, rewritten atomically on each change.

    Paths inside are relative to the system dir, so the record still reads
    right if the directory is later mounted somewhere else."""

    def __init__(self, path: Path, data: Optional[dict[str, Any]] = None) -> None:
        self.path = path
        self.data: dict[str, Any] = data or {"version": JOURNAL_VERSION, "steps": {}}
        self.data.setdefault("steps", {})

    @classmethod
    def load(cls, system_dir: str | Path | None = None) -> "Journal":
        path = journal_path(system_dir)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path)
        except OSError as exc:
            logger.warning(f"[relocate] could not read the journal {path}: {exc}; starting a new one")
            return cls(path)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("steps"), dict):
                raise ValueError("not a journal")
        except ValueError as exc:
            # Every step re-derives its work from the filesystem, so a damaged
            # journal costs only the record of past conflicts — keep the bad
            # copy for a human and carry on.
            aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(path, aside)
            except OSError:
                pass
            logger.warning(f"[relocate] the journal was unreadable ({exc}); moved aside to {aside.name}")
            return cls(path)
        return cls(path, data)

    def step(self, step_id: str) -> Optional[dict[str, Any]]:
        return self.data["steps"].get(step_id)

    def steps(self) -> dict[str, dict[str, Any]]:
        return dict(self.data["steps"])

    def record(self, step_id: str, **fields: Any) -> dict[str, Any]:
        entry = dict(self.data["steps"].get(step_id) or {})
        entry.update(fields)
        entry["at"] = time.time()
        if entry.get("state") not in _PROBLEM_STATES:
            for stale in ("error", "conflicts", "failures"):
                if stale not in fields:
                    entry.pop(stale, None)
        if entry.get("state") == STATE_DONE:
            # The pre-move manifest only matters while a move can still be
            # interrupted; a finished step does not need to carry it.
            entry.pop("manifest", None)
        self.data["steps"][step_id] = entry
        self.save()
        return entry

    def save(self) -> None:
        self.data["version"] = JOURNAL_VERSION
        self.data["updated_at"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{_uuid.uuid4().hex[:8]}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, self.path)


def pending_errors(system_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Conflicts and failures still waiting for a person, newest first.

    Each entry carries ``step``, ``state``, ``error`` (an actionable sentence)
    and, for a conflict, the relative paths on both sides — what a status
    endpoint shows. Read-only: never takes the lock."""
    journal = Journal.load(system_dir)
    out = []
    for step_id, entry in journal.steps().items():
        if entry.get("state") in _PROBLEM_STATES:
            out.append({"step": step_id, **entry})
    out.sort(key=lambda e: -float(e.get("at") or 0))
    return out


def problem_uid(problem: dict[str, Any]) -> Optional[str]:
    """The profile uuid a :func:`pending_errors` entry is about, or None for
    an installation-wide one (``indexes``, the old shared mirror…).

    By uuid, not by the ``profile`` name the entry may also carry: an index
    step only ever knew the uuid, and a name can be reused by a later,
    unrelated profile that must not see its predecessor's paths."""
    step = str(problem.get("step") or "")
    if not step.startswith(("index:", "authored:")):
        return None
    uid = _step_uid(step, problem)
    return uid if _safe_segment(uid) else None


def forget_index(system_dir: str | Path | None, uid: str) -> bool:
    """A profile's index was deleted on purpose (Settings → delete the local
    index, the profile deleted or cleaned): remove the pre-rename copy too and
    close its relocation step.

    Without this, a copy left at ``storage/userdocs/<uid>`` — one the engine
    was using in place before it could move, or one a conflict kept — would
    be moved back by the next boot's relocation, resurrecting the index the
    user just deleted, and a conflict about it would be reported forever.
    Waits only briefly for the installation lock; without it the copy is
    still removed (deleting is what was asked) and the next boot closes the
    step, since its source is gone. Returns whether the old copy is gone."""
    if not _safe_segment(uid):
        return False
    base = _sys(system_dir)
    legacy = base.joinpath(*_LEGACY_INDEX_PARTS, str(uid))
    with _Locked(base, RETIRE_LOCK_WAIT_S) as held:
        if legacy.exists():
            shutil.rmtree(legacy, ignore_errors=True)
        gone = not legacy.exists()
        if held.lock is not None and gone:
            journal = Journal.load(base)
            step_id = f"index:{uid}"
            if (journal.step(step_id) or {}).get("state") not in (None, STATE_DONE):
                journal.record(step_id, state=STATE_DONE, resolved="deleted")
    return gone


# ── Installation lock ──────────────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """Is ``pid`` a live process on this host? Never signals it: on Windows
    ``os.kill(pid, 0)`` would send CTRL_C_EVENT."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class InstallationLock:
    """An exclusive lock file shared by every process using the system dir.

    ``O_CREAT | O_EXCL`` makes taking it atomic on every platform and file
    system that Cremind runs on. The file names its holder (pid, host, a
    random token) so a stale one can be recognised: the holder's pid is gone
    on this host, or the file has not been touched for ``stale_after_s``
    (another host, or a holder that hung). Release removes it only if it is
    still ours, so a lock taken over from us is never deleted from under its
    new owner.
    """

    def __init__(
        self,
        system_dir: str | Path | None = None,
        *,
        wait_s: float = LOCK_WAIT_S,
        stale_after_s: float = LOCK_STALE_AFTER_S,
    ) -> None:
        self.path = lock_path(system_dir)
        self.wait_s = wait_s
        self.stale_after_s = stale_after_s
        self.token = _uuid.uuid4().hex
        self.held = False

    def _holder(self) -> Optional[dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if age > self.stale_after_s:
            return True
        holder = self._holder()
        if holder is None:
            # Created but not yet written (or garbled by a crash at that very
            # moment): give a live creator a moment to finish writing.
            return age > 60
        if holder.get("host") == socket.gethostname():
            pid = int(holder.get("pid") or 0)
            if pid == os.getpid():
                # Ours by pid but not by token: left behind by an earlier run
                # in this process that could not release it. Runs in one
                # process are serialised by _process_lock, so it is stale.
                return holder.get("token") != self.token
            return not _pid_alive(pid)
        return False

    def acquire(self) -> bool:
        deadline = time.monotonic() + max(0.0, self.wait_s)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if self._is_stale():
                    holder = self._holder()
                    logger.warning(f"[relocate] taking over a stale lock {self.path} (holder: {holder})")
                    try:
                        os.remove(self.path)
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        logger.warning(f"[relocate] could not remove the stale lock: {exc}")
                        return False
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.25)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({
                    "pid": os.getpid(), "host": socket.gethostname(),
                    "token": self.token, "started_at": time.time(),
                }, fh)
            self.held = True
            return True

    def heartbeat(self) -> None:
        if self.held:
            try:
                os.utime(self.path, None)
            except OSError:
                pass

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        holder = self._holder()
        if holder is not None and holder.get("token") != self.token:
            logger.warning("[relocate] the lock was taken over while we held it; leaving it")
            return
        try:
            os.remove(self.path)
        except OSError:
            pass

    def __enter__(self) -> "InstallationLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


class _Locked:
    """Both locks for one run: this process's, then the installation's.
    ``lock`` is None when another process kept the installation lock past
    the wait (the caller skips its work and says so)."""

    def __init__(self, base: Path, wait_s: float) -> None:
        self._base = base
        self._wait_s = wait_s
        self.lock: Optional[InstallationLock] = None

    def __enter__(self) -> "_Locked":
        _process_lock.acquire()
        lock = InstallationLock(self._base, wait_s=self._wait_s)
        try:
            self.lock = lock if lock.acquire() else None
        except BaseException:
            _process_lock.release()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.lock is not None:
                self.lock.release()
        finally:
            _process_lock.release()


# ── File operations ────────────────────────────────────────────────────────


def _is_cross_device(exc: OSError) -> bool:
    # POSIX EXDEV; Windows ERROR_NOT_SAME_DEVICE (17).
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint(path: Path) -> dict[str, Any]:
    """Size, plus a hash when the file is small enough to be worth reading."""
    size = path.stat().st_size
    fp: dict[str, Any] = {"size": size}
    if size <= HASH_MAX_BYTES:
        fp["sha256"] = _hash(path)
    return fp


def _same_fingerprint(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("size") != b.get("size"):
        return False
    if "sha256" in a and "sha256" in b:
        return a["sha256"] == b["sha256"]
    return True


def _same_content(a: Path, b: Path) -> bool:
    """Byte-identical files (always hashed: this decides whether one copy may
    be deleted, which a size match alone cannot justify)."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return _hash(a) == _hash(b)
    except OSError:
        return False


def _manifest(root: Path, *, skip_top: frozenset[str] = frozenset()) -> dict[str, dict[str, Any]]:
    """``{relative posix path: fingerprint}`` of every regular file below
    ``root``. Symlinks are listed by their own size and never followed."""
    out: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        if here == root and skip_top:
            dirnames[:] = [d for d in dirnames if d not in skip_top]
            filenames = [f for f in filenames if f not in skip_top]
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            p = here / name
            rel = p.relative_to(root).as_posix()
            try:
                if p.is_symlink():
                    out[rel] = {"size": p.lstat().st_size, "symlink": True}
                else:
                    out[rel] = _fingerprint(p)
            except OSError:
                continue
    return out


def _manifests_match(expected: dict[str, dict[str, Any]], actual: dict[str, dict[str, Any]]) -> bool:
    if set(expected) != set(actual):
        return False
    return all(_same_fingerprint(expected[k], actual[k]) for k in expected)


def _trees_identical(a: Path, b: Path) -> bool:
    """Same files, byte for byte. Used only where the answer licenses deleting
    one side, so sizes are not enough (two SQLite files of the same page count
    have the same size) and every file is hashed, however large."""
    files_a, files_b = _manifest(a), _manifest(b)
    if set(files_a) != set(files_b):
        return False
    return all(
        _same_content(a.joinpath(*rel.split("/")), b.joinpath(*rel.split("/")))
        for rel in files_a
    )


def _move_file(src: Path, dst: Path) -> None:
    """Move one file to a destination that does not exist yet."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if not _is_cross_device(exc):
            raise
    tmp = dst.with_name(dst.name + TMP_SUFFIX)
    shutil.copy2(src, tmp)
    if not _same_content(src, tmp):
        tmp.unlink(missing_ok=True)
        raise OSError(f"copy of {src} did not verify")
    os.replace(tmp, dst)
    src.unlink()


def _move_dir(src: Path, dst: Path, lock: Optional[InstallationLock] = None) -> None:
    """Move a directory to a destination that does not exist yet: a rename on
    one device, else copy to a temporary sibling, verify, rename, remove."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if not _is_cross_device(exc):
            raise
    tmp = dst.with_name(dst.name + TMP_SUFFIX)
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    expected = _manifest(src)

    def _copy(s: str, d: str) -> Any:
        if lock is not None:
            lock.heartbeat()
        return shutil.copy2(s, d)

    shutil.copytree(src, tmp, copy_function=_copy, symlinks=True)
    if not _manifests_match(expected, _manifest(tmp)):
        shutil.rmtree(tmp, ignore_errors=True)
        raise OSError(f"copy of {src} did not verify")
    os.rename(tmp, dst)
    shutil.rmtree(src)


def _prune_empty_dirs(root: Path, *, keep_root: bool = False) -> None:
    """Remove empty directories below (and, unless ``keep_root``, including)
    ``root``, deepest first. Non-empty ones are left untouched."""
    if not root.is_dir():
        return
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        if keep_root and p == root:
            continue
        try:
            p.rmdir()
        except OSError:
            pass


def _read_index_uid(index_db: Path) -> Optional[str]:
    """``meta.profile_uid`` of an index file, or None when it cannot be read.

    A normal read/write connection, closed before returning: it replays a WAL
    left by a crash (a read-only open cannot), and closing the last connection
    checkpoints it — so the directory moved afterwards holds a settled file.
    Never creates a file: callers only ask about one that exists."""
    if not index_db.is_file():
        return None
    try:
        conn = sqlite3.connect(str(index_db), timeout=5, isolation_level=None)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'profile_uid'").fetchone()
        return str(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# ── Steps ──────────────────────────────────────────────────────────────────


def _index_like(entry: Path, known_uids: frozenset[str]) -> bool:
    """Is ``entry`` (a child of ``storage/documents``) part of the index store
    rather than a manual page a profile named ``storage`` wrote there?"""
    name = entry.name
    if not entry.is_dir():
        return False
    return (
        name == "tmp"
        or name in known_uids
        or name.endswith(TMP_SUFFIX)
        or (entry / _INDEX_DB).exists()
    )


def _relocate_authored(
    base: Path,
    name: str,
    uid: str,
    journal: Journal,
    report: RelocationReport,
    known_uids: frozenset[str],
    lock: InstallationLock,
) -> None:
    """``<SYS>/<name>/documents`` → ``storage/cremind_documents/profiles/<uid>``,
    file by file, into a destination that may already hold pages.

    Per file: moved when the destination has none, dropped when the
    destination already has the identical bytes (a resumed or repeated run),
    kept in place — a conflict — when the destination differs."""
    step_id = f"authored:{uid}"
    src = base / name / "documents"
    dst = base.joinpath(*_AUTHORED_PARTS, uid)
    prior = journal.step(step_id)
    if not src.is_dir():
        if prior and prior.get("state") != STATE_DONE:
            # Resolved by hand, or finished by a run that crashed before
            # recording it: the old location is gone either way.
            journal.record(step_id, state=STATE_DONE, profile=name, resolved=True)
        return

    skip = frozenset(
        c.name for c in src.iterdir() if name == "storage" and _index_like(c, known_uids)
    )
    # Links are left where they are: moving one would move the link, not the
    # page, and following it could carry in a file from anywhere on disk.
    files = {rel: fp for rel, fp in _manifest(src, skip_top=skip).items() if not fp.get("symlink")}
    if not files:
        if not skip:
            _prune_empty_dirs(src)
        if prior and prior.get("state") != STATE_DONE:
            journal.record(step_id, state=STATE_DONE, profile=name, resolved=True)
        return

    journal.record(
        step_id, state=STATE_MOVING, kind="authored", profile=name, uid=uid,
        src=_rel(base, src), dst=_rel(base, dst), files=len(files),
    )
    moved: list[str] = []
    conflicts: list[str] = []
    failures: list[str] = []
    for rel in sorted(files):
        lock.heartbeat()
        s = src.joinpath(*rel.split("/"))
        d = dst.joinpath(*rel.split("/"))
        leftover = d.with_name(d.name + TMP_SUFFIX)
        if leftover.exists():
            leftover.unlink(missing_ok=True)
        try:
            if not d.exists() and not d.is_symlink():
                _move_file(s, d)
                moved.append(rel)
            elif d.is_file() and _same_content(s, d):
                s.unlink()
            else:
                conflicts.append(rel)
        except OSError as exc:
            failures.append(f"{rel}: {exc}")

    # Verify what was moved before recording it.
    bad = [
        rel for rel in moved
        if not dst.joinpath(*rel.split("/")).is_file()
        or not _same_fingerprint(files[rel], _fingerprint(dst.joinpath(*rel.split("/"))))
    ]
    report.moved_authored_files += len(moved)
    if name == "storage":
        # Only the page directories: storage/documents itself holds indexes.
        for child in src.iterdir():
            if child.is_dir() and child.name not in skip:
                _prune_empty_dirs(child)
    else:
        _prune_empty_dirs(src)

    if bad or failures:
        err = (
            f"Moving profile '{name}''s Cremind manual pages from {_rel(base, src)} to "
            f"{_rel(base, dst)} failed for {len(bad) + len(failures)} file(s); they are still "
            "in the old folder and the next restart retries. Check disk space and permissions."
        )
        entry = journal.record(step_id, state=STATE_ERROR, error=err, failures=(failures + bad)[:50])
        report.errors.append({"step": step_id, **entry})
        logger.error(f"[relocate] {err} {failures[:5] + bad[:5]}")
        return
    if conflicts:
        err = (
            f"Profile '{name}' has Cremind manual pages in both {_rel(base, src)} (old) and "
            f"{_rel(base, dst)} (current) with different content: {', '.join(conflicts[:10])}"
            f"{' …' if len(conflicts) > 10 else ''}. Nothing was overwritten. Keep the version "
            f"you want in {_rel(base, dst)}, delete the old copy, and restart."
        )
        entry = journal.record(step_id, state=STATE_CONFLICT, error=err, conflicts=conflicts[:200])
        report.errors.append({"step": step_id, **entry})
        logger.warning(f"[relocate] {err}")
        return
    journal.record(step_id, state=STATE_DONE, moved=len(moved))
    if moved:
        logger.info(
            f"[relocate] moved {len(moved)} Cremind manual page(s) of profile '{name}' "
            f"to {_rel(base, dst)}"
        )


def _authored_candidates(
    journal: Journal, pairs: list[tuple[str, str]], *, restoring: bool,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Which profiles' ``<SYS>/<name>/documents`` this run may move — and
    which it must leave alone.

    The old folder is keyed by NAME, and a name outlives its profile: a
    profile deleted before the upgrade leaves ``<SYS>/<name>/documents``
    behind, and after the upgrade that path is just an ordinary folder in a
    profile's own tree. Re-running the move on every boot would hand the old
    tenant's pages to any later profile created under the same name, and
    sweep a folder a profile created for its own files into its manual. So
    the move is one-shot per installation:

    - the first run records which profiles (by uuid) existed —
      :data:`AUTHORED_BASELINE_STEP` — and moves each of them once;
    - later runs only retry those profiles' unfinished steps (a conflict, a
      failure, a crash mid-move); a profile whose step is done, or that had
      nothing to move, is never looked at again;
    - a profile created after that first run is never moved into
      (:func:`_note_unowned_authored` records a warning instead);
    - a restore is its own first run for the profiles it restored: an archive
      from before the move carries each restored profile's pages at
      ``<name>/documents``, owned by exactly those rows, so they are added to
      the baseline and moved whatever their earlier steps said.

    A journal that had to be set aside as unreadable loses the baseline; the
    next run records a new one from the profiles present then.
    """
    baseline = journal.step(AUTHORED_BASELINE_STEP)
    if baseline is None:
        baseline = journal.record(
            AUTHORED_BASELINE_STEP, state=STATE_MOVING, profiles={u: n for n, u in pairs},
        )
    owners: dict[str, str] = dict(baseline.get("profiles") or {})
    if restoring:
        owners.update({u: n for n, u in pairs})
        journal.record(AUTHORED_BASELINE_STEP, profiles=owners)
        return sorted(pairs), []
    first_pass = baseline.get("state") != STATE_DONE
    movable: list[tuple[str, str]] = []
    others: list[tuple[str, str]] = []
    for name, uid in sorted(pairs):
        if uid not in owners:
            others.append((name, uid))
            continue
        state = (journal.step(f"authored:{uid}") or {}).get("state")
        if state == STATE_DONE or (state is None and not first_pass):
            continue
        movable.append((name, uid))
    return movable, others


def _note_unowned_authored(
    base: Path,
    name: str,
    uid: str,
    journal: Journal,
    report: RelocationReport,
    known_uids: frozenset[str],
) -> None:
    """A profile created after the first relocation has a ``documents``
    folder at the old manual location: it is not moved (it may be the pages
    of a deleted profile that had the same name, or simply this profile's own
    folder), and a warning is recorded once — not a conflict, nothing waits
    on a person. For a profile named ``storage`` that folder is the index
    store; only something other than index directories counts."""
    src = base / name / "documents"
    step_id = f"authored:{uid}"
    try:
        has_content = src.is_dir() and any(
            not (name == "storage" and _index_like(c, known_uids)) for c in src.iterdir()
        )
    except OSError:
        has_content = False
    if not has_content or (journal.step(step_id) or {}).get("state") == STATE_KEPT:
        return
    warning = (
        f"{_rel(base, src)} was left where it is: profile '{name}' was created after the "
        "Cremind manual moved to storage/cremind_documents, so that folder is not its old "
        "manual (it may belong to an earlier profile of the same name). Move any pages it "
        "should own into its manual folder by hand."
    )
    entry = journal.record(
        step_id, state=STATE_KEPT, kind="authored", profile=name, uid=uid,
        src=_rel(base, src), warning=warning,
    )
    report.kept.append({"step": step_id, **entry})
    logger.warning(f"[relocate] {warning}")


def _relocate_index(
    base: Path,
    uid: str,
    journal: Journal,
    report: RelocationReport,
    lock: InstallationLock,
    profile: Optional[str] = None,
) -> None:
    """``storage/userdocs/<uid>`` → ``storage/documents/<uid>``, whole.

    Runs while nothing has the index open (before the engine starts). The
    directory is moved as a unit — index.db, its -wal/-shm and any .bak
    backups together, so SQLite finds the WAL next to its database and no
    committed transaction is lost.

    Until it has moved, the engine uses the old directory in place
    (:func:`app.documents.index.index_dir`) rather than creating a new index
    beside it, so a skipped or failed move is simply retried here on the next
    boot — never turned into a conflict by an empty index at ``dst``.

    ``profile`` is the uuid's profile name when a row still maps it (not for
    the index of a deleted profile): recorded on the step so the status
    route can show a profile its own problems."""
    step_id = f"index:{uid}"
    src = base.joinpath(*_LEGACY_INDEX_PARTS, uid)
    dst = base.joinpath(*_INDEX_PARTS, uid)
    leftover = dst.with_name(dst.name + TMP_SUFFIX)
    if leftover.exists():
        shutil.rmtree(leftover, ignore_errors=True)
    who = {"profile": profile} if profile else {}

    prior = journal.step(step_id) or {}
    if (
        prior.get("state") == STATE_MOVING
        and prior.get("manifest")
        and dst.is_dir()
        and _manifests_match(prior["manifest"], _manifest(dst))
        and _read_index_uid(dst / _INDEX_DB) in (None, uid)
    ):
        # A cross-device move that crashed while removing its source: the
        # copy at ``dst`` was verified before it was renamed into place (only
        # then does a destination appear), so what is left at ``src`` is the
        # half-deleted original. Nothing ran in between — the engine starts
        # after relocation — so ``dst`` cannot be a fresh index.
        shutil.rmtree(src, ignore_errors=True)
        journal.record(step_id, state=STATE_DONE)
        report.moved_indexes += 1
        return

    stored_uid = _read_index_uid(src / _INDEX_DB)
    if stored_uid is not None and stored_uid != uid:
        err = (
            f"The Documentation search index in {_rel(base, src)} says it belongs to profile "
            f"id {stored_uid!r}, not {uid!r}, so it was not moved (it would be refused anyway). "
            "Delete that folder — indexes are rebuilt from your files — and restart."
        )
        entry = journal.record(step_id, state=STATE_ERROR, kind="index", uid=uid,
                               src=_rel(base, src), dst=_rel(base, dst), error=err, **who)
        report.errors.append({"step": step_id, **entry})
        logger.error(f"[relocate] {err}")
        return

    expected = _manifest(src)
    journal.record(
        step_id, state=STATE_MOVING, kind="index", uid=uid, src=_rel(base, src),
        dst=_rel(base, dst), manifest=expected,
        identity="verified" if stored_uid else "unreadable", **who,
    )
    if dst.exists():
        if dst.is_dir() and not any(dst.iterdir()):
            dst.rmdir()
        elif dst.is_dir() and _trees_identical(src, dst):
            # Byte-identical copies (a restore, a hand copy): the old one is
            # redundant.
            shutil.rmtree(src)
            journal.record(step_id, state=STATE_DONE)
            report.moved_indexes += 1
            return
        else:
            err = (
                f"Profile id {uid} has a Documentation search index in both {_rel(base, src)} "
                f"(old) and {_rel(base, dst)} (current) and they differ. Both were kept; the "
                f"current one is in use. If it is complete, delete {_rel(base, src)}; otherwise "
                f"stop Cremind, replace {_rel(base, dst)} with the old folder, and restart."
            )
            entry = journal.record(step_id, state=STATE_CONFLICT, error=err)
            report.errors.append({"step": step_id, **entry})
            logger.warning(f"[relocate] {err}")
            return
    try:
        _move_dir(src, dst, lock)
    except OSError as exc:
        err = (
            f"Could not move the Documentation search index {_rel(base, src)} to "
            f"{_rel(base, dst)}: {exc}. It was left where it is and the next restart retries; "
            "make sure nothing else has the folder open."
        )
        entry = journal.record(step_id, state=STATE_ERROR, error=err)
        report.errors.append({"step": step_id, **entry})
        logger.error(f"[relocate] {err}")
        return
    _finish_index_step(base, step_id, journal, report)


def _finish_index_step(base: Path, step_id: str, journal: Journal, report: RelocationReport) -> None:
    """Verify a moved index against the manifest recorded before the move."""
    entry = journal.step(step_id) or {}
    uid = str(entry.get("uid") or step_id.split(":", 1)[1])
    dst = base.joinpath(*_INDEX_PARTS, uid)
    if not dst.exists():
        # Neither side exists any more: removed by hand (or its profile's
        # purge) — there is nothing left to verify or to warn about.
        journal.record(step_id, state=STATE_DONE, resolved="gone")
        return
    expected = entry.get("manifest") or {}
    actual = _manifest(dst)
    stored_uid = _read_index_uid(dst / _INDEX_DB)
    if stored_uid is not None and stored_uid != uid:
        ok, why = False, f"its index now says it belongs to {stored_uid!r}"
    elif not _manifests_match(expected, actual):
        ok, why = False, "the destination does not hold the same files"
    else:
        ok, why = True, ""
    if ok:
        journal.record(step_id, state=STATE_DONE)
        report.moved_indexes += 1
        logger.info(f"[relocate] moved the Documentation search index of profile id {uid} to {_rel(base, dst)}")
        return
    err = (
        f"The Documentation search index moved to {_rel(base, dst)} did not verify ({why}). "
        "If search for that profile misbehaves, rebuild its index from Settings → "
        "Documentation search."
    )
    entry = journal.record(step_id, state=STATE_ERROR, error=err)
    report.errors.append({"step": step_id, **entry})
    logger.error(f"[relocate] {err}")


def _step_uid(step_id: str, entry: dict[str, Any]) -> str:
    """The uuid an ``index:``/``authored:`` step is about (the entry's own
    ``uid`` field, else the step id's suffix)."""
    return str(entry.get("uid") or step_id.split(":", 1)[-1])


def _relocate_indexes(
    base: Path,
    journal: Journal,
    report: RelocationReport,
    lock: InstallationLock,
    names: Optional[dict[str, str]] = None,
) -> None:
    legacy_root = base.joinpath(*_LEGACY_INDEX_PARTS)
    names = names or {}
    # Steps whose source is gone, which the loop below will not visit:
    # - a crash interrupted a move after the move itself — verify and close;
    # - a conflict or failure a person resolved by deleting the old copy (as
    #   its message told them to), or a profile deletion / index purge took
    #   with it — nothing is left to move or to report, so close it the way
    #   ``_relocate_authored`` closes a manual step whose folder is gone.
    for step_id, entry in journal.steps().items():
        if not step_id.startswith("index:") or entry.get("state") == STATE_DONE:
            continue
        uid = _step_uid(step_id, entry)
        if not _safe_segment(uid) or (legacy_root / uid).exists():
            continue
        if entry.get("state") == STATE_MOVING:
            _finish_index_step(base, step_id, journal, report)
        else:
            journal.record(step_id, state=STATE_DONE, resolved=True)
    if not legacy_root.is_dir():
        return
    for entry in sorted(legacy_root.iterdir(), key=lambda p: p.name):
        lock.heartbeat()
        if entry.name == "tmp":
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                report.removed.append(_rel(base, entry))
                journal.record("index_scratch", state=STATE_DONE)
            continue
        if not entry.is_dir() or not _safe_segment(entry.name):
            continue
        _relocate_index(base, entry.name, journal, report, lock, profile=names.get(entry.name))
    try:
        legacy_root.rmdir()
    except OSError:
        return  # something was kept (a conflict, a stray file)
    report.removed.append(_rel(base, legacy_root))
    journal.record("index_legacy_root", state=STATE_DONE)


def relocate(
    system_dir: str | Path | None,
    profiles: Iterable[tuple[str, str]],
    *,
    reason: str = "boot",
    wait_s: float = LOCK_WAIT_S,
) -> RelocationReport:
    """Run every filesystem step. ``profiles`` is ``(name, uuid)`` per profile
    row — the only map from the old name-keyed paths to the new uuid-keyed
    ones. Never raises for a step's failure (it is journaled and reported);
    returns ``busy=True`` without doing anything when another process holds
    the lock past ``wait_s``."""
    base = _sys(system_dir)
    report = RelocationReport()
    pairs = [(str(n), str(u)) for n, u in profiles if _safe_segment(n) and _safe_segment(u)]
    known_uids = frozenset(u for _, u in pairs)
    with _Locked(base, wait_s) as held:
        lock = held.lock
        if lock is None:
            logger.warning(
                f"[relocate] another process holds {lock_path(base)}; skipping this {reason} run"
            )
            report.busy = True
            return report
        journal = Journal.load(base)
        report.ran = True
        # Manual pages first: for a profile named ``storage`` they sit in the
        # directory the indexes are about to move into.
        movable, others = _authored_candidates(journal, pairs, restoring=reason == "restore")
        for name, uid in movable:
            try:
                _relocate_authored(base, name, uid, journal, report, known_uids, lock)
            except Exception as exc:  # noqa: BLE001 — one profile never blocks the rest
                step_id = f"authored:{uid}"
                entry = journal.record(
                    step_id, state=STATE_ERROR, profile=name, uid=uid,
                    error=f"Unexpected failure moving profile '{name}''s Cremind manual "
                          f"pages: {exc}. The next restart retries.",
                )
                report.errors.append({"step": step_id, **entry})
                logger.exception(f"[relocate] manual pages of profile {name!r} failed")
            lock.heartbeat()
        for name, uid in others:
            _note_unowned_authored(base, name, uid, journal, report, known_uids)
        if (journal.step(AUTHORED_BASELINE_STEP) or {}).get("state") != STATE_DONE:
            journal.record(AUTHORED_BASELINE_STEP, state=STATE_DONE)
        try:
            _relocate_indexes(base, journal, report, lock, {u: n for n, u in pairs})
            if (journal.step("indexes") or {}).get("state") == STATE_ERROR:
                journal.record("indexes", state=STATE_DONE)
        except Exception as exc:  # noqa: BLE001
            entry = journal.record(
                "indexes", state=STATE_ERROR,
                error=f"Unexpected failure moving Documentation search indexes: {exc}. "
                      "The next restart retries.",
            )
            report.errors.append({"step": "indexes", **entry})
            logger.exception("[relocate] index relocation failed")
    if report.moved_authored_files or report.moved_indexes or report.errors:
        logger.info(f"[relocate] {reason}: {report.to_dict()}")
    return report


# ── Retirements that wait for something else ───────────────────────────────


def retire_legacy_trees(
    system_dir: str | Path | None, profile_names: Iterable[str],
) -> list[str]:
    """Delete the old shared mirror (``<SYS>/documents``) and the old ``cli``
    scope tree (``<SYS>/cli/documents``) — unless a PROFILE of that name owns
    the directory, in which case it is left alone and logged.

    Call only after the new shared directory has been seeded: until then the
    old mirror is the only copy of the bundled manual on disk. Returns what
    was removed."""
    base = _sys(system_dir)
    names = set(profile_names)
    removed: list[str] = []
    shared = base.joinpath(*_SHARED_PARTS)
    seeded = shared.is_dir() and any(shared.rglob("*.md"))
    with _Locked(base, RETIRE_LOCK_WAIT_S) as held:
        if held.lock is None:
            return removed
        journal = Journal.load(base)
        legacy_shared = base / "documents"
        if legacy_shared.exists():
            if "documents" in names:
                if (journal.step("shared_legacy") or {}).get("state") != STATE_KEPT:
                    logger.info(
                        "[relocate] <SYS>/documents belongs to a profile named 'documents'; "
                        "the old shared-manual mirror in it is left alone"
                    )
                    journal.record("shared_legacy", state=STATE_KEPT)
            elif not seeded:
                journal.record("shared_legacy", state=STATE_PENDING)
            else:
                shutil.rmtree(legacy_shared, ignore_errors=True)
                if not legacy_shared.exists():
                    removed.append("documents")
                    journal.record("shared_legacy", state=STATE_DONE)
        legacy_cli = base / "cli" / "documents"
        if legacy_cli.exists():
            if "cli" in names:
                if (journal.step("cli_legacy") or {}).get("state") != STATE_KEPT:
                    journal.record("cli_legacy", state=STATE_KEPT)
            else:
                shutil.rmtree(legacy_cli, ignore_errors=True)
                try:
                    (base / "cli").rmdir()
                except OSError:
                    pass
                if not legacy_cli.exists():
                    removed.append("cli/documents")
                    journal.record("cli_legacy", state=STATE_DONE)
    if removed:
        logger.info(f"[relocate] retired the old manual trees: {removed}")
    return removed


def retire_legacy_manual_collection(
    store: Any, *, system_dir: str | Path | None = None,
) -> str:
    """Drop the Cremind manual's pre-rename collection once the new one holds
    points. Returns the step's state: ``done`` (dropped, or never there),
    ``pending`` (the store is unreachable or the new collection is not built
    yet — the next boot or embedding rebuild tries again).

    Never ``collection_exists`` (an outage reads as "missing") and never
    before the replacement is populated: until then a degraded install still
    has something to fall back to."""
    from app.cremind_documents.sync import COLLECTION_NAME, LEGACY_COLLECTION_NAME

    base = _sys(system_dir)
    with _Locked(base, RETIRE_LOCK_WAIT_S) as held:
        if held.lock is None:
            return STATE_PENDING
        journal = Journal.load(base)
        if store is None:
            if (journal.step("manual_collection") or {}).get("state") == STATE_DONE:
                return STATE_DONE
            journal.record("manual_collection", state=STATE_PENDING, reason="no vector store")
            return STATE_PENDING
        try:
            names = set(store.list_collections())
        except Exception as exc:  # noqa: BLE001
            journal.record("manual_collection", state=STATE_PENDING, reason=f"store unreachable: {exc}")
            return STATE_PENDING
        if LEGACY_COLLECTION_NAME not in names:
            journal.record("manual_collection", state=STATE_DONE)
            return STATE_DONE
        populated = False
        if COLLECTION_NAME in names:
            try:
                populated = int(store.count(COLLECTION_NAME)) > 0
            except Exception:  # noqa: BLE001 — a store without count(): trust presence
                populated = True
        if not populated:
            journal.record("manual_collection", state=STATE_PENDING, reason="replacement not built yet")
            return STATE_PENDING
        try:
            store.delete_collection(LEGACY_COLLECTION_NAME)
        except Exception as exc:  # noqa: BLE001
            journal.record("manual_collection", state=STATE_PENDING, reason=f"drop failed: {exc}")
            return STATE_PENDING
        journal.record("manual_collection", state=STATE_DONE)
        logger.info(f"[relocate] dropped the pre-rename manual collection {LEGACY_COLLECTION_NAME!r}")
        return STATE_DONE


# ── Entry points ───────────────────────────────────────────────────────────


def _profile_rows() -> list[tuple[str, str]]:
    from app.storage.documents_storage import get_documents_storage

    return [(name, uid) for uid, name in get_documents_storage().profile_uids().items()]


def run_at_boot(system_dir: str | Path | None = None) -> RelocationReport:
    """The boot hook: read the profile rows, then :func:`relocate`.

    Without the rows nothing can be mapped (and the order rule — manual pages
    before indexes — cannot be honoured), so nothing moves this time."""
    try:
        rows = _profile_rows()
    except Exception:  # noqa: BLE001
        logger.exception("[relocate] could not read the profiles; nothing is moved this boot")
        return RelocationReport()
    return relocate(system_dir, rows, reason="boot")


def run_after_restore(system_dir: str | Path, profiles: Iterable[tuple[str, str]]) -> RelocationReport:
    """After a restore copied its file trees: an archive from before the
    layout change puts manual pages back at ``<profile>/documents``. The
    profile rows are the ones just restored (read by the caller from the
    restore's own engine)."""
    return relocate(system_dir, profiles, reason="restore")


__all__ = [
    "AUTHORED_BASELINE_STEP",
    "InstallationLock",
    "Journal",
    "RelocationReport",
    "forget_index",
    "journal_path",
    "lock_path",
    "pending_errors",
    "problem_uid",
    "relocate",
    "retire_legacy_manual_collection",
    "retire_legacy_trees",
    "run_after_restore",
    "run_at_boot",
]
