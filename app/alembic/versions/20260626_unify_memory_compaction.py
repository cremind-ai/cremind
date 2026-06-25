"""Unify memory with compaction — drop short-term table + memory watermark.

Revision ID: 20260626_unify_memory_compaction
Revises: 20260625_history_compaction
Create Date: 2026-06-26

Short-term memory was unified into the conversation's running compaction summary,
and memory extraction now rides on the compaction fold (single watermark). So this
revision removes the now-dead persistence:

- drops the ``short_term_memories`` table (its rows were ephemeral, per-conversation
  session notes; the running summary now serves that role) and its indexes,
- drops ``conversations.memory_watermark`` and ``conversations.memory_last_extracted_at``
  (compaction's own ``compaction_watermark`` is now the single fold watermark).

``long_term_memories`` is kept (the DB path when vector embedding is off).

Inspector-guarded + ``render_as_batch`` (configured in ``app/alembic/env.py``), same
defensive shape as ``20260618_memory`` / ``20260625_history_compaction``: on a fresh
install the baseline rebuilds from the live ORM metadata (which no longer has the
short-term model or the memory_* columns), so there is nothing to drop and the
guards make this a no-op. ``downgrade`` recreates the table + columns so a rollback
is clean.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260626_unify_memory_compaction"
down_revision: Union[str, Sequence[str], None] = "20260625_history_compaction"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop the short-term table (its indexes go with it).
    if "short_term_memories" in set(inspector.get_table_names()):
        op.drop_table("short_term_memories")

    conv_columns = {c["name"] for c in inspector.get_columns("conversations")}
    drop_columns = [
        c for c in ("memory_last_extracted_at", "memory_watermark") if c in conv_columns
    ]
    if drop_columns:
        with op.batch_alter_table("conversations") as batch_op:
            for column in drop_columns:
                batch_op.drop_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    conv_columns = {c["name"] for c in inspector.get_columns("conversations")}
    pending_columns = []
    if "memory_watermark" not in conv_columns:
        pending_columns.append(
            sa.Column("memory_watermark", sa.Integer(), nullable=False, server_default="0")
        )
    if "memory_last_extracted_at" not in conv_columns:
        pending_columns.append(
            sa.Column("memory_last_extracted_at", sa.Float(), nullable=True)
        )
    if pending_columns:
        with op.batch_alter_table("conversations") as batch_op:
            for column in pending_columns:
                batch_op.add_column(column)

    if "short_term_memories" not in set(inspector.get_table_names()):
        op.create_table(
            "short_term_memories",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("conversation_id", sa.String(length=128), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ordering", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_short_term_memories_conversation_id",
            "short_term_memories",
            ["conversation_id"],
        )
        op.create_index(
            "ix_short_term_memories_profile", "short_term_memories", ["profile"]
        )
