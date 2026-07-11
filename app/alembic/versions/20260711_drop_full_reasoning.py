"""Purge the dead ``full_reasoning`` per-tool LLM config rows.

Revision ID: 20260711_drop_full_reasoning
Revises: 20260705_drop_profile_skill_mode
Create Date: 2026-07-11

``full_reasoning`` was a per-tool "LLM parameter" (``tool_configs`` rows with
``scope='llm'``, ``key='full_reasoning'``) surfaced in the Setup wizard, the
Tools/Agents settings UI, and the CLI. It was a vestige of a removed
inner-routing-LLM design: the value was written and forwarded into
``adapter._full_reasoning`` but never read, so it changed no behavior. The code
that wrote it has been removed; this migration deletes the rows already stored
in existing installs so the dead key stops appearing in ``get_llm_params``
results and blueprint exports.

Data-only cleanup — no schema/model change. The ``DELETE`` is plain SQL valid on
**both SQLite and PostgreSQL**. Guarded on ``tool_configs`` existing so a fresh
install (baseline rebuilt from live metadata) is a harmless no-op.

``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260711_drop_full_reasoning"
down_revision: Union[str, Sequence[str], None] = "20260705_drop_profile_skill_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tool_configs" not in set(inspector.get_table_names()):
        return
    op.execute(
        sa.text(
            "DELETE FROM tool_configs "
            "WHERE scope = 'llm' AND \"key\" = 'full_reasoning'"
        )
    )


def downgrade() -> None:
    # The rows held a dead flag that no code reads; there is nothing to restore.
    pass
