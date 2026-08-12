"""Per-profile session-token generation counter (``profiles.token_serial``).

Revision ID: 20260813_token_serial
Revises: 20260812_sender_confirm
Create Date: 2026-08-13

Backs token revocation. Session JWTs are stateless HS256 with no ``jti`` and no
denylist, so re-minting a profile's token file left the *previous* token valid
for its full ``JWT_EXPIRATION_HOURS`` (30 days by default) — a leaked token
could not be killed except by rotating the global ``jwt_secret``, which logs out
every profile at once.

This column is the revocation state. ``BaseConfig.mint_token`` stamps it into
every token as the ``tsr`` claim and :mod:`app.auth.serial` compares it on every
decode, so ``UPDATE profiles SET token_serial = token_serial + 1`` invalidates
every token issued to that profile and nothing else.

Backfilling every existing row to ``0`` is what makes the upgrade non-breaking:
a token minted before this feature carries no ``tsr`` claim, the comparison
reads a missing claim as ``0``, and it keeps working until the profile is first
rotated. The DB-level ``server_default`` matters beyond the backfill —
``app/utils/client_storage.py`` inserts the ``__server__`` pseudo-profile with
raw SQL naming only ``(id, name, created_at, updated_at)``.

**Deliberately not batch-wrapped**, unlike the rest of this directory. A
``server_default`` that is a ``ClauseElement`` makes Alembic's
``SQLiteImpl.requires_recreate_in_batch`` return True, and the SQLite batch
recreate issues ``DROP TABLE profiles``. ``app/databases/sqlite.py`` enables
``PRAGMA foreign_keys=ON`` on every connection, and SQLite's ``DROP TABLE``
performs an implicit ``DELETE`` — which would fire ``ON DELETE CASCADE`` across
all ~12 child tables (conversations, messages, channels, configs) and destroy
the install. Neither backend needs the rebuild anyway: SQLite has supported
``ALTER TABLE … ADD COLUMN`` with a constant DEFAULT on a populated table since
forever, and ``DROP COLUMN`` since 3.35. Keeping it a plain ALTER also leaves
the UNIQUE ``ix_profiles_name`` index untouched, so there is nothing to
re-assert. ``tests/storage/test_token_serial_migration.py`` pins this by
asserting a seeded child row survives the upgrade.

Inspector-guarded on both table and column, so a fresh install (baseline rebuilt
from live ORM metadata, which already has the column) and a pre-existing
``~/.cremind`` DB upgrade identically on **both SQLite and PostgreSQL**.

No index: the column is only ever read via a whole-table snapshot
(``SELECT name, token_serial FROM profiles``) that ``app/auth/serial.py`` caches,
never filtered on. ``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped — purely additive.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260813_token_serial"
down_revision: Union[str, Sequence[str], None] = "20260812_sender_confirm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "profiles"
_COLUMN = "token_serial"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in cols:
        return

    # Plain ALTER, never op.batch_alter_table — see the module docstring.
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in cols:
        return

    # Same reasoning as upgrade(): a batch rebuild of ``profiles`` would
    # cascade-delete every child row. SQLite >= 3.35 drops columns natively.
    op.drop_column(_TABLE, _COLUMN)
