"""Calendar & Schedule — all_day flag on schedule events (multi-day support).

Revision ID: 20260621b_schedule_all_day
Revises: 20260621_schedule_events
Create Date: 2026-06-21

Adds a single additive ``all_day`` boolean column to
``schedule_event_subscriptions`` so multi-day / all-day events (e.g. "a trip from
today to 3 days later") render and sync correctly. Multi-day *span* is still
carried by ``dtstart`` + ``duration_minutes``; this flag only distinguishes
all-day events (display + Google ``date`` body).

Inspector-guarded + batch (SQLite rebuild via ``render_as_batch``) so it runs
cleanly on SQLite and PostgreSQL and no-ops if the column already exists.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260621b_schedule_all_day"
down_revision: Union[str, Sequence[str], None] = "20260621_schedule_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "schedule_event_subscriptions" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("schedule_event_subscriptions")}
    if "all_day" not in cols:
        with op.batch_alter_table("schedule_event_subscriptions") as batch_op:
            batch_op.add_column(
                sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false())
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "schedule_event_subscriptions" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("schedule_event_subscriptions")}
    if "all_day" in cols:
        with op.batch_alter_table("schedule_event_subscriptions") as batch_op:
            batch_op.drop_column("all_day")
