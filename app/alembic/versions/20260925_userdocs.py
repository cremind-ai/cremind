"""User Document Search — per-profile settings, caption cache, vision quota.

Revision ID: 20260925_userdocs
Revises: 20260829_channel_groups
Create Date: 2026-09-25

Three new tables, all small and all keyed by profile:

``userdoc_sources`` is one row per (profile, kind) — the profile's local folder
and, optionally, its Google Drive — holding *settings only*: whether it is on,
which folder, the exclude rules and the options blob. ``UNIQUE(profile, kind)``
is what makes "turn it on" an idempotent upsert.

``userdoc_captions`` caches what the vision model said about an image, keyed by
the image's sha256. ``userdoc_vision_usage`` counts captions per profile per
local day for the daily cap.

The index itself — files, chunks, the full-text index, the sync cursor — is NOT
here. It lives in a per-profile SQLite file owned by ``app.userdocs.index``,
because it is rebuildable, can run to gigabytes, and everything in this
database is copied by every backup and every upgrade snapshot.

Purely additive: no existing table is altered, so the same DDL runs on SQLite
and PostgreSQL. ``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260925_userdocs"
down_revision: Union[str, Sequence[str], None] = "20260829_channel_groups"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, columns). Created after the tables, guarded, so a partial
# earlier run self-heals rather than failing on a duplicate index.
_INDEXES = (
    ("ix_userdoc_sources_profile", "userdoc_sources", ["profile"]),
)

_TABLES_IN_DROP_ORDER = ("userdoc_vision_usage", "userdoc_captions", "userdoc_sources")


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "userdoc_sources" not in tables:
        op.create_table(
            "userdoc_sources",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False, server_default=sa.false(),
            ),
            sa.Column(
                "root_mode", sa.String(length=16), nullable=False,
                server_default="inherit",
            ),
            sa.Column("root_path", sa.Text(), nullable=True),
            sa.Column("excludes", sa.JSON(), nullable=True),
            sa.Column("options", sa.JSON(), nullable=True),
            sa.Column("first_sync_confirmed_at", sa.Float(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["profile"], ["profiles.name"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "profile", "kind", name="uq_userdoc_sources_profile_kind",
            ),
        )

    if "userdoc_captions" not in tables:
        op.create_table(
            "userdoc_captions",
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("variant", sa.String(length=8), nullable=False),
            sa.Column("caption_json", sa.JSON(), nullable=True),
            sa.Column("caption_text", sa.Text(), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=True),
            sa.Column("model", sa.String(length=128), nullable=True),
            sa.Column(
                "prompt_version", sa.Integer(), nullable=False, server_default="1",
            ),
            sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(
                ["profile"], ["profiles.name"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("profile", "sha256"),
        )

    if "userdoc_vision_usage" not in tables:
        op.create_table(
            "userdoc_vision_usage",
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("day", sa.String(length=10), nullable=False),
            sa.Column("captions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ocr_pages", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.ForeignKeyConstraint(
                ["profile"], ["profiles.name"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("profile", "day"),
        )

    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())
    for name, table, columns in _INDEXES:
        if table not in present:
            continue
        existing = {i["name"] for i in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for name in _TABLES_IN_DROP_ORDER:
        if name in tables:
            op.drop_table(name)
