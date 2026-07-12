"""Unit tests for the per-tool ``default`` flag (ToolConfig.default).

A built-in may declare ``TOOL_CONFIG["default"] = False`` to start DISABLED in
the Setup Wizard and, absent a ``profile_tools`` row, at runtime too. Skills
start OFF both in the wizard (``visible_for_profile`` reports
``default_enabled: False``) and at runtime (the ``_default_enabled`` fallback is
off), so a fresh profile / the wizard / a factory reset all start skills off —
the owner opts each one in.

Fixtures mirror ``test_registry_locked.py`` / ``test_registry_skill_enabled.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, AsyncGenerator

from a2a.server.models import Base
from sqlalchemy import text

import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.tool_storage import ToolStorage
from app.tools.base import Tool, ToolType
from app.tools.config_manager import ToolConfigManager
from app.tools.ids import slugify
from app.tools.registry import ToolRegistry


class _FakeBuiltin(Tool):
    """Minimal built-in tool for registry tests."""

    tool_type = ToolType.BUILTIN

    def __init__(
        self, name: str, *, locked: bool = False, default_enabled: bool = True
    ) -> None:
        super().__init__()
        self._name = name
        self.locked = locked
        self.default_enabled = default_enabled

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} (test tool)"

    async def execute(self, **_: Any) -> AsyncGenerator[Any, None]:  # pragma: no cover
        if False:
            yield None


class _FakeSkill(Tool):
    """Minimal skill tool for registry tests."""

    tool_type = ToolType.SKILL

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} (test skill)"

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
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                "VALUES (:id, :name, :c, :u)"
            ),
            {"id": name, "name": name, "c": now, "u": now},
        )


def test_builtin_default_false_off_by_default(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    off_id = reg.register_builtin(_FakeBuiltin("opt_in_tool", default_enabled=False))
    on_id = reg.register_builtin(_FakeBuiltin("normal_tool", default_enabled=True))

    # No profile_tools row -> the per-tool default decides.
    exposed = {t.tool_id for t in reg.tools_for_profile("admin")}
    assert off_id not in exposed   # default False -> not exposed
    assert on_id in exposed        # default True -> exposed

    rows = {r["tool_id"]: r for r in reg.visible_for_profile("admin")}
    assert rows[off_id]["enabled"] is False
    assert rows[off_id]["default_enabled"] is False   # wizard shows it off
    assert rows[on_id]["default_enabled"] is True


def test_builtin_default_false_can_be_opted_in(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    off_id = reg.register_builtin(_FakeBuiltin("opt_in_tool", default_enabled=False))

    reg.set_profile_tool_enabled("admin", off_id, True)

    exposed = {t.tool_id for t in reg.tools_for_profile("admin")}
    assert off_id in exposed
    # The wizard default is unchanged by a per-profile override.
    rows = {r["tool_id"]: r for r in reg.visible_for_profile("admin")}
    assert rows[off_id]["enabled"] is True
    assert rows[off_id]["default_enabled"] is False


def test_locked_overrides_default_false(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    # A contradictory combo (locked wins): always on, wizard toggle forced on.
    tool_id = reg.register_builtin(
        _FakeBuiltin("locked_tool", locked=True, default_enabled=False)
    )

    exposed = {t.tool_id for t in reg.tools_for_profile("admin")}
    assert tool_id in exposed
    rows = {r["tool_id"]: r for r in reg.visible_for_profile("admin")}
    assert rows[tool_id]["default_enabled"] is True


def test_skill_default_off_wizard_and_runtime(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    skill_id = reg.register_skill_sync(
        _FakeSkill("weather"), source="admin/weather", owner_profile="admin",
    )

    # Wizard-facing default: off (admin opts in).
    rows = {r["tool_id"]: r for r in reg.visible_for_profile("admin")}
    assert rows[skill_id]["default_enabled"] is False
    assert rows[skill_id]["enabled"] is False
    # Runtime fallback: also off for the owner absent a profile_tools row.
    exposed = {t.tool_id for t in reg.tools_for_profile("admin")}
    assert skill_id not in exposed

    # Opting in via a profile_tools row exposes it.
    reg.set_profile_tool_enabled("admin", skill_id, True)
    assert skill_id in {t.tool_id for t in reg.tools_for_profile("admin")}


def test_catalog_honors_default_flag(monkeypatch) -> None:
    from app.tools.builtin import list_builtin_tool_catalog
    from app.tools.builtin import weather

    weather_id = slugify(getattr(weather, "SERVER_NAME", "weather"))

    # Baseline: weather ships with ``default: False`` -> surfaced by the
    # catalog but off; at least one tool that omits ``default`` stays on.
    baseline = {r["tool_id"]: r for r in list_builtin_tool_catalog()}
    assert baseline[weather_id]["enabled"] is False
    assert baseline[weather_id]["default_enabled"] is False
    assert any(r["default_enabled"] is True for r in baseline.values()), \
        "expected at least one default-on tool"

    # Flipping the declaration drives both wizard fields on.
    monkeypatch.setitem(weather.TOOL_CONFIG, "default", True)
    patched = {r["tool_id"]: r for r in list_builtin_tool_catalog()}
    assert patched[weather_id]["enabled"] is True
    assert patched[weather_id]["default_enabled"] is True
