"""Event-task inbox — record HOW a task result reached its origin chat.

Revision ID: 20260814_task_inbox
Revises: 20260813_token_serial
Create Date: 2026-08-14

A task result no longer always arrives as a continuation turn. When the origin
conversation is mid-reasoning the result waits in that conversation's inbox and
the agent may pull it with ``get_event_task_results`` instead; whatever is still
unread is injected as one coalesced turn when the turn ends.

``origin_delivered_at`` keeps its exact meaning ("handed over, and the claim is
the exactly-once lock") — the inbox needs no state of its own, because a
terminal run row with ``deliver_to_origin`` and ``origin_delivered_at IS NULL``
already *is* a pending inbox entry. So this migration adds one purely
descriptive column:

``event_runs.origin_delivery_mode`` — "injected" | "read" | "skipped" | NULL.

Deliberately never a predicate, so no existing query (the retention prune, the
boot sweep's work list, ``has_live_run_for_subscription``) has to learn about
it. It exists for the surfaces: without it the Events page and ``cremind
event-runs`` show "delivered" for a result that never produced a turn, which is
unexplainable to a user going to look for it.

No backfill: NULL correctly reads as "delivered before modes were recorded", and
both surfaces fall back to ``origin_delivered_at`` for those rows.
``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped — this is purely additive.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260814_task_inbox"
down_revision: Union[str, Sequence[str], None] = "20260813_token_serial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every index this table is expected to carry. SQLite implements ALTER via a
# table copy, which drops reflected indexes — so they are re-asserted after the
# batch, exactly as 20260810_event_tasks does.
_EVENT_RUN_INDEXES = (
    ("ix_event_runs_profile", ["profile"]),
    ("ix_event_runs_conversation_id", ["conversation_id"]),
    ("ix_event_runs_status", ["status"]),
    ("ix_event_runs_origin_conversation_id", ["origin_conversation_id"]),
    ("ix_event_runs_sub", ["source_kind", "subscription_id", "created_at"]),
    ("ix_event_runs_profile_created", ["profile", "created_at"]),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "event_runs" not in set(inspector.get_table_names()):
        return

    cols = {c["name"] for c in inspector.get_columns("event_runs")}
    if "origin_delivery_mode" not in cols:
        with op.batch_alter_table("event_runs") as batch_op:
            batch_op.add_column(
                sa.Column("origin_delivery_mode", sa.String(length=16), nullable=True)
            )

    existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes("event_runs")}
    for name, columns in _EVENT_RUN_INDEXES:
        if name not in existing_idx:
            op.create_index(name, "event_runs", columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "event_runs" not in set(inspector.get_table_names()):
        return

    cols = {c["name"] for c in inspector.get_columns("event_runs")}
    if "origin_delivery_mode" in cols:
        with op.batch_alter_table("event_runs") as batch_op:
            batch_op.drop_column("origin_delivery_mode")

    existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes("event_runs")}
    for name, columns in _EVENT_RUN_INDEXES:
        if name not in existing_idx:
            op.create_index(name, "event_runs", columns)
