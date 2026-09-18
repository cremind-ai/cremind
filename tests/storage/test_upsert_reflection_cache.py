"""``dialect_upsert`` must not re-reflect the schema on every write.

It used to build ``Table(name, MetaData(), autoload_with=engine)`` per call — a
full catalog reflection for each key written. Every config write in the app goes
through it (``ToolStorage.set_config``, ``set_profile_tool``, ``upsert_tool``,
``DynamicConfigStorage.set``, ``ClientStorage``), so creating a profile, which
writes several keys for each of ~30 built-in tools, spent the better part of a
minute on catalog queries alone. Against Postgres that was the 52-second gap in
the middle of the Setup Wizard.

The cache is keyed on the ``Engine``, not the table name, because the Setup
Wizard hot-swaps database providers mid-process: a name-keyed cache could hand
SQLite-shaped tables to a Postgres engine. And it is cleared explicitly on
``invalidate_storage_singletons`` so a table cached before a migration can never
render an INSERT that silently omits the new column.
"""

from __future__ import annotations

import time
from pathlib import Path

from a2a.server.models import Base
from sqlalchemy import text

import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage._sync_base import clear_reflection_cache, dialect_upsert
from app.storage.tool_storage import ToolStorage

_VALUES = {
    "profile": "javis",
    "tool_id": "exec_shell",
    "scope": "variable",
    "key": "TIMEOUT",
    "value": "120",
    "is_secret": False,
    "updated_at": 0.0,
}
_CONFLICT = ["profile", "tool_id", "scope", "key"]
_UPDATE = ["value", "is_secret", "updated_at"]


def _engine(tmp_path: Path, name: str):
    provider = SqliteDatabaseProvider(str(tmp_path / name))
    engine = provider.sync_engine()
    Base.metadata.create_all(bind=engine)
    return engine


def _upsert(engine):
    return dialect_upsert(engine, "tool_configs", _VALUES, _CONFLICT, _UPDATE)


def _table_of(stmt):
    return stmt.table


def test_one_engine_reflects_a_table_once(tmp_path: Path) -> None:
    clear_reflection_cache()
    engine = _engine(tmp_path, "a.db")

    first = _table_of(_upsert(engine))
    second = _table_of(_upsert(engine))

    assert first is second, "the reflected table should be reused, not rebuilt"


def test_each_engine_gets_its_own_table(tmp_path: Path) -> None:
    """The wizard's provider hot-swap must not inherit the old engine's schema."""
    clear_reflection_cache()
    one = _engine(tmp_path, "one.db")
    two = _engine(tmp_path, "two.db")

    assert _table_of(_upsert(one)) is not _table_of(_upsert(two))


def test_clearing_forces_a_fresh_reflection(tmp_path: Path) -> None:
    clear_reflection_cache()
    engine = _engine(tmp_path, "c.db")

    before = _table_of(_upsert(engine))
    clear_reflection_cache()
    after = _table_of(_upsert(engine))

    assert before is not after


def test_invalidating_the_storage_singletons_clears_the_cache(tmp_path: Path) -> None:
    """Wired up, not just available — a migration mid-process depends on it."""
    from app.storage import invalidate_storage_singletons

    clear_reflection_cache()
    engine = _engine(tmp_path, "d.db")

    before = _table_of(_upsert(engine))
    invalidate_storage_singletons()
    after = _table_of(_upsert(engine))

    assert before is not after


def test_the_statement_still_upserts(tmp_path: Path) -> None:
    """Caching changes how the statement is built, never what it does."""
    clear_reflection_cache()
    provider = SqliteDatabaseProvider(str(tmp_path / "e.db"))
    engine = provider.sync_engine()
    Base.metadata.create_all(bind=engine)

    # tool_configs has FKs to profiles/tools, and the provider enables SQLite's
    # foreign-key enforcement, so the parents have to exist first.
    store = ToolStorage(provider)
    now = time.time() * 1000
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                "VALUES (:id, :name, :c, :u)"
            ),
            {"id": "javis", "name": "javis", "c": now, "u": now},
        )
    store.upsert_tool(
        tool_id="exec_shell", name="exec_shell", tool_type="builtin",
        source="exec_shell", owner_profile="javis",
    )

    with engine.begin() as conn:
        conn.execute(_upsert(engine))
        conn.execute(
            dialect_upsert(
                engine, "tool_configs", {**_VALUES, "value": "300"}, _CONFLICT, _UPDATE,
            )
        )

    with engine.connect() as conn:
        rows = conn.execute(_table_of(_upsert(engine)).select()).fetchall()

    assert len(rows) == 1, "the second write must update, not insert a duplicate"
    assert dict(rows[0]._mapping)["value"] == "300"
