"""Shared helpers for synchronous storage modules.

Every sync storage module obtains its engine from the active
:class:`DatabaseProvider` and runs queries through SQLAlchemy Core. Two
helpers live here so each module doesn't reinvent them:

- :class:`SyncStorageBase` — base class that resolves the active engine on
  demand. Subclasses call ``self._engine`` to get a `sqlalchemy.Engine`.
- :func:`dialect_upsert` — picks the right ``insert()`` constructor (SQLite
  vs PostgreSQL) and renders an ``ON CONFLICT DO UPDATE`` statement.

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

from typing import Any

from sqlalchemy import Engine, Table

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

    from sqlalchemy import MetaData
    table = Table(table_name, MetaData(), autoload_with=engine)
    stmt = _insert(table).values(**values)
    update_dict = {field: getattr(stmt.excluded, field) for field in update_fields}
    return stmt.on_conflict_do_update(index_elements=conflict_keys, set_=update_dict)
