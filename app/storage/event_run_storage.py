"""Async storage for event runs — one row per fired event trigger.

Each fired subscription (skill / file-watcher / schedule) executes in its own
hidden conversation and is tracked by one ``event_runs`` row. Schema lives in
:class:`app.storage.models.EventRunModel`; the table is created by Alembic. This
class only reads/writes it.

Async (like :class:`app.storage.usage_storage.UsageStorage`) because every
caller — the run dispatcher, the stream runner's status hooks, the REST API, and
boot recovery — runs on the event loop.

Retention: :meth:`create` prunes terminal runs beyond the per-rule cap in the
same transaction, returning the pruned runs' conversation ids so the caller can
tear down their in-memory queue/stream state. Running/pending runs are never
pruned. Usage rows survive pruning (their FK to conversations SET-NULLs and the
``event_run_id`` stays set), so Usage & Cost keeps counting.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.databases import DatabaseProvider, get_database_provider
from app.storage.models import EventRunModel
from app.utils.logger import logger

# Terminal statuses — safe to prune, never re-armed.
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
# Non-terminal — never pruned (a running run is live; a pending run still awaits
# the user's reply and must remain answerable).
ACTIVE_STATUSES = ("running", "pending")

# When a run's result actually became available. The inbox is ordered by this
# rather than by ``created_at`` (fire time) because a run that parked ``pending``
# on a question for hours finishes AFTER runs that fired later — and the
# coalesced turn tells the model the results are "in the order they finished".
_TERMINAL_TS = func.coalesce(
    EventRunModel.finished_at, EventRunModel.updated_at, EventRunModel.created_at,
)


class EventRunStorage:
    """Async CRUD + aggregates + retention for ``event_runs``."""

    def __init__(self, provider: DatabaseProvider | None = None):
        self.provider = provider or get_database_provider()
        self._engine: AsyncEngine | None = None
        self._session_maker: async_sessionmaker | None = None

    @property
    def async_session_maker(self) -> async_sessionmaker:
        if self._session_maker is None:
            self._engine = self.provider.async_engine()
            self._session_maker = async_sessionmaker(self._engine, expire_on_commit=False)
        return self._session_maker

    # ── writes ────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        profile: str,
        source_kind: str,
        subscription_id: str,
        conversation_id: str | None,
        label: str,
        action: str,
        trigger_payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        history_cap: int = 50,
        origin_conversation_id: str | None = None,
        deliver_to_origin: bool = False,
        status: str = "running",
        error: str | None = None,
        finished: bool = False,
    ) -> dict[str, Any]:
        """Insert a new run and prune the rule's terminal history.

        Returns ``{"run": <row dict>, "pruned_conversation_ids": [...]}``. The
        caller must discard the pruned conversations' queue/stream state and
        delete those conversations (usage rows survive, see module docstring).

        ``origin_conversation_id`` is the conversation that registered the rule,
        frozen here because the rule row is editable and deletable. With
        ``deliver_to_origin`` set (an EVENT TASK run) the final answer is
        injected back into it when the run reaches a terminal status.

        ``status``/``error``/``finished`` default to a live ``running`` run;
        they exist so a task that times out before ever firing can be recorded
        as an already-terminal run and ride the same delivery path.
        """
        now = time.time() * 1000
        rid = str(uuid.uuid4())
        row = EventRunModel(
            id=rid,
            profile=profile,
            source_kind=source_kind,
            subscription_id=subscription_id,
            conversation_id=conversation_id,
            run_id=run_id,
            status=status,
            label=label or "",
            action=action or "",
            trigger_payload=trigger_payload,
            pending_question=None,
            error=error,
            turn_count=0,
            created_at=now,
            updated_at=now,
            finished_at=now if finished else None,
            origin_conversation_id=origin_conversation_id,
            deliver_to_origin=bool(deliver_to_origin),
        )
        pruned: list[str] = []
        async with self.async_session_maker.begin() as session:
            session.add(row)
            await session.flush()
            # Prune terminal runs beyond the cap for this rule (oldest first).
            if history_cap and history_cap > 0:
                keep_stmt = (
                    select(EventRunModel.id, EventRunModel.conversation_id)
                    .where(
                        EventRunModel.source_kind == source_kind,
                        EventRunModel.subscription_id == subscription_id,
                        EventRunModel.status.in_(TERMINAL_STATUSES),
                        # Never prune a task result that has not reached its
                        # origin conversation yet — the boot sweep still owes
                        # that delivery, and pruning would strand the flow.
                        or_(
                            EventRunModel.deliver_to_origin.is_(False),
                            EventRunModel.origin_delivered_at.isnot(None),
                        ),
                    )
                    .order_by(EventRunModel.created_at.desc())
                    .offset(history_cap)
                )
                doomed = (await session.execute(keep_stmt)).all()
                if doomed:
                    doomed_ids = [d[0] for d in doomed]
                    pruned = [d[1] for d in doomed if d[1]]
                    await session.execute(
                        delete(EventRunModel).where(EventRunModel.id.in_(doomed_ids))
                    )
        return {"run": self._row_to_dict(row), "pruned_conversation_ids": pruned}

    async def update_status(
        self,
        run_id_pk: str,
        *,
        status: str | None = None,
        run_id: str | None = None,
        pending_question: str | None = None,
        error: str | None = None,
        clear_pending: bool = False,
        increment_turn: bool = False,
        mark_finished: bool = False,
    ) -> None:
        """Patch a run row (status/pending/error/turn/finished_at).

        ``clear_pending`` nulls ``pending_question`` (used when a reply resumes a
        pending run). ``mark_finished`` stamps ``finished_at`` (terminal states).
        """
        values: dict[str, Any] = {"updated_at": time.time() * 1000}
        if status is not None:
            values["status"] = status
        if run_id is not None:
            values["run_id"] = run_id
        if pending_question is not None:
            values["pending_question"] = pending_question
        if clear_pending:
            values["pending_question"] = None
        if error is not None:
            values["error"] = error
        if mark_finished:
            values["finished_at"] = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            if increment_turn:
                values["turn_count"] = EventRunModel.turn_count + 1
            await session.execute(
                update(EventRunModel).where(EventRunModel.id == run_id_pk).values(**values)
            )

    # ── event-task delivery ───────────────────────────────────────────────

    async def claim_delivery(self, run_id_pk: str) -> bool:
        """Claim the right to deliver this run's result to its origin chat.

        The conditional ``origin_delivered_at IS NULL`` is the exactly-once
        lock. It covers three races at once: the live terminal hook vs the boot
        sweep, two boot sweeps, and a run that reaches a terminal status twice
        (someone replies inside a finished run's mini-chat). Returns ``True``
        only for the caller that must actually inject the turn.
        """
        async with self.async_session_maker.begin() as session:
            result = await session.execute(
                update(EventRunModel)
                .where(
                    EventRunModel.id == run_id_pk,
                    EventRunModel.deliver_to_origin.is_(True),
                    EventRunModel.origin_delivered_at.is_(None),
                    EventRunModel.status.in_(TERMINAL_STATUSES),
                )
                .values(origin_delivered_at=time.time() * 1000)
            )
            return (result.rowcount or 0) > 0

    async def clear_delivery_claim(self, run_id_pk: str) -> None:
        """Release a delivery claim whose hand-over failed, so it is retried.

        Two callers only, both releasing a claim *they themselves* took moments
        earlier: an injection whose enqueue raised, and a mid-turn read that was
        cancelled before the text reached the model. Never call this for a row a
        sibling claimed — that would resurrect an already-delivered result and
        re-deliver it on every boot.
        """
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(EventRunModel)
                .where(EventRunModel.id == run_id_pk)
                .values(origin_delivered_at=None, origin_delivery_mode=None)
            )

    async def set_delivery_mode(self, run_id_pk: str, mode: str) -> None:
        """Record HOW a claimed result was handed over (descriptive only)."""
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(EventRunModel)
                .where(EventRunModel.id == run_id_pk)
                .values(origin_delivery_mode=mode)
            )

    async def close_out_delivery(self, run_id_pk: str, mode: str = "skipped") -> bool:
        """Take a result out of the inbox WITHOUT reporting it.

        For results that must never reach a conversation: a cancelled run, an
        origin that no longer exists, a stale or over-the-cap standing result,
        and every owed result a restore brought back from an archive.

        The lock is :meth:`claim_delivery`'s conditional UPDATE — this adds no
        second one. A sibling that already claimed the row makes this return
        ``False`` and nothing is written, which is what keeps "closed out" and
        "delivered" mutually exclusive. Setting ``origin_delivered_at`` also
        un-pins the row from the retention prune, so a closed-out backlog can
        finally age out of ``event_runs``.
        """
        if not await self.claim_delivery(run_id_pk):
            return False
        await self.set_delivery_mode(run_id_pk, mode)
        return True

    async def list_pending_for_origin(
        self, origin_conversation_id: str,
    ) -> list[dict[str, Any]]:
        """A conversation's pending-results inbox, oldest FINISHED first.

        The same predicate as :meth:`list_undelivered_task_runs`, sliced by
        origin: a terminal run that has not been handed over yet *is* the
        inbox entry — parking a result writes nothing, because the terminal
        status write already put the row in this set.

        Deliberately unbounded: the caller must SEE every owed row, because
        whatever it decides not to report it has to close out through
        :meth:`close_out_delivery`. A SQL ``LIMIT`` here would hide those rows
        and leave them owed (and unprunable) forever.

        ``cancelled`` is deliberately excluded. A cancelled run is a deliberate
        kill from the Events page, and v1 terminates it quietly; surfacing "your
        task was cancelled" here would regress that. Such rows are claimed and
        marked ``skipped`` by :func:`on_run_terminal` (or the boot sweep), never
        read and never injected.
        """
        conds = [
            EventRunModel.origin_conversation_id == origin_conversation_id,
            EventRunModel.deliver_to_origin.is_(True),
            EventRunModel.origin_delivered_at.is_(None),
            EventRunModel.status.in_(("completed", "failed")),
        ]
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(EventRunModel)
                .where(*conds)
                .order_by(_TERMINAL_TS.asc(), EventRunModel.created_at.asc())
            )).scalars().all()
        return [self._row_to_dict(r) for r in rows]

    async def list_undelivered_task_runs(
        self, profile: str | None = None,
    ) -> list[dict[str, Any]]:
        """Terminal runs whose result never reached the origin chat.

        The boot sweep's work list: a crash between the terminal status write
        and the injection leaves exactly these rows behind. Ordered oldest
        FINISHED first, like :meth:`list_pending_for_origin`.
        """
        conds = [
            EventRunModel.deliver_to_origin.is_(True),
            EventRunModel.origin_delivered_at.is_(None),
            EventRunModel.status.in_(TERMINAL_STATUSES),
        ]
        if profile:
            conds.append(EventRunModel.profile == profile)
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(EventRunModel)
                .where(*conds)
                .order_by(_TERMINAL_TS.asc(), EventRunModel.created_at.asc())
            )).scalars().all()
        return [self._row_to_dict(r) for r in rows]

    async def has_live_run_for_subscription(
        self, source_kind: str, subscription_id: str,
    ) -> bool:
        """True if the rule has a run that is still running/pending or undelivered."""
        async with self.async_session_maker() as session:
            n = (await session.execute(
                select(func.count()).select_from(EventRunModel).where(
                    EventRunModel.source_kind == source_kind,
                    EventRunModel.subscription_id == subscription_id,
                    or_(
                        EventRunModel.status.in_(ACTIVE_STATUSES),
                        and_(
                            EventRunModel.deliver_to_origin.is_(True),
                            EventRunModel.origin_delivered_at.is_(None),
                        ),
                    ),
                )
            )).scalar_one()
        return int(n or 0) > 0

    # ── reads ─────────────────────────────────────────────────────────────

    async def get(self, run_id_pk: str) -> Optional[dict[str, Any]]:
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(EventRunModel).where(EventRunModel.id == run_id_pk)
            )).scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def get_by_conversation(self, conversation_id: str) -> Optional[dict[str, Any]]:
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(EventRunModel).where(EventRunModel.conversation_id == conversation_id)
            )).scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def list(
        self,
        *,
        profile: str | None = None,
        source_kind: str | None = None,
        subscription_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ``(rows, total)`` newest-first for the given filters."""
        conds = []
        if profile:
            conds.append(EventRunModel.profile == profile)
        if source_kind:
            conds.append(EventRunModel.source_kind == source_kind)
        if subscription_id:
            conds.append(EventRunModel.subscription_id == subscription_id)
        if status:
            conds.append(EventRunModel.status == status)
        async with self.async_session_maker() as session:
            total = (await session.execute(
                select(func.count()).select_from(EventRunModel).where(*conds)
            )).scalar_one()
            rows = (await session.execute(
                select(EventRunModel)
                .where(*conds)
                .order_by(EventRunModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )).scalars().all()
        return [self._row_to_dict(r) for r in rows], int(total or 0)

    async def recent_for_profile(self, profile: str, limit: int = 200) -> list[dict[str, Any]]:
        """Most-recent runs for a profile (the admin-stream snapshot)."""
        rows, _ = await self.list(profile=profile, limit=limit)
        return rows

    async def subscription_summaries(
        self, profile: str,
    ) -> dict[str, dict[str, Any]]:
        """Per-subscription aggregates for the admin snapshot.

        Keyed by ``f"{source_kind}:{subscription_id}"`` →
        ``{run_count, active_count, pending_count, last_run_at, last_status}``.
        """
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(
                    EventRunModel.source_kind,
                    EventRunModel.subscription_id,
                    func.count(),
                    func.sum(case((EventRunModel.status == "running", 1), else_=0)),
                    func.sum(case((EventRunModel.status == "pending", 1), else_=0)),
                    func.max(EventRunModel.created_at),
                )
                .where(EventRunModel.profile == profile)
                .group_by(EventRunModel.source_kind, EventRunModel.subscription_id)
            )).all()

            # Status of the most-recent run per rule. Done as a max-then-join
            # rather than a window function so it runs on both SQLite and
            # PostgreSQL; a created_at tie just resolves to either row, which is
            # harmless for a "last outcome" hint.
            latest = (
                select(
                    EventRunModel.source_kind.label("sk"),
                    EventRunModel.subscription_id.label("sub_id"),
                    func.max(EventRunModel.created_at).label("m"),
                )
                .where(EventRunModel.profile == profile)
                .group_by(EventRunModel.source_kind, EventRunModel.subscription_id)
                .subquery()
            )
            status_rows = (await session.execute(
                select(
                    EventRunModel.source_kind,
                    EventRunModel.subscription_id,
                    EventRunModel.status,
                ).join(
                    latest,
                    and_(
                        EventRunModel.source_kind == latest.c.sk,
                        EventRunModel.subscription_id == latest.c.sub_id,
                        EventRunModel.created_at == latest.c.m,
                    ),
                )
            )).all()
        last_status_by_key = {
            f"{sk}:{sub_id}": status for sk, sub_id, status in status_rows
        }
        out: dict[str, dict[str, Any]] = {}
        for sk, sub_id, count, running, pending, last_at in rows:
            key = f"{sk}:{sub_id}"
            out[key] = {
                "run_count": int(count or 0),
                "active_count": int(running or 0),
                "pending_count": int(pending or 0),
                "last_run_at": float(last_at) if last_at is not None else None,
                "last_status": last_status_by_key.get(key),
            }
        return out

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def list_for_subscription(
        self, source_kind: str, subscription_id: str,
    ) -> list[dict[str, Any]]:
        """All runs of a rule (for cascade delete)."""
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(EventRunModel).where(
                    EventRunModel.source_kind == source_kind,
                    EventRunModel.subscription_id == subscription_id,
                )
            )).scalars().all()
        return [self._row_to_dict(r) for r in rows]

    async def delete(self, run_id_pk: str) -> Optional[dict[str, Any]]:
        """Delete one run row, returning it (so the caller can drop its conv)."""
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(EventRunModel).where(EventRunModel.id == run_id_pk)
            )).scalar_one_or_none()
            if row is None:
                return None
            d = self._row_to_dict(row)
            await session.execute(
                delete(EventRunModel).where(EventRunModel.id == run_id_pk)
            )
            return d

    async def delete_for_subscription(
        self, source_kind: str, subscription_id: str,
    ) -> list[str]:
        """Delete all runs of a rule; return their conversation ids."""
        async with self.async_session_maker.begin() as session:
            rows = (await session.execute(
                select(EventRunModel.conversation_id).where(
                    EventRunModel.source_kind == source_kind,
                    EventRunModel.subscription_id == subscription_id,
                )
            )).all()
            conv_ids = [r[0] for r in rows if r[0]]
            await session.execute(
                delete(EventRunModel).where(
                    EventRunModel.source_kind == source_kind,
                    EventRunModel.subscription_id == subscription_id,
                )
            )
        return conv_ids

    async def recover_after_restart(self) -> int:
        """Mark ``running`` runs as ``failed`` (interrupted) at boot.

        ``pending`` runs are left as-is — their reply path is DB-backed and
        survives a restart, so they remain answerable. Returns the count fixed.

        An interrupted run keeps ``deliver_to_origin`` here, so the boot
        delivery sweep still reports the failure into the conversation that was
        waiting on it rather than leaving the flow hanging.

        ``finished_at`` is stamped from the row's own last activity, NOT from
        boot time. A three-week outage would otherwise make every interrupted
        run look like it finished seconds ago, and the delivery age bound — the
        one thing standing between that outage and a backlog of stale reports
        landing in people's chats — would never fire for exactly the population
        a long outage produces.
        """
        now_ms = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            result = await session.execute(
                update(EventRunModel)
                .where(EventRunModel.status == "running")
                .values(
                    status="failed",
                    error="Interrupted by server restart",
                    finished_at=func.coalesce(
                        EventRunModel.finished_at,
                        EventRunModel.updated_at,
                        EventRunModel.created_at,
                        now_ms,
                    ),
                    updated_at=now_ms,
                )
            )
            n = result.rowcount or 0
        if n:
            logger.info(f"[event_runs] boot recovery: marked {n} interrupted run(s) failed")
        return n

    # ── serialization ─────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(r: EventRunModel) -> dict[str, Any]:
        return {
            "id": r.id,
            "profile": r.profile,
            "source_kind": r.source_kind,
            "subscription_id": r.subscription_id,
            "conversation_id": r.conversation_id,
            "run_id": r.run_id,
            "status": r.status,
            "label": r.label,
            "action": r.action,
            "trigger_payload": r.trigger_payload,
            "pending_question": r.pending_question,
            "error": r.error,
            "turn_count": int(r.turn_count or 0),
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "finished_at": r.finished_at,
            "origin_conversation_id": r.origin_conversation_id,
            "deliver_to_origin": bool(r.deliver_to_origin),
            "origin_delivered_at": r.origin_delivered_at,
            "origin_delivery_mode": r.origin_delivery_mode,
        }


_instance: EventRunStorage | None = None


def get_event_run_storage(provider: DatabaseProvider | None = None) -> EventRunStorage:
    global _instance
    if _instance is None:
        _instance = EventRunStorage(provider)
    return _instance
