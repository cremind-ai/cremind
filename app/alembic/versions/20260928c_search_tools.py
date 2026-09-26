"""Per-conversation search-tool selection.

Revision ID: 20260928c_search_tools
Revises: 20260928b_document_tables
Create Date: 2026-09-28

The chat composer lets a person choose which of the four search sources the
agent may use in a conversation or a group room (see
:mod:`app.agent.search_tools`). Five additive columns carry it:

``conversations.search_tools`` (JSON, nullable)
    The stored selection: ``NULL`` = the default (every source), ``[]`` =
    none, a list = those ids in priority order.
``conversations.search_tools_version`` (INTEGER NOT NULL DEFAULT '0')
    The compare-and-set counter an edit must name; a stale edit gets a 409.
``conversations.search_cache_baseline`` (JSON, nullable)
    What the last main-model request actually sent for search, recorded at
    the request. Drives the prompt-cache warning and "saved for the next
    response". Group seats keep theirs here too, on their seat conversation.
``group_chats.search_tools`` / ``group_chats.search_tools_version``
    The room's shared selection and its counter.

Every existing row reads as the default (``NULL``, version 0): nothing about a
conversation's behaviour changes until someone edits its selection.

**Plain ``op.add_column``, never a batch rebuild.** A SQLite rebuild of
``conversations`` or ``group_chats`` drops and re-creates the table, and with
foreign keys on that cascade-deletes every message, seat and timeline row
hanging off it. ``ALTER TABLE … ADD COLUMN`` is enough — the version default is
the STRING ``"0"`` precisely so SQLite can add the NOT NULL column in place
(an expression default is what forces Alembic into a rebuild).

Inspector-guarded: the baseline revision builds ``conversations`` from the LIVE
ORM metadata, so a fresh install already has these columns, and minimal test
schemas may lack either table. ``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260928c_search_tools"
down_revision: Union[str, Sequence[str], None] = "20260928b_document_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> dict[str, list[sa.Column]]:
    # Fresh Column objects per call: a Column can belong to only one table.
    return {
        "conversations": [
            sa.Column("search_tools", sa.JSON(), nullable=True),
            sa.Column("search_tools_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("search_cache_baseline", sa.JSON(), nullable=True),
        ],
        "group_chats": [
            sa.Column("search_tools", sa.JSON(), nullable=True),
            sa.Column("search_tools_version", sa.Integer(), nullable=False, server_default="0"),
        ],
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, columns in _columns().items():
        if table not in tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for column in columns:
            if column.name not in present:
                op.add_column(table, column)


def downgrade() -> None:
    # SQLite >= 3.35 drops a column in place (no rebuild, so no cascade). The
    # rebuild fallback is deliberately NOT used here — see the docstring.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, columns in _columns().items():
        if table not in tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for column in reversed(columns):
            if column.name in present:
                bind.execute(sa.text(
                    f"ALTER TABLE {table} DROP COLUMN {column.name}"
                ))
