"""Async storage for per-invocation LLM token usage + estimated cost.

Backs the "Usage & Cost" dashboard and the per-conversation usage panel. Writes
one ``usage_records`` row per LLM invocation in a turn (one reasoning step or one
tool/sub-agent child-LLM call), freezing the estimated USD cost at write time via
:mod:`app.lib.llm.pricing`. Reads are indexed ``GROUP BY`` aggregations over that
single fact table — there is no denormalized summary table; ``func.sum`` ignores
NULL costs, so backfilled/unpriced rows contribute exact tokens but $0 cost (the
``has_unpriced`` flag lets the UI footnote that).

Mirrors :class:`app.storage.memory_storage.MemoryStorage`'s async-session usage
and never issues DDL — schema creation belongs to Alembic.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional, TypedDict

from sqlalchemy import Integer, cast, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.databases import DatabaseProvider, get_database_provider
from app.lib.llm.pricing import cost_columns_for
from app.storage.models import ConversationModel, UsageRecordModel
from app.utils.logger import logger

_DAY_MS = 86_400_000  # one day in epoch milliseconds


class UsageRecordInput(TypedDict, total=False):
    """One attributed LLM invocation, before cost is computed/frozen."""

    source_kind: str        # reasoning | tool | subagent | intrinsic | aggregate
    tool_id: Optional[str]
    label: Optional[str]
    step_index: int
    provider: Optional[str]
    model: Optional[str]
    input_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int


class UsageStorage:
    """Async insert + dashboard aggregation for ``usage_records``."""

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

    async def add_usage_records(
        self,
        conversation_id: str | None,
        profile: str,
        records: list[UsageRecordInput],
        message_id: str | None = None,
        event_run_id: str | None = None,
    ) -> list[str]:
        """Insert one turn's attributed usage rows in a single transaction.

        Cost is frozen per row from current catalog rates. Returns the new row
        ids. Silently no-ops on an empty list. Never raises on a pricing error —
        cost falls back to NULL so usage is recorded regardless.

        ``conversation_id`` may be ``None`` (e.g. an event-gate rejection that
        never opened a conversation). ``event_run_id`` is stamped on every row
        when the turn ran inside a hidden event-run conversation, so per-run
        usage stays attributable even after the run and its conversation are
        deleted.
        """
        if not records:
            return []
        now = time.time() * 1000
        ids: list[str] = []
        rows: list[UsageRecordModel] = []
        for rec in records:
            provider = rec.get("provider")
            model = rec.get("model")
            tokens = {
                "input_tokens": int(rec.get("input_tokens") or 0),
                "cache_read_input_tokens": int(rec.get("cache_read_input_tokens") or 0),
                "cache_creation_input_tokens": int(rec.get("cache_creation_input_tokens") or 0),
                "output_tokens": int(rec.get("output_tokens") or 0),
            }
            try:
                cost = cost_columns_for(provider, model, tokens, profile=profile)
            except Exception as exc:  # noqa: BLE001 — never lose usage over a pricing bug
                logger.warning(f"[usage] cost computation failed for {provider}/{model}: {exc}")
                cost = {
                    "uncached_input_usd": None, "cache_read_usd": None,
                    "cache_write_usd": None, "output_usd": None,
                    "total_usd": None, "rate_snapshot": None, "model_group": None,
                }
            rid = str(uuid.uuid4())
            ids.append(rid)
            rows.append(UsageRecordModel(
                id=rid,
                conversation_id=conversation_id,
                message_id=message_id,
                event_run_id=event_run_id,
                profile=profile,
                provider=provider,
                model=model,
                model_group=cost.get("model_group"),
                source_kind=rec.get("source_kind") or "reasoning",
                tool_id=rec.get("tool_id"),
                label=rec.get("label"),
                step_index=int(rec.get("step_index") or 0),
                **tokens,
                uncached_input_usd=cost.get("uncached_input_usd"),
                cache_read_usd=cost.get("cache_read_usd"),
                cache_write_usd=cost.get("cache_write_usd"),
                output_usd=cost.get("output_usd"),
                total_usd=cost.get("total_usd"),
                rate_snapshot=cost.get("rate_snapshot"),
                created_at=now,
            ))
        async with self.async_session_maker.begin() as session:
            session.add_all(rows)
        return ids

    async def set_records_message_id(self, ids: list[str], message_id: str) -> None:
        """Attach a message id to rows inserted before the turn's id was known."""
        if not ids:
            return
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(UsageRecordModel)
                .where(UsageRecordModel.id.in_(ids))
                .values(message_id=message_id)
            )

    async def delete_for_profile(self, profile: str) -> int:
        """Delete every usage record for a profile; returns the row count.

        ``usage_records.profile`` has no foreign key (usage is deliberately
        durable — it survives conversation/profile deletes so cost history is
        never silently lost), so this explicit scoped delete is the only way to
        clear a profile's usage. Portable across SQLite and PostgreSQL.
        """
        async with self.async_session_maker.begin() as session:
            result = await session.execute(
                delete(UsageRecordModel).where(UsageRecordModel.profile == profile)
            )
        return int(result.rowcount or 0)

    # ── per-conversation reads ──────────────────────────────────────────────

    async def per_request_breakdown(
        self, conversation_id: str, message_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """All usage rows for a conversation (optionally one turn), ordered for drill-down."""
        async with self.async_session_maker() as session:
            stmt = (
                select(UsageRecordModel)
                .where(UsageRecordModel.conversation_id == conversation_id)
                .order_by(
                    UsageRecordModel.message_id,
                    UsageRecordModel.step_index.asc(),
                    UsageRecordModel.created_at.asc(),
                )
            )
            if message_id is not None:
                stmt = stmt.where(UsageRecordModel.message_id == message_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._row_to_dict(r) for r in rows]

    async def conversation_rollup(self, conversation_id: str) -> dict[str, Any]:
        """Token + cost totals + cache-hit rate for one conversation."""
        async with self.async_session_maker() as session:
            row = (await session.execute(
                self._totals_select().where(
                    UsageRecordModel.conversation_id == conversation_id
                )
            )).one()
        return self._totals_row_to_dict(row)

    async def rollup_by_event_run(self, run_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Token + cost totals per event run, keyed by ``event_run_id``.

        One ``GROUP BY event_run_id`` over ``usage_records`` — the source of
        truth for per-run usage that survives deletion of the run and its
        conversation (usage rows outlive both; ``event_run_id`` stays set).
        Returns only runs that have at least one usage row.
        """
        if not run_ids:
            return {}
        async with self.async_session_maker() as session:
            stmt = (
                select(
                    UsageRecordModel.event_run_id,
                    func.coalesce(func.sum(UsageRecordModel.input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.cache_read_input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.cache_creation_input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.output_tokens), 0),
                    func.sum(UsageRecordModel.total_usd),
                    func.count(func.distinct(UsageRecordModel.message_id)),
                )
                .where(UsageRecordModel.event_run_id.in_(run_ids))
                .group_by(UsageRecordModel.event_run_id)
            )
            rows = (await session.execute(stmt)).all()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            it, cr, cc, ot = int(r[1]), int(r[2]), int(r[3]), int(r[4])
            out[r[0]] = {
                "input_tokens": it,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
                "output_tokens": ot,
                "total_tokens": it + cr + cc + ot,
                "total_usd": float(r[5]) if r[5] is not None else 0.0,
                "request_count": int(r[6]),
            }
        return out

    # ── dashboard reads (optional profile / time-range scope) ────────────────

    async def totals(
        self, *, profile: str | None = None,
        start_ms: float | None = None, end_ms: float | None = None,
    ) -> dict[str, Any]:
        async with self.async_session_maker() as session:
            stmt = self._scope(self._totals_select(extra_conv_count=True), profile, start_ms, end_ms)
            row = (await session.execute(stmt)).one()
        result = self._totals_row_to_dict(row)
        result["conversation_count"] = int(row.conversation_count or 0)
        return result

    async def by_day(
        self, *, profile: str | None = None,
        start_ms: float | None = None, end_ms: float | None = None,
        tz_offset_min: int = 0,
    ) -> list[dict[str, Any]]:
        """Daily time series, bucketed in the caller's local day (epoch-ms math)."""
        offset_ms = tz_offset_min * 60_000
        bucket = cast((UsageRecordModel.created_at + offset_ms) / _DAY_MS, Integer).label("bucket")
        async with self.async_session_maker() as session:
            stmt = self._scope(
                select(
                    bucket,
                    func.coalesce(func.sum(UsageRecordModel.input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.cache_read_input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.cache_creation_input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.output_tokens), 0),
                    func.sum(UsageRecordModel.total_usd),
                    func.count(),
                ),
                profile, start_ms, end_ms,
            ).group_by(bucket).order_by(bucket)
            rows = (await session.execute(stmt)).all()
        return [
            {
                "bucket": int(r[0]),  # days since epoch (local); UI maps to ISO date
                "input_tokens": int(r[1]),
                "cache_read_input_tokens": int(r[2]),
                "cache_creation_input_tokens": int(r[3]),
                "output_tokens": int(r[4]),
                "total_usd": float(r[5]) if r[5] is not None else 0.0,
                "request_count": int(r[6]),
            }
            for r in rows
        ]

    async def by_model(self, **scope) -> list[dict[str, Any]]:
        return await self._grouped(
            [UsageRecordModel.provider, UsageRecordModel.model], **scope,
            key=lambda r: f"{r[0] or 'unknown'}/{r[1] or 'unknown'}",
            display=lambda r: r[1] or "unknown",
        )

    async def by_provider(self, **scope) -> list[dict[str, Any]]:
        return await self._grouped(
            [UsageRecordModel.provider], **scope,
            key=lambda r: r[0] or "unknown",
            display=lambda r: r[0] or "unknown",
        )

    async def by_source(self, **scope) -> list[dict[str, Any]]:
        """Per reasoning-agent / sub-agent / tool breakdown (the comprehensive view)."""
        return await self._grouped(
            [UsageRecordModel.source_kind, UsageRecordModel.tool_id, UsageRecordModel.label],
            **scope,
            key=lambda r: f"{r[0]}:{r[1] or ''}",
            display=lambda r: (r[2] or r[1] or r[0]),
            extra=lambda r: {"source_type": r[0], "tool_id": r[1]},
        )

    async def cache_hit_rate(
        self, *, profile: str | None = None,
        start_ms: float | None = None, end_ms: float | None = None,
    ) -> dict[str, Any]:
        async with self.async_session_maker() as session:
            row = (await session.execute(self._scope(
                select(
                    func.coalesce(func.sum(UsageRecordModel.input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.cache_read_input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.cache_creation_input_tokens), 0),
                    func.sum(UsageRecordModel.cache_read_usd),
                    func.sum(UsageRecordModel.cache_write_usd),
                ),
                profile, start_ms, end_ms,
            ))).one()
        uncached, cache_read, cache_creation = int(row[0]), int(row[1]), int(row[2])
        denom = uncached + cache_read
        return {
            "cache_hit_rate": (cache_read / denom) if denom else 0.0,
            "cached_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "cache_read_usd": float(row[3]) if row[3] is not None else 0.0,
            "cache_write_usd": float(row[4]) if row[4] is not None else 0.0,
        }

    async def top_conversations(
        self, *, profile: str | None = None,
        start_ms: float | None = None, end_ms: float | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Conversations ranked by spend (falls back to tokens when cost is null)."""
        async with self.async_session_maker() as session:
            stmt = self._scope(
                select(
                    UsageRecordModel.conversation_id,
                    ConversationModel.title,
                    func.coalesce(func.sum(UsageRecordModel.input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.cache_read_input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.cache_creation_input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.output_tokens), 0),
                    func.sum(UsageRecordModel.total_usd),
                    func.count(func.distinct(UsageRecordModel.message_id)),
                    func.max(UsageRecordModel.created_at),
                ).join(
                    ConversationModel,
                    ConversationModel.id == UsageRecordModel.conversation_id,
                ).where(
                    # After a conversation delete the FK SET-NULLs conversation_id
                    # (usage totals still count it), but there is no id/title left
                    # to rank — exclude those rows from the leaderboard.
                    UsageRecordModel.conversation_id.is_not(None),
                ),
                profile, start_ms, end_ms,
            ).group_by(
                UsageRecordModel.conversation_id, ConversationModel.title,
            ).order_by(
                func.coalesce(func.sum(UsageRecordModel.total_usd), 0).desc(),
                func.coalesce(func.sum(UsageRecordModel.output_tokens), 0).desc(),
            ).limit(limit)
            rows = (await session.execute(stmt)).all()
        return [
            {
                "conversation_id": r[0],
                "title": r[1] or "Untitled Chat",
                "input_tokens": int(r[2]),
                "cache_read_input_tokens": int(r[3]),
                "cache_creation_input_tokens": int(r[4]),
                "output_tokens": int(r[5]),
                "total_tokens": int(r[2]) + int(r[3]) + int(r[4]) + int(r[5]),
                "total_usd": float(r[6]) if r[6] is not None else 0.0,
                "request_count": int(r[7]),
                "last_active_at": float(r[8]) if r[8] is not None else 0.0,
            }
            for r in rows
        ]

    async def has_unpriced(
        self, *, profile: str | None = None,
        start_ms: float | None = None, end_ms: float | None = None,
    ) -> bool:
        """True if any in-scope row carries tokens but no estimated cost."""
        async with self.async_session_maker() as session:
            count = (await session.execute(self._scope(
                select(func.count()).where(
                    UsageRecordModel.total_usd.is_(None),
                    (UsageRecordModel.input_tokens + UsageRecordModel.cache_read_input_tokens
                     + UsageRecordModel.output_tokens) > 0,
                ),
                profile, start_ms, end_ms,
            ))).scalar_one()
        return bool(count)

    # ── internal helpers ────────────────────────────────────────────────────

    async def _grouped(
        self, group_cols: list, *, profile: str | None = None,
        start_ms: float | None = None, end_ms: float | None = None,
        key, display, extra=None,
    ) -> list[dict[str, Any]]:
        async with self.async_session_maker() as session:
            stmt = self._scope(
                select(
                    *group_cols,
                    func.coalesce(func.sum(UsageRecordModel.input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.cache_read_input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.cache_creation_input_tokens), 0),
                    func.coalesce(func.sum(UsageRecordModel.output_tokens), 0),
                    func.sum(UsageRecordModel.total_usd),
                    func.count(),
                ),
                profile, start_ms, end_ms,
            ).group_by(*group_cols).order_by(
                func.coalesce(func.sum(UsageRecordModel.total_usd), 0).desc(),
                func.coalesce(func.sum(UsageRecordModel.output_tokens), 0).desc(),
            )
            rows = (await session.execute(stmt)).all()
        n = len(group_cols)
        out = []
        for r in rows:
            it, cr, cc, ot = int(r[n]), int(r[n + 1]), int(r[n + 2]), int(r[n + 3])
            entry = {
                "key": key(r),
                "display_name": display(r),
                "input_tokens": it,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
                "output_tokens": ot,
                "total_tokens": it + cr + cc + ot,
                "estimated_cost_usd": float(r[n + 4]) if r[n + 4] is not None else 0.0,
                "request_count": int(r[n + 5]),
            }
            if extra:
                entry.update(extra(r))
            out.append(entry)
        return out

    @staticmethod
    def _totals_select(extra_conv_count: bool = False):
        cols = [
            func.coalesce(func.sum(UsageRecordModel.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageRecordModel.cache_read_input_tokens), 0).label("cache_read_input_tokens"),
            func.coalesce(func.sum(UsageRecordModel.cache_creation_input_tokens), 0).label("cache_creation_input_tokens"),
            func.coalesce(func.sum(UsageRecordModel.output_tokens), 0).label("output_tokens"),
            func.sum(UsageRecordModel.uncached_input_usd).label("uncached_input_usd"),
            func.sum(UsageRecordModel.cache_read_usd).label("cache_read_usd"),
            func.sum(UsageRecordModel.cache_write_usd).label("cache_write_usd"),
            func.sum(UsageRecordModel.output_usd).label("output_usd"),
            func.sum(UsageRecordModel.total_usd).label("total_usd"),
            func.count(func.distinct(UsageRecordModel.message_id)).label("request_count"),
        ]
        if extra_conv_count:
            cols.append(
                func.count(func.distinct(UsageRecordModel.conversation_id)).label("conversation_count")
            )
        return select(*cols)

    @staticmethod
    def _scope(stmt, profile, start_ms, end_ms):
        if profile:
            stmt = stmt.where(UsageRecordModel.profile == profile)
        if start_ms is not None:
            stmt = stmt.where(UsageRecordModel.created_at >= start_ms)
        if end_ms is not None:
            stmt = stmt.where(UsageRecordModel.created_at < end_ms)
        return stmt

    @staticmethod
    def _totals_row_to_dict(row) -> dict[str, Any]:
        def usd(v):
            return float(v) if v is not None else 0.0
        it, cr, cc, ot = (
            int(row.input_tokens), int(row.cache_read_input_tokens),
            int(row.cache_creation_input_tokens), int(row.output_tokens),
        )
        return {
            "input_tokens": it,
            "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cc,
            "output_tokens": ot,
            "total_tokens": it + cr + cc + ot,
            "uncached_input_usd": usd(row.uncached_input_usd),
            "cache_read_usd": usd(row.cache_read_usd),
            "cache_write_usd": usd(row.cache_write_usd),
            "output_usd": usd(row.output_usd),
            "estimated_cost_usd": usd(row.total_usd),
            "request_count": int(row.request_count or 0),
        }

    @staticmethod
    def _row_to_dict(r: UsageRecordModel) -> dict[str, Any]:
        it, cr, cc, ot = (
            int(r.input_tokens), int(r.cache_read_input_tokens),
            int(r.cache_creation_input_tokens), int(r.output_tokens),
        )
        return {
            "id": r.id,
            "message_id": r.message_id,
            "provider": r.provider,
            "model": r.model,
            "model_group": r.model_group,
            "source_kind": r.source_kind,
            "tool_id": r.tool_id,
            "label": r.label,
            "step_index": r.step_index,
            "input_tokens": it,
            "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cc,
            "output_tokens": ot,
            "total_tokens": it + cr + cc + ot,
            "uncached_input_usd": r.uncached_input_usd,
            "cache_read_usd": r.cache_read_usd,
            "cache_write_usd": r.cache_write_usd,
            "output_usd": r.output_usd,
            "estimated_cost_usd": r.total_usd,
            "rate_snapshot": r.rate_snapshot,
            "created_at": r.created_at,
        }
