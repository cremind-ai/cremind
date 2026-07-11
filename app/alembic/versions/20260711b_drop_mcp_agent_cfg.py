"""Purge the dead per-MCP-agent LLM / system_prompt config rows.

Revision ID: 20260711b_drop_mcp_agent_cfg
Revises: 20260711_drop_full_reasoning
Create Date: 2026-07-11

MCP servers registered via ``cremind agents`` used to accept per-agent LLM
overrides (``llm_provider`` / ``llm_model`` / ``reasoning_effort``, stored as
``tool_configs`` rows with ``scope='llm'``) and a ``system_prompt``
(``scope='meta'``). These were a vestige of a removed inner-routing-LLM design:
MCP dispatch uses the reasoning model's native function calling, so the
per-agent LLM never generated anything (``llm_provider``/``llm_model`` only
altered a cosmetic model label; ``reasoning_effort`` had no effect) and
``system_prompt`` was written into ``adapter._system_prompt`` but never read.
The code that wrote them has been removed; this migration deletes the rows
already stored in existing installs so the dead keys stop appearing in
``get_llm_params`` / ``get_meta`` results and blueprint exports.

Scoped to MCP agents (``tool_id LIKE 'mcp.%'``) on purpose: the parallel
per-built-in-tool override written via ``cremind tools set-llm`` /
``/api/tools/{tool_id}/llm`` is a separate cleanup and its rows are left
untouched.

Data-only cleanup — no schema/model change. The ``DELETE`` statements are plain
SQL valid on **both SQLite and PostgreSQL**. Guarded on ``tool_configs``
existing so a fresh install (baseline rebuilt from live metadata) is a harmless
no-op.

``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260711b_drop_mcp_agent_cfg"
down_revision: Union[str, Sequence[str], None] = "20260711_drop_full_reasoning"
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
            "WHERE scope = 'llm' "
            "AND \"key\" IN ('llm_provider', 'llm_model', 'reasoning_effort') "
            "AND tool_id LIKE 'mcp.%'"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM tool_configs "
            "WHERE scope = 'meta' AND \"key\" = 'system_prompt' "
            "AND tool_id LIKE 'mcp.%'"
        )
    )


def downgrade() -> None:
    # The rows held dead per-agent LLM overrides no code reads; nothing to restore.
    pass
