"""Portable logical database dump — the cross-backend heart of a backup.

A gzipped JSON Lines stream: one header record, then ``{"table":…, "row":{…}}``
records in topological (FK-safe) order. Unlike the provider-specific
``cremind db backup`` snapshots (a raw SQLite file / a PG-dialect DDL+rows
dump), this format carries **no schema DDL** — schema is reconstructed on
restore by running Alembic *to the revision the dump was taken at*, then loading
rows through reflected tables, then upgrading to head. That indirection is
exactly what lets a SQLite dump restore into PostgreSQL and vice-versa.

Row-value fidelity comes for free from SQLAlchemy's typed columns: ``Boolean``
normalises SQLite's 0/1 to real bools, ``JSON`` (de)serialises dict/list on both
dialects. Cremind's schema uses only String/Text/Integer/Float/Boolean/JSON
(all timestamps are Float epochs), so there are no datetime/decimal quirks.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable

from sqlalchemy import Engine, MetaData, Table, inspect, text

from app.utils import logger

DBDUMP_FORMAT = "cremind-dbdump"
DBDUMP_VERSION = 1
_BATCH_ROWS = 500


@dataclass
class DumpStats:
    alembic_revision: str | None
    row_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class LoadStats:
    alembic_revision: str | None
    row_counts: dict[str, int] = field(default_factory=dict)


def _collect_live_tables(engine: Engine) -> tuple[list[Table], str | None]:
    """Return (ORM tables present in the live DB, current alembic revision).

    Filters ``Base.metadata.sorted_tables`` to tables that actually exist —
    the shared ``Base`` also carries a2a SDK tables the Cremind baseline never
    creates, and selecting from a non-existent relation would crash.
    """
    from a2a.server.models import Base
    import app.storage.models  # noqa: F401 — registers Cremind tables on Base

    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    live = [t for t in Base.metadata.sorted_tables if t.name in present]

    revision: str | None = None
    if "alembic_version" in present:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            if row is not None:
                revision = row[0]
    return live, revision


def _jsonable(value: Any) -> Any:
    """Coerce a column value to a JSON-safe form; raise on anything unexpected.

    Cremind's schema is String/Text/Integer/Float/Boolean/JSON only, so the
    passthrough set is small. An unexpected type is almost always a new column
    that needs explicit handling — better to fail loudly than to str() it and
    lose fidelity on restore.
    """
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    raise TypeError(
        f"Cannot serialise column value of type {type(value).__name__} to the "
        f"portable dump. Add handling in app/backup/dbdump.py::_jsonable or switch "
        f"the column to a JSON-friendly type."
    )


def dump_logical(engine: Engine, fileobj: BinaryIO) -> DumpStats:
    """Stream a ``cremind-dbdump`` v1 gzipped JSONL into ``fileobj`` (binary).

    Runs under a single snapshot-consistent read transaction: REPEATABLE READ on
    Postgres, a WAL read snapshot on SQLite. Safe to call with the server live.
    """
    live_tables, revision = _collect_live_tables(engine)
    row_counts: dict[str, int] = {}

    connect_opts: dict[str, Any] = {}
    if engine.dialect.name.startswith("postgres"):
        connect_opts["isolation_level"] = "REPEATABLE READ"

    with engine.connect().execution_options(**connect_opts) as conn:
        with gzip.GzipFile(fileobj=fileobj, mode="wb") as gz:
            header = {
                "format": DBDUMP_FORMAT,
                "version": DBDUMP_VERSION,
                "source_dialect": engine.dialect.name,
                "alembic_revision": revision,
                "tables": [t.name for t in live_tables],
            }
            gz.write((json.dumps(header) + "\n").encode("utf-8"))

            for table in live_tables:
                col_names = [c.name for c in table.columns]
                count = 0
                result = conn.execute(table.select())
                for row in result.mappings():
                    payload = {name: _jsonable(row[name]) for name in col_names}
                    gz.write(
                        (json.dumps({"table": table.name, "row": payload}) + "\n").encode("utf-8")
                    )
                    count += 1
                row_counts[table.name] = count

    return DumpStats(alembic_revision=revision, row_counts=row_counts)


def read_dump_header(fileobj: BinaryIO) -> dict[str, Any]:
    """Read and validate the header of a gzipped dump without consuming rows.

    Opens its own gzip view over ``fileobj`` — callers that then want to load
    rows should re-open the stream from the start (``load_logical`` does).
    """
    with gzip.GzipFile(fileobj=fileobj, mode="rb") as gz:
        line = gz.readline()
    if not line:
        raise ValueError("Database dump is empty.")
    header = json.loads(line.decode("utf-8"))
    if header.get("format") != DBDUMP_FORMAT:
        raise ValueError(
            f"Unrecognised DB dump format {header.get('format')!r} (expected {DBDUMP_FORMAT!r})."
        )
    if int(header.get("version") or 0) > DBDUMP_VERSION:
        raise ValueError(
            f"DB dump version {header.get('version')} is newer than this build supports."
        )
    return header


def load_logical(
    engine: Engine,
    fileobj: BinaryIO,
    row_transform: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> LoadStats:
    """Insert rows from a dump into an ALREADY-MIGRATED schema.

    The target schema must already exist at the dump's revision (the restore
    recipe runs ``migrations.upgrade(revision)`` first). Rows are inserted in
    file order — topological, so FKs are satisfied — through **reflected**
    tables, batched, inside one transaction. ``row_transform`` (path relocation)
    runs on each row before insert.
    """
    row_counts: dict[str, int] = {}
    revision: str | None = None

    with gzip.GzipFile(fileobj=fileobj, mode="rb") as gz:
        first = gz.readline()
        if not first:
            raise ValueError("Database dump is empty.")
        header = json.loads(first.decode("utf-8"))
        if header.get("format") != DBDUMP_FORMAT:
            raise ValueError(
                f"Unrecognised DB dump format {header.get('format')!r} (expected {DBDUMP_FORMAT!r})."
            )
        revision = header.get("alembic_revision")

        reflect_meta = MetaData()
        reflected: dict[str, Table] = {}

        with engine.begin() as conn:
            available = set(inspect(conn).get_table_names())
            pending_name: str | None = None
            pending_rows: list[dict[str, Any]] = []

            def _table(name: str) -> Table | None:
                if name in reflected:
                    return reflected[name]
                if name not in available:
                    return None
                tbl = Table(name, reflect_meta, autoload_with=conn)
                reflected[name] = tbl
                return tbl

            def _flush() -> None:
                nonlocal pending_rows
                if pending_name and pending_rows:
                    tbl = _table(pending_name)
                    if tbl is not None:
                        conn.execute(tbl.insert(), pending_rows)
                        row_counts[pending_name] = row_counts.get(pending_name, 0) + len(pending_rows)
                pending_rows = []

            for raw in gz:
                if not raw.strip():
                    continue
                record = json.loads(raw.decode("utf-8"))
                table_name = record.get("table")
                if "row" not in record or not table_name:
                    continue
                if table_name not in available:
                    # Table not created at this revision (e.g. an a2a table a
                    # future source produced). Tolerate — skip its rows.
                    if pending_name != f"__skip__{table_name}":
                        logger.warning(f"[backup:restore] skipping rows for unknown table {table_name!r}")
                        pending_name = f"__skip__{table_name}"
                    continue
                if pending_name != table_name:
                    _flush()
                    pending_name = table_name
                row = record["row"]
                if row_transform is not None:
                    row = row_transform(table_name, row)
                pending_rows.append(row)
                if len(pending_rows) >= _BATCH_ROWS:
                    _flush()
            _flush()

    return LoadStats(alembic_revision=revision, row_counts=row_counts)


def drop_all_tables(engine: Engine) -> None:
    """Drop every table in the live DB (reflection-based, FK-safe order).

    ``MetaData.reflect`` + ``drop_all`` drops in reverse-dependency order, which
    satisfies both SQLite (FK-on implicit deletes) and Postgres without needing
    dialect-specific CASCADE. Catches tables a half-applied migration left
    behind, not just the ones ORM metadata knows about.
    """
    meta = MetaData()
    meta.reflect(bind=engine)
    if meta.tables:
        meta.drop_all(bind=engine)


__all__ = [
    "DBDUMP_FORMAT",
    "DBDUMP_VERSION",
    "DumpStats",
    "LoadStats",
    "drop_all_tables",
    "dump_logical",
    "load_logical",
    "read_dump_header",
]
