"""TagProjectionWorker: journal -> deliveries, expiry, pruning, periodic content.

One background task (template: :class:`app.events.task_timeout_manager.
TaskTimeoutManager`), woken by :func:`app.tags.journal.wake` after a journal
commit and otherwise polling every 2 s. Started after the boot delivery sweep,
stopped in ``_do_shutdown``.

**Projection** — per profile with Tags enabled, in ONE transaction:

1. read the stream head (``projected_seq``, ``next_seq``) and at most 200
   events in ``(projected_seq, next_seq]``;
2. claim them: ``UPDATE tag_streams SET projected_seq = :last WHERE
   projected_seq = :old`` (a second projector loses and rolls back). That
   takes the profile's stream-row lock, which every change of a tag's owner
   or epoch also takes first (``storage.lock_device``), so the owned tags
   read next cannot change hands before this batch commits;
3. build cards (:mod:`app.tags.cards`), route them to the profile's own tags
   (:mod:`app.tags.routing`) — a tag that still waits for its screen to be
   cleared gets nothing now; :mod:`app.tags.backfill` delivers what it missed
   when the clear succeeds — and insert the deliveries, retiring older active
   ones with the same ``(tag, replace_key)``.

**Maintenance** (every minute) — expire deliveries past ``expires_at`` (one
already on its way over the radio — ``gateway_received`` or later — only 10
minutes after, so the companion's final receipt wins) and commands (a
claimed one only an hour after its deadline; an expired
``clear_tag`` is re-queued, see ``storage.requeue_clear``); prune terminal
deliveries and projected events older than 30 days (recording the
pruned-through delivery seq, which makes an older connector cursor answer
410); derive ``tag.diagnostics`` (battery low, not seen for 2 h); sample
event-run todo progress (``run.progress``).

**Periodic content** (every 5 minutes) — ``calendar.upcoming``,
``automation.upcoming``, ``usage.summary``, ``indexing.problem``,
``health.summary``: journalled when the content's hash changed, or when the
same content was last sent 12 h ago (its card lives 24 h, so a standing
condition never drops off the tag), and only when the profile routes that
card kind somewhere. Diagnostics are refreshed the same way.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import delete, func, or_, select, update

from app.tags import journal, routing
from app.tags.cards import NON_CONTENT_KINDS, cards_for_event
from app.tags.journal import JournalEntry
from app.tags.storage import (
    ACTIVE_STAGES, DELIVERIES, DEVICES, STREAMS, TERMINAL_STAGES, TagStorage, device_json,
    get_tag_storage, merge_stream_state, now_ms, open_key_devices, write_deliveries,
)
from app.storage.models import TagEventModel
from app.utils.logger import logger

EVENTS = TagEventModel.__table__

POLL_S = 2.0
BATCH = 200
MAINTENANCE_S = 60.0
PERIODIC_S = 300.0
RETENTION_MS = 30 * 24 * 3600 * 1000.0
BATTERY_LOW_MV = 2400
OFFLINE_AFTER_MS = 2 * 3600 * 1000.0
# Periodic and diagnostics cards live 24 h: a condition that still holds is
# journalled again after half of that.
REFRESH_AFTER_MS = 12 * 3600 * 1000.0
# Expiry: a card still with Cremind or the companion expires on time; one on
# its way over the radio waits this long for the companion's final receipt.
QUEUE_STAGES = ("queued", "companion_accepted")
IN_FLIGHT_STAGES = tuple(s for s in ACTIVE_STAGES if s not in QUEUE_STAGES)
IN_FLIGHT_GRACE_MS = 10 * 60 * 1000.0


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


class TagProjectionWorker:
    def __init__(self, storage: TagStorage | None = None) -> None:
        self._storage = storage
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._stopping = False
        self._last_maintenance = 0.0
        self._last_periodic = 0.0
        # run id -> (done, total, sampled at ms)
        self._progress: dict[str, tuple[int, int, float]] = {}

    @property
    def storage(self) -> TagStorage:
        return self._storage or get_tag_storage()

    # ── lifecycle ──

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._stopping = False
        self._loop = loop
        self._wake = asyncio.Event()
        journal.configure_standalone(self.storage.provider)
        journal.set_wake_callback(self.wake)
        self._task = loop.create_task(self._run_loop(), name="tag_projection_worker")
        logger.info("TagProjectionWorker: started")

    def stop(self) -> None:
        self._stopping = True
        journal.set_wake_callback(None)
        journal.configure_standalone(None)
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    def wake(self) -> None:
        loop, event = self._loop, self._wake
        if loop is None or event is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass

    async def _run_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_S)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            self._wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("TagProjectionWorker: tick failed")

    async def tick(self, *, force_housekeeping: bool = False) -> None:
        profiles = await self.storage.enabled_profiles()
        backlog = set(await self.storage.profiles_with_backlog())
        for profile in profiles:
            if profile not in backlog:
                continue
            try:
                while await self.project_profile(profile) >= BATCH:
                    pass
            except Exception:  # noqa: BLE001
                logger.exception(f"TagProjectionWorker: projection failed for {profile!r}")
        mono = time.monotonic()
        if force_housekeeping or mono - self._last_maintenance >= MAINTENANCE_S:
            self._last_maintenance = mono
            await self.maintenance(profiles)
        if force_housekeeping or mono - self._last_periodic >= PERIODIC_S:
            self._last_periodic = mono
            await self.periodic(profiles)

    # ── projection ──

    async def _options(self, profile: str) -> dict[str, Any]:
        settings = await self.storage.get_settings(profile)
        return await routing.effective_options_async((settings or {}).get("options"))

    async def project_profile(self, profile: str) -> int:
        """Project one batch of ``profile``'s journal. Returns events consumed."""
        options = await self._options(profile)
        now = now_ms()
        async with self.storage.engine.begin() as conn:
            stream = (await conn.execute(select(STREAMS).where(STREAMS.c.profile == profile))).first()
            if stream is None or int(stream.projected_seq) >= int(stream.next_seq):
                return 0
            old = int(stream.projected_seq)
            events = (await conn.execute(
                select(EVENTS).where(
                    EVENTS.c.profile == profile, EVENTS.c.seq > old,
                    EVENTS.c.seq <= int(stream.next_seq),
                ).order_by(EVENTS.c.seq.asc()).limit(BATCH)
            )).all()
            if not events:
                return 0
            last = int(events[-1].seq)
            claimed = await conn.execute(
                update(STREAMS).where(STREAMS.c.profile == profile, STREAMS.c.projected_seq == old)
                .values(projected_seq=last, updated_at=now)
            )
            if not claimed.rowcount:
                return 0
            owned = [device_json(r) for r in (await conn.execute(select(DEVICES).where(
                DEVICES.c.owner_profile == profile, DEVICES.c.kind == "tag",
            ))).all()]
            by_id = {d["id"]: d for d in owned}
            items: list[tuple[dict[str, Any], Any, str | None]] = []
            db_open: dict[str, set[str]] = {}
            batch_open: dict[str, set[str]] = {}
            batch_closed: dict[str, set[str]] = {}
            for ev in events:
                event = dict(ev._mapping)
                for spec in cards_for_event(event, profile=profile, options=options):
                    if spec.expires_at <= now:
                        continue
                    if spec.target_device_id:
                        targets = [by_id[spec.target_device_id]] if spec.target_device_id in by_id else []
                        if spec.kind == "resolved":
                            targets = [d for d in targets if d["id"] in await self._open(
                                conn, profile, spec.resolves, now, db_open, batch_open, batch_closed)]
                    elif spec.kind == "resolved":
                        open_ids = await self._open(conn, profile, spec.resolves, now,
                                                    db_open, batch_open, batch_closed)
                        targets = [by_id[i] for i in sorted(open_ids) if i in by_id]
                    else:
                        targets = routing.route_targets(spec.kind, options, owned)
                    if spec.kind not in NON_CONTENT_KINDS:
                        # Held (clear pending) and paused tags get no content.
                        targets = [d for d in targets if not d["clear_required"] and d["status"] != "paused"]
                    for device in targets:
                        items.append((device, spec, event["id"]))
                        if spec.kind == "resolved" and spec.resolves:
                            batch_open.setdefault(spec.resolves, set()).discard(device["id"])
                            batch_closed.setdefault(spec.resolves, set()).add(device["id"])
                        elif spec.replace_key:
                            batch_open.setdefault(spec.replace_key, set()).add(device["id"])
                            batch_closed.setdefault(spec.replace_key, set()).discard(device["id"])
            if items:
                await write_deliveries(conn, profile, items, now)
        return len(events)

    @staticmethod
    async def _open(conn, profile: str, key: str | None, now: float, db_open: dict,
                    batch_open: dict, batch_closed: dict) -> set[str]:
        if not key:
            return set()
        if key not in db_open:
            db_open[key] = await open_key_devices(conn, profile, key, now)
        return (db_open[key] - batch_closed.get(key, set())) | batch_open.get(key, set())

    # ── maintenance ──

    async def maintenance(self, profiles: list[str]) -> None:
        now = now_ms()
        try:
            await self.expire(now)
        except Exception:  # noqa: BLE001
            logger.exception("TagProjectionWorker: expiry failed")
        try:
            from app.tags import idempotency, operations

            await operations.expire_operations(now)
            await idempotency.prune(now)
        except Exception:  # noqa: BLE001
            logger.exception("TagProjectionWorker: setup-operation expiry failed")
        try:
            await self.prune(now)
        except Exception:  # noqa: BLE001
            logger.exception("TagProjectionWorker: pruning failed")
        for profile in profiles:
            try:
                await self.diagnostics(profile, now)
            except Exception:  # noqa: BLE001
                logger.exception(f"TagProjectionWorker: diagnostics failed for {profile!r}")
        try:
            await self.sample_progress(profiles, now)
        except Exception:  # noqa: BLE001
            logger.exception("TagProjectionWorker: progress sampling failed")

    async def expire(self, now: float) -> int:
        """Expire deliveries past ``expires_at``. One the radio path already
        holds (``gateway_received`` or later) gets :data:`IN_FLIGHT_GRACE_MS`
        more: the companion reports its real outcome — ``uncertain`` when the
        refresh state is unknown — right around the deadline, and that receipt
        must win over this sweep, not race it."""
        async with self.storage.engine.begin() as conn:
            result = await conn.execute(update(DELIVERIES).where(or_(
                DELIVERIES.c.stage.in_(QUEUE_STAGES) & (DELIVERIES.c.expires_at <= now),
                DELIVERIES.c.stage.in_(IN_FLIGHT_STAGES)
                & (DELIVERIES.c.expires_at + IN_FLIGHT_GRACE_MS <= now),
            )).values(stage="expired", outcome="expired", finished_at=now, updated_at=now))
        await self.storage.expire_commands(now)
        return int(result.rowcount or 0)

    async def prune(self, now: float) -> None:
        cutoff = now - RETENTION_MS
        async with self.storage.engine.begin() as conn:
            streams = (await conn.execute(select(STREAMS.c.profile, STREAMS.c.projected_seq))).all()
            for s in streams:
                pruned_max = (await conn.execute(select(func.max(DELIVERIES.c.seq)).where(
                    DELIVERIES.c.profile == s.profile, DELIVERIES.c.stage.in_(TERMINAL_STAGES),
                    func.coalesce(DELIVERIES.c.finished_at, DELIVERIES.c.updated_at) < cutoff,
                ))).scalar()
                if pruned_max is not None:
                    await conn.execute(delete(DELIVERIES).where(
                        DELIVERIES.c.profile == s.profile, DELIVERIES.c.stage.in_(TERMINAL_STAGES),
                        func.coalesce(DELIVERIES.c.finished_at, DELIVERIES.c.updated_at) < cutoff,
                    ))
                    await merge_stream_state(conn, s.profile, lambda st, m=int(pruned_max): st.update(
                        pruned_through_seq=max(int(st.get("pruned_through_seq") or 0), m)))
                await conn.execute(delete(EVENTS).where(
                    EVENTS.c.profile == s.profile, EVENTS.c.created_at < cutoff,
                    EVENTS.c.seq <= int(s.projected_seq),
                ))

    async def diagnostics(self, profile: str, now: float) -> None:
        async with self.storage.engine.connect() as conn:
            owned = (await conn.execute(select(DEVICES).where(
                DEVICES.c.owner_profile == profile, DEVICES.c.kind == "tag",
            ))).all()
            stream = (await conn.execute(select(STREAMS.c.state).where(STREAMS.c.profile == profile))).first()
        known = {
            dev: _stamped(value, "issue")
            for dev, value in (((stream.state if stream is not None else None) or {}).get("diag") or {}).items()
        }
        entries: list[JournalEntry] = []
        current: dict[str, dict[str, Any]] = {}
        for dev in owned:
            if dev.battery_mv is not None and dev.battery_mv < BATTERY_LOW_MV:
                issue, detail = "battery_low", f"{dev.battery_mv} mV"
            elif dev.last_contact_at is not None and now - float(dev.last_contact_at) > OFFLINE_AFTER_MS:
                hours = int((now - float(dev.last_contact_at)) / 3_600_000)
                issue, detail = "offline", f"Last seen {hours} h ago"
            else:
                issue, detail = "ok", None
            before = known.get(dev.id) or {"issue": "ok", "at": now}
            # Unchanged: nothing, unless a standing problem's card is due to
            # expire (it lives 24 h) — then it is sent again.
            if before["issue"] == issue and (issue == "ok" or now - before["at"] < REFRESH_AFTER_MS):
                current[dev.id] = before
                continue
            current[dev.id] = {"issue": issue, "at": now}
            entries.append(JournalEntry(
                kind="tag.diagnostics",
                payload={"device_id": dev.id, "name": dev.name or dev.hw_id, "issue": issue,
                         "detail": detail, "battery_mv": dev.battery_mv,
                         "last_contact_at": dev.last_contact_at},
                source_type="tag", source_id=dev.id, replace_key=f"diag:{dev.id}",
            ))
        if not entries and current == known:
            return
        await self._append_with_state(profile, entries, lambda st: st.update(diag=current))

    async def sample_progress(self, profiles: list[str], now: float) -> None:
        if not profiles:
            return
        from app.agent import plan_state
        from app.storage import get_event_run_storage

        store = get_event_run_storage()
        seen: set[str] = set()
        for profile in profiles:
            options = await self._options(profile)
            cadence_ms = float(options.get("progress_cadence_s") or 300) * 1000
            rows, _ = await store.list(profile=profile, status="running", limit=50)
            entries: list[JournalEntry] = []
            for run in rows:
                todos = plan_state.get_todos(run.get("run_id") or "") or []
                if not todos:
                    continue
                total = len(todos)
                done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
                seen.add(run["id"])
                prev = self._progress.get(run["id"])
                if prev and (prev[0], prev[1]) == (done, total):
                    continue
                if prev and now - prev[2] < cadence_ms:
                    continue
                self._progress[run["id"]] = (done, total, now)
                from app.tags.sanitize import clean_text

                entries.append(JournalEntry(
                    kind="run.progress",
                    payload={"run_id": run["id"], "title": clean_text(run.get("label") or "Automation", 120),
                             "done": done, "total": total},
                    source_type="event_run", source_id=run["id"], replace_key=f"run:{run['id']}",
                ))
            if entries:
                await journal.append_standalone(profile, entries)
        for rid in list(self._progress):
            if rid not in seen:
                self._progress.pop(rid, None)

    # ── periodic content ──

    async def periodic(self, profiles: list[str]) -> None:
        for profile in profiles:
            try:
                await self.periodic_profile(profile)
            except Exception:  # noqa: BLE001
                logger.exception(f"TagProjectionWorker: periodic content failed for {profile!r}")

    async def periodic_profile(self, profile: str) -> None:
        options = await self._options(profile)
        routes = options.get("routes") or {}
        producers = (
            ("calendar.upcoming", "calendar", self._calendar),
            ("automation.upcoming", "automation", self._automations),
            ("usage.summary", "usage", self._usage),
            ("indexing.problem", "indexing_problem", self._indexing),
            ("health.summary", "health", self._health),
        )
        stream = await self.storage.get_stream(profile)
        hashes = {
            kind: _stamped(value, "h")
            for kind, value in (((stream or {}).get("state") or {}).get("periodic") or {}).items()
        }
        now = now_ms()
        entries: list[JournalEntry] = []
        new_hashes = dict(hashes)
        for kind, card_kind, producer in producers:
            if routes.get(card_kind, "all") == "none":
                continue
            try:
                payload = await producer(profile, options)
            except Exception:  # noqa: BLE001
                logger.debug(f"[tags] periodic {kind} failed for {profile!r}", exc_info=True)
                continue
            if payload is None:
                continue
            digest = _hash(payload)
            before = hashes.get(kind)
            # Unchanged content is sent again once its card is half-way to its
            # 24 h expiry, so a standing condition never drops off the tag.
            if before and before["h"] == digest and (payload.get("empty") or now - before["at"] < REFRESH_AFTER_MS):
                continue
            new_hashes[kind] = {"h": digest, "at": now}
            if payload.get("empty") and kind not in hashes:
                continue  # nothing was ever shown, so there is nothing to retire
            entries.append(JournalEntry(kind=kind, payload=payload, source_type="periodic",
                                        source_id=kind, replace_key=f"periodic:{card_kind}"))
        if new_hashes != hashes:
            await self._append_with_state(profile, entries, lambda st: st.update(periodic=new_hashes))

    async def _append_with_state(self, profile: str, entries: list[JournalEntry], mutate) -> None:
        engine = self.storage.engine
        if not await journal.is_enabled_async(engine, profile):
            return
        async with engine.begin() as conn:
            if entries:
                await journal.append_async(conn, profile, entries)
            await merge_stream_state(conn, profile, mutate)
        if entries:
            self.wake()

    @staticmethod
    def _local_now(profile: str, options: dict[str, Any]) -> datetime:
        from app.config.timezone import _safe_zone, resolve_tzinfo

        tz = _safe_zone(options.get("timezone")) or resolve_tzinfo(profile)
        return datetime.now(tz)

    async def _calendar(self, profile: str, options: dict[str, Any]) -> dict[str, Any] | None:
        from app.calendar.provider import get_calendar_provider
        from app.tags.sanitize import clean_text

        start = self._local_now(profile, options).replace(tzinfo=None, microsecond=0)
        end = start + timedelta(hours=24)

        def _load():
            provider = get_calendar_provider(profile)
            return provider.list_occurrences(profile, start.isoformat(), end.isoformat())

        occ = await asyncio.wait_for(asyncio.to_thread(_load), timeout=15)
        lines = []
        for item in occ[:3]:
            when = str(item.get("start") or "")[11:16]
            lines.append(f"{when} {clean_text(item.get('title') or 'Event', 60)}".strip())
        if not lines:
            return {"empty": True}
        return {"title": f"Next: {lines[0]}", "body": "\n".join(lines[1:]) or None, "count": len(occ)}

    async def _automations(self, profile: str, options: dict[str, Any]) -> dict[str, Any] | None:
        from app.storage import get_schedule_event_storage
        from app.tags.sanitize import clean_text

        rows = await asyncio.to_thread(get_schedule_event_storage().list_by_profile, profile)
        horizon = time.time() + 24 * 3600
        tz = self._local_now(profile, options).tzinfo
        due = sorted(
            (r for r in rows if r.get("action") and r.get("status") == "active"
             and r.get("next_fire_at") and float(r["next_fire_at"]) <= horizon),
            key=lambda r: float(r["next_fire_at"]),
        )
        if not due:
            return {"empty": True}
        lines = [
            f"{datetime.fromtimestamp(float(r['next_fire_at']), tz).strftime('%H:%M')} "
            f"{clean_text(r.get('title') or 'Automation', 60)}"
            for r in due[:3]
        ]
        return {"title": f"Next automation: {lines[0]}", "body": "\n".join(lines[1:]) or None,
                "count": len(due)}

    async def _usage(self, profile: str, options: dict[str, Any]) -> dict[str, Any] | None:
        from app.storage import get_usage_storage

        local = self._local_now(profile, options)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        totals = await get_usage_storage().totals(
            profile=profile, start_ms=start.timestamp() * 1000, end_ms=time.time() * 1000,
        )
        tokens = int(totals.get("total_tokens") or 0)
        usd = totals.get("total_usd")
        body = f"{tokens:,} tokens" + (f" · ${float(usd):.2f}" if usd else "")
        return {"title": "Today's usage", "body": body, "tokens": tokens}

    async def _indexing(self, profile: str, options: dict[str, Any]) -> dict[str, Any] | None:
        from app.documents.state import build_snapshot

        snap = await asyncio.to_thread(build_snapshot, profile)
        state = str(snap.get("state") or "")
        if snap.get("enabled") and state in ("error", "failed", "blocked", "needs_confirmation"):
            reason = str(snap.get("reason") or "").replace("_", " ")
            return {"title": f"Document indexing: {state.replace('_', ' ')}", "body": reason or None,
                    "state": state}
        return {"empty": True}

    async def _health(self, profile: str, options: dict[str, Any]) -> dict[str, Any] | None:
        from app.storage import get_conversation_storage

        channels = await get_conversation_storage().list_channels(profile)
        problems = []
        for ch in channels:
            if ch.get("channel_type") == "main":
                continue
            state = ch.get("state") or {}
            if not ch.get("enabled") and state.get("last_error"):
                problems.append(f"{ch.get('channel_type')}: stopped")
            elif state.get("link_status") == "unlinked":
                problems.append(f"{ch.get('channel_type')}: unlinked")
        if not problems:
            return {"empty": True}
        return {"title": f"{len(problems)} channel problem(s)", "body": "\n".join(problems[:3]),
                "count": len(problems)}


def _stamped(value: Any, field: str) -> dict[str, Any]:
    """A stored ``{field, "at"}`` entry; an entry from before timestamps
    were kept counts as sent long ago (so it is refreshed on the next run)."""
    if isinstance(value, dict):
        return {field: value.get(field), "at": float(value.get("at") or 0)}
    return {field: value, "at": 0.0}


_instance: TagProjectionWorker | None = None


def get_tag_projection_worker() -> TagProjectionWorker:
    global _instance
    if _instance is None:
        _instance = TagProjectionWorker()
    return _instance
