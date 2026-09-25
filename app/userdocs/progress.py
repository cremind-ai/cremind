"""Live sync progress for one profile — the "never silent" half of the feature.

The engine calls into a :class:`SyncProgress` at every stage of every file
(started, extracting, captioning, embedding, finished) from its worker
threads. This object keeps the counts in memory and turns them into the
``runtime`` part of the snapshot (see :mod:`app.userdocs.state`), which the
progress bus fans out to the web UI and ``cremind userdocs status -f``.

Two properties matter:

**Publishing is throttled on the trailing edge.** A first sync can finish
dozens of small files a second; every one of them must be *counted*, but the
browser only needs a frame every few hundred milliseconds. State transitions
(indexing → paused, a confirmation appearing) publish immediately so the page
never shows a stale banner.

**Nothing here touches the database.** Snapshots are built from in-memory
counters only; the per-status totals are refreshed from the index by the
engine on its own schedule (:meth:`set_totals`), never once per frame.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

# At most one published frame per interval per profile, trailing edge.
PUBLISH_INTERVAL_S = 0.4
RECENT_LIMIT = 20
FAILED_PREVIEW_LIMIT = 5
CURRENT_LIMIT = 5


def _now_ms() -> float:
    return time.time() * 1000


class SyncProgress:
    def __init__(self, profile: str, publish: Callable[[str], None]):
        self.profile = profile
        self._publish = publish
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._last_publish = 0.0
        self._closed = False

        self.state = "idle"
        self.reason: str | None = None
        self.detail: dict[str, Any] | None = None
        self.phase: str | None = None
        self.batch: dict[str, Any] = {"label": None, "total": 0, "done": 0, "failed": 0, "skipped": 0}
        self._batch_started: float | None = None
        # EWMA of files/second, for the ETA. Seeded on the first finished file.
        self._rate: float | None = None
        self._last_finish: float | None = None
        self.stages: dict[str, int] = {}
        self._current: dict[Any, dict[str, Any]] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=RECENT_LIMIT)
        self._failed: deque[dict[str, Any]] = deque(maxlen=FAILED_PREVIEW_LIMIT)
        self.queue_preview: list[str] = []
        self.vision: dict[str, Any] = {}
        self.storage: dict[str, Any] = {}
        self.reembed: dict[str, Any] = {"done": 0, "total": 0}
        self.vector_coverage_pct: float | None = None
        self.confirmation: dict[str, Any] | None = None
        self.sources: dict[str, dict[str, Any]] = {}
        self.watch_mode: str | None = None

    # ── state ─────────────────────────────────────────────────────────────

    def set_state(self, state: str, reason: str | None = None, detail: dict[str, Any] | None = None) -> None:
        with self._lock:
            changed = (state, reason) != (self.state, self.reason)
            self.state, self.reason, self.detail = state, reason, detail
        self._schedule(force=changed)

    def set_phase(self, phase: str | None) -> None:
        with self._lock:
            self.phase = phase
        self._schedule()

    def set_source(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self.sources.setdefault(kind, {}).update(fields)
        self._schedule()

    def set_confirmation(self, confirmation: dict[str, Any] | None) -> None:
        with self._lock:
            self.confirmation = confirmation
        self._schedule(force=True)

    def set_totals(self, stages: dict[str, int]) -> None:
        """Replace the per-status file counts (read from the index by the engine)."""
        with self._lock:
            self.stages = dict(stages)
        self._schedule()

    def set_vision(self, **fields: Any) -> None:
        with self._lock:
            self.vision.update(fields)
        self._schedule()

    def set_storage(self, **fields: Any) -> None:
        with self._lock:
            self.storage.update(fields)
        self._schedule()

    def set_reembed(self, done: int, total: int) -> None:
        with self._lock:
            self.reembed = {"done": int(done), "total": int(total)}
            self.vector_coverage_pct = (100.0 * done / total) if total else None
        self._schedule()

    def set_queue_preview(self, rel_paths: list[str]) -> None:
        with self._lock:
            self.queue_preview = list(rel_paths[:10])
        self._schedule()

    # ── batches and files ──────────────────────────────────────────────────

    def begin_batch(self, label: str, total: int) -> None:
        with self._lock:
            self.batch = {"label": label, "total": int(total), "done": 0, "failed": 0, "skipped": 0}
            self._batch_started = time.monotonic()
            self._rate = None
            self._last_finish = None
        self._schedule(force=True)

    def add_to_batch(self, n: int) -> None:
        with self._lock:
            self.batch["total"] = int(self.batch.get("total") or 0) + int(n)
        self._schedule()

    def end_batch(self) -> None:
        with self._lock:
            self.batch = {"label": None, "total": 0, "done": 0, "failed": 0, "skipped": 0}
            self._batch_started = None
            self._current.clear()
        self._schedule(force=True)

    def file_started(self, key: Any, *, name: str, rel_path: str, stage: str = "extract") -> None:
        with self._lock:
            self._current[key] = {
                "name": name, "rel_path": rel_path, "stage": stage,
                "progress": None, "started_at": _now_ms(),
            }
        self._schedule()

    def file_stage(self, key: Any, stage: str, progress: dict[str, Any] | None = None) -> None:
        with self._lock:
            cur = self._current.get(key)
            if cur is None:
                return
            cur["stage"] = stage
            cur["progress"] = progress
        self._schedule()

    def file_finished(
        self,
        key: Any,
        outcome: str,
        *,
        name: str,
        rel_path: str,
        message: str,
        reason: str | None = None,
        fid: str | None = None,
    ) -> None:
        """Record one file's end. ``outcome``: added | updated | unchanged |
        moved | removed | metadata_only | failed | skipped."""
        now = time.monotonic()
        with self._lock:
            self._current.pop(key, None)
            if self.batch.get("label"):
                self.batch["done"] = int(self.batch.get("done") or 0) + 1
                if outcome == "failed":
                    self.batch["failed"] = int(self.batch.get("failed") or 0) + 1
                elif outcome in ("skipped", "unchanged"):
                    self.batch["skipped"] = int(self.batch.get("skipped") or 0) + 1
                if self._last_finish is not None:
                    dt = max(1e-3, now - self._last_finish)
                    inst = 1.0 / dt
                    self._rate = inst if self._rate is None else 0.9 * self._rate + 0.1 * inst
                self._last_finish = now
            if outcome not in ("unchanged",):
                self._recent.appendleft({
                    "ts": _now_ms(), "kind": outcome, "name": name,
                    "rel_path": rel_path, "message": message,
                })
            if outcome == "failed":
                self._failed.appendleft({
                    "fid": fid, "name": name, "rel_path": rel_path,
                    "reason": reason, "message": message,
                })
        self._schedule()

    def clear_failed(self) -> None:
        with self._lock:
            self._failed.clear()
        self._schedule()

    # ── snapshot ───────────────────────────────────────────────────────────

    def _eta_locked(self) -> int | None:
        total = int(self.batch.get("total") or 0)
        done = int(self.batch.get("done") or 0)
        if not total or done >= total or not self._rate:
            return None
        return int((total - done) / max(self._rate, 1e-3))

    def snapshot(self) -> dict[str, Any]:
        """The engine-owned part of the profile snapshot."""
        with self._lock:
            batch = dict(self.batch)
            if batch.get("label"):
                batch["eta_s"] = self._eta_locked()
                batch["rate_per_min"] = round(self._rate * 60, 1) if self._rate else None
            current = sorted(self._current.values(), key=lambda c: c["started_at"])[:CURRENT_LIMIT]
            return {
                "state": self.state,
                "reason": self.reason,
                "detail": self.detail,
                "phase": self.phase,
                "batch": batch,
                "stages": dict(self.stages),
                "current": [dict(c) for c in current],
                "queue_preview": list(self.queue_preview),
                "recent": list(self._recent),
                "failed_preview": list(self._failed),
                "vision": dict(self.vision),
                "storage": dict(self.storage),
                "reembed": dict(self.reembed),
                "vector_coverage_pct": self.vector_coverage_pct,
                "confirmation": self.confirmation,
                "engine_sources": {k: dict(v) for k, v in self.sources.items()},
            }

    # ── publishing ─────────────────────────────────────────────────────────

    def _schedule(self, force: bool = False) -> None:
        if self._closed:
            return
        now = time.monotonic()
        with self._lock:
            due = force or (now - self._last_publish) >= PUBLISH_INTERVAL_S
            if due:
                self._last_publish = now
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
            elif self._timer is None:
                delay = PUBLISH_INTERVAL_S - (now - self._last_publish)
                self._timer = threading.Timer(max(0.01, delay), self._fire)
                self._timer.daemon = True
                self._timer.start()
        if due:
            self._emit()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            self._last_publish = time.monotonic()
        self._emit()

    def _emit(self) -> None:
        try:
            self._publish(self.profile)
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass

    def close(self) -> None:
        self._closed = True
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
