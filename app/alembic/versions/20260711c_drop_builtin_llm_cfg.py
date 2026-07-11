"""Purge the dead per-built-in-tool LLM / system_prompt config rows.

Revision ID: 20260711c_drop_builtin_llm_cfg
Revises: 20260711b_drop_mcp_agent_cfg
Create Date: 2026-07-11

Built-in tools (and skills / a2a) used to accept a per-tool LLM override
(``llm_provider`` / ``llm_model`` / ``reasoning_effort``, stored as
``tool_configs`` rows with ``scope='llm'``) written via ``cremind tools
set-llm`` / the Setup Wizard / blueprint import, plus a ``system_prompt``
(``scope='meta'``). Like the MCP-agent override purged in
``20260711b_drop_mcp_agent_cfg``, these were vestiges of a removed
inner-routing-LLM design: the built-in child LLM is resolved by
``ModelGroupManager.create_llm_for_tool`` (which ignores the ``llm`` scope),
and ``system_prompt`` was written into ``adapter._system_prompt`` but never
read. The code that wrote/read them has been removed (the ``llm`` scope no
longer exists in ``VALID_SCOPES``); this migration deletes the rows already
stored in existing installs so the dead keys stop appearing in ``get_meta``
results, blueprint exports, and the per-tool config snapshot.

Companion to ``20260711b`` (which was scoped to ``tool_id LIKE 'mcp.%'``):
this drops the ``llm`` scope entirely (any tool_id) and the built-in/skill/a2a
``system_prompt`` meta rows. The MCP rows were already removed by ``20260711b``,
so re-covering them here is a harmless no-op.

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
revision: str = "20260711c_drop_builtin_llm_cfg"
down_revision: Union[str, Sequence[str], None] = "20260711b_drop_mcp_agent_cfg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tool_configs" not in set(inspector.get_table_names()):
        return
    # The whole ``llm`` scope is retired (no code reads or writes it anymore).
    op.execute(sa.text("DELETE FROM tool_configs WHERE scope = 'llm'"))
    op.execute(
        sa.text(
            "DELETE FROM tool_configs "
            "WHERE scope = 'meta' AND \"key\" = 'system_prompt'"
        )
    )


def downgrade() -> None:
    # The rows held dead per-tool LLM / system_prompt overrides no code reads;
    # nothing to restore.
    pass
