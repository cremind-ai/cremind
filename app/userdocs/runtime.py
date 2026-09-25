"""One profile's User Document Search engine: its index, its folder, its sync.

A :class:`ProfileRuntime` is created by :mod:`app.userdocs.service` for every
profile that has the feature switched on, and owns:

- the profile's index file (:class:`~app.userdocs.index.IndexDB`),
- the folder watcher (or the polling schedule when watching cannot work),
- full scans (boot catch-up, periodic reconcile, after a burst of events),
- the per-file pipeline — fingerprint → extract → chunk → diff → write,
- the Google Drive half (``rt.drive``, a
  :class:`~app.userdocs.sources.drive.DriveSource`), which shares the index
  file and the content tail (:meth:`index_content`) but has its own sync,
  holds and confirmations. Either half may be on without the other: the
  index stays open while either is.

Vectors are deliberately *not* written here. The pipeline stores chunks with
``vec_gen = NULL`` and the service's embedding loop fills them in
(:mod:`app.userdocs.vector_sync`). One code path therefore serves a normal
edit, a backlog after the vector store was down, and a full re-embed after the
embedding model changed — and the keyword index is current the moment a file
is written, whatever the vector store is doing.

Threading: :meth:`configure`, :meth:`drain_watch_paths` and the periodic work
run on the service's maintenance thread; :meth:`run_scan` and
:meth:`run_estimate` on its scan executor; :meth:`process` on the pipeline
workers (several files of one profile may be in flight at once — never the
same file, the service excludes in-flight ids). Everything a thread touches
here is either the index (which serialises its own writes) or guarded by
``self.lock``.
"""

from __future__ import annotations

import datetime as _dt
import os
import threading
import time
from typing import Any, Callable, Iterable

from app.userdocs import settings as uds
from app.userdocs import state as uds_state
from app.userdocs import types as t
from app.userdocs.progress import SyncProgress
from app.utils.logger import logger

SOURCE = uds.SOURCE_LOCAL

# Work priorities (lower is more urgent). P0 is reserved for "the agent is
# reading this file right now" (PR4).
P_INTERACTIVE = 0
P_LIVE = 1        # an edit the watcher just saw
P_BULK = 2        # first sync, reconcile
P_UPGRADE = 3     # moves, extractor/chunker upgrades, deferred work

# A watcher delete is only a tombstone for this long, so a delete+create pair
# that the watcher delivered in separate batches (a slow move, an editor's
# save-by-rename) can still find the old chunks and reuse their vectors.
TOMBSTONE_GRACE_S = 120.0
# Rows the user chose to keep after a mass deletion stay hidden and re-checked
# this long before they are finally removed.
MISSING_KEEP_S = 14 * 86400
# A first sync this small starts without asking.
FIRST_SYNC_AUTO = {"files": 2000, "bytes": 500 * 1024 * 1024, "images": 100}
# More deletes than this within the burst window are handed to a full scan,
# whose root guard decides whether it is a real deletion or an unmounted disk.
DELETE_BURST_LIMIT = 500
# Retry schedule for files that failed to extract.
BACKOFF_S = (60.0, 300.0, 900.0, 3600.0, 6 * 3600.0)
MAX_ATTEMPTS = len(BACKOFF_S)
NATIVE_RECONCILE_S = 6 * 3600.0


def _now() -> float:
    return time.time()


def iso_local(ts: float | None) -> str | None:
    if not ts:
        return None
    try:
        return _dt.datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def ts_from_iso(value: Any) -> float | None:
    """Epoch seconds for an ISO date/time from document or EXIF metadata.
    A naive value is read as local time (EXIF has no zone unless the camera
    wrote OffsetTimeOriginal, which the extractor folds in)."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        try:
            d = _dt.datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    try:
        return d.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def extract_outcome(result: t.ExtractResult) -> tuple[str, str | None, str | None]:
    """(status, reason, error) of an extraction, as the index records it."""
    doc_meta = result.doc_meta or {}
    if result.status in (t.EXTRACT_OK, t.EXTRACT_PARTIAL):
        return "indexed", (f"partial:{result.reason}" if result.status == t.EXTRACT_PARTIAL else None), None
    if result.status == t.EXTRACT_METADATA_ONLY:
        return "metadata_only", result.reason, None
    if result.reason == "awaiting_extractor":
        return "awaiting_extractor", (doc_meta.get("missing") or "extractor"), None
    return "error", (result.reason or "corrupt"), doc_meta.get("error")


def _norm(path: str | None) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _parent_rel(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def chunks_from_rows(rows: Iterable[dict[str, Any]]) -> list[t.Chunk]:
    """Rebuild :class:`~app.userdocs.types.Chunk` objects from stored rows —
    for re-carding a file whose content did not change (a move, a touched
    mtime) and for copying an identical file, without extracting again."""
    out: list[t.Chunk] = []
    for r in rows:
        out.append(t.Chunk(
            ordinal=int(r.get("ordinal") or 0),
            ctype=r.get("ctype") or t.CTYPE_BODY,
            heading=r.get("heading") or "",
            text=r.get("text") or "",
            text_hash=r.get("text_hash") or "",
            occ=int(r.get("occ") or 0),
            section_key=r.get("section_key"),
            locator=dict(r.get("locator") or {}),
            refs=list(r.get("refs") or []),
            token_est=int(r.get("token_est") or 0),
            folded=r.get("folded"),
        ))
    out.sort(key=lambda c: c.ordinal)
    return out


class ProfileRuntime:
    def __init__(self, service: Any, profile: str, uid: str):
        self.service = service
        self.profile = profile
        self.uid = uid
        self.lock = threading.RLock()
        self.progress = SyncProgress(profile, publish=uds_state.publish_snapshot)
        self.db: Any = None

        self.settings: dict[str, Any] = {}      # the local row (per-profile options live here)
        self.drive_settings: dict[str, Any] = {}
        self.root: str | None = None
        self.matcher: Any = None
        self.watcher: Any = None
        self.watch_mode: str | None = None
        self.watch_reason: str | None = None
        # (root, deploy_env.docker_root_status(root)): read by configure, served
        # by the snapshot — see _refresh_docker_status.
        self._docker_status: tuple[str, dict[str, Any] | None] | None = None

        self.active = False            # configured and allowed to sync
        self.paused_user = False
        self.hold: dict[str, Any] | None = None       # {reason, detail}
        self.suspended: str | None = None
        self.confirmation: dict[str, Any] | None = None
        self.level = "ok"              # governor level, a governor.Level value
        self.estimate: dict[str, Any] | None = None

        self.scanning = False
        self.estimating = False
        # Scans started / finished so far: what a scan ticket is checked against.
        self.scans_started = 0
        self.scans_finished = 0
        self._draining = 0
        self.scan_requested: str | None = None
        self.next_scan_at = 0.0
        self.last_scan_s = 0.0
        self.last_vector_check = 0.0

        self.in_flight: set[int] = set()
        self.pending_vector_deletes: list[int] = []
        self._pending_changed: set[str] = set()
        self._pending_removed: set[str] = set()
        self._folder_cache: dict[str, int] = {}
        # Dirty files per source, from the last refresh_totals: what is
        # waiting depends on whether that source may work right now.
        self._dirty: dict[str, int] = {}
        self.stop_event = threading.Event()
        self._closed = False

        from app.userdocs.discovery.guard import DeleteBurst, RootGuard
        from app.userdocs.sources.drive import DriveSource

        self.guard = RootGuard()
        self.delete_burst = DeleteBurst()
        self.drive = DriveSource(self)

    # ── index file ─────────────────────────────────────────────────────────

    def ensure_db(self):
        """Open (or create, or rebuild) this profile's index file.

        Refused once the runtime is closed: a Drive sync still unwinding
        after the profile's index was deleted must not create it again."""
        with self.lock:
            if self.db is not None and not self.db.closed:
                return self.db
            if self._closed:
                raise RuntimeError("the runtime is closed")
            from app.userdocs.index import (
                IndexCorrupt,
                IndexDB,
                IndexIncompatible,
                index_path,
                rebuild_file,
            )

            path = index_path(self.uid)
            try:
                self.db = IndexDB.open(path, profile_uid=self.uid)
            except (IndexIncompatible, IndexCorrupt) as exc:
                logger.warning(f"[userdocs] {self.profile}: index unusable ({exc}); rebuilding it")
                rebuild_file(path)
                self.db = IndexDB.open(path, profile_uid=self.uid)
                self.db.add_activity(
                    "rebuilt", "The index file was unusable and has been rebuilt from your files.",
                    source=SOURCE, level="warning",
                )
            return self.db

    def close(self) -> None:
        # Drive first: its sync notices the close before the index goes.
        try:
            self.drive.close()
        except Exception:  # noqa: BLE001
            logger.exception(f"[userdocs] {self.profile}: closing the Drive source failed")
        self.stop_event.set()
        self._stop_watching()
        self.progress.close()
        with self.lock:
            self._closed = True
            if self.db is not None:
                try:
                    self.db.close()
                except Exception:  # noqa: BLE001
                    pass
                self.db = None

    # ── configuration ──────────────────────────────────────────────────────

    def configure(self) -> None:
        """Bring the runtime in line with the saved settings and the admin gate.

        Called after every settings save, admin-gate change and embedding
        transition, and at boot. Idempotent: it only starts or stops what
        actually changed.

        Drive is configured first, from its own row (it applies the admin
        gate itself): whether it is on decides if the index stays open when
        the local folder is off.
        """
        from app.storage.userdocs_storage import get_userdocs_storage

        storage = get_userdocs_storage()
        row = storage.get_source(self.profile, SOURCE) or {}
        drive_row = storage.get_source(self.profile, uds.SOURCE_DRIVE) or {}
        policy = uds.read_admin_policy()
        ok, reason = uds.feature_effective(policy)
        self.settings = row
        self.drive_settings = drive_row
        self._configure_drive(drive_row, ok)

        if not row.get("enabled"):
            self._deactivate("disabled", None)
            return
        if not ok:
            self._deactivate("suspended", "admin_gate" if reason == "admin_gate_off" else "embedding_off")
            return

        db = self.ensure_db()
        self.suspended = None
        self.paused_user = bool(db.get_source_state(SOURCE).get("paused_user"))

        custom = row.get("root_mode") == uds.ROOT_CUSTOM
        check = uds.validate_root(row.get("root_path") if custom else None, is_admin=self.profile == "admin")
        if not check.ok:
            self._set_hold("root_invalid", {"code": check.code, "message": check.message, "root": check.path})
            return
        stored = row.get("root_path")
        if not custom and stored and _norm(check.path) != _norm(stored):
            # The server-wide working directory moved under an inherited root.
            # Nobody asked this profile, so nothing moves until it confirms.
            self._set_hold("pending_root_change", {"from": stored, "to": check.path})
            self._set_confirmation({"kind": "root_change", "from": stored, "to": check.path,
                                    "files": db.count_by_status(SOURCE).get("indexed", 0)})
            return

        new_root = check.path
        identity = db.get_source_state(SOURCE).get("root_identity") or {}
        old_root = identity.get("realpath") if isinstance(identity, dict) else None
        if old_root and _norm(old_root) != _norm(new_root):
            self._rebase(old_root, new_root)

        from app.userdocs.discovery.ignore import IgnoreMatcher

        opts = uds.normalize_options(row.get("options"))
        with self.lock:
            root_changed = _norm(self.root) != _norm(new_root)
            self.root = new_root
            self.matcher = IgnoreMatcher(
                new_root,
                excludes=uds.normalize_excludes(row.get("excludes")),
                locked_excludes=check.locked_excludes,
                system_dir=uds.system_dir(),
            )
            self.hold = None
            if self.confirmation and self.confirmation.get("kind") in ("root_change",):
                self.confirmation = None
        self.progress.set_source(SOURCE, root=new_root)
        self._refresh_docker_status(new_root)

        if not row.get("first_sync_confirmed_at"):
            est = self.estimate if (self.estimate and self.estimate.get("root") == new_root) else None
            if est is None or est.get("state") != "done":
                self.service.submit_estimate(self)
                self._publish_state()
                return
            if self._estimate_is_small(est):
                storage.upsert_source(self.profile, SOURCE, first_sync_confirmed_at=_now() * 1000)
            else:
                self._set_confirmation({"kind": "first_sync", "estimate": est})
                return

        if self.paused_user:
            self._stop_watching()
            self.active = True
            self._publish_state()
            return

        self.active = True
        if root_changed or self.watcher is None and self.watch_mode != "poll":
            self._stop_watching()
            self._start_watching(opts)
        self.request_scan("configure")

    def _configure_drive(self, row: dict[str, Any], ok: bool) -> None:
        """Hand the drive row to the Drive half. The user's pause is one
        switch for the whole profile, kept in the local source state, so a
        Drive-only profile reads it here."""
        if ok and row.get("enabled"):
            try:
                db = self.ensure_db()
                self.paused_user = bool(db.get_source_state(SOURCE).get("paused_user"))
            except Exception:  # noqa: BLE001
                logger.exception(f"[userdocs] {self.profile}: could not open the index for Google Drive")
        try:
            self.drive.configure(row or None)
        except Exception:  # noqa: BLE001 — Drive must never stop the local folder from configuring
            logger.exception(f"[userdocs] {self.profile}: configuring Google Drive failed")

    def _deactivate(self, state: str, reason: str | None) -> None:
        self.active = False
        self._stop_watching()
        # Disabled but kept: close the file (it reopens on enable) — unless
        # Drive, which shares it, is still on.
        close = state == "disabled" and not self.drive.enabled
        if close and self.db is not None and self.pending_vector_deletes:
            # Nothing flushes them once the file is closed (a Drive index
            # deleted as Drive was turned off, say).
            from app.userdocs import vector_sync

            vector_sync.flush_deletes(self)
        with self.lock:
            self.suspended = reason if state == "suspended" else None
            if close and self.db is not None:
                try:
                    self.db.close()
                except Exception:  # noqa: BLE001
                    pass
                self.db = None
        if self.drive.enabled:
            self._publish_state()
        else:
            self.progress.set_state(state, reason)

    def _set_hold(self, reason: str, detail: dict[str, Any] | None) -> None:
        with self.lock:
            self.hold = {"reason": reason, "detail": detail or {}}
        self._stop_watching()
        if self.db is not None:
            self.db.update_source_state(SOURCE, state="hold", reason=reason, detail=detail or {}, hold_since=_now())
        self._publish_state()

    def _set_confirmation(self, confirmation: dict[str, Any] | None) -> None:
        with self.lock:
            self.confirmation = confirmation
        self.progress.set_confirmation(confirmation)
        self._publish_state()

    def _estimate_is_small(self, est: dict[str, Any]) -> bool:
        return (
            int(est.get("files") or 0) <= FIRST_SYNC_AUTO["files"]
            and int(est.get("bytes") or 0) <= FIRST_SYNC_AUTO["bytes"]
            and int(est.get("images_to_caption") or 0) <= FIRST_SYNC_AUTO["images"]
        )

    def _rebase(self, old_root: str, new_root: str) -> None:
        """The user moved the folder (already confirmed). Rows whose file is
        still inside the new folder keep their chunks and vectors under a new
        relative path; the rest leave the index."""
        from app.userdocs.discovery.walker import path_hash

        db = self.ensure_db()
        manifest = db.load_manifest(SOURCE)
        drop: list[int] = []
        moved = 0
        for row in manifest.values():
            abs_old = os.path.join(old_root, *row.rel_path.split("/"))
            if uds.is_inside(abs_old, new_root):
                rel = os.path.relpath(abs_old, new_root).replace(os.sep, "/")
                if db.update_file(row.id, rel_path=rel, path_hash=path_hash(rel), folder_id=None):
                    moved += 1
                db.mark_dirty([row.id], priority=P_UPGRADE)
            else:
                drop.append(row.id)
        self.purge_file_ids(drop)
        folder_ids = list(db.folder_ids(SOURCE).values())
        if folder_ids:
            self.queue_vector_deletes(db.delete_folders(folder_ids))
        self._folder_cache.clear()
        db.update_source_state(SOURCE, root_identity={"realpath": new_root})
        db.add_activity(
            "root_changed",
            f"Indexed folder changed to {new_root}: {moved} files kept, {len(drop)} removed.",
            source=SOURCE, detail={"from": old_root, "to": new_root, "kept": moved, "removed": len(drop)},
        )

    # ── the container underneath ───────────────────────────────────────────

    def _refresh_docker_status(self, root: str) -> None:
        """Whether ``root`` is a real host folder or just a directory in the
        container's own layer (:func:`~app.userdocs.deploy_env.docker_root_status`).

        Read here, on the maintenance thread, rather than by the snapshot:
        snapshots are built on every progress frame and from request threads,
        and resolving the path can stall on a hung network mount. Once per
        configure is enough, because a container's mounts do not change while
        it runs — fixing a missing one recreates the container, which restarts
        this process. A failed probe is recorded as unknown, never raised.
        """
        try:
            from app.userdocs.deploy_env import docker_root_status

            status: dict[str, Any] | None = docker_root_status(root)
        except Exception as exc:  # noqa: BLE001 — a probe must never stop configure
            logger.debug(f"[userdocs] {self.profile}: checking the container mount of {root} failed: {exc}")
            status = None
        self._docker_status = (root, status)

    def docker_view(self) -> dict[str, Any] | None:
        """The snapshot's ``docker`` block: whether the indexed folder would
        outlive the container. ``None`` while the local folder is off, or
        before configure has checked the current root. The UI and
        ``cremind userdocs status`` warn when ``in_container`` and
        ``root_mounted is False`` and not ``bind_expected`` — an install whose
        compose file predates the documents bind mount (with the bind
        expected, the root guard holds the folder instead)."""
        cached = self._docker_status
        root = self.root
        if not self.local_on() or not root or cached is None or cached[0] != root or not cached[1]:
            return None
        status = cached[1]
        return {
            "in_container": bool(status.get("in_container")),
            "kubernetes": bool(status.get("kubernetes")),
            "root_mounted": status.get("root_mounted"),
            "persistent": status.get("persistent"),
            "fstype": status.get("fstype"),
            "bind_expected": bool(status.get("bind_expected")),
            "snippet": status.get("snippet"),
        }

    # ── watching ───────────────────────────────────────────────────────────

    def _start_watching(self, opts: dict[str, Any]) -> None:
        from app.userdocs.deploy_env import choose_watch_mode
        from app.userdocs.discovery.watcher import SourceWatcher, WatcherStartError

        dirs_needed = int((self.estimate or {}).get("dirs") or 0)
        mode, why = choose_watch_mode(self.root, (opts.get("observer") or {}).get("mode") or "auto", dirs_needed)
        watcher = None
        if mode == "native":
            watcher = SourceWatcher(self.root, self.matcher, self.on_watch_paths)
            try:
                watcher.start()
            except WatcherStartError as exc:
                logger.warning(f"[userdocs] {self.profile}: watching {self.root} failed ({exc}); polling instead")
                watcher, mode, why = None, "poll", exc.reason
        with self.lock:
            self.watcher = watcher
            self.watch_mode = mode
            self.watch_reason = why
        self.progress.set_source(SOURCE, watch=mode, watch_reason=why)

    def _stop_watching(self) -> None:
        with self.lock:
            watcher, self.watcher = self.watcher, None
        if watcher is not None:
            try:
                watcher.stop(timeout=0.3)
            except Exception:  # noqa: BLE001
                pass

    def on_watch_paths(self, changed: set[str], removed: set[str]) -> None:
        """Watcher thread: only record the paths; the maintenance thread
        applies them (see :meth:`drain_watch_paths`)."""
        with self.lock:
            self._pending_changed |= set(changed)
            self._pending_removed |= set(removed)
        self.service.wake()

    def has_pending_paths(self) -> bool:
        with self.lock:
            return bool(self._pending_changed or self._pending_removed)

    def drain_watch_paths(self) -> None:
        """Turn the watcher's settled paths into index rows (maintenance thread).

        A deleted file becomes a tombstone rather than disappearing, and a
        delete and create in one batch with the same size, mtime and inode is
        a move: the row keeps its chunks and vectors and only its card is
        refreshed.
        """
        with self.lock:
            changed, removed = self._pending_changed, self._pending_removed
            self._pending_changed, self._pending_removed = set(), set()
            self._draining += 1
        try:
            self._drain(changed, removed)
        finally:
            with self.lock:
                self._draining -= 1

    def _drain(self, changed: set[str], removed: set[str]) -> None:
        from app.userdocs.discovery.ignore import SKIP
        from app.userdocs.discovery.hashing import fs_path
        from app.userdocs.discovery.walker import fit_i63, path_hash
        from app.userdocs.discovery.watcher import ROOT_RESCAN
        from app.userdocs.textnorm import fold

        if not self.active or self.hold or self.db is None or self.root is None:
            return
        db = self.db

        if ROOT_RESCAN in changed or any(p.endswith("/") for p in changed):
            # A new folder, a moved folder, or an ignore file changed: a scan
            # settles it (and its move matching covers moved folders).
            self.request_scan("watch")
            changed = {p for p in changed if p != ROOT_RESCAN and not p.endswith("/")}

        gone: dict[int, dict[str, Any]] = {}
        for p in removed:
            for r in db.files_under(SOURCE, p.rstrip("/")):
                if r.get("status") != "tombstone":
                    gone[int(r["id"])] = r
        if gone and self.delete_burst.record(len(gone)) > DELETE_BURST_LIMIT:
            self.request_scan("delete_burst")
            return

        now = _now()
        touched = 0
        # A move can arrive as a delete in one batch and a create in the next
        # (slow renames, save-by-rename editors), so recent tombstones are
        # move candidates too — that is what their grace period is for.
        candidates: dict[int, dict[str, Any]] = {int(r["id"]): r for r in gone.values()}
        if changed:
            for r in db.list_files(status="tombstone", source=SOURCE, limit=500):
                candidates.setdefault(int(r["id"]), r)

        def _same_file(g: dict[str, Any], fp: dict[str, Any]) -> bool:
            if (g.get("size"), g.get("mtime_ns")) != (fp["size"], fp["mtime_ns"]):
                return False
            # Windows' DirEntry.stat() reports inode 0 while os.stat() reports
            # the real file id, so an inode only counts when both sides know it.
            return not (g.get("ino") and fp["ino"]) or int(g["ino"]) == int(fp["ino"])

        for rel in sorted(changed):
            abs_path = os.path.join(self.root, *rel.split("/"))
            if self.matcher.classify(rel, abs_path) == SKIP:
                row = db.file_by_path(SOURCE, path_hash(rel))
                if row is not None:
                    gone[int(row["id"])] = row
                continue
            try:
                st = os.stat(fs_path(abs_path), follow_symlinks=False)
            except OSError:
                continue
            fp = {
                "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns), "mtime": float(st.st_mtime),
                "ino": fit_i63(st.st_ino), "dev": fit_i63(st.st_dev),
            }
            ph = path_hash(rel)
            row = db.file_by_path(SOURCE, ph)
            if row is None:
                twin = next((g for g in candidates.values() if _same_file(g, fp)), None)
                folder_id = self.folder_id_for(_parent_rel(rel))
                name = rel.rsplit("/", 1)[-1]
                if twin is not None:
                    candidates.pop(int(twin["id"]), None)
                    gone.pop(int(twin["id"]), None)
                    db.update_file(int(twin["id"]), rel_path=rel, path_hash=ph, name=name,
                                   name_folded=fold(name), folder_id=folder_id, deleted_at=None,
                                   status="dirty" if twin.get("status") == "tombstone" else twin.get("status"),
                                   **fp)
                    db.mark_dirty([int(twin["id"])], priority=P_LIVE)
                else:
                    try:
                        db.insert_file(SOURCE, rel, ph, name=name, name_folded=fold(name),
                                       ext=os.path.splitext(name)[1].lower(), folder_id=folder_id,
                                       status="dirty", priority=P_LIVE, birthtime=_birthtime(st), **fp)
                    except Exception as exc:  # noqa: BLE001 — a racing scan inserted it
                        logger.debug(f"[userdocs] insert {rel} skipped: {exc}")
                touched += 1
            elif (
                (row.get("size"), row.get("mtime_ns")) != (fp["size"], fp["mtime_ns"])
                or row.get("status") in ("tombstone", "missing", "deferred")
            ):
                db.update_file(int(row["id"]), deleted_at=None, missing_since=None, **fp)
                db.mark_dirty([int(row["id"])], priority=P_LIVE)
                touched += 1

        for r in gone.values():
            db.update_file(int(r["id"]), status="tombstone", deleted_at=now)
        if touched:
            self.note_queued(touched, "changes")
        if touched or gone:
            self.service.wake()

    # ── folders ────────────────────────────────────────────────────────────

    def folder_id_for(self, rel_dir: str) -> int | None:
        """The folder row for ``rel_dir`` (``""`` = the root, which has none),
        creating the chain of parent rows on the way."""
        if not rel_dir or self.db is None:
            return None
        cached = self._folder_cache.get(rel_dir)
        if cached is not None:
            return cached
        from app.userdocs.discovery.walker import path_hash
        from app.userdocs.textnorm import fold

        parent = self.folder_id_for(_parent_rel(rel_dir))
        name = rel_dir.rsplit("/", 1)[-1]
        fid = self.db.upsert_folder(
            SOURCE, rel_dir, path_hash(rel_dir),
            name=name, name_folded=fold(name), parent_id=parent,
            depth=rel_dir.count("/") + 1, status="live", updated_at=_now(),
        )
        if len(self._folder_cache) > 50_000:
            self._folder_cache.clear()
        self._folder_cache[rel_dir] = fid
        return fid

    # ── scans ──────────────────────────────────────────────────────────────

    def request_scan(self, reason: str) -> None:
        with self.lock:
            self.scan_requested = self.scan_requested or reason
        self.service.wake()

    def scan_due(self, now: float) -> bool:
        if not self.active or self.hold or self.paused_user or self.scanning or self.confirmation:
            return False
        return bool(self.scan_requested) or (self.next_scan_at and now >= self.next_scan_at)

    # ── freshness: research reads the index, so it first asks for it to be current

    def scan_ticket(self, reason: str) -> int:
        """Ask for a full scan; :meth:`scan_done` says when it has run. A scan
        already under way when asked does not count: it may have walked past
        a folder before the file the caller is after appeared in it."""
        with self.lock:
            ticket = self.scans_started + 1
            self.scan_requested = self.scan_requested or reason
        self.service.wake()
        return ticket

    def scan_done(self, ticket: int) -> bool:
        with self.lock:
            return self.scans_finished >= ticket

    def finds_new_files_by_scan(self) -> bool:
        """True when only a scan would notice a new file: polling mode, or a
        native watcher that is gone or has died."""
        with self.lock:
            watcher, mode = self.watcher, self.watch_mode
        return mode == "poll" or watcher is None or not watcher.is_alive()

    def watch_settled(self) -> bool:
        """The watcher has handed over everything it saw, and every path it
        handed over is in the index (as a queued file or a tombstone)."""
        with self.lock:
            watcher = self.watcher
            busy = bool(self._pending_changed or self._pending_removed or self._draining)
        return not busy and (watcher is None or watcher.pending_count() == 0)

    def sync_blocker(self, source: str = SOURCE) -> tuple[str, str | None] | None:
        """``(state, reason)`` as the UI shows it when queued files of
        ``source`` are not being indexed now (sync paused, the folder or
        Drive held, the first sync not confirmed yet); None while that queue
        is worked — the same test the service's workers use
        (``UserDocsService._work_sources``)."""
        if source == uds.SOURCE_DRIVE:
            if self.db is None:
                return self.effective_state()
            return self.drive.work_blocker()
        with self.lock:
            runs = (
                self.active and self.db is not None and not self.paused_user and not self.hold
                and not (self.confirmation and self.confirmation.get("kind") == "first_sync")
            )
        return None if runs else self.effective_state()

    def scan_blocker(self) -> tuple[str, str | None] | None:
        """Like :meth:`sync_blocker`, for scans (:meth:`scan_due`'s test): a
        pending confirmation of any kind holds scans, not only the first sync."""
        with self.lock:
            runs = self.active and not self.hold and not self.paused_user and not self.confirmation
        return None if runs else self.effective_state()

    def indexing_any(self, file_ids: Iterable[int]) -> bool:
        with self.lock:
            return any(int(i) in self.in_flight for i in file_ids)

    def queue_if_changed(self, rows: Iterable[dict[str, Any]]) -> tuple[list[int], list[int]]:
        """Put the out-of-date files among ``rows`` first in the queue, and
        return ``(changed, queued)``: both are what a caller waits for.

        ``changed`` are files whose content on disk is not what was indexed,
        or that are gone (processing turns a vanished file into a tombstone);
        they are queued at P_INTERACTIVE. ``queued`` were already waiting and
        are moved up to it without being queued again, so one being indexed
        right now does not start over.

        Drive files cannot be compared with anything local: Drive's change
        feed is what queues them (a sync, which the caller asks for first).
        Here they are only moved up when already queued.
        """
        from app.userdocs.discovery.hashing import changed_on_disk

        db, root = self.db, self.root
        if db is None:
            return [], []
        changed: list[int] = []
        queued: list[int] = []
        for r in rows:
            source, status = r.get("source"), r.get("status")
            if source not in (SOURCE, uds.SOURCE_DRIVE):
                continue
            if status == "dirty":
                queued.append(int(r["id"]))
            elif (source == SOURCE and root is not None and status not in ("tombstone", "missing")
                  and changed_on_disk(root, r)):
                changed.append(int(r["id"]))
        if changed:
            db.mark_dirty(changed, priority=P_INTERACTIVE)
            self.note_queued(len(changed), "changes")
        if queued:
            db.prioritize(queued, priority=P_INTERACTIVE)
        if changed or queued:
            self.service.wake()
        return changed, queued

    def run_scan(self) -> None:
        """A full reconcile of the folder against the index (scan executor)."""
        from app.userdocs.discovery.guard import RootGuard
        from app.userdocs.discovery.hashing import sha256_file
        from app.userdocs.discovery.scan import scan_diff

        with self.lock:
            if self.scanning:
                return
            self.scanning = True
            reason = self.scan_requested or "reconcile"
            self.scan_requested = None
            self.scans_started += 1
            seq = self.scans_started
        started = time.monotonic()
        try:
            if not self.active or self.root is None or self.db is None:
                return
            db = self.db
            self.progress.set_state("scanning", None)
            manifest = db.load_manifest(SOURCE)
            identity = db.get_source_state(SOURCE).get("root_identity")
            from app.userdocs.deploy_env import docker_root_status

            bind_expected = bool(docker_root_status(self.root).get("bind_expected"))
            verdict = self.guard.check_root(
                self.root, manifest_count=len(manifest),
                root_identity=identity if isinstance(identity, dict) else None,
                docker_bind_expected=bind_expected,
            )
            if not verdict.ok:
                self._set_hold("root_unavailable", {"why": verdict.reason, **(verdict.detail or {})})
                db.add_activity("held", f"Folder unavailable ({verdict.reason}); nothing was removed.",
                                source=SOURCE, level="warning")
                return
            with self.lock:
                if self.hold and self.hold.get("reason") == "root_unavailable":
                    self.hold = None

            def _progress(n: int) -> None:
                self.progress.set_phase(f"scanning ({n} files seen)")

            result = scan_diff(
                self.root, self.matcher, manifest,
                hasher=sha256_file, stop=self.stop_event, on_progress=_progress,
            )
            if result.stopped:
                return
            if verdict.reason and self.guard.device_change_holds(len(result.missing), max(1, len(manifest))):
                self._set_hold("root_unavailable", {"why": "device_changed", **(verdict.detail or {})})
                return
            self._apply_scan(result, total=len(manifest))
            db.update_source_state(
                SOURCE, root_identity=RootGuard.identity_of(self.root),
                last_scan_finished_at=_now(), last_scan_s=time.monotonic() - started,
            )
            if result.truncated:
                db.add_activity("limit_reached", "The folder has more files than can be indexed; "
                                "the rest were skipped. Add exclusions to narrow it down.",
                                source=SOURCE, level="warning")
            logger.info(
                f"[userdocs] {self.profile}: scan ({reason}) new={len(result.new)} "
                f"changed={len(result.changed)} moved={len(result.moves)} missing={len(result.missing)} "
                f"excluded={len(result.excluded)} in {time.monotonic() - started:.1f}s"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[userdocs] {self.profile}: scan failed")
            if self.db is not None:
                self.db.add_activity("error", f"Scan failed: {exc}", source=SOURCE, level="error")
        finally:
            elapsed = time.monotonic() - started
            with self.lock:
                self.scanning = False
                self.scans_finished = max(self.scans_finished, seq)
                self.last_scan_s = elapsed
                interval = (
                    min(900.0, max(30.0, 10 * elapsed)) if self.watch_mode == "poll" else NATIVE_RECONCILE_S
                )
                self.next_scan_at = _now() + interval
            self.progress.set_phase(None)
            self.refresh_totals()
            self._publish_state()
            self.service.wake()

    def _apply_scan(self, result: Any, *, total: int) -> None:
        from app.userdocs.discovery.walker import path_hash
        from app.userdocs.textnorm import fold

        db = self.db
        now = _now()
        self._folder_cache.clear()
        seen_folders: set[int] = set()
        for d in result.dirs:
            fid = self.folder_id_for(d.rel_path)
            if fid is not None:
                seen_folders.add(fid)

        def _fp(e: t.FsEntry) -> dict[str, Any]:
            return {
                "size": int(e.size), "mtime_ns": int(e.mtime_ns), "mtime": e.mtime_ns / 1e9,
                "ino": int(e.ino), "dev": int(e.dev), "birthtime": e.birthtime,
            }

        for row, e in result.moves:
            name = e.rel_path.rsplit("/", 1)[-1]
            db.update_file(row.id, rel_path=e.rel_path, path_hash=path_hash(e.rel_path), name=name,
                           name_folded=fold(name), folder_id=self.folder_id_for(_parent_rel(e.rel_path)),
                           missing_since=None, **_fp(e))
            db.mark_dirty([row.id], priority=P_UPGRADE)

        changed_ids: list[int] = []
        for row, e in result.changed:
            db.update_file(row.id, missing_since=None, deleted_at=None, **_fp(e))
            changed_ids.append(row.id)
        if changed_ids:
            db.mark_dirty(changed_ids, priority=P_BULK)

        for row, e in result.returned:
            db.update_file(row.id, status="indexed", missing_since=None, **_fp(e))

        # Newest first, so a first sync makes recent work searchable soonest.
        for e in sorted(result.new, key=lambda x: -x.mtime_ns):
            name = e.rel_path.rsplit("/", 1)[-1]
            try:
                db.insert_file(
                    SOURCE, e.rel_path, path_hash(e.rel_path),
                    name=name, name_folded=fold(name), ext=os.path.splitext(name)[1].lower(),
                    folder_id=self.folder_id_for(_parent_rel(e.rel_path)),
                    status="dirty", priority=P_BULK, **_fp(e),
                )
            except Exception as exc:  # noqa: BLE001 — a watcher event inserted it first
                logger.debug(f"[userdocs] scan insert {e.rel_path} skipped: {exc}")

        queued = len(result.new) + len(result.changed) + len(result.moves)
        if queued:
            self.note_queued(queued, "sync" if total else "initial_sync")

        if result.excluded:
            self.purge_file_ids([r.id for r in result.excluded])

        missing = [r for r in result.missing if r.status != "missing"]
        if missing:
            bulk = self.guard.check_bulk(len(missing), max(1, total))
            if bulk:
                for r in missing:
                    db.update_file(r.id, status="missing", missing_since=now)
                self._set_confirmation({"kind": "mass_delete", "missing": len(missing), "total": total})
                db.add_activity(
                    "held", f"{len(missing)} of {total} files disappeared at once. They are hidden "
                    "from search until you confirm or reject removing them.",
                    source=SOURCE, level="warning", detail={"missing": len(missing), "total": total},
                )
            else:
                self.purge_file_ids([r.id for r in missing])
                if missing:
                    db.add_activity("removed", f"Removed {len(missing)} deleted files from the index.",
                                    source=SOURCE, detail={"files": len(missing)})

        # Kept-after-a-mass-delete rows that stayed gone long enough.
        expired = [
            int(r["id"]) for r in db.list_files(status="missing", source=SOURCE, limit=5000)
            if r.get("missing_since") and now - float(r["missing_since"]) > MISSING_KEEP_S
        ]
        self.purge_file_ids(expired)

        if not result.truncated:
            stale = [fid for fid in db.folder_ids(SOURCE).values() if fid not in seen_folders]
            if stale:
                self.queue_vector_deletes(db.delete_folders(stale))
        db.refresh_folder_stats(SOURCE)
        self.refresh_project_cards(seen_folders)

    # ── projects ───────────────────────────────────────────────────────────

    def refresh_project_cards(self, folder_ids: Iterable[int], *, limit: int = 2000) -> None:
        """Maintain a searchable card for every folder that looks like a
        project (README, pyproject, package.json, .git, or several source
        files of one language) — what "the robot project folder I worked on
        last year" is answered from."""
        from app.userdocs.chunking import diff_chunks, make_folder_card
        from app.userdocs.discovery.projects import detect_project

        db = self.db
        done = 0
        for fid in folder_ids:
            if done >= limit or self.stop_event.is_set():
                break
            folder = db.get_folder(fid)
            if not folder:
                continue
            files, subs = db.names_in_folder(fid)
            abs_dir = os.path.join(self.root, *folder["rel_path"].split("/"))
            try:
                meta = detect_project(abs_dir, files + subs)
            except Exception:  # noqa: BLE001
                meta = None
            if not meta:
                if folder.get("is_project"):
                    db.upsert_folder(SOURCE, folder["rel_path"], folder["path_hash"], is_project=0,
                                     project_meta=None, card_hash=None)
                    diff = diff_chunks(db.folder_chunks(fid), [])
                    db.apply_chunks(file_id=None, folder_id=fid, source=SOURCE, diff=diff)
                    self.queue_vector_deletes(diff.remove)
                continue
            under = db.files_under(SOURCE, folder["rel_path"], statuses=("indexed", "metadata_only", "dirty"))
            mtimes = [float(r["mtime"]) for r in under if r.get("mtime")]
            readme_head = None
            if meta.get("readme"):
                readme_head = _read_head(os.path.join(abs_dir, meta["readme"]))
            card = make_folder_card(
                name=folder.get("name") or folder["rel_path"],
                rel_path=folder["rel_path"],
                file_count=len(under),
                languages=meta.get("languages") or {},
                markers=meta.get("markers") or [],
                deps=meta.get("deps") or [],
                readme_head=readme_head,
                top_files=sorted(files)[:15],
                activity_min_iso=iso_local(min(mtimes)) if mtimes else None,
                activity_max_iso=iso_local(max(mtimes)) if mtimes else None,
                git_last_commit_iso=iso_local(meta.get("git_last_commit_at")),
            )
            if folder.get("card_hash") == card.text_hash:
                continue
            diff = diff_chunks(db.folder_chunks(fid), [card])
            db.apply_chunks(file_id=None, folder_id=fid, source=SOURCE, diff=diff)
            self.queue_vector_deletes(diff.remove)
            db.upsert_folder(SOURCE, folder["rel_path"], folder["path_hash"], is_project=1,
                             project_meta=meta, card_hash=card.text_hash,
                             git_last_commit_at=meta.get("git_last_commit_at"))
            done += 1
        if done:
            self.service.wake_embedder()

    # ── the per-file pipeline ──────────────────────────────────────────────

    def process(self, row: dict[str, Any]) -> None:
        """Index one dirty file (pipeline worker thread): a folder file here,
        a Drive file by the Drive half — both end in :meth:`index_content`."""
        fid = int(row["id"])
        rel = row["rel_path"]
        name = row.get("name") or rel.rsplit("/", 1)[-1]
        source = row.get("source") or SOURCE
        self.progress.file_started(fid, name=name, rel_path=rel, stage="read")
        try:
            if source == uds.SOURCE_DRIVE:
                outcome, message, reason = self.drive.process(row)
            else:
                outcome, message, reason = self._process(row)
        except Exception as exc:  # noqa: BLE001 — one bad file never stops the queue
            logger.exception(f"[userdocs] {self.profile}: indexing {rel} failed")
            outcome, message, reason = "failed", f"{name}: {exc}", "internal_error"
            self._mark_failed(row, reason, str(exc))
        self.progress.file_finished(fid, outcome, name=name, rel_path=rel, message=message,
                                    reason=reason, fid=row.get("cite_id"))
        if outcome not in ("unchanged",) and self.db is not None:
            level = "error" if outcome == "failed" else "info"
            self.db.add_activity(outcome, message, source=source, level=level, file_id=fid, rel_path=rel)

    def _mark_failed(self, row: dict[str, Any], reason: str, error: str | None) -> None:
        if self.db is None:
            return
        attempts = int(row.get("attempts") or 0) + 1
        nxt = _now() + BACKOFF_S[min(attempts, MAX_ATTEMPTS) - 1] if attempts < MAX_ATTEMPTS else None
        self.db.update_file(int(row["id"]), if_queued_at=row.get("queued_at"), status="error",
                            status_reason=reason, error=(error or "")[:2000], attempts=attempts,
                            next_attempt_at=nxt)

    def _process(self, row: dict[str, Any]) -> tuple[str, str, str | None]:
        from app.userdocs import governor as gov
        from app.userdocs.chunking import CHUNKER_VERSION
        from app.userdocs.discovery.hashing import QUICK_HASH_MIN_SIZE, fs_path, is_placeholder, quick_hash, sha256_file
        from app.userdocs.discovery.ignore import METADATA_ONLY, SKIP
        from app.userdocs.extract import EXTRACTOR_VERSION
        from app.userdocs.kinds import guess_kind

        db = self.db
        if db is None or self.root is None:
            raise RuntimeError("runtime is not configured")
        fid = int(row["id"])
        rel = row["rel_path"]
        name = row.get("name") or rel.rsplit("/", 1)[-1]
        abs_path = os.path.join(self.root, *rel.split("/"))

        try:
            st = os.stat(fs_path(abs_path), follow_symlinks=False)
        except FileNotFoundError:
            db.update_file(fid, if_queued_at=row.get("queued_at"), status="tombstone", deleted_at=_now())
            return "removed", f"{name} was deleted", None
        except PermissionError as exc:
            self._mark_failed(row, "permission_denied", str(exc))
            return "failed", f"{name}: permission denied", "permission_denied"

        disposition = self.matcher.classify(rel, abs_path)
        if disposition == SKIP:
            self.purge_file_ids([fid])
            return "removed", f"{name} is now excluded", None

        is_dir = os.path.isdir(abs_path)
        placeholder = False
        try:
            placeholder = is_placeholder(name, st)
        except Exception:  # noqa: BLE001
            pass

        mime = None
        if is_dir:
            kind, content_allowed = t.KIND_BUNDLE, False
        elif placeholder or disposition == METADATA_ONLY:
            kind, content_allowed = guess_kind(name), False
        else:
            from app.userdocs.extract.detect import detect_file

            kind, mime = detect_file(fs_path(abs_path))
            content_allowed = kind not in t.METADATA_ONLY_KINDS

        fingerprint = {
            "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns), "mtime": float(st.st_mtime),
        }
        sha: str | None = None
        hash_kind: str | None = None
        if not is_dir and not placeholder and disposition != METADATA_ONLY:
            self.progress.file_stage(fid, "hash")
            if content_allowed or st.st_size < QUICK_HASH_MIN_SIZE:
                sha, hash_kind = sha256_file(fs_path(abs_path), stop=self.stop_event), "full"
            else:
                sha, hash_kind = quick_hash(fs_path(abs_path), int(st.st_size)), "quick"
            if sha is None and self.stop_event.is_set():
                return "skipped", f"{name}: stopped", None

        versions_current = (
            row.get("extractor_version") == EXTRACTOR_VERSION and row.get("chunker_version") == CHUNKER_VERSION
        )
        existing = db.get_chunks(fid)
        content_unchanged = bool(
            sha and sha == row.get("sha256") and versions_current
            and row.get("status") in ("indexed", "metadata_only") and existing
        )

        doc_meta: dict[str, Any] = dict(row.get("doc_meta") or {}) if content_unchanged else {}
        exif: dict[str, Any] | None = row.get("exif") if content_unchanged else None
        image: dict[str, Any] | None = None
        body: list[t.Chunk] = []
        status, reason, error = ("indexed" if content_allowed else "metadata_only"), None, None
        if not content_allowed:
            reason = "placeholder" if placeholder else ("secret" if disposition == METADATA_ONLY else kind)
        caption_state = row.get("caption_state")

        if content_unchanged:
            rows = db.chunk_rows([c.id for c in existing])
            body = chunks_from_rows(r for r in rows if r.get("ctype") != t.CTYPE_FILE_CARD)
            status, reason = row.get("status") or status, row.get("status_reason")
        elif content_allowed:
            op = "add_content" if not row.get("sha256") else gov.classify_edit(
                int(st.st_size) - int(row.get("size") or 0))
            try:
                level = gov.Level(self.level)
            except ValueError:
                level = gov.Level.OK
            if not gov.Governor.allows(level, op):
                db.update_file(fid, if_queued_at=row.get("queued_at"), status="deferred",
                               status_reason=str(self.level), **fingerprint)
                return "skipped", f"{name}: waiting for storage space", "deferred"
            twin = next(
                (f for f in (db.files_by_sha(sha) if sha else [])
                 if int(f["id"]) != fid and f.get("status") == "indexed"
                 and f.get("extractor_version") == EXTRACTOR_VERSION
                 and f.get("chunker_version") == CHUNKER_VERSION),
                None,
            )
            if twin is not None:
                twin_rows = db.chunk_rows([c.id for c in db.get_chunks(int(twin["id"]))])
                body = chunks_from_rows(r for r in twin_rows if r.get("ctype") != t.CTYPE_FILE_CARD)
                doc_meta = dict(twin.get("doc_meta") or {})
                exif = twin.get("exif")
                caption_state = twin.get("caption_state")
            else:
                self.progress.file_stage(fid, "extract")
                result = self.service.extract(t.ExtractRequest(
                    name=name, kind=kind, path=fs_path(abs_path),
                    limits=self.service.extract_limits(),
                ), size=int(st.st_size))
                doc_meta = dict(result.doc_meta or {})
                exif = result.exif
                image = result.image
                status, reason, error = extract_outcome(result)
                if sha != row.get("sha256"):
                    # New content: a caption of the old picture says nothing
                    # about this one.
                    caption_state = None
                if status == "indexed":
                    body, caption_state = self._body_from_result(fid, result, doc_meta, caption_state)

        return self._write_file(
            row=row, name=name, rel=rel, kind=kind, mime=mime, sha=sha, hash_kind=hash_kind,
            size=int(st.st_size), mtime=float(st.st_mtime), body=body, existing=existing,
            doc_meta=doc_meta, exif=exif, image=image, status=status, reason=reason, error=error,
            caption_state=caption_state, content_unchanged=content_unchanged,
            extra={"status": "metadata only (content not read)"} if not content_allowed else None,
            source=SOURCE, file_fields=fingerprint, caption_path=fs_path(abs_path),
        )

    # ── the content tail, shared by the folder and Drive ───────────────────

    def index_content(
        self, *, row: dict[str, Any], name: str, rel: str, kind: str, mime: str | None, sha: str | None,
        size: int, result: t.ExtractResult | None, status: str, reason: str | None, error: str | None,
        image: dict[str, Any] | None, exif: dict[str, Any] | None, image_bytes: bytes | None,
        taken_ts: float | None, created_ts: float | None, extra: dict[str, Any] | None, source: str,
        drive_fields: dict[str, Any],
    ) -> tuple[str, str, str | None]:
        """Index content a source already has in hand — the Drive half's way
        into the same tail the folder uses: chunks, legal metadata, scanned
        pages, the image caption (from ``image_bytes``), the file card, the
        chunk diff and the row. ``result`` None indexes the file by its card
        alone (metadata only: any earlier body leaves the index).

        ``drive_fields`` (the Drive columns, ``mtime`` and ``birthtime``) are
        written next to ``size``. Same return contract as the pipeline:
        (event, activity text, error reason)."""
        db = self.db
        if db is None:
            raise RuntimeError("runtime is not configured")
        fid = int(row["id"])
        existing = db.get_chunks(fid)
        doc_meta: dict[str, Any] = dict(result.doc_meta or {}) if result is not None else {}
        # A caption made for these very bytes still stands; new bytes start over.
        caption_state = row.get("caption_state") if (sha and sha == row.get("sha256")) else None
        body: list[t.Chunk] = []
        if result is not None and status == "indexed":
            body, caption_state = self._body_from_result(fid, result, doc_meta, caption_state)
        fields = dict(drive_fields or {})
        fields["size"] = int(size or 0)
        return self._write_file(
            row=row, name=name, rel=rel, kind=kind, mime=mime, sha=sha, hash_kind="full" if sha else None,
            size=int(size or 0), mtime=fields.get("mtime"), body=body, existing=existing, doc_meta=doc_meta,
            exif=exif, image=image, status=status, reason=reason, error=error, caption_state=caption_state,
            content_unchanged=False, extra=extra or None, source=source, file_fields=fields,
            caption_data=image_bytes, taken_ts=taken_ts, created_ts=created_ts,
        )

    def _body_from_result(
        self, fid: int, result: t.ExtractResult, doc_meta: dict[str, Any], caption_state: str | None,
    ) -> tuple[list[t.Chunk], str | None]:
        """The body chunks of a successful extraction: chunked text (legal
        documents by article), then the transcribed scanned pages. Fills
        ``doc_meta`` in place; returns (body, caption state)."""
        from app.userdocs.chunking import chunk_blocks, detect_legal_meta, looks_legal

        self.progress.file_stage(fid, "chunk")
        legal = looks_legal(result.blocks)
        body = chunk_blocks(result.blocks, legal=legal)
        if legal:
            meta = detect_legal_meta(result.blocks)
            if meta:
                doc_meta["legal"] = meta
        if result.ocr_pages:
            doc_meta["scanned_pages"] = [p.get("page") for p in result.ocr_pages]
            ocr_chunks, pending, ocr_state = self._ocr_pages(result.ocr_pages, start_ordinal=len(body))
            body += ocr_chunks
            if pending:
                doc_meta["ocr_pending_pages"] = pending
                caption_state = ocr_state
            else:
                doc_meta.pop("ocr_pending_pages", None)
                caption_state = "done" if ocr_chunks else caption_state
        return body, caption_state

    def _write_file(
        self, *, row: dict[str, Any], name: str, rel: str, kind: str, mime: str | None, sha: str | None,
        hash_kind: str | None, size: int, mtime: float | None, body: list[t.Chunk], existing: list[Any],
        doc_meta: dict[str, Any], exif: dict[str, Any] | None, image: dict[str, Any] | None, status: str,
        reason: str | None, error: str | None, caption_state: str | None, content_unchanged: bool,
        extra: dict[str, Any] | None, source: str, file_fields: dict[str, Any],
        caption_path: str | None = None, caption_data: bytes | None = None,
        taken_ts: float | None = None, created_ts: float | None = None,
    ) -> tuple[str, str, str | None]:
        """Caption, card, chunk diff, row: the end of every file's indexing."""
        from app.userdocs.chunking import CHUNKER_VERSION, diff_chunks, make_file_card
        from app.userdocs.extract import EXTRACTOR_VERSION

        db = self.db
        if db is None:
            raise RuntimeError("runtime is not configured")
        fid = int(row["id"])
        newly_captioned = False
        if kind == t.KIND_IMAGE and status == "indexed" and caption_state != "done":
            # An image is found by its name, folder, date and camera from the
            # moment it is indexed; the caption adds what it *shows*, when the
            # Specialized Vision Model, consent and today's quota allow.
            self.progress.file_stage(fid, "caption")
            caption_state, caption_chunk = self._caption_image(
                rel=rel, sha=sha, abs_path=caption_path, data=caption_data, image=image, exif=exif, size=size,
            )
            if caption_chunk is not None:
                body = [c for c in body if c.ctype != t.CTYPE_CAPTION] + [caption_chunk]
                newly_captioned = True

        if taken_ts is None:
            taken_ts = ts_from_iso((exif or {}).get("taken_at"))
        if created_ts is None:
            created_ts = ts_from_iso(doc_meta.get("created"))
        camera = " ".join(x for x in ((exif or {}).get("make"), (exif or {}).get("model")) if x) or None
        summary = body[0].text if body else None
        card = make_file_card(
            name=name, rel_path=rel, kind=kind, size=size,
            mtime_iso=iso_local(mtime) or "",
            title=doc_meta.get("title"), author=doc_meta.get("author"),
            created_iso=iso_local(created_ts), taken_iso=iso_local(taken_ts), camera=camera,
            summary_text=summary,
            extra=extra,
        )
        new_chunks = [card] + body
        diff = diff_chunks(existing, new_chunks)

        fields: dict[str, Any] = {
            "kind": kind, "mime": mime, "sha256": sha, "hash_kind": hash_kind,
            "doc_meta": doc_meta or None, "exif": exif,
            "is_camera_photo": 1 if (image or {}).get("is_camera_photo") or (camera and taken_ts) else 0,
            "taken_at": taken_ts, "doc_created_at": created_ts,
            "extractor_version": EXTRACTOR_VERSION, "chunker_version": CHUNKER_VERSION,
            "caption_state": caption_state,
            **file_fields,
        }
        self.progress.file_stage(fid, "index", {"done": 0, "total": len(diff.add)})
        try:
            db.apply_chunks(file_id=fid, folder_id=row.get("folder_id"), source=source, diff=diff,
                            file_fields=fields)
        except LookupError:
            return "skipped", f"{name} changed while it was being indexed", None
        self.queue_vector_deletes(diff.remove)

        now = _now()
        if status == "error":
            attempts = int(row.get("attempts") or 0) + 1
            nxt = now + BACKOFF_S[min(attempts, MAX_ATTEMPTS) - 1] if attempts < MAX_ATTEMPTS else None
            db.update_file(fid, if_queued_at=row.get("queued_at"), status="error", status_reason=reason,
                           error=(error or "")[:2000], attempts=attempts, next_attempt_at=nxt)
        else:
            db.update_file(fid, if_queued_at=row.get("queued_at"), status=status, status_reason=reason,
                           error=None, attempts=0, next_attempt_at=None, indexed_at=now)
        self.service.wake_embedder()

        total = len(new_chunks)
        if status == "error":
            return "failed", f"{name}: could not be read ({reason})", reason
        if content_unchanged:
            if newly_captioned:
                return "captioned", f"{name}: caption added", None
            if not diff.add:
                return "unchanged", f"{name} is unchanged", None
            return "moved", f"{name}: location or details updated", None
        if not row.get("sha256"):
            if status == "metadata_only":
                return "metadata_only", f"{name} indexed by name and details only ({reason})", reason
            return "added", f"{name} indexed ({total} chunks)", None
        return "updated", f"{name} — {len(diff.add)} of {total} chunks re-embedded", None

    # ── vision: captions and scanned pages ─────────────────────────────────

    def vision_gate(self) -> tuple[Any, str | None]:
        """(resolution, blocking state) — the state is None when a vision call
        may be made right now (model chosen, consent recorded for it)."""
        from app.userdocs.vision import resolver

        res = resolver.resolve_dedicated_vision(self.profile)
        if not res.ok:
            return res, "awaiting_vision"
        opts = uds.normalize_options(self.settings.get("options"))
        if not resolver.consent_matches(opts, res):
            return res, "awaiting_consent"
        return res, None

    def caption_cap(self) -> int:
        from app.userdocs.vision import captioner

        return captioner.daily_cap(uds.normalize_options(self.settings.get("options")))

    def _caption_image(
        self, *, rel: str, sha: str | None, abs_path: str | None = None, data: bytes | None = None,
        load: Callable[[], bytes | None] | None = None, image: dict[str, Any] | None,
        exif: dict[str, Any] | None, size: int,
    ) -> tuple[str, t.Chunk | None]:
        """Caption one image from a file (``abs_path``), bytes in hand
        (``data``) or bytes fetched only once a vision call will really be
        made (``load``) — a cached caption or an ineligible image costs no
        download."""
        from app.storage.userdocs_storage import get_userdocs_storage
        from app.userdocs.chunking import make_caption_chunk
        from app.userdocs.vision import captioner, resolver

        storage = get_userdocs_storage()
        if sha:
            cached = storage.get_caption(self.profile, sha)
            if cached and cached.get("caption_text"):
                return "done", make_caption_chunk(cached["caption_text"])
        opts = uds.normalize_options(self.settings.get("options"))
        width, height = captioner.image_dims(image, exif)
        ok, why = captioner.eligible(width=width, height=height, size_bytes=size, rel_path=rel, options=opts)
        if not ok:
            return why or "skipped_small", None
        res, blocked = self.vision_gate()
        if blocked:
            return blocked, None
        day = captioner.local_day(self.profile)
        if not storage.reserve_vision(self.profile, day, self.caption_cap()):
            return "over_cap", None
        try:
            if data is None and abs_path is None and load is not None:
                data = load()
            if data is not None:
                jpeg = captioner.prepare_jpeg(data=data)
            elif abs_path is not None:
                jpeg = captioner.prepare_jpeg(path=abs_path)
            else:
                raise ValueError("no image to describe")
            llm = resolver.build_vision_llm(self.profile, res)
            out = captioner.run_vision(llm, self.profile, jpeg, mode="image", exif=exif)
        except Exception as exc:  # noqa: BLE001 — a failed call must not eat the quota
            storage.refund_vision(self.profile, day)
            logger.warning(f"[userdocs] {self.profile}: captioning {rel} failed: {exc}")
            return "failed", None
        if not out.text.strip():
            return "failed", None
        storage.add_vision_tokens(self.profile, day, out.tokens_in + out.tokens_out)
        if sha:
            storage.put_caption(
                self.profile, sha, variant="image", caption_text=out.text, caption_json=out.data,
                provider=out.provider, model=out.model, prompt_version=captioner.PROMPT_VERSION,
                tokens_in=out.tokens_in, tokens_out=out.tokens_out,
            )
        return "done", make_caption_chunk(out.text)

    def _ocr_pages(
        self, pages: list[dict[str, Any]], *, start_ordinal: int,
    ) -> tuple[list[t.Chunk], list[int], str | None]:
        """Transcribe scanned PDF pages with the vision model. Returns (chunks,
        page numbers still waiting, why they wait)."""
        import base64

        from app.storage.userdocs_storage import get_userdocs_storage
        from app.userdocs.chunking import make_ocr_chunks
        from app.userdocs.vision import captioner, resolver

        storage = get_userdocs_storage()
        chunks: list[t.Chunk] = []
        pending: list[int] = []
        state: str | None = None
        res, blocked = self.vision_gate()
        llm = None
        day = captioner.local_day(self.profile)
        for page in sorted(pages, key=lambda p: int(p.get("page") or 0)):
            num = int(page.get("page") or 0)
            key = str(page.get("sha256") or "")
            cached = storage.get_caption(self.profile, key) if key else None
            text = cached.get("caption_text") if cached else None
            if text is None:
                if blocked:
                    pending.append(num)
                    state = blocked
                    continue
                if not storage.reserve_vision(self.profile, day, self.caption_cap(), ocr=True):
                    pending.append(num)
                    state = "over_cap"
                    continue
                try:
                    png = base64.b64decode(page.get("png_b64") or "")
                    jpeg = captioner.prepare_jpeg(data=png, max_side=captioner.OCR_MAX_SIDE)
                    llm = llm or resolver.build_vision_llm(self.profile, res)
                    out = captioner.run_vision(llm, self.profile, jpeg, mode="ocr")
                except Exception as exc:  # noqa: BLE001
                    storage.refund_vision(self.profile, day, ocr=True)
                    logger.warning(f"[userdocs] {self.profile}: OCR of page {num} failed: {exc}")
                    pending.append(num)
                    state = "failed"
                    continue
                text = out.text
                storage.add_vision_tokens(self.profile, day, out.tokens_in + out.tokens_out)
                if key:
                    storage.put_caption(self.profile, key, variant="ocr", caption_text=text, provider=out.provider,
                                        model=out.model, prompt_version=captioner.PROMPT_VERSION,
                                        tokens_in=out.tokens_in, tokens_out=out.tokens_out)
            if text and text.strip():
                made = make_ocr_chunks(num, text, start_ordinal + len(chunks))
                chunks += made
        return chunks, pending, state

    def requeue_waiting_vision(self) -> int:
        """Re-queue images (and scanned PDFs) that are waiting for a vision
        model, consent or tomorrow's quota, once what they wait for is there.
        Called by the service's housekeeping; cheap when nothing waits.
        Covers the folder and Drive alike (a Drive image is downloaded again
        for its caption); the caption options are the profile's, on the
        local row."""
        from app.storage.userdocs_storage import get_userdocs_storage
        from app.userdocs.vision import captioner

        sources = self.syncing_sources()
        if self.db is None or not sources or self.paused_user:
            return 0
        opts = uds.normalize_options(self.settings.get("options"))
        if not (opts.get("caption") or {}).get("enabled", True):
            self.progress.set_vision(ready=False, reason="captions_off", waiting=0)
            return 0
        # Images indexed while descriptions were off wait too, once they are on.
        waiting = self.db.read_sql(
            f"SELECT id, kind FROM files WHERE source IN ({', '.join('?' * len(sources))}) AND caption_state IN "
            "('awaiting_vision', 'awaiting_consent', 'over_cap', 'captions_off') "
            "AND status = 'indexed' ORDER BY COALESCE(taken_at, mtime) DESC LIMIT 5000",
            tuple(sources),
        )
        _res, blocked = self.vision_gate()
        usage = get_userdocs_storage().vision_usage(self.profile, captioner.local_day(self.profile))
        cap = self.caption_cap()
        self.progress.set_vision(
            ready=blocked is None, reason=blocked, waiting=len(waiting),
            quota={"used": usage["captions"], "cap": cap},
        )
        if not waiting or blocked:
            return 0
        room = max(0, cap - int(usage["captions"]))
        if room <= 0:
            return 0
        ids = [int(r["id"]) for r in waiting[:room]]
        for r in waiting[:room]:
            if r.get("kind") != t.KIND_IMAGE:
                # Scanned pages need their page images again: re-extract.
                self.db.update_file(int(r["id"]), extractor_version=None)
        self.db.mark_dirty(ids, priority=P_UPGRADE + 1)
        return len(ids)

    # ── removal ────────────────────────────────────────────────────────────

    def queue_vector_deletes(self, chunk_ids: Iterable[int]) -> None:
        ids = [int(i) for i in chunk_ids]
        if not ids:
            return
        with self.lock:
            self.pending_vector_deletes.extend(ids)
        self.service.wake_embedder()

    def take_vector_deletes(self) -> list[int]:
        with self.lock:
            ids, self.pending_vector_deletes = self.pending_vector_deletes, []
        return ids

    def purge_file_ids(self, file_ids: Iterable[int]) -> int:
        n = 0
        if self.db is None:
            return 0
        for fid in list(file_ids):
            try:
                self.queue_vector_deletes(self.db.delete_file(int(fid)))
                n += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[userdocs] purge {fid} failed: {exc}")
        return n

    def purge_tombstones(self, now: float) -> int:
        if self.db is None:
            return 0
        expired = [
            int(r["id"]) for r in self.db.list_files(status="tombstone", source=SOURCE, limit=5000)
            if not r.get("deleted_at") or now - float(r["deleted_at"]) >= TOMBSTONE_GRACE_S
        ]
        n = self.purge_file_ids(expired)
        if n:
            self.db.add_activity("removed", f"Removed {n} deleted file{'s' if n != 1 else ''} from the index.",
                                 source=SOURCE, detail={"files": n})
            self.refresh_totals()
        return n

    # ── confirmations ──────────────────────────────────────────────────────

    def confirm_deletions(self) -> int:
        if self.db is None:
            return 0
        ids = [int(r["id"]) for r in self.db.list_files(status="missing", source=SOURCE, limit=1_000_000)]
        n = self.purge_file_ids(ids)
        self._set_confirmation(None)
        self.db.add_activity("removed", f"Removed {n} vanished files from the index (confirmed).",
                             source=SOURCE, detail={"files": n})
        self.refresh_totals()
        return n

    def reject_deletions(self) -> None:
        self._set_confirmation(None)
        if self.db is not None:
            self.db.add_activity("kept", "Kept the vanished files; they stay hidden and are re-checked "
                                 "on every scan for 14 days.", source=SOURCE)

    # ── estimate ───────────────────────────────────────────────────────────

    def run_estimate(self) -> dict[str, Any]:
        """Stat-only walk: what a full sync would cost (scan executor)."""
        from app.userdocs import governor as gov
        from app.userdocs.discovery.ignore import IgnoreMatcher, METADATA_ONLY
        from app.userdocs.discovery.walker import walk
        from app.userdocs.kinds import guess_kind

        row = self.settings or {}
        custom = row.get("root_mode") == uds.ROOT_CUSTOM
        check = uds.validate_root(row.get("root_path") if custom else None, is_admin=self.profile == "admin")
        est: dict[str, Any] = {"state": "running", "root": check.path, "started_at": _now() * 1000}
        with self.lock:
            self.estimate = est
            self.estimating = True
        self.progress.set_state("estimating", None)
        try:
            if not check.ok:
                est.update(state="error", error=check.message)
                return est
            matcher = IgnoreMatcher(
                check.path, excludes=uds.normalize_excludes(row.get("excludes")),
                locked_excludes=check.locked_excludes, system_dir=uds.system_dir(),
            )
            by_kind: dict[str, int] = {}
            files = dirs = total_bytes = chunks = images = placeholders = 0
            opts = uds.normalize_options(row.get("options"))
            min_bytes = int(opts["caption"]["min_kb"]) * 1024
            for e in walk(check.path, matcher, stop=self.stop_event):
                if e.is_dir:
                    dirs += 1
                    if dirs % 500 == 0:
                        self.progress.set_phase(f"estimating ({files} files)")
                    continue
                files += 1
                total_bytes += int(e.size)
                kind = guess_kind(e.rel_path.rsplit("/", 1)[-1])
                by_kind[kind] = by_kind.get(kind, 0) + 1
                if e.placeholder:
                    placeholders += 1
                if e.disposition == METADATA_ONLY or kind in t.METADATA_ONLY_KINDS or e.placeholder:
                    chunks += 1
                    continue
                if kind == t.KIND_IMAGE and e.size >= min_bytes:
                    images += 1
                chunks += gov.estimate_chunks(kind, int(e.size)) + 1
            backend = _vector_backend()
            size = gov.estimate_bytes(chunks, backend=backend)
            est.update(
                state="done", files=files, dirs=dirs, bytes=total_bytes, by_kind=by_kind,
                images_to_caption=images, placeholders=placeholders, chunks=chunks,
                index_bytes=size["total"], seconds=int(files * 0.05 + chunks / 25),
                finished_at=_now() * 1000,
            )
            if self.stop_event.is_set():
                est["state"] = "stopped"
            return est
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[userdocs] {self.profile}: estimate failed")
            est.update(state="error", error=str(exc))
            return est
        finally:
            with self.lock:
                self.estimating = False
            self.progress.set_phase(None)
            if self.db is not None:
                try:
                    self.db.update_source_state(SOURCE, estimate=est)
                except Exception:  # noqa: BLE001
                    pass

    # ── snapshot ───────────────────────────────────────────────────────────

    def local_on(self) -> bool:
        """The local folder is switched on (it may still be held or waiting)."""
        return bool(self.active or self.settings.get("enabled"))

    def syncing_sources(self) -> list[str]:
        """The sources this runtime keeps in sync right now: the folder while
        it is active, Drive while it is on (held or not — a held Drive keeps
        its index and its queue)."""
        out = [SOURCE] if self.active else []
        if self.drive.enabled:
            out.append(uds.SOURCE_DRIVE)
        return out

    def _drive_state(self) -> tuple[str | None, str | None]:
        try:
            view = self.drive.view()
        except Exception:  # noqa: BLE001 — the snapshot must never fail on Drive
            return None, None
        return view.get("state"), view.get("reason")

    def _drive_can_work(self) -> bool:
        try:
            return bool(self.drive.work_allowed())
        except Exception:  # noqa: BLE001
            return False

    def _drive_hidden(self) -> bool:
        """Drive's results are hidden from search (a revoked or unlinked
        account), so its files do not count in the totals either."""
        from app.userdocs.query.filters import HIDDEN_HOLD_REASONS

        state, reason = self._drive_state()
        return state == "hold" and reason in HIDDEN_HOLD_REASONS

    def refresh_totals(self) -> None:
        """Per-status file counts of the sources that are on — the folder's
        and Drive's together (a hidden Drive's are left out)."""
        if self.db is None:
            return
        drive_on = self.drive.enabled
        try:
            local = self.db.count_by_status(SOURCE) if (self.local_on() or not drive_on) else {}
            drive = self.db.count_by_status(uds.SOURCE_DRIVE) if drive_on else {}
        except Exception:  # noqa: BLE001
            return
        with self.lock:
            self._dirty = {SOURCE: int(local.get("dirty", 0)), uds.SOURCE_DRIVE: int(drive.get("dirty", 0))}
        stages = dict(local)
        if drive and not self._drive_hidden():
            for k, v in drive.items():
                stages[k] = stages.get(k, 0) + int(v)
        try:
            self.progress.set_totals(stages)
            self.drive.refresh_counts()
        except Exception:  # noqa: BLE001
            pass

    def note_queued(self, n: int, label: str) -> None:
        """Count newly queued files into the visible batch (for "3,120/12,840
        files · ~30 min left"); a batch opens on the first and closes when
        the queue drains (:meth:`maybe_end_batch`)."""
        if self.progress.batch.get("label"):
            self.progress.add_to_batch(n)
        else:
            self.progress.begin_batch(label, n)

    def maybe_end_batch(self) -> None:
        if not self.progress.batch.get("label"):
            return
        with self.lock:
            busy = bool(self.in_flight) or self.scanning
        if busy or self.db is None or self._drive_state()[0] == "syncing":
            return
        waiting = 0
        if self.local_on() or not self.drive.enabled:
            waiting += int(self.db.count_by_status(SOURCE).get("dirty", 0))
        if self._drive_can_work():
            # A held Drive's queue does not keep the batch open: nothing
            # would drain it until the hold clears.
            waiting += int(self.db.count_by_status(uds.SOURCE_DRIVE).get("dirty", 0))
        if not waiting:
            self.progress.end_batch()

    def pending_count(self) -> int:
        """Files waiting that can be worked on: the folder's, and Drive's
        while Drive may work."""
        with self.lock:
            dirty = dict(self._dirty)
        n = int(dirty.get(SOURCE, 0))
        if dirty.get(uds.SOURCE_DRIVE) and self._drive_can_work():
            n += int(dirty[uds.SOURCE_DRIVE])
        return n

    def _publish_state(self) -> None:
        state, reason = self.effective_state()
        self.progress.set_state(state, reason, (self.hold or {}).get("detail") if self.hold else None)

    def effective_state(self) -> tuple[str, str | None]:
        """Precedence: hold > confirmation > user pause > storage pause >
        re-embed > scanning/estimating > indexing > idle.

        Holds and confirmations here are the local folder's; Drive reports
        its own in ``snapshot["drive"]``. With only Drive on, the state is
        what the engine is doing for it — never "disabled"."""
        if self.suspended:
            return "suspended", self.suspended
        local_on = self.local_on()
        drive_on = self.drive.enabled
        if not local_on and not drive_on:
            return "disabled", None
        if local_on:
            if self.hold:
                return "hold", self.hold.get("reason")
            if self.confirmation:
                return "awaiting_confirmation", self.confirmation.get("kind")
            if self.estimating:
                return "estimating", None
        if self.paused_user:
            return "paused", "user"
        if self.level in ("budget", "disk_low", "disk_critical"):
            return "paused", self.level
        if self.scanning or (drive_on and self._drive_state()[0] == "syncing"):
            return "scanning", None
        reembed = self.progress.reembed
        if reembed.get("total") and reembed.get("done", 0) < reembed.get("total", 0):
            return "reembedding", None
        if self.pending_count() > 0 or self.in_flight:
            return "indexing", None
        return "idle", None

    def runtime_snapshot(self) -> dict[str, Any]:
        state, reason = self.effective_state()
        snap = self.progress.snapshot()
        snap["state"], snap["reason"] = state, reason
        if self.hold:
            snap["detail"] = self.hold.get("detail")
        snap["confirmation"] = self.confirmation
        snap["watch"] = {"mode": self.watch_mode, "reason": self.watch_reason}
        snap["docker"] = self.docker_view()
        coverage = snap.get("vector_coverage_pct")
        snap["tool_mode"] = "normal" if coverage in (None, 100.0) else "partial"
        try:
            snap["drive"] = self.drive.view()
        except Exception:  # noqa: BLE001
            snap["drive"] = {"enabled": False, "state": "disabled", "reason": None}
        return snap


def _birthtime(st: os.stat_result) -> float | None:
    bt = getattr(st, "st_birthtime", None)
    if bt:
        return float(bt)
    if os.name == "nt":
        return float(st.st_ctime)  # creation time on Windows
    return None


def _read_head(path: str, limit: int = 2000) -> str | None:
    try:
        from app.userdocs.discovery.hashing import read_text_head

        return read_text_head(path, limit)
    except Exception:  # noqa: BLE001
        return None


def _vector_backend() -> str:
    try:
        from app.config.settings import BaseConfig

        return "chroma" if (BaseConfig.get_vectorstore_provider() or "").lower() == "chroma" else "qdrant"
    except Exception:  # noqa: BLE001
        return "qdrant"
