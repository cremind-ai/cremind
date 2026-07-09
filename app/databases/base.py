"""Database provider abstraction.

A :class:`DatabaseProvider` exposes the seam between application code and the
underlying database engine. Concrete providers (:class:`SqliteDatabaseProvider`,
:class:`PostgresDatabaseProvider`) build SQLAlchemy engines and answer
dialect-specific questions; storage classes never branch on backend.

The same provider hands out **both** an async engine (for the conversation /
message ORM path that must not block the event loop) and a sync engine (for
the small synchronous storage modules that read config during process startup,
where async would force a refactor of every call site). Each provider also
owns its own backup/restore implementation — SQLite uses the ``sqlite3``
backup API for a file-level snapshot, Postgres dumps schema + rows to a
gzipped JSON Lines file (no ``pg_dump`` binary required). Both routes share
the same caller surface so :mod:`app.storage.backup` never branches on
provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class DatabaseProvider(ABC):
    """Strategy interface for a relational database backend."""

    name: str = "base"

    @abstractmethod
    def async_engine(self) -> AsyncEngine:
        """Return a cached async engine for this provider."""

    @abstractmethod
    def sync_engine(self) -> Engine:
        """Return a cached sync engine for this provider."""

    @abstractmethod
    async def apply_pragmas(self, conn: AsyncConnection) -> None:
        """Apply per-connection settings (WAL, foreign-key enforcement, etc.).

        No-op for backends that don't have SQLite-style pragmas.
        """

    @abstractmethod
    def add_column_if_not_exists(self, table: str, column_def: str) -> str:
        """Render a dialect-correct ``ALTER TABLE ... ADD COLUMN`` statement.

        SQLite has no ``IF NOT EXISTS`` clause for ADD COLUMN, so callers wrap
        the resulting statement in try/except. Postgres supports the native
        ``IF NOT EXISTS`` clause and is idempotent on its own.
        """

    @abstractmethod
    def alter_column_type(self, table: str, column: str, new_type: str) -> str | None:
        """Render a dialect-correct statement that widens (or otherwise changes)
        a column's type.

        Returns ``None`` when the dialect doesn't enforce/need the alteration
        (SQLite, which treats VARCHAR length as advisory). Callers run the
        statement only when a value is returned.
        """

    @abstractmethod
    def supports_add_column_if_not_exists(self) -> bool:
        """True iff the dialect's ADD COLUMN is naturally idempotent.

        Callers that issue additive migrations consult this to decide whether
        a duplicate-column error should be swallowed.
        """

    @abstractmethod
    def upsert(self, table_name: str, values: dict[str, Any], conflict_keys: list[str], update_fields: list[str]) -> Any:
        """Build an INSERT ... ON CONFLICT statement using the dialect's UPSERT.

        Returns a ready-to-execute SQLAlchemy statement. Both SQLite and
        Postgres support the syntax; the dialect-specific ``insert()``
        constructor differs, which is exactly what this method hides.
        """

    @abstractmethod
    async def health_check(self) -> None:
        """Open a short-lived connection and execute SELECT 1.

        Raises on failure. Used by the setup wizard to validate Postgres
        credentials before persisting them.
        """

    @abstractmethod
    async def dispose(self) -> None:
        """Release engine resources. Called when the wizard hot-swaps providers."""

    # ── Backup / restore ───────────────────────────────────────────────────
    # Owned by the provider because the on-disk format is fundamentally
    # backend-specific: SQLite snapshots are gzipped copies of the live DB
    # file (via the ``sqlite3`` backup API); Postgres snapshots are gzipped
    # JSON Lines containing ORM-derived DDL plus row data, so the upgrader
    # never has to shell to ``pg_dump`` / ``pg_restore``.

    @property
    @abstractmethod
    def backup_suffix(self) -> str:
        """File extension appended after ``<timestamp>.`` when the caller doesn't
        supply a destination path. Includes any compression suffix — e.g.
        ``"sqlite.gz"`` or ``"pgsnap.gz"``.
        """

    @abstractmethod
    def backup(self, dest: Path) -> Path:
        """Write a snapshot to ``dest``. Returns the path actually written.

        Synchronous: the upgrader takes the snapshot with the service stopped
        (or about to be), so blocking the event loop isn't a concern. Each
        provider picks the encoding that round-trips cleanly through its own
        :meth:`restore`.
        """

    @abstractmethod
    def restore(self, src: Path) -> None:
        """Replace the live database with the contents of ``src``.

        Destructive — callers ensure no writers are connected (the CLI prompts
        for confirmation, the upgrader has already torn down storage).
        Implementations should fail cleanly on format mismatches rather than
        silently corrupting the live DB.
        """

    # ── Portable logical dump (cross-backend) ────────────────────────────────
    # Separate from backup()/restore() above, which are same-backend fast paths
    # the upgrader relies on. These produce/consume the provider-neutral
    # ``cremind-dbdump`` format used by the full-system Backup & Restore feature,
    # which must round-trip across SQLite↔PostgreSQL. Concrete (not abstract):
    # the implementation is fully engine-agnostic through SQLAlchemy, so every
    # provider gets it for free.

    def dump_logical(self, fileobj) -> Any:
        """Stream a portable ``cremind-dbdump`` gzipped JSONL into ``fileobj``.

        Returns a ``DumpStats`` (revision + per-table row counts).
        """
        from app.backup.dbdump import dump_logical

        return dump_logical(self.sync_engine(), fileobj)

    def load_logical(self, fileobj, row_transform=None) -> Any:
        """Insert rows from a portable dump into an already-migrated schema.

        Returns a ``LoadStats``. The caller runs migrations to the dump's
        revision first (see :mod:`app.backup.engine`).
        """
        from app.backup.dbdump import load_logical

        return load_logical(self.sync_engine(), fileobj, row_transform)
