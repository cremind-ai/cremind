"""User Document Search — deep-research jobs.

Revision ID: 20260927_userdocs_research
Revises: 20260926_userdocs_citations
Create Date: 2026-09-27

One new table, ``userdoc_research_jobs``: a research job over the user's
documents (compile a folder, analyze a case against the law), with its
checkpoint and its dossier, so a job survives a restart and resumes after the
user answers a clarification.

Delivery back to the conversation is two integer counters (``rev`` /
``delivered_rev``) claimed with one conditional UPDATE, not a column on
``event_runs``: a research job is not an event run and can become deliverable
several times over its life.

Two cascades carry the lifecycle: deleting a profile or a conversation deletes
its jobs. ``conversation_id`` is nullable because jobs started over REST/CLI
belong to no conversation.

Purely additive: no existing table is altered, so the same DDL runs on SQLite
and PostgreSQL. ``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260927_userdocs_research"
down_revision: Union[str, Sequence[str], None] = "20260926_userdocs_citations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "userdoc_research_jobs"

# (index name, columns). Created after the table, guarded, so a partial earlier
# run self-heals rather than failing on a duplicate index.
_INDEXES = (
    ("ix_userdoc_research_jobs_profile_created", ["profile", "created_at"]),
    ("ix_userdoc_research_jobs_conversation", ["conversation_id"]),
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=16), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("conversation_id", sa.String(length=128), nullable=True),
            sa.Column("run_id", sa.String(length=160), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("phase", sa.String(length=64), nullable=True),
            sa.Column("mode", sa.String(length=16), nullable=False),
            sa.Column("domain", sa.String(length=16), nullable=False),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("scope", sa.JSON(), nullable=True),
            sa.Column("reference_scope", sa.JSON(), nullable=True),
            sa.Column("answers", sa.JSON(), nullable=True),
            sa.Column("state", sa.JSON(), nullable=True),
            sa.Column("dossier", sa.JSON(), nullable=True),
            sa.Column("model_group", sa.String(length=8), nullable=True),
            sa.Column("provider", sa.String(length=64), nullable=True),
            sa.Column("model", sa.String(length=128), nullable=True),
            sa.Column("tokens_in", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("tokens_out", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("budget", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("elapsed_s", sa.Float(), nullable=False, server_default=sa.text("0")),
            sa.Column("rev", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("delivered_rev", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("finished_at", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(
                ["profile"], ["profiles.name"], ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["conversation_id"], ["conversations.id"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        existing = {i["name"] for i in inspector.get_indexes(_TABLE)}
        for name, columns in _INDEXES:
            if name not in existing:
                op.create_index(name, _TABLE, columns)


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
