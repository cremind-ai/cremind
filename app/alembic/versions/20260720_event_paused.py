"""Per-subscription pause flag for skill-events and file-watchers.

Revision ID: 20260720_event_paused
Revises: 20260711c_drop_builtin_llm_cfg
Create Date: 2026-07-20

Adds a single additive ``paused`` boolean column to
``skill_event_subscriptions`` and ``file_watcher_subscriptions`` so a
multi-fire event can be paused (retained but skipped at dispatch) from the
Events page — mirroring the schedule engine's ``status='paused'``. Schedules
already have their own ``status`` column and are untouched here.

Inspector-guarded + batch (SQLite rebuild via ``render_as_batch``) so it runs
cleanly on SQLite and PostgreSQL and no-ops if the column already exists.
``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped — an additive
nullable-with-default column is backward-compatible.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260720_event_paused"
down_revision: Union[str, Sequence[str], None] = "20260711c_drop_builtin_llm_cfg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("skill_event_subscriptions", "file_watcher_subscriptions")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table in _TABLES:
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "paused" not in cols:
            with op.batch_alter_table(table) as batch_op:
                batch_op.add_column(
                    sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false())
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table in _TABLES:
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "paused" in cols:
            with op.batch_alter_table(table) as batch_op:
                batch_op.drop_column("paused")
