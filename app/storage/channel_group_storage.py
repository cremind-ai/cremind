"""Async storage for channel group chats — platform groups an agent is in.

Schema lives in :mod:`app.storage.models` (``channel_groups``,
``channel_group_members``); the tables are created by Alembic. This class only
reads and writes them.

Two things here are load-bearing rather than incidental:

**Discovery is idempotent.** :meth:`create_group` catches the
``UNIQUE(channel_id, platform_chat_id)`` violation and re-reads the winner's row
instead of raising. Two messages from an unknown group can arrive on the same
tick, and the alternative to losing gracefully is two pending rows and two
notifications for one group.

**The roster and the seen list are one table with a precedence rule.** A platform
member list, where it can be read at all, carries display names and admin flags;
somebody merely posting carries neither but proves they are there. So
:meth:`upsert_member` lets a ``roster`` write overwrite what it knows and a
``seen`` write only fill in blanks — otherwise every message from a named member
would erase the name the roster gave them.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.databases import DatabaseProvider, get_database_provider
from app.storage.models import ChannelGroupMemberModel, ChannelGroupModel
from app.utils.logger import logger

# Spelled out rather than imported from :mod:`app.channels.groups.constants`,
# which is where they are documented: importing the channels package from here
# would close a cycle (``app.channels`` pulls in the registry, which pulls in
# ``app.storage``). These four are the columns' own vocabulary and are pinned
# against the canonical names by ``tests/storage/test_channel_group_storage.py``.
_STATUS_PENDING = "pending"
_DISCOVERED_VIA_MESSAGE = "message"
_MEMBER_SOURCE_ROSTER = "roster"
_MEMBER_SOURCE_SEEN = "seen"


class ChannelGroupStorage:
    """Async CRUD for the two channel-group tables."""

    def __init__(self, provider: DatabaseProvider | None = None):
        self.provider = provider or get_database_provider()
        self._engine: AsyncEngine | None = None
        self._session_maker: async_sessionmaker | None = None

    @property
    def async_session_maker(self) -> async_sessionmaker:
        if self._session_maker is None:
            self._engine = self.provider.async_engine()
            self._session_maker = async_sessionmaker(
                self._engine, expire_on_commit=False,
            )
        return self._session_maker

    # ── groups ────────────────────────────────────────────────────────────

    async def create_group(
        self,
        *,
        channel_id: str,
        profile: str,
        platform_chat_id: str,
        chat_type: str | None = None,
        title: str | None = None,
        discovered_via: str = _DISCOVERED_VIA_MESSAGE,
        status: str = _STATUS_PENDING,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a newly-discovered group, or return the one already there.

        Never raises on a duplicate: the caller is an inbound message handler
        and "somebody else just created this" is a normal outcome, not an error.
        """
        now = time.time() * 1000
        gid = str(uuid.uuid4())
        try:
            async with self.async_session_maker.begin() as session:
                session.add(ChannelGroupModel(
                    id=gid,
                    channel_id=channel_id,
                    profile=profile,
                    platform_chat_id=str(platform_chat_id),
                    chat_type=chat_type,
                    title=title,
                    status=status,
                    discovered_via=discovered_via,
                    settings=settings,
                    created_at=now,
                    updated_at=now,
                ))
        except IntegrityError:
            existing = await self.get_group_by_chat(channel_id, platform_chat_id)
            if existing is not None:
                logger.debug(
                    f"[channel_group] {channel_id}:{platform_chat_id} was created "
                    "concurrently; using the existing row"
                )
                return existing
            raise
        return await self.get_group(gid) or {}

    async def get_group(self, group_id: str) -> dict[str, Any] | None:
        if not group_id:
            return None
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(ChannelGroupModel).where(ChannelGroupModel.id == group_id)
            )).scalar_one_or_none()
            if row is None:
                return None
            return await self._group_to_dict(session, row)

    async def get_group_by_chat(
        self, channel_id: str, platform_chat_id: str,
    ) -> dict[str, Any] | None:
        """The inbound path's lookup: one indexed hit on the unique pair."""
        if not channel_id or not platform_chat_id:
            return None
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(ChannelGroupModel).where(
                    ChannelGroupModel.channel_id == channel_id,
                    ChannelGroupModel.platform_chat_id == str(platform_chat_id),
                )
            )).scalar_one_or_none()
            if row is None:
                return None
            return await self._group_to_dict(session, row)

    async def get_group_by_conversation(
        self, conversation_id: str,
    ) -> dict[str, Any] | None:
        """Which group a conversation belongs to, if any.

        Read by the reply forwarder when a run it did not start finishes in a
        group conversation (a mid-turn flush, an event run), and by the prompt's
        origin resolver.
        """
        if not conversation_id:
            return None
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(ChannelGroupModel).where(
                    ChannelGroupModel.conversation_id == conversation_id
                )
            )).scalar_one_or_none()
            if row is None:
                return None
            return await self._group_to_dict(session, row)

    async def list_groups(
        self, channel_id: str, *, status: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self.async_session_maker() as session:
            stmt = select(ChannelGroupModel).where(
                ChannelGroupModel.channel_id == channel_id
            )
            if status:
                stmt = stmt.where(ChannelGroupModel.status == status)
            rows = (await session.execute(
                stmt.order_by(ChannelGroupModel.created_at.desc())
            )).scalars().all()
            return [await self._group_to_dict(session, r) for r in rows]

    async def list_groups_for_profile(self, profile: str) -> list[dict[str, Any]]:
        async with self.async_session_maker() as session:
            rows = (await session.execute(
                select(ChannelGroupModel)
                .where(ChannelGroupModel.profile == profile)
                .order_by(ChannelGroupModel.created_at.desc())
            )).scalars().all()
            return [await self._group_to_dict(session, r) for r in rows]

    async def update_group(self, group_id: str, **fields: Any) -> dict[str, Any] | None:
        """Patch a group. Unknown and ``None`` fields are ignored.

        ``None`` means "not supplied" rather than "set to null" — every nullable
        column here is either set by a specific call site that passes a value or
        left alone, and a caller patching one field must not blank the rest.
        """
        allowed = {
            "title", "chat_type", "status", "conversation_id", "settings",
            "roster_refreshed_at", "last_message_at", "discovered_via",
        }
        patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not patch:
            return await self.get_group(group_id)
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(ChannelGroupModel).where(ChannelGroupModel.id == group_id)
            )).scalar_one_or_none()
            if row is None:
                return None
            for key, value in patch.items():
                setattr(row, key, value)
            row.updated_at = time.time() * 1000
            session.add(row)
        return await self.get_group(group_id)

    async def delete_group(self, group_id: str) -> bool:
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(ChannelGroupModel).where(ChannelGroupModel.id == group_id)
            )).scalar_one_or_none()
            if row is None:
                return False
            # Children CASCADE at the DB level, but SQLite only enforces that
            # with foreign_keys=ON; delete them explicitly so the outcome does
            # not depend on a pragma.
            members = (await session.execute(
                select(ChannelGroupMemberModel).where(
                    ChannelGroupMemberModel.group_id == group_id
                )
            )).scalars().all()
            for member in members:
                await session.delete(member)
            await session.delete(row)
        return True

    # ── members ───────────────────────────────────────────────────────────

    async def upsert_member(
        self,
        group_id: str,
        *,
        member_id: str,
        alt_ids: Sequence[str] | None = None,
        display_name: str | None = None,
        username: str | None = None,
        is_bot: bool = False,
        role: str | None = None,
        source: str = _MEMBER_SOURCE_SEEN,
        seen_at: float | None = None,
        count_message: bool = False,
    ) -> dict[str, Any] | None:
        """Record one member. ``roster`` overwrites; ``seen`` only fills blanks.

        Returns the stored row, or ``None`` when the group is gone (a message
        arriving while the operator forgets the group — nothing to record).
        """
        if not group_id or not member_id:
            return None
        now = seen_at if seen_at is not None else time.time() * 1000
        alts = [str(v).strip() for v in (alt_ids or ()) if str(v or "").strip()]
        try:
            async with self.async_session_maker.begin() as session:
                row = (await session.execute(
                    select(ChannelGroupMemberModel).where(
                        ChannelGroupMemberModel.group_id == group_id,
                        ChannelGroupMemberModel.member_id == str(member_id),
                    )
                )).scalar_one_or_none()
                if row is None:
                    row = ChannelGroupMemberModel(
                        id=str(uuid.uuid4()),
                        group_id=group_id,
                        member_id=str(member_id),
                        alt_ids=alts or None,
                        display_name=display_name,
                        username=username,
                        is_bot=bool(is_bot),
                        role=role,
                        source=source,
                        first_seen_at=now,
                        last_seen_at=now,
                        message_count=1 if count_message else 0,
                    )
                    session.add(row)
                    await session.flush()
                    return self._member_to_dict(row)

                authoritative = source == _MEMBER_SOURCE_ROSTER
                if alts and (authoritative or not row.alt_ids):
                    row.alt_ids = alts
                if display_name and (authoritative or not row.display_name):
                    row.display_name = display_name
                if username and (authoritative or not row.username):
                    row.username = username
                if role and (authoritative or not row.role):
                    row.role = role
                if is_bot:
                    # Only ever set, never cleared: the platform that says "bot"
                    # knows, and the ones that say nothing say nothing.
                    row.is_bot = True
                if authoritative:
                    row.source = _MEMBER_SOURCE_ROSTER
                if source == _MEMBER_SOURCE_SEEN or row.last_seen_at is None:
                    row.last_seen_at = now
                if count_message:
                    row.message_count = int(row.message_count or 0) + 1
                session.add(row)
                await session.flush()
                return self._member_to_dict(row)
        except IntegrityError:
            logger.debug(
                f"[channel_group] concurrent member upsert for {member_id}",
                exc_info=True,
            )
            return None

    async def list_members(self, group_id: str) -> list[dict[str, Any]]:
        async with self.async_session_maker() as session:
            return await self._members(session, group_id)

    async def replace_roster(
        self, group_id: str, members: Iterable[dict[str, Any]],
    ) -> int:
        """Apply a full platform member list. Returns how many it wrote.

        Members the platform no longer lists are demoted to ``seen`` rather than
        deleted: somebody who left is still the author of messages in the
        transcript, and the member policy may still name them.
        """
        written = 0
        listed: set[str] = set()
        for entry in members or ():
            member_id = str(entry.get("member_id") or "").strip()
            if not member_id:
                continue
            listed.add(member_id)
            stored = await self.upsert_member(
                group_id,
                member_id=member_id,
                alt_ids=entry.get("alt_ids") or (),
                display_name=entry.get("display_name"),
                username=entry.get("username"),
                is_bot=bool(entry.get("is_bot")),
                role=entry.get("role"),
                source=_MEMBER_SOURCE_ROSTER,
            )
            if stored is not None:
                written += 1
        if listed:
            async with self.async_session_maker.begin() as session:
                rows = (await session.execute(
                    select(ChannelGroupMemberModel).where(
                        ChannelGroupMemberModel.group_id == group_id,
                        ChannelGroupMemberModel.source == _MEMBER_SOURCE_ROSTER,
                    )
                )).scalars().all()
                for row in rows:
                    if row.member_id not in listed:
                        row.source = _MEMBER_SOURCE_SEEN
                        session.add(row)
        return written

    # ── mapping ───────────────────────────────────────────────────────────

    async def _group_to_dict(self, session, row: ChannelGroupModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "channel_id": row.channel_id,
            "profile": row.profile,
            "platform_chat_id": row.platform_chat_id,
            "chat_type": row.chat_type,
            "title": row.title or "",
            "status": row.status,
            "discovered_via": row.discovered_via,
            "conversation_id": row.conversation_id,
            "settings": row.settings or {},
            "members": await self._members(session, row.id),
            "roster_refreshed_at": row.roster_refreshed_at,
            "last_message_at": row.last_message_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def _members(self, session, group_id: str) -> list[dict[str, Any]]:
        rows = (await session.execute(
            select(ChannelGroupMemberModel)
            .where(ChannelGroupMemberModel.group_id == group_id)
            .order_by(ChannelGroupMemberModel.member_id)
        )).scalars().all()
        return [self._member_to_dict(r) for r in rows]

    @staticmethod
    def _member_to_dict(row: ChannelGroupMemberModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "group_id": row.group_id,
            "member_id": row.member_id,
            "alt_ids": list(row.alt_ids or []),
            "display_name": row.display_name or "",
            "username": row.username or "",
            "is_bot": bool(row.is_bot),
            "role": row.role,
            "source": row.source,
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
            "message_count": int(row.message_count or 0),
        }


_instance: Optional[ChannelGroupStorage] = None


def get_channel_group_storage(
    provider: DatabaseProvider | None = None,
) -> ChannelGroupStorage:
    """Process-wide storage singleton (see :mod:`app.storage`)."""
    global _instance
    if _instance is None:
        _instance = ChannelGroupStorage(provider)
    return _instance
