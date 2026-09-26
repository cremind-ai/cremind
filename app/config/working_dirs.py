"""Each profile's User Working Directory — where it resolves, who owns a path.

A profile's working directory is its own folder: the root of its file panel,
the default directory of every built-in tool call and terminal, the value of
``$CREMIND_USER_WORKING_DIR``, and the folder Documentation search indexes.

**Where it lives.** ``profiles.working_dir`` holds an explicit folder the admin
chose; NULL means the default, ``<workspaces root>/<profile name>``. The
workspaces root is ``<SYS>/workspaces`` unless the ``CREMIND_WORKSPACES_DIR``
environment variable names another folder — the container images point it into
the documents mount (``/root/Documents/cremind-workspaces``) so the host still
sees the files. Before this layout the folder was one server-wide setting
(``server_config.user_working_dir``); the ``20260929_profile_working_dir``
migration hands that value to the admin profile, so an upgraded admin keeps its
folder and every other profile starts with its own.

**Who may reach a path.** A path belongs to the profile whose folder is the
DEEPEST one containing it (:func:`owners_of`). The zones are every profile's
effective working directory plus every entry directly under the workspaces
root — an entry named after no live profile (a deleted profile's archive, a
stray folder) belongs to nobody, and one that is itself some profile's folder
(``<name>-2``) to that profile, not to one that happens to share its name. So
an admin whose legacy folder happens to contain the workspaces root still owns
its own files, while each workspace inside stays its owner's alone, and two
profiles the admin deliberately pointed at the same folder share it. :func:`is_foreign` is the one predicate every file
surface asks (the file API, the agent's file tools, cwd switches, terminals,
file watchers, Documentation search); the admin is not exempt.

Cremind's system folder outside the workspaces root (every profile's skills,
uploads and persona live there) is never a zone's: its own rules decide it,
even when an upgraded admin's legacy folder contains it. And a path is judged
as the folder it IS, not as it is spelled: on Windows a share or
device spelling of a local folder (``\\\\localhost\\C$\\…``, ``\\\\?\\C:\\…``) is
placed by file identity (:func:`owners_of`), so no spelling walks around a zone.

Pure standard library at module level (the offline CLI imports it); storage is
reached lazily through :func:`app.config.settings.get_dynamic`'s backing store.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Iterable

from app.utils.logger import logger

WORKSPACES_DIRNAME = "workspaces"
WORKSPACES_ENV = "CREMIND_WORKSPACES_DIR"
# Deleted profiles' folders are moved here (never handed to a new profile of
# the same name). Starts with a dot, which no profile name can.
DELETED_DIRNAME = ".deleted"

# Profile names that are never real profiles (``__server__`` is the pseudo
# profile owning server-side client rows).
_PSEUDO_PREFIX = "__"

_CACHE_TTL_S = 5.0

_WINDOWS = os.name == "nt"


# ── Paths ──────────────────────────────────────────────────────────────────


def _system_dir() -> str:
    from app.config.settings import BaseConfig

    return str(BaseConfig.CREMIND_SYSTEM_DIR)


def _resolved(path: str) -> str:
    """``realpath`` exactly as the OS spells it (a ``\\\\?\\`` prefix kept)."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def _plain_spelling(path: str) -> str:
    r"""Windows' literal-path spellings as ordinary ones: ``\\?\C:\x`` (or
    ``\\.\C:\x``) → ``C:\x``, ``\\?\UNC\host\share\x`` → ``\\host\share\x``.
    ``realpath`` keeps a prefix it was given, so without this a zone stored as
    ``C:\...`` never contains the same folder spelled with one. Other device
    paths (``\\?\Volume{…}\``) stay as they are; :func:`owners_of` places them
    by file identity."""
    if not _WINDOWS or not path.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    rest = path[4:]
    if rest[:4].upper() == "UNC\\":
        return "\\\\" + rest[4:]
    if len(rest) >= 2 and rest[0].isalpha() and rest[1] == ":" and (len(rest) == 2 or rest[2] == "\\"):
        return rest if len(rest) > 2 else rest + "\\"
    return path


def real_path(path: str) -> str:
    """``path`` resolved (``~``, ``..``, links) in its plain spelling."""
    return _plain_spelling(_resolved(path))


def _norm(path: str) -> str:
    return os.path.normcase(path).rstrip("\\/") or os.path.normcase(path)


def is_inside(child: str, parent: str) -> bool:
    """``child`` equals or sits inside ``parent`` (both already resolved)."""
    c, p = _norm(child), _norm(parent)
    if c == p:
        return True
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives on Windows
        return False


def _depth(path: str) -> int:
    return len([part for part in _norm(path).replace("\\", "/").split("/") if part])


def workspaces_root() -> str:
    """The folder that holds every profile's default working directory.
    Not created here."""
    raw = (os.environ.get(WORKSPACES_ENV) or "").strip()
    if raw:
        return os.path.normpath(os.path.abspath(os.path.expanduser(raw)))
    return os.path.normpath(os.path.join(_system_dir(), WORKSPACES_DIRNAME))


def deleted_root() -> str:
    return os.path.join(workspaces_root(), DELETED_DIRNAME)


def valid_profile_dirname(profile: object) -> bool:
    """A profile name this module will turn into a directory name."""
    s = str(profile or "")
    if not s or s in (".", "..") or s.startswith(_PSEUDO_PREFIX) or s.startswith("."):
        return False
    return not any(sep in s for sep in ("/", "\\", os.sep, "\0", ":"))


def default_working_dir(profile: str) -> str:
    """``<workspaces root>/<profile>``. Raises ValueError for a name that is
    not a plain directory name."""
    if not valid_profile_dirname(profile):
        raise ValueError(f"invalid profile name for a working directory: {profile!r}")
    return os.path.join(workspaces_root(), str(profile))


# ── Storage ────────────────────────────────────────────────────────────────


def _storage() -> Any | None:
    # By module path: ``app.config`` re-exports a Dynaconf object named
    # ``settings`` that shadows the module of the same name.
    import importlib

    return getattr(importlib.import_module("app.config.settings"), "_dynamic_config_storage", None)


def stored_working_dir(profile: str) -> str | None:
    """The folder the admin chose for ``profile`` (as stored), or None for
    the default. None too when storage is not up (setup mode, offline CLI)."""
    store = _storage()
    if store is None or not profile:
        return None
    try:
        value = store.get_profile_working_dir(profile)
    except Exception as exc:  # noqa: BLE001 — no column yet, no DB: the default
        logger.debug(f"[working_dirs] could not read the working directory of {profile!r}: {exc}")
        return None
    value = (value or "").strip() if isinstance(value, str) else None
    return value or None


def _expand(value: str) -> str:
    return _plain_spelling(os.path.normpath(os.path.abspath(os.path.expanduser(value))))


def profile_working_dir(profile: str, *, create: bool = True) -> str:
    """The profile's working directory: the admin's choice, else the default.

    Created when missing (``create=True``), like the server-wide folder always
    was — so "it exists" never proves anything is mounted there. A failure to
    create it is logged and the path still returned; the tool that needs it
    reports the error."""
    if not profile:
        raise ValueError("a working directory belongs to a profile; none was given")
    explicit = stored_working_dir(profile)
    path = _expand(explicit) if explicit else default_working_dir(profile)
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            logger.warning(f"[working_dirs] could not create the working directory {path!r} of {profile!r}: {exc}")
    return path


# ── Change listeners ───────────────────────────────────────────────────────

_listeners: list[Callable[[str], None]] = []
_listeners_lock = threading.Lock()


def add_change_listener(fn: Callable[[str], None]) -> None:
    """``fn(profile)`` runs after that profile's working directory changed
    (set, reset to the default, or its profile deleted). Must not block."""
    with _listeners_lock:
        if fn not in _listeners:
            _listeners.append(fn)


def remove_change_listener(fn: Callable[[str], None]) -> None:
    with _listeners_lock:
        if fn in _listeners:
            _listeners.remove(fn)


def notify_changed(profile: str) -> None:
    invalidate()
    with _listeners_lock:
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(profile)
        except Exception:  # noqa: BLE001 — one listener never stops the others
            logger.exception(f"[working_dirs] change listener failed for {profile!r}")


def set_working_dir(profile: str, path: str | None) -> str:
    """Store the admin's choice (``None`` or the default path → the default)
    and return the effective folder. Validate with
    :func:`validate_working_dir` first; this only writes."""
    store = _storage()
    if store is None:
        raise RuntimeError("storage is not available")
    value: str | None = None
    if path:
        expanded = _expand(path)
        if _norm(expanded) != _norm(default_working_dir(profile)):
            value = expanded
    store.set_profile_working_dir(profile, value)
    notify_changed(profile)
    return profile_working_dir(profile)


# ── Ownership ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Snapshot:
    at: float
    # False when there is no storage to ask (setup mode, the offline CLI, a
    # unit test without a DB). Ownership is then unknown and :func:`owners_of`
    # answers None — the other access rules still apply.
    available: bool
    by_norm: dict[str, str]          # normcased name → name
    # (normcased resolved dir, owners) — normcased so comparisons are plain.
    zones: tuple[tuple[str, frozenset[str]], ...]
    root: str                        # normcased resolved workspaces root
    sysdir: str                      # normcased resolved system directory
    # Storage is configured but could not be read, and no earlier reading
    # exists: ownership fails closed — no zones, so every workspaces entry is
    # nobody's (see :func:`ownership_unavailable`).
    closed: bool = False


_snapshot: _Snapshot | None = None
# The last snapshot read from storage, reused while storage fails.
_last_good: _Snapshot | None = None
_rows_failing = False
_snapshot_lock = threading.Lock()


class _RowsUnreadable(Exception):
    """Storage is configured, but listing the profiles raised."""


def invalidate() -> None:
    global _snapshot
    with _snapshot_lock:
        _snapshot = None


def _profile_rows() -> dict[str, str | None] | None:
    """Every live profile's stored folder; None when there is no storage.
    Raises :class:`_RowsUnreadable` when storage exists but failed — which is
    not "no profiles": answering None there would open every folder."""
    global _rows_failing
    store = _storage()
    if store is None:
        return None
    try:
        rows = store.profile_working_dirs()
    except Exception as exc:  # noqa: BLE001
        if not _rows_failing:
            logger.warning(f"[working_dirs] could not list the profiles' working directories: {exc}")
        _rows_failing = True
        raise _RowsUnreadable() from exc
    _rows_failing = False
    return {str(k): (v or None) for k, v in (rows or {}).items() if valid_profile_dirname(k)}


def _build_snapshot(rows: dict[str, str | None] | None) -> _Snapshot:
    groups: dict[str, set[str]] = {}
    for name, explicit in (rows or {}).items():
        try:
            path = _expand(explicit) if explicit else default_working_dir(name)
        except ValueError:
            continue
        groups.setdefault(_norm(real_path(path)), set()).add(name)
    return _Snapshot(
        at=time.monotonic(),
        available=rows is not None,
        by_norm={os.path.normcase(n): n for n in (rows or {})},
        zones=tuple((key, frozenset(owners)) for key, owners in groups.items()),
        root=_norm(real_path(workspaces_root())),
        sysdir=_norm(real_path(_system_dir())),
    )


def _current() -> _Snapshot:
    global _snapshot, _last_good
    with _snapshot_lock:
        snap = _snapshot
        if snap is not None and time.monotonic() - snap.at <= _CACHE_TTL_S:
            return snap
        try:
            rows = _profile_rows()
        except _RowsUnreadable:
            # Never cached: the next question asks storage again. Meanwhile
            # the last reading stands (for the same folders), else nobody
            # reaches anything under the workspaces root.
            closed = replace(_build_snapshot({}), closed=True)
            last = _last_good
            if last is not None and (last.root, last.sysdir) == (closed.root, closed.sysdir):
                return last
            return closed
        snap = _build_snapshot(rows)
        _snapshot = snap
        if snap.available:
            _last_good = snap
        return snap


def ownership_unavailable() -> bool:
    """True while storage is configured but the profiles cannot be read and
    no earlier reading exists: :func:`owners_of` is failing closed, so a
    caller about to act on the whole picture (Documentation search choosing
    what to lock out of an index) should refuse and retry later."""
    return _current().closed


def _in_system_slice(real: str, snap: _Snapshot) -> bool:
    """In the system directory but not under the workspaces root: every
    profile's skills, uploads and persona. Never a zone's, even when a legacy
    admin folder (``~``, ``~/.cremind``) contains it — the system-dir rules
    decide it."""
    return is_inside(real, snap.sysdir) and not (real != snap.root and is_inside(real, snap.root))


def _owners_in(snap: _Snapshot, real: str) -> frozenset[str] | None:
    """:func:`owners_of` for a normcased local ``real``."""
    if _in_system_slice(real, snap):
        return None
    best = -1
    owners: set[str] = set()
    for zone, zone_owners in snap.zones:
        if is_inside(real, zone):
            d = _depth(zone)
            if d > best:
                best, owners = d, set(zone_owners)
            elif d == best:
                owners |= zone_owners
    root = snap.root
    if real != root and is_inside(real, root):
        rel = os.path.relpath(real, root)
        child = rel.replace("\\", "/").split("/", 1)[0]
        entry = os.path.join(root, child)
        # An entry that IS some profile's folder (``<ws>/bob-2``, given to
        # ``bob``) is that profile's alone: a profile later named ``bob-2``
        # does not share it by name. Otherwise the name decides (a profile
        # moved elsewhere keeps its old default; no live name: nobody's).
        if not any(zone == entry for zone, _o in snap.zones):
            d = _depth(entry)
            entry_owner = snap.by_norm.get(os.path.normcase(child))
            entry_owners = {entry_owner} if entry_owner else set()
            if d > best:
                best, owners = d, entry_owners
            elif d == best:
                owners |= entry_owners
    if best < 0:
        return None
    return frozenset(owners)


# ── File identity (Windows share and device spellings) ─────────────────────

# (snapshot, {(st_dev, st_ino): normcased local folder}) — see _identities.
_identity_cache: tuple[_Snapshot, dict[tuple[int, int], str]] | None = None


def _file_id(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    # A filesystem without file ids reports 0: no identity to compare.
    return (st.st_dev, st.st_ino) if st.st_ino else None


def _identities(snap: _Snapshot) -> dict[tuple[int, int], str]:
    """``{(st_dev, st_ino): folder}`` for every folder a path is placed under:
    the zones, the workspaces root and each entry in it, the system directory.
    Built the first time a share or device path needs it, then kept with the
    snapshot — ordinary paths never pay for it."""
    global _identity_cache
    cached = _identity_cache
    if cached is not None and cached[0] is snap:
        return cached[1]
    places = [zone for zone, _o in snap.zones] + [snap.root, snap.sysdir]
    try:
        with os.scandir(snap.root) as it:
            for entry in it:
                p = _norm(entry.path)
                # A link there is placed by where it leads, like any local path.
                if entry.is_dir() and _norm(real_path(entry.path)) == p:
                    places.append(p)
    except OSError:
        pass
    ids: dict[tuple[int, int], str] = {}
    for place in places:
        fid = _file_id(place)
        if fid is not None:
            ids.setdefault(fid, place)
    _identity_cache = (snap, ids)
    return ids


def _localized(real: str, snap: _Snapshot) -> str:
    r"""``real`` (normcased) in the spelling the zones use.

    On Windows, ``realpath`` leaves a share spelling of a local folder
    (``\\localhost\C$\…``, ``\\127.0.0.1\C$\…``, a drive mapped to one) or a
    volume path (``\\?\Volume{…}\…``) as it is, and no zone string contains
    it — yet it is the same folder (the same ``st_dev``/``st_ino``). So a path
    still starting with ``\\`` that lies in no zone, root or system directory
    itself spelled that way is placed by identity: its deepest ancestor that
    IS one of the folders in :func:`_identities` gives the local spelling, and
    the components below it follow. One that matches nothing is an ordinary
    network path and comes back unchanged."""
    if not _WINDOWS or not real.startswith("\\\\"):
        return real
    known = [zone for zone, _o in snap.zones] + [snap.root, snap.sysdir]
    if any(k.startswith("\\\\") and is_inside(real, k) for k in known):
        return real
    ids = _identities(snap)
    if not ids:
        return real
    cur, below = real, []
    while True:
        fid = _file_id(cur)
        if fid is not None and fid in ids:
            return _norm(os.path.join(ids[fid], *reversed(below)))
        parent = os.path.dirname(cur)
        if parent == cur:
            return real
        below.append(os.path.basename(cur))
        cur = parent


def local_path(path: str) -> str:
    """``path`` resolved like :func:`real_path` and, on Windows, a share or
    device spelling of a folder Cremind knows (a zone, the workspaces root or
    an entry in it, the system directory) translated to its local path —
    letter case as on disk. For rules that compare a path with a local one
    (the system directory's private folders, the index store); any other
    path comes back as :func:`real_path` gives it."""
    real = real_path(path)
    if not (_WINDOWS and real.startswith("\\\\")):
        return real
    local = _localized(_norm(real), _current())
    return real if local == _norm(real) else real_path(local)


def owners_of(path: str) -> frozenset[str] | None:
    """Who owns ``path``: the profiles whose zone is the deepest one containing
    it (see the module docstring). ``None`` when no zone contains it (an
    ordinary path — the other rules decide), and for the system directory
    outside the workspaces root; an EMPTY set when it lies in a
    workspaces-root entry no live profile owns (nobody may reach it). A share
    or device spelling of a local folder is judged as that folder."""
    if not path:
        return None
    snap = _current()
    if not snap.available:
        return None
    return _owners_in(snap, _localized(_norm(real_path(path)), snap))


def profiles_using(path: str) -> frozenset[str]:
    """The live profiles whose working directory IS ``path`` — not merely
    contains it, and not by a workspaces entry's name (see :func:`owners_of`).
    Empty when none, or when ownership is unknown."""
    snap = _current()
    if not path or not snap.available:
        return frozenset()
    key = _localized(_norm(real_path(path)), snap)
    return frozenset().union(*(owners for zone, owners in snap.zones if zone == key))


def is_foreign(path: str, profile: str | None) -> bool:
    """True when ``path`` lies in a working directory that is not
    ``profile``'s (or in an unowned workspaces entry). A missing profile is
    treated as nobody: every owned path is foreign to it."""
    owners = owners_of(path)
    if owners is None:
        return False
    return not profile or profile not in owners


def foreign_dirs_inside(root: str, profile: str) -> list[str]:
    """Other profiles' working directories (and unowned workspaces entries)
    that sit strictly inside ``root`` — for walkers that must prune them
    (Documentation search's locked excludes, directory listings). Paths come
    back normcased (lower-case on Windows): compare, don't display. They are
    spelled like ``root`` resolves (a walk of ``\\\\?\\C:\\x`` or a share
    spelling of a local folder gets them under that same spelling)."""
    snap = _current()
    if not snap.available:
        return []
    given = _norm(_resolved(root))
    r = _localized(_norm(real_path(root)), snap)
    out: list[str] = []
    for zone, owners in snap.zones:
        if zone != r and is_inside(zone, r) and profile not in owners and not _in_system_slice(zone, snap):
            out.append(zone)
    ws = snap.root
    if is_inside(ws, r) or is_inside(r, ws):
        try:
            entries = list(os.scandir(ws)) if os.path.isdir(ws) else []
        except OSError:
            entries = []
        for entry in entries:
            p = _norm(entry.path)
            if p == r or not is_inside(p, r):
                continue
            # Ask the full rule, not the entry's name: a profile's own
            # explicit folder there (``<name>-2``) is its, whatever it is called.
            owners = _owners_in(snap, _norm(real_path(entry.path)))
            if owners is not None and profile not in owners and p not in out:
                out.append(p)
    out = sorted(set(out))
    if given != r:
        out = [given + p[len(r):] if p.startswith(r) else p for p in out]
    return out


# ── Validation (admin sets a profile's folder) ─────────────────────────────


@dataclass
class WorkingDirCheck:
    ok: bool
    path: str | None = None
    is_default: bool = False
    code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_working_dir(profile: str, raw: str | None, *, create: bool = False) -> WorkingDirCheck:
    """May ``profile``'s working directory be ``raw``? ``None``/empty means the
    default, always allowed. Otherwise the folder must be absolute; must not be
    an operating-system location or a bare drive root; must not sit inside
    Cremind's system folder (other than the profile's own default); must not
    sit inside the workspaces root (another profile's space) nor strictly
    inside another profile's folder (pointing two profiles at the SAME folder
    is allowed — that is how an admin shares one). A network spelling of a
    folder on this computer's own disk is judged — and stored — as its local
    path when it is one Cremind knows, else refused. ``create=True`` also
    creates it and reports a failure."""
    try:
        default = default_working_dir(profile)
    except ValueError as exc:
        return WorkingDirCheck(False, None, False, "invalid_profile", str(exc))
    if raw is None or not str(raw).strip():
        return _created(WorkingDirCheck(True, default, True), create)
    text = str(raw).strip()
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        return WorkingDirCheck(False, text, False, "not_absolute",
                               "Give a full path (for example C:\\Users\\me\\Work or /home/me/work).")
    path = _expand(expanded)
    local = _local_folder(path)
    if local is None:
        return WorkingDirCheck(False, path, False, "network_path_to_local_folder",
                               "That is a network path to a folder on this computer's own disk. Give "
                               "its local path instead (for example C:\\Users\\me\\Work).")
    if _norm(local) != _norm(real_path(path)):
        path = local  # a share spelling of a folder Cremind knows: stored as that folder
    if _norm(real_path(path)) == _norm(real_path(default)) or _norm(path) == _norm(default):
        return _created(WorkingDirCheck(True, default, True), create)
    if os.path.exists(path) and not os.path.isdir(path):
        return WorkingDirCheck(False, path, False, "not_directory", "That path is a file, not a folder.")
    real = real_path(path)
    sysdir = real_path(_system_dir())
    if is_inside(real, sysdir):
        return WorkingDirCheck(False, path, False, "inside_system_dir",
                               "That folder is inside Cremind's system folder, which holds credentials "
                               "and every profile's data. Use the default or a folder outside it.")
    from app.documents.settings import _is_forbidden  # noqa: PLC0415 — shared OS-location rule

    if _is_forbidden(real):
        return WorkingDirCheck(False, path, False, "forbidden_system_path",
                               "That is an operating-system location, not a working folder.")
    ws = real_path(workspaces_root())
    if is_inside(real, ws):
        return WorkingDirCheck(False, path, False, "inside_workspaces",
                               "That folder is inside the profiles' workspaces folder; each profile's "
                               "folder there is its own. Use the default or a folder outside it.")
    owners = owners_of(real)
    if owners is not None and profile not in owners:
        # Allowed only when it is exactly another profile's folder (sharing).
        exact = any(_norm(zone) == _norm(real) for zone, _o in _current().zones)
        if not exact:
            return WorkingDirCheck(False, path, False, "inside_other_working_dir",
                                   "That folder is inside another profile's working directory. Choose "
                                   "that same folder to share it, or a folder of its own.")
    return _created(WorkingDirCheck(True, path, False), create)


def _local_folder(path: str) -> str | None:
    r"""``path`` resolved, spelled the way the zones are. A share or device
    spelling of a folder Cremind knows comes back as its local path (see
    :func:`_localized`). None for a share spelling of any OTHER folder on this
    computer's own disks (``\\localhost\C$\Work``): stored like that, the
    folder's ordinary spelling would reach it past its zone."""
    local = local_path(path)
    if _WINDOWS and local.startswith("\\\\") and _on_local_volume(local):
        return None
    return local


def _st_dev(path: str) -> int | None:
    try:
        return os.stat(path).st_dev
    except (OSError, ValueError):
        return None


def _on_local_volume(path: str) -> bool:
    """Is ``path`` — a share spelling — on one of this computer's own drives?
    Compared by volume serial (``st_dev``); a folder not created yet is
    answered for by its deepest existing ancestor."""
    cur = path
    while (dev := _st_dev(cur)) is None:
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent
    try:
        drives = os.listdrives()
    except (AttributeError, OSError):
        return False
    for drive in drives:
        if _resolved(drive).startswith("\\\\"):
            continue  # a drive letter mapped to a share is not this computer's disk
        if _st_dev(drive) == dev:
            return True
    return False


def _created(check: WorkingDirCheck, create: bool) -> WorkingDirCheck:
    if not create or not check.ok or not check.path:
        return check
    try:
        os.makedirs(check.path, exist_ok=True)
    except OSError as exc:
        return WorkingDirCheck(False, check.path, check.is_default, "not_creatable",
                               f"Cremind could not create that folder: {exc}")
    if not os.access(check.path, os.R_OK | os.W_OK | os.X_OK):
        return WorkingDirCheck(False, check.path, check.is_default, "not_writable",
                               "Cremind cannot read and write that folder.")
    return check


# ── Profile lifecycle ──────────────────────────────────────────────────────


def is_default_location(profile: str, path: str) -> bool:
    try:
        return _norm(real_path(path)) == _norm(real_path(default_working_dir(profile)))
    except ValueError:
        return False


def fresh_default_for_new_profile(profile: str) -> str | None:
    """For a profile being created: None when its default folder is free — it
    simply uses the default — else an explicit sibling (``<name>-2``,
    ``<name>-3``…) that is, so a new profile never adopts files someone left in
    a folder of its name, nor another profile's folder. See :func:`_free_for`."""
    default = default_working_dir(profile)
    invalidate()  # judged against the profiles as they stand now
    if _free_for(profile, default):
        return None
    for n in range(2, 1000):
        candidate = f"{default}-{n}"
        if _free_for(profile, candidate):
            return candidate
    raise RuntimeError(f"no free working directory for {profile!r} under {workspaces_root()}")


def _free_for(profile: str, path: str) -> bool:
    """Absent or empty, and no other profile's: not a live profile's default
    (``<ws>/dan-2`` while a profile ``dan-2`` exists — the entry rule of
    :func:`owners_of` names it, wherever its folder is) and not a folder some
    other profile was given (a ``<name>-2`` sibling, one the admin chose)."""
    if not _empty_or_missing(path):
        return False
    owners = owners_of(path)
    return not (owners and owners - {profile})


def _empty_or_missing(path: str) -> bool:
    if not os.path.exists(path):
        return True
    if not os.path.isdir(path):
        return False
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return False


def archive_default_dir(profile: str, *, stamp: str | None = None) -> str | None:
    """Move a deleted profile's DEFAULT-location folder to
    ``<workspaces root>/.deleted/<name>-<stamp>`` so a later profile of that
    name starts empty. Returns the new path, or None when there was nothing to
    move. Folders the admin chose elsewhere are never touched."""
    src = default_working_dir(profile)
    if not os.path.isdir(src):
        return None
    if _empty_or_missing(src):
        try:
            os.rmdir(src)
        except OSError:
            pass
        return None
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(deleted_root(), exist_ok=True)
    dst = os.path.join(deleted_root(), f"{profile}-{stamp}")
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(deleted_root(), f"{profile}-{stamp}-{n}")
    os.replace(src, dst)
    return dst


def delete_default_dir(profile: str) -> bool:
    """Delete a deleted profile's DEFAULT-location folder outright."""
    import shutil

    src = default_working_dir(profile)
    if not os.path.isdir(src):
        return False
    shutil.rmtree(src)
    return True


def list_all(profiles: Iterable[str]) -> dict[str, str]:
    """``{profile: effective folder}`` without creating anything."""
    out: dict[str, str] = {}
    for p in profiles:
        try:
            out[p] = profile_working_dir(p, create=False)
        except ValueError:
            continue
    return out


__all__ = [
    "DELETED_DIRNAME",
    "WORKSPACES_DIRNAME",
    "WORKSPACES_ENV",
    "WorkingDirCheck",
    "add_change_listener",
    "archive_default_dir",
    "default_working_dir",
    "delete_default_dir",
    "deleted_root",
    "foreign_dirs_inside",
    "fresh_default_for_new_profile",
    "invalidate",
    "is_default_location",
    "is_foreign",
    "is_inside",
    "list_all",
    "local_path",
    "notify_changed",
    "owners_of",
    "ownership_unavailable",
    "profiles_using",
    "profile_working_dir",
    "real_path",
    "remove_change_listener",
    "set_working_dir",
    "stored_working_dir",
    "valid_profile_dirname",
    "validate_working_dir",
    "workspaces_root",
]
