"""Async storage for group chats — rooms whose participants are whole profiles.

Schema lives in :mod:`app.storage.models` (``group_chats``,
``group_chat_members``, ``group_chat_messages``); the tables are created by
Alembic. This class only reads and writes them.

Async for the same reason as :class:`app.storage.event_run_storage.EventRunStorage`:
every caller — the fan-out service, the channel adapters, the REST API and the
boot sweeps — already runs on the event loop.

Two things here are load-bearing rather than incidental:

**Duplicate posts are refused, not raised.** :meth:`add_message` returns ``None``
when the row would collide with an existing ``(source_message_id, segment)`` — a
re-post of an agent turn a crash interrupted. The caller reads ``None`` as
"already said" and skips the fan-out, so a boot sweep cannot answer one message
twice. The collision is checked first and caught second: a concurrent inserter
can still win the race between the two, and the ``IntegrityError`` is handled
OUTSIDE the transaction block so the session is already rolled back when it
surfaces — an error caught inside would leave a poisoned transaction on
PostgreSQL.

**Hop accounting is a query, not a counter.** :meth:`last_user_ordering` and
:meth:`max_agent_hop_after` let the caller derive how far the current message is
from the last human one straight from the timeline. Keeping it in the DB rather than
in memory means a turn that dies and is re-run cannot restart the chain, which
is the whole point of the cap.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Iterable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.databases import DatabaseProvider, get_database_provider
from app.storage.models import (
    GroupChatMemberModel,
    GroupChatMessageModel,
    GroupChatModel,
)
from app.utils.logger import logger


class GroupChatStorage:
    """Async CRUD for the four group-chat tables."""

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

    # ── groups ────────────────────────────────────────────────────────────

    async def create_group(
        self,
        *,
        name: str,
        settings: dict[str, Any] | None = None,
        created_by: str | None = None,
        members: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Create a group and seat its members. Returns the full group dict."""
        now = time.time() * 1000
        gid = str(uuid.uuid4())
        async with self.async_session_maker.begin() as session:
            session.add(GroupChatModel(
                id=gid,
                name=name,
                settings=settings or {},
                created_by=created_by,
                created_at=now,
                updated_at=now,
            ))
            await session.flush()
            for profile in dict.fromkeys(members):  # de-dupe, keep order
                session.add(GroupChatMemberModel(
                    group_id=gid, profile=profile, joined_at=now,
                ))
        return await self.get_group(gid) or {}

    async def get_group(self, group_id: str) -> dict[str, Any] | None:
        """One group with its members, or ``None``."""
        if not group_id:
            return None
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(GroupChatModel).where(GroupChatModel.id == group_id)
            )).scalar_one_or_none()
            if row is None:
                return None
            return await self._group_to_dict(session, row)

    async def group_exists(self, group_id: str) -> bool:
        """Whether the row is still there, without assembling the group.

        :meth:`get_group` costs several queries and a dict full of members;
        this is for the callers that only need to know the room has
        not been deleted out from under them (a seat turn publishing its
        closing status after the group it belonged to was removed).
        """
        if not group_id:
            return False
        async with self.async_session_maker() as session:
            found = (await session.execute(
                select(GroupChatModel.id)
                .where(GroupChatModel.id == group_id)
                .limit(1)
            )).scalar_one_or_none()
            return found is not None

    async def find_group(self, name_or_id: str) -> list[dict[str, Any]]:
        """Groups matching an id exactly, else a case-insensitive name.

        Returns a list so an ambiguous name is the caller's decision to report
        rather than a silent pick.
        """
        ref = (name_or_id or "").strip()
        if not ref:
            return []
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(GroupChatModel).where(GroupChatModel.id == ref)
            )).scalar_one_or_none()
            if row is not None:
                return [await self._group_to_dict(session, row)]
            rows = (await session.execute(
                select(GroupChatModel).where(
                    func.lower(GroupChatModel.name) == ref.lower()
                )
            )).scalars().all()
            return [await self._group_to_dict(session, r) for r in rows]

    async def list_groups(self, *, member: str | None = None) -> list[dict[str, Any]]:
        """Every group, or only those ``member`` sits in. Newest activity first."""
        async with self.async_session_maker() as session:
            stmt = select(GroupChatModel)
            if member is not None:
                stmt = stmt.join(
                    GroupChatMemberModel,
                    GroupChatMemberModel.group_id == GroupChatModel.id,
                ).where(GroupChatMemberModel.profile == member)
            stmt = stmt.order_by(GroupChatModel.updated_at.desc())
            rows = (await session.execute(stmt)).scalars().all()
            out = []
            for row in rows:
                group = await self._group_to_dict(session, row)
                group["last_message"] = await self._last_message(session, row.id)
                out.append(group)
            return out

    async def update_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Patch name and/or settings. ``settings`` replaces the whole blob."""
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(GroupChatModel).where(GroupChatModel.id == group_id)
            )).scalar_one_or_none()
            if row is None:
                return None
            if name is not None:
                row.name = name
            if settings is not None:
                row.settings = settings
            row.updated_at = time.time() * 1000
            session.add(row)
        return await self.get_group(group_id)

    async def touch_group(self, group_id: str) -> None:
        """Bump ``updated_at`` so the group list sorts by recent activity."""
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(GroupChatModel).where(GroupChatModel.id == group_id)
            )).scalar_one_or_none()
            if row is not None:
                row.updated_at = time.time() * 1000
                session.add(row)

    async def set_members(
        self, group_id: str, profiles: Iterable[str],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Make the membership exactly ``profiles``.

        Returns ``(added_profiles, removed_member_rows)`` — the removed rows
        carry their ``shadow_conversation_id`` so the caller can tear those
        conversations down.
        """
        wanted = list(dict.fromkeys(p for p in profiles if p))
        now = time.time() * 1000
        added: list[str] = []
        removed: list[dict[str, Any]] = []
        async with self.async_session_maker.begin() as session:
            existing = (await session.execute(
                select(GroupChatMemberModel).where(
                    GroupChatMemberModel.group_id == group_id
                )
            )).scalars().all()
            current = {m.profile: m for m in existing}
            for profile in wanted:
                if profile not in current:
                    session.add(GroupChatMemberModel(
                        group_id=group_id, profile=profile, joined_at=now,
                    ))
                    added.append(profile)
            for profile, row in current.items():
                if profile not in wanted:
                    removed.append(self._member_to_dict(row))
                    await session.delete(row)
        return added, removed

    async def set_shadow_conversation(
        self, group_id: str, profile: str, conversation_id: str | None,
    ) -> None:
        """Record (or clear) a member's seat conversation."""
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(GroupChatMemberModel).where(
                    GroupChatMemberModel.group_id == group_id,
                    GroupChatMemberModel.profile == profile,
                )
            )).scalar_one_or_none()
            if row is not None:
                row.shadow_conversation_id = conversation_id
                session.add(row)

    async def get_member(self, group_id: str, profile: str) -> dict[str, Any] | None:
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(GroupChatMemberModel).where(
                    GroupChatMemberModel.group_id == group_id,
                    GroupChatMemberModel.profile == profile,
                )
            )).scalar_one_or_none()
            return self._member_to_dict(row) if row else None

    async def list_members(self, group_id: str) -> list[dict[str, Any]]:
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(GroupChatMemberModel)
                .where(GroupChatMemberModel.group_id == group_id)
                .order_by(GroupChatMemberModel.profile)
            )).scalars().all()
            return [self._member_to_dict(r) for r in rows]

    async def list_memberships(self) -> list[dict[str, Any]]:
        """Every membership row in the system — the in-memory index's source."""
        async with self.async_session_maker() as session:
            rows = (await session.execute(select(GroupChatMemberModel))).scalars().all()
            return [self._member_to_dict(r) for r in rows]

    async def delete_group(self, group_id: str) -> bool:
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(GroupChatModel).where(GroupChatModel.id == group_id)
            )).scalar_one_or_none()
            if row is None:
                return False
            # Children CASCADE at the DB level, but SQLite only enforces that
            # with foreign_keys=ON; delete them explicitly so the outcome does
            # not depend on a pragma.
            await session.execute(
                delete(GroupChatMessageModel).where(
                    GroupChatMessageModel.group_id == group_id
                )
            )
            await session.execute(
                delete(GroupChatMemberModel).where(
                    GroupChatMemberModel.group_id == group_id
                )
            )
            await session.delete(row)
        return True

    # ── messages ──────────────────────────────────────────────────────────

    async def add_message(
        self,
        *,
        group_id: str,
        sender_kind: str,
        sender_name: str,
        content: str,
        hop: int = 0,
        sender_profile: str | None = None,
        sender_identity: dict[str, Any] | None = None,
        source_conversation_id: str | None = None,
        source_message_id: str | None = None,
        segment: int = 0,
        delivered_to: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Append one post to the timeline, or ``None`` when it is a duplicate.

        ``None`` means an equal ``(source_message_id, segment)`` row already
        exists — a re-post of an agent turn. The caller must treat it as
        "already said" and skip the fan-out.
        """
        now = time.time() * 1000
        mid = str(uuid.uuid4())
        try:
            async with self.async_session_maker.begin() as session:
                if source_message_id and await self._source_exists(
                    session, source_message_id, segment,
                ):
                    return None
                next_ordering = (await session.execute(
                    select(func.coalesce(func.max(GroupChatMessageModel.ordering), -1))
                    .where(GroupChatMessageModel.group_id == group_id)
                )).scalar() + 1
                row = GroupChatMessageModel(
                    id=mid,
                    group_id=group_id,
                    ordering=next_ordering,
                    sender_kind=sender_kind,
                    sender_profile=sender_profile,
                    sender_name=sender_name,
                    sender_identity=sender_identity,
                    content=content,
                    hop=int(hop or 0),
                    source_conversation_id=source_conversation_id,
                    source_message_id=source_message_id,
                    segment=int(segment or 0),
                    delivered_to=list(delivered_to or []),
                    message_metadata=metadata,
                    created_at=now,
                )
                session.add(row)
                await session.flush()
                result = self._message_to_dict(row)
        except IntegrityError:
            # Lost the race between the checks above and the insert. The other
            # writer's row stands; ours is rolled back by the context manager
            # (so the session is clean by the time we get here).
            logger.debug(
                f"[group] duplicate message refused "
                f"(source={source_message_id!r}/{segment})"
            )
            return None
        return result

    async def list_messages(
        self,
        group_id: str,
        *,
        after: int = -1,
        limit: int = 200,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """Timeline slice, oldest-first by default.

        ``newest_first=True`` takes the newest ``limit`` rows and returns them
        oldest-first, which is what a room opening on a long history wants.
        """
        async with self.async_session_maker() as session:
            stmt = select(GroupChatMessageModel).where(
                GroupChatMessageModel.group_id == group_id,
                GroupChatMessageModel.ordering > after,
            )
            if newest_first:
                stmt = stmt.order_by(GroupChatMessageModel.ordering.desc()).limit(limit)
                rows = list((await session.execute(stmt)).scalars().all())
                rows.reverse()
            else:
                stmt = stmt.order_by(GroupChatMessageModel.ordering).limit(limit)
                rows = list((await session.execute(stmt)).scalars().all())
            return [self._message_to_dict(r) for r in rows]

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(GroupChatMessageModel).where(
                    GroupChatMessageModel.id == message_id
                )
            )).scalar_one_or_none()
            return self._message_to_dict(row) if row else None

    async def find_by_source(self, source_message_id: str) -> list[dict[str, Any]]:
        """Every timeline row produced by one agent turn (its segments)."""
        if not source_message_id:
            return []
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(GroupChatMessageModel)
                .where(GroupChatMessageModel.source_message_id == source_message_id)
                .order_by(GroupChatMessageModel.segment)
            )).scalars().all()
            return [self._message_to_dict(r) for r in rows]

    async def rekey_source(
        self, from_source_id: str, to_source_id: str,
    ) -> list[str]:
        """Re-point rows from a provisional source id to the real one.

        A seat that speaks mid-turn posts before its turn's message exists, so
        those rows are keyed by the run instead (see
        :func:`app.groups.hooks.live_source_key`). When the turn finally
        persists, this hands them their real owner — which is what lets the
        Thinking-Process panel find the trace behind an interim post, and what
        makes a re-run of the completion hook recognise them as already said.

        Returns the ids it moved, oldest first, so the caller can count them as
        posted. A no-op when nothing carries the provisional key.
        """
        if not from_source_id or not to_source_id:
            return []
        async with self.async_session_maker.begin() as session:
            rows = list((await session.execute(
                select(GroupChatMessageModel)
                .where(GroupChatMessageModel.source_message_id == from_source_id)
                .order_by(GroupChatMessageModel.segment)
            )).scalars().all())
            moved: list[str] = []
            for row in rows:
                row.source_message_id = to_source_id
                session.add(row)
                moved.append(row.id)
            return moved

    async def last_user_ordering(self, group_id: str) -> int:
        """Ordering of the newest message a PERSON posted, or ``-1``.

        This is the floor the hop counter measures from: a human speaking starts
        the conversation over, which is what lets a room that stopped answering
        itself pick back up the moment someone asks for something. Deliberately
        only ``user`` — a ``system`` notice ("an agent's turn failed") is the room
        talking about itself and must not hand the agents a fresh budget.
        """
        async with self.async_session_maker() as session:
            value = (await session.execute(
                select(func.max(GroupChatMessageModel.ordering)).where(
                    GroupChatMessageModel.group_id == group_id,
                    GroupChatMessageModel.sender_kind == "user",
                )
            )).scalar()
            return -1 if value is None else int(value)

    async def max_agent_hop_after(self, group_id: str, after_ordering: int) -> int:
        """Highest hop among agent posts since ``after_ordering``, or ``-1``.

        Counts the poster's OWN earlier posts too: an agent that keeps talking
        with nobody answering is exactly the runaway the cap exists to stop, and
        exempting it from its own chain would let it monologue forever.
        """
        async with self.async_session_maker() as session:
            value = (await session.execute(
                select(func.max(GroupChatMessageModel.hop)).where(
                    GroupChatMessageModel.group_id == group_id,
                    GroupChatMessageModel.ordering > after_ordering,
                    GroupChatMessageModel.sender_kind == "agent",
                )
            )).scalar()
            return -1 if value is None else int(value)

    async def count_agent_posts_since(self, group_id: str, since_ms: float) -> int:
        """Agent posts in the recent window — the flood guard's input."""
        async with self.async_session_maker() as session:
            value = (await session.execute(
                select(func.count()).select_from(GroupChatMessageModel).where(
                    GroupChatMessageModel.group_id == group_id,
                    GroupChatMessageModel.sender_kind == "agent",
                    GroupChatMessageModel.created_at >= since_ms,
                )
            )).scalar()
            return int(value or 0)

    async def update_message_metadata(
        self, message_id: str, patch: dict[str, Any],
    ) -> None:
        """Merge top-level keys into one post's metadata blob.

        Shallow on purpose, like
        :meth:`app.storage.conversation_storage.ConversationStorage.update_message_metadata`:
        a caller replaces a whole key or leaves it alone, so two writers of
        different keys cannot half-overwrite each other's sub-dicts.

        Exists because the routing stamp is written AFTER the row: the
        classification is a provider round trip, and
        :func:`app.groups.fanout.post_message` inserts first so that the timeline
        records posts in arrival order rather than in the order a model happened
        to answer.
        """
        if not message_id or not patch:
            return
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(GroupChatMessageModel).where(
                    GroupChatMessageModel.id == message_id
                )
            )).scalar_one_or_none()
            if row is None:
                return
            # A fresh dict, not a mutation: the column is a plain JSON type with
            # no change tracking, so an in-place update would never be flushed.
            row.message_metadata = {**(row.message_metadata or {}), **patch}
            session.add(row)

    async def update_delivered_to(
        self, message_id: str, profiles: Iterable[str],
    ) -> None:
        """Record which members' seats now hold this message."""
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(GroupChatMessageModel).where(
                    GroupChatMessageModel.id == message_id
                )
            )).scalar_one_or_none()
            if row is None:
                return
            merged = list(dict.fromkeys(list(row.delivered_to or []) + list(profiles)))
            row.delivered_to = merged
            session.add(row)

    # ── mappers ───────────────────────────────────────────────────────────

    async def _group_to_dict(self, session, row: GroupChatModel) -> dict[str, Any]:
        members = (await session.execute(
            select(GroupChatMemberModel)
            .where(GroupChatMemberModel.group_id == row.id)
            .order_by(GroupChatMemberModel.profile)
        )).scalars().all()
        return {
            "id": row.id,
            "name": row.name,
            "settings": row.settings or {},
            "created_by": row.created_by,
            "members": [m.profile for m in members],
            "member_rows": [self._member_to_dict(m) for m in members],
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def _last_message(self, session, group_id: str) -> dict[str, Any] | None:
        row = (await session.execute(
            select(GroupChatMessageModel)
            .where(GroupChatMessageModel.group_id == group_id)
            .order_by(GroupChatMessageModel.ordering.desc())
            .limit(1)
        )).scalar_one_or_none()
        if row is None:
            return None
        return {
            "sender_kind": row.sender_kind,
            "sender_name": row.sender_name,
            "content": (row.content or "")[:200],
            "created_at": row.created_at,
        }

    async def _source_exists(self, session, source_message_id: str, segment: int) -> bool:
        found = (await session.execute(
            select(GroupChatMessageModel.id).where(
                GroupChatMessageModel.source_message_id == source_message_id,
                GroupChatMessageModel.segment == int(segment or 0),
            ).limit(1)
        )).scalar_one_or_none()
        return found is not None

    @staticmethod
    def _member_to_dict(row: GroupChatMemberModel) -> dict[str, Any]:
        return {
            "group_id": row.group_id,
            "profile": row.profile,
            "shadow_conversation_id": row.shadow_conversation_id,
            "joined_at": row.joined_at,
        }

    @staticmethod
    def _message_to_dict(row: GroupChatMessageModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "group_id": row.group_id,
            "ordering": row.ordering,
            "sender_kind": row.sender_kind,
            "sender_profile": row.sender_profile,
            "sender_name": row.sender_name,
            "sender_identity": row.sender_identity or {},
            "content": row.content,
            "hop": row.hop,
            "source_conversation_id": row.source_conversation_id,
            "source_message_id": row.source_message_id,
            "segment": row.segment,
            "delivered_to": list(row.delivered_to or []),
            "metadata": row.message_metadata or {},
            "created_at": row.created_at,
        }


_instance: Optional[GroupChatStorage] = None


def get_group_chat_storage(provider: DatabaseProvider | None = None) -> GroupChatStorage:
    """Process-wide storage singleton (see :mod:`app.storage`)."""
    global _instance
    if _instance is None:
        _instance = GroupChatStorage(provider)
    return _instance
