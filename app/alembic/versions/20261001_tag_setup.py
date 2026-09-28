"""Cremind Tag simple setup — private workers, bindings, sessions, vault.

Revision ID: 20261001_tag_setup
Revises: 20260930_tags
Create Date: 2026-10-01

Protocol v2 (cremind-tag ``docs/connect-setup.md``): a Cremind Connect setup
session creates a **private** companion (one worker of one profile) whose
gateway, bridges and tags belong to that profile alone.

- ``tag_companions`` gains ``mode`` (every existing row becomes
  ``legacy_shared`` through the server default — unchanged behaviour), the
  owner (profile name + immutable UUID), the Connect installation and
  controller key of the worker, a worker ``generation`` and ``state``, a
  pause flag, the authorization lease and the gateway's canonical id.
- New tables: ``tag_authority``, ``tag_connect_installations``,
  ``tag_bindings``, ``tag_setup_sessions``, ``tag_operations``, ``tag_vault``,
  ``tag_revocations``, ``tag_idempotency`` (see ``app/storage/models.py``).

Purely additive and re-runnable: every column and table is added only when
missing, every index re-inspected. Columns are added with plain
``ALTER TABLE … ADD COLUMN`` — never ``batch_alter_table``, whose SQLite table
rebuild would cascade-delete the companion's credentials and devices.
``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261001_tag_setup"
down_revision: Union[str, Sequence[str], None] = "20260930_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COMPANION_COLUMNS = (
    ("mode", lambda: sa.Column("mode", sa.String(length=16), nullable=False,
                               server_default=sa.text("'legacy_shared'"))),
    ("owner_profile", lambda: sa.Column("owner_profile", sa.String(length=128), nullable=True)),
    ("owner_profile_id", lambda: sa.Column("owner_profile_id", sa.String(length=36), nullable=True)),
    ("installation_id", lambda: sa.Column("installation_id", sa.String(length=64), nullable=True)),
    ("controller_pub", lambda: sa.Column("controller_pub", sa.String(length=64), nullable=True)),
    ("generation", lambda: sa.Column("generation", sa.BigInteger(), nullable=False, server_default=sa.text("0"))),
    ("state", lambda: sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'active'"))),
    ("paused", lambda: sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false())),
    ("lease_expires_at", lambda: sa.Column("lease_expires_at", sa.Float(), nullable=True)),
    ("lease_credential_id", lambda: sa.Column("lease_credential_id", sa.String(length=40), nullable=True)),
    ("gateway_device_id", lambda: sa.Column("gateway_device_id", sa.String(length=32), nullable=True)),
)

_TABLES = (
    "tag_authority",
    "tag_connect_installations",
    "tag_bindings",
    "tag_setup_sessions",
    "tag_operations",
    "tag_vault",
    "tag_revocations",
    "tag_idempotency",
)

_INDEXES = (
    ("tag_bindings", "ix_tag_bindings_companion", ["companion_id"]),
    ("tag_bindings", "ix_tag_bindings_owner", ["owner_profile_id"]),
    ("tag_setup_sessions", "ix_tag_setup_sessions_owner", ["owner_profile", "created_at"]),
    ("tag_operations", "ix_tag_operations_owner", ["owner_profile", "created_at"]),
    ("tag_operations", "ix_tag_operations_companion", ["companion_id", "state"]),
)


def _table_defs() -> dict[str, tuple]:
    zero = sa.text("0")
    empty = sa.text("''")
    return {
        "tag_authority": (
            sa.Column("id", sa.String(length=16), nullable=False),
            sa.Column("installation_id", sa.String(length=36), nullable=False),
            sa.Column("authority_pub", sa.String(length=64), nullable=False),
            sa.Column("signing_kid", sa.String(length=32), nullable=False),
            sa.Column("master_kid", sa.String(length=32), nullable=False),
            sa.Column("master_check", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        ),
        "tag_connect_installations": (
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("public_key", sa.String(length=64), nullable=False),
            sa.Column("computer", sa.String(length=255), nullable=False, server_default=empty),
            sa.Column("platform", sa.String(length=16), nullable=False, server_default=empty),
            sa.Column("version", sa.String(length=64), nullable=False, server_default=empty),
            sa.Column("first_seen_at", sa.Float(), nullable=False),
            sa.Column("last_seen_at", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        ),
        "tag_bindings": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("device_id", sa.String(length=32), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("identity_pub", sa.String(length=64), nullable=False),
            sa.Column("short_id", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("companion_id", sa.String(length=36), nullable=False),
            sa.Column("tag_device_id", sa.String(length=36), nullable=True),
            sa.Column("owner_profile", sa.String(length=128), nullable=False),
            sa.Column("owner_profile_id", sa.String(length=36), nullable=False),
            sa.Column("state", sa.String(length=24), nullable=False, server_default=sa.text("'pairing'")),
            sa.Column("generation", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("pending_generation", sa.BigInteger(), nullable=True),
            sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("fw", sa.String(length=32), nullable=True),
            sa.Column("board", sa.Integer(), nullable=True),
            sa.Column("info", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("paired_at", sa.Float(), nullable=True),
            sa.Column("ready_at", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["companion_id"], ["tag_companions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tag_device_id"], ["tag_devices.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("device_id", name="uq_tag_bindings_device"),
        ),
        "tag_setup_sessions": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("operation", sa.String(length=24), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("owner_profile", sa.String(length=128), nullable=False),
            sa.Column("owner_profile_id", sa.String(length=36), nullable=False),
            sa.Column("session_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("token_sha256", sa.String(length=64), nullable=False),
            sa.Column("server_origin", sa.String(length=255), nullable=False),
            sa.Column("server_nonce", sa.String(length=64), nullable=True),
            sa.Column("installation_id", sa.String(length=64), nullable=True),
            sa.Column("installation_pub", sa.String(length=64), nullable=True),
            sa.Column("computer", sa.JSON(), nullable=True),
            sa.Column("verification_phrase", sa.String(length=128), nullable=True),
            sa.Column("gateway", sa.JSON(), nullable=True),
            sa.Column("companion_id", sa.String(length=36), nullable=True),
            sa.Column("operation_id", sa.String(length=36), nullable=True),
            sa.Column("native_approved_at", sa.Float(), nullable=True),
            sa.Column("browser_confirmed_at", sa.Float(), nullable=True),
            sa.Column("redeemed_at", sa.Float(), nullable=True),
            sa.Column("redeem_key", sa.String(length=64), nullable=True),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("error", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["owner_profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        ),
        "tag_operations": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=24), nullable=False),
            sa.Column("state", sa.String(length=24), nullable=False, server_default=sa.text("'queued'")),
            sa.Column("stage", sa.String(length=32), nullable=False, server_default=sa.text("'queued'")),
            sa.Column("stage_detail", sa.String(length=255), nullable=True),
            sa.Column("owner_profile", sa.String(length=128), nullable=False),
            sa.Column("owner_profile_id", sa.String(length=36), nullable=False),
            sa.Column("companion_id", sa.String(length=36), nullable=True),
            sa.Column("binding_id", sa.String(length=36), nullable=True),
            sa.Column("parent_id", sa.String(length=36), nullable=True),
            sa.Column("args", sa.JSON(), nullable=True),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("secret_sealed", sa.JSON(), nullable=True),
            sa.Column("command_ids", sa.JSON(), nullable=True),
            sa.Column("error", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.Column("finished_at", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["owner_profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["companion_id"], ["tag_companions.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        ),
        "tag_vault": (
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("companion_id", sa.String(length=36), nullable=False),
            sa.Column("subject", sa.String(length=32), nullable=False),
            sa.Column("owner_profile_id", sa.String(length=36), nullable=False),
            sa.Column("version", sa.BigInteger(), nullable=False),
            sa.Column("generation", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("stage", sa.String(length=16), nullable=False),
            sa.Column("key_id", sa.String(length=32), nullable=False),
            sa.Column("wrapped_key", sa.Text(), nullable=False),
            sa.Column("nonce", sa.String(length=32), nullable=False),
            sa.Column("ciphertext", sa.Text(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["companion_id"], ["tag_companions.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("companion_id", "subject", "version", name="uq_tag_vault_subject_version"),
        ),
        "tag_revocations": (
            sa.Column("device_id", sa.String(length=32), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("identity_pub", sa.String(length=64), nullable=False, server_default=empty),
            sa.Column("last_owner_profile", sa.String(length=128), nullable=False, server_default=empty),
            sa.Column("last_owner_profile_id", sa.String(length=36), nullable=False, server_default=empty),
            sa.Column("highest_generation", sa.BigInteger(), nullable=False, server_default=zero),
            sa.Column("reason", sa.String(length=64), nullable=False, server_default=empty),
            sa.Column("cleanup", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("revoked_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("device_id"),
        ),
        "tag_idempotency": (
            sa.Column("key", sa.String(length=200), nullable=False),
            sa.Column("request_sha256", sa.String(length=64), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=False),
            sa.Column("response", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("key"),
        ),
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tag_companions" in inspector.get_table_names():
        have = {c["name"] for c in inspector.get_columns("tag_companions")}
        for name, column in _COMPANION_COLUMNS:
            if name not in have:
                op.add_column("tag_companions", column())

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


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for name in reversed(_TABLES):
        if name in existing:
            op.drop_table(name)
    if "tag_companions" in existing:
        have = {c["name"] for c in sa.inspect(bind).get_columns("tag_companions")}
        for name, _ in reversed(_COMPANION_COLUMNS):
            if name in have:
                # Plain ALTER TABLE … DROP COLUMN (SQLite 3.35+): no table rebuild.
                op.drop_column("tag_companions", name)
