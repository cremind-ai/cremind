"""Calendar & Schedule — drop the reminder-only flag (events always run an action).

Revision ID: 20260621c_drop_reminder
Revises: 20260621b_schedule_all_day
Create Date: 2026-06-21

Reminder mode was removed: every schedule event now runs its action in the
registering conversation when it fires (the action defaults to the title when
none is given). The ``is_reminder_only`` column on ``schedule_event_subscriptions``
is therefore obsolete and dropped. Inspector-guarded + batch (SQLite rebuild via
``render_as_batch``) so it runs cleanly on SQLite and PostgreSQL and no-ops if the
column is already gone.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260621c_drop_reminder"
down_revision: Union[str, Sequence[str], None] = "20260621b_schedule_all_day"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "schedule_event_subscriptions" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("schedule_event_subscriptions")}
    if "is_reminder_only" in cols:
        with op.batch_alter_table("schedule_event_subscriptions") as batch_op:
            batch_op.drop_column("is_reminder_only")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "schedule_event_subscriptions" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("schedule_event_subscriptions")}
    if "is_reminder_only" not in cols:
        with op.batch_alter_table("schedule_event_subscriptions") as batch_op:
            batch_op.add_column(
                sa.Column("is_reminder_only", sa.Boolean(), nullable=False, server_default=sa.false())
            )
