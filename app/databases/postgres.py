"""PostgreSQL database provider."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import Engine, MetaData, Table, create_engine, inspect, text
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex, CreateTable

from app.utils import logger

from .base import DatabaseProvider


# Bumped whenever the on-disk format changes in a non-backwards-compatible
# way. ``restore()`` refuses to read a file produced by a newer version.
_SNAPSHOT_FORMAT = "cremind-pgsnap"
_SNAPSHOT_VERSION = 1
# pg_dump's custom format starts with this magic; we sniff for it during
# restore so users see a clear error instead of a cryptic JSON parse
# failure when they hand us a pre-refactor backup.
_LEGACY_PGDUMP_MAGIC = b"PGDMP"
# Tunable batch size for INSERT during restore. Smaller batches recover
# memory faster on huge tables; larger batches reduce round-trips. 500
# is an order-of-magnitude balance — Cremind tables are typically much
# smaller than this so it usually flushes once per table.
_RESTORE_BATCH_ROWS = 500


class PostgresDatabaseProvider(DatabaseProvider):
    """PostgreSQL provider built on asyncpg (async) and psycopg v3 (sync).

    Connection params come from :mod:`app.config.bootstrap` — they live in
    ``bootstrap.toml`` (or the matching ``CREMIND_POSTGRES_*`` env vars), not
    in the regular Dynaconf settings, because we have to know them before any
    DB connection can succeed.
    """

    name = "postgres"

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        sslmode: str = "prefer",
    ):
        self.host = host
        self.port = int(port)
        self.database = database
        self.user = user
        self.password = password
        self.sslmode = (sslmode or "prefer").lower()
        self._async_engine: AsyncEngine | None = None
        self._sync_engine: Engine | None = None

    # ── URL builders ──────────────────────────────────────────────────────
    # Password is URL-encoded so unusual characters (``@``, ``/``, ``:``)
    # don't break the DSN. asyncpg uses the ``ssl`` query param while
    # psycopg uses ``sslmode``; map ours into the right name per driver.

    def _async_url(self) -> str:
        ssl = self._asyncpg_ssl_param()
        suffix = f"?ssl={ssl}" if ssl else ""
        return (
            f"postgresql+asyncpg://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.database}{suffix}"
        )

    def _sync_url(self) -> str:
        return (
            f"postgresql+psycopg://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.database}?sslmode={self.sslmode}"
        )

    def _asyncpg_ssl_param(self) -> str:
        # asyncpg accepts: disable, allow, prefer, require, verify-ca, verify-full.
        # Pass the user's choice through verbatim — it speaks the same vocabulary.
        return self.sslmode if self.sslmode and self.sslmode != "disable" else ""

    # ── Engines ───────────────────────────────────────────────────────────

    def async_engine(self) -> AsyncEngine:
        if self._async_engine is None:
            # NullPool: open/close per session inside the calling task. The
            # default pooled engine produces noisy CancelledError tracebacks
            # on client disconnect — SQLAlchemy's pool tries to terminate a
            # pooled connection under uvicorn's request cancel scope, and
            # asyncpg's graceful-close coroutine is awaited via the greenlet
            # bridge which propagates the cancellation. The teardown still
            # succeeds (asyncpg.terminate is forceful) but the traceback is
            # logged as a warning every time a streaming client closes its
            # tab. Per-session connections sidestep the race entirely. The
            # SQLite provider uses the same NullPool for identical reasons.
            self._async_engine = create_async_engine(
                self._async_url(),
                echo=False,
                poolclass=NullPool,
            )
            logger.info(
                f"[db] postgres async engine created "
                f"host={self.host} port={self.port} database={self.database} "
                f"user={self.user} sslmode={self.sslmode}"
            )
        return self._async_engine

    def sync_engine(self) -> Engine:
        if self._sync_engine is None:
            self._sync_engine = create_engine(
                self._sync_url(),
                echo=False,
                pool_pre_ping=True,
            )
            logger.info(
                f"[db] postgres sync engine created "
                f"host={self.host} port={self.port} database={self.database} "
                f"user={self.user} sslmode={self.sslmode}"
            )
        return self._sync_engine

    async def apply_pragmas(self, conn: AsyncConnection) -> None:
        # Postgres doesn't need WAL or FK pragmas — both are first-class
        # behavior of the engine itself.
        return None

    def add_column_if_not_exists(self, table: str, column_def: str) -> str:
        return f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column_def}"

    def supports_add_column_if_not_exists(self) -> bool:
        return True

    def alter_column_type(self, table: str, column: str, new_type: str) -> str | None:
        return f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {new_type}"

    def upsert(self, table_name: str, values: dict[str, Any], conflict_keys: list[str], update_fields: list[str]) -> Any:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy import Table, MetaData
        table = Table(table_name, MetaData(), autoload_with=self.sync_engine())
        stmt = pg_insert(table).values(**values)
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
    # Pure-Python snapshot: gzipped JSON Lines containing the ORM-derived
    # schema (CREATE TABLE / CREATE INDEX rendered against the PG dialect)
    # plus row data per table. No shell-out to ``pg_dump`` / ``pg_restore``
    # — the Cremind container ships psycopg/asyncpg but not the postgres
    # CLI, so the old subprocess approach failed with ``FileNotFoundError``
    # on every Docker install.
    #
    # Round-trip targets the same logical content pg_dump would capture
    # (schema + data + alembic_version) but the format is Cremind-specific
    # and not consumable by ``pg_restore``. Tradeoff is intentional: the
    # snapshot only has to be readable by the matching :meth:`restore` and
    # nothing else, so format simplicity wins over compatibility.

    backup_suffix: str = "pgsnap.gz"

    def backup(self, dest: Path) -> Path:
        """Write a gzipped JSONL snapshot of schema + rows to ``dest``."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        live_tables, alembic_rev = _collect_metadata(self.sync_engine())

        with self.sync_engine().connect() as conn:
            with gzip.open(dest, "wt", encoding="utf-8") as gz:
                _write_header(gz, live_tables, alembic_rev)
                for table in live_tables:
                    _write_table_schema(gz, table)
                    _write_table_rows(gz, conn, table)
        return dest

    def restore(self, src: Path) -> None:
        """Replace the live database with the contents of ``src``.

        Single transaction: drop existing tables, replay DDL, insert rows,
        stamp ``alembic_version``. A mid-restore failure rolls everything
        back so the DB stays at the pre-restore state rather than ending
        up half-dropped.
        """
        src = Path(src)
        _reject_legacy_pgdump(src)
        engine = self.sync_engine()

        # Stream the snapshot once — both header validation and table
        # processing walk the same generator. ``_iter_snapshot_lines``
        # yields parsed dicts.
        with gzip.open(src, "rt", encoding="utf-8") as gz:
            header = _read_and_validate_header(gz)
            with engine.begin() as conn:
                _drop_existing_tables(conn)
                _replay_snapshot(conn, gz)
                _stamp_alembic(conn, header.get("alembic_revision"))


# ── Snapshot helpers ──────────────────────────────────────────────────────
# Kept at module scope (not as static methods) so the test suite can exercise
# them directly without instantiating a provider. They're prefixed ``_`` so
# nothing outside this module imports them.


def _collect_metadata(engine: Engine) -> tuple[list[Table], str | None]:
    """Return (live ORM tables, current alembic revision or None).

    Filters ``Base.metadata.sorted_tables`` to only the tables that actually
    exist in the live DB. The shared ``Base`` carries ORM-registered tables
    from the a2a SDK (``push_notification_configs``, ``tasks``) alongside
    Cremind's own — but Cremind's Alembic baseline migration only creates the
    Cremind-owned subset (see ``app/alembic/versions/20260509_baseline.py``).
    Dumping every metadata table would issue ``SELECT * FROM
    push_notification_configs`` against a DB where that relation has never
    been created and crash with ``psycopg.errors.UndefinedTable`` — which
    was exactly the 0.2.5rc1 failure mode.

    Sticking with ORM-registered metadata (filtered by presence) rather
    than full reflection: reflection picks up half-applied state from a
    crashed migration and renders types in PG-specific spellings that may
    not match what our migrations would have produced. Filtering keeps the
    portable DDL while sidestepping the missing-table crash.
    """
    # ``from a2a.server.models import Base`` — same source the Alembic env
    # uses. ``import app.storage.models`` is the side-effect that registers
    # Cremind's tables on the shared ``Base.metadata``.
    from a2a.server.models import Base
    import app.storage.models  # noqa: F401 — registers tables on Base

    inspector = inspect(engine)
    present = set(inspector.get_table_names())

    live_tables: list[Table] = [
        t for t in Base.metadata.sorted_tables if t.name in present
    ]

    alembic_rev: str | None = None
    if "alembic_version" in present:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            if row is not None:
                alembic_rev = row[0]
    return live_tables, alembic_rev


def _write_header(gz, tables: list[Table], alembic_rev: str | None) -> None:
    header = {
        "format": _SNAPSHOT_FORMAT,
        "version": _SNAPSHOT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "alembic_revision": alembic_rev,
        "tables": [t.name for t in tables],
    }
    gz.write(json.dumps(header) + "\n")


def _write_table_schema(gz, table: Table) -> None:
    """Emit a ``{"table":..., "ddl":..., "indexes":[...]}`` record.

    Compiled against the PG dialect so the resulting DDL uses ``VARCHAR``
    bounds, ``JSON``/``BOOLEAN`` etc. with the spellings PG expects. Index
    DDL is emitted alongside so restore can recreate them in the same pass
    without a second metadata round-trip.
    """
    dialect = pg_dialect.dialect()
    ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
    indexes = [str(CreateIndex(idx).compile(dialect=dialect)).strip() for idx in table.indexes]
    record = {"table": table.name, "ddl": ddl, "indexes": indexes}
    gz.write(json.dumps(record) + "\n")


def _write_table_rows(gz, conn, table: Table) -> None:
    """Stream rows from ``table`` as ``{"table": name, "row": {...}}`` records.

    Column iteration uses ``column.name`` (the DB-side name), NOT the
    Python attribute name — they diverge for models like ``MessageModel``
    where ``message_metadata`` maps to the column ``metadata``. The
    insert path on restore reads the same DB names, so getting this
    right here is what keeps the round-trip lossless.
    """
    column_names = [col.name for col in table.columns]
    result = conn.execute(table.select())
    for row in result.mappings():
        payload = {name: _jsonable(row[name]) for name in column_names}
        gz.write(json.dumps({"table": table.name, "row": payload}) + "\n")


def _jsonable(value: Any) -> Any:
    """Coerce a column value into a JSON-safe form.

    Cremind's schema is intentionally generic (String/Text/Float/Integer/
    Boolean/JSON only — see ``app/storage/models.py``) so the passthrough
    set is small. Anything outside it raises rather than silently
    str()-ing the value: an unexpected type is almost always a schema
    addition that needs explicit handling, and silent stringification
    would lose data fidelity on restore.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list)):
        return value
    raise TypeError(
        f"Cannot serialise value of type {type(value).__name__} to the snapshot "
        f"format. Add handling in app/databases/postgres.py::_jsonable or "
        f"switch the column to a JSON-friendly type."
    )


def _reject_legacy_pgdump(src: Path) -> None:
    """If ``src`` is a pre-refactor ``pg_dump`` custom-format file, raise loudly.

    The old format starts with ASCII ``PGDMP`` once decompressed. Sniffing
    the first chunk is cheap and lets us surface a precise error instead of
    a JSONDecodeError when the user's old snapshot is fed to the new
    restore path.
    """
    try:
        with gzip.open(src, "rb") as gz:
            head = gz.read(len(_LEGACY_PGDUMP_MAGIC))
    except OSError as e:
        raise RuntimeError(f"Could not read snapshot at {src}: {e}") from e
    if head == _LEGACY_PGDUMP_MAGIC:
        raise RuntimeError(
            "This backup is in the legacy pg_dump format from Cremind before "
            "the pure-Python snapshot refactor. Restore via pg_restore manually "
            "or take a fresh backup with the current `cremind db backup`."
        )


def _read_and_validate_header(gz) -> dict[str, Any]:
    """Read the first JSONL line and confirm format/version match."""
    line = gz.readline()
    if not line:
        raise RuntimeError("Snapshot is empty.")
    try:
        header = json.loads(line)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Snapshot header is not valid JSON: {e}") from e
    if header.get("format") != _SNAPSHOT_FORMAT:
        raise RuntimeError(
            f"Unrecognised snapshot format {header.get('format')!r}; "
            f"expected {_SNAPSHOT_FORMAT!r}."
        )
    version = header.get("version")
    if version != _SNAPSHOT_VERSION:
        raise RuntimeError(
            f"Snapshot format version {version!r} is not supported by this build "
            f"(expected {_SNAPSHOT_VERSION}). Upgrade or downgrade Cremind to a "
            f"version that matches the snapshot."
        )
    return header


def _drop_existing_tables(conn) -> None:
    """Drop every user-schema table in the live DB before replaying the snapshot.

    Uses reflection (``inspector.get_table_names()``) rather than
    ``Base.metadata.sorted_tables`` so we catch:

    - Tables that ORM metadata declares but the live DB never created
      (e.g. a2a's ``push_notification_configs`` / ``tasks`` when the
      Cremind baseline migration didn't apply them — these need no drop,
      but reflection naturally skips them).
    - Tables the live DB has but the post-upgrade ORM doesn't know about
      (e.g. a half-applied migration that ``ADD``ed a table the snapshot
      doesn't recreate) — without reflection these would survive the
      "wipe" and collide with the snapshot's data on restore.

    ``CASCADE`` because PG won't drop a table that has dependent FK
    constraints otherwise, and we're about to recreate everything from
    scratch anyway.
    """
    inspector = inspect(conn)
    for name in inspector.get_table_names():
        conn.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))


def _replay_snapshot(conn, gz) -> None:
    """Walk the remaining JSONL records, recreating schema and inserting rows.

    Buffers row inserts per table — flushes at the table boundary or every
    ``_RESTORE_BATCH_ROWS`` rows, whichever comes first. Buffering keeps
    SQLAlchemy's ``executemany`` path engaged on the bulk inserts that
    dominate restore time.
    """
    # Rebuild a fresh metadata as we replay DDL so ``Table.insert()`` knows
    # the column shape post-restore. We can't use ``Base.metadata`` here
    # because the DDL we just executed is the source of truth — if the
    # snapshot is from a slightly different schema version than this
    # build's ORM, the snapshot wins for the duration of this restore.
    replay_meta = MetaData()
    pending_table: Table | None = None
    pending_rows: list[dict[str, Any]] = []

    def _flush() -> None:
        nonlocal pending_rows
        if pending_table is not None and pending_rows:
            conn.execute(pending_table.insert(), pending_rows)
            pending_rows = []

    for line in gz:
        if not line.strip():
            continue
        record = json.loads(line)
        table_name = record.get("table")
        if "ddl" in record:
            _flush()
            pending_rows = []
            conn.execute(text(record["ddl"]))
            for idx_ddl in record.get("indexes", ()):
                conn.execute(text(idx_ddl))
            pending_table = Table(table_name, replay_meta, autoload_with=conn)
        elif "row" in record:
            if pending_table is None or pending_table.name != table_name:
                _flush()
                pending_table = Table(table_name, replay_meta, autoload_with=conn)
            pending_rows.append(record["row"])
            if len(pending_rows) >= _RESTORE_BATCH_ROWS:
                _flush()
        else:
            # Unknown record kind — tolerate it so the format can grow
            # later without breaking older restores. Skipping is safe
            # because new kinds will be additive (extras the restore
            # doesn't need to act on).
            logger.debug(f"[db:restore] skipping unknown snapshot record kind: {set(record)}")
    _flush()


def _stamp_alembic(conn, revision: str | None) -> None:
    """Recreate the ``alembic_version`` table and stamp ``revision``.

    Done unconditionally even when ``revision`` is None — Alembic itself
    treats a missing table differently from an empty one (a missing table
    means "stamp not yet run", an empty table is invalid), and writing
    nothing into the column when the snapshot lacks a revision keeps that
    semantic faithful.
    """
    conn.execute(text(
        'CREATE TABLE IF NOT EXISTS "alembic_version" ('
        'version_num VARCHAR(32) NOT NULL PRIMARY KEY)'
    ))
    if revision:
        conn.execute(
            text('INSERT INTO "alembic_version" (version_num) VALUES (:rev)'),
            {"rev": revision},
        )
