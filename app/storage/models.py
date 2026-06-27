"""SQLAlchemy ORM models for Cremind's persistence layer.

All tool-related state is keyed by ``tool_id`` (a slugified, globally-unique
identifier). Profiles cascade-delete their conversations, messages, configs,
profile-tool memberships, and per-profile tool configs.

Tables
------
- profiles            : tenant identity
- channels            : per-profile messaging channels; one ``main`` row per
                        profile is auto-created. UNIQUE(profile, channel_type).
                        (FK profile, CASCADE)
- conversations       : chat threads (FK profile CASCADE, FK channel CASCADE)
- messages            : per-conversation messages (FK conversation, CASCADE)
- channel_senders     : external sender state per channel — auth + per-sender
                        conversation pointer (FK channel CASCADE,
                        FK conversation SET NULL)
- auth_tokens         : per-profile OAuth tokens for tools (FK profile, CASCADE)
- tools               : global tool registry (one row per tool_id)
- profile_tools       : M:N join for A2A and MCP tools per profile
                        (FK profile CASCADE, FK tool CASCADE)
- tool_configs        : per-profile per-tool scoped config
                        (FK profile CASCADE, FK tool CASCADE)
- server_config       : global server settings (no FK)
- llm_config          : per-profile LLM settings (FK profile, CASCADE)
- user_config         : per-profile general application settings (FK profile, CASCADE)
"""

import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from a2a.server.models import Base


class ProfileModel(Base):
    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class ChannelModel(Base):
    """Per-profile messaging channel.

    Each profile has exactly one ``main`` channel (auto-created with the
    profile) representing the built-in web/CLI conversations. External
    channels (telegram/whatsapp/discord/messenger/slack) are added by the
    user; ``UNIQUE(profile, channel_type)`` enforces one connection per type
    per profile.

    Secrets (bot tokens, passwords) are NOT stored in ``config`` — they go
    into ``dynamic_config_storage`` keyed by ``("channels", f"{id}.{field}")``
    with ``is_secret=True`` so existing redaction applies.
    """

    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_type: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="bot")
    auth_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    response_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("profile", "channel_type", name="uq_channels_profile_type"),
    )


class ConversationModel(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), nullable=False, index=True
    )
    # Nullable in the SQLAlchemy model so existing rows pass the additive
    # ALTER TABLE migration. New rows always populate this (storage layer
    # resolves the profile's main channel when no channel_id is passed).
    channel_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("channels.id", ondelete="CASCADE"), nullable=True, index=True
    )
    context_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # Wide enough for composite task ids like ``msg:<conv-uuid>:<msg-uuid>``
    # produced by the A2A executor (~77 chars). 36 was the original sizing
    # for bare UUIDs, which Postgres VARCHAR strictly enforces.
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(256), default="Untitled Chat")
    # Per-conversation override of the agent's working directory. Mirrors the
    # value held in ContextStorage under ``_working_directory_override`` and
    # survives server restarts (ContextStorage is in-memory only). Validated
    # against the filesystem on conversation load — stale entries (path was
    # deleted) are cleared and the conversation falls back to the user
    # default. ``NULL`` means "no override, use the user default".
    working_directory: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # History-compaction state. ``compaction_summary``
    # is the running summary of every message with ``ordering <= compaction_watermark``;
    # the verbatim tail (``ordering > compaction_watermark``) is sent to the LLM as-is.
    # Oldest turns are folded into the summary once the tail's tokens cross the
    # configured threshold, then the watermark advances. Defaults to -1 (nothing
    # folded — message ``ordering`` starts at 0, so the sentinel must be < 0 or the
    # first message would be excluded from the tail).
    # ``server_default`` (not just the ORM-side ``default``) so tables created
    # straight from this metadata — fresh-install baseline and tests that raw-INSERT
    # a conversation row — carry the -1 default at the DB level too.
    compaction_watermark: Mapped[int] = mapped_column(
        Integer, nullable=False, default=-1, server_default="-1"
    )
    compaction_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    compaction_last_compacted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class MessageModel(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    parts: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    thinking_steps: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Native LLM reasoning trace (assistant tool_calls + role:"tool" results + the
    # final-answer assistant message) for this turn, in OpenAI chat format. Replayed
    # into conversation history on later turns so the prompt-cache prefix covers the
    # reasoning context. NULL for turns with no tool calls (replays content-only).
    llm_messages: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    message_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, name="metadata")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    ordering: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UsageRecordModel(Base):
    """One LLM invocation (smallest logical unit) within an agent turn.

    A turn (one assistant ``MessageModel``) fans out to N usage_records: one per
    reasoning step plus one per tool/sub-agent child-LLM call. Raw token counts
    are always stored; the ``*_usd`` cost columns are frozen at write time from
    the rate snapshot in effect, so historical estimates never move when catalog
    prices change. Costs are nullable — left null when the model is unknown
    (e.g. backfilled rows, or remote A2A sub-agents that don't report a model).
    Everything the dashboard groups by (conversation, profile, provider, model,
    source, tool, time) is an indexed column on this one fact table.
    """

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # ── scope / grouping keys ──
    conversation_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The assistant turn this rolled up into. SET NULL (not CASCADE) so a turn
    # re-write / message delete doesn't drop usage history; the conversation
    # CASCADE still governs lifecycle. Nullable so a record can be inserted
    # before the turn's message id is known, and so backfilled rows can attach.
    message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Denormalized for cheap filtering / parity with other per-profile tables.
    profile: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    provider: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # Catalog group_hint (high|low|...) so the dashboard can group by tier.
    model_group: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # reasoning | tool | subagent | intrinsic | aggregate
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="reasoning", index=True)
    tool_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # 0-based step ordinal within the turn (drill-down / ordering).
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── raw token counts (always present, never recomputed) ──
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── frozen cost (USD), computed at write time; nullable when rates unknown ──
    uncached_input_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cache_read_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cache_write_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_usd: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    # Rate snapshot the cost was computed from (rates, multipliers, source,
    # pricing_version) — makes each row a self-describing, auditable receipt.
    rate_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[float] = mapped_column(Float, nullable=False)  # epoch ms

    __table_args__ = (
        Index("ix_usage_records_conv_msg", "conversation_id", "message_id"),
        Index("ix_usage_records_profile_created", "profile", "created_at"),
    )


class ChannelSenderModel(Base):
    """External sender state for a channel — auth flag + per-sender conversation.

    ``conversation_id`` uses ``ON DELETE SET NULL`` (instead of CASCADE) so
    deleting a single conversation doesn't drop the sender's auth/OTP state.
    Channel deletion still cascades both rows away via ``channel_id``.
    """

    __tablename__ = "channel_senders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_id: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    authenticated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pending_otp: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pending_otp_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("channel_id", "sender_id", name="uq_channel_senders"),
    )


class AuthTokenModel(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # ``agent_name`` is kept as the lookup key (instead of FK-ing to tools)
    # because OAuth tokens are sometimes saved before the corresponding tool
    # row has been created (e.g., during MCP server registration). Profile
    # cascade still cleans them up on profile deletion.
    agent_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False, default="a2a")
    token_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="access_token")
    token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class ToolModel(Base):
    """Global tool registry. Intrinsic tools are NOT persisted here (always in-memory)."""
    __tablename__ = "tools"

    tool_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    arguments_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    owner_profile: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("tool_type", "source", name="uq_tools_type_source"),
    )


class ProfileToolModel(Base):
    """M:N visibility/enabled state. Populated only for a2a / mcp tool types."""
    __tablename__ = "profile_tools"

    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), primary_key=True
    )
    # ON UPDATE CASCADE so a tool can be atomically renamed (used to displace a
    # dynamic tool when a fixed-name intrinsic/built-in tool claims its slug).
    tool_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tools.tool_id", ondelete="CASCADE", onupdate="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    added_at: Mapped[float] = mapped_column(Float, nullable=False)


class ToolConfigModel(Base):
    """Per-profile per-tool configuration scoped by purpose.

    scope ∈ {'arg', 'variable', 'llm', 'meta'}.
    """
    __tablename__ = "tool_configs"

    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), primary_key=True
    )
    tool_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tools.tool_id", ondelete="CASCADE", onupdate="CASCADE"),
        primary_key=True,
    )
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class ServerConfigModel(Base):
    """Global server-wide dynamic configuration."""
    __tablename__ = "server_config"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class LLMConfigModel(Base):
    """Per-profile LLM provider credentials and model group assignments."""
    __tablename__ = "llm_config"

    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class UserConfigModel(Base):
    """Per-profile general application configuration (Settings → Config page).

    Distinct from ``llm_config`` (provider credentials) and ``server_config``
    (global). Drives runtime tunables such as agent ``max_steps``, history
    token windows, and per-LLM-call retry counts.
    """
    __tablename__ = "user_config"

    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class AutostartProcessModel(Base):
    """Long-running processes registered to (re)start with the Cremind server."""
    __tablename__ = "autostart_processes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    working_dir: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_pty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempted_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class SkillEventSubscriptionModel(Base):
    """Conversation-scoped subscription to a skill's filesystem event.

    A row says: when ``<skill_dir>/events/<event_type>/<id>.md`` appears for
    the skill named ``skill_name``, run ``action`` (a natural-language
    instruction) in ``conversation_id`` with the file content appended.

    Multiple rows for the same (conversation_id, skill_name, event_type) are
    allowed — when the event fires they execute sequentially in created_at
    order via the per-conversation queue worker.
    """
    __tablename__ = "skill_event_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    skill_name: Mapped[str] = mapped_column(String(256), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class FileWatcherSubscriptionModel(Base):
    """Conversation-scoped subscription to a filesystem watch.

    A row says: while the Cremind server is running, watch ``root_path`` (with
    optional recursion) for filesystem events of types listed in
    ``event_types``; when one fires, build a synthetic trigger payload from
    the watchdog event and run ``action`` in ``conversation_id``.

    ``event_types`` and ``extensions`` use comma-separated strings to match
    the existing storage convention (skill_event_subscriptions, channels) —
    bounded enums/extensions, no JSON1 dependency, easy LIKE queries.

    ``target_kind`` ∈ {"file", "folder", "any"} narrows which watchdog
    events the handler dispatches.
    """
    __tablename__ = "file_watcher_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="any")
    event_types: Mapped[str] = mapped_column(String(128), nullable=False)
    extensions: Mapped[str | None] = mapped_column(String(256), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class ScheduleEventSubscriptionModel(Base):
    """Conversation-scoped, time-based event subscription (the Calendar &
    Schedule engine).

    A row is ONE schedule rule, not one occurrence. It says: at ``next_fire_at``
    (and, for a recurrence, at every following occurrence the ``rrule`` yields),
    fire ``action`` in ``conversation_id`` — or, for a reminder-only row, just
    raise a notification. After each fire the ``ScheduleManager`` advances
    ``next_fire_at`` to the following occurrence (a rolling pointer), so an
    open-ended recurrence stays a single row forever; bounded series (COUNT /
    UNTIL) stop and flip ``status`` to ``completed``.

    The calendar UI expands occurrences on demand for the visible window only;
    they are never persisted here.
    """
    __tablename__ = "schedule_event_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # Natural-language command run in the conversation when the event fires.
    # Defaults to the title at creation time, so a bare command still executes.
    action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # All-day event (no time-of-day). Multi-day spans are carried by dtstart +
    # duration_minutes (days × 1440); all_day just changes display + Google body.
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # One of: instant | interval | recurrence | explicit_set (from the scheduler
    # parser's schedule_kind). explicit_set is stored as one row per occurrence.
    schedule_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="instant")
    # First/next occurrence as naive local wall-clock ISO (YYYY-MM-DDTHH:MM:SS).
    dtstart: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    # RFC 5545 RRULE value (no "RRULE:" prefix). NULL for a one-shot event.
    rrule: Mapped[str | None] = mapped_column(Text, nullable=True)
    recurrence_end_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # never|count|until
    recurrence_end_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Rolling pointer: epoch seconds of the next fire. NULL once completed/cancelled.
    next_fire_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    occurrences_fired: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active|completed|cancelled|paused
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="agent")  # agent|manual
    # Set when mirrored to an external provider (e.g. "google"); reserved for Phase 2.
    external_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class LongTermMemoryModel(Base):
    """Per-profile long-term memory entry (FIFO queue).

    Each row is one durable, session-independent user fact (name, age, stable
    preferences). Bounded by ``memory.long_term_queue_size`` (default 20); the
    storage layer FIFO-evicts the oldest rows on overflow. Optional per
    extraction — many extractions add nothing here.
    """
    __tablename__ = "long_term_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The conversation this fact was learned from (informational; not an FK so a
    # conversation delete doesn't drop durable profile facts).
    source_conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ordering: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
