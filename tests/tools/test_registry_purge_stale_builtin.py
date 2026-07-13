"""ToolRegistry.purge_stale_builtin_rows: drop persisted built-in rows for
modules that no longer ship, cascading to their per-profile children, while
leaving current built-ins, skills, and MCP rows untouched.

Regression context: built-in registration is upsert-only, so a discarded
prototype (e.g. a ``tool_config`` module that never landed) left a stale row in
the ``tools`` table forever, stranding its ``profile_tools`` / ``tool_configs``
children.
"""

from __future__ import annotations

import time
from pathlib import Path

from a2a.server.models import Base

import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from sqlalchemy import text

from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.tool_storage import ToolStorage
from app.tools.base import ToolType
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


def _make_registry(tmp_path: Path) -> ToolRegistry:
    provider = SqliteDatabaseProvider(str(tmp_path / "tools.db"))
    Base.metadata.create_all(bind=provider.sync_engine())
    storage = ToolStorage(provider)
    return ToolRegistry(storage, ToolConfigManager(storage))


def _seed_builtin_row(storage: ToolStorage, tool_id: str, source: str) -> None:
    storage.upsert_tool(
        tool_id=tool_id, name=tool_id, tool_type=ToolType.BUILTIN.value,
        source=source, description=tool_id, arguments_schema=None,
    )


def _seed_profile_tool(storage: ToolStorage, profile: str, tool_id: str) -> None:
    now = time.time() * 1000
    with storage._engine.begin() as conn:  # noqa: SLF001 — test seeding
        conn.execute(
            text(
                "INSERT INTO profile_tools (profile, tool_id, enabled, added_at) "
                "VALUES (:p, :t, 1, :a)"
            ),
            {"p": profile, "t": tool_id, "a": now},
        )


def _child_rows(storage: ToolStorage, tool_id: str) -> tuple[int, int]:
    with storage._engine.connect() as conn:  # noqa: SLF001 — test read
        pt = conn.execute(
            text("SELECT COUNT(*) FROM profile_tools WHERE tool_id = :t"),
            {"t": tool_id},
        ).scalar_one()
        tc = conn.execute(
            text("SELECT COUNT(*) FROM tool_configs WHERE tool_id = :t"),
            {"t": tool_id},
        ).scalar_one()
    return pt, tc


def test_purge_removes_ghost_row_and_cascades(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    storage = reg.storage
    _seed_profile(storage, "admin")

    # A stale built-in from a removed module, with per-profile children.
    _seed_builtin_row(storage, "ghost_tool", source="ghost_tool")
    _seed_profile_tool(storage, "admin", "ghost_tool")             # profile_tools row
    reg.config.set_variable("ghost_tool", "admin", "K", "v")       # tool_configs row
    assert _child_rows(storage, "ghost_tool") == (1, 1)

    # A current built-in (source IS a shipped module) must survive.
    _seed_builtin_row(storage, "weather_id", source="weather")

    removed = reg.purge_stale_builtin_rows(["weather", "exec_shell"])

    assert removed == 1
    assert storage.get_tool("ghost_tool") is None
    assert _child_rows(storage, "ghost_tool") == (0, 0)   # FK cascade cleaned them
    assert storage.get_tool("weather_id") is not None     # shipped module kept


def test_purge_keeps_in_memory_registered_builtin(tmp_path: Path) -> None:
    """A row whose source isn't in the valid list but whose tool_id IS
    registered in memory (belt-and-braces) is kept."""
    reg = _make_registry(tmp_path)
    storage = reg.storage
    _seed_builtin_row(storage, "live_tool", source="live_tool")
    reg._tools["live_tool"] = object()  # simulate an in-memory registration

    removed = reg.purge_stale_builtin_rows(["something_else"])

    assert removed == 0
    assert storage.get_tool("live_tool") is not None


def test_purge_ignores_skills_and_mcp(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    storage = reg.storage
    storage.upsert_tool(
        tool_id="skill_x", name="skill_x", tool_type=ToolType.SKILL.value,
        source="/skills/x", description="x", arguments_schema=None,
    )
    storage.upsert_tool(
        tool_id="mcp_y", name="mcp_y", tool_type=ToolType.MCP.value,
        source="mcp://y", description="y", arguments_schema=None,
    )

    removed = reg.purge_stale_builtin_rows([])  # no valid built-in sources

    assert removed == 0
    assert storage.get_tool("skill_x") is not None
    assert storage.get_tool("mcp_y") is not None
