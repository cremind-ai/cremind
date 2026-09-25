"""The per-profile index database: one SQLite file per profile, owned here.

Why raw :mod:`sqlite3` and not SQLAlchemy: this file needs FTS5 virtual tables
and triggers, WAL and ``auto_vacuum`` pragmas that must run before the first
table exists, ``BEGIN IMMEDIATE`` transactions, and ``incremental_vacuum`` —
all of which an ORM would only get in the way of. The file is derived data
with its own schema version (:mod:`app.userdocs.index.schema`); it is never
touched by Alembic, backups or upgrade snapshots.

Connection model — the sync engine runs in worker threads, the query engine in
``asyncio.to_thread``, so everything here is synchronous and thread-safe:

- **One writer connection**, guarded by an ``RLock``. Every write method holds
  it for exactly one short ``BEGIN IMMEDIATE … COMMIT`` (rolled back on any
  error), so two writers never interleave and a crash leaves either the whole
  step or none of it. Long operations (purge, directory rename, clearing
  vector generations) commit in batches and release the lock between them, so
  the scheduler and search are never stuck behind one giant transaction.
- **Thread-local reader connections** for every read. WAL lets them read the
  last committed state while the writer works; they are opened with
  ``query_only`` so a read method can never write by accident.

One :class:`IndexDB` per file per process: the lock is per instance, so two
instances over the same file would be two writers contending on SQLite's
file lock instead of queueing on ours.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator, Sequence

from app.userdocs.index import schema as sch
from app.userdocs.types import ChunkDiff, ManifestRow, OldChunk
from app.utils.logger import logger

# Crockford base32 minus i, l, o, u: no characters a reader can confuse, and
# exactly what the citation regex ``[0-9a-hjkmnp-tv-z]{8}`` accepts.
CITE_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
CITE_LEN = 8

BUSY_TIMEOUT_MS = 5000

# ``PRAGMA quick_check`` reads every page, so on a multi-GB index it would cost
# seconds of disk I/O on every boot for every profile. Above this size the
# open only proves the header and schema are readable; deeper damage surfaces
# as an error from the statement that hits it (see :func:`is_corrupt_error`).
QUICK_CHECK_MAX_BYTES = 256 * 1024 * 1024

# After a checkpoint, SQLite leaves the -wal file at its high-water mark unless
# told otherwise. The storage governor measures disk usage, so a burst of
# indexing must not leave a permanently inflated WAL behind.
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024

# Bound on bound parameters per statement: portable to SQLite builds whose
# SQLITE_MAX_VARIABLE_NUMBER is still the old default of 999.
_MAX_PARAMS = 500
_RENAME_BATCH = 1000
_CLEAR_VEC_BATCH = 5000
_LIKE_MAX_TERMS = 16
_ACTIVITY_KEEP = 5000
# add_activity prunes the log itself every this many inserts, so the table
# stays bounded even if no caller ever runs prune_activity.
_ACTIVITY_PRUNE_EVERY = 256

_CHECKPOINT_MODES = frozenset({"PASSIVE", "FULL", "RESTART", "TRUNCATE"})

_UNSET: Any = object()


class IndexDBError(Exception):
    """Base class for index-file problems the caller resolves by rebuilding."""


class IndexIncompatible(IndexDBError):
    """The file belongs to another profile, or was written by a newer schema.

    Never migrated or "fixed" in place: the caller moves it aside with
    :func:`rebuild_file` and builds a fresh index."""


class IndexCorrupt(IndexDBError):
    """SQLite cannot read the file, or ``quick_check`` found damage."""


def is_corrupt_error(exc: BaseException) -> bool:
    """True when ``exc`` means the file itself is damaged (SQLITE_CORRUPT or
    SQLITE_NOTADB) — as opposed to a busy lock or a bad query, which must never
    trigger a rebuild."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)
    msg = str(exc).lower()
    return "malformed" in msg or "not a database" in msg


# ── Paths ──────────────────────────────────────────────────────────────────


def index_dir(profile_uid: str) -> str:
    """``<SYSTEM_DIR>/storage/userdocs/<profile_uid>`` — the whole directory is
    this profile's derived data (the index, and nothing a backup keeps)."""
    uid = str(profile_uid)
    # The uid names a directory that GC deletes wholesale; anything that could
    # climb out of storage/userdocs is a programming error, not a path.
    if not uid or uid in (".", "..") or any(sep in uid for sep in ("/", "\\", os.sep)):
        raise ValueError(f"invalid profile uid for an index directory: {profile_uid!r}")
    from app.config.settings import BaseConfig

    return os.path.join(BaseConfig.CREMIND_SYSTEM_DIR, "storage", "userdocs", uid)


def index_path(profile_uid: str) -> str:
    return os.path.join(index_dir(profile_uid), "index.db")


_SIDECARS = ("", "-wal", "-shm")


def rebuild_file(path: str) -> str | None:
    """Move ``path`` (and its ``-wal``/``-shm``) aside to ``<path>.bak`` so a
    fresh index can be created in its place. Returns the backup path, or None
    when there was no file to move.

    The WAL moves with the database (``index.db.bak-wal``): it holds committed
    transactions not yet checkpointed, and SQLite finds it by name when the
    backup is opened. Any older backup is removed first, sidecars included — a
    stale ``.bak-wal`` left next to a newer ``.bak`` would be replayed into it.
    Every connection to ``path`` must be closed first (Windows refuses to
    rename an open file)."""
    path = os.path.abspath(path)
    bak = path + ".bak"
    if not any(os.path.exists(path + s) for s in _SIDECARS):
        return None
    for s in _SIDECARS:
        try:
            os.remove(bak + s)
        except FileNotFoundError:
            pass
    for s in _SIDECARS:
        if os.path.exists(path + s):
            os.replace(path + s, bak + s)
    logger.warning(f"[userdocs] index moved aside for rebuild: {path} -> {bak}")
    return bak


def discard_backup(path: str) -> None:
    """Delete ``<path>.bak`` and its sidecars once a rebuild has succeeded."""
    bak = os.path.abspath(path) + ".bak"
    for s in _SIDECARS:
        try:
            os.remove(bak + s)
        except FileNotFoundError:
            pass


# ── Encoding helpers ───────────────────────────────────────────────────────


def _json(value: Any) -> str:
    # sort_keys: the chunk diff compares a kept chunk's stored locator with the
    # new one as text, so equal dicts must encode to equal strings.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _encode(table: str, col: str, value: Any) -> Any:
    if value is None:
        return None
    if col in sch.JSON_COLUMNS.get(table, ()):
        return _json(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list, tuple)):
        return _json(value)
    return value


def _decode(table: str, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for col in sch.JSON_COLUMNS.get(table, ()):
        v = d.get(col)
        if isinstance(v, str):
            try:
                d[col] = json.loads(v)
            except ValueError:
                pass  # plain text from a hand edit or an older writer: keep as is
    return d


def _batches(seq: Sequence[Any], n: int = _MAX_PARAMS) -> Iterator[list[Any]]:
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])


def _qmarks(n: int) -> str:
    return ", ".join("?" * n)


def _unique_ints(ids: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(int(i) for i in ids))


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _connect(path: str) -> sqlite3.Connection:
    # isolation_level=None: no implicit transactions — every write method
    # issues its own BEGIN IMMEDIATE, every read is its own snapshot.
    conn = sqlite3.connect(
        path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> float:
    return time.time()


def _rollback(conn: sqlite3.Connection) -> None:
    # SQLite may already have ended the transaction (a failed COMMIT, an
    # I/O error); an unconditional ROLLBACK would then raise and hide the
    # error that actually happened.
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


# ── The database ───────────────────────────────────────────────────────────


class IndexDB:
    """One profile's index file. Construct with :meth:`open`."""

    def __init__(
        self,
        path: str,
        writer: sqlite3.Connection,
        *,
        profile_uid: str,
        epoch: str,
        lexical: str,
        columns: dict[str, tuple[str, ...]],
    ) -> None:
        self._path = path
        self._writer = writer
        self._profile_uid = profile_uid
        self._epoch = epoch
        self._lexical = lexical
        self._columns = columns
        self._colsets = {t: frozenset(c) for t, c in columns.items()}
        self._wlock = threading.RLock()
        self._tx_depth = 0  # guarded by _wlock
        self._local = threading.local()
        self._readers: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._readers_lock = threading.Lock()
        self._closed = False
        self._activity_since_prune = 0

    # ── Open / close ───────────────────────────────────────────────────────

    @classmethod
    def open(cls, path: str, *, profile_uid: str, quick_check: bool | None = None) -> "IndexDB":
        """Open (creating if needed) the index at ``path`` for ``profile_uid``.

        Raises :class:`IndexIncompatible` for another profile's file or a newer
        schema, and :class:`IndexCorrupt` for a file SQLite cannot read. Both
        mean "call :func:`rebuild_file` and open again". Any other
        ``sqlite3`` error (a lock held by another process, a full disk)
        propagates unchanged — rebuilding would not fix it and would throw
        the index away.

        ``quick_check``: None runs ``PRAGMA quick_check`` only on files up to
        :data:`QUICK_CHECK_MAX_BYTES`; True/False force it on/off.
        """
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        conn = _connect(path)
        try:
            return cls._init(conn, path, str(profile_uid), quick_check, size)
        except sqlite3.DatabaseError as exc:
            conn.close()
            if is_corrupt_error(exc):
                logger.warning(f"[userdocs] index file is corrupt: {path}: {exc}")
                raise IndexCorrupt(f"{path}: {exc}") from exc
            raise
        except BaseException:
            conn.close()
            raise

    @classmethod
    def _init(
        cls,
        conn: sqlite3.Connection,
        path: str,
        profile_uid: str,
        quick_check: bool | None,
        size: int,
    ) -> "IndexDB":
        # The first read of sqlite_master is what fails on a non-database file.
        objects = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        fresh = objects == 0

        if quick_check is None:
            quick_check = not fresh and size <= QUICK_CHECK_MAX_BYTES
        if quick_check and not fresh:
            problems = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
            if problems != ["ok"]:
                detail = "; ".join(str(p) for p in problems[:3])
                logger.warning(f"[userdocs] index quick_check failed: {path}: {detail}")
                raise IndexCorrupt(f"{path}: quick_check: {detail}")

        if fresh:
            # Only possible before the first table exists: afterwards SQLite
            # can switch auto_vacuum only through a full VACUUM. INCREMENTAL
            # is what lets a purge actually give disk back (incremental_vacuum)
            # without rewriting the whole file.
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            # Some network/virtual filesystems cannot host the WAL's shared
            # memory. Still correct in rollback-journal mode, only readers
            # then wait for writers (busy_timeout covers it).
            logger.warning(f"[userdocs] index {path} runs in journal_mode={mode}, not WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}")

        if fresh:
            epoch = secrets.token_hex(3)
            conn.execute("BEGIN IMMEDIATE")
            try:
                sch.migrate(conn, 0)
                now = _now()
                conn.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [
                        ("profile_uid", profile_uid),
                        ("epoch", epoch),
                        # ``lexical`` is written by _settle_lexical just below.
                        ("created_at", repr(now)),
                    ],
                )
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
            logger.info(f"[userdocs] created index {path} (epoch {epoch})")
        else:
            epoch = cls._validate_and_migrate(conn, path, profile_uid)

        lexical = cls._settle_lexical(conn, path)
        columns = {
            t: tuple(r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall())
            for t in sch.JSON_COLUMNS
        }
        return cls(path, conn, profile_uid=profile_uid, epoch=epoch, lexical=lexical, columns=columns)

    @staticmethod
    def _validate_and_migrate(conn: sqlite3.Connection, path: str, profile_uid: str) -> str:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if not has_meta:
            raise IndexIncompatible(f"{path}: not a user-documents index (no meta table)")
        meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta").fetchall()}
        stored_uid = meta.get("profile_uid")
        if stored_uid != profile_uid:
            # A copied or restored directory under the wrong uid must never be
            # adopted: it would expose one profile's files to another.
            logger.warning(f"[userdocs] index {path} belongs to profile {stored_uid!r}, not {profile_uid!r}")
            raise IndexIncompatible(f"{path}: belongs to another profile")
        try:
            version = int(meta.get("schema_version") or 0)
        except ValueError:
            version = -1
        if version < 1 or version > sch.SCHEMA_VERSION:
            logger.warning(
                f"[userdocs] index {path} has schema {meta.get('schema_version')!r}; "
                f"this build reads up to {sch.SCHEMA_VERSION}"
            )
            raise IndexIncompatible(f"{path}: schema version {meta.get('schema_version')!r}")
        epoch = meta.get("epoch")
        if not epoch:
            raise IndexIncompatible(f"{path}: no epoch")
        if version < sch.SCHEMA_VERSION:
            conn.execute("BEGIN IMMEDIATE")
            try:
                sch.migrate(conn, version)
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
            logger.info(f"[userdocs] migrated index {path} from schema {version} to {sch.SCHEMA_VERSION}")
        return epoch

    @staticmethod
    def _settle_lexical(conn: sqlite3.Connection, path: str) -> str:
        """Make the FTS objects match what this SQLite build can do, touching
        the file only when something is actually out of step."""
        stored = (conn.execute("SELECT value FROM meta WHERE key = 'lexical'").fetchone() or [None])[0]
        if sch.fts5_available(conn):
            if stored == sch.LEXICAL_FTS5 and sch.lexical_objects_present(conn):
                return sch.LEXICAL_FTS5
            IndexDB._build_lexical(conn)
            if stored == sch.LEXICAL_FTS5:
                logger.warning(f"[userdocs] full-text objects were missing in {path}; recreated and rebuilt")
            elif stored == sch.LEXICAL_LIKE:
                logger.info(f"[userdocs] FTS5 is available now; full-text index built for {path}")
            return sch.LEXICAL_FTS5
        conn.execute("BEGIN IMMEDIATE")
        try:
            sch.drop_lexical_triggers(conn)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('lexical', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (sch.LEXICAL_LIKE,),
            )
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise
        if stored != sch.LEXICAL_LIKE:
            logger.warning(f"[userdocs] SQLite has no FTS5; keyword search for {path} falls back to LIKE")
        return sch.LEXICAL_LIKE

    @staticmethod
    def _build_lexical(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            sch.create_lexical(conn)
            conn.execute(f"INSERT INTO {sch.FTS_TABLE}({sch.FTS_TABLE}) VALUES ('rebuild')")
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('lexical', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (sch.LEXICAL_FTS5,),
            )
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    def close(self) -> None:
        """Close every connection. Idempotent. Stop the threads using this
        index first: a read in flight on another thread fails once its
        connection is closed."""
        with self._wlock:
            if self._closed:
                return
            self._closed = True
            with self._readers_lock:
                readers, self._readers = self._readers, []
            for _, conn in readers:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            # The writer closes last: the last connection to close checkpoints
            # the WAL back into the file, which a query_only reader cannot do.
            try:
                self._writer.close()
            except sqlite3.Error:
                pass

    @property
    def path(self) -> str:
        return self._path

    @property
    def epoch(self) -> str:
        return self._epoch

    @property
    def profile_uid(self) -> str:
        return self._profile_uid

    @property
    def lexical(self) -> str:
        return self._lexical

    @property
    def closed(self) -> bool:
        return self._closed

    def ensure_lexical_schema(self) -> None:
        """Recreate the FTS table and triggers if missing, then rebuild the
        full-text index from ``chunks``. Costs a pass over every chunk — run
        it for recovery, not routinely."""
        with self._wlock:
            conn = self._writer_conn()
            _rollback(conn)  # never nested in _tx: anything open is a leftover
            if not sch.fts5_available(conn):
                self._lexical = self._settle_lexical(conn, self._path)
                return
            self._build_lexical(conn)
            self._lexical = sch.LEXICAL_FTS5

    # ── Connection plumbing ────────────────────────────────────────────────

    def _writer_conn(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError(f"index {self._path} is closed")
        return self._writer

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction under the writer lock. A nested call on the
        same thread joins the open transaction instead of starting one."""
        with self._wlock:
            conn = self._writer_conn()
            if self._tx_depth:
                yield conn
                return
            # Not nested, yet a transaction is open: a leftover whose rollback
            # failed. Joining it would mean this write is never committed.
            _rollback(conn)
            conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
            finally:
                self._tx_depth = 0

    def _reader(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError(f"index {self._path} is closed")
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = _connect(self._path)
        conn.execute("PRAGMA query_only = ON")
        me = threading.current_thread()
        with self._readers_lock:
            if self._closed:
                conn.close()
                raise RuntimeError(f"index {self._path} is closed")
            # Pooled worker threads come and go; close the connections of
            # threads that have exited so they do not pile up until close().
            alive: list[tuple[threading.Thread, sqlite3.Connection]] = []
            for t, c in self._readers:
                if t.is_alive():
                    alive.append((t, c))
                else:
                    try:
                        c.close()
                    except sqlite3.Error:
                        pass
            alive.append((me, conn))
            self._readers = alive
        self._local.conn = conn
        return conn

    def _read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        # Always fetchall: a half-read cursor keeps its read snapshot open,
        # which stops checkpoints from ever emptying the WAL.
        return self._reader().execute(sql, params).fetchall()

    def _read_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self._read(sql, params)
        return rows[0] if rows else None

    def _check_fields(self, table: str, fields: dict[str, Any], *, forbidden: Iterable[str] = ()) -> None:
        # Column names are interpolated into SQL, so only real columns pass.
        blocked = set(forbidden)
        bad = sorted(k for k in fields if k not in self._colsets[table] or k in blocked)
        if bad:
            raise ValueError(f"unknown or read-only {table} column(s): {', '.join(bad)}")

    def _assignments(self, table: str, fields: dict[str, Any]) -> tuple[str, list[Any]]:
        cols = list(fields)
        return (
            ", ".join(f"{c} = ?" for c in cols),
            [_encode(table, c, fields[c]) for c in cols],
        )

    def _insert(self, conn: sqlite3.Connection, table: str, fields: dict[str, Any]) -> int:
        cols = list(fields)
        cur = conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({_qmarks(len(cols))})",
            [_encode(table, c, fields[c]) for c in cols],
        )
        return int(cur.lastrowid)

    # ── Meta ───────────────────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        row = self._read_one("SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else row[0]

    def set_meta(self, key: str, value: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, None if value is None else str(value)),
            )

    # ── Source state ───────────────────────────────────────────────────────

    def get_source_state(self, kind: str) -> dict[str, Any]:
        """The runtime state row for source ``kind``; a row of defaults (every
        field None, ``paused_user`` 0) when the source has none yet."""
        row = _decode("source_state", self._read_one("SELECT * FROM source_state WHERE kind = ?", (kind,)))
        if row is not None:
            return row
        blank: dict[str, Any] = {c: None for c in self._columns["source_state"]}
        blank.update(kind=kind, paused_user=0)
        return blank

    def update_source_state(self, kind: str, **fields: Any) -> None:
        """Upsert the given fields of ``kind``'s state row (``updated_at``
        defaults to now). Dicts and lists are stored as JSON."""
        self._check_fields("source_state", fields, forbidden={"kind"})
        fields.setdefault("updated_at", _now())
        cols = list(fields)
        with self._tx() as conn:
            conn.execute(
                f"INSERT INTO source_state (kind, {', '.join(cols)}) VALUES (?, {_qmarks(len(cols))}) "
                f"ON CONFLICT(kind) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}",
                [kind, *(_encode("source_state", c, fields[c]) for c in cols)],
            )

    # ── Cite ids ───────────────────────────────────────────────────────────

    def new_cite_id(self) -> str:
        """A fresh 8-character cite id, unused by any file or folder. The
        insert methods allocate their own inside their transaction; this is
        for callers that need one up front."""
        with self._wlock:
            return self._alloc_cite_id(self._writer_conn())

    @staticmethod
    def _alloc_cite_id(conn: sqlite3.Connection) -> str:
        # One allocator for both tables: a citation token names a file or a
        # folder without saying which, so an id must be unique across both.
        # 32^8 ≈ 10^12 ids, so the retry loop is a formality.
        for _ in range(64):
            cid = "".join(secrets.choice(CITE_ALPHABET) for _ in range(CITE_LEN))
            hit = conn.execute(
                "SELECT 1 FROM files WHERE cite_id = ? UNION ALL SELECT 1 FROM folders WHERE cite_id = ? LIMIT 1",
                (cid, cid),
            ).fetchone()
            if hit is None:
                return cid
        raise RuntimeError("could not allocate a unique cite id")

    # ── Folders ────────────────────────────────────────────────────────────

    def upsert_folder(self, source: str, rel_path: str, path_hash: str, **fields: Any) -> int:
        """Create the folder (with a new cite id) or update the given fields
        of the existing one; returns its id. ``rel_path`` is rewritten too, so
        a case-only rename on a case-insensitive filesystem sticks."""
        self._check_fields("folders", fields, forbidden={"id", "cite_id", "source", "rel_path", "path_hash"})
        fields.setdefault("updated_at", _now())
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id FROM folders WHERE source = ? AND path_hash = ?", (source, path_hash)
            ).fetchone()
            if row is not None:
                sets, params = self._assignments("folders", {"rel_path": rel_path, **fields})
                conn.execute(f"UPDATE folders SET {sets} WHERE id = ?", [*params, row[0]])
                return int(row[0])
            return self._insert(
                conn,
                "folders",
                {
                    "cite_id": self._alloc_cite_id(conn),
                    "source": source,
                    "rel_path": rel_path,
                    "path_hash": path_hash,
                    **fields,
                },
            )

    def get_folder(self, folder_id: int) -> dict[str, Any] | None:
        return _decode("folders", self._read_one("SELECT * FROM folders WHERE id = ?", (folder_id,)))

    def folder_by_path(self, source: str, path_hash: str) -> dict[str, Any] | None:
        return _decode(
            "folders",
            self._read_one("SELECT * FROM folders WHERE source = ? AND path_hash = ?", (source, path_hash)),
        )

    def folder_by_cite(self, cite_id: str) -> dict[str, Any] | None:
        return _decode("folders", self._read_one("SELECT * FROM folders WHERE cite_id = ?", (cite_id,)))

    def list_folders(
        self,
        *,
        source: str | None = None,
        is_project: bool | None = None,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Folders in id order, keyset-paginated by ``after_id``."""
        where, params = ["id > ?"], [int(after_id)]
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if is_project is not None:
            where.append("is_project = ?")
            params.append(1 if is_project else 0)
        rows = self._read(
            f"SELECT * FROM folders WHERE {' AND '.join(where)} ORDER BY id LIMIT ?", [*params, int(limit)]
        )
        return [_decode("folders", r) for r in rows]

    def delete_folders(self, ids: Iterable[int]) -> list[int]:
        """Delete folder rows and their folder-card chunks; returns the deleted
        chunk ids (their vectors must go too).

        Files, chunks and child folders that still point at a deleted folder
        keep the now-dangling id: folder ids are never reused (AUTOINCREMENT),
        so it resolves to nothing, and nulling it would rewrite every body
        chunk of a large folder in one transaction."""
        ids = _unique_ints(ids)
        deleted: list[int] = []
        with self._tx() as conn:
            for batch in _batches(ids):
                q = _qmarks(len(batch))
                deleted += [
                    r[0]
                    for r in conn.execute(
                        f"SELECT id FROM chunks WHERE file_id IS NULL AND folder_id IN ({q})", batch
                    ).fetchall()
                ]
                conn.execute(f"DELETE FROM chunks WHERE file_id IS NULL AND folder_id IN ({q})", batch)
                conn.execute(f"DELETE FROM folders WHERE id IN ({q})", batch)
        return deleted

    # ── Files ──────────────────────────────────────────────────────────────

    def load_manifest(self, source: str) -> dict[str, ManifestRow]:
        """Every non-tombstone file of ``source``, keyed by ``path_hash`` (the
        normcase'd key, so a scan on a case-insensitive filesystem matches
        rows regardless of how the OS spells the case today)."""
        rows = self._read(
            "SELECT id, path_hash, rel_path, size, mtime_ns, ino, dev, sha256, status "
            "FROM files WHERE source = ? AND status != 'tombstone'",
            (source,),
        )
        return {
            r["path_hash"]: ManifestRow(
                id=r["id"],
                path_hash=r["path_hash"],
                rel_path=r["rel_path"],
                size=r["size"] or 0,
                mtime_ns=r["mtime_ns"] or 0,
                ino=r["ino"] or 0,
                dev=r["dev"] or 0,
                sha256=r["sha256"],
                status=r["status"],
            )
            for r in rows
        }

    def insert_file(self, source: str, rel_path: str, path_hash: str, **fields: Any) -> dict[str, Any]:
        """Insert a new file row (status defaults to 'dirty') and return it in
        full, with its id and new cite id. ``first_seen_at`` and ``queued_at``
        default to now. Raises ``sqlite3.IntegrityError`` when (source,
        path_hash) already exists — look it up with :meth:`file_by_path`."""
        self._check_fields("files", fields, forbidden={"id", "cite_id", "source", "rel_path", "path_hash"})
        now = _now()
        fields.setdefault("first_seen_at", now)
        fields.setdefault("queued_at", now)
        with self._tx() as conn:
            fid = self._insert(
                conn,
                "files",
                {
                    "cite_id": self._alloc_cite_id(conn),
                    "source": source,
                    "rel_path": rel_path,
                    "path_hash": path_hash,
                    **fields,
                },
            )
            row = conn.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
        return _decode("files", row)  # type: ignore[return-value]

    def update_file(self, file_id: int, *, if_queued_at: Any = _UNSET, **fields: Any) -> bool:
        """Update the given fields of one file; True when the row was changed.

        ``if_queued_at`` makes it a compare-and-set: the update applies only if
        the row's ``queued_at`` still equals the value the worker read from
        :meth:`next_work`. :meth:`mark_dirty` always moves ``queued_at``, so a
        file edited while it was being indexed is not marked 'indexed' with
        the old content — it stays dirty and is picked up again."""
        self._check_fields("files", fields, forbidden={"id", "cite_id"})
        if not fields:
            return False
        sets, params = self._assignments("files", fields)
        sql = f"UPDATE files SET {sets} WHERE id = ?"
        params.append(int(file_id))
        if if_queued_at is not _UNSET:
            sql += " AND queued_at IS ?"
            params.append(if_queued_at)
        with self._tx() as conn:
            return conn.execute(sql, params).rowcount > 0

    def get_file(self, file_id: int) -> dict[str, Any] | None:
        return _decode("files", self._read_one("SELECT * FROM files WHERE id = ?", (file_id,)))

    def file_by_cite(self, cite_id: str) -> dict[str, Any] | None:
        return _decode("files", self._read_one("SELECT * FROM files WHERE cite_id = ?", (cite_id,)))

    def file_by_path(self, source: str, path_hash: str) -> dict[str, Any] | None:
        return _decode(
            "files",
            self._read_one("SELECT * FROM files WHERE source = ? AND path_hash = ?", (source, path_hash)),
        )

    def files_by_sha(self, sha256: str, limit: int = 5) -> list[dict[str, Any]]:
        """Rows with this content hash, any status (move matching wants the
        missing ones too), oldest first."""
        rows = self._read("SELECT * FROM files WHERE sha256 = ? ORDER BY id LIMIT ?", (sha256, int(limit)))
        return [_decode("files", r) for r in rows]

    def mark_dirty(self, file_ids: Iterable[int], *, priority: int) -> int:
        """Queue files for (re)indexing; returns how many rows were marked.

        Priority: lower is more urgent. A file already waiting keeps the more
        urgent of its current and the new priority; a file not waiting takes
        the new one (a stale priority from its last run means nothing).
        ``queued_at`` is always moved to now — the compare-and-set in
        :meth:`update_file` relies on that — with a microsecond step per
        position, so ids earlier in ``file_ids`` are picked first within a
        priority. ``attempts``/``next_attempt_at`` reset: a changed file
        deserves a fresh try, not the backoff of its previous content."""
        ids = _unique_ints(file_ids)
        if not ids:
            return 0
        now = _now()
        p = int(priority)
        with self._tx() as conn:
            cur = conn.executemany(
                "UPDATE files SET "
                "priority = CASE WHEN status = 'dirty' AND priority IS NOT NULL AND priority < ? "
                "THEN priority ELSE ? END, "
                "status = 'dirty', queued_at = ?, attempts = 0, next_attempt_at = NULL "
                "WHERE id = ?",
                [(p, p, now + i * 1e-6, fid) for i, fid in enumerate(ids)],
            )
            return max(cur.rowcount, 0)

    def prioritize(self, file_ids: Iterable[int], *, priority: int) -> int:
        """Move files already waiting up to ``priority`` (never down).
        ``queued_at`` is left alone, unlike :meth:`mark_dirty`: a file being
        indexed right now keeps its compare-and-set and is not made to start
        over."""
        ids = _unique_ints(file_ids)
        if not ids:
            return 0
        p = int(priority)
        with self._tx() as conn:
            cur = conn.executemany(
                "UPDATE files SET priority = ? "
                "WHERE id = ? AND status = 'dirty' AND (priority IS NULL OR priority > ?)",
                [(p, fid, p) for fid in ids],
            )
            return max(cur.rowcount, 0)

    def next_work(self, *, limit: int, now: float, exclude_ids: Iterable[int] = ()) -> list[dict[str, Any]]:
        """Files to process next: dirty ones, and errored ones whose backoff
        has expired, most urgent first (priority, then queue time).
        ``exclude_ids`` are the files already in flight.

        Two index-served queries merged here rather than one ``OR``, which
        SQLite would answer with a scan of the whole table on every call."""
        excl = set(int(i) for i in exclude_ids)
        n = int(limit) + len(excl)
        if int(limit) <= 0:
            return []
        dirty = self._read(
            "SELECT * FROM files WHERE status = 'dirty' ORDER BY priority, queued_at, id LIMIT ?", (n,)
        )
        due = self._read(
            "SELECT * FROM files WHERE status = 'error' AND next_attempt_at <= ? "
            "ORDER BY priority, queued_at, id LIMIT ?",
            (float(now), n),
        )

        def key(r: sqlite3.Row) -> tuple:
            # SQLite sorts NULL first; mirror it so both halves merge alike.
            p, q = r["priority"], r["queued_at"]
            return (p is not None, p or 0, q is not None, q or 0.0, r["id"])

        merged = sorted((r for r in (*dirty, *due) if r["id"] not in excl), key=key)
        return [_decode("files", r) for r in merged[: int(limit)]]  # type: ignore[misc]

    def rename_prefix(
        self,
        source: str,
        old_prefix: str,
        new_prefix: str,
        path_hash_fn: Callable[[str], str],
    ) -> int:
        """A directory moved from ``old_prefix`` to ``new_prefix``: rewrite
        ``rel_path`` and ``path_hash`` of every file and folder at or under it
        (folders' ``depth`` shifts by the change in nesting). Returns rows
        rewritten. Commits in batches.

        Paths only: the caller refreshes the moved folder's ``name`` /
        ``name_folded`` (:meth:`upsert_folder`) and re-diffs file cards, which
        embed the path. A row whose new path is already taken is left where it
        was (``UPDATE OR IGNORE``) — the next scan diff reconciles it."""
        old = old_prefix.strip("/")
        new = new_prefix.strip("/")
        if not old or not new:
            raise ValueError("rename_prefix needs non-empty prefixes")
        if old == new:
            return 0
        depth_delta = new.count("/") - old.count("/")
        # Everything under old/ sorts in [old + "/", old + "0"): "0" is the
        # code point right after "/", and BINARY collation compares UTF-8
        # bytes in code-point order.
        hi = old + "0"
        total = 0
        for table in ("files", "folders"):
            set_sql = "rel_path = ?, path_hash = ?" + (", depth = depth + ?" if table == "folders" else "")

            def params_for(rows: list[sqlite3.Row]) -> list[tuple]:
                out = []
                for r in rows:
                    rel = new + r["rel_path"][len(old):]
                    if table == "folders":
                        out.append((rel, path_hash_fn(rel), depth_delta, r["id"]))
                    else:
                        out.append((rel, path_hash_fn(rel), r["id"]))
                return out

            with self._tx() as conn:
                rows = conn.execute(
                    f"SELECT id, rel_path FROM {table} WHERE source = ? AND rel_path = ?", (source, old)
                ).fetchall()
                if rows:
                    total += max(
                        conn.executemany(f"UPDATE OR IGNORE {table} SET {set_sql} WHERE id = ?", params_for(rows)).rowcount,
                        0,
                    )
            cursor = old + "/"
            while True:
                with self._tx() as conn:
                    rows = conn.execute(
                        f"SELECT id, rel_path FROM {table} WHERE source = ? AND rel_path > ? AND rel_path < ? "
                        "ORDER BY rel_path LIMIT ?",
                        (source, cursor, hi, _RENAME_BATCH),
                    ).fetchall()
                    if not rows:
                        break
                    total += max(
                        conn.executemany(f"UPDATE OR IGNORE {table} SET {set_sql} WHERE id = ?", params_for(rows)).rowcount,
                        0,
                    )
                    cursor = rows[-1]["rel_path"]
        return total

    def delete_file(self, file_id: int) -> list[int]:
        """Delete a file row, its chunks and its LLM-cache rows in one
        transaction; returns the deleted chunk ids for vector deletion."""
        fid = int(file_id)
        with self._tx() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE file_id = ?", (fid,)).fetchall()]
            conn.execute("DELETE FROM chunks WHERE file_id = ?", (fid,))
            conn.execute("DELETE FROM llm_cache WHERE file_id = ?", (fid,))
            conn.execute("DELETE FROM files WHERE id = ?", (fid,))
        return ids

    def _count_by(self, col: str, source: str | None) -> dict[str, int]:
        where, params = ("WHERE source = ?", (source,)) if source is not None else ("", ())
        rows = self._read(f"SELECT {col}, COUNT(*) FROM files {where} GROUP BY {col}", params)
        return {(r[0] if r[0] is not None else ""): int(r[1]) for r in rows}

    def count_by_status(self, source: str | None = None) -> dict[str, int]:
        return self._count_by("status", source)

    def count_by_kind(self, source: str | None = None) -> dict[str, int]:
        return self._count_by("kind", source)

    def list_files(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        q: str | None = None,
        after: tuple[str, int] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Files ordered by (rel_path, id), keyset-paginated with ``after`` =
        the last row's (rel_path, id). Tombstones are listed only when asked
        for by ``status``. ``q`` matches a substring of the folded name or of
        the path."""
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            where.append("status = ?")
            params.append(status)
        else:
            where.append("status != 'tombstone'")
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if q:
            from app.userdocs.textnorm import fold

            where.append("(name_folded LIKE ? ESCAPE '\\' OR rel_path LIKE ? ESCAPE '\\')")
            params += [_like_pattern(fold(q)), _like_pattern(q)]
        if after is not None:
            where.append("(rel_path > ? OR (rel_path = ? AND id > ?))")
            params += [after[0], after[0], int(after[1])]
        rows = self._read(
            f"SELECT * FROM files WHERE {' AND '.join(where)} ORDER BY rel_path, id LIMIT ?",
            [*params, int(limit)],
        )
        return [_decode("files", r) for r in rows]

    def files_under(
        self, source: str, prefix: str, *, statuses: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Files at ``prefix`` itself or anywhere below it (``prefix`` without a
        trailing slash; ``""`` means the whole source). Used for a deleted or
        moved folder, where a watcher reports only the folder's own path."""
        prefix = prefix.strip("/")
        params: list[Any] = [source]
        where = ["source = ?"]
        if prefix:
            where.append("(rel_path = ? OR substr(rel_path, 1, ?) = ?)")
            params += [prefix, len(prefix) + 1, prefix + "/"]
        if statuses:
            where.append(f"status IN ({_qmarks(len(statuses))})")
            params += list(statuses)
        rows = self._read(f"SELECT * FROM files WHERE {' AND '.join(where)}", params)
        return [_decode("files", r) for r in rows]

    def folder_ids(self, source: str) -> dict[str, int]:
        """``{path_hash: id}`` for every folder of ``source`` — the scan uses
        it to link files to folders without one query per directory."""
        rows = self._read("SELECT path_hash, id FROM folders WHERE source = ?", (source,))
        return {r[0]: int(r[1]) for r in rows}

    def refresh_folder_stats(self, source: str) -> None:
        """Recompute each folder's direct file count, bytes and mtime range
        from ``files``. One statement, so it is cheap next to a full scan."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE folders SET "
                "file_count = (SELECT COUNT(*) FROM files f WHERE f.folder_id = folders.id "
                "  AND f.status NOT IN ('tombstone', 'missing')), "
                "total_bytes = (SELECT COALESCE(SUM(f.size), 0) FROM files f WHERE f.folder_id = folders.id "
                "  AND f.status NOT IN ('tombstone', 'missing')), "
                "min_mtime = (SELECT MIN(f.mtime) FROM files f WHERE f.folder_id = folders.id "
                "  AND f.status NOT IN ('tombstone', 'missing')), "
                "max_mtime = (SELECT MAX(f.mtime) FROM files f WHERE f.folder_id = folders.id "
                "  AND f.status NOT IN ('tombstone', 'missing')) "
                "WHERE source = ?",
                (source,),
            )

    def names_in_folder(self, folder_id: int) -> tuple[list[str], list[str]]:
        """(file names, subfolder names) directly inside a folder, from the
        index — project detection needs names, not another walk of the disk."""
        files = [
            r[0] for r in self._read(
                "SELECT name FROM files WHERE folder_id = ? AND status NOT IN ('tombstone', 'missing')",
                (int(folder_id),),
            ) if r[0]
        ]
        subs = [
            r[0] for r in self._read("SELECT name FROM folders WHERE parent_id = ?", (int(folder_id),)) if r[0]
        ]
        return files, subs

    def stats(self) -> dict[str, Any]:
        """Counts for status views, read as one consistent snapshot.

        ``chunks_without_vectors`` counts chunks that search cannot reach by
        vector: those not embedded into the active collection's generation
        (all of them when no collection is active)."""
        conn = self._reader()
        conn.execute("BEGIN")
        try:
            files = conn.execute("SELECT COUNT(*), COALESCE(SUM(text_bytes), 0) FROM files").fetchone()
            folders = conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            active = conn.execute(
                "SELECT gen FROM collections WHERE state = 'active' ORDER BY gen DESC LIMIT 1"
            ).fetchone()
            if active is None:
                without = chunks
            else:
                g = active[0]
                without = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM chunks WHERE vec_gen IS NULL) + "
                    "(SELECT COUNT(*) FROM chunks WHERE vec_gen < ?) + "
                    "(SELECT COUNT(*) FROM chunks WHERE vec_gen > ?)",
                    (g, g),
                ).fetchone()[0]
            by_status = {
                (r[0] or ""): int(r[1])
                for r in conn.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall()
            }
            by_kind = {
                (r[0] or ""): int(r[1])
                for r in conn.execute("SELECT kind, COUNT(*) FROM files GROUP BY kind").fetchall()
            }
        finally:
            conn.execute("COMMIT")
        return {
            "files": int(files[0]),
            "folders": int(folders),
            "chunks": int(chunks),
            "chunks_without_vectors": int(without),
            "text_bytes": int(files[1]),
            "by_status": by_status,
            "by_kind": by_kind,
        }

    # ── Chunks ─────────────────────────────────────────────────────────────

    @staticmethod
    def _old_chunk(r: sqlite3.Row) -> OldChunk:
        loc = r["locator"]
        try:
            locator = json.loads(loc) if isinstance(loc, str) else {}
        except ValueError:
            locator = {}
        return OldChunk(
            id=r["id"],
            text_hash=r["text_hash"],
            ordinal=r["ordinal"] if r["ordinal"] is not None else 0,
            occ=r["occ"] or 0,
            locator=locator if isinstance(locator, dict) else {},
            section_key=r["section_key"],
        )

    def get_chunks(self, file_id: int) -> list[OldChunk]:
        """A file's chunks as the diff needs them, in reading order."""
        rows = self._read(
            "SELECT id, text_hash, ordinal, occ, locator, section_key FROM chunks "
            "WHERE file_id = ? ORDER BY ordinal, id",
            (int(file_id),),
        )
        return [self._old_chunk(r) for r in rows]

    def folder_chunks(self, folder_id: int) -> list[OldChunk]:
        """A folder's own card chunks (not the body chunks of files in it,
        which carry the same ``folder_id`` for vector filtering)."""
        rows = self._read(
            "SELECT id, text_hash, ordinal, occ, locator, section_key FROM chunks "
            "WHERE file_id IS NULL AND folder_id = ? ORDER BY ordinal, id",
            (int(folder_id),),
        )
        return [self._old_chunk(r) for r in rows]

    def apply_chunks(
        self,
        *,
        file_id: int | None,
        folder_id: int | None,
        source: str,
        diff: ChunkDiff,
        file_fields: dict[str, Any] | None = None,
    ) -> list[int]:
        """Apply one chunk diff in ONE transaction; returns the ids of the
        inserted ``diff.add`` chunks, in order (all with ``vec_gen`` NULL —
        they still need vectors).

        For a file pass ``file_id`` (and its ``folder_id``); for a folder card
        pass ``file_id=None`` and the ``folder_id``. Order inside the
        transaction: removed rows are deleted first and changing ``occ``
        values pass through a temporary negative value, so the
        UNIQUE(file_id, text_hash, occ) slots a kept or added chunk moves into
        are always free. Kept chunks get only ordinal/occ/locator/section_key/
        refs (and folder_id after a move) rewritten, and only when different:
        their heading and text are part of the hash, so they cannot change,
        and the FTS trigger — which watches heading/text/folded only — never
        fires for them. Finally the file row gets its recounted
        ``chunk_count``/``text_bytes`` plus ``file_fields``.

        Raises LookupError when the file/folder no longer exists or a kept or
        removed chunk is not one of its chunks (the diff is stale: re-read and
        re-diff); nothing is written then."""
        if file_id is None and folder_id is None:
            raise ValueError("apply_chunks needs a file_id or a folder_id")
        if file_fields and file_id is None:
            raise ValueError("file_fields needs a file_id")
        file_fields = dict(file_fields or {})
        self._check_fields("files", file_fields, forbidden={"id", "cite_id", "chunk_count", "text_bytes"})
        if file_id is not None:
            owner_sql, owner_params = "file_id = ?", [int(file_id)]
        else:
            owner_sql, owner_params = "file_id IS NULL AND folder_id = ?", [int(folder_id)]  # type: ignore[arg-type]
        now = _now()

        with self._tx() as conn:
            if file_id is not None:
                exists = conn.execute("SELECT 1 FROM files WHERE id = ?", (int(file_id),)).fetchone()
            else:
                exists = conn.execute("SELECT 1 FROM folders WHERE id = ?", (int(folder_id),)).fetchone()  # type: ignore[arg-type]
            if exists is None:
                raise LookupError(f"{'file' if file_id is not None else 'folder'} "
                                  f"{file_id if file_id is not None else folder_id} no longer exists")

            # 1. Removed chunks.
            removed = _unique_ints(diff.remove)
            for batch in _batches(removed):
                cur = conn.execute(
                    f"DELETE FROM chunks WHERE id IN ({_qmarks(len(batch))}) AND {owner_sql}",
                    [*batch, *owner_params],
                )
                if cur.rowcount != len(batch):
                    raise LookupError("a removed chunk is not a chunk of this file (stale diff)")

            # 2. Kept chunks: position-only updates.
            if diff.keep:
                current: dict[int, sqlite3.Row] = {}
                for batch in _batches([int(cid) for cid, _ in diff.keep]):
                    for r in conn.execute(
                        "SELECT id, folder_id, ordinal, occ, locator, section_key, refs FROM chunks "
                        f"WHERE id IN ({_qmarks(len(batch))}) AND {owner_sql}",
                        [*batch, *owner_params],
                    ).fetchall():
                        current[r["id"]] = r
                updates: list[tuple[int, dict[str, Any]]] = []
                for cid, ch in diff.keep:
                    r = current.get(int(cid))
                    if r is None:
                        raise LookupError(f"kept chunk {cid} is not a chunk of this file (stale diff)")
                    upd: dict[str, Any] = {}
                    if r["ordinal"] != ch.ordinal:
                        upd["ordinal"] = ch.ordinal
                    if (r["occ"] or 0) != ch.occ:
                        upd["occ"] = ch.occ
                    loc = _json(ch.locator or {})
                    if r["locator"] != loc:
                        upd["locator"] = loc
                    if r["section_key"] != ch.section_key:
                        upd["section_key"] = ch.section_key
                    refs = _json(ch.refs or [])
                    if r["refs"] != refs:
                        upd["refs"] = refs
                    if file_id is not None and folder_id is not None and r["folder_id"] != folder_id:
                        upd["folder_id"] = folder_id
                    if upd:
                        updates.append((int(cid), upd))
                moving = [cid for cid, upd in updates if "occ" in upd]
                if moving:
                    # Real occurrences are >= 0 and ids are unique, so -id is
                    # a free slot for every row mid-swap.
                    conn.executemany("UPDATE chunks SET occ = ? WHERE id = ?", [(-cid, cid) for cid in moving])
                for cid, upd in updates:
                    cols = list(upd)
                    conn.execute(
                        f"UPDATE chunks SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                        [*(upd[c] for c in cols), cid],
                    )

            # 3. Added chunks.
            new_ids: list[int] = []
            for ch in diff.add:
                cur = conn.execute(
                    "INSERT INTO chunks (file_id, folder_id, source, ctype, ordinal, text_hash, occ, heading, "
                    "text, folded, section_key, locator, refs, token_est, vec_gen, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        file_id,
                        folder_id,
                        source,
                        ch.ctype,
                        ch.ordinal,
                        ch.text_hash,
                        ch.occ,
                        ch.heading,
                        ch.text,
                        ch.folded,
                        ch.section_key,
                        _json(ch.locator or {}),
                        _json(ch.refs or []),
                        ch.token_est,
                        now,
                    ),
                )
                new_ids.append(int(cur.lastrowid))

            # 4. The file row.
            if file_id is not None:
                self._recount_files(conn, [int(file_id)], extra=file_fields)
        return new_ids

    def _recount_files(
        self, conn: sqlite3.Connection, file_ids: list[int], *, extra: dict[str, Any] | None = None
    ) -> None:
        # Recounted rather than adjusted by deltas, so the numbers heal
        # themselves whichever path last touched the chunks. Cheap: the count
        # walks the (file_id, ordinal) index, the byte sum one file's rows.
        for fid in file_ids:
            cnt, text_bytes = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(CAST(text AS BLOB))), 0) FROM chunks WHERE file_id = ?",
                (fid,),
            ).fetchone()
            fields = {**(extra or {}), "chunk_count": int(cnt), "text_bytes": int(text_bytes)}
            sets, params = self._assignments("files", fields)
            conn.execute(f"UPDATE files SET {sets} WHERE id = ?", [*params, fid])

    def chunk_rows(self, ids: Iterable[int]) -> list[dict[str, Any]]:
        """Full chunk rows (JSON decoded) in the order of ``ids``; ids that no
        longer exist are skipped."""
        order = _unique_ints(ids)
        found: dict[int, dict[str, Any]] = {}
        for batch in _batches(order):
            for r in self._read(f"SELECT * FROM chunks WHERE id IN ({_qmarks(len(batch))})", batch):
                found[r["id"]] = _decode("chunks", r)  # type: ignore[assignment]
        return [found[i] for i in order if i in found]

    def set_vec_gen(self, ids: Iterable[int], gen: int) -> int:
        """Record that these chunks' vectors are in collection ``gen``.
        Chunks deleted since they were embedded are simply not there to
        update; their vectors are orphans for GC."""
        ids = _unique_ints(ids)
        n = 0
        with self._tx() as conn:
            for batch in _batches(ids):
                n += conn.execute(
                    f"UPDATE chunks SET vec_gen = ? WHERE id IN ({_qmarks(len(batch))})", [int(gen), *batch]
                ).rowcount
        return n

    def clear_vec_gen(self) -> int:
        """Mark every chunk as not embedded (e.g. the collection was wiped).
        Commits in batches."""
        total = 0
        while True:
            with self._tx() as conn:
                n = conn.execute(
                    "UPDATE chunks SET vec_gen = NULL WHERE id IN "
                    "(SELECT id FROM chunks WHERE vec_gen IS NOT NULL LIMIT ?)",
                    (_CLEAR_VEC_BATCH,),
                ).rowcount
            total += n
            if n <= 0:
                return total

    def chunks_needing_vectors(self, *, gen: int, after_id: int = 0, limit: int = 512) -> list[dict[str, Any]]:
        """Chunks with no vector in generation ``gen``, in id order after
        ``after_id``: id, heading, text, text_hash, ctype, file_id, folder_id,
        source. ``text_hash`` lets the caller reuse a vector
        (:meth:`reusable_vectors`) before embedding."""
        rows = self._read(
            "SELECT id, heading, text, text_hash, ctype, file_id, folder_id, source FROM chunks "
            "WHERE id > ? AND (vec_gen IS NULL OR vec_gen != ?) ORDER BY id LIMIT ?",
            (int(after_id), int(gen), int(limit)),
        )
        return [dict(r) for r in rows]

    def reusable_vectors(self, text_hashes: Iterable[str], *, gen: int) -> dict[str, int]:
        """For each hash, some chunk id already embedded in ``gen`` with that
        exact embed text — its vector can be copied instead of re-computed."""
        hashes = list(dict.fromkeys(text_hashes))
        out: dict[str, int] = {}
        for batch in _batches(hashes):
            for r in self._read(
                f"SELECT text_hash, MIN(id) FROM chunks WHERE vec_gen = ? AND text_hash IN ({_qmarks(len(batch))}) "
                "GROUP BY text_hash",
                [int(gen), *batch],
            ):
                out[r[0]] = int(r[1])
        return out

    def delete_chunks(self, ids: Iterable[int]) -> int:
        """Delete chunks by id (their files' counts are recounted); returns
        the number deleted."""
        ids = _unique_ints(ids)
        n = 0
        with self._tx() as conn:
            files: set[int] = set()
            for batch in _batches(ids):
                q = _qmarks(len(batch))
                files.update(
                    r[0]
                    for r in conn.execute(
                        f"SELECT DISTINCT file_id FROM chunks WHERE id IN ({q}) AND file_id IS NOT NULL", batch
                    ).fetchall()
                )
                n += conn.execute(f"DELETE FROM chunks WHERE id IN ({q})", batch).rowcount
            self._recount_files(conn, sorted(files))
        return n

    # ── Lexical search ─────────────────────────────────────────────────────

    def fts_search(
        self, match: str, *, limit: int = 60, file_ids: Sequence[int] | None = None
    ) -> list[tuple[int, float]]:
        """FTS5 search; ``match`` is a full FTS5 query the caller built safely
        (every user term double-quoted). Returns (chunk id, relevance), best
        first, where relevance = -bm25 (positive; higher is better) with
        column weights heading 0.3, text 1.0, folded 0.8.

        ``file_ids`` restricts to those files' chunks (an empty list matches
        nothing). A malformed query raises ValueError; an index without FTS5
        (``lexical == 'like'``) raises RuntimeError — use :meth:`like_search`.
        """
        if self._lexical != sch.LEXICAL_FTS5:
            raise RuntimeError("this index has no full-text table (lexical='like'); use like_search")
        rank = f"bm25({sch.FTS_TABLE}, 0.3, 1.0, 0.8)"
        try:
            if file_ids is None:
                rows = self._read(
                    f"SELECT rowid, {rank} AS s FROM {sch.FTS_TABLE} WHERE {sch.FTS_TABLE} MATCH ? "
                    "ORDER BY s LIMIT ?",
                    (match, int(limit)),
                )
            else:
                ids = _unique_ints(file_ids)
                rows = []
                # bm25 statistics are corpus-wide, so scores from separate
                # batches are directly comparable and merge exactly.
                for batch in _batches(ids):
                    rows += self._read(
                        f"SELECT {sch.FTS_TABLE}.rowid, {rank} AS s FROM {sch.FTS_TABLE} "
                        f"JOIN chunks c ON c.id = {sch.FTS_TABLE}.rowid "
                        f"WHERE {sch.FTS_TABLE} MATCH ? AND c.file_id IN ({_qmarks(len(batch))}) "
                        "ORDER BY s LIMIT ?",
                        [match, *batch, int(limit)],
                    )
                rows.sort(key=lambda r: (r[1], r[0]))
                rows = rows[: int(limit)]
        except sqlite3.OperationalError as exc:
            raise ValueError(f"full-text query rejected: {exc}") from exc
        return [(int(r[0]), -float(r[1])) for r in rows]

    def like_search(
        self, terms: Sequence[str], *, limit: int = 60, file_ids: Sequence[int] | None = None
    ) -> list[tuple[int, float]]:
        """Keyword fallback when FTS5 is unavailable: substring match of each
        term against heading/text/folded. Score = number of distinct terms
        matched; best first. A full scan of ``chunks`` — a degraded mode, not
        a fast path. (LIKE folds case for ASCII only; pass folded terms too
        for accent-insensitive matching.)"""
        uniq = [t for t in dict.fromkeys(terms) if t and t.strip()][:_LIKE_MAX_TERMS]
        if not uniq:
            return []
        parts: list[str] = []
        score_params: list[Any] = []
        for t in uniq:
            pat = _like_pattern(t)
            parts.append(
                "(CASE WHEN text LIKE ? ESCAPE '\\' OR heading LIKE ? ESCAPE '\\' "
                "OR folded LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)"
            )
            score_params += [pat, pat, pat]
        score = " + ".join(parts)

        def run(where: str, where_params: list[Any]) -> list[sqlite3.Row]:
            return self._read(
                f"SELECT id, s FROM (SELECT id, {score} AS s FROM chunks {where}) WHERE s > 0 "
                "ORDER BY s DESC, id LIMIT ?",
                [*score_params, *where_params, int(limit)],
            )

        if file_ids is None:
            rows = run("", [])
        else:
            rows = []
            for batch in _batches(_unique_ints(file_ids)):
                rows += run(f"WHERE file_id IN ({_qmarks(len(batch))})", batch)
            rows.sort(key=lambda r: (-r[1], r[0]))
            rows = rows[: int(limit)]
        return [(int(r[0]), float(r[1])) for r in rows]

    # ── Vector collections ─────────────────────────────────────────────────

    def active_collection(self) -> dict[str, Any] | None:
        return _decode(
            "collections",
            self._read_one("SELECT * FROM collections WHERE state = 'active' ORDER BY gen DESC LIMIT 1"),
        )

    def building_collection(self) -> dict[str, Any] | None:
        return _decode(
            "collections",
            self._read_one("SELECT * FROM collections WHERE state = 'building' ORDER BY gen DESC LIMIT 1"),
        )

    def list_collections(self) -> list[dict[str, Any]]:
        return [_decode("collections", r) for r in self._read("SELECT * FROM collections ORDER BY gen")]  # type: ignore[misc]

    def add_collection(
        self, gen: int, name: str, *, model_key: str, dim: int, store_key: str, state: str
    ) -> None:
        """Record a vector collection. Adding one as 'active' retires any other
        active collection, so exactly one answers searches."""
        now = _now()
        with self._tx() as conn:
            if state == "active":
                self._retire_active(conn, except_gen=int(gen))
            conn.execute(
                "INSERT INTO collections (gen, name, model_key, dim, store_key, state, created_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(gen), name, model_key, int(dim), store_key, state, now, now if state == "active" else None),
            )
            self._bump_gen_seq(conn, int(gen))

    def set_collection_state(self, gen: int, state: str) -> bool:
        """Change a collection's state (building | active | retiring).
        Activating one retires the previously active one and stamps
        ``completed_at``."""
        with self._tx() as conn:
            if state == "active":
                self._retire_active(conn, except_gen=int(gen))
                cur = conn.execute(
                    "UPDATE collections SET state = ?, completed_at = COALESCE(completed_at, ?) WHERE gen = ?",
                    (state, _now(), int(gen)),
                )
            else:
                cur = conn.execute("UPDATE collections SET state = ? WHERE gen = ?", (state, int(gen)))
            return cur.rowcount > 0

    @staticmethod
    def _retire_active(conn: sqlite3.Connection, *, except_gen: int) -> None:
        conn.execute("UPDATE collections SET state = 'retiring' WHERE state = 'active' AND gen != ?", (except_gen,))

    def delete_collection_row(self, gen: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM collections WHERE gen = ?", (int(gen),))

    def next_gen(self) -> int:
        """Reserve and return a new, never-used generation number.

        Never ``MAX(gen) + 1`` alone: if the newest collection row is deleted
        (an aborted re-embed), that number would come back while chunks still
        carry it in ``vec_gen`` — and would count as embedded in a collection
        that never received their vectors. The counter lives in ``meta`` and
        also clears every ``vec_gen`` ever written."""
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'gen_seq'").fetchone()
            seq = int(row[0]) if row and row[0] else 0
            top = conn.execute(
                "SELECT MAX(COALESCE((SELECT MAX(gen) FROM collections), 0), "
                "COALESCE((SELECT MAX(vec_gen) FROM chunks), 0))"
            ).fetchone()[0]
            gen = max(seq, int(top or 0)) + 1
            self._bump_gen_seq(conn, gen)
            return gen

    @staticmethod
    def _bump_gen_seq(conn: sqlite3.Connection, gen: int) -> None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('gen_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER)) AS TEXT)",
            (str(gen),),
        )

    # ── Tasks ──────────────────────────────────────────────────────────────

    def add_task(
        self,
        kind: str,
        *,
        source: str | None = None,
        priority: int = 3,
        checkpoint: Any = None,
        not_before: float | None = None,
    ) -> int:
        """Queue a background task; returns its id. If the same kind+source is
        already queued, that task is returned instead, taking the more urgent
        priority and the earlier ``not_before`` (None = due now). Its
        checkpoint is kept — it may hold progress."""
        now = _now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id, priority, not_before FROM tasks WHERE kind = ? AND source IS ? AND state = 'queued' "
                "ORDER BY id LIMIT 1",
                (kind, source),
            ).fetchone()
            if row is not None:
                p = int(priority) if row["priority"] is None else min(int(row["priority"]), int(priority))
                nb = None if (not_before is None or row["not_before"] is None) else min(row["not_before"], not_before)
                conn.execute(
                    "UPDATE tasks SET priority = ?, not_before = ?, updated_at = ? WHERE id = ?",
                    (p, nb, now, row["id"]),
                )
                return int(row["id"])
            return self._insert(
                conn,
                "tasks",
                {
                    "kind": kind,
                    "source": source,
                    "state": "queued",
                    "priority": int(priority),
                    "checkpoint": checkpoint,
                    "not_before": not_before,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def due_tasks(self, now: float, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT * FROM tasks WHERE state = 'queued' AND (not_before IS NULL OR not_before <= ?) "
            "ORDER BY priority, id LIMIT ?",
            (float(now), int(limit)),
        )
        return [_decode("tasks", r) for r in rows]  # type: ignore[misc]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        return _decode("tasks", self._read_one("SELECT * FROM tasks WHERE id = ?", (int(task_id),)))

    def update_task(self, task_id: int, **fields: Any) -> bool:
        self._check_fields("tasks", fields, forbidden={"id"})
        fields.setdefault("updated_at", _now())
        sets, params = self._assignments("tasks", fields)
        with self._tx() as conn:
            return conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", [*params, int(task_id)]).rowcount > 0

    def delete_task(self, task_id: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))

    def reset_running_tasks(self) -> int:
        """Boot recovery: tasks left 'running' by a crash go back to 'queued'
        (their checkpoints let them resume). A queued duplicate of the same
        kind+source is dropped in favour of the one with progress, so the
        de-duplication in :meth:`add_task` still holds."""
        now = _now()
        with self._tx() as conn:
            running = conn.execute("SELECT id, kind, source FROM tasks WHERE state = 'running'").fetchall()
            for r in running:
                conn.execute(
                    "DELETE FROM tasks WHERE state = 'queued' AND kind = ? AND source IS ? AND id != ?",
                    (r["kind"], r["source"], r["id"]),
                )
                conn.execute("UPDATE tasks SET state = 'queued', updated_at = ? WHERE id = ?", (now, r["id"]))
            return len(running)

    # ── Activity log ───────────────────────────────────────────────────────

    def add_activity(
        self,
        kind: str,
        message: str,
        *,
        source: str | None = None,
        level: str = "info",
        file_id: int | None = None,
        rel_path: str | None = None,
        detail: Any = None,
    ) -> int:
        """Append to the activity log; returns the row id. The log prunes
        itself to the newest 5000 rows every few hundred inserts."""
        with self._tx() as conn:
            aid = self._insert(
                conn,
                "activity",
                {
                    "ts": _now(),
                    "source": source,
                    "level": level,
                    "kind": kind,
                    "file_id": file_id,
                    "rel_path": rel_path,
                    "message": message,
                    "detail": detail,
                },
            )
            self._activity_since_prune += 1
            if self._activity_since_prune >= _ACTIVITY_PRUNE_EVERY:
                self._activity_since_prune = 0
                self._prune_activity(conn, _ACTIVITY_KEEP)
        return aid

    def list_activity(self, *, before_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first; page older with ``before_id`` = the last id seen."""
        if before_id is None:
            rows = self._read("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (int(limit),))
        else:
            rows = self._read(
                "SELECT * FROM activity WHERE id < ? ORDER BY id DESC LIMIT ?", (int(before_id), int(limit))
            )
        return [_decode("activity", r) for r in rows]  # type: ignore[misc]

    def prune_activity(self, keep: int = _ACTIVITY_KEEP) -> int:
        """Keep only the newest ``keep`` rows; returns rows deleted."""
        with self._tx() as conn:
            return self._prune_activity(conn, int(keep))

    @staticmethod
    def _prune_activity(conn: sqlite3.Connection, keep: int) -> int:
        if keep <= 0:
            return conn.execute("DELETE FROM activity").rowcount
        row = conn.execute("SELECT id FROM activity ORDER BY id DESC LIMIT 1 OFFSET ?", (keep - 1,)).fetchone()
        if row is None:
            return 0
        return conn.execute("DELETE FROM activity WHERE id < ?", (row[0],)).rowcount

    # ── LLM cache ──────────────────────────────────────────────────────────

    def cache_get(self, key: str) -> Any | None:
        row = self._read_one("SELECT value FROM llm_cache WHERE key = ?", (key,))
        if row is None or row[0] is None:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return row[0]

    def cache_put(
        self,
        key: str,
        value: Any,
        *,
        purpose: str,
        file_id: int | None = None,
        content_hash: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache (key, purpose, file_id, content_hash, value, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, purpose, file_id, content_hash, _encode("llm_cache", "value", value), _now()),
            )

    def cache_delete_for_files(self, file_ids: Iterable[int]) -> int:
        ids = _unique_ints(file_ids)
        n = 0
        with self._tx() as conn:
            for batch in _batches(ids):
                n += conn.execute(
                    f"DELETE FROM llm_cache WHERE file_id IN ({_qmarks(len(batch))})", batch
                ).rowcount
        return n

    # ── Purge ──────────────────────────────────────────────────────────────

    def purge_source(self, source: str, *, batch: int = 2000) -> Iterator[list[int]]:
        """Delete everything of ``source``: its files (with their chunks and
        LLM-cache rows), its folders (with their cards), any stray chunk still
        tagged with it, and finally its ``source_state`` row. A generator:
        each yielded list is the chunk ids one committed batch deleted, so the
        caller deletes those vectors before asking for the next batch — the
        writer lock is not held across a yield.

        Iterate it to exhaustion; stopping early leaves the rest in place
        (safe to resume by calling again). Stop the source's workers first,
        or they may re-insert rows mid-purge."""
        batch = max(1, int(batch))
        file_batch = min(batch, _MAX_PARAMS)

        # 1. Files, grouped so one round deletes about ``batch`` chunks.
        while True:
            with self._tx() as conn:
                files = conn.execute(
                    "SELECT id, chunk_count FROM files WHERE source = ? LIMIT ?", (source, file_batch)
                ).fetchall()
                if not files:
                    break
                take: list[int] = []
                est = 0
                for r in files:
                    c = r["chunk_count"] or 0
                    if take and est + c > batch:
                        break
                    take.append(r["id"])
                    est += c
                q = _qmarks(len(take))
                ids = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT id FROM chunks WHERE file_id IN ({q}) LIMIT ?", [*take, batch + 1]
                    ).fetchall()
                ]
                if len(ids) > batch:
                    # One file bigger than a batch (or stale counts): delete a
                    # bounded slice now; its row goes in a later round.
                    ids = ids[:batch]
                    for b in _batches(ids):
                        conn.execute(f"DELETE FROM chunks WHERE id IN ({_qmarks(len(b))})", b)
                else:
                    conn.execute(f"DELETE FROM chunks WHERE file_id IN ({q})", take)
                    conn.execute(f"DELETE FROM llm_cache WHERE file_id IN ({q})", take)
                    conn.execute(f"DELETE FROM files WHERE id IN ({q})", take)
            if ids:
                yield ids

        # 2. Folders and their cards.
        while True:
            with self._tx() as conn:
                folder_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM folders WHERE source = ? LIMIT ?", (source, min(file_batch, batch))
                    ).fetchall()
                ]
                if not folder_ids:
                    break
                q = _qmarks(len(folder_ids))
                ids = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT id FROM chunks WHERE file_id IS NULL AND folder_id IN ({q})", folder_ids
                    ).fetchall()
                ]
                conn.execute(f"DELETE FROM chunks WHERE file_id IS NULL AND folder_id IN ({q})", folder_ids)
                conn.execute(f"DELETE FROM folders WHERE id IN ({q})", folder_ids)
            if ids:
                yield ids

        # 3. Strays: chunks tagged with the source whose owner row is already
        # gone. Normally none, so this is one scan that finds nothing.
        while True:
            with self._tx() as conn:
                ids = [
                    r[0]
                    for r in conn.execute("SELECT id FROM chunks WHERE source = ? LIMIT ?", (source, batch)).fetchall()
                ]
                if not ids:
                    break
                for b in _batches(ids):
                    conn.execute(f"DELETE FROM chunks WHERE id IN ({_qmarks(len(b))})", b)
            yield ids

        with self._tx() as conn:
            conn.execute("DELETE FROM source_state WHERE kind = ?", (source,))

    def all_chunk_ids_page(self, after_id: int, limit: int) -> list[int]:
        """Every chunk id in order, keyset-paged — for vector GC."""
        return [
            r[0] for r in self._read("SELECT id FROM chunks WHERE id > ? ORDER BY id LIMIT ?", (int(after_id), int(limit)))
        ]

    # ── Query-engine reads ─────────────────────────────────────────────────
    #
    # Read-only helpers for :mod:`app.userdocs.query`. They run on the
    # thread-local ``query_only`` readers, so a search can never write and
    # never queues behind the sync engine's writer.

    def read_sql(self, sql: str, params: Sequence[Any] = (), *, table: str | None = None) -> list[dict[str, Any]]:
        """Rows of one read-only statement as dicts, with ``table``'s JSON
        columns decoded.

        For the query engine's filter and catalog SQL, which is composed from
        constant fragments with every user value bound as a parameter. The
        reader connection is ``query_only``, so a statement that tried to
        write would fail rather than change the index."""
        rows = self._read(sql, params)
        if table is None:
            return [dict(r) for r in rows]
        return [_decode(table, r) for r in rows]  # type: ignore[misc]

    def files_by_ids(self, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """``{id: file row}`` for the ids that still exist, any status."""
        out: dict[int, dict[str, Any]] = {}
        for batch in _batches(_unique_ints(ids)):
            for r in self._read(f"SELECT * FROM files WHERE id IN ({_qmarks(len(batch))})", batch):
                out[int(r["id"])] = _decode("files", r)  # type: ignore[assignment]
        return out

    def folders_by_ids(self, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """``{id: folder row}`` for the ids that still exist."""
        out: dict[int, dict[str, Any]] = {}
        for batch in _batches(_unique_ints(ids)):
            for r in self._read(f"SELECT * FROM folders WHERE id IN ({_qmarks(len(batch))})", batch):
                out[int(r["id"])] = _decode("folders", r)  # type: ignore[assignment]
        return out

    def folders_brief(self) -> list[dict[str, Any]]:
        """Every folder's identity and dates, without its project metadata —
        what folder-name resolution and folder grouping need, for all
        folders in one read."""
        rows = self._read(
            "SELECT id, cite_id, source, parent_id, rel_path, name, name_folded, depth, is_project, "
            "status, file_count, min_mtime, max_mtime, git_last_commit_at FROM folders"
        )
        return [dict(r) for r in rows]

    def chunks_of_file(self, file_id: int) -> list[dict[str, Any]]:
        """Every chunk of a file (card included), full rows, in reading
        order — what the reader reassembles the text from."""
        rows = self._read(
            "SELECT * FROM chunks WHERE file_id = ? ORDER BY ordinal, id", (int(file_id),)
        )
        return [_decode("chunks", r) for r in rows]  # type: ignore[misc]

    def fts_search_ctype(self, match: str, ctype: str, *, limit: int = 60) -> list[tuple[int, float]]:
        """:meth:`fts_search` restricted to one chunk type.

        Cards are how the catalog matches a file or folder by what it *is*
        rather than by every paragraph in it — and a folder card has no
        ``file_id``, so the file-restricted variant can never reach one."""
        if self._lexical != sch.LEXICAL_FTS5:
            raise RuntimeError("this index has no full-text table (lexical='like'); use like_search")
        rank = f"bm25({sch.FTS_TABLE}, 0.3, 1.0, 0.8)"
        try:
            rows = self._read(
                f"SELECT {sch.FTS_TABLE}.rowid, {rank} AS s FROM {sch.FTS_TABLE} "
                f"JOIN chunks c ON c.id = {sch.FTS_TABLE}.rowid "
                f"WHERE {sch.FTS_TABLE} MATCH ? AND c.ctype = ? ORDER BY s LIMIT ?",
                (match, ctype, int(limit)),
            )
        except sqlite3.OperationalError as exc:
            raise ValueError(f"full-text query rejected: {exc}") from exc
        return [(int(r[0]), -float(r[1])) for r in rows]

    def query_overview(self) -> dict[str, int]:
        """The counts a search result's header reports, in one pass over
        ``files``: searchable files, files still waiting to be (re)indexed,
        images waiting for a caption, and files that could not be read."""
        row = self._read_one(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status NOT IN ('missing', 'tombstone') THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status IN ('dirty', 'deferred') THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN kind = 'image' AND caption_state IN "
            "  ('awaiting_vision', 'awaiting_consent', 'over_cap') "
            "  AND status NOT IN ('missing', 'tombstone') THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0) "
            "FROM files"
        )
        visible, pending, captions, errors = (int(v or 0) for v in (row or (0, 0, 0, 0)))
        return {"files": visible, "pending": pending, "awaiting_captions": captions, "unreadable": errors}

    def vector_coverage(self, gen: int) -> tuple[int, int]:
        """(chunks with a vector in generation ``gen``, all chunks)."""
        with_vec = self._read_one("SELECT COUNT(*) FROM chunks WHERE vec_gen = ?", (int(gen),))
        total = self._read_one("SELECT COUNT(*) FROM chunks")
        return int(with_vec[0] if with_vec else 0), int(total[0] if total else 0)

    def min_first_seen(self, source: str) -> float | None:
        """When the source's first scan found its oldest-known file — the
        start of the first sync, after which ``first_seen_at`` means "the file
        appeared" rather than "the index was built"."""
        row = self._read_one("SELECT MIN(first_seen_at) FROM files WHERE source = ?", (source,))
        return float(row[0]) if row and row[0] is not None else None

    # ── Maintenance ────────────────────────────────────────────────────────

    def _pragma_int(self, name: str) -> int:
        return int(self._reader().execute(f"PRAGMA {name}").fetchone()[0])

    def size_bytes(self) -> int:
        """Bytes this index really occupies: pages in use (free pages are
        reusable, so they do not count) plus the WAL."""
        conn = self._reader()
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        free = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        return max(page_count - free, 0) * page_size + self._sidecar_size("-wal")

    def file_bytes(self) -> int:
        """Bytes on disk: the file plus its -wal and -shm."""
        return sum(self._sidecar_size(s) for s in _SIDECARS)

    def freelist_bytes(self) -> int:
        return self._pragma_int("freelist_count") * self._pragma_int("page_size")

    def _sidecar_size(self, suffix: str) -> int:
        try:
            return os.path.getsize(self._path + suffix)
        except OSError:
            return 0

    def incremental_vacuum(self, pages: int | None = None) -> int:
        """Return up to ``pages`` free pages (all when None) to the OS; returns
        pages freed. The file itself shrinks at the next checkpoint."""
        with self._wlock:
            conn = self._writer_conn()
            before = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            sql = "PRAGMA incremental_vacuum" if not pages else f"PRAGMA incremental_vacuum({int(pages)})"
            # fetchall is required: the pragma frees one page per step, and a
            # bare execute() steps it only once.
            conn.execute(sql).fetchall()
            after = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            return max(before - after, 0)

    def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        """``PRAGMA wal_checkpoint(mode)``; returns (busy, wal_frames,
        checkpointed_frames). PASSIVE never waits for readers; TRUNCATE also
        shrinks the -wal file to zero when it can."""
        mode = mode.upper()
        if mode not in _CHECKPOINT_MODES:
            raise ValueError(f"unknown checkpoint mode {mode!r}")
        with self._wlock:
            row = self._writer_conn().execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        return (int(row[0]), int(row[1]), int(row[2]))
