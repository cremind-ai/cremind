"""Unit tests for ToolStorage.delete_all_configs.

Backs the "Reset to Default" fix: resetting a built-in skill must wipe its
per-profile saved config (Skill Variables, arguments, ...), not just restore
the on-disk files. The config rows live in ``tool_configs`` keyed by
``(profile, tool_id, scope, key)``; the tool_id is unchanged across a reset, so
the rows must be deleted explicitly.
"""

from __future__ import annotations

import time
from pathlib import Path

from a2a.server.models import Base
from sqlalchemy import text

import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.tool_storage import SCOPE_ARG, SCOPE_META, SCOPE_VARIABLE, ToolStorage


def _make_store(tmp_path: Path) -> ToolStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "tools.db"))
    # tool_configs has FKs to tools/profiles; create the full schema so SQLite's
    # foreign-key enforcement (enabled by the provider) has its parent tables.
    Base.metadata.create_all(bind=provider.sync_engine())
    return ToolStorage(provider)


def _seed_profile(store: ToolStorage, name: str) -> None:
    now = time.time() * 1000
    with store._engine.begin() as conn:  # noqa: SLF001 — test seeding
        conn.execute(
            text(
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                "VALUES (:id, :name, :c, :u)"
            ),
            {"id": name, "name": name, "c": now, "u": now},
        )


def _seed_tool(store: ToolStorage, tool_id: str, owner: str) -> None:
    store.upsert_tool(
        tool_id=tool_id, name=tool_id, tool_type="skill",
        source=tool_id, owner_profile=owner,
    )


def test_delete_all_configs_removes_every_scope(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_profile(store, "admin")
    tid, other = "admin__gmail", "admin__other"
    _seed_tool(store, tid, "admin")
    _seed_tool(store, other, "admin")

    store.set_config(profile="admin", tool_id=tid, scope=SCOPE_VARIABLE,
                     key="GMAIL_TOKEN", value="secret", is_secret=True)
    store.set_config(profile="admin", tool_id=tid, scope=SCOPE_VARIABLE,
                     key="GMAIL_USER", value="me@example.com")
    store.set_config(profile="admin", tool_id=tid, scope=SCOPE_ARG, key="foo", value="bar")
    store.set_config(profile="admin", tool_id=tid, scope=SCOPE_META,
                     key="description", value="custom")
    # A different skill's config must survive.
    store.set_config(profile="admin", tool_id=other, scope=SCOPE_VARIABLE, key="K", value="v")

    removed = store.delete_all_configs(profile="admin", tool_id=tid)

    assert removed == 4
    assert store.get_all_scopes(profile="admin", tool_id=tid, include_secrets=True) == {}
    assert store.get_scope(
        profile="admin", tool_id=other, scope=SCOPE_VARIABLE, include_secrets=True,
    ) == {"K": "v"}


def test_delete_all_configs_is_scoped_to_profile(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_profile(store, "admin")
    _seed_profile(store, "other")
    tid = "admin__gmail"
    _seed_tool(store, tid, "admin")

    store.set_config(profile="admin", tool_id=tid, scope=SCOPE_VARIABLE, key="K", value="a")
    store.set_config(profile="other", tool_id=tid, scope=SCOPE_VARIABLE, key="K", value="b")

    removed = store.delete_all_configs(profile="admin", tool_id=tid)

    assert removed == 1
    assert store.get_scope(profile="other", tool_id=tid, scope=SCOPE_VARIABLE) == {"K": "b"}


def test_delete_all_configs_noop_when_empty(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.delete_all_configs(profile="admin", tool_id="admin__nothing") == 0
