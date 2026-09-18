"""Shared helpers for synchronous storage modules.

Every sync storage module obtains its engine from the active
:class:`DatabaseProvider` and runs queries through SQLAlchemy Core. Two
helpers live here so each module doesn't reinvent them:

- :class:`SyncStorageBase` — base class that resolves the active engine on
  demand. Subclasses call ``self._engine`` to get a `sqlalchemy.Engine`.
- :func:`dialect_upsert` — picks the right ``insert()`` constructor (SQLite
  vs PostgreSQL) and renders an ``ON CONFLICT DO UPDATE`` statement. The
  reflected table it needs is cached per engine; see
  :func:`clear_reflection_cache` for when that is dropped.

Why route the sync path through SQLAlchemy Core rather than the raw DB-API:

  1. The exact same Python code runs against both backends — application
     logic doesn't branch on dialect.
  2. SQLAlchemy generates parameter placeholders in the right style for each
     driver (``?`` for sqlite3, ``%s`` for psycopg) so we don't have to
     hand-template SQL strings.
  3. Connection acquisition is hidden behind ``engine.begin()`` — same
     transactional semantics on both backends.
"""

from __future__ import annotations

import weakref
from typing import Any

from sqlalchemy import Engine, MetaData, Table

from app.databases import DatabaseProvider, get_database_provider


class SyncStorageBase:
    """Shared resolver for the active sync engine.

    Subclasses can either be constructed with an explicit provider (used by
    tests and the wizard's hot-swap path) or rely on the global singleton
    via :func:`app.databases.get_database_provider`.
    """

    def __init__(self, provider: DatabaseProvider | None = None):
        self._provider_override = provider

    @property
    def provider(self) -> DatabaseProvider:
        return self._provider_override or get_database_provider()

    @property
    def _engine(self) -> Engine:
        return self.provider.sync_engine()


# Reflected ``Table`` objects, keyed by the engine that produced them.
#
# Reflection is expensive — ``autoload_with`` issues a fistful of catalog
# queries per call — and every config write goes through ``dialect_upsert``,
# so re-reflecting per key turned a profile-creation request into ~70s of
# blocking round-trips. The reflected table only depends on (engine, name),
# so cache it.
#
# Keyed on the ``Engine`` object rather than the table name alone because the
# Setup Wizard hot-swaps providers mid-process (``set_database_provider`` ->
# ``invalidate_storage_singletons`` -> new provider): a name-keyed cache would
# hand SQLite-shaped tables to a Postgres engine. Weak keys let an abandoned
# engine's entry die with it.
_TABLE_CACHE: "weakref.WeakKeyDictionary[Engine, dict[str, Table]]" = weakref.WeakKeyDictionary()


def clear_reflection_cache() -> None:
    """Forget every cached ``Table``.

    Called from :func:`app.storage.invalidate_storage_singletons`. A cached
    table that predates a migration would render an ``INSERT`` missing the
    new column — silent data loss — so the cache is dropped whenever the
    storage layer is rebuilt rather than trusting boot ordering.
    """
    _TABLE_CACHE.clear()


def _reflected_table(engine: Engine, table_name: str) -> Table:
    """Reflect ``table_name`` off ``engine``, memoised per engine.

    A reflected table is only ever read here, so sharing one across threads
    is safe; two threads racing to reflect the same name simply produce
    equivalent objects and the loser's copy is discarded.
    """
    tables = _TABLE_CACHE.get(engine)
    if tables is None:
        tables = {}
        _TABLE_CACHE[engine] = tables
    table = tables.get(table_name)
    if table is None:
        table = Table(table_name, MetaData(), autoload_with=engine)
        tables[table_name] = table
    return table


def dialect_upsert(
    engine: Engine,
    table_name: str,
    values: dict[str, Any],
    conflict_keys: list[str],
    update_fields: list[str],
):
    """Build a dialect-aware ``INSERT ... ON CONFLICT DO UPDATE`` statement.

    Both SQLite and Postgres expose ``ON CONFLICT`` but the constructor
    lives in different sub-modules; we pick by inspecting the engine's
    dialect name.
    """
    dialect_name = engine.dialect.name
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    elif dialect_name in ("postgresql", "postgres"):
        from sqlalchemy.dialects.postgresql import insert as _insert
    else:
        raise NotImplementedError(f"Upsert not implemented for dialect {dialect_name!r}")

    table = _reflected_table(engine, table_name)
    stmt = _insert(table).values(**values)
    update_dict = {field: getattr(stmt.excluded, field) for field in update_fields}
    return stmt.on_conflict_do_update(index_elements=conflict_keys, set_=update_dict)
