"""Async storage for the conversation-memory feature.

Two bounded FIFO queues, both backend-agnostic (SQLite / PostgreSQL via the
active :class:`~app.databases.DatabaseProvider`):

- **short-term** (``short_term_memories``) — per *conversation*; one distilled
  summary per extraction. Capped by ``memory.short_term_queue_size``.
- **long-term** (``long_term_memories``) — per *profile*; durable user facts.
  Capped by ``memory.long_term_queue_size``.

Queue size is enforced in Python after each insert (FIFO-evict the rows with
the lowest ``ordering``). The per-conversation extraction *watermark* lives on
``conversations.memory_watermark`` so it survives short-term FIFO eviction.

This class mirrors :class:`app.storage.conversation_storage.ConversationStorage`'s
async-session usage and never issues DDL — schema creation belongs to Alembic.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.databases import DatabaseProvider, get_database_provider
from app.storage.models import (
    ConversationModel,
    LongTermMemoryModel,
    MessageModel,
    ShortTermMemoryModel,
)
from app.utils.common import count_content_tokens
from app.utils.logger import logger

# Message roles whose ``content`` counts as conversational message content for
# memory extraction / token accounting. Reasoning lives in ``thinking_steps``
# (a separate column) and is intentionally never read here.
_CONTENT_ROLES = ("user", "agent", "assistant")


class MemoryStorage:
    """Async CRUD + FIFO eviction for short/long-term memory."""

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

    async def get_short_term(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return this conversation's short-term entries, oldest → newest."""
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(ShortTermMemoryModel)
                .where(ShortTermMemoryModel.conversation_id == conversation_id)
                .order_by(ShortTermMemoryModel.ordering.asc())
            )).scalars().all()
            return [self._short_to_dict(r) for r in rows]

    async def get_long_term(self, profile: str) -> list[dict[str, Any]]:
        """Return this profile's long-term entries, oldest → newest."""
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(LongTermMemoryModel)
                .where(LongTermMemoryModel.profile == profile)
                .order_by(LongTermMemoryModel.ordering.asc())
            )).scalars().all()
            return [self._long_to_dict(r) for r in rows]

    async def get_watermark(self, conversation_id: str) -> tuple[int, float | None]:
        """Return ``(memory_watermark, memory_last_extracted_at)`` for a conversation."""
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(
                    ConversationModel.memory_watermark,
                    ConversationModel.memory_last_extracted_at,
                ).where(ConversationModel.id == conversation_id)
            )).first()
            if row is None:
                return 0, None
            return int(row[0] or 0), row[1]

    async def get_messages_after(
        self, conversation_id: str, watermark: int,
    ) -> list[dict[str, Any]]:
        """Return message *content* (role + text + ordering) with ``ordering > watermark``.

        Only ``MessageModel.content`` is read — reasoning ``thinking_steps`` are
        excluded by construction. Ordered oldest → newest.
        """
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(
                    MessageModel.role,
                    MessageModel.content,
                    MessageModel.ordering,
                )
                .where(
                    MessageModel.conversation_id == conversation_id,
                    MessageModel.ordering > watermark,
                    MessageModel.role.in_(_CONTENT_ROLES),
                )
                .order_by(MessageModel.ordering.asc())
            )).all()
        return [
            {"role": r[0], "content": r[1] or "", "ordering": int(r[2] or 0)}
            for r in rows
        ]

    async def unextracted_content_tokens(
        self, conversation_id: str, watermark: int,
    ) -> int:
        """Total message-content tokens (content only) since the watermark."""
        messages = await self.get_messages_after(conversation_id, watermark)
        return sum(count_content_tokens(m["content"]) for m in messages)

    # ── writes ───────────────────────────────────────────────────────────

    async def add_short_term(
        self, conversation_id: str, profile: str, content: str, token_count: int,
        queue_size: int,
    ) -> dict[str, Any]:
        """Insert a short-term entry, then FIFO-evict beyond ``queue_size``."""
        async with self.async_session_maker.begin() as session:
            next_order = (await session.execute(
                select(func.coalesce(func.max(ShortTermMemoryModel.ordering), 0))
                .where(ShortTermMemoryModel.conversation_id == conversation_id)
            )).scalar_one() + 1
            row = ShortTermMemoryModel(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                profile=profile,
                content=content,
                token_count=int(token_count),
                ordering=next_order,
                created_at=time.time(),
            )
            session.add(row)
            await session.flush()
            await self._evict_short_term(session, conversation_id, queue_size)
            result = self._short_to_dict(row)
        return result

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

    async def set_watermark(
        self, conversation_id: str, ordering: int, ts: float | None = None,
    ) -> None:
        """Advance the conversation's extraction watermark + last-extracted timestamp."""
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == conversation_id)
                .values(
                    memory_watermark=int(ordering),
                    memory_last_extracted_at=ts if ts is not None else time.time(),
                )
            )

    # ── eviction helpers ───────────────────────────────────────────────────

    @staticmethod
    async def _evict_short_term(session, conversation_id: str, queue_size: int) -> None:
        if queue_size <= 0:
            return
        ids = (await session.execute(
            select(ShortTermMemoryModel.id)
            .where(ShortTermMemoryModel.conversation_id == conversation_id)
            .order_by(ShortTermMemoryModel.ordering.desc())
            .offset(queue_size)
        )).scalars().all()
        if ids:
            await session.execute(
                delete(ShortTermMemoryModel).where(ShortTermMemoryModel.id.in_(ids))
            )
            logger.debug(
                f"[memory] evicted {len(ids)} short-term entry(s) for conv={conversation_id}"
            )

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
    def _short_to_dict(row: ShortTermMemoryModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "content": row.content,
            "token_count": row.token_count,
            "created_at": row.created_at,
        }

    @staticmethod
    def _long_to_dict(row: LongTermMemoryModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "content": row.content,
            "token_count": row.token_count,
            "source_conversation_id": row.source_conversation_id,
            "created_at": row.created_at,
        }
