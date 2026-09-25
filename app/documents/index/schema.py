"""Schema of the per-profile Documentation search index file.

The index file (``<SYSTEM_DIR>/storage/documents/<profile_uid>/index.db``) is
derived data: every row can be rebuilt from the user's files plus the caption
cache in the main database. So it is versioned here, by :data:`SCHEMA_VERSION`
and a forward-only :data:`MIGRATIONS` list run at open, and **not** by Alembic.
Alembic's history belongs to the main database, which is backed up and
snapshotted by upgrades; this file is excluded from both. A file written by a
*newer* Cremind is never downgraded in place — the caller moves it aside and
rebuilds (see :class:`app.documents.index.db.IndexIncompatible`).

The FTS5 objects are deliberately **not** part of the migrations. Whether FTS5
exists depends on the SQLite build the process happens to load, not on the
file, so they are (re)created by :func:`create_lexical` whenever the build has
FTS5 and the file lacks them — which also repairs a file first written by a
build without FTS5, where search fell back to ``LIKE``.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

SCHEMA_VERSION = 1

LEXICAL_FTS5 = "fts5"
LEXICAL_LIKE = "like"

FTS_TABLE = "chunks_fts"
FTS_TRIGGERS = ("chunks_ai", "chunks_ad", "chunks_au")

# Columns that hold ``json.dumps`` text. The DB layer encodes them on write and
# decodes them on read, so callers only ever see dicts and lists. Kept here,
# next to the DDL, so a new JSON column cannot be added in one place only.
JSON_COLUMNS: dict[str, frozenset[str]] = {
    "source_state": frozenset({"detail", "root_identity", "estimate"}),
    "folders": frozenset({"project_meta"}),
    "files": frozenset({"doc_meta", "exif"}),
    "chunks": frozenset({"locator", "refs"}),
    "tasks": frozenset({"checkpoint"}),
    "activity": frozenset({"detail"}),
    "llm_cache": frozenset({"value"}),
    "meta": frozenset(),
    "collections": frozenset(),
}

_V1 = (
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE source_state (
        kind                   TEXT PRIMARY KEY,
        state                  TEXT,
        reason                 TEXT,
        detail                 TEXT,
        paused_user            INTEGER DEFAULT 0,
        paused_governor        TEXT,
        root_identity          TEXT,
        watch_mode             TEXT,
        drive_account_key      TEXT,
        drive_cursor           TEXT,
        drive_cursor_at        REAL,
        hold_since             REAL,
        purge_after            REAL,
        estimate               TEXT,
        last_scan_started_at   REAL,
        last_scan_finished_at  REAL,
        last_scan_s            REAL,
        updated_at             REAL
    )
    """,
    """
    CREATE TABLE folders (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        cite_id             TEXT UNIQUE NOT NULL,
        source              TEXT NOT NULL,
        parent_id           INTEGER,
        rel_path            TEXT NOT NULL,
        path_hash           TEXT NOT NULL,
        name                TEXT,
        name_folded         TEXT,
        depth               INTEGER,
        drive_id            TEXT,
        file_count          INTEGER DEFAULT 0,
        total_bytes         INTEGER DEFAULT 0,
        min_mtime           REAL,
        max_mtime           REAL,
        git_last_commit_at  REAL,
        is_project          INTEGER DEFAULT 0,
        project_meta        TEXT,
        card_hash           TEXT,
        status              TEXT DEFAULT 'live',
        updated_at          REAL,
        UNIQUE (source, path_hash)
    )
    """,
    # A directory rename rewrites every path under it; this makes that a range
    # scan instead of a table scan.
    "CREATE INDEX idx_folders_source_rel ON folders (source, rel_path)",
    """
    CREATE TABLE files (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        cite_id                  TEXT UNIQUE NOT NULL,
        source                   TEXT NOT NULL,
        folder_id                INTEGER,
        name                     TEXT,
        name_folded              TEXT,
        rel_path                 TEXT NOT NULL,
        path_hash                TEXT NOT NULL,
        ext                      TEXT,
        kind                     TEXT,
        mime                     TEXT,
        origin                   TEXT DEFAULT 'user',
        size                     INTEGER,
        mtime_ns                 INTEGER,
        mtime                    REAL,
        ino                      INTEGER,
        dev                      INTEGER,
        birthtime                REAL,
        sha256                   TEXT,
        hash_kind                TEXT,
        status                   TEXT NOT NULL DEFAULT 'dirty',
        status_reason            TEXT,
        error                    TEXT,
        attempts                 INTEGER DEFAULT 0,
        next_attempt_at          REAL,
        priority                 INTEGER DEFAULT 2,
        queued_at                REAL,
        caption_state            TEXT,
        extractor_version        INTEGER,
        chunker_version          INTEGER,
        doc_meta                 TEXT,
        exif                     TEXT,
        is_camera_photo          INTEGER DEFAULT 0,
        taken_at                 REAL,
        doc_created_at           REAL,
        first_seen_at            REAL,
        indexed_at               REAL,
        deleted_at               REAL,
        missing_since            REAL,
        chunk_count              INTEGER DEFAULT 0,
        text_bytes               INTEGER DEFAULT 0,
        drive_file_id            TEXT,
        drive_md5                TEXT,
        drive_version            TEXT,
        drive_modified           TEXT,
        drive_created            TEXT,
        drive_modified_by_me_at  REAL,
        drive_mime               TEXT,
        drive_web_link           TEXT,
        UNIQUE (source, path_hash)
    )
    """,
    # The work queue is this table: ``next_work`` walks this index.
    "CREATE INDEX idx_files_queue ON files (status, priority, queued_at)",
    "CREATE INDEX idx_files_sha ON files (sha256)",
    "CREATE INDEX idx_files_folder ON files (folder_id)",
    "CREATE INDEX idx_files_kind ON files (kind)",
    "CREATE INDEX idx_files_mtime ON files (mtime)",
    "CREATE INDEX idx_files_taken ON files (taken_at)",
    "CREATE INDEX idx_files_drive ON files (drive_file_id)",
    "CREATE INDEX idx_files_name ON files (name_folded)",
    # Directory renames (range on the prefix) and the file list's keyset
    # pagination on (rel_path, id).
    "CREATE INDEX idx_files_source_rel ON files (source, rel_path)",
    # AUTOINCREMENT is load-bearing: a chunk id is also its vector's point id,
    # so a reused rowid would let a stale vector (whose delete was lost in a
    # crash) resolve to a new, unrelated chunk. AUTOINCREMENT never reuses one,
    # even after the highest row is deleted.
    #
    # UNIQUE (file_id, text_hash, occ): NULL file_id rows (folder cards) are
    # distinct from each other under SQLite's NULL semantics, which is intended
    # — folder cards are keyed by folder_id, not file_id.
    """
    CREATE TABLE chunks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id      INTEGER,
        folder_id    INTEGER,
        source       TEXT,
        ctype        TEXT NOT NULL,
        ordinal      INTEGER,
        text_hash    TEXT NOT NULL,
        occ          INTEGER DEFAULT 0,
        heading      TEXT,
        text         TEXT NOT NULL,
        folded       TEXT,
        section_key  TEXT,
        locator      TEXT,
        refs         TEXT,
        token_est    INTEGER,
        vec_gen      INTEGER,
        created_at   REAL,
        UNIQUE (file_id, text_hash, occ)
    )
    """,
    "CREATE INDEX idx_chunks_file_ord ON chunks (file_id, ordinal)",
    "CREATE INDEX idx_chunks_hash ON chunks (text_hash)",
    "CREATE INDEX idx_chunks_section ON chunks (file_id, section_key)",
    "CREATE INDEX idx_chunks_folder ON chunks (folder_id)",
    "CREATE INDEX idx_chunks_vec_gen ON chunks (vec_gen)",
    """
    CREATE TABLE collections (
        gen           INTEGER PRIMARY KEY,
        name          TEXT NOT NULL,
        model_key     TEXT,
        dim           INTEGER,
        store_key     TEXT,
        state         TEXT,
        created_at    REAL,
        completed_at  REAL
    )
    """,
    """
    CREATE TABLE tasks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT NOT NULL,
        source      TEXT,
        state       TEXT DEFAULT 'queued',
        priority    INTEGER DEFAULT 3,
        checkpoint  TEXT,
        attempts    INTEGER DEFAULT 0,
        error       TEXT,
        not_before  REAL,
        created_at  REAL,
        updated_at  REAL
    )
    """,
    """
    CREATE TABLE activity (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        REAL,
        source    TEXT,
        level     TEXT,
        kind      TEXT,
        file_id   INTEGER,
        rel_path  TEXT,
        message   TEXT,
        detail    TEXT
    )
    """,
    """
    CREATE TABLE llm_cache (
        key           TEXT PRIMARY KEY,
        purpose       TEXT,
        file_id       INTEGER,
        content_hash  TEXT,
        value         TEXT,
        created_at    REAL
    )
    """,
    "CREATE INDEX idx_llm_cache_file ON llm_cache (file_id)",
)


def _v1(conn: sqlite3.Connection) -> None:
    for stmt in _V1:
        conn.execute(stmt)


# Ordered, forward-only. Each step runs inside the opener's write transaction
# and must leave the file valid for its version. A step that changes
# ``chunks`` columns must keep the FTS triggers in mind (see
# :func:`create_lexical`).
MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _v1),
)

assert MIGRATIONS[-1][0] == SCHEMA_VERSION, "SCHEMA_VERSION must name the last migration"


def migrate(conn: sqlite3.Connection, from_version: int) -> int:
    """Run every step newer than ``from_version`` (0 for a new file) inside
    the caller's transaction, recording ``schema_version`` after each one.
    Returns the version reached."""
    version = from_version
    for step_version, step in MIGRATIONS:
        if step_version <= version:
            continue
        step(conn)  # step 1 creates ``meta`` itself
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(step_version),),
        )
        version = step_version
    return version


# ── Lexical index (FTS5) ───────────────────────────────────────────────────

_FTS_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
    "heading, text, folded, content='chunks', content_rowid='id', "
    # remove_diacritics 0: "đất" (land) and "dat" must stay different tokens,
    # otherwise every Vietnamese query drowns in unrelated homographs. The
    # diacritics-free match goes through the separate ``folded`` column.
    "tokenize='unicode61 remove_diacritics 0')"
)

_FTS_TRIGGERS_DDL = (
    f"""
    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO {FTS_TABLE}(rowid, heading, text, folded)
        VALUES (new.id, new.heading, new.text, new.folded);
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, heading, text, folded)
        VALUES ('delete', old.id, old.heading, old.text, old.folded);
    END
    """,
    # ``UPDATE OF heading, text, folded`` is load-bearing: the chunk diff
    # rewrites ordinal/locator/occ/section_key of every chunk that merely
    # shifted, and vec_gen after each embed batch. Those updates must never
    # touch the FTS index — that is what makes a shifted-but-unchanged chunk
    # cost nothing.
    f"""
    CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF heading, text, folded ON chunks BEGIN
        INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, heading, text, folded)
        VALUES ('delete', old.id, old.heading, old.text, old.folded);
        INSERT INTO {FTS_TABLE}(rowid, heading, text, folded)
        VALUES (new.id, new.heading, new.text, new.folded);
    END
    """,
)


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Whether this process's SQLite build has FTS5.

    Probed by creating a throwaway TEMP virtual table rather than reading
    ``compile_options``: FTS5 can also arrive as a loadable extension, and the
    probe is the only answer that cannot be wrong. Must run outside a
    transaction on a writable connection.
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__ud_fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__ud_fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def lexical_objects_present(conn: sqlite3.Connection) -> bool:
    """True when the FTS table and all three sync triggers exist."""
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN (?, ?, ?, ?)",
            (FTS_TABLE, *FTS_TRIGGERS),
        ).fetchall()
    }
    return names == {FTS_TABLE, *FTS_TRIGGERS}


def create_lexical(conn: sqlite3.Connection) -> None:
    """Create whatever FTS objects are missing (idempotent). The caller runs
    the ``'rebuild'`` command afterwards when the table may be out of step
    with ``chunks``."""
    conn.execute(_FTS_DDL)
    for ddl in _FTS_TRIGGERS_DDL:
        conn.execute(ddl)


def drop_lexical_triggers(conn: sqlite3.Connection) -> None:
    """Drop the sync triggers so writes to ``chunks`` keep working when the
    loaded SQLite has no FTS5 — a trigger that inserts into an unloadable
    virtual table would fail every chunk write. The virtual table itself is
    left alone (it cannot be dropped without its module) and is rebuilt from
    ``chunks`` once FTS5 is back."""
    for name in FTS_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
