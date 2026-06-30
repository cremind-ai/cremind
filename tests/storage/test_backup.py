"""Tests for provider-owned backup/restore.

These exercise the contract that landed when ``app/storage/backup.py`` was
demoted to a thin dispatcher: each :class:`DatabaseProvider` owns its own
backup format, no provider-name branching survives in the storage layer,
and the Postgres path no longer needs ``pg_dump`` on PATH.

A handful of tests cover the SQLite round-trip end-to-end (real engine,
real backup, real restore, assertions on row contents). The Postgres tests
exercise the snapshot format machinery directly — pure-Python serializer,
header validation, legacy-format detection — without needing a live PG
server, so they run in the same CI pass as everything else. A round-trip
against a real PG is left to manual verification per the plan.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.databases.postgres import (
    _SNAPSHOT_FORMAT,
    _SNAPSHOT_VERSION,
    _collect_metadata,
    _jsonable,
    _read_and_validate_header,
    _reject_legacy_pgdump,
    _write_header,
    _write_table_rows,
    _write_table_schema,
)
from app.databases.sqlite import SqliteDatabaseProvider


# ── SQLite round-trip ─────────────────────────────────────────────────────


def test_sqlite_backup_restore_round_trip(tmp_path: Path) -> None:
    """Populate → backup → wipe → restore → assert rows survive.

    Catches regressions in the provider plumbing (mostly): the underlying
    ``sqlite3.backup()`` call is well-tested, but the gzip step, the
    ``.tmp`` intermediate, and the Windows-safe ``closing()`` wrapping
    are all Cremind additions worth locking in.
    """
    db_path = tmp_path / "live.db"
    provider = SqliteDatabaseProvider(str(db_path))

    engine = provider.sync_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY, label TEXT)"))
        conn.execute(text("INSERT INTO widgets (id, label) VALUES (1, 'alpha'), (2, 'beta')"))

    # Take the snapshot.
    snap = tmp_path / "snap.sqlite.gz"
    written = provider.backup(snap)
    assert written == snap
    assert snap.is_file() and snap.stat().st_size > 0

    # Tear down the engine (releases file handles on Windows) and wipe the
    # live DB so we know restore is doing the work, not stale state.
    engine.dispose()
    db_path.unlink()
    assert not db_path.exists()

    # Restore + verify.
    provider2 = SqliteDatabaseProvider(str(db_path))
    provider2.restore(snap)
    assert db_path.is_file()

    engine2 = provider2.sync_engine()
    with engine2.connect() as conn:
        rows = conn.execute(text("SELECT id, label FROM widgets ORDER BY id")).all()
    assert rows == [(1, "alpha"), (2, "beta")]
    engine2.dispose()


def test_sqlite_backup_missing_db_raises(tmp_path: Path) -> None:
    """Backup against a never-materialised DB should raise FileNotFoundError.

    The SQLite provider deliberately doesn't mkdir the parent until the
    engine is built (see [app/databases/sqlite.py] _ensure_parent_dir),
    so a backup call without any prior engine work is the natural way to
    hit "no DB file yet".
    """
    db_path = tmp_path / "missing.db"
    provider = SqliteDatabaseProvider(str(db_path))
    with pytest.raises(FileNotFoundError):
        provider.backup(tmp_path / "snap.sqlite.gz")


# ── Postgres snapshot helpers (no live PG required) ──────────────────────


def test_jsonable_passthrough_for_primitives() -> None:
    """The serializer leaves JSON-native types untouched."""
    assert _jsonable(None) is None
    assert _jsonable(True) is True
    assert _jsonable(42) == 42
    assert _jsonable(3.14) == 3.14
    assert _jsonable("hello") == "hello"
    assert _jsonable({"a": 1, "b": [1, 2, 3]}) == {"a": 1, "b": [1, 2, 3]}
    assert _jsonable([1, "x", None]) == [1, "x", None]


def test_jsonable_rejects_unsupported_types() -> None:
    """Anything outside the documented passthrough set must raise.

    Cremind's schema only uses generic types per app/storage/models.py, so
    a value of a type we haven't listed almost always means someone added
    a column the snapshot format doesn't know how to round-trip. Silent
    str() coercion would corrupt on restore.
    """

    class Custom:
        pass

    with pytest.raises(TypeError, match="Cannot serialise value of type"):
        _jsonable(Custom())
    with pytest.raises(TypeError):
        _jsonable(b"bytes")


def test_reject_legacy_pgdump_recognises_magic_bytes(tmp_path: Path) -> None:
    """A gzipped file starting with ``PGDMP`` is the pg_dump custom format
    from the pre-refactor backup path. Restore should refuse it loudly
    rather than letting it through to the JSON parser.
    """
    legacy = tmp_path / "old.pgdump.gz"
    with gzip.open(legacy, "wb") as gz:
        gz.write(b"PGDMP\x00\x00more-bytes-here")
    with pytest.raises(RuntimeError, match="legacy pg_dump format"):
        _reject_legacy_pgdump(legacy)


def test_reject_legacy_pgdump_passes_through_new_format(tmp_path: Path) -> None:
    """Snapshots produced by the new path must NOT trip the legacy guard."""
    new = tmp_path / "new.pgsnap.gz"
    header = {"format": _SNAPSHOT_FORMAT, "version": _SNAPSHOT_VERSION, "tables": []}
    with gzip.open(new, "wt", encoding="utf-8") as gz:
        gz.write(json.dumps(header) + "\n")
    # Should return without raising.
    _reject_legacy_pgdump(new)


def test_read_and_validate_header_accepts_current_format(tmp_path: Path) -> None:
    """A well-formed header parses cleanly and surfaces the alembic revision."""
    header = {
        "format": _SNAPSHOT_FORMAT,
        "version": _SNAPSHOT_VERSION,
        "created_at": "2026-05-26T00:00:00+00:00",
        "alembic_revision": "20260509_baseline",
        "tables": ["profiles"],
    }
    path = tmp_path / "snap.pgsnap.gz"
    with gzip.open(path, "wt", encoding="utf-8") as gz:
        gz.write(json.dumps(header) + "\n")

    with gzip.open(path, "rt", encoding="utf-8") as gz:
        parsed = _read_and_validate_header(gz)
    assert parsed["alembic_revision"] == "20260509_baseline"


def test_read_and_validate_header_rejects_wrong_format(tmp_path: Path) -> None:
    """A header with a foreign ``format`` field surfaces a clear error."""
    path = tmp_path / "wrong.gz"
    with gzip.open(path, "wt", encoding="utf-8") as gz:
        gz.write(json.dumps({"format": "something-else", "version": 1}) + "\n")
    with gzip.open(path, "rt", encoding="utf-8") as gz:
        with pytest.raises(RuntimeError, match="Unrecognised snapshot format"):
            _read_and_validate_header(gz)


def test_read_and_validate_header_rejects_wrong_version(tmp_path: Path) -> None:
    """Version skew between the snapshot writer and reader must be loud."""
    path = tmp_path / "wrong.gz"
    with gzip.open(path, "wt", encoding="utf-8") as gz:
        gz.write(json.dumps({"format": _SNAPSHOT_FORMAT, "version": 999}) + "\n")
    with gzip.open(path, "rt", encoding="utf-8") as gz:
        with pytest.raises(RuntimeError, match="version 999"):
            _read_and_validate_header(gz)


# ── Regression: partial schema (the 0.2.5rc1 failure) ───────────────────


def test_collect_metadata_skips_tables_not_in_live_db(tmp_path: Path) -> None:
    """0.2.5rc1 crashed at the backup step with
    ``psycopg.errors.UndefinedTable: relation "push_notification_configs"
    does not exist`` because the shared ``Base.metadata`` carries a2a SDK
    tables that Cremind's Alembic baseline does NOT create. The backup
    iterated every metadata-registered table and ran ``SELECT *`` against
    a relation that had never been created.

    ``_collect_metadata`` must return only the tables present in the live
    DB so the backup loop never queries a missing relation. SQLite is fine
    for this test — the filter logic is dialect-independent.
    """
    from sqlalchemy import create_engine

    db_path = tmp_path / "partial.db"
    engine = create_engine(f"sqlite:///{db_path}")
    # Create just one of Cremind's tables. Critically, do NOT create
    # push_notification_configs (a2a SDK) or any of the others.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE profiles (id TEXT PRIMARY KEY, name TEXT)"))

    live, alembic_rev = _collect_metadata(engine)
    table_names = {t.name for t in live}

    assert "profiles" in table_names
    # The fix: a2a tables must NOT appear in the live list when they
    # don't exist in the live DB.
    assert "push_notification_configs" not in table_names
    assert "tasks" not in table_names
    # No alembic_version table created → revision should be None.
    assert alembic_rev is None
    engine.dispose()


def test_backup_iteration_skips_missing_tables_end_to_end(tmp_path: Path) -> None:
    """End-to-end backup-loop exercise against a partial schema.

    Drives the same writer functions ``PostgresDatabaseProvider.backup``
    uses, but against a SQLite engine so the test runs without a real
    Postgres. The shape of the assertion is the regression target: the
    loop must NOT raise even though ``Base.metadata.sorted_tables``
    references tables that don't exist in this DB. The DDL emitted will
    be PG-flavoured (because ``_write_table_schema`` compiles against
    the pg dialect by design), which is fine — we never replay it here,
    we only assert the writer completes.

    Uses the ORM's own metadata to create the partial schema, so the
    columns the writer SELECTs match what's on disk.
    """
    from sqlalchemy import create_engine
    from a2a.server.models import Base
    import app.storage.models  # noqa: F401 — registers tables on Base

    db_path = tmp_path / "partial.db"
    engine = create_engine(f"sqlite:///{db_path}")
    # Create ONLY the profiles table (using the ORM's own definition so
    # the columns match). Critically not creating push_notification_configs
    # / tasks / any other a2a or Cremind table — that's the regression
    # condition.
    Base.metadata.tables["profiles"].create(bind=engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('p1', 'alice', 0, 0)"
        ))

    snapshot = tmp_path / "snap.pgsnap.gz"
    live, alembic_rev = _collect_metadata(engine)
    with engine.connect() as conn, gzip.open(snapshot, "wt", encoding="utf-8") as gz:
        _write_header(gz, live, alembic_rev)
        for table in live:
            _write_table_schema(gz, table)
            _write_table_rows(gz, conn, table)
    engine.dispose()

    # The snapshot exists, was written without raising, and the header
    # only lists tables that actually existed.
    with gzip.open(snapshot, "rt", encoding="utf-8") as gz:
        header = json.loads(gz.readline())
    assert "profiles" in header["tables"]
    assert "push_notification_configs" not in header["tables"]


# ── Regression: no subprocess shell-out ──────────────────────────────────


def test_storage_backup_module_has_no_subprocess_dependency() -> None:
    """Before the refactor, ``app/storage/backup.py`` imported ``subprocess``
    and shelled to ``pg_dump`` / ``pg_restore``. The whole reason for the
    refactor was that those binaries aren't available in the Cremind
    Docker image — moving the logic onto providers (and using
    SQLAlchemy in PG's case) removed the binary dependency.

    This test locks the simplification in: if ``subprocess`` ever creeps
    back into the storage backup module, that's a regression.
    """
    import app.storage.backup as backup_module

    assert not hasattr(backup_module, "subprocess"), (
        "app/storage/backup.py should not import subprocess — backup logic "
        "lives on the providers now and no longer shells out to pg_dump."
    )


# ── Provider contract ────────────────────────────────────────────────────


def test_each_provider_exposes_a_backup_suffix() -> None:
    """The dispatcher in storage/backup.py builds the default file name as
    ``<timestamp>.<provider.backup_suffix>``. Every provider must declare
    one — the abstract method on DatabaseProvider enforces this at class
    construction, but pinning the actual values keeps the file extensions
    documented in one place.
    """
    from app.databases.postgres import PostgresDatabaseProvider
    from app.databases.sqlite import SqliteDatabaseProvider

    assert SqliteDatabaseProvider("dummy.db").backup_suffix == "sqlite.gz"
    pg: Any = PostgresDatabaseProvider(
        host="localhost", port=5432, database="x", user="y", password="z"
    )
    assert pg.backup_suffix == "pgsnap.gz"
