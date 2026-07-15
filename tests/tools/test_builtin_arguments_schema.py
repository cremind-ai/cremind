"""Regression: register_builtin_tools must persist each built-in's declared
``TOOL_CONFIG["arguments"]`` as the group's ``arguments_schema``.

Previously the ``BuiltInToolGroup(...)`` construction omitted
``arguments_schema=tool_info.get("arguments")``, so every live built-in reported
``arguments_schema = None`` — ``exec_shell`` / ``google_places`` silently lost
their argument schema and ``cremind tools get-args`` showed nothing.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from a2a.server.models import Base

import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from sqlalchemy import text

from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.tool_storage import ToolStorage
from app.tools.config_manager import ToolConfigManager
from app.tools.registry import ToolRegistry


def _seed_profile(storage: ToolStorage, name: str) -> None:
    now = time.time() * 1000
    with storage._engine.begin() as conn:  # noqa: SLF001 — test seeding
        conn.execute(
            text(
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                "VALUES (:id, :name, :c, :u)"
            ),
            {"id": name, "name": name, "c": now, "u": now},
        )


def _register_all(tmp_path: Path) -> ToolRegistry:
    from app.tools.builtin import register_builtin_tools

    provider = SqliteDatabaseProvider(str(tmp_path / "tools.db"))
    Base.metadata.create_all(bind=provider.sync_engine())
    storage = ToolStorage(provider)
    reg = ToolRegistry(storage, ToolConfigManager(storage))
    _seed_profile(storage, "admin")

    asyncio.run(register_builtin_tools(
        registry=reg,
        config_manager=reg.config,
        llm_factory=lambda *a, **k: None,
        setup_profile="admin",
    ))
    return reg


def test_arguments_schema_wired_for_exec_shell(tmp_path: Path) -> None:
    from app.tools.builtin import exec_shell

    reg = _register_all(tmp_path)
    tool = reg.get("exec_shell")
    assert tool is not None
    # The live group carries the module's declared arguments schema. This is
    # the value GET /api/tools[/{id}] returns and `cremind tools get-args`
    # reads — the actual regression (it used to be None for every built-in).
    assert tool.arguments_schema == exec_shell.TOOL_CONFIG["arguments"]
    # It is now persisted too (used to be NULL). The tools-table column is
    # JSON-serialized by storage, so decode before comparing.
    row = reg.storage.get_tool("exec_shell")
    assert row["arguments_schema"] is not None
    persisted = row["arguments_schema"]
    if isinstance(persisted, str):
        persisted = json.loads(persisted)
    assert persisted == exec_shell.TOOL_CONFIG["arguments"]


def test_arguments_schema_none_for_argumentless_tool(tmp_path: Path) -> None:
    reg = _register_all(tmp_path)
    # current_time declares no ``arguments`` -> schema stays None.
    tool = reg.get("current_time")
    assert tool is not None
    assert tool.arguments_schema is None
