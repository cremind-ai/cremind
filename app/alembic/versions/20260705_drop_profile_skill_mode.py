"""Drop the dead ``profiles.skill_mode`` column.

Revision ID: 20260705_drop_profile_skill_mode
Revises: 20260703_event_runs
Create Date: 2026-07-05

``skill_mode`` backed the **Automatic Skill Mode** feature, which has been
removed (see ``app/agent/agent.py`` — the per-profile tool-card embeddings that
drove it are gone). The mapped column was deleted from
:class:`app.storage.models.ProfileModel`, but the physical column lingered in
every pre-existing ``~/.cremind`` DB as ``VARCHAR(16) NOT NULL`` — and, on the
oldest installs, with **no default**.

That is a live bug, not just dead weight: because the ORM model no longer
declares ``skill_mode``, *every* INSERT into ``profiles`` omits it —
``create_profile`` (ORM) and the ``__server__`` pseudo-profile bootstrap (raw
SQL) alike — so on any DB where the column lacks a usable default the insert
fails with ``NOT NULL constraint failed: profiles.skill_mode``.

This migration removes the column so the physical schema matches the ORM model.
It is inspector-guarded and therefore identical for a fresh install (baseline
rebuilt from live metadata → the column never existed → no-op) and a
pre-existing DB (column present → dropped), on **both SQLite and PostgreSQL**.
``op.batch_alter_table`` is a plain ``ALTER TABLE`` on PostgreSQL and a
table-copy on SQLite (which historically could not ``DROP COLUMN``), mirroring
the ``conversations.kind`` handling in ``20260703_event_runs``.

``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260705_drop_profile_skill_mode"
down_revision: Union[str, Sequence[str], None] = "20260703_event_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "profiles" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("profiles")}
    if "skill_mode" in cols:
        with op.batch_alter_table("profiles") as batch_op:
            batch_op.drop_column("skill_mode")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "profiles" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("profiles")}
    if "skill_mode" not in cols:
        # ``server_default`` so re-adding a NOT NULL column to a table that
        # already has rows succeeds on both backends.
        with op.batch_alter_table("profiles") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "skill_mode", sa.String(length=16),
                    nullable=False, server_default="manual",
                )
            )
