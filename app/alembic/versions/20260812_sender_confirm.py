"""Per-client send-confirmation override on channel_senders.

Revision ID: 20260812_sender_confirm
Revises: 20260811_sender_phone
Create Date: 2026-08-12

One additive nullable column, ``channel_senders.send_confirmation``, letting a
single channel client opt out of (or into) the profile's "confirm before
messaging clients" setting — see :mod:`app.channels.send_policy`:

- ``NULL``       — inherit the profile setting (every existing row).
- ``"skip"``     — the agent may message this client without asking, which is
                   what lets an unattended automation reach a pre-approved
                   contact instead of stalling on a prompt nobody can answer.
- ``"required"`` — keep asking for this client even when the profile setting is
                   off.

A nullable mode string rather than a boolean: ``NULL`` has to stay
distinguishable from "explicitly false", and the codebase has no nullable
Boolean columns — mode strings (``channels.mode`` / ``auth_mode`` /
``response_mode``) are the established shape for this kind of switch.

Nullable with no server default, so every existing row stays valid and no
backfill is needed — an unset override simply inherits, which is the behaviour
those rows already had. Inspector-guarded and batch-wrapped so a fresh install
(baseline built from live ORM metadata, which already has the column) and a
pre-existing ``~/.cremind`` DB upgrade identically on **both SQLite and
PostgreSQL**.

No index: the value is read from a sender row already loaded by
``list_senders`` during recipient resolution, never queried on its own.
``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped — a purely additive column.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260812_sender_confirm"
down_revision: Union[str, Sequence[str], None] = "20260811_sender_phone"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "channel_senders"
_COLUMN = "send_confirmation"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in cols:
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.add_column(
                sa.Column(_COLUMN, sa.String(length=16), nullable=True)
            )

    # The SQLite batch rebuild copies the table, which can drop the index on
    # ``channel_id``; re-assert it. (The UNIQUE(channel_id, sender_id)
    # constraint is part of the reflected table definition and survives.)
    existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}
    if "ix_channel_senders_channel_id" not in existing_idx:
        op.create_index("ix_channel_senders_channel_id", _TABLE, ["channel_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in cols:
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)

    existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}
    if "ix_channel_senders_channel_id" not in existing_idx:
        op.create_index("ix_channel_senders_channel_id", _TABLE, ["channel_id"])
