"""Group chats — one room, several profiles' agents.

Revision ID: 20260827_group_chats
Revises: 20260814_task_inbox
Create Date: 2026-08-27

Three new tables. They are the first SYSTEM-WIDE rows in the schema that several
profiles read and write: every other table belongs to exactly one profile, but a
group is a shared room, so the ROOM is global and ``group_chat_members`` carries
the per-profile part. Isolation is unchanged where it matters — each member still
answers from its own hidden ``conversations`` row with its own persona, tools and
LLM; only posted message text crosses the boundary.

``group_chats.created_by`` is SET NULL rather than CASCADE: deleting the profile
that set a room up must not delete the room the other members are still in.
Membership and messages CASCADE from the group itself.

One UNIQUE constraint carries real behaviour rather than tidiness:

``group_chat_messages (source_message_id, segment)``
    Re-posting an agent's turn is idempotent, so a crash between writing the
    timeline row and stamping the agent's message cannot double-post when the
    boot sweep retries.

Both columns are nullable and NULLs compare distinct in a UNIQUE index on SQLite
and PostgreSQL alike, so rows with no source turn are unconstrained.

Purely additive: nothing existing is altered, so no batch operations are needed
and the same DDL runs on both backends. ``MIN_SUPPORTED_UPGRADE_FROM`` is not
bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260827_group_chats"
down_revision: Union[str, Sequence[str], None] = "20260814_task_inbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, columns). Created after the tables, guarded, so a partial
# earlier run self-heals rather than failing on a duplicate index.
_INDEXES = (
    ("ix_group_chat_members_profile", "group_chat_members", ["profile"]),
    (
        "ix_group_chat_messages_group_ordering",
        "group_chat_messages",
        ["group_id", "ordering"],
    ),
    (
        "ix_group_chat_messages_source_message_id",
        "group_chat_messages",
        ["source_message_id"],
    ),
)

# Reverse creation order — children first, so the FKs are gone before the parent.
_TABLES_IN_DROP_ORDER = (
    "group_chat_messages",
    "group_chat_members",
    "group_chats",
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "group_chats" not in tables:
        op.create_table(
            "group_chats",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("settings", sa.JSON(), nullable=True),
            sa.Column("created_by", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["created_by"], ["profiles.name"], ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    if "group_chat_members" not in tables:
        op.create_table(
            "group_chat_members",
            sa.Column("group_id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("shadow_conversation_id", sa.String(length=128), nullable=True),
            sa.Column("joined_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["group_id"], ["group_chats.id"], ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["profile"], ["profiles.name"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("group_id", "profile"),
        )

    if "group_chat_messages" not in tables:
        op.create_table(
            "group_chat_messages",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("group_id", sa.String(length=36), nullable=False),
            sa.Column("ordering", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sender_kind", sa.String(length=16), nullable=False),
            sa.Column("sender_profile", sa.String(length=128), nullable=True),
            sa.Column("sender_name", sa.String(length=256), nullable=False),
            sa.Column("sender_identity", sa.JSON(), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("hop", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source_conversation_id", sa.String(length=128), nullable=True),
            sa.Column("source_message_id", sa.String(length=36), nullable=True),
            sa.Column("segment", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("delivered_to", sa.JSON(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["group_id"], ["group_chats.id"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_message_id", "segment",
                name="uq_group_chat_messages_source",
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


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for name in _TABLES_IN_DROP_ORDER:
        if name in tables:
            op.drop_table(name)
