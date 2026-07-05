import re
import time
import uuid

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.databases import DatabaseProvider, get_database_provider
from app.storage.migrations import ensure_at_head
from app.storage.models import (
    ChannelModel, ChannelSenderModel, ConversationModel,
    MessageModel, ProfileModel,
)
from app.utils.logger import logger


# Conversation id format for user-renamed ids. Server-allocated UUIDs already
# satisfy this (lowercase hex + hyphen, leading char alphanumeric). The regex
# is the single source of truth — frontend duplicates it for UX validation
# but the server is authoritative.
_CONVERSATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


def is_valid_conversation_id(s: str) -> bool:
    """True iff `s` is a syntactically valid conversation id.

    Allowed: lowercase a-z, digits 0-9, `-`, `_`. Must start with a letter or
    digit (no leading separator). Length 1..128.
    """
    return isinstance(s, str) and bool(_CONVERSATION_ID_RE.match(s))


class ConversationStorage:
    """Async conversation and message storage. Backend chosen by DatabaseProvider."""

    def __init__(self, provider: DatabaseProvider | None = None):
        # ``provider`` is allowed to be None so the storage singleton getters
        # can stay agnostic about whether the global has been initialized
        # yet. Engine acquisition is deferred to first access — on SQLite,
        # ``provider.async_engine()`` calls ``_ensure_parent_dir()`` which
        # would create ``<CREMIND_SYSTEM_DIR>/storage/`` before the Setup Wizard has
        # picked a backend. The wizard's hot-swap path still relies on
        # ``invalidate_storage_singletons()`` to rebuild this instance.
        self.provider = provider or get_database_provider()
        self._engine: AsyncEngine | None = None
        self._session_maker: async_sessionmaker | None = None
        self._initialized = False

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = self.provider.async_engine()
            self._session_maker = async_sessionmaker(self._engine, expire_on_commit=False)
        return self._engine

    @property
    def async_session_maker(self) -> async_sessionmaker:
        if self._session_maker is None:
            # Trigger engine construction (also builds the session maker).
            _ = self.engine
        return self._session_maker  # type: ignore[return-value]

    async def initialize(self):
        """Bring the schema to the latest revision and run idempotent backfills.

        Schema management is delegated to Alembic via
        :func:`app.storage.migrations.ensure_at_head` — that function knows
        how to handle fresh installs, already-stamped installs, and legacy
        pre-Alembic installs (compat preflight + stamp + upgrade).
        """
        if self._initialized:
            return

        # Per-backend session setup (WAL + FK on SQLite; no-op on Postgres).
        # Run before migrations so Alembic's connections inherit the same
        # pragmas the application uses.
        async with self.engine.begin() as conn:
            await self.provider.apply_pragmas(conn)

        # Migrate to head — creates everything on a fresh DB, no-ops on a DB
        # already at head, and stamp+upgrades a legacy install.
        await ensure_at_head()

        # Backfill: ensure every existing profile has a main channel and that
        # every existing conversation points at it. Idempotent — runs after
        # every migration in case a new release adds profiles outside the
        # storage layer (e.g., bulk import migration).
        await self._backfill_main_channels()

        self._initialized = True
        logger.info(f"ConversationStorage initialized (backend={self.provider.name})")

    async def _backfill_main_channels(self) -> None:
        """Ensure each profile has a ``main`` channel and back-fill ``channel_id``.

        Called once at the end of ``initialize()``. Safe to run on a clean DB
        (it's a no-op when there are no profiles yet).
        """
        async with self.async_session_maker.begin() as session:
            profiles = (await session.execute(
                select(ProfileModel.name)
            )).scalars().all()
            for profile_name in profiles:
                main_id = await self._ensure_main_channel(session, profile_name)
                # Back-fill conversations missing channel_id for this profile.
                await session.execute(
                    update(ConversationModel)
                    .where(
                        ConversationModel.profile == profile_name,
                        ConversationModel.channel_id.is_(None),
                    )
                    .values(channel_id=main_id)
                )

    @staticmethod
    async def _ensure_main_channel(session: AsyncSession, profile: str) -> str:
        """Return the id of the ``main`` channel for ``profile``, creating it if needed.

        Must run inside a caller-provided session/transaction so the create is
        atomic with adjacent writes.
        """
        existing = (await session.execute(
            select(ChannelModel.id).where(
                ChannelModel.profile == profile,
                ChannelModel.channel_type == "main",
            )
        )).scalar_one_or_none()
        if existing:
            return existing
        now = time.time() * 1000
        ch = ChannelModel(
            id=str(uuid.uuid4()),
            profile=profile,
            channel_type="main",
            mode="bot",
            auth_mode="none",
            response_mode="normal",
            enabled=True,
            config=None,
            state=None,
            created_at=now,
            updated_at=now,
        )
        session.add(ch)
        await session.flush()
        return ch.id

    async def _ensure_initialized(self):
        if not self._initialized:
            await self.initialize()

    # ── Profile CRUD ──

    async def create_profile(self, name: str) -> dict:
        await self._ensure_initialized()
        now = time.time() * 1000
        profile = ProfileModel(
            id=str(uuid.uuid4()),
            name=name,
            created_at=now,
            updated_at=now,
        )
        async with self.async_session_maker.begin() as session:
            session.add(profile)
            await session.flush()
            # Auto-create the profile's "main" channel in the same transaction
            # so every conversation can resolve a channel_id without a special
            # case for "freshly created profile, no channel yet".
            await self._ensure_main_channel(session, name)
        return self._profile_to_dict(profile)

    async def get_profile(self, name: str) -> dict | None:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ProfileModel).where(ProfileModel.name == name)
            )
            profile = result.scalar_one_or_none()
            return self._profile_to_dict(profile) if profile else None

    async def list_profiles(self) -> list[dict]:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ProfileModel).order_by(ProfileModel.name.asc())
            )
            profiles = result.scalars().all()
            return [self._profile_to_dict(p) for p in profiles]

    async def delete_profile(self, name: str) -> bool:
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            # Cascade will handle conversations and messages
            result = await session.execute(
                delete(ProfileModel).where(ProfileModel.name == name)
            )
            deleted = result.rowcount > 0
            if deleted:
                logger.info(f"[storage] deleted profile name={name}")
            return deleted

    async def profile_exists(self, name: str) -> bool:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(func.count()).select_from(ProfileModel).where(ProfileModel.name == name)
            )
            return result.scalar() > 0

    # ── Conversation CRUD ──

    async def create_conversation(
        self, profile: str, context_id: str | None = None,
        task_id: str | None = None, title: str = "Untitled Chat",
        channel_id: str | None = None, kind: str = "chat",
    ) -> dict:
        """Create a conversation. If ``channel_id`` is omitted, the profile's
        ``main`` channel is used (auto-created if missing).

        ``kind`` is ``chat`` (normal thread, shown in the sidebar) or
        ``event_run`` (a hidden per-trigger conversation backing one event run,
        excluded from :meth:`list_conversations`).
        """
        await self._ensure_initialized()
        now = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            resolved_channel_id = channel_id or await self._ensure_main_channel(session, profile)
            conv = ConversationModel(
                id=str(uuid.uuid4()),
                profile=profile,
                channel_id=resolved_channel_id,
                context_id=context_id,
                task_id=task_id,
                title=title,
                kind=kind,
                created_at=now,
                updated_at=now,
            )
            session.add(conv)
        return self._conv_to_dict(conv)

    async def get_conversation(self, conversation_id: str) -> dict | None:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ConversationModel).where(ConversationModel.id == conversation_id)
            )
            conv = result.scalar_one_or_none()
            return self._conv_to_dict(conv) if conv else None

    async def get_conversation_by_context(self, profile: str, context_id: str) -> dict | None:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ConversationModel).where(
                    ConversationModel.profile == profile,
                    ConversationModel.context_id == context_id,
                )
            )
            conv = result.scalar_one_or_none()
            return self._conv_to_dict(conv) if conv else None

    async def list_conversations(
        self, profile: str, limit: int = 50, offset: int = 0,
        channel_id: str | None = None, channel_type: str | None = None,
    ) -> list[dict]:
        """List conversations for a profile, optionally filtered by channel.

        If both ``channel_id`` and ``channel_type`` are provided, ``channel_id``
        wins. ``channel_type`` is resolved against the profile's channels;
        an unknown type returns an empty list.
        """
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            resolved_channel_id = channel_id
            if resolved_channel_id is None and channel_type is not None:
                resolved_channel_id = (await session.execute(
                    select(ChannelModel.id).where(
                        ChannelModel.profile == profile,
                        ChannelModel.channel_type == channel_type,
                    )
                )).scalar_one_or_none()
                if resolved_channel_id is None:
                    return []

            msg_count_subq = (
                select(
                    MessageModel.conversation_id,
                    func.count(MessageModel.id).label("message_count"),
                )
                .group_by(MessageModel.conversation_id)
                .subquery()
            )
            stmt = (
                select(ConversationModel, msg_count_subq.c.message_count)
                .outerjoin(msg_count_subq, ConversationModel.id == msg_count_subq.c.conversation_id)
                .where(ConversationModel.profile == profile)
                # Hidden per-trigger event-run conversations never appear in the
                # conversation list / sidebar — they are reached via the Events
                # page run history, not the normal thread list.
                .where(ConversationModel.kind == "chat")
            )
            if resolved_channel_id is not None:
                stmt = stmt.where(ConversationModel.channel_id == resolved_channel_id)
            stmt = stmt.order_by(ConversationModel.updated_at.desc()).limit(limit).offset(offset)

            result = await session.execute(stmt)
            rows = result.all()
            return [
                {**self._conv_to_dict(conv), "message_count": count or 0}
                for conv, count in rows
            ]

    async def update_conversation(self, conversation_id: str, **kwargs) -> None:
        await self._ensure_initialized()
        kwargs["updated_at"] = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == conversation_id)
                .values(**kwargs)
            )

    async def conversation_id_exists(self, conversation_id: str) -> bool:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(func.count())
                .select_from(ConversationModel)
                .where(ConversationModel.id == conversation_id)
            )
            return (result.scalar() or 0) > 0

    async def rename_conversation_id(
        self, old_id: str, new_id: str, *, new_title: str | None = None,
    ) -> dict | None:
        """Rename a conversation's primary id, cascading to FK children.

        Atomic across **every** table with a foreign key to
        ``conversations.id`` — discovered from the ORM metadata rather than a
        hand-maintained list, so a table that later adds such an FK is repointed
        automatically. Relies on SQLite's ``PRAGMA defer_foreign_keys=ON`` (FKs
        are re-checked at COMMIT, so the parent row can be updated before all
        children).

        Returns the refreshed conversation dict on success, or ``None`` if
        ``old_id`` does not exist or ``new_id`` is already in use.

        ``new_title`` overrides the default behavior of resetting the title
        to ``new_id``. Pass an explicit string to keep a custom title; pass
        ``None`` to let the title follow the new id.
        """
        await self._ensure_initialized()

        if not is_valid_conversation_id(new_id):
            return None
        if old_id == new_id:
            return await self.get_conversation(old_id)

        async with self.async_session_maker.begin() as session:
            # Pre-flight existence checks share the transaction, so a
            # concurrent rename can't slip in between check and update.
            old_row = (await session.execute(
                select(ConversationModel).where(ConversationModel.id == old_id)
            )).scalar_one_or_none()
            if old_row is None:
                return None
            collision = (await session.execute(
                select(func.count())
                .select_from(ConversationModel)
                .where(ConversationModel.id == new_id)
            )).scalar() or 0
            if collision > 0:
                return None

            # Defer FK checks until COMMIT so we can update the parent row
            # while children still point at the old id. Pragma resets at
            # COMMIT — scoped to this connection/transaction.
            await session.execute(text("PRAGMA defer_foreign_keys=ON"))

            # Repoint every child column that FK-references conversations.id,
            # discovered from the schema so new FK tables are covered without a
            # code change here. Order is irrelevant under defer_foreign_keys;
            # sorted_tables is used only for determinism.
            conv_table = ConversationModel.__table__
            for table in conv_table.metadata.sorted_tables:
                if table is conv_table:
                    continue
                for column in table.columns:
                    if any(fk.column is conv_table.c.id for fk in column.foreign_keys):
                        await session.execute(
                            table.update()
                            .where(column == old_id)
                            .values({column.key: new_id})
                        )

            now = time.time() * 1000
            values: dict = {
                "id": new_id,
                "title": new_title if new_title is not None else new_id,
                "updated_at": now,
            }
            # context_id is back-filled to the conversation id at first stream
            # tick (see app/agent/stream_runner.py). Rewrite if it currently
            # equals the old id so tool-storage scoping follows the rename.
            if old_row.context_id == old_id:
                values["context_id"] = new_id

            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == old_id)
                .values(**values)
            )

        logger.info(f"[storage] renamed conversation old_id={old_id} new_id={new_id}")
        return await self.get_conversation(new_id)

    async def delete_conversation(self, conversation_id: str) -> bool:
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            # Delete messages first (cascade should handle this, but be explicit)
            await session.execute(
                delete(MessageModel).where(MessageModel.conversation_id == conversation_id)
            )
            result = await session.execute(
                delete(ConversationModel).where(ConversationModel.id == conversation_id)
            )
            return result.rowcount > 0

    async def delete_all_conversations(self, profile: str) -> int:
        """Delete all *chat* conversations for a profile.

        Scoped to ``kind='chat'`` so it never sweeps hidden event-run
        conversations out from under their run rows — those are torn down via
        the event-run lifecycle when their rule (or registering chat) is
        deleted. The API layer collects and cascades any rules homed on the
        deleted chats before calling this.
        """
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            # Get chat conversation IDs for this profile
            result = await session.execute(
                select(ConversationModel.id).where(
                    ConversationModel.profile == profile,
                    ConversationModel.kind == "chat",
                )
            )
            conv_ids = [row[0] for row in result.all()]
            if not conv_ids:
                return 0
            # Delete messages
            await session.execute(
                delete(MessageModel).where(MessageModel.conversation_id.in_(conv_ids))
            )
            # Delete conversations
            result = await session.execute(
                delete(ConversationModel).where(ConversationModel.id.in_(conv_ids))
            )
            return result.rowcount

    async def get_or_create_conversation(
        self, profile: str, context_id: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        if context_id:
            conv = await self.get_conversation_by_context(profile, context_id)
            if conv:
                return conv
        return await self.create_conversation(
            profile=profile, context_id=context_id, task_id=task_id,
        )

    # ── Channel CRUD ──

    async def list_channels(self, profile: str) -> list[dict]:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ChannelModel)
                .where(ChannelModel.profile == profile)
                .order_by(ChannelModel.created_at.asc())
            )
            return [self._channel_to_dict(c) for c in result.scalars().all()]

    async def get_channel(self, channel_id: str) -> dict | None:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ChannelModel).where(ChannelModel.id == channel_id)
            )
            ch = result.scalar_one_or_none()
            return self._channel_to_dict(ch) if ch else None

    async def get_channel_by_type(self, profile: str, channel_type: str) -> dict | None:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ChannelModel).where(
                    ChannelModel.profile == profile,
                    ChannelModel.channel_type == channel_type,
                )
            )
            ch = result.scalar_one_or_none()
            return self._channel_to_dict(ch) if ch else None

    async def create_channel(
        self, profile: str, channel_type: str, *,
        mode: str = "bot", auth_mode: str = "none",
        response_mode: str = "normal", enabled: bool = True,
        config: dict | None = None,
    ) -> dict:
        """Create a channel row. Raises if (profile, channel_type) already exists."""
        await self._ensure_initialized()
        now = time.time() * 1000
        ch = ChannelModel(
            id=str(uuid.uuid4()),
            profile=profile,
            channel_type=channel_type,
            mode=mode,
            auth_mode=auth_mode,
            response_mode=response_mode,
            enabled=enabled,
            config=config,
            state=None,
            created_at=now,
            updated_at=now,
        )
        async with self.async_session_maker.begin() as session:
            session.add(ch)
        return self._channel_to_dict(ch)

    async def update_channel(self, channel_id: str, **fields) -> dict | None:
        await self._ensure_initialized()
        if not fields:
            return await self.get_channel(channel_id)
        fields["updated_at"] = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(ChannelModel)
                .where(ChannelModel.id == channel_id)
                .values(**fields)
            )
        return await self.get_channel(channel_id)

    async def delete_channel(self, channel_id: str) -> bool:
        """Delete a channel. ``main`` is rejected upstream (in the API layer)."""
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            result = await session.execute(
                delete(ChannelModel).where(ChannelModel.id == channel_id)
            )
            return result.rowcount > 0

    async def list_enabled_external_channels(self) -> list[dict]:
        """Channels to start at server boot — enabled and not ``main``."""
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ChannelModel).where(
                    ChannelModel.enabled.is_(True),
                    ChannelModel.channel_type != "main",
                )
            )
            return [self._channel_to_dict(c) for c in result.scalars().all()]

    # ── Channel sender CRUD ──

    async def get_or_create_sender(
        self, channel_id: str, sender_id: str, display_name: str | None = None,
    ) -> dict:
        """Return the sender row (creating it if absent). Refreshes display_name."""
        await self._ensure_initialized()
        now = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(ChannelSenderModel).where(
                    ChannelSenderModel.channel_id == channel_id,
                    ChannelSenderModel.sender_id == sender_id,
                )
            )).scalar_one_or_none()
            if row is None:
                row = ChannelSenderModel(
                    id=str(uuid.uuid4()),
                    channel_id=channel_id,
                    sender_id=sender_id,
                    display_name=display_name,
                    authenticated=False,
                    pending_otp=None,
                    pending_otp_expires_at=None,
                    conversation_id=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                await session.flush()
            elif display_name and row.display_name != display_name:
                row.display_name = display_name
                row.updated_at = now
            return self._sender_to_dict(row)

    async def list_senders(self, channel_id: str) -> list[dict]:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ChannelSenderModel)
                .where(ChannelSenderModel.channel_id == channel_id)
                .order_by(ChannelSenderModel.created_at.asc())
            )
            return [self._sender_to_dict(s) for s in result.scalars().all()]

    async def update_sender(self, sender_row_id: str, **fields) -> dict | None:
        await self._ensure_initialized()
        if not fields:
            return None
        fields["updated_at"] = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(ChannelSenderModel)
                .where(ChannelSenderModel.id == sender_row_id)
                .values(**fields)
            )
            row = (await session.execute(
                select(ChannelSenderModel).where(ChannelSenderModel.id == sender_row_id)
            )).scalar_one_or_none()
            return self._sender_to_dict(row) if row else None

    # ── Message CRUD ──

    async def add_message(
        self, conversation_id: str, role: str, content: str | None = None,
        parts: list | None = None, thinking_steps: list | None = None,
        token_usage: dict | None = None, metadata: dict | None = None,
        summary: str | None = None, llm_messages: list | None = None,
    ) -> dict:
        await self._ensure_initialized()
        now = time.time() * 1000

        async with self.async_session_maker.begin() as session:
            # Get next ordering number
            result = await session.execute(
                select(func.coalesce(func.max(MessageModel.ordering), -1))
                .where(MessageModel.conversation_id == conversation_id)
            )
            next_ordering = result.scalar() + 1

            # Serialize parts if they are pydantic models
            serialized_parts = None
            if parts is not None:
                serialized_parts = []
                for part in parts:
                    if hasattr(part, "model_dump"):
                        serialized_parts.append(part.model_dump(mode="json"))
                    elif hasattr(part, "root") and hasattr(part.root, "model_dump"):
                        serialized_parts.append(part.root.model_dump(mode="json"))
                    elif isinstance(part, dict):
                        serialized_parts.append(part)
                    else:
                        serialized_parts.append({"kind": "text", "text": str(part)})

            msg = MessageModel(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                role=role,
                content=content,
                parts=serialized_parts,
                thinking_steps=thinking_steps,
                llm_messages=llm_messages,
                token_usage=token_usage,
                message_metadata=metadata,
                summary=summary,
                created_at=now,
                ordering=next_ordering,
            )
            session.add(msg)

        return self._msg_to_dict(msg)

    async def get_messages(self, conversation_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(MessageModel)
                .where(MessageModel.conversation_id == conversation_id)
                .order_by(MessageModel.ordering.asc())
                .limit(limit)
                .offset(offset)
            )
            messages = result.scalars().all()
            return [self._msg_to_dict(msg) for msg in messages]

    async def get_messages_after(
        self, conversation_id: str, after_ordering: int, limit: int = 5000,
        newest_first: bool = False,
    ) -> list[dict]:
        """Full message dicts with ``ordering > after_ordering``, oldest → newest.

        Unlike :meth:`get_messages` (which caps at the *oldest* 100), this returns
        the un-compacted tail regardless of total length, so compaction sees every
        message past the watermark. Pass ``-1`` to get the whole conversation.

        With ``newest_first=True`` the ``limit`` is applied at the **frontier** —
        the newest ``limit`` messages above ``after_ordering``, still returned in
        chronological order — so a very long un-compacted tail keeps its recent
        end rather than the oldest ``limit`` rows. (For a tail shorter than
        ``limit`` the two modes return the identical set in the identical order.)
        """
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            order = (
                MessageModel.ordering.desc() if newest_first
                else MessageModel.ordering.asc()
            )
            result = await session.execute(
                select(MessageModel)
                .where(
                    MessageModel.conversation_id == conversation_id,
                    MessageModel.ordering > after_ordering,
                )
                .order_by(order)
                .limit(limit)
            )
            rows = result.scalars().all()
            if newest_first:
                rows = list(reversed(rows))
            return [self._msg_to_dict(msg) for msg in rows]

    async def get_max_ordering(self, conversation_id: str) -> int:
        """Highest ``ordering`` in the conversation, or ``-1`` when empty.

        The true frontier. Compaction must pin the watermark to this (captured
        when the fold input was assembled), never to ``max()`` over a possibly
        row-capped read, so an interleaved message cannot be buried below the
        watermark and silently dropped from the tail.
        """
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(func.coalesce(func.max(MessageModel.ordering), -1))
                .where(MessageModel.conversation_id == conversation_id)
            )
            return int(result.scalar())

    async def get_latest_agent_message(self, conversation_id: str) -> dict | None:
        """Most recent ``agent`` message dict (highest ``ordering``), or ``None``.

        Used by compaction to read the turn's model-reported context size
        (``token_usage.context_tokens``) and stamped ``provider``/``model``.
        """
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(MessageModel)
                .where(
                    MessageModel.conversation_id == conversation_id,
                    MessageModel.role == "agent",
                )
                .order_by(MessageModel.ordering.desc())
                .limit(1)
            )).scalars().first()
            return self._msg_to_dict(row) if row is not None else None

    # ── Compaction state ──

    async def get_compaction_state(
        self, conversation_id: str,
    ) -> tuple[str | None, int, float | None]:
        """Return ``(compaction_summary, compaction_watermark, compaction_last_compacted_at)``.

        Returns ``(None, -1, None)`` when the row is missing or nothing has been
        compacted yet (``-1`` so a ``> watermark`` tail query includes message 0).
        """
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            row = (await session.execute(
                select(
                    ConversationModel.compaction_summary,
                    ConversationModel.compaction_watermark,
                    ConversationModel.compaction_last_compacted_at,
                ).where(ConversationModel.id == conversation_id)
            )).first()
            if row is None:
                return None, -1, None
            return row[0], int(row[1]) if row[1] is not None else -1, row[2]

    async def set_compaction_state(
        self, conversation_id: str, summary: str | None, watermark: int,
        ts: float | None = None,
    ) -> None:
        """Persist the running summary + watermark. Deliberately does NOT touch
        ``updated_at`` — compaction is background bookkeeping and must not reorder
        the conversation list."""
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == conversation_id)
                .values(
                    compaction_summary=summary,
                    compaction_watermark=int(watermark),
                    compaction_last_compacted_at=(
                        ts if ts is not None else time.time() * 1000
                    ),
                )
            )

    # ── Helpers ──

    @staticmethod
    def _profile_to_dict(profile: ProfileModel) -> dict:
        return {
            "id": profile.id,
            "name": profile.name,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
            "skill_mode": getattr(profile, "skill_mode", "manual") or "manual",
        }

    @staticmethod
    def _conv_to_dict(conv: ConversationModel) -> dict:
        return {
            "id": conv.id,
            "profile": conv.profile,
            "channel_id": conv.channel_id,
            "context_id": conv.context_id,
            "task_id": conv.task_id,
            "title": conv.title,
            "kind": getattr(conv, "kind", "chat"),
            "working_directory": getattr(conv, "working_directory", None),
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
        }

    @staticmethod
    def _channel_to_dict(ch: ChannelModel) -> dict:
        return {
            "id": ch.id,
            "profile": ch.profile,
            "channel_type": ch.channel_type,
            "mode": ch.mode,
            "auth_mode": ch.auth_mode,
            "response_mode": ch.response_mode,
            "enabled": bool(ch.enabled),
            "config": ch.config or {},
            "state": ch.state or {},
            "created_at": ch.created_at,
            "updated_at": ch.updated_at,
        }

    @staticmethod
    def _sender_to_dict(s: ChannelSenderModel) -> dict:
        return {
            "id": s.id,
            "channel_id": s.channel_id,
            "sender_id": s.sender_id,
            "display_name": s.display_name,
            "authenticated": bool(s.authenticated),
            "pending_otp": s.pending_otp,
            "pending_otp_expires_at": s.pending_otp_expires_at,
            "conversation_id": s.conversation_id,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }

    @staticmethod
    def _msg_to_dict(msg: MessageModel) -> dict:
        return {
            "id": msg.id,
            "conversation_id": msg.conversation_id,
            "role": msg.role,
            "content": msg.content,
            "parts": msg.parts,
            "thinking_steps": msg.thinking_steps,
            "llm_messages": msg.llm_messages,
            "token_usage": msg.token_usage,
            "metadata": msg.message_metadata,
            "summary": msg.summary,
            "created_at": msg.created_at,
            "ordering": msg.ordering,
        }
