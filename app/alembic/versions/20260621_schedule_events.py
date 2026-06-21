"""Calendar & Schedule — time-based schedule-event subscriptions.

Revision ID: 20260621_schedule_events
Revises: 20260618_memory
Create Date: 2026-06-21

Adds ``schedule_event_subscriptions``: one row per schedule *rule* (not per
occurrence) for the Calendar & Schedule engine. The ``ScheduleManager`` fires
the row's action in its conversation at ``next_fire_at`` and, for a recurrence,
advances that pointer to the next occurrence after each fire — so an open-ended
recurrence is a single durable row.

Purely additive: one ``create_table`` + three indexes. Runs cleanly on SQLite
(``render_as_batch`` in ``app/alembic/env.py``) and PostgreSQL, and never
touches existing data. The table is not in the baseline's hardcoded table list,
so the inspector guard is belt-and-suspenders for reruns / fresh installs.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260621_schedule_events"
down_revision: Union[str, Sequence[str], None] = "20260618_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "schedule_event_subscriptions" not in existing_tables:
        op.create_table(
            "schedule_event_subscriptions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("conversation_id", sa.String(length=128), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
            sa.Column("action", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_reminder_only", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("schedule_kind", sa.String(length=32), nullable=False, server_default="instant"),
            sa.Column("dtstart", sa.String(length=32), nullable=False),
            sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("rrule", sa.Text(), nullable=True),
            sa.Column("recurrence_end_type", sa.String(length=16), nullable=True),
            sa.Column("recurrence_end_value", sa.String(length=64), nullable=True),
            sa.Column("timezone", sa.String(length=64), nullable=True),
            sa.Column("next_fire_at", sa.Float(), nullable=True),
            sa.Column("occurrences_fired", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
            sa.Column("source", sa.String(length=16), nullable=False, server_default="agent"),
            sa.Column("external_provider", sa.String(length=32), nullable=True),
            sa.Column("external_event_id", sa.String(length=256), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_schedule_event_subscriptions_conversation_id",
            "schedule_event_subscriptions",
            ["conversation_id"],
        )
        op.create_index(
            "ix_schedule_event_subscriptions_profile",
            "schedule_event_subscriptions",
            ["profile"],
        )
        op.create_index(
            "ix_schedule_event_subscriptions_next_fire_at",
            "schedule_event_subscriptions",
            ["next_fire_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "schedule_event_subscriptions" in set(inspector.get_table_names()):
        op.drop_table("schedule_event_subscriptions")
