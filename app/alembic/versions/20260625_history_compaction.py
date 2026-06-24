"""History compaction — running summary + watermark on conversations.

Revision ID: 20260625_history_compaction
Revises: 20260624_usage_records
Create Date: 2026-06-25

Adds three additive columns to ``conversations`` for the summarization-based
conversation-history compaction feature, which replaces the old token-window
truncation:

- ``compaction_watermark`` — ``MessageModel.ordering`` of the newest message
  already folded into the running summary. Defaults to ``-1`` (nothing folded;
  message ordering starts at 0, so the sentinel must be < 0).
- ``compaction_summary`` — running summary of every message ``<= watermark``.
- ``compaction_last_compacted_at`` — last-compaction timestamp (diagnostic).

Purely additive and inspector-guarded, same defensive shape as
``20260618_memory``: on a fresh install the baseline rebuilds from the live ORM
metadata (which now includes these columns), so the migration only adds what is
missing on an existing DB. Runs cleanly on SQLite (``render_as_batch``) and
PostgreSQL without touching existing data (old rows backfill to -1 / NULL).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260625_history_compaction"
down_revision: Union[str, Sequence[str], None] = "20260624_usage_records"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    conv_columns = {c["name"] for c in inspector.get_columns("conversations")}
    pending_columns = []
    if "compaction_watermark" not in conv_columns:
        pending_columns.append(
            sa.Column("compaction_watermark", sa.Integer(), nullable=False, server_default="-1")
        )
    if "compaction_summary" not in conv_columns:
        pending_columns.append(sa.Column("compaction_summary", sa.Text(), nullable=True))
    if "compaction_last_compacted_at" not in conv_columns:
        pending_columns.append(
            sa.Column("compaction_last_compacted_at", sa.Float(), nullable=True)
        )
    if pending_columns:
        # ``batch_alter_table`` rebuilds the table on SQLite — only enter it when
        # there is something to add (avoids a needless rebuild on fresh installs
        # where the baseline already created the columns).
        with op.batch_alter_table("conversations") as batch_op:
            for column in pending_columns:
                batch_op.add_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    conv_columns = {c["name"] for c in inspector.get_columns("conversations")}
    drop_columns = [
        c
        for c in (
            "compaction_last_compacted_at",
            "compaction_summary",
            "compaction_watermark",
        )
        if c in conv_columns
    ]
    if drop_columns:
        with op.batch_alter_table("conversations") as batch_op:
            for column in drop_columns:
                batch_op.drop_column(column)
