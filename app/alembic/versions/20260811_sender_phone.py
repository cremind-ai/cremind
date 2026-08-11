"""Sender phone + WhatsApp lid — address channel clients by phone number.

Revision ID: 20260811_sender_phone
Revises: 20260810_event_tasks
Create Date: 2026-08-11

Two additive nullable columns on ``channel_senders``, backing the direct-send
feature (:mod:`app.channels.direct_send`), which messages individual channel
clients addressed by platform id **or** phone number:

1. ``phone`` — the contact's number in canonical digits-only form (E.164
   without the ``+``). Auto-derived where the platform exposes it (WhatsApp
   ``<digits>@s.whatsapp.net`` sender ids) and otherwise operator-supplied; it
   is what turns "here is a spreadsheet of phone numbers" into deliverable
   sender ids.

2. ``wa_lid`` — the WhatsApp linked-identity alias (``<id>@lid``) captured when
   we cold-message a number. Multi-device WhatsApp lets the same human reply
   from an opaque ``@lid`` JID; without this the reply would create a second
   sender row and split the conversation, so the inbound path adopts the
   existing row when the ``@lid`` matches.

Both are nullable with no server default, so every existing row stays valid and
no backfill is needed — the WhatsApp derivation fills historical rows lazily on
each sender's next inbound message. Inspector-guarded and batch-wrapped so a
fresh install (baseline built from live ORM metadata, which already has the
columns) and a pre-existing ``~/.cremind`` DB upgrade identically on **both
SQLite and PostgreSQL**.

No index: matching is an exact lookup over one channel's (small) sender list,
already served by the existing ``ix_channel_senders_channel_id``.
``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped — purely additive columns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260811_sender_phone"
down_revision: Union[str, Sequence[str], None] = "20260810_event_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "channel_senders"
_COLUMNS = (
    ("phone", lambda: sa.Column("phone", sa.String(length=32), nullable=True)),
    ("wa_lid", lambda: sa.Column("wa_lid", sa.String(length=64), nullable=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    missing = [factory for name, factory in _COLUMNS if name not in cols]
    if missing:
        with op.batch_alter_table(_TABLE) as batch_op:
            for factory in missing:
                batch_op.add_column(factory())

    # The SQLite batch rebuild copies the table, which can drop the index on
    # ``channel_id``; re-assert it. (The UNIQUE(channel_id, sender_id)
    # constraint is part of the reflected table definition and is preserved by
    # the copy.)
    existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}
    if "ix_channel_senders_channel_id" not in existing_idx:
        op.create_index("ix_channel_senders_channel_id", _TABLE, ["channel_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    present = [name for name, _ in _COLUMNS if name in cols]
    if present:
        with op.batch_alter_table(_TABLE) as batch_op:
            for name in present:
                batch_op.drop_column(name)

    existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}
    if "ix_channel_senders_channel_id" not in existing_idx:
        op.create_index("ix_channel_senders_channel_id", _TABLE, ["channel_id"])
