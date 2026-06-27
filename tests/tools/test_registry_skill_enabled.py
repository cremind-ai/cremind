"""Unit tests for honoring the per-profile ``enabled`` flag on skills.

Regression cover for the bug where a skill disabled in Settings
(``profile_tools.enabled = 0``) was still exposed to the reasoning agent and
could be loaded on request. ``tools_for_profile`` now applies the same enabled
check to owned skills that it already applied to every other tool type, while:

- skills owned by *other* profiles stay invisible (ownership filter unchanged);
- a skill with no ``profile_tools`` row stays on (SKILL default is enabled);
- ``owned_skills`` ignores the enabled flag (used for ``.env`` materialization,
  which must cover disabled skills too).

Mirrors the fixture/seed pattern in ``test_registry_locked.py``.
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
from app.tools.registry import ToolRegistry


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


def _register_skill(reg: ToolRegistry, profile: str, name: str) -> str:
    return reg.register_skill_sync(
        _FakeSkill(name), source=f"{profile}/{name}", owner_profile=profile,
    )


def test_owned_skill_enabled_by_default(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    skill_id = _register_skill(reg, "admin", "weather")

    # No profile_tools row -> SKILL default (on).
    exposed = {t.tool_id for t in reg.tools_for_profile("admin")}
    assert skill_id in exposed


def test_disabled_skill_hidden_from_agent_but_visible_in_settings(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    skill_id = _register_skill(reg, "admin", "weather")

    reg.set_profile_tool_enabled("admin", skill_id, False)

    # The bug: agent must NOT see the disabled skill...
    exposed = {t.tool_id for t in reg.tools_for_profile("admin")}
    assert skill_id not in exposed

    # ...but Settings still lists it (as off) so it can be re-enabled.
    rows = {r["tool_id"]: r for r in reg.visible_for_profile("admin")}
    assert rows[skill_id]["enabled"] is False


def test_reenabled_skill_exposed_again(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    skill_id = _register_skill(reg, "admin", "weather")

    reg.set_profile_tool_enabled("admin", skill_id, False)
    reg.set_profile_tool_enabled("admin", skill_id, True)

    exposed = {t.tool_id for t in reg.tools_for_profile("admin")}
    assert skill_id in exposed


def test_skill_isolation_across_profiles_unchanged(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    _seed_profile(reg.storage, "other")
    skill_id = _register_skill(reg, "admin", "weather")

    # A profile never sees another profile's skill, enabled or not.
    assert skill_id not in {t.tool_id for t in reg.tools_for_profile("other")}


def test_owned_skills_ignores_enabled_flag(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    _seed_profile(reg.storage, "other")
    skill_id = _register_skill(reg, "admin", "weather")
    other_skill_id = _register_skill(reg, "other", "calendar")

    reg.set_profile_tool_enabled("admin", skill_id, False)

    # Disabled owned skill still listed (env materialization needs it)...
    owned = {t.tool_id for t in reg.owned_skills("admin")}
    assert skill_id in owned
    # ...and ownership is still scoped to the profile.
    assert other_skill_id not in owned
