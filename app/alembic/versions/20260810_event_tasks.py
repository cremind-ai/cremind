"""Event tasks — one-shot events that return their result to the origin chat.

Revision ID: 20260810_event_tasks
Revises: 20260720_event_paused
Create Date: 2026-08-10

An EVENT TASK is a one-shot subscription: it fires once, runs its action in a
hidden event-run conversation, delivers that run's final answer back into the
conversation that registered it (so the agent there continues its flow), then
terminates. Three additive changes, all inspector-guarded and batch-wrapped so a
fresh install (baseline built from live ORM metadata) and a pre-existing
``~/.cremind`` DB upgrade identically on **both SQLite and PostgreSQL**:

1. ``skill_event_subscriptions`` / ``file_watcher_subscriptions`` —
   ``task`` (bool), ``task_status`` (active|triggered|completed|cancelled|
   timed_out; NULL for standing rules), ``timeout_at`` and ``completed_at``
   (epoch seconds, this table's convention). ``task_status`` is the atomic
   claim target that makes a task fire exactly once.

2. ``schedule_event_subscriptions`` — ``task`` (bool) only. The existing
   ``status`` column already carries the lifecycle (a one-shot flips to
   ``completed`` at fire), and a schedule task fires at a known time so it
   needs no timeout.

3. ``event_runs`` — ``origin_conversation_id`` (FK → conversations, SET NULL,
   indexed), ``deliver_to_origin`` (bool) and ``origin_delivered_at`` (epoch
   MS). The nullable ``origin_delivered_at`` is the exactly-once delivery
   claim; the SET NULL FK degrades a run whose origin was deleted to
   "notification only" instead of dangling.

No backfill: the server defaults make every existing row valid (standing
subscriptions get ``task=false``/``task_status=NULL``, historical runs get
``deliver_to_origin=false``). ``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped —
these are purely additive columns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260810_event_tasks"
down_revision: Union[str, Sequence[str], None] = "20260720_event_paused"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Naming convention used inside the SQLite batch rebuild so the reflected,
# originally-unnamed FKs acquire deterministic names (needed by downgrade).
_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

# skill_event_subscriptions + file_watcher_subscriptions share the whole task
# column set (they are the two "waits for something to happen" families).
_SUB_TABLES = ("skill_event_subscriptions", "file_watcher_subscriptions")
_SUB_COLUMNS = (
    ("task", lambda: sa.Column("task", sa.Boolean(), nullable=False, server_default=sa.false())),
    ("task_status", lambda: sa.Column("task_status", sa.String(length=16), nullable=True)),
    ("timeout_at", lambda: sa.Column("timeout_at", sa.Float(), nullable=True)),
    ("completed_at", lambda: sa.Column("completed_at", sa.Float(), nullable=True)),
)

_EVENT_RUN_COLUMNS = (
    (
        "origin_conversation_id",
        lambda: sa.Column("origin_conversation_id", sa.String(length=128), nullable=True),
    ),
    (
        "deliver_to_origin",
        lambda: sa.Column(
            "deliver_to_origin", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    ),
    ("origin_delivered_at", lambda: sa.Column("origin_delivered_at", sa.Float(), nullable=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ── 1. skill-event + file-watcher subscriptions ────────────────────────
    for table in _SUB_TABLES:
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        missing = [factory for name, factory in _SUB_COLUMNS if name not in cols]
        if missing:
            with op.batch_alter_table(table) as batch_op:
                for factory in missing:
                    batch_op.add_column(factory())

    # ── 2. schedule subscriptions ──────────────────────────────────────────
    if "schedule_event_subscriptions" in tables:
        cols = {c["name"] for c in inspector.get_columns("schedule_event_subscriptions")}
        if "task" not in cols:
            with op.batch_alter_table("schedule_event_subscriptions") as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "task", sa.Boolean(), nullable=False, server_default=sa.false()
                    )
                )

    # ── 3. event_runs: origin + delivery tracking ──────────────────────────
    if "event_runs" in tables:
        cols = {c["name"] for c in inspector.get_columns("event_runs")}
        missing = [factory for name, factory in _EVENT_RUN_COLUMNS if name not in cols]
        need_origin_fk = "origin_conversation_id" not in cols
        if missing:
            # One batch (single SQLite table copy) adds the columns and the FK.
            with op.batch_alter_table(
                "event_runs", naming_convention=_NAMING_CONVENTION,
            ) as batch_op:
                for factory in missing:
                    batch_op.add_column(factory())
                if need_origin_fk:
                    batch_op.create_foreign_key(
                        "fk_event_runs_origin_conversation_id",
                        "conversations", ["origin_conversation_id"], ["id"],
                        ondelete="SET NULL",
                    )
        # Re-assert every expected index — the batch rebuild may have dropped
        # some, and the new origin index is created here too.
        existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes("event_runs")}
        for name, columns in (
            ("ix_event_runs_profile", ["profile"]),
            ("ix_event_runs_conversation_id", ["conversation_id"]),
            ("ix_event_runs_status", ["status"]),
            ("ix_event_runs_origin_conversation_id", ["origin_conversation_id"]),
            ("ix_event_runs_sub", ["source_kind", "subscription_id", "created_at"]),
            ("ix_event_runs_profile_created", ["profile", "created_at"]),
        ):
            if name not in existing_idx:
                op.create_index(name, "event_runs", columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "event_runs" in tables:
        cols = {c["name"] for c in inspector.get_columns("event_runs")}
        present = [name for name, _ in _EVENT_RUN_COLUMNS if name in cols]
        if present:
            with op.batch_alter_table(
                "event_runs", naming_convention=_NAMING_CONVENTION,
            ) as batch_op:
                if "origin_conversation_id" in cols:
                    # Best-effort: a fresh-install FK may be unnamed, in which
                    # case the batch rebuild simply drops it with the column.
                    try:
                        batch_op.drop_constraint(
                            "fk_event_runs_origin_conversation_id", type_="foreignkey",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                for name in present:
                    batch_op.drop_column(name)

    if "schedule_event_subscriptions" in tables:
        cols = {c["name"] for c in inspector.get_columns("schedule_event_subscriptions")}
        if "task" in cols:
            with op.batch_alter_table("schedule_event_subscriptions") as batch_op:
                batch_op.drop_column("task")

    for table in _SUB_TABLES:
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        present = [name for name, _ in _SUB_COLUMNS if name in cols]
        if present:
            with op.batch_alter_table(table) as batch_op:
                for name in present:
                    batch_op.drop_column(name)
