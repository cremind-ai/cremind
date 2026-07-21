"""Time-driven manager for schedule-event subscriptions (the Calendar &
Schedule trigger engine).

The clock-based sibling of :class:`app.events.file_watcher_manager.FileWatcherManager`.
Where that mounts watchdog observers, this maintains a single in-memory min-heap
keyed by ``next_fire_at`` and one asyncio task that sleeps until the earliest
due time. When a rule fires it either raises a reminder notification
(reminder-only rows) or enqueues an agent run on the per-conversation queue, then
**advances the rolling pointer** to the following occurrence — so an open-ended
recurrence is one durable row, never an exploding set of registrations.

Lazy invalidation: each heap entry carries a sequence number; ``arm``/``refresh``
bump the latest sequence for a subscription, so stale heap entries are recognized
and skipped on pop instead of being removed from the middle of the heap.

The feature gate is per-profile: only subscriptions whose profile has
*Calendar & Schedule* enabled are armed. Toggling the switch arms/disarms a
profile's rows wholesale (see :meth:`set_profile_enabled`).
"""

from __future__ import annotations

import asyncio
import heapq
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.calendar import recurrence as R
from app.calendar.feature import is_enabled as feature_enabled
from app.config.timezone import resolve_tzinfo
from app.storage import get_schedule_event_storage
from app.utils.logger import logger


# Heap entry: (fire_at_epoch, seq, sub_id)
_HeapEntry = Tuple[float, int, str]


def _until_for(sub: Dict[str, Any]) -> Optional[str]:
    """Return the UNTIL date bound (ISO) for a sub, or None."""
    if sub.get("recurrence_end_type") == "until":
        return sub.get("recurrence_end_value")
    return None


class ScheduleManager:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
        self._heap: List[_HeapEntry] = []
        # sub_id -> latest seq scheduled. Missing => disarmed (entries stale).
        self._latest_seq: Dict[str, int] = {}
        self._seq = 0
        self._wake: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the loop, arm every active subscription (feature-enabled
        profiles only), and launch the scheduler task."""
        self._loop = loop
        self._wake = asyncio.Event()
        self._stopping = False
        try:
            active = get_schedule_event_storage().list_active()
        except Exception:  # noqa: BLE001
            logger.exception("ScheduleManager: failed to load active subscriptions on start")
            active = []
        armed = 0
        for sub in active:
            if sub.get("next_fire_at") is None:
                continue
            if not feature_enabled(sub["profile"]):
                continue
            self._arm(sub["id"], float(sub["next_fire_at"]))
            armed += 1
        logger.info(f"ScheduleManager: started; armed {armed}/{len(active)} active subscription(s)")
        self._task = loop.create_task(self._run_loop(), name="schedule_manager")

    def stop(self) -> None:
        self._stopping = True
        self._signal()
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    # ── public arm / disarm API (idempotent, thread-safe) ──────────────

    def arm(self, sub: Dict[str, Any]) -> None:
        """Arm (or re-arm) a single subscription from its current row."""
        if not sub or sub.get("status") != "active":
            return
        if sub.get("next_fire_at") is None:
            return
        if not feature_enabled(sub["profile"]):
            return
        self._arm(sub["id"], float(sub["next_fire_at"]))

    def refresh(self, sub_id: str) -> None:
        """Re-read a subscription from storage and re-arm/disarm accordingly."""
        try:
            sub = get_schedule_event_storage().get(sub_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"ScheduleManager.refresh: lookup failed for {sub_id}")
            return
        if sub is None:
            self.remove(sub_id)
            return
        if sub.get("status") == "active" and sub.get("next_fire_at") is not None \
                and feature_enabled(sub["profile"]):
            self._arm(sub_id, float(sub["next_fire_at"]))
        else:
            self.remove(sub_id)

    def remove(self, sub_id: str) -> None:
        """Disarm a subscription (deleted / cancelled / paused / feature off)."""
        with self._lock:
            self._latest_seq.pop(sub_id, None)
        self._signal()

    def set_profile_enabled(self, profile: str, enabled: bool) -> None:
        """Arm or disarm every active subscription of ``profile`` when the
        feature switch flips."""
        try:
            subs = get_schedule_event_storage().list_by_profile(profile)
        except Exception:  # noqa: BLE001
            logger.exception(f"ScheduleManager.set_profile_enabled: lookup failed for {profile}")
            return
        for sub in subs:
            if enabled and sub.get("status") == "active" and sub.get("next_fire_at") is not None:
                self._arm(sub["id"], float(sub["next_fire_at"]))
            else:
                self.remove(sub["id"])

    # ── internals ──────────────────────────────────────────────────────

    def _arm(self, sub_id: str, fire_at: float) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._latest_seq[sub_id] = seq
            heapq.heappush(self._heap, (fire_at, seq, sub_id))
        self._signal()

    def _signal(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass

    async def _run_loop(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            due_sub_id: Optional[str] = None
            timeout: Optional[float] = None
            with self._lock:
                # Drop stale entries at the top, then inspect the earliest.
                while self._heap:
                    fire_at, seq, sub_id = self._heap[0]
                    if self._latest_seq.get(sub_id) != seq:
                        heapq.heappop(self._heap)
                        continue
                    now = time.time()
                    if fire_at <= now:
                        heapq.heappop(self._heap)
                        # This entry is now consumed; clearing latest_seq avoids a
                        # duplicate fire if _fire re-arms with a fresh entry.
                        self._latest_seq.pop(sub_id, None)
                        due_sub_id = sub_id
                    else:
                        timeout = fire_at - now
                    break
            if due_sub_id is not None:
                try:
                    await self._fire(due_sub_id)
                except Exception:  # noqa: BLE001
                    logger.exception(f"ScheduleManager: fire failed for {due_sub_id}")
                continue
            # Nothing due: wait until the next due time or an arm/disarm signal.
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def _fire(self, sub_id: str) -> None:
        store = get_schedule_event_storage()
        sub = store.get(sub_id)
        if sub is None or sub.get("status") != "active":
            return
        # Defensive: respect a feature flag flipped off between arm and fire.
        if not feature_enabled(sub["profile"]):
            return

        next_fire_at = sub.get("next_fire_at")
        if next_fire_at is None:
            return
        # Naive wall-clock <-> epoch must go through the profile's configured
        # zone, not the process OS zone (which is UTC on a Docker/VPS install).
        tz = resolve_tzinfo(sub["profile"])
        occurrence_dt = R.from_epoch(float(next_fire_at), tz)
        fired_iso = R.format_local(occurrence_dt)

        # Advance the rolling pointer FIRST so a slow agent run can't double-fire.
        # Catch-up policy: base the next search at max(this occurrence, now) so a
        # server that was down through several occurrences fires once and resumes
        # on the next *future* slot rather than replaying the whole backlog.
        now_dt = R.from_epoch(time.time(), tz)
        base = occurrence_dt if occurrence_dt >= now_dt else now_dt
        nxt = R.next_occurrence_after(
            rrule=sub.get("rrule"),
            dtstart=sub["dtstart"],
            after=base,
            until=_until_for(sub),
        )
        occurrences_fired = int(sub.get("occurrences_fired", 0)) + 1
        if nxt is not None:
            new_epoch = R.to_epoch(nxt, tz)
            store.update_next_fire(sub_id, next_fire_at=new_epoch, occurrences_fired=occurrences_fired)
            self._arm(sub_id, new_epoch)
        else:
            store.set_status(sub_id, "completed", next_fire_at=None)
            store.update_next_fire(sub_id, next_fire_at=None, occurrences_fired=occurrences_fired)

        # Deliver the trigger: ALWAYS run the action in the registering
        # conversation. When no explicit action was set, the title is the
        # command (e.g. "tắt đèn hiên"), so the agent still executes it.
        action = (sub.get("action") or "").strip() or (sub.get("title") or "").strip()
        payload = {
            "title": sub.get("title", ""),
            "fired_at": fired_iso,
            "schedule_kind": sub.get("schedule_kind"),
            "rrule": sub.get("rrule"),
            "next_fire_at_iso": R.format_local(nxt) if nxt is not None else None,
        }
        try:
            from app.events import run_dispatcher
            await run_dispatcher.dispatch_schedule_event(
                sub=sub, action=action, payload=payload,
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"ScheduleManager: dispatch failed for {sub_id}")

        # Nudge any open Events-page / calendar SSE subscribers.
        self._publish_admin_changed(sub["profile"])
        logger.info(
            f"ScheduleManager: fired {sub_id} ({sub.get('title')!r}) at {fired_iso}; "
            f"next={R.format_local(nxt) if nxt else 'none (completed)'}"
        )

    @staticmethod
    def _publish_admin_changed(profile: str) -> None:
        try:
            from app.api.calendar import publish_schedule_events_admin_changed
            publish_schedule_events_admin_changed(profile)
        except Exception:  # noqa: BLE001
            # API module may not be wired yet during early boot; non-fatal.
            pass


_instance: Optional[ScheduleManager] = None


def get_schedule_manager() -> ScheduleManager:
    global _instance
    if _instance is None:
        _instance = ScheduleManager()
    return _instance
