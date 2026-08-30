"""Channel group chats — an agent taking part in a real platform group.

Revision ID: 20260829_channel_groups
Revises: 20260827_group_chats
Create Date: 2026-08-29

Two new tables, and one dropped.

``channel_groups`` is a platform group (a Telegram supergroup, a Slack channel, a
WhatsApp group) that ONE channel's account has been added to. Everything hangs
off ``channel_id``, which is what keeps profiles apart: two profiles whose bots
sit in the same Telegram group get a row each and approve independently.
``UNIQUE(channel_id, platform_chat_id)`` is what makes "have we seen this chat?"
one indexed lookup on the inbound path, and it is also the race arbiter — two
tasks discovering a group at once both insert, one loses, and the loser re-reads
the winner's row instead of creating a second pending request.

``conversation_id`` is SET NULL rather than CASCADE: deleting the transcript must
not silently un-approve the group and start asking again.

``channel_group_members`` is who is in the group, from the platform's roster
where it can be read and from having posted where it cannot.
``UNIQUE(group_id, member_id)`` makes the upsert on every inbound message safe.

**The drop.** ``group_chat_channel_bindings`` belonged to the abandoned design
where a Cremind room was mirrored into a platform chat. Conversation group chats
and channel group chats are now independent features, and nothing reads that
table any more. It shipped in no release — ``20260827_group_chats`` was edited in
place to stop creating it — so this drop only ever fires on a development
database that ran the earlier version, and ``downgrade`` deliberately does not
bring it back.

Purely additive otherwise: no existing table is altered, so no batch operations
are needed and the same DDL runs on SQLite and PostgreSQL alike.
``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260829_channel_groups"
down_revision: Union[str, Sequence[str], None] = "20260827_group_chats"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, columns). Created after the tables, guarded, so a partial
# earlier run self-heals rather than failing on a duplicate index.
_INDEXES = (
    ("ix_channel_groups_channel_id", "channel_groups", ["channel_id"]),
    ("ix_channel_groups_profile", "channel_groups", ["profile"]),
    ("ix_channel_groups_conversation_id", "channel_groups", ["conversation_id"]),
    (
        "ix_channel_group_members_group_id",
        "channel_group_members",
        ["group_id"],
    ),
)

# Reverse creation order — children first, so the FKs are gone before the parent.
_TABLES_IN_DROP_ORDER = ("channel_group_members", "channel_groups")

# The abandoned mirroring table. Dropping it takes its index with it on both
# backends, so it needs no separate drop_index.
_OBSOLETE_TABLE = "group_chat_channel_bindings"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "channel_groups" not in tables:
        op.create_table(
            "channel_groups",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("channel_id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("platform_chat_id", sa.String(length=128), nullable=False),
            sa.Column("chat_type", sa.String(length=32), nullable=True),
            sa.Column("title", sa.String(length=256), nullable=True),
            sa.Column(
                "status", sa.String(length=16), nullable=False,
                server_default="pending",
            ),
            sa.Column(
                "discovered_via", sa.String(length=16), nullable=False,
                server_default="message",
            ),
            sa.Column("conversation_id", sa.String(length=128), nullable=True),
            sa.Column("settings", sa.JSON(), nullable=True),
            sa.Column("roster_refreshed_at", sa.Float(), nullable=True),
            sa.Column("last_message_at", sa.Float(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["channel_id"], ["channels.id"], ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["profile"], ["profiles.name"], ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["conversation_id"], ["conversations.id"], ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "channel_id", "platform_chat_id", name="uq_channel_groups_chat",
            ),
        )

    if "channel_group_members" not in tables:
        op.create_table(
            "channel_group_members",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("group_id", sa.String(length=36), nullable=False),
            sa.Column("member_id", sa.String(length=256), nullable=False),
            sa.Column("alt_ids", sa.JSON(), nullable=True),
            sa.Column("display_name", sa.String(length=256), nullable=True),
            sa.Column("username", sa.String(length=128), nullable=True),
            sa.Column(
                "is_bot", sa.Boolean(), nullable=False, server_default=sa.false(),
            ),
            sa.Column("role", sa.String(length=16), nullable=True),
            sa.Column("source", sa.String(length=16), nullable=False),
            sa.Column("first_seen_at", sa.Float(), nullable=True),
            sa.Column("last_seen_at", sa.Float(), nullable=True),
            sa.Column(
                "message_count", sa.Integer(), nullable=False, server_default="0",
            ),
            sa.ForeignKeyConstraint(
                ["group_id"], ["channel_groups.id"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "group_id", "member_id", name="uq_channel_group_members",
            ),
        )

    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())
    for name, table, columns in _INDEXES:
        if table not in present:
            continue
        existing = {i["name"] for i in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, columns)

    if _OBSOLETE_TABLE in present:
        op.drop_table(_OBSOLETE_TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for name in _TABLES_IN_DROP_ORDER:
        if name in tables:
            op.drop_table(name)
