"""Cremind Tag — companions, connector credentials, devices, journal, deliveries.

Revision ID: 20260930_tags
Revises: 20260929_profile_working_dir
Create Date: 2026-09-30

Ten new tables (see the Cremind Tag section of ``app/storage/models.py``):

- ``tag_companions`` / ``tag_credentials`` / ``tag_devices`` / ``tag_commands`` /
  ``tag_previews`` — the hardware side, system-wide, each child cascading with
  its companion or device. A ``content`` credential and a tag's owner point at
  ``profiles.name``: the credential cascades with the profile, the owner is SET
  NULL (the delete path releases the tag first, see ``app.tags.service``).
- ``tag_streams`` / ``tag_events`` / ``tag_deliveries`` / ``tag_settings`` — the
  per-profile journal and what it projected onto tags, cascading with the
  profile.
- ``tag_counters`` — one ``delivery_id`` row, seeded here. No table uses a
  database sequence (a restore would not reset one).

Purely additive: no existing table is altered, every create is guarded by an
inspector check and every index is re-inspected, so a re-run (or a partial
earlier run) is a no-op on SQLite and PostgreSQL alike. ``tag_deliveries.id``
is ``BIGINT`` with ``autoincrement=False`` so PostgreSQL does not make it a
``BIGSERIAL``. Epochs and revisions are ``BIGINT`` too: the protocol's are
uint32, which PostgreSQL's ``INTEGER`` cannot hold past 2**31 - 1.
``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260930_tags"
down_revision: Union[str, Sequence[str], None] = "20260929_profile_working_dir"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Creation order (FK-safe); the downgrade drops in reverse.
_TABLES = (
    "tag_companions",
    "tag_credentials",
    "tag_devices",
    "tag_streams",
    "tag_events",
    "tag_deliveries",
    "tag_counters",
    "tag_commands",
    "tag_previews",
    "tag_settings",
)

# (table, index name, columns). Created after the tables, guarded.
_INDEXES = (
    ("tag_credentials", "ix_tag_credentials_companion", ["companion_id"]),
    ("tag_credentials", "ix_tag_credentials_profile", ["profile"]),
    ("tag_devices", "ix_tag_devices_owner", ["owner_profile"]),
    ("tag_deliveries", "ix_tag_deliveries_device_created", ["tag_device_id", "created_at"]),
    ("tag_deliveries", "ix_tag_deliveries_stage", ["stage"]),
    ("tag_commands", "ix_tag_commands_companion_status", ["companion_id", "status"]),
)


def _table_defs() -> dict[str, tuple]:
    zero = sa.text("0")
    empty = sa.text("''")
    return {
        "tag_companions": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("created_by", sa.String(length=128), nullable=False, server_default=empty),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("last_seen_at", sa.Float(), nullable=True),
            sa.Column("version", sa.String(length=64), nullable=True),
            sa.Column("host", sa.String(length=255), nullable=True),
            sa.Column("heartbeat", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        ),
        "tag_credentials": (
            sa.Column("id", sa.String(length=40), nullable=False),
            sa.Column("companion_id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=True),
            sa.Column("secret_sha256", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=128), nullable=False, server_default=empty),
            sa.Column("created_by", sa.String(length=128), nullable=False, server_default=empty),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("last_used_at", sa.Float(), nullable=True),
            sa.Column("revoked_at", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["companion_id"], ["tag_companions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        ),
        "tag_devices": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("companion_id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("hw_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False, server_default=empty),
            sa.Column("owner_profile", sa.String(length=128), nullable=True),
            sa.Column("bridge_device_id", sa.String(length=36), nullable=True),
            sa.Column("epoch", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("rotation", sa.Integer(), nullable=False, server_default=zero),
            sa.Column("board", sa.Integer(), nullable=True),
            sa.Column("panel", sa.Integer(), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("planes", sa.Integer(), nullable=True),
            sa.Column("fw", sa.String(length=32), nullable=True),
            sa.Column("info", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'unclaimed'")),
            sa.Column("battery_mv", sa.Integer(), nullable=True),
            sa.Column("rssi", sa.Integer(), nullable=True),
            sa.Column("last_contact_at", sa.Float(), nullable=True),
            sa.Column("desired_revision", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("displayed_revision", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("displayed_digest", sa.String(length=64), nullable=True),
            sa.Column("clear_required", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("claimed_at", sa.Float(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["companion_id"], ["tag_companions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["owner_profile"], ["profiles.name"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["bridge_device_id"], ["tag_devices.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("companion_id", "kind", "hw_id", name="uq_tag_devices_companion_kind_hw"),
        ),
        "tag_streams": (
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("stream_id", sa.String(length=36), nullable=False),
            sa.Column("next_seq", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("projected_seq", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("next_delivery_seq", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("state", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("profile"),
        ),
        "tag_events": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("seq", sa.BigInteger(), nullable=False),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("durability", sa.String(length=16), nullable=False),
            sa.Column("replace_key", sa.String(length=160), nullable=True),
            sa.Column("source_type", sa.String(length=32), nullable=False),
            sa.Column("source_id", sa.String(length=160), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("profile", "seq", name="uq_tag_events_profile_seq"),
        ),
        "tag_deliveries": (
            sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("seq", sa.BigInteger(), nullable=False),
            sa.Column("companion_id", sa.String(length=36), nullable=True),
            sa.Column("tag_device_id", sa.String(length=36), nullable=False),
            sa.Column("epoch", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("event_id", sa.String(length=36), nullable=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default=zero),
            sa.Column("replace_key", sa.String(length=160), nullable=True),
            sa.Column("resolves", sa.String(length=160), nullable=True),
            sa.Column("card", sa.JSON(), nullable=True),
            sa.Column("stage", sa.String(length=32), nullable=False, server_default=sa.text("'queued'")),
            sa.Column("outcome", sa.String(length=32), nullable=True),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("revision", sa.BigInteger(), nullable=True),
            sa.Column("digest", sa.String(length=64), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("timing", sa.JSON(), nullable=True),
            sa.Column("stage_times", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.Column("finished_at", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["companion_id"], ["tag_companions.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["tag_device_id"], ["tag_devices.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["event_id"], ["tag_events.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("profile", "seq", name="uq_tag_deliveries_profile_seq"),
        ),
        "tag_counters": (
            sa.Column("name", sa.String(length=32), nullable=False),
            sa.Column("value", sa.BigInteger(), nullable=False, server_default=zero),
            sa.PrimaryKeyConstraint("name"),
        ),
        "tag_commands": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("companion_id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("args", sa.JSON(), nullable=True),
            sa.Column("requested_by", sa.String(length=128), nullable=False, server_default=empty),
            sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'queued'")),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("claimed_at", sa.Float(), nullable=True),
            sa.Column("completed_at", sa.Float(), nullable=True),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["companion_id"], ["tag_companions.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        ),
        "tag_previews": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("tag_device_id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("revision", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("epoch", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("png_base64", sa.Text(), nullable=False),
            sa.Column("delivery_ids", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["tag_device_id"], ["tag_devices.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tag_device_id", "kind", name="uq_tag_previews_device_kind"),
        ),
        "tag_settings": (
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("options", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("profile"),
        ),
    }


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    defs = _table_defs()
    for name in _TABLES:
        if name not in existing:
            op.create_table(name, *defs[name])

    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())
    for table, name, columns in _INDEXES:
        if table not in present:
            continue
        if name not in {i["name"] for i in inspector.get_indexes(table)}:
            op.create_index(name, table, columns)

    if "tag_counters" in present:
        seeded = bind.execute(
            sa.text("SELECT 1 FROM tag_counters WHERE name = :n"), {"n": "delivery_id"},
        ).fetchone()
        if seeded is None:
            bind.execute(
                sa.text("INSERT INTO tag_counters (name, value) VALUES (:n, 0)"),
                {"n": "delivery_id"},
            )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for name in reversed(_TABLES):
        if name in existing:
            op.drop_table(name)
