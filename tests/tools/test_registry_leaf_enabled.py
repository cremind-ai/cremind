"""Unit tests for per-sub-tool ("leaf") enable/disable.

Built-in tool groups and MCP servers each expose multiple callable leaves
(``leaf_function_specs`` → ``<tool_id>__<leaf>``). This covers the per-profile
opt-out store (``tool_configs`` ``scope="leaf"``) and the registry helpers the
reasoning agent and Settings API consume:

- leaves are enabled by default (only disabled leaves are persisted);
- disabling a leaf persists and is reflected in ``leaves_for_profile`` /
  ``disabled_leaves_by_tool``; re-enabling removes the row;
- locked tools' leaves CAN be disabled (the lock only protects the group),
  while hidden system tools are refused;
- a disconnected MCP server lists no leaves but keeps its persisted choices;
- leaf rows cascade away when the tool is deleted.

Mirrors the fixture/seed pattern in ``test_registry_skill_enabled.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, AsyncGenerator, List, Optional

import pytest
from a2a.server.models import Base
from sqlalchemy import text

import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.tool_storage import SCOPE_LEAF, ToolStorage
from app.tools.base import FunctionSpec, Tool, ToolSkill, ToolType
from app.tools.config_manager import ToolConfigManager
from app.tools.registry import ToolRegistry


class _FakeGroup(Tool):
    """Minimal multi-leaf tool for registry tests.

    ``skills`` is the listing source the registry merges with the disabled set
    (statically for built-ins, live for MCP). ``connection_error`` mirrors the
    MCP stub attribute used to flag a disconnected server.
    """

    tool_type = ToolType.BUILTIN

    def __init__(
        self,
        name: str,
        leaf_names: List[str],
        *,
        hidden: bool = False,
        locked: bool = False,
        connection_error: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._name = name
        self._leaf_names = leaf_names
        self.hidden = hidden
        self.locked = locked
        self._connection_error = connection_error

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} group"

    @property
    def connection_error(self) -> Optional[str]:
        return self._connection_error

    @property
    def skills(self) -> List[ToolSkill]:
        return [
            ToolSkill(id=n, name=n, description=f"{n} description")
            for n in self._leaf_names
        ]

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


def _register(reg: ToolRegistry, tool: _FakeGroup) -> str:
    return reg.register_builtin(tool)


def test_leaves_enabled_by_default(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    tid = _register(reg, _FakeGroup("system_file", ["read_file", "write_file"]))

    payload = reg.leaves_for_profile("admin", tid)
    assert payload["supports_leaf_toggle"] is True
    assert payload["disconnected"] is False
    assert {leaf["leaf_name"]: leaf["enabled"] for leaf in payload["leaves"]} == {
        "read_file": True,
        "write_file": True,
    }
    # No rows persisted while everything is at its default.
    assert reg.disabled_leaves_by_tool("admin") == {}


def test_single_leaf_group_not_toggleable(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    tid = _register(reg, _FakeGroup("web_search", ["web_search"]))

    payload = reg.leaves_for_profile("admin", tid)
    assert payload["supports_leaf_toggle"] is False


def test_disable_leaf_persists_and_is_reported(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    tid = _register(reg, _FakeGroup("system_file", ["read_file", "write_file"]))

    reg.set_profile_tool_leaf_enabled("admin", tid, "write_file", False)

    payload = reg.leaves_for_profile("admin", tid)
    assert {leaf["leaf_name"]: leaf["enabled"] for leaf in payload["leaves"]} == {
        "read_file": True,
        "write_file": False,
    }
    assert reg.disabled_leaves_by_tool("admin") == {tid: {"write_file"}}


def test_disabled_leaf_excluded_from_dispatch(tmp_path: Path) -> None:
    """The reasoning agent drops disabled leaves by ``leaf_name`` — assert the
    exact predicate keeps siblings and removes the disabled one."""
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    tid = _register(reg, _FakeGroup("system_file", ["read_file", "write_file"]))
    reg.set_profile_tool_leaf_enabled("admin", tid, "write_file", False)

    disabled = reg.disabled_leaves_by_tool("admin").get(tid, set())
    specs = [
        FunctionSpec(name=f"{tid}__{n}", leaf_name=n, schema={})
        for n in ("read_file", "write_file")
    ]
    kept = [fs.leaf_name for fs in specs if fs.leaf_name not in disabled]
    assert kept == ["read_file"]


def test_reenable_leaf_removes_row(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    tid = _register(reg, _FakeGroup("system_file", ["read_file", "write_file"]))

    reg.set_profile_tool_leaf_enabled("admin", tid, "write_file", False)
    reg.set_profile_tool_leaf_enabled("admin", tid, "write_file", True)

    assert reg.disabled_leaves_by_tool("admin") == {}
    # The opt-out row is deleted, not just flipped.
    with reg.storage._engine.connect() as conn:  # noqa: SLF001
        count = conn.execute(
            text("SELECT COUNT(*) FROM tool_configs WHERE scope = :s"),
            {"s": SCOPE_LEAF},
        ).scalar()
    assert count == 0


def test_locked_tool_leaf_can_be_disabled(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    tid = _register(
        reg, _FakeGroup("system_file", ["read_file", "overwrite_file"], locked=True)
    )

    # The lock protects the group's presence, not each capability.
    reg.set_profile_tool_leaf_enabled("admin", tid, "overwrite_file", False)
    assert reg.disabled_leaves_by_tool("admin") == {tid: {"overwrite_file"}}


def test_hidden_tool_leaf_refused(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    tid = _register(
        reg, _FakeGroup("system_hidden", ["a", "b"], hidden=True)
    )

    with pytest.raises(ValueError):
        reg.set_profile_tool_leaf_enabled("admin", tid, "a", False)


def test_disconnected_mcp_lists_empty_but_keeps_choices(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    tool = _FakeGroup("remote_mcp", ["alpha", "beta"])
    tid = _register(reg, tool)

    reg.set_profile_tool_leaf_enabled("admin", tid, "beta", False)

    # Simulate the server going down: skills vanish, connection_error set.
    tool._leaf_names = []
    tool._connection_error = "connection refused"

    payload = reg.leaves_for_profile("admin", tid)
    assert payload["disconnected"] is True
    assert payload["leaves"] == []
    assert payload["supports_leaf_toggle"] is False
    # The persisted opt-out survives the disconnect (re-applies on reconnect).
    assert reg.disabled_leaves_by_tool("admin") == {tid: {"beta"}}


def test_delete_tool_cascades_leaf_rows(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    tid = _register(reg, _FakeGroup("system_file", ["read_file", "write_file"]))
    reg.set_profile_tool_leaf_enabled("admin", tid, "write_file", False)

    reg.storage.delete_tool(tid)

    with reg.storage._engine.connect() as conn:  # noqa: SLF001
        count = conn.execute(
            text("SELECT COUNT(*) FROM tool_configs WHERE tool_id = :t AND scope = :s"),
            {"t": tid, "s": SCOPE_LEAF},
        ).scalar()
    assert count == 0


def test_leaf_scope_accepted_by_storage(tmp_path: Path) -> None:
    """Guards the ``VALID_SCOPES`` edit — a leaf-scoped write must not raise."""
    reg = _make_registry(tmp_path)
    _seed_profile(reg.storage, "admin")
    tid = _register(reg, _FakeGroup("system_file", ["read_file", "write_file"]))

    # Should not raise ValueError("Invalid tool_config scope 'leaf'").
    reg.storage.set_config(
        profile="admin", tool_id=tid, scope=SCOPE_LEAF, key="write_file", value="false",
    )
    assert reg.config.get_disabled_leaves(tid, "admin") == {"write_file"}
