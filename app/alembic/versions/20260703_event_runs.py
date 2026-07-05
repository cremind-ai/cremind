"""Event runs — decouple event execution from conversations.

Revision ID: 20260703_event_runs
Revises: 20260627_llm_messages
Create Date: 2026-07-03

Three changes, all additive / defensive / inspector-guarded so a fresh install
(baseline rebuilt from live ORM metadata) and a pre-existing ``~/.cremind`` DB
upgrade identically, on **both SQLite and PostgreSQL**:

1. ``conversations.kind`` — ``chat`` (normal thread) vs ``event_run`` (hidden
   per-trigger conversation). Additive column, defaults ``chat``.

2. ``event_runs`` — one row per fired event trigger (skill / file-watcher /
   schedule), tracking its own hidden conversation and status
   (running | pending | completed | failed | cancelled). New table.

3. ``usage_records`` — the ``conversation_id`` FK changes from
   ``NOT NULL + ON DELETE CASCADE`` to ``NULLABLE + ON DELETE SET NULL`` so
   deleting a conversation / event run / rule no longer erases its usage from
   Usage & Cost; plus a new ``event_run_id`` column (plain, indexed, no FK) so
   per-run usage stays attributable after the run and its conversation are gone.

The ``usage_records`` FK change requires a table rebuild on SQLite (SQLite can't
``ALTER`` a constraint). That is done in a single ``batch_alter_table`` (one
copy): a ``naming_convention`` gives the reflected — originally unnamed — FK a
deterministic name so it can be dropped, then the ``SET NULL`` FK and the new
column are added together. On PostgreSQL the original FK name is discovered via
the inspector (never hardcoded) and altered in place. All ``usage_records``
indexes are re-asserted afterwards, guarded, in case the rebuild dropped any.

No backfill: ``event_runs`` starts empty; historical trigger firings are not
reconstructed. ``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260703_event_runs"
down_revision: Union[str, Sequence[str], None] = "20260627_llm_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Naming convention used inside the SQLite batch rebuild so the reflected,
# originally-unnamed FKs acquire deterministic names we can target with
# ``drop_constraint``.
_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

# Every index expected on ``usage_records`` after the rebuild. Re-asserted
# (guarded) so a batch rebuild that dropped an index self-heals.
_USAGE_INDEXES = (
    ("ix_usage_records_conversation_id", ["conversation_id"]),
    ("ix_usage_records_message_id", ["message_id"]),
    ("ix_usage_records_event_run_id", ["event_run_id"]),
    ("ix_usage_records_profile", ["profile"]),
    ("ix_usage_records_provider", ["provider"]),
    ("ix_usage_records_model", ["model"]),
    ("ix_usage_records_source_kind", ["source_kind"]),
    ("ix_usage_records_tool_id", ["tool_id"]),
    ("ix_usage_records_total_usd", ["total_usd"]),
    ("ix_usage_records_conv_msg", ["conversation_id", "message_id"]),
    ("ix_usage_records_profile_created", ["profile", "created_at"]),
)


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ── 1. conversations.kind ──────────────────────────────────────────
    if "conversations" in tables:
        conv_cols = {c["name"] for c in inspector.get_columns("conversations")}
        if "kind" not in conv_cols:
            with op.batch_alter_table("conversations") as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "kind", sa.String(length=16),
                        nullable=False, server_default="chat",
                    )
                )

    # ── 2. event_runs ──────────────────────────────────────────────────
    if "event_runs" not in tables:
        op.create_table(
            "event_runs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("source_kind", sa.String(length=16), nullable=False),
            sa.Column("subscription_id", sa.String(length=36), nullable=False),
            sa.Column("conversation_id", sa.String(length=128), nullable=True),
            sa.Column("run_id", sa.String(length=200), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
            sa.Column("label", sa.String(length=512), nullable=False, server_default=""),
            sa.Column("action", sa.Text(), nullable=False, server_default=""),
            sa.Column("trigger_payload", sa.JSON(), nullable=True),
            sa.Column("pending_question", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("finished_at", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_event_runs_profile", "event_runs", ["profile"])
        op.create_index("ix_event_runs_conversation_id", "event_runs", ["conversation_id"])
        op.create_index("ix_event_runs_status", "event_runs", ["status"])
        op.create_index("ix_event_runs_sub", "event_runs", ["source_kind", "subscription_id", "created_at"])
        op.create_index("ix_event_runs_profile_created", "event_runs", ["profile", "created_at"])

    # ── 3. usage_records: nullable + SET NULL conversation FK, + event_run_id ──
    if "usage_records" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("usage_records")}
        need_event_run_id = "event_run_id" not in cols
        # On a fresh install the baseline already built the column nullable.
        need_fk_change = not cols["conversation_id"]["nullable"]

        if need_event_run_id or need_fk_change:
            if need_fk_change:
                # Orphan pre-clean: FK enforcement is ON during the SQLite copy,
                # so a row pointing at a vanished conversation/message (only
                # possible via prior corruption under the old CASCADE) would
                # abort the rebuild. Normalize before rebuilding.
                bind.execute(sa.text(
                    "DELETE FROM usage_records WHERE conversation_id IS NOT NULL "
                    "AND conversation_id NOT IN (SELECT id FROM conversations)"
                ))
                bind.execute(sa.text(
                    "UPDATE usage_records SET message_id = NULL "
                    "WHERE message_id IS NOT NULL "
                    "AND message_id NOT IN (SELECT id FROM messages)"
                ))

            if is_sqlite:
                with op.batch_alter_table(
                    "usage_records", naming_convention=_NAMING_CONVENTION,
                ) as batch_op:
                    if need_fk_change:
                        batch_op.drop_constraint(
                            "fk_usage_records_conversation_id_conversations",
                            type_="foreignkey",
                        )
                        batch_op.alter_column(
                            "conversation_id",
                            existing_type=sa.String(length=128),
                            nullable=True,
                        )
                        batch_op.create_foreign_key(
                            "fk_usage_records_conversation_id",
                            "conversations", ["conversation_id"], ["id"],
                            ondelete="SET NULL",
                        )
                    if need_event_run_id:
                        batch_op.add_column(
                            sa.Column("event_run_id", sa.String(length=36), nullable=True)
                        )
            else:
                if need_fk_change:
                    fks = inspector.get_foreign_keys("usage_records")
                    conv_fk = next(
                        (f for f in fks if f["constrained_columns"] == ["conversation_id"]),
                        None,
                    )
                    if conv_fk and conv_fk.get("name"):
                        op.drop_constraint(conv_fk["name"], "usage_records", type_="foreignkey")
                    op.alter_column(
                        "usage_records", "conversation_id",
                        existing_type=sa.String(length=128), nullable=True,
                    )
                    op.create_foreign_key(
                        "fk_usage_records_conversation_id",
                        "usage_records", "conversations",
                        ["conversation_id"], ["id"], ondelete="SET NULL",
                    )
                if need_event_run_id:
                    op.add_column(
                        "usage_records",
                        sa.Column("event_run_id", sa.String(length=36), nullable=True),
                    )

            # Re-assert every expected index (the rebuild may have dropped some;
            # the new event_run_id index is created here too).
            existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes("usage_records")}
            for name, columns in _USAGE_INDEXES:
                if name not in existing_idx:
                    op.create_index(name, "usage_records", columns)


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # Revert usage_records: SET NULL FK → CASCADE, drop event_run_id. The
    # column is left nullable (post-feature rows may carry NULL conversation_id;
    # forcing NOT NULL could fail — this is a best-effort downgrade).
    if "usage_records" in tables:
        cols = {c["name"] for c in inspector.get_columns("usage_records")}
        if "event_run_id" in cols:
            if is_sqlite:
                with op.batch_alter_table(
                    "usage_records", naming_convention=_NAMING_CONVENTION,
                ) as batch_op:
                    batch_op.drop_constraint(
                        "fk_usage_records_conversation_id", type_="foreignkey",
                    )
                    batch_op.create_foreign_key(
                        "fk_usage_records_conversation_id",
                        "conversations", ["conversation_id"], ["id"],
                        ondelete="CASCADE",
                    )
                    batch_op.drop_column("event_run_id")
            else:
                fks = inspector.get_foreign_keys("usage_records")
                conv_fk = next(
                    (f for f in fks if f["constrained_columns"] == ["conversation_id"]),
                    None,
                )
                if conv_fk and conv_fk.get("name"):
                    op.drop_constraint(conv_fk["name"], "usage_records", type_="foreignkey")
                op.create_foreign_key(
                    "fk_usage_records_conversation_id",
                    "usage_records", "conversations",
                    ["conversation_id"], ["id"], ondelete="CASCADE",
                )
                op.drop_column("usage_records", "event_run_id")

    if "event_runs" in tables:
        op.drop_table("event_runs")

    if "conversations" in tables:
        conv_cols = {c["name"] for c in inspector.get_columns("conversations")}
        if "kind" in conv_cols:
            with op.batch_alter_table("conversations") as batch_op:
                batch_op.drop_column("kind")
