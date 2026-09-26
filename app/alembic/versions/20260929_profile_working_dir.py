"""Per-profile User Working Directory (``profiles.working_dir``).

Revision ID: 20260929_profile_working_dir
Revises: 20260928c_search_tools
Create Date: 2026-09-29

The User Working Directory used to be ONE server-wide folder
(``server_config.user_working_dir``, ``~/Documents`` by default), so every
profile's file panel, tools, terminals and Documentation search index shared
it. Each profile now has its own (see :mod:`app.config.working_dirs`):
``profiles.working_dir`` holds a folder the admin chose, and NULL means the
default ``<workspaces root>/<profile name>``.

**The upgrade keeps the admin where it was.** The server-wide value moves onto
the ``admin`` row — the admin's folder, its file panel and its search index are
exactly as before — and the ``server_config`` key is removed so nothing reads a
stale copy. Every other profile starts on its own default folder. Nothing on
disk is moved or deleted. When no ``admin`` row exists yet (setup never
finished) the key is left for the setup wizard to supersede.

**Plain ``op.add_column``, never a batch rebuild** — a SQLite rebuild of
``profiles`` drops the table, and with foreign keys on that cascade-deletes
every conversation, channel and config row (see ``20260813_token_serial``).

The downgrade puts the admin's folder back into ``server_config`` (its default
folder spelled out when it had none, so the older build keeps pointing at the
admin's files) and drops the column in place (SQLite >= 3.35).

Inspector-guarded on table and column: a fresh install builds ``profiles`` from
the live ORM metadata and already has the column. ``MIN_SUPPORTED_UPGRADE_FROM``
is not bumped.
"""

from __future__ import annotations

import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260929_profile_working_dir"
down_revision: Union[str, Sequence[str], None] = "20260928c_search_tools"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "profiles"
_COLUMN = "working_dir"
_KEY = "user_working_dir"
_ADMIN = "admin"


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _has_column(bind) -> bool:
    return _COLUMN in {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if _TABLE not in tables:
        return
    if not _has_column(bind):
        # Plain ALTER, never op.batch_alter_table — see the module docstring.
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))

    if "server_config" not in tables:
        return
    row = bind.execute(
        sa.text("SELECT value FROM server_config WHERE key = :key"), {"key": _KEY}
    ).fetchone()
    value = (row[0] or "").strip() if row and isinstance(row[0], str) else ""
    admin = bind.execute(
        sa.text(f"SELECT {_COLUMN} FROM {_TABLE} WHERE name = :name"), {"name": _ADMIN}
    ).fetchone()
    if admin is None:
        return
    if value and not (admin[0] or "").strip():
        bind.execute(
            sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = :value WHERE name = :name"),
            {"value": value, "name": _ADMIN},
        )
    if row is not None:
        bind.execute(sa.text("DELETE FROM server_config WHERE key = :key"), {"key": _KEY})


def _admin_default() -> str | None:
    try:
        from app.config.working_dirs import default_working_dir

        return default_working_dir(_ADMIN)
    except Exception:  # noqa: BLE001 — the older build then falls back to ~/Documents
        return None


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if _TABLE not in tables or not _has_column(bind):
        return
    if "server_config" in tables:
        admin = bind.execute(
            sa.text(f"SELECT {_COLUMN} FROM {_TABLE} WHERE name = :name"), {"name": _ADMIN}
        ).fetchone()
        if admin is not None:
            value = (admin[0] or "").strip() or _admin_default()
            present = bind.execute(
                sa.text("SELECT 1 FROM server_config WHERE key = :key"), {"key": _KEY}
            ).fetchone()
            if value and present is None:
                bind.execute(
                    sa.text(
                        "INSERT INTO server_config (key, value, is_secret, updated_at) "
                        "VALUES (:key, :value, :secret, :now)"
                    ),
                    {"key": _KEY, "value": value, "secret": False, "now": time.time() * 1000},
                )
    # In place (SQLite >= 3.35 / PostgreSQL); never a rebuild of ``profiles``.
    bind.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}"))
