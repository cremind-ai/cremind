"""Cremind Tag hardware hosts — gateways driven by Cremind itself.

Revision ID: 20261002_tag_hosts
Revises: 20261001_tag_setup
Create Date: 2026-10-02

A **hardware host** is a computer whose USB ports Cremind drives gateways on:
the backend's own (``server``) or an enrolled Cremind desktop installation
(``desktop``). Until now every worker was an external Cremind Connect or a
manual runtime.

- New tables ``tag_hosts`` (identity, capabilities, runtime status),
  ``tag_host_access`` (which profiles may search and claim UNCLAIMED hardware
  on a host; it never exposes another profile's devices) and
  ``tag_host_credentials`` (a desktop host's credential, scoped to that host
  and one profile; SHA-256 of the secret only; left out of every backup like
  ``tag_credentials``).
- ``tag_companions`` gains ``execution_kind`` (where the worker runs, apart
  from ``mode``, who owns the hardware) and ``host_id``. Every existing row is
  ``legacy_external`` through the server default: its worker is a Connect or
  manual runtime reaching the connector API over HTTP, exactly as before.
- ``tag_operations`` gains ``host_id`` and ``ix_tag_operations_host``: a
  gateway search or a connection runs on a host before any worker exists.

Purely additive and re-runnable, SQLite and PostgreSQL alike: columns are
added with plain ``ALTER TABLE … ADD COLUMN`` (never ``batch_alter_table``,
whose SQLite rebuild would cascade-delete a companion's credentials and
devices), tables and indexes only when missing. Existing ids and meanings
are kept. ``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261002_tag_hosts"
down_revision: Union[str, Sequence[str], None] = "20261001_tag_setup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ADDED_COLUMNS = (
    ("tag_companions", "execution_kind",
     lambda: sa.Column("execution_kind", sa.String(length=16), nullable=False,
                       server_default=sa.text("'legacy_external'"))),
    ("tag_companions", "host_id", lambda: sa.Column("host_id", sa.String(length=36), nullable=True)),
    ("tag_operations", "host_id", lambda: sa.Column("host_id", sa.String(length=36), nullable=True)),
)

_TABLES = ("tag_hosts", "tag_host_access", "tag_host_credentials")

_INDEXES = (
    ("tag_operations", "ix_tag_operations_host", ["host_id", "state"]),
    ("tag_host_access", "ix_tag_host_access_profile", ["profile_id"]),
    ("tag_host_credentials", "ix_tag_host_credentials_host", ["host_id"]),
)


def _table_defs() -> dict[str, tuple]:
    empty = sa.text("''")
    return {
        "tag_hosts": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False, server_default=empty),
            sa.Column("owner_profile", sa.String(length=128), nullable=True),
            sa.Column("owner_profile_id", sa.String(length=36), nullable=True),
            sa.Column("installation_id", sa.String(length=64), nullable=True),
            sa.Column("public_key", sa.String(length=64), nullable=True),
            sa.Column("platform", sa.String(length=16), nullable=False, server_default=empty),
            sa.Column("version", sa.String(length=64), nullable=False, server_default=empty),
            sa.Column("capabilities", sa.JSON(), nullable=True),
            sa.Column("status", sa.JSON(), nullable=True),
            sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'active'")),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("last_seen_at", sa.Float(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        ),
        "tag_host_access": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("host_id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("profile_id", sa.String(length=36), nullable=False),
            sa.Column("granted_by", sa.String(length=128), nullable=False, server_default=empty),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("revoked_at", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["host_id"], ["tag_hosts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("host_id", "profile_id", name="uq_tag_host_access_host_profile"),
        ),
        "tag_host_credentials": (
            sa.Column("id", sa.String(length=40), nullable=False),
            sa.Column("host_id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("profile_id", sa.String(length=36), nullable=False),
            sa.Column("secret_sha256", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=128), nullable=False, server_default=empty),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("last_used_at", sa.Float(), nullable=True),
            sa.Column("revoked_at", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["host_id"], ["tag_hosts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        ),
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, name, column in _ADDED_COLUMNS:
        if table in tables and name not in {c["name"] for c in inspector.get_columns(table)}:
            op.add_column(table, column())

    defs = _table_defs()
    for name in _TABLES:
        if name not in set(sa.inspect(bind).get_table_names()):
            op.create_table(name, *defs[name])

    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())
    for table, name, columns in _INDEXES:
        if table in present and name not in {i["name"] for i in inspector.get_indexes(table)}:
            op.create_index(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())
    if "tag_operations" in present and "ix_tag_operations_host" in {
            i["name"] for i in inspector.get_indexes("tag_operations")}:
        op.drop_index("ix_tag_operations_host", table_name="tag_operations")
    for name in reversed(_TABLES):
        if name in present:
            op.drop_table(name)
    inspector = sa.inspect(bind)
    for table, name, _ in reversed(_ADDED_COLUMNS):
        if table in present and name in {c["name"] for c in inspector.get_columns(table)}:
            # Plain ALTER TABLE … DROP COLUMN (SQLite 3.35+): no table rebuild.
            op.drop_column(table, name)
