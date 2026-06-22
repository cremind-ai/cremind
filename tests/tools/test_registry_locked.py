"""Unit tests for the ``locked`` built-in tool flag (ToolConfig.locked).

A ``locked`` built-in stays VISIBLE in the Settings UI but cannot be disabled:

- ``set_profile_tool_enabled`` refuses a disable (but allows an idempotent
  re-enable).
- ``tools_for_profile`` always exposes it, so a stale ``profile_tools`` row
  (e.g. written before the tool became locked) can't suppress it.
- ``visible_for_profile`` reports it as enabled regardless of that stale row.

The behaviour is exercised with a synthetic built-in tool so the tests don't
depend on which shipped tools happen to carry the flag. A separate test pins
the three tools converted from ``hidden`` to ``locked``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
from a2a.server.models import Base
from sqlalchemy import text

import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.tool_storage import ToolStorage
from app.tools.base import Tool, ToolType
from app.tools.config_manager import ToolConfigManager
from app.tools.registry import ToolRegistry


class _FakeBuiltin(Tool):
    """Minimal built-in tool for registry tests."""

    tool_type = ToolType.BUILTIN

    def __init__(self, name: str, *, locked: bool = False) -> None:
        super().__init__()
        self._name = name
        self.locked = locked

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} (test tool)"

    async def execute(self, **_: Any) -> AsyncGenerator[Any, None]:  # pragma: no cover
        if False:
            yield None


def _make_registry(tmp_path: Path) -> ToolRegistry:
    provider = SqliteDatabaseProvider(str(tmp_path / "tools.db"))
    Base.metadata.create_all(bind=provider.sync_engine())
    storage = ToolStorage(provider)
    return ToolRegistry(storage, ToolConfigManager(storage))


def _seed_profile(storage: ToolStorage, name: str) -> None:
    now = time.time() * 1000
    with storage._engine.begin() as conn:  # noqa: SLF001 — test seeding
        conn.execute(
            text(
                "INSERT INTO profiles (id, name, created_at, updated_at, skill_mode) "
                "VALUES (:id, :name, :c, :u, 'manual')"
            ),
            {"id": name, "name": name, "c": now, "u": now},
        )


def _register(reg: ToolRegistry):
    locked_id = reg.register_builtin(_FakeBuiltin("locked_tool", locked=True))
    control_id = reg.register_builtin(_FakeBuiltin("control_tool", locked=False))
    return locked_id, control_id


def test_locked_tool_cannot_be_disabled(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    locked_id, control_id = _register(reg)

    with pytest.raises(ValueError):
        reg.set_profile_tool_enabled("admin", locked_id, False)

    # A normal (unlocked) built-in still disables fine.
    reg.set_profile_tool_enabled("admin", control_id, False)
    assert reg.storage.get_profile_tool_enabled("admin", control_id) is False


def test_locked_tool_allows_idempotent_reenable(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    locked_id, _ = _register(reg)

    # enable=True is a no-op for an always-on tool and must not raise.
    reg.set_profile_tool_enabled("admin", locked_id, True)


def test_locked_tool_always_exposed_despite_stale_row(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    locked_id, control_id = _register(reg)

    # Simulate a pre-flag disabled row by writing straight to storage,
    # bypassing the guard in set_profile_tool_enabled.
    reg.storage.set_profile_tool("admin", locked_id, False)
    reg.storage.set_profile_tool("admin", control_id, False)

    exposed = {t.tool_id for t in reg.tools_for_profile("admin")}
    assert locked_id in exposed       # locked: stale row can't suppress it
    assert control_id not in exposed  # control: stale row honored


def test_locked_tool_visible_row_reports_enabled(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    locked_id, control_id = _register(reg)

    reg.storage.set_profile_tool("admin", locked_id, False)
    reg.storage.set_profile_tool("admin", control_id, False)

    rows = {r["tool_id"]: r for r in reg.visible_for_profile("admin")}
    assert rows[locked_id]["enabled"] is True    # forced on
    assert rows[control_id]["enabled"] is False  # honored


def test_converted_shipped_tools_carry_locked_flag() -> None:
    from app.tools.builtin import get_builtin_tool_config

    # These three were converted from hidden -> locked: visible but undisableable.
    for name in ("exec_shell", "system_file", "documentation_search"):
        tool_cfg = get_builtin_tool_config(name).get("tool", {})
        assert tool_cfg.get("locked") is True, name
        assert tool_cfg.get("hidden") in (None, False), name

    # A normal visible tool stays unlocked.
    web = get_builtin_tool_config("web_search").get("tool", {})
    assert not web.get("locked", False)
