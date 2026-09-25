"""The User Document Search engine: threads, lifecycle, and the API facade.

One :class:`UserDocsService` per server process, holding a
:class:`~app.userdocs.runtime.ProfileRuntime` per profile that has the feature
on. Its threads:

- **maintenance** (1): applies settings changes, drains watcher events, starts
  scans when due, removes tombstones, measures storage. Every hook called from
  elsewhere (settings saved, embedding changed, purge requested) only queues a
  command here, so no caller — including the event loop — ever waits on it.
- **pipeline workers** (``userdocs.workers``, default 2): take dirty files
  round-robin across profiles, so one profile's 50,000-file first sync cannot
  starve another profile's single edit.
- **embedder** (1): fills in vectors (:mod:`app.userdocs.vector_sync`).
- **scan executor** (2): full scans and estimates, which can take minutes on a
  large tree and must not hold up the maintenance loop.

Nothing here blocks the event loop: the API calls the facade methods through
``asyncio.to_thread``, and :meth:`stop` returns within its budget even with a
hung extractor (the pool kills it).
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.userdocs import settings as uds
from app.userdocs import state as uds_state
from app.userdocs.errors import EngineError, NotEnabled, NotFound, UnknownAction
from app.userdocs.runtime import P_BULK, P_INTERACTIVE, P_UPGRADE, SOURCE, ProfileRuntime
from app.utils.logger import logger

MB = 1024 * 1024
GOVERNOR_INTERVAL_S = 60.0
GOVERNOR_IDLE_INTERVAL_S = 600.0
HOUSEKEEPING_INTERVAL_S = 60.0
GC_INTERVAL_S = 7 * 86400.0

_service: "UserDocsService | None" = None


def get_service() -> "UserDocsService | None":
    return _service


def userdocs_root() -> str:
    return os.path.join(uds.system_dir(), "storage", "userdocs")


class UserDocsService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runtimes: dict[str, ProfileRuntime] = {}
        self._stop = threading.Event()
        self._cv = threading.Condition()
        self._embed_wake = threading.Event()
        self._commands: "queue.Queue[tuple[Any, ...]]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._scan_exec = ThreadPoolExecutor(max_workers=2, thread_name_prefix="userdocs-scan")
        self._pool: Any = None
        self._pool_lock = threading.Lock()
        self._rr = 0
        self._last_governor = 0.0
        self._last_housekeeping = 0.0
        self._last_gc = 0.0
        self._enospc_at = 0.0
        from app.userdocs import governor as gov

        policy = uds.read_admin_policy()
        self.governor = gov.Governor(
            global_budget_bytes=policy.storage_budget_mb * MB,
            profile_budget_bytes=policy.per_profile_budget_mb * MB,
        )

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        from app.config.embedding_state import add_listener
        from app.userdocs.extract.pool import wipe_stale_tmp

        tmp_root = os.path.join(userdocs_root(), "tmp")
        try:
            wipe_stale_tmp(tmp_root)
        except Exception:  # noqa: BLE001
            pass
        uds_state.set_runtime_provider(self.runtime_snapshot)
        uds_state.add_settings_listener(self._on_settings_changed)
        uds_state.set_purge_handler(self._on_purge)
        uds.set_effect_counter(self.count_effect)
        add_listener(self._on_embedding)

        workers = max(1, min(8, uds.read_admin_policy().workers))
        specs = [("userdocs-maint", self._maintenance), ("userdocs-embed", self._embedder)]
        specs += [(f"userdocs-work-{i}", self._worker) for i in range(workers)]
        for name, fn in specs:
            th = threading.Thread(target=self._guarded(fn), name=name, daemon=True)
            th.start()
            self._threads.append(th)
        self.command("boot")
        logger.info(f"[userdocs] engine started ({workers} pipeline workers)")

    def _guarded(self, fn):
        def run() -> None:
            while not self._stop.is_set():
                try:
                    fn()
                    return
                except Exception:  # noqa: BLE001 — a crashed loop restarts, never dies silently
                    logger.exception(f"[userdocs] {threading.current_thread().name} crashed; restarting")
                    time.sleep(1.0)
        return run

    def stop(self, budget_s: float = 1.5) -> None:
        from app.config.embedding_state import remove_listener

        deadline = time.monotonic() + budget_s
        self._stop.set()
        self.wake()
        self._embed_wake.set()
        try:
            remove_listener(self._on_embedding)
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            runtimes = list(self._runtimes.values())
        for rt in runtimes:
            rt.stop_event.set()
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.close()
            except Exception:  # noqa: BLE001
                pass
        self._scan_exec.shutdown(wait=False, cancel_futures=True)
        for th in self._threads:
            th.join(timeout=max(0.0, deadline - time.monotonic()))
        for rt in runtimes:
            rt.close()
        uds_state.set_runtime_provider(None)
        uds_state.set_purge_handler(None)
        uds.set_effect_counter(None)

    # ── wake-ups and commands ──────────────────────────────────────────────

    def wake(self) -> None:
        with self._cv:
            self._cv.notify_all()

    def wake_embedder(self) -> None:
        self._embed_wake.set()

    def command(self, *cmd: Any) -> None:
        self._commands.put(cmd)
        self.wake()

    def _on_settings_changed(self, profile: str, kind: str) -> None:
        self.command("configure", profile)

    def _on_purge(self, profile: str, kind: str) -> None:
        self.command("purge", profile, kind)

    def _on_embedding(self, status: Any, embedding: Any, vector_store: Any) -> None:
        # May run on the event loop: queue and return.
        self.command("configure_all")
        self.wake_embedder()

    # ── runtimes ───────────────────────────────────────────────────────────

    def runtime(self, profile: str, *, create: bool = False) -> ProfileRuntime | None:
        with self._lock:
            rt = self._runtimes.get(profile)
            if rt is not None or not create:
                return rt
        from app.storage.userdocs_storage import get_userdocs_storage

        uid = get_userdocs_storage().profile_uid(profile)
        if not uid:
            return None
        with self._lock:
            rt = self._runtimes.get(profile)
            if rt is None:
                rt = ProfileRuntime(self, profile, uid)
                self._runtimes[profile] = rt
            return rt

    def runtime_snapshot(self, profile: str) -> dict[str, Any] | None:
        rt = self.runtime(profile)
        return rt.runtime_snapshot() if rt is not None else None

    def _require(self, profile: str, *, open_index: bool = True) -> ProfileRuntime:
        from app.userdocs.index import index_path

        rt = self.runtime(profile, create=True)
        if rt is None:
            raise NotFound("Unknown profile.")
        if open_index:
            if rt.db is None and not os.path.exists(index_path(rt.uid)):
                raise NotEnabled("User Document Search has no index for this profile yet.")
            rt.ensure_db()
        return rt

    # ── maintenance loop ───────────────────────────────────────────────────

    def _maintenance(self) -> None:
        while not self._stop.is_set():
            while True:
                try:
                    cmd = self._commands.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._run_command(cmd)
                except Exception:  # noqa: BLE001
                    logger.exception(f"[userdocs] command {cmd[0]} failed")
            now = time.time()
            with self._lock:
                runtimes = list(self._runtimes.values())
            for rt in runtimes:
                if self._stop.is_set():
                    return
                try:
                    if rt.has_pending_paths():
                        rt.drain_watch_paths()
                    if rt.scan_due(now):
                        self._scan_exec.submit(rt.run_scan)
                except Exception:  # noqa: BLE001
                    logger.exception(f"[userdocs] {rt.profile}: maintenance step failed")
            busy = any(rt.active and (rt.pending_count() or rt.scanning) for rt in runtimes)
            interval = GOVERNOR_INTERVAL_S if busy else GOVERNOR_IDLE_INTERVAL_S
            if now - self._last_governor >= interval:
                self._last_governor = now
                self._governor_tick(runtimes)
            if now - self._last_housekeeping >= HOUSEKEEPING_INTERVAL_S:
                self._last_housekeeping = now
                for rt in runtimes:
                    try:
                        rt.purge_tombstones(now)
                        rt.refresh_totals()
                    except Exception:  # noqa: BLE001
                        logger.exception(f"[userdocs] {rt.profile}: housekeeping failed")
            if now - self._last_gc >= GC_INTERVAL_S:
                self._last_gc = now
                self._scan_exec.submit(self._gc_all)
            with self._cv:
                if self._commands.empty():
                    self._cv.wait(timeout=2.0)

    def _run_command(self, cmd: tuple[Any, ...]) -> None:
        name = cmd[0]
        if name == "boot":
            self._boot()
        elif name == "configure":
            rt = self.runtime(cmd[1], create=True)
            if rt is not None:
                rt.configure()
        elif name == "configure_all":
            from app.storage.userdocs_storage import get_userdocs_storage

            profiles = {r["profile"] for r in get_userdocs_storage().list_sources()}
            with self._lock:
                profiles |= set(self._runtimes)
            for p in sorted(profiles):
                rt = self.runtime(p, create=True)
                if rt is not None:
                    rt.configure()
        elif name == "purge":
            self._purge(cmd[1], cmd[2])
        elif name == "after_estimate":
            rt = self.runtime(cmd[1])
            if rt is not None:
                rt.configure()

    def _boot(self) -> None:
        from app.storage.userdocs_storage import get_userdocs_storage

        rows = get_userdocs_storage().list_sources()
        for p in sorted({r["profile"] for r in rows if r.get("enabled")}):
            rt = self.runtime(p, create=True)
            if rt is None:
                continue
            try:
                rt.configure()
                if rt.db is not None:
                    rt.db.reset_running_tasks()
                    rt.refresh_totals()
            except Exception:  # noqa: BLE001
                logger.exception(f"[userdocs] {p}: boot configure failed")
        self._scan_exec.submit(self._gc_orphan_indexes)

    # ── pipeline workers ───────────────────────────────────────────────────

    def _next_work(self) -> tuple[ProfileRuntime, dict[str, Any]] | None:
        with self._lock:
            runtimes = [
                rt for rt in self._runtimes.values()
                if rt.active and rt.db is not None and not rt.paused_user and not rt.hold
                and not (rt.confirmation and rt.confirmation.get("kind") == "first_sync")
            ]
            if not runtimes:
                return None
            self._rr = (self._rr + 1) % len(runtimes)
            order = runtimes[self._rr:] + runtimes[:self._rr]
        now = time.time()
        for rt in order:
            with rt.lock:
                exclude = set(rt.in_flight)
            try:
                rows = rt.db.next_work(limit=1, now=now, exclude_ids=exclude)
            except Exception:  # noqa: BLE001
                continue
            if not rows:
                continue
            row = rows[0]
            fid = int(row["id"])
            with rt.lock:
                if fid in rt.in_flight:
                    continue
                rt.in_flight.add(fid)
            return rt, row
        return None

    def _worker(self) -> None:
        while not self._stop.is_set():
            item = self._next_work()
            if item is None:
                with self._cv:
                    self._cv.wait(timeout=2.0)
                continue
            rt, row = item
            try:
                rt.process(row)
            finally:
                with rt.lock:
                    rt.in_flight.discard(int(row["id"]))
                if not rt.in_flight:
                    rt.refresh_totals()

    # ── embedder ───────────────────────────────────────────────────────────

    def _embedder(self) -> None:
        from app.userdocs import vector_sync

        while not self._stop.is_set():
            with self._lock:
                runtimes = list(self._runtimes.values())
            did = 0
            for rt in runtimes:
                if self._stop.is_set():
                    return
                try:
                    did += vector_sync.step(rt)
                except Exception:  # noqa: BLE001
                    logger.exception(f"[userdocs] {rt.profile}: embedding step failed")
            if not did:
                self._embed_wake.wait(timeout=5.0)
                self._embed_wake.clear()

    # ── extraction ─────────────────────────────────────────────────────────

    def _get_pool(self):
        with self._pool_lock:
            if self._pool is None and not self._stop.is_set():
                from app.userdocs.extract.pool import ExtractorPool

                self._pool = ExtractorPool(
                    size=max(1, min(8, uds.read_admin_policy().workers)),
                    tmp_root=os.path.join(userdocs_root(), "tmp"),
                )
            return self._pool

    def extract(self, req: Any, *, size: int) -> Any:
        from app.userdocs import types as t
        from app.userdocs.extract.pool import ExtractorPool

        if size > self.max_file_bytes():
            return t.ExtractResult(status=t.EXTRACT_METADATA_ONLY, kind=req.kind, reason="too_large")
        pool = self._get_pool()
        if pool is None:
            return t.ExtractResult(status=t.EXTRACT_ERROR, kind=req.kind, reason="pool_closed")
        return pool.extract(req, timeout_s=ExtractorPool.timeout_for(req.kind, size))

    def max_file_bytes(self) -> int:
        return uds.read_admin_policy().max_file_mb * MB

    def extract_limits(self) -> dict[str, Any]:
        return {}

    # ── storage governor ───────────────────────────────────────────────────

    def note_enospc(self, rt: ProfileRuntime) -> None:
        self._enospc_at = time.time()
        self._last_governor = 0.0
        self.wake()

    def _usage(self, rt: ProfileRuntime) -> tuple[int, int]:
        from app.userdocs import governor as gov
        from app.userdocs.runtime import _vector_backend

        if rt.db is None:
            return 0, 0
        stats = rt.db.stats()
        vectorised = int(stats.get("chunks") or 0) - int(stats.get("chunks_without_vectors") or 0)
        vec = gov.estimate_bytes(vectorised, backend=_vector_backend())["vectors"]
        return int(rt.db.size_bytes()), int(vec)

    def _governor_tick(self, runtimes: list[ProfileRuntime]) -> None:
        from app.userdocs import governor as gov

        try:
            policy = uds.read_admin_policy()
            self.governor = self.governor if (
                self.governor.global_budget_bytes == policy.storage_budget_mb * MB
                and self.governor.profile_budget_bytes == policy.per_profile_budget_mb * MB
            ) else gov.Governor(
                global_budget_bytes=policy.storage_budget_mb * MB,
                profile_budget_bytes=policy.per_profile_budget_mb * MB,
            )
            cap = gov.measure_capacity(
                uds.system_dir(),
                vector_capacity_mb=policy.vector_capacity_mb, db_capacity_mb=policy.db_capacity_mb,
            )
            usages = {rt.profile: self._usage(rt) for rt in runtimes if rt.db is not None}
            global_total = sum(a + b for a, b in usages.values())
            enospc = (time.time() - self._enospc_at) < 900
            for rt in runtimes:
                if rt.profile not in usages:
                    continue
                idx, vec = usages[rt.profile]
                level = self.governor.evaluate(
                    gov.Usage(index_bytes=idx, vector_bytes_est=vec, profile_total=idx + vec,
                              global_total=global_total),
                    cap, store_error_enospc=enospc, profile=rt.profile,
                )
                previous, rt.level = rt.level, level.value
                rt.progress.set_storage(
                    level=level.value, used_bytes=idx + vec, global_bytes=global_total,
                    budget_bytes=policy.storage_budget_mb * MB,
                    profile_budget_bytes=policy.per_profile_budget_mb * MB or None,
                    free_bytes=cap.fs_free, total_bytes=cap.fs_total, method=cap.method,
                    message=self.governor.describe(level),
                )
                paused = ("budget", "disk_low", "disk_critical")
                if previous in paused and level.value not in paused and rt.db is not None:
                    deferred = [int(r["id"]) for r in rt.db.list_files(status="deferred", limit=100_000)]
                    if deferred:
                        rt.db.mark_dirty(deferred, priority=P_UPGRADE)
                        rt.db.add_activity("resumed", f"Storage is available again; indexing {len(deferred)} "
                                           "deferred files.", source=SOURCE)
                if previous != level.value:
                    rt._publish_state()
        except Exception:  # noqa: BLE001
            logger.exception("[userdocs] storage governor tick failed")

    # ── purge / GC ─────────────────────────────────────────────────────────

    def _purge(self, profile: str, kind: str) -> None:
        """Delete a source's index. When nothing else is left in the profile's
        index, the whole file and its collections go — the only way to give
        the disk space back at once."""
        from app.storage.userdocs_storage import get_userdocs_storage
        from app.userdocs import vector_sync
        from app.userdocs.index import index_dir

        rt = self.runtime(profile, create=True)
        if rt is None:
            return
        storage = get_userdocs_storage()
        other = uds.SOURCE_DRIVE if kind == uds.SOURCE_LOCAL else uds.SOURCE_LOCAL
        db = None
        try:
            db = rt.ensure_db()
        except Exception:  # noqa: BLE001
            pass
        others_left = bool(db and sum(db.count_by_status(other).values()))
        if db is not None and others_left:
            for ids in db.purge_source(kind):
                rt.queue_vector_deletes(ids)
            db.add_activity("purged", f"Deleted the {kind} index as requested.", source=kind)
            rt.refresh_totals()
            return
        rt.close()
        with self._lock:
            self._runtimes.pop(profile, None)
        vector_sync.drop_collections(rt.uid)
        shutil.rmtree(index_dir(rt.uid), ignore_errors=True)
        storage.delete_captions(profile)
        storage.delete_vision_usage(profile)
        logger.info(f"[userdocs] {profile}: index deleted")
        new_rt = self.runtime(profile, create=True)
        if new_rt is not None:
            new_rt.progress.set_state("disabled", None)
            uds_state.publish_snapshot(profile)

    def _gc_orphan_indexes(self) -> None:
        """Index folders and collections whose profile no longer exists."""
        from app.storage.userdocs_storage import get_userdocs_storage
        from app.userdocs import vector_sync
        from app.userdocs.vectors import parse_collection_name, profile_tag

        try:
            live = get_userdocs_storage().profile_uids()
        except Exception:  # noqa: BLE001
            return
        root = userdocs_root()
        if os.path.isdir(root):
            for name in os.listdir(root):
                if name == "tmp" or name in live:
                    continue
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                logger.info(f"[userdocs] removed the index of a deleted profile ({name})")
        handles = vector_sync.live_handles()
        if handles is None:
            return
        _emb, store = handles
        tags = {profile_tag(uid) for uid in live}
        try:
            names = store.list_collections()
        except Exception:  # noqa: BLE001
            return
        for name in names:
            parsed = parse_collection_name(name)
            if parsed and parsed[0] not in tags:
                try:
                    store.delete_collection(name)
                    logger.info(f"[userdocs] dropped orphan collection {name}")
                except Exception:  # noqa: BLE001
                    pass

    def _gc_all(self) -> None:
        from app.userdocs import vector_sync

        with self._lock:
            runtimes = list(self._runtimes.values())
        for rt in runtimes:
            try:
                vector_sync.gc(rt)
                if rt.db is not None:
                    rt.db.incremental_vacuum()
                    rt.db.checkpoint("TRUNCATE")
            except Exception:  # noqa: BLE001
                logger.exception(f"[userdocs] {rt.profile}: GC failed")

    # ── estimates ──────────────────────────────────────────────────────────

    def submit_estimate(self, rt: ProfileRuntime) -> None:
        if rt.estimating:
            return

        def run() -> None:
            rt.run_estimate()
            self.command("after_estimate", rt.profile)

        self._scan_exec.submit(run)

    # ── API facade ─────────────────────────────────────────────────────────

    def control(self, profile: str, action: str, **kw: Any) -> dict[str, Any]:
        from app.storage.userdocs_storage import get_userdocs_storage

        rt = self.runtime(profile, create=True)
        if rt is None:
            raise NotFound("Unknown profile.")
        storage = get_userdocs_storage()
        if action == "start":
            if not (storage.get_source(profile, SOURCE) or {}).get("enabled"):
                raise NotEnabled("Turn User Document Search on first.")
            storage.upsert_source(profile, SOURCE, first_sync_confirmed_at=time.time() * 1000)
            rt._set_confirmation(None)
            self.command("configure", profile)
        elif action == "pause":
            db = rt.ensure_db()
            db.update_source_state(SOURCE, paused_user=1)
            rt.paused_user = True
            rt._stop_watching()
            rt.stop_event.set()
            rt.stop_event = threading.Event()
            rt._publish_state()
        elif action == "resume":
            db = rt.ensure_db()
            db.update_source_state(SOURCE, paused_user=0)
            rt.paused_user = False
            self.command("configure", profile)
        elif action == "rescan":
            self._require(profile)
            rt.request_scan("user")
        elif action == "reindex":
            n = self._reindex(rt, kw.get("targets") or [])
            return {"accepted": True, "files": n, "snapshot": uds_state.build_snapshot(profile)}
        elif action == "retry_failed":
            n = self._retry(rt, kw.get("targets") or [])
            return {"accepted": True, "files": n, "snapshot": uds_state.build_snapshot(profile)}
        elif action == "rebuild":
            self._rebuild(profile, rt, bool(kw.get("reextract")), kw.get("confirm"))
        elif action == "confirm_deletions":
            self._require(profile)
            rt.confirm_deletions()
        elif action == "reject_deletions":
            self._require(profile)
            rt.reject_deletions()
        elif action == "confirm_root_change":
            hold = rt.hold or {}
            if hold.get("reason") != "pending_root_change":
                raise EngineError("There is no folder change waiting for confirmation.")
            storage.upsert_source(profile, SOURCE, root_path=(hold.get("detail") or {}).get("to"))
            rt.hold = None
            rt._set_confirmation(None)
            self.command("configure", profile)
        else:
            raise UnknownAction(f"Unknown action {action!r}.")
        self.wake()
        return {"accepted": True, "snapshot": uds_state.build_snapshot(profile)}

    def _resolve_targets(self, rt: ProfileRuntime, targets: list[str]) -> list[int]:
        db = rt.ensure_db()
        ids: set[int] = set()
        for raw in targets:
            s = str(raw).strip().replace("\\", "/")
            if not s:
                continue
            row = db.file_by_cite(s.lower()) if len(s) == 8 and "/" not in s else None
            if row is not None:
                ids.add(int(row["id"]))
                continue
            if rt.root and os.path.isabs(s):
                try:
                    s = os.path.relpath(s, rt.root).replace(os.sep, "/")
                except ValueError:
                    continue
            for r in db.files_under(SOURCE, s.strip("/")):
                ids.add(int(r["id"]))
        return sorted(ids)

    def _reindex(self, rt: ProfileRuntime, targets: list[str]) -> int:
        ids = self._resolve_targets(rt, targets)
        if not ids:
            raise NotFound("No indexed file or folder matches.")
        for fid in ids:
            # Forget the versions so the pipeline re-reads the content even
            # though its hash did not change.
            rt.db.update_file(fid, extractor_version=None, chunker_version=None)
        rt.db.mark_dirty(ids, priority=P_INTERACTIVE)
        self.wake()
        return len(ids)

    def _retry(self, rt: ProfileRuntime, targets: list[str]) -> int:
        db = rt.ensure_db()
        if targets:
            ids = self._resolve_targets(rt, targets)
        else:
            ids = [int(r["id"]) for r in db.list_files(status="error", limit=100_000)]
            ids += [int(r["id"]) for r in db.list_files(status="awaiting_extractor", limit=100_000)]
        for fid in ids:
            db.update_file(fid, attempts=0, next_attempt_at=None)
        if ids:
            db.mark_dirty(ids, priority=P_BULK)
        rt.progress.clear_failed()
        self.wake()
        return len(ids)

    def _rebuild(self, profile: str, rt: ProfileRuntime, reextract: bool, confirm: Any) -> None:
        from app.userdocs.errors import EngineError as _E

        db = rt.ensure_db()
        stats = db.stats()
        effect = uds.Effect("reextract_all" if reextract else "reembed_all",
                            files=int(stats.get("files") or 0), detail={"chunks": int(stats.get("chunks") or 0)})
        token = uds.plan_token(profile, {"action": "rebuild", "reextract": reextract},
                               db.get_meta("epoch"), [effect])
        if confirm != token:
            raise _E(
                "Rebuilding re-embeds the whole index" + (" and re-reads every file" if reextract else "")
                + ". Review and confirm.",
                code="ConfirmationRequired",
                plan={"effects": [effect.to_dict()], "destructive": True},
                confirm=token,
            )
        db.clear_vec_gen()
        if reextract:
            ids = [int(r["id"]) for r in db.list_files(source=SOURCE, limit=1_000_000)]
            for fid in ids:
                db.update_file(fid, extractor_version=None, chunker_version=None)
            db.mark_dirty(ids, priority=P_UPGRADE)
        db.add_activity("rebuild", "Index rebuild requested" + (" (re-reading every file)." if reextract else "."),
                        source=SOURCE)
        self.wake()
        self.wake_embedder()

    def list_files(self, profile: str, *, status=None, kind=None, source=None, q=None,
                   after=None, limit: int = 100) -> dict[str, Any]:
        rt = self._require(profile)
        rows = rt.db.list_files(status=status, kind=kind, source=source, q=q, after=after, limit=limit)
        files = [_file_view(r) for r in rows]
        nxt = {"rel_path": rows[-1]["rel_path"], "id": int(rows[-1]["id"])} if len(rows) == limit else None
        return {"files": files, "next": nxt, "counts": rt.db.count_by_status()}

    def file_detail(self, profile: str, fid: str) -> dict[str, Any]:
        rt = self._require(profile)
        row = rt.db.file_by_cite(str(fid).lower())
        if row is None:
            raise NotFound("No such file.")
        view = _file_view(row)
        view.update({
            "doc_meta": row.get("doc_meta"), "exif": row.get("exif"), "error": row.get("error"),
            "attempts": row.get("attempts"), "first_seen_at": _ms(row.get("first_seen_at")),
            "taken_at": _ms(row.get("taken_at")), "doc_created_at": _ms(row.get("doc_created_at")),
            "source": row.get("source"),
        })
        return view

    def activity(self, profile: str, *, before_id: int | None = None, limit: int = 50) -> dict[str, Any]:
        rt = self._require(profile)
        events = rt.db.list_activity(before_id=before_id, limit=limit)
        for e in events:
            e["ts"] = _ms(e.get("ts"))
        return {"events": events}

    def get_estimate(self, profile: str) -> dict[str, Any]:
        rt = self.runtime(profile, create=True)
        if rt is None:
            raise NotFound("Unknown profile.")
        if rt.estimate:
            return dict(rt.estimate)
        if rt.db is not None:
            est = rt.db.get_source_state(SOURCE).get("estimate")
            if est:
                return est
        return {"state": "none"}

    def start_estimate(self, profile: str) -> dict[str, Any]:
        from app.storage.userdocs_storage import get_userdocs_storage

        rt = self.runtime(profile, create=True)
        if rt is None:
            raise NotFound("Unknown profile.")
        rt.settings = get_userdocs_storage().get_source(profile, SOURCE) or {}
        self.submit_estimate(rt)
        return {"state": "running"}

    def storage(self, profile: str) -> dict[str, Any]:
        rt = self._require(profile)
        idx, vec = self._usage(rt)
        snap = rt.progress.snapshot().get("storage") or {}
        return {
            "index_bytes": idx, "vector_bytes_est": vec, "total_bytes": idx + vec,
            "level": snap.get("level") or rt.level, "message": snap.get("message"),
            "budget_bytes": snap.get("budget_bytes"), "profile_budget_bytes": snap.get("profile_budget_bytes"),
            "free_bytes": snap.get("free_bytes"), "disk_total_bytes": snap.get("total_bytes"),
            "method": snap.get("method"),
        }

    def count_effect(self, profile: str, kind: str, what: str, ctx: dict[str, Any]) -> uds.Effect | None:
        """Real counts for a settings change plan (see settings.plan_source_change)."""
        from app.userdocs.discovery.ignore import IgnoreMatcher, SKIP
        from app.userdocs.index import index_path

        rt = self.runtime(profile, create=True)
        if rt is None or (rt.db is None and not os.path.exists(index_path(rt.uid))):
            return uds.Effect(what, files=0)
        db = rt.ensure_db()
        if what in ("purge_all", "purge_drive"):
            src = uds.SOURCE_DRIVE if what == "purge_drive" else SOURCE
            n = sum(db.count_by_status(src).values())
            return uds.Effect(what, files=n, bytes=int(db.size_bytes()) if what == "purge_all" else 0)
        if what == "purge_out_of_scope":
            patch = ctx.get("patch") or {}
            manifest = db.load_manifest(SOURCE)
            old_root = rt.root or (ctx.get("current") or {}).get("root_path")
            if "root_path" in patch and patch.get("root_path") and old_root:
                new_root = patch["root_path"]
                out = sum(
                    1 for r in manifest.values()
                    if not uds.is_inside(os.path.join(old_root, *r.rel_path.split("/")), new_root)
                )
                return uds.Effect(what, files=out, detail={"to": new_root})
            if "excludes" in patch and old_root:
                matcher = IgnoreMatcher(old_root, excludes=uds.normalize_excludes(patch["excludes"]),
                                        system_dir=uds.system_dir())
                out = sum(
                    1 for r in manifest.values()
                    if matcher.classify(r.rel_path, os.path.join(old_root, *r.rel_path.split("/"))) == SKIP
                )
                return uds.Effect(what, files=out)
        return uds.Effect(what, files=0)


def _ms(ts: Any) -> float | None:
    return float(ts) * 1000 if ts else None


def _file_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "fid": r.get("cite_id"), "id": int(r["id"]), "rel_path": r.get("rel_path"), "name": r.get("name"),
        "kind": r.get("kind"), "status": r.get("status"), "status_reason": r.get("status_reason"),
        "size": r.get("size"), "modified": _ms(r.get("mtime")), "indexed_at": _ms(r.get("indexed_at")),
        "chunks": r.get("chunk_count"), "caption_state": r.get("caption_state"),
    }


# ── module-level lifecycle hooks (safe to call with no engine running) ─────


def start_service() -> UserDocsService | None:
    """Start the engine once per process. Never raises into the boot path."""
    global _service
    if _service is not None:
        return _service
    try:
        svc = UserDocsService()
        svc.start()
        _service = svc
        return svc
    except Exception:  # noqa: BLE001
        logger.exception("[userdocs] engine failed to start")
        return None


def stop_service(budget_s: float = 1.5) -> None:
    global _service
    svc, _service = _service, None
    if svc is not None:
        svc.stop(budget_s=budget_s)


def forget_profile(profile: str, uid: str | None) -> None:
    """A profile was deleted: stop its runtime and delete its index and
    collections. The index is keyed by the profile's uuid, so a profile
    re-created under the same name could never read it anyway — this only
    gives the space back."""
    from app.userdocs import vector_sync
    from app.userdocs.index import index_dir

    svc = get_service()
    if svc is not None:
        with svc._lock:
            rt = svc._runtimes.pop(profile, None)
        if rt is not None:
            uid = uid or rt.uid
            rt.close()
    if not uid:
        return
    try:
        vector_sync.drop_collections(uid)
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(index_dir(uid), ignore_errors=True)


def clean_profile(profile: str) -> dict[str, Any]:
    """The "User documents" clean-data component: delete the index, captions,
    quota counters and settings — never the user's files."""
    from app.storage.userdocs_storage import get_userdocs_storage

    storage = get_userdocs_storage()
    uid = storage.profile_uid(profile)
    forget_profile(profile, uid)
    captions = storage.delete_captions(profile)
    storage.delete_vision_usage(profile)
    removed = 0
    for kind in uds.SOURCE_KINDS:
        removed += 1 if storage.delete_source(profile, kind) else 0
    uds_state.publish_snapshot(profile)
    return {"sources": removed, "captions": captions}
