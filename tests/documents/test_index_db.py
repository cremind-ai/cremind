"""The per-profile index database (``app.documents.index``).

The index file is the store of record for everything the sync engine knows
about a profile's files, so these tests pin the properties the rest of the
engine leans on without re-checking:

- a file is never adopted by the wrong profile or a schema it cannot read, and
  a damaged file is recognised as such (so the caller rebuilds, not crashes);
- ids are never reused (a chunk id is also its vector's id), cite ids are
  unique across files and folders;
- a chunk diff lands atomically, and a chunk that merely moved costs no
  full-text write — the whole point of diffing by hash;
- keyword search handles Vietnamese with and without diacritics;
- long operations commit in batches, and many readers never see
  "database is locked" while the writer works.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.documents.index import db as db_mod
from app.documents.index import schema as sch
from app.documents.index import (
    CITE_ALPHABET,
    IndexCorrupt,
    IndexDB,
    IndexIncompatible,
    discard_backup,
    index_dir,
    index_path,
    is_corrupt_error,
    rebuild_file,
)
from app.documents.types import Chunk, ChunkDiff, ManifestRow

UID = "uid-a"


def mk(ordinal: int, text: str, *, heading: str = "", occ: int = 0, locator=None,
       folded: str | None = None, ctype: str = "body", section_key: str | None = None,
       refs=None) -> Chunk:
    h = hashlib.blake2b(f"{heading}\n{text}".encode(), digest_size=16).hexdigest()
    return Chunk(ordinal=ordinal, ctype=ctype, heading=heading, text=text, text_hash=h, occ=occ,
                 section_key=section_key, locator=dict(locator or {}), refs=list(refs or []),
                 folded=folded, token_est=len(text) // 4)


def ph(rel: str) -> str:
    return "h:" + rel.lower()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / UID / "index.db")


@pytest.fixture
def db(db_path: str):
    d = IndexDB.open(db_path, profile_uid=UID)
    yield d
    d.close()


def add_file(db: IndexDB, rel: str, texts: list[str], *, source: str = "local", **fields) -> tuple[int, list[int]]:
    fid = db.insert_file(source, rel, ph(rel), name=rel.rsplit("/", 1)[-1], **fields)["id"]
    ids = db.apply_chunks(file_id=fid, folder_id=None, source=source,
                          diff=ChunkDiff(add=[mk(i, t) for i, t in enumerate(texts)]))
    return fid, ids


def raw(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


# ── Open, versioning, recovery ─────────────────────────────────────────────


def test_reopen_keeps_epoch_and_identity(db_path):
    first = IndexDB.open(db_path, profile_uid=UID)
    epoch = first.epoch
    assert len(epoch) == 6 and int(epoch, 16) >= 0
    assert first.profile_uid == UID
    assert first.path == os.path.abspath(db_path)
    assert first.lexical == "fts5"
    first.set_meta("custom", "kept")
    first.close()
    first.close()  # idempotent

    again = IndexDB.open(db_path, profile_uid=UID)
    try:
        assert again.epoch == epoch
        assert again.get_meta("custom") == "kept"
        assert again.get_meta("schema_version") == str(sch.SCHEMA_VERSION)
        assert again.get_meta("created_at")
    finally:
        again.close()
    with pytest.raises(RuntimeError):
        again.get_meta("custom")


def test_new_file_is_wal_with_incremental_auto_vacuum(db, db_path):
    conn = raw(db_path)
    try:
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # INCREMENTAL
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_other_profiles_file_is_incompatible(db_path):
    IndexDB.open(db_path, profile_uid=UID).close()
    with pytest.raises(IndexIncompatible):
        IndexDB.open(db_path, profile_uid="uid-b")


def test_newer_schema_is_incompatible(db_path):
    IndexDB.open(db_path, profile_uid=UID).close()
    conn = raw(db_path)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(sch.SCHEMA_VERSION + 1),))
    conn.close()
    with pytest.raises(IndexIncompatible):
        IndexDB.open(db_path, profile_uid=UID)


def test_older_schema_is_migrated_forward_in_place(db_path, monkeypatch):
    db = IndexDB.open(db_path, profile_uid=UID)
    epoch = db.epoch
    _, ids = add_file(db, "a.txt", ["kept across the upgrade"])
    db.close()

    def v2(conn):
        conn.execute("ALTER TABLE files ADD COLUMN v2_flag INTEGER DEFAULT 7")

    monkeypatch.setattr(sch, "MIGRATIONS", (*sch.MIGRATIONS, (2, v2)))
    monkeypatch.setattr(sch, "SCHEMA_VERSION", 2)
    db = IndexDB.open(db_path, profile_uid=UID)
    try:
        assert db.get_meta("schema_version") == "2"
        assert db.epoch == epoch
        assert db.get_file(1)["v2_flag"] == 7
        assert [cid for cid, _ in db.fts_search('"upgrade"')] == ids
    finally:
        db.close()


def test_foreign_sqlite_file_is_incompatible(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = raw(db_path)
    conn.execute("CREATE TABLE something_else (x)")
    conn.close()
    with pytest.raises(IndexIncompatible):
        IndexDB.open(db_path, profile_uid=UID)


def test_garbage_file_is_corrupt_and_rebuild_moves_it_aside(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    Path(db_path).write_bytes(b"definitely not an sqlite database " * 200)
    with pytest.raises(IndexCorrupt):
        IndexDB.open(db_path, profile_uid=UID)

    bak = rebuild_file(db_path)
    assert bak == os.path.abspath(db_path) + ".bak"
    assert not os.path.exists(db_path)
    assert Path(bak).read_bytes().startswith(b"definitely not")

    fresh = IndexDB.open(db_path, profile_uid=UID)
    fresh.close()
    discard_backup(db_path)
    assert not os.path.exists(bak)


def test_damaged_pages_fail_quick_check(db_path):
    db = IndexDB.open(db_path, profile_uid=UID)
    for i in range(40):
        add_file(db, f"d/f{i}.txt", [f"chunk {i} {j} " * 30 for j in range(5)])
    db.close()
    size = os.path.getsize(db_path)
    assert size > 16 * 4096
    with open(db_path, "r+b") as fh:
        fh.seek(4 * 4096)
        fh.write(b"\xa5" * (8 * 4096))
    with pytest.raises(IndexCorrupt):
        IndexDB.open(db_path, profile_uid=UID)


def test_rebuild_file_moves_sidecars_and_drops_a_stale_backup(tmp_path):
    p = str(tmp_path / "index.db")
    Path(p).write_bytes(b"db")
    Path(p + "-wal").write_bytes(b"wal")
    # A leftover from an older rebuild that the new backup has no WAL for:
    # it must not survive next to the new .bak, or SQLite would replay it.
    Path(p + ".bak").write_bytes(b"old")
    Path(p + ".bak-shm").write_bytes(b"old-shm")
    bak = rebuild_file(p)
    assert Path(bak).read_bytes() == b"db"
    assert Path(bak + "-wal").read_bytes() == b"wal"
    assert not os.path.exists(bak + "-shm")
    assert not os.path.exists(p) and not os.path.exists(p + "-wal")
    assert rebuild_file(p) is None


def test_is_corrupt_error_never_calls_a_lock_corruption():
    assert not is_corrupt_error(sqlite3.OperationalError("database is locked"))
    assert not is_corrupt_error(ValueError("x"))
    assert is_corrupt_error(sqlite3.DatabaseError("database disk image is malformed"))


def test_index_path_lives_under_the_system_dir(tmp_path, monkeypatch):
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    assert index_path("abc") == os.path.join(str(tmp_path), "storage", "documents", "abc", "index.db")
    for bad in ("", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            index_dir(bad)


def test_fts5_missing_falls_back_to_like_and_recovers(db_path, monkeypatch):
    monkeypatch.setattr(sch, "fts5_available", lambda conn: False)
    db = IndexDB.open(db_path, profile_uid=UID)
    try:
        assert db.lexical == "like"
        assert db.get_meta("lexical") == "like"
        _, ids = add_file(db, "a.txt", ["Quyền sử dụng đất", "robot tracking"])
        with pytest.raises(RuntimeError):
            db.fts_search('"robot"')
        assert db.like_search(["robot"], limit=10) == [(ids[1], 1.0)]
    finally:
        db.close()

    # FTS5 is back: the next open builds the index from the stored chunks.
    monkeypatch.undo()
    db = IndexDB.open(db_path, profile_uid=UID)
    try:
        assert db.lexical == "fts5"
        assert [cid for cid, _ in db.fts_search('"robot"')] == [ids[1]]
    finally:
        db.close()


def test_file_written_with_fts_keeps_working_without_it(db_path, monkeypatch):
    db = IndexDB.open(db_path, profile_uid=UID)
    add_file(db, "a.txt", ["alpha"])
    db.close()

    monkeypatch.setattr(sch, "fts5_available", lambda conn: False)
    db = IndexDB.open(db_path, profile_uid=UID)
    try:
        # The sync triggers are gone, so chunk writes do not hit the
        # unloadable virtual table.
        _, ids = add_file(db, "b.txt", ["bravo"])
        assert db.lexical == "like"
    finally:
        db.close()

    monkeypatch.undo()
    db = IndexDB.open(db_path, profile_uid=UID)
    try:
        assert [cid for cid, _ in db.fts_search('"bravo"')] == ids
    finally:
        db.close()


def test_missing_fts_objects_are_recreated_on_open(db_path):
    db = IndexDB.open(db_path, profile_uid=UID)
    _, ids = add_file(db, "a.txt", ["alpha"])
    db.close()
    conn = raw(db_path)
    conn.execute("DROP TRIGGER chunks_au")
    conn.execute("DROP TABLE chunks_fts")
    conn.close()

    db = IndexDB.open(db_path, profile_uid=UID)
    try:
        assert [cid for cid, _ in db.fts_search('"alpha"')] == ids
        db.ensure_lexical_schema()  # explicit call is idempotent
        assert [cid for cid, _ in db.fts_search('"alpha"')] == ids
    finally:
        db.close()


# ── Meta, source state, field guards ───────────────────────────────────────


def test_source_state_defaults_and_json_round_trip(db):
    blank = db.get_source_state("local")
    assert blank["kind"] == "local" and blank["paused_user"] == 0 and blank["state"] is None
    db.update_source_state("local", state="indexing", estimate={"files": 10, "images": 2},
                           root_identity={"dev": 1, "ino": 2, "realpath": "/x"})
    db.update_source_state("local", reason="scan")
    s = db.get_source_state("local")
    assert s["state"] == "indexing" and s["reason"] == "scan"
    assert s["estimate"] == {"files": 10, "images": 2}
    assert s["root_identity"]["realpath"] == "/x"
    assert s["updated_at"] > 0
    assert db.get_source_state("drive")["state"] is None


def test_a_leftover_transaction_does_not_swallow_later_writes(db, db_path):
    db._writer.execute("BEGIN")  # as if a rollback had failed
    db.set_meta("after", "committed")
    conn = raw(db_path)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'after'").fetchone() == ("committed",)
    finally:
        conn.close()


def test_unknown_columns_are_rejected(db):
    fid = db.insert_file("local", "a.txt", ph("a.txt"))["id"]
    with pytest.raises(ValueError):
        db.update_file(fid, **{"status = 'x' --": 1})
    with pytest.raises(ValueError):
        db.update_file(fid, id=5)
    with pytest.raises(ValueError):
        db.update_source_state("local", nope=1)


# ── Cite ids ───────────────────────────────────────────────────────────────


def test_cite_ids_are_unique_across_files_and_folders(db, monkeypatch):
    cid = db.new_cite_id()
    assert len(cid) == 8 and set(cid) <= set(CITE_ALPHABET)

    script = iter("a" * 8 + "a" * 8 + "b" * 8)
    monkeypatch.setattr(db_mod.secrets, "choice", lambda seq: next(script))
    f = db.insert_file("local", "a.txt", ph("a.txt"))
    folder_id = db.upsert_folder("local", "docs", ph("docs"), name="docs")
    assert f["cite_id"] == "aaaaaaaa"
    # The folder's first draw collided with the file's id and was redrawn.
    assert db.get_folder(folder_id)["cite_id"] == "bbbbbbbb"
    assert db.folder_by_cite("bbbbbbbb")["id"] == folder_id
    assert db.file_by_cite("aaaaaaaa")["id"] == f["id"]


# ── Files: manifest, queue ─────────────────────────────────────────────────


def test_manifest_round_trip(db):
    a = db.insert_file("local", "a.txt", ph("a.txt"), size=10, mtime_ns=111, ino=5, dev=7, sha256="s1",
                       status="indexed")
    b = db.insert_file("local", "B/c.pdf", ph("B/c.pdf"), size=20, mtime_ns=222)
    db.insert_file("local", "gone.txt", ph("gone.txt"), status="tombstone")
    db.insert_file("drive", "Drive/x.doc", ph("Drive/x.doc"))
    m = db.load_manifest("local")
    assert set(m) == {ph("a.txt"), ph("B/c.pdf")}
    assert m[ph("a.txt")] == ManifestRow(id=a["id"], path_hash=ph("a.txt"), rel_path="a.txt", size=10,
                                         mtime_ns=111, ino=5, dev=7, sha256="s1", status="indexed")
    assert m[ph("B/c.pdf")].status == "dirty" and m[ph("B/c.pdf")].ino == 0
    assert b["first_seen_at"] and b["queued_at"] and b["status"] == "dirty" and b["priority"] == 2
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_file("local", "a.txt", ph("a.txt"))
    assert db.file_by_path("local", ph("a.txt"))["id"] == a["id"]
    assert [r["id"] for r in db.files_by_sha("s1")] == [a["id"]]


def test_next_work_orders_by_priority_then_queue_and_honours_backoff(db):
    ids = [db.insert_file("local", f"f{i}.txt", ph(f"f{i}.txt"), status="indexed")["id"] for i in range(6)]
    now = time.time()
    assert db.next_work(limit=10, now=now) == []

    db.mark_dirty([ids[3], ids[1]], priority=2)  # list order is queue order
    db.mark_dirty([ids[5]], priority=0)
    db.update_file(ids[0], status="error", next_attempt_at=now - 10, priority=1)
    db.update_file(ids[2], status="error", next_attempt_at=now + 1000, priority=0)  # still backing off
    order = [w["id"] for w in db.next_work(limit=10, now=now)]
    assert order == [ids[5], ids[0], ids[3], ids[1]]
    assert [w["id"] for w in db.next_work(limit=2, now=now, exclude_ids={ids[5]})] == [ids[0], ids[3]]
    assert ids[2] in [w["id"] for w in db.next_work(limit=10, now=now + 2000)]

    # Already waiting: keeps the more urgent priority.
    db.mark_dirty([ids[1]], priority=4)
    assert db.get_file(ids[1])["priority"] == 2
    db.mark_dirty([ids[1]], priority=1)
    assert db.get_file(ids[1])["priority"] == 1
    # Not waiting: a stale urgent priority from the last run means nothing.
    db.update_file(ids[4], status="indexed", priority=0)
    db.mark_dirty([ids[4]], priority=3)
    assert db.get_file(ids[4])["priority"] == 3
    # A changed file starts over, not from the old content's backoff.
    db.update_file(ids[2], attempts=4)
    db.mark_dirty([ids[2]], priority=2)
    row = db.get_file(ids[2])
    assert row["status"] == "dirty" and row["attempts"] == 0 and row["next_attempt_at"] is None


def test_update_file_compare_and_set_on_queued_at(db):
    fid = db.insert_file("local", "a.txt", ph("a.txt"))["id"]
    picked = db.next_work(limit=1, now=time.time())[0]
    # The file is edited while the worker is busy with the old content.
    db.mark_dirty([fid], priority=1)
    assert db.update_file(fid, if_queued_at=picked["queued_at"], status="indexed") is False
    assert db.get_file(fid)["status"] == "dirty"
    again = db.next_work(limit=1, now=time.time())[0]
    assert db.update_file(fid, if_queued_at=again["queued_at"], status="indexed") is True
    assert db.get_file(fid)["status"] == "indexed"


def test_list_files_filters_and_keyset_pages(db):
    rels = ["b/Báo cáo.docx", "a/one.txt", "a/two.txt", "c/Reports/x.pdf", "d/zed.md"]
    folded = {"b/Báo cáo.docx": "bao cao.docx"}
    for rel in rels:
        name = rel.rsplit("/", 1)[-1]
        db.insert_file("local", rel, ph(rel), name=name, name_folded=folded.get(rel, name.lower()),
                       kind="text" if rel.endswith((".txt", ".md")) else "doc")
    db.insert_file("local", "e/old.txt", ph("e/old.txt"), status="tombstone")

    pages, after = [], None
    while True:
        page = db.list_files(limit=2, after=after)
        if not page:
            break
        pages += [r["rel_path"] for r in page]
        after = (page[-1]["rel_path"], page[-1]["id"])
    assert pages == sorted(rels)

    assert [r["rel_path"] for r in db.list_files(q="báo")] == ["b/Báo cáo.docx"]
    assert [r["rel_path"] for r in db.list_files(q="BAO")] == ["b/Báo cáo.docx"]
    assert [r["rel_path"] for r in db.list_files(q="Reports")] == ["c/Reports/x.pdf"]
    assert [r["rel_path"] for r in db.list_files(q="100%")] == []
    assert len(db.list_files(kind="text")) == 3
    assert [r["rel_path"] for r in db.list_files(status="tombstone")] == ["e/old.txt"]
    assert db.count_by_status() == {"dirty": 5, "tombstone": 1}
    assert db.count_by_kind("local")["doc"] == 2


def test_rename_prefix_moves_the_subtree_only(db, monkeypatch):
    monkeypatch.setattr(db_mod, "_RENAME_BATCH", 2)  # exercise the batching
    for rel, depth in (("Docs", 0), ("Docs/Reports", 1), ("Docs/Reports/2024", 2), ("Docs/Reports-old", 1)):
        db.upsert_folder("local", rel, ph(rel), name=rel.rsplit("/", 1)[-1], depth=depth)
    moved = ["Docs/Reports/a.docx", "Docs/Reports/2024/b.xlsx", "Docs/Reports/2024/c.xlsx", "Docs/Reports/z.txt"]
    stay = ["Docs/Reports-old/c.txt", "Docs/Reports.txt", "Docs/x.txt"]
    for rel in moved + stay:
        db.insert_file("local", rel, ph(rel))
    db.insert_file("drive", "Docs/Reports/a.docx", ph("Docs/Reports/a.docx"))

    n = db.rename_prefix("local", "Docs/Reports", "Archive/2023/Reports", ph)
    assert n == len(moved) + 2  # + the Reports and 2024 folders
    for rel in moved:
        new = "Archive/2023/Reports" + rel[len("Docs/Reports"):]
        row = db.file_by_path("local", ph(new))
        assert row is not None and row["rel_path"] == new
        assert db.file_by_path("local", ph(rel)) is None
    for rel in stay:
        assert db.file_by_path("local", ph(rel))["rel_path"] == rel
    assert db.file_by_path("drive", ph("Docs/Reports/a.docx")) is not None
    assert db.folder_by_path("local", ph("Archive/2023/Reports"))["depth"] == 2
    assert db.folder_by_path("local", ph("Archive/2023/Reports/2024"))["depth"] == 3
    assert db.folder_by_path("local", ph("Docs/Reports-old"))["depth"] == 1

    # A row whose destination is taken stays put for the scan to reconcile.
    db.insert_file("local", "New/a.txt", ph("New/a.txt"))
    db.insert_file("local", "Old/a.txt", ph("Old/a.txt"))
    db.insert_file("local", "Old/b.txt", ph("Old/b.txt"))
    assert db.rename_prefix("local", "Old", "New", ph) == 1
    assert db.file_by_path("local", ph("Old/a.txt")) is not None
    assert db.file_by_path("local", ph("New/b.txt")) is not None
    with pytest.raises(ValueError):
        db.rename_prefix("local", "", "x", ph)


def test_delete_file_returns_chunk_ids_and_clears_its_rows(db):
    fid, ids = add_file(db, "a.txt", ["alpha one", "alpha two"])
    other, _ = add_file(db, "b.txt", ["alpha three"])
    db.cache_put("k1", {"v": 1}, purpose="map_extract", file_id=fid)
    db.cache_put("k2", [1, 2], purpose="rerank", file_id=other)
    assert sorted(db.delete_file(fid)) == sorted(ids)
    assert db.get_file(fid) is None
    assert db.cache_get("k1") is None and db.cache_get("k2") == [1, 2]
    assert len(db.fts_search('"alpha"')) == 1
    assert db.cache_delete_for_files([other]) == 1


# ── Chunks and the diff ────────────────────────────────────────────────────


def test_apply_chunks_keep_add_remove(db):
    fid = db.insert_file("local", "doc.md", ph("doc.md"))["id"]
    first = [mk(0, "alpha intro"), mk(1, "bravo body", refs=[{"raw": "Điều 5"}]), mk(2, "charlie end")]
    ids = db.apply_chunks(file_id=fid, folder_id=None, source="local", diff=ChunkDiff(add=first),
                          file_fields={"status": "indexed", "chunker_version": 3})
    assert len(ids) == 3 and ids == sorted(ids)

    # An edit: a new chunk at the top, "charlie" deleted, the rest shifted.
    new = [mk(0, "delta new"), mk(1, "alpha intro", locator={"line_start": 5}),
           mk(2, "bravo body", locator={"line_start": 9}, refs=[{"raw": "Điều 5"}])]
    added = db.apply_chunks(
        file_id=fid, folder_id=None, source="local",
        diff=ChunkDiff(keep=[(ids[0], new[1]), (ids[1], new[2])], add=[new[0]], remove=[ids[2]]),
    )
    assert len(added) == 1 and added[0] > ids[2]

    old = db.get_chunks(fid)
    assert [(c.id, c.ordinal) for c in old] == [(added[0], 0), (ids[0], 1), (ids[1], 2)]
    assert old[1].locator == {"line_start": 5}
    assert [cid for cid, _ in db.fts_search('"delta"')] == added
    assert db.fts_search('"charlie"') == []
    assert [cid for cid, _ in db.fts_search('"alpha"')] == [ids[0]]

    row = db.get_file(fid)
    assert row["chunk_count"] == 3
    assert row["text_bytes"] == sum(len(t.encode()) for t in ("delta new", "alpha intro", "bravo body"))
    assert row["status"] == "indexed" and row["chunker_version"] == 3

    rows = db.chunk_rows([ids[1], 999999, added[0]])
    assert [r["id"] for r in rows] == [ids[1], added[0]]
    assert rows[0]["refs"] == [{"raw": "Điều 5"}] and rows[0]["locator"] == {"line_start": 9}
    assert rows[0]["file_id"] == fid and rows[1]["vec_gen"] is None


def _fts_snapshot(path: str) -> tuple:
    conn = raw(path)
    try:
        return (
            conn.execute("SELECT id, block FROM chunks_fts_data ORDER BY id").fetchall(),
            conn.execute("SELECT id, sz FROM chunks_fts_docsize ORDER BY id").fetchall(),
        )
    finally:
        conn.close()


def test_moving_a_chunk_writes_nothing_to_the_full_text_index(db, db_path):
    fid = db.insert_file("local", "doc.md", ph("doc.md"))["id"]
    chunks = [mk(i, f"paragraph number {i}", locator={"line_start": i}) for i in range(5)]
    ids = db.apply_chunks(file_id=fid, folder_id=7, source="local", diff=ChunkDiff(add=chunks))

    # Everything shifts down by 3 lines and one position; folder changes too.
    moved = [(cid, mk(i + 1, f"paragraph number {i}", locator={"line_start": i + 3}, section_key=f"s{i}"))
             for i, cid in enumerate(ids)]
    before = _fts_snapshot(db_path)
    trace: list[str] = []
    db._writer.set_trace_callback(trace.append)
    try:
        assert db.apply_chunks(file_id=fid, folder_id=8, source="local", diff=ChunkDiff(keep=moved)) == []
        db.set_vec_gen(ids, 4)
    finally:
        db._writer.set_trace_callback(None)
    assert trace, "the trace callback saw nothing — the check below would prove nothing"
    assert not [s for s in trace if "chunks_fts" in s]
    assert _fts_snapshot(db_path) == before
    assert [c.ordinal for c in db.get_chunks(fid)] == [1, 2, 3, 4, 5]
    assert {r["folder_id"] for r in db.chunk_rows(ids)} == {8}

    # Control: a real text change does reach the FTS index.
    db._writer.set_trace_callback(trace.append)
    try:
        db.apply_chunks(file_id=fid, folder_id=8, source="local", diff=ChunkDiff(add=[mk(9, "brand new")]))
    finally:
        db._writer.set_trace_callback(None)
    assert [s for s in trace if "chunks_fts" in s]


def test_duplicate_chunks_can_swap_occurrences(db):
    fid = db.insert_file("local", "dup.txt", ph("dup.txt"))["id"]
    dup = "the same boilerplate paragraph"
    ids = db.apply_chunks(file_id=fid, folder_id=None, source="local",
                          diff=ChunkDiff(add=[mk(0, dup, occ=0), mk(1, "middle"), mk(2, dup, occ=1)]))
    # Re-diffed so each old row takes the other's occurrence number: a naive
    # row-by-row update would hit UNIQUE(file_id, text_hash, occ).
    db.apply_chunks(file_id=fid, folder_id=None, source="local", diff=ChunkDiff(
        keep=[(ids[2], mk(0, dup, occ=0)), (ids[1], mk(1, "middle")), (ids[0], mk(2, dup, occ=1))]))
    got = {c.id: (c.ordinal, c.occ) for c in db.get_chunks(fid)}
    assert got == {ids[2]: (0, 0), ids[1]: (1, 0), ids[0]: (2, 1)}

    # Removing occurrence 0 while renumbering 1 → 0 and adding a new 1.
    db.apply_chunks(file_id=fid, folder_id=None, source="local", diff=ChunkDiff(
        keep=[(ids[0], mk(0, dup, occ=0))], add=[mk(1, dup, occ=1)], remove=[ids[2], ids[1]]))
    assert sorted((c.ordinal, c.occ) for c in db.get_chunks(fid)) == [(0, 0), (1, 1)]


def test_stale_diff_is_rejected_without_writing(db):
    a, a_ids = add_file(db, "a.txt", ["alpha"])
    b, b_ids = add_file(db, "b.txt", ["bravo"])
    with pytest.raises(LookupError):
        db.apply_chunks(file_id=a, folder_id=None, source="local",
                        diff=ChunkDiff(keep=[(b_ids[0], mk(0, "bravo"))], add=[mk(1, "zulu")]))
    with pytest.raises(LookupError):
        db.apply_chunks(file_id=a, folder_id=None, source="local",
                        diff=ChunkDiff(add=[mk(1, "zulu")], remove=[b_ids[0]]))
    # A valid removal followed by an invalid keep: the removal is rolled back.
    with pytest.raises(LookupError):
        db.apply_chunks(file_id=a, folder_id=None, source="local",
                        diff=ChunkDiff(remove=[a_ids[0]], keep=[(b_ids[0], mk(0, "bravo"))]))
    assert [c.id for c in db.get_chunks(a)] == a_ids
    assert [cid for cid, _ in db.fts_search('"alpha"')] == a_ids
    assert db.fts_search('"zulu"') == []
    assert db.get_file(a)["chunk_count"] == 1 and len(db.get_chunks(b)) == 1
    with pytest.raises(LookupError):
        db.apply_chunks(file_id=424242, folder_id=None, source="local", diff=ChunkDiff(add=[mk(0, "x")]))
    with pytest.raises(ValueError):
        db.apply_chunks(file_id=None, folder_id=None, source="local", diff=ChunkDiff())
    with pytest.raises(ValueError):
        db.apply_chunks(file_id=None, folder_id=1, source="local", diff=ChunkDiff(), file_fields={"status": "x"})


def test_ids_are_never_reused(db):
    fid, ids = add_file(db, "a.txt", ["one", "two", "three"])
    db.delete_chunks([ids[-1]])
    [again] = db.apply_chunks(file_id=fid, folder_id=None, source="local", diff=ChunkDiff(add=[mk(5, "four")]))
    assert again > ids[-1]
    assert db.get_file(fid)["chunk_count"] == 3

    top = db.insert_file("local", "z.txt", ph("z.txt"))["id"]
    db.delete_file(top)
    assert db.insert_file("local", "y.txt", ph("y.txt"))["id"] > top


def test_folder_cards(db):
    folder = db.upsert_folder("local", "Robot", ph("Robot"), name="Robot", is_project=1,
                              project_meta={"languages": {"python": 12}})
    assert db.upsert_folder("local", "Robot", ph("Robot"), file_count=12) == folder
    row = db.get_folder(folder)
    assert row["project_meta"] == {"languages": {"python": 12}} and row["file_count"] == 12
    assert [f["id"] for f in db.list_folders(is_project=True)] == [folder]

    [card] = db.apply_chunks(file_id=None, folder_id=folder, source="local",
                             diff=ChunkDiff(add=[mk(0, "Robot project motion tracking", ctype="folder_card")]))
    fid, body = add_file(db, "Robot/main.py", ["import cv2"])
    db.apply_chunks(file_id=fid, folder_id=folder, source="local",
                    diff=ChunkDiff(keep=[(body[0], mk(0, "import cv2"))]))
    # Body chunks of files in the folder carry its id too, but are not cards.
    assert [c.id for c in db.folder_chunks(folder)] == [card]
    assert db.delete_folders([folder]) == [card]
    assert db.get_folder(folder) is None
    assert db.fts_search('"tracking"') == []
    assert [c.id for c in db.get_chunks(fid)] == body


# ── Lexical search ─────────────────────────────────────────────────────────


def test_fts_search_ranks_and_restricts_to_files(db):
    f1, [c1] = add_file(db, "a.txt", ["robot robot robot tracking"])
    f2, [c2] = add_file(db, "b.txt", ["robot arm and a lot of other words in a longer passage"])
    f3 = db.insert_file("local", "c.txt", ph("c.txt"))["id"]
    [c3] = db.apply_chunks(file_id=f3, folder_id=None, source="local",
                           diff=ChunkDiff(add=[mk(0, "unrelated words entirely", heading="robot")]))
    add_file(db, "d.txt", ["nothing to see"])

    hits = db.fts_search('"robot"')
    assert [cid for cid, _ in hits] == [c1, c2, c3]
    scores = [s for _, s in hits]
    assert all(s > 0 for s in scores) and scores == sorted(scores, reverse=True)

    assert [cid for cid, _ in db.fts_search('"robot"', file_ids=[f2])] == [c2]
    assert db.fts_search('"robot"', file_ids=[]) == []
    many = list(range(100000, 100600)) + [f3, f1]  # > one parameter batch
    assert [cid for cid, _ in db.fts_search('"robot"', file_ids=many)] == [c1, c3]
    assert [cid for cid, _ in db.fts_search('"robot"', limit=1)] == [c1]
    with pytest.raises(ValueError):
        db.fts_search('"unterminated')


def test_vietnamese_with_and_without_diacritics(db):
    text = "Điều 12. Quyền sử dụng đất của hộ gia đình"
    fid = db.insert_file("local", "luat.pdf", ph("luat.pdf"))["id"]
    [vi] = db.apply_chunks(file_id=fid, folder_id=None, source="local", diff=ChunkDiff(add=[
        mk(0, text, heading="Luật Đất đai", folded="luat dat dai dieu 12. quyen su dung dat cua ho gia dinh"),
    ]))
    add_file(db, "en.txt", ["the data shows a clear trend"])

    assert [cid for cid, _ in db.fts_search('text:"đất"')] == [vi]
    assert [cid for cid, _ in db.fts_search('text:"ĐẤT"')] == [vi]  # case folds, accents stay
    assert [cid for cid, _ in db.fts_search('folded:"dat"')] == [vi]
    assert db.fts_search('text:"dat"') == []  # remove_diacritics 0: "dat" is not "đất"
    assert [cid for cid, _ in db.fts_search('text:"quyền sử dụng"')] == [vi]

    assert db.like_search(["đất", "gia đình", "missing"], limit=5) == [(vi, 2.0)]
    assert db.like_search(["dat"], limit=5, file_ids=[fid]) == [(vi, 1.0)]  # via folded
    assert db.like_search([" ", ""], limit=5) == []


# ── Vectors and collections ────────────────────────────────────────────────


def test_vector_bookkeeping(db):
    a, a_ids = add_file(db, "a.txt", ["shared text", "only in a", "also only in a"])
    b, b_ids = add_file(db, "b.txt", ["shared text"])
    need = db.chunks_needing_vectors(gen=1)
    assert [r["id"] for r in need] == a_ids + b_ids
    assert set(need[0]) == {"id", "heading", "text", "text_hash", "ctype", "file_id", "folder_id", "source"}
    assert [r["id"] for r in db.chunks_needing_vectors(gen=1, after_id=a_ids[1], limit=2)] == [a_ids[2], b_ids[0]]

    assert db.set_vec_gen(a_ids, 1) == 3
    assert [r["id"] for r in db.chunks_needing_vectors(gen=1)] == b_ids
    shared = need[0]["text_hash"]
    assert db.reusable_vectors([shared, "nope"], gen=1) == {shared: a_ids[0]}
    assert db.reusable_vectors([shared], gen=2) == {}

    assert db.stats()["chunks_without_vectors"] == 4  # no active collection yet
    db.add_collection(1, "ud_x_1", model_key="me5", dim=768, store_key="qdrant", state="active")
    assert db.stats()["chunks_without_vectors"] == 1
    assert len(db.chunks_needing_vectors(gen=2)) == 4  # a new generation needs everything

    assert db.clear_vec_gen() == 3
    assert len(db.chunks_needing_vectors(gen=1)) == 4
    assert sorted(db.all_chunk_ids_page(0, 10)) == sorted(a_ids + b_ids)
    assert db.all_chunk_ids_page(a_ids[-1], 10) == b_ids


def test_collections_have_one_active_and_gens_are_never_reused(db):
    g1 = db.next_gen()
    db.add_collection(g1, "ud_a_1", model_key="me5", dim=768, store_key="qdrant", state="active")
    g2 = db.next_gen()
    assert g2 == g1 + 1
    db.add_collection(g2, "ud_a_2", model_key="gemma", dim=768, store_key="qdrant", state="building")
    assert db.active_collection()["gen"] == g1 and db.building_collection()["gen"] == g2

    assert db.set_collection_state(g2, "active")
    assert db.active_collection()["gen"] == g2 and db.active_collection()["completed_at"]
    assert {c["gen"]: c["state"] for c in db.list_collections()} == {g1: "retiring", g2: "active"}

    db.delete_collection_row(g2)
    assert db.next_gen() == g2 + 1  # an aborted gen's number never comes back
    _, ids = add_file(db, "a.txt", ["x"])
    db.set_vec_gen(ids, 40)  # even a vec_gen with no collection row is respected
    assert db.next_gen() == 41
    assert db.set_collection_state(999, "retiring") is False


# ── Tasks, activity, cache ─────────────────────────────────────────────────


def test_tasks_dedupe_and_boot_reset(db):
    now = time.time()
    t1 = db.add_task("scan", source="local", priority=3, not_before=now + 100)
    assert db.add_task("scan", source="local", priority=1) == t1
    t = db.get_task(t1)
    assert t["priority"] == 1 and t["not_before"] is None  # the earlier, more urgent request wins
    t3 = db.add_task("scan", source="drive")
    t4 = db.add_task("gc_vectors")
    assert db.add_task("gc_vectors") == t4  # NULL source de-duplicates too
    db.add_task("estimate", source="local", not_before=now + 100)
    assert [d["id"] for d in db.due_tasks(now)] == [t1, t3, t4]

    db.update_task(t1, state="running", checkpoint={"after_id": 42})
    t6 = db.add_task("scan", source="local")
    assert t6 != t1
    assert db.reset_running_tasks() == 1
    assert db.get_task(t6) is None  # folded into the one that has progress
    t = db.get_task(t1)
    assert t["state"] == "queued" and t["checkpoint"] == {"after_id": 42}
    db.delete_task(t1)
    assert db.get_task(t1) is None


def test_activity_is_paged_and_pruned(db, monkeypatch):
    ids = [db.add_activity("indexed", f"file {i}", source="local", file_id=i, detail={"chunks_new": i})
           for i in range(30)]
    newest = db.list_activity(limit=5)
    assert [r["id"] for r in newest] == ids[::-1][:5]
    assert newest[0]["detail"] == {"chunks_new": 29} and newest[0]["level"] == "info"
    assert [r["id"] for r in db.list_activity(before_id=newest[-1]["id"], limit=3)] == ids[::-1][5:8]

    assert db.prune_activity(keep=10) == 20
    assert [r["id"] for r in db.list_activity(limit=100)] == ids[::-1][:10]

    monkeypatch.setattr(db_mod, "_ACTIVITY_PRUNE_EVERY", 5)
    monkeypatch.setattr(db_mod, "_ACTIVITY_KEEP", 7)
    for i in range(40):
        db.add_activity("indexed", f"more {i}")
    assert len(db.list_activity(limit=100)) <= 7 + 5


def test_llm_cache_round_trip(db):
    assert db.cache_get("missing") is None
    db.cache_put("k", {"restored": "Luật Đất đai"}, purpose="diacritics", content_hash="c1")
    assert db.cache_get("k") == {"restored": "Luật Đất đai"}
    db.cache_put("k", "plain", purpose="diacritics")
    assert db.cache_get("k") == "plain"


# ── Purge, maintenance ─────────────────────────────────────────────────────


def test_purge_source_deletes_in_bounded_batches(db, db_path):
    local_ids: list[int] = []
    for i in range(7):
        _, ids = add_file(db, f"l/f{i}.txt", [f"localword {i} {j}" for j in range(10)])
        local_ids += ids
    big_fid, big = add_file(db, "l/big.txt", [f"localword big {j}" for j in range(60)])
    local_ids += big
    folder = db.upsert_folder("local", "l", ph("l"), name="l")
    local_ids += db.apply_chunks(file_id=None, folder_id=folder, source="local",
                                 diff=ChunkDiff(add=[mk(0, "localword card", ctype="folder_card")]))
    # A stray: a chunk whose file row vanished behind the index's back.
    stray_fid, stray = add_file(db, "l/stray.txt", ["localword stray"])
    local_ids += stray
    conn = raw(db_path)
    conn.execute("DELETE FROM files WHERE id = ?", (stray_fid,))
    conn.close()
    _, drive_ids = add_file(db, "Drive/x.doc", ["driveword"], source="drive")
    db.update_source_state("local", state="indexing", drive_cursor="c")
    db.cache_put("k", 1, purpose="rerank", file_id=big_fid)

    batches = list(db.purge_source("local", batch=25))
    assert all(0 < len(b) <= 25 for b in batches)
    flat = [cid for b in batches for cid in b]
    assert len(flat) == len(set(flat)) and set(flat) == set(local_ids)
    assert db.fts_search('"localword"') == []
    assert db.count_by_status("local") == {}
    assert db.list_folders(source="local") == []
    assert db.get_source_state("local")["state"] is None
    assert db.cache_get("k") is None
    assert [cid for cid, _ in db.fts_search('"driveword"')] == drive_ids
    assert db.count_by_status("drive") == {"dirty": 1}
    assert list(db.purge_source("local")) == []


def test_size_shrinks_after_purge_and_incremental_vacuum(db):
    filler = " ".join(f"word{k}" for k in range(150))
    for i in range(60):
        add_file(db, f"f{i}.txt", [f"{filler} {i} {j}" for j in range(15)])
    db.checkpoint("TRUNCATE")
    used_before = db.size_bytes()
    disk_before = db.file_bytes()
    assert used_before > 1_000_000

    for _ in db.purge_source("local"):
        pass
    assert db.freelist_bytes() > 0
    freed = db.incremental_vacuum()
    assert freed > 0 and db.freelist_bytes() == 0
    db.checkpoint("TRUNCATE")
    assert db.size_bytes() < used_before / 4
    assert db.file_bytes() < disk_before / 4
    with pytest.raises(ValueError):
        db.checkpoint("NOPE")


def test_stats(db):
    add_file(db, "a.txt", ["one", "two"], kind="text")
    db.upsert_folder("local", "x", ph("x"))
    s = db.stats()
    assert s["files"] == 1 and s["folders"] == 1 and s["chunks"] == 2
    assert s["text_bytes"] == len("one") + len("two")
    assert s["by_status"] == {"dirty": 1} and s["by_kind"] == {"text": 1}


# ── Concurrency ────────────────────────────────────────────────────────────


def test_one_writer_and_four_readers_never_see_a_lock(db):
    errors: list[BaseException] = []
    stop = threading.Event()
    reads = [0] * 4
    fids: list[int] = []

    def writer():
        try:
            i = 0
            while not stop.is_set():
                fid, ids = add_file(db, f"w/f{i}.txt", [f"alpha {i} {j} tracking" for j in range(20)])
                fids.append(fid)
                keep = [(cid, mk(j + 1, f"alpha {i} {j} tracking")) for j, cid in enumerate(ids[5:], start=5)]
                db.apply_chunks(file_id=fid, folder_id=None, source="local", diff=ChunkDiff(
                    keep=keep, remove=ids[:5], add=[mk(0, f"beta {i} new")]))
                db.set_vec_gen(ids[5:], 1)
                db.add_activity("indexed", f"f{i}")
                i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader(n: int):
        try:
            while not stop.is_set():
                db.fts_search('"alpha" OR "beta"', limit=20)
                if fids:
                    db.get_chunks(fids[-1])
                db.stats()
                db.list_files(limit=10)
                db.next_work(limit=5, now=time.time())
                reads[n] += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    assert not errors, errors
    assert len(fids) > 3 and all(r > 3 for r in reads)
    assert db.stats()["chunks"] == len(fids) * 16
