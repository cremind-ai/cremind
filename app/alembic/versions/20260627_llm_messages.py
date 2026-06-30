"""Reasoning-trace replay — llm_messages column on messages.

Revision ID: 20260627_llm_messages
Revises: 20260626_unify_memory_compaction
Create Date: 2026-06-27

Adds one additive column to ``messages`` for the reasoning-trace replay feature:

- ``llm_messages`` — the turn's native LLM reasoning trace (assistant ``tool_calls``
  + ``role:"tool"`` results + the final-answer assistant message) in OpenAI chat
  format. Replayed into conversation history on later turns so the prompt-cache
  prefix covers the reasoning context. NULL for turns with no tool calls (those
  replay content-only, exactly as before).

Purely additive and inspector-guarded, same defensive shape as
``20260625_history_compaction``: on a fresh install the baseline rebuilds from the
live ORM metadata (which now includes this column), so the migration only adds what
is missing on an existing DB. Runs cleanly on SQLite (``render_as_batch``) and
PostgreSQL without touching existing data (old rows backfill to NULL).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260627_llm_messages"
down_revision: Union[str, Sequence[str], None] = "20260626_unify_memory_compaction"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    msg_columns = {c["name"] for c in inspector.get_columns("messages")}
    if "llm_messages" not in msg_columns:
        # ``batch_alter_table`` rebuilds the table on SQLite — only enter it when
        # there is something to add (avoids a needless rebuild on fresh installs
        # where the baseline already created the column).
        with op.batch_alter_table("messages") as batch_op:
            batch_op.add_column(sa.Column("llm_messages", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    msg_columns = {c["name"] for c in inspector.get_columns("messages")}
    if "llm_messages" in msg_columns:
        with op.batch_alter_table("messages") as batch_op:
            batch_op.drop_column("llm_messages")
