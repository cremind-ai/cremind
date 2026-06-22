"""Memory feature — short/long-term memory tables + conversation watermark.

Revision ID: 20260618_memory
Revises: 20260509_baseline
Create Date: 2026-06-18

Adds the persistence for the conversation-memory feature:

- ``short_term_memories`` — per-conversation FIFO queue of distilled session
  summaries (mistakes, repeated commands, user habits).
- ``long_term_memories`` — per-profile FIFO queue of durable user facts.
- two additive columns on ``conversations`` (``memory_watermark``,
  ``memory_last_extracted_at``) tracking which messages have been folded into
  memory so extraction never re-processes the same window.

Purely additive: the two ``create_table`` calls and the batch ``add_column``
run cleanly on both SQLite (via ``render_as_batch`` table rebuild, configured
in ``app/alembic/env.py``) and PostgreSQL, and upgrade existing databases
without touching their data (the watermark defaults to 0 for old rows).

Idempotency note: the baseline migration rebuilds the schema from the *live*
ORM metadata, which now includes the two new ``conversations`` columns. So on
a **fresh** install the baseline already creates those columns, while on an
**existing** (pre-memory) install it does not. To work in both cases this
migration inspects the live schema and only creates what is missing — the same
defensive shape as the runtime compat-preflight in ``app/storage/migrations.py``.
The new tables are *not* in the baseline's hardcoded table list, so the baseline
never creates them; the inspector guard is belt-and-suspenders for reruns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260618_memory"
down_revision: Union[str, Sequence[str], None] = "20260509_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "short_term_memories" not in existing_tables:
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

    if "long_term_memories" not in existing_tables:
        op.create_table(
            "long_term_memories",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source_conversation_id", sa.String(length=128), nullable=True),
            sa.Column("ordering", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["profile"], ["profiles.name"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_long_term_memories_profile", "long_term_memories", ["profile"]
        )

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
        # ``batch_alter_table`` rebuilds the table on SQLite — only enter it when
        # there is actually something to add (avoids a needless rebuild on fresh
        # installs where the baseline already created both columns).
        with op.batch_alter_table("conversations") as batch_op:
            for column in pending_columns:
                batch_op.add_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    conv_columns = {c["name"] for c in inspector.get_columns("conversations")}
    drop_columns = [
        c for c in ("memory_last_extracted_at", "memory_watermark") if c in conv_columns
    ]
    if drop_columns:
        with op.batch_alter_table("conversations") as batch_op:
            for column in drop_columns:
                batch_op.drop_column(column)

    existing_tables = set(inspector.get_table_names())
    if "long_term_memories" in existing_tables:
        op.drop_table("long_term_memories")
    if "short_term_memories" in existing_tables:
        op.drop_table("short_term_memories")
