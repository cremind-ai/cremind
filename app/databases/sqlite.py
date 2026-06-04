"""SQLite database provider."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.utils import logger

from .base import DatabaseProvider


class SqliteDatabaseProvider(DatabaseProvider):
    """SQLite + aiosqlite provider.

    NullPool on the async engine: open/close per session inside the calling
    task. Avoids the aiosqlite teardown race where the pool terminates a
    pooled connection under a foreign cancel scope (uvicorn request scope),
    which produced noisy CancelledError tracebacks on client disconnect.
    """

    name = "sqlite"

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Deliberately do NOT mkdir here. The directory is only created when
        # an engine is actually built — this keeps the disk untouched while
        # the boot sequence is still constructing storage objects, in case a
        # later hot-swap discards this provider before any DB I/O happened.
        self._async_engine: AsyncEngine | None = None
        self._sync_engine: Engine | None = None

    def _ensure_parent_dir(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def async_engine(self) -> AsyncEngine:
        if self._async_engine is None:
            self._ensure_parent_dir()
            self._async_engine = create_async_engine(
                f"sqlite+aiosqlite:///{self.db_path}",
                echo=False,
                poolclass=NullPool,
            )
            logger.info(f"[db] sqlite async engine created path={self.db_path}")
        return self._async_engine

    def sync_engine(self) -> Engine:
        if self._sync_engine is None:
            self._ensure_parent_dir()
            self._sync_engine = create_engine(
                f"sqlite:///{self.db_path}",
                echo=False,
                connect_args={"check_same_thread": False},
            )
            logger.info(f"[db] sqlite sync engine created path={self.db_path}")

            @event.listens_for(self._sync_engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        return self._sync_engine

    async def apply_pragmas(self, conn: AsyncConnection) -> None:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))

        # Enforce foreign keys on every new connection to the underlying engine.
        sync_engine = conn.sync_connection.engine if conn.sync_connection else None
        if sync_engine is not None:
            @event.listens_for(sync_engine, "connect")
            def _enforce_fks(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    def add_column_if_not_exists(self, table: str, column_def: str) -> str:
        # SQLite doesn't support IF NOT EXISTS on ADD COLUMN. Caller wraps
        # this in try/except (see supports_add_column_if_not_exists).
        return f"ALTER TABLE {table} ADD COLUMN {column_def}"

    def supports_add_column_if_not_exists(self) -> bool:
        return False

    def alter_column_type(self, table: str, column: str, new_type: str) -> str | None:
        # SQLite treats VARCHAR length as advisory and has no in-place
        # ALTER COLUMN TYPE. Returning None tells the caller to skip.
        return None

    def upsert(self, table_name: str, values: dict[str, Any], conflict_keys: list[str], update_fields: list[str]) -> Any:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from sqlalchemy import Table, MetaData
        # Use a lightweight Table reference — caller has already ensured the
        # table exists via the schema migration in ConversationStorage.
        table = Table(table_name, MetaData(), autoload_with=self.sync_engine())
        stmt = sqlite_insert(table).values(**values)
        update_dict = {field: getattr(stmt.excluded, field) for field in update_fields}
        return stmt.on_conflict_do_update(index_elements=conflict_keys, set_=update_dict)

    async def health_check(self) -> None:
        async with self.async_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        if self._async_engine is not None:
            await self._async_engine.dispose()
            self._async_engine = None
        if self._sync_engine is not None:
            self._sync_engine.dispose()
            self._sync_engine = None

    # ── Backup / restore ───────────────────────────────────────────────────

    backup_suffix: str = "sqlite.gz"

    def backup(self, dest: Path) -> Path:
        """Take a consistent snapshot via SQLite's online backup API, then gzip.

        The intermediate ``.tmp`` file exists because :func:`sqlite3.connect`
        opens a real OS file handle on the destination — we want the gzip
        compression step to read a finalized copy rather than a half-written
        one, and ``closing()`` (below) is what releases that handle so the
        temp can be unlinked on Windows.
        """
        src = Path(self.db_path)
        if not src.is_file():
            raise FileNotFoundError(f"SQLite database not found at {src}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
        try:
            # ``with sqlite3.connect(...)`` only manages the transaction — it
            # does not close the connection, so the OS file handle on
            # ``tmp_dest`` would linger until GC and block ``unlink()`` on
            # Windows (WinError 32). ``closing()`` forces ``.close()`` on
            # scope exit.
            with (
                closing(sqlite3.connect(str(src))) as src_conn,
                closing(sqlite3.connect(str(tmp_dest))) as dst_conn,
            ):
                src_conn.backup(dst_conn)
            with open(tmp_dest, "rb") as raw, gzip.open(dest, "wb") as gz:
                shutil.copyfileobj(raw, gz)
        finally:
            if tmp_dest.exists():
                tmp_dest.unlink()
        return dest

    def restore(self, src: Path) -> None:
        """Replace the live database with the contents of ``src`` (a gzipped
        SQLite file, or — for legacy callers — a raw .db file).

        Destructive: the caller has already confirmed no writers are
        connected. The parent dir is mkdir'd to mirror engine-build behaviour
        in case the live DB was never materialised (deferred-storage mode).
        """
        dst = Path(self.db_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffixes[-1:] == [".gz"]:
            with gzip.open(src, "rb") as gz, open(dst, "wb") as raw:
                shutil.copyfileobj(gz, raw)
        else:
            shutil.copy2(src, dst)
