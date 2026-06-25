"""Async storage for long-term conversation memory.

A single bounded FIFO queue, backend-agnostic (SQLite / PostgreSQL via the active
:class:`~app.databases.DatabaseProvider`):

- **long-term** (``long_term_memories``) — per *profile*; durable user facts.
  Capped by ``memory.long_term_queue_size``.

This is the DB path, used when vector embedding is OFF. When embedding is ON,
long-term memory lives in the vector store instead (see
:mod:`app.agent.memory_vectorstore`). Short-term memory was unified into the
conversation's running summary (see :mod:`app.agent.compaction`), so there is no
longer a short-term table or an extraction watermark here.

Queue size is enforced in Python after each insert (FIFO-evict the rows with the
lowest ``ordering``). This class mirrors
:class:`app.storage.conversation_storage.ConversationStorage`'s async-session
usage and never issues DDL — schema creation belongs to Alembic.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.databases import DatabaseProvider, get_database_provider
from app.storage.models import LongTermMemoryModel
from app.utils.logger import logger


class MemoryStorage:
    """Async CRUD + FIFO eviction for long-term memory (DB path)."""

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

    # ── reads ────────────────────────────────────────────────────────────

    async def get_long_term(self, profile: str) -> list[dict[str, Any]]:
        """Return this profile's long-term entries, oldest → newest."""
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(LongTermMemoryModel)
                .where(LongTermMemoryModel.profile == profile)
                .order_by(LongTermMemoryModel.ordering.asc())
            )).scalars().all()
            return [self._long_to_dict(r) for r in rows]

    # ── writes ───────────────────────────────────────────────────────────

    async def add_long_term(
        self, profile: str, content: str, token_count: int,
        source_conversation_id: str | None, queue_size: int,
    ) -> dict[str, Any] | None:
        """Insert a long-term fact (skipping exact duplicates), FIFO-evict beyond ``queue_size``.

        Returns the inserted row dict, or ``None`` when the content already exists
        for the profile (dedup) or is empty.
        """
        content = (content or "").strip()
        if not content:
            return None
        async with self.async_session_maker.begin() as session:
            exists = (await session.execute(
                select(LongTermMemoryModel.id)
                .where(
                    LongTermMemoryModel.profile == profile,
                    LongTermMemoryModel.content == content,
                )
                .limit(1)
            )).first()
            if exists is not None:
                return None
            next_order = (await session.execute(
                select(func.coalesce(func.max(LongTermMemoryModel.ordering), 0))
                .where(LongTermMemoryModel.profile == profile)
            )).scalar_one() + 1
            row = LongTermMemoryModel(
                id=str(uuid.uuid4()),
                profile=profile,
                content=content,
                token_count=int(token_count),
                source_conversation_id=source_conversation_id,
                ordering=next_order,
                created_at=time.time(),
            )
            session.add(row)
            await session.flush()
            await self._evict_long_term(session, profile, queue_size)
            result = self._long_to_dict(row)
        return result

    # ── eviction helpers ───────────────────────────────────────────────────

    @staticmethod
    async def _evict_long_term(session, profile: str, queue_size: int) -> None:
        if queue_size <= 0:
            return
        ids = (await session.execute(
            select(LongTermMemoryModel.id)
            .where(LongTermMemoryModel.profile == profile)
            .order_by(LongTermMemoryModel.ordering.desc())
            .offset(queue_size)
        )).scalars().all()
        if ids:
            await session.execute(
                delete(LongTermMemoryModel).where(LongTermMemoryModel.id.in_(ids))
            )
            logger.debug(
                f"[memory] evicted {len(ids)} long-term entry(s) for profile={profile}"
            )

    # ── serialization ──────────────────────────────────────────────────────

    @staticmethod
    def _long_to_dict(row: LongTermMemoryModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "content": row.content,
            "token_count": row.token_count,
            "source_conversation_id": row.source_conversation_id,
            "created_at": row.created_at,
        }
