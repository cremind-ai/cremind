"""User Document Search — the citation registry.

Revision ID: 20260926_userdocs_citations
Revises: 20260925_userdocs
Create Date: 2026-09-26

One new table, ``userdoc_citations``: every citation token a User Documents
tool printed, per conversation, with a snapshot of what it pointed at. When an
answer is saved its tokens are checked against this table, so an invented or
tampered token is flagged instead of rendered as a trustworthy source.

Two cascades carry the lifecycle: deleting a profile or a conversation deletes
its rows. ``UNIQUE(conversation_id, token)`` makes re-issuing a token a no-op.
``conversation_id`` is nullable because the A2A path runs its tools before the
conversation row of a first message exists; those rows are bound when the
answer is finalized.

Purely additive: no existing table is altered, so the same DDL runs on SQLite
and PostgreSQL. ``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260926_userdocs_citations"
down_revision: Union[str, Sequence[str], None] = "20260925_userdocs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "userdoc_citations"

# (index name, columns). Created after the table, guarded, so a partial earlier
# run self-heals rather than failing on a duplicate index.
_INDEXES = (
    ("ix_userdoc_citations_profile_cite", ["profile", "cite_id"]),
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("conversation_id", sa.String(length=128), nullable=True),
            sa.Column("token", sa.String(length=32), nullable=False),
            sa.Column("cite_id", sa.String(length=8), nullable=False),
            sa.Column("target", sa.String(length=8), nullable=False),
            sa.Column("ref_id", sa.Integer(), nullable=True),
            sa.Column("text_hash", sa.String(length=32), nullable=True),
            sa.Column("source_kind", sa.String(length=8), nullable=False),
            sa.Column("locator", sa.JSON(), nullable=True),
            sa.Column("label", sa.String(length=256), nullable=False),
            sa.Column("rel_path", sa.Text(), nullable=False),
            sa.Column("snippet", sa.Text(), nullable=False),
            sa.Column("leaf", sa.String(length=16), nullable=False),
            sa.Column("web_link", sa.Text(), nullable=True),
            sa.Column("issued_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["profile"], ["profiles.name"], ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["conversation_id"], ["conversations.id"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "conversation_id", "token", name="uq_userdoc_citations_conv_token",
            ),
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
