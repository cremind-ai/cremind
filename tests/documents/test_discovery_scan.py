"""scan_diff and move matching: new, changed, missing, moved — and never a false "missing".

A rename must come back as a move (the index keeps its chunks, captions and
citations), a copy as a new file, an edit as a change, and an incomplete walk
must never claim that anything disappeared.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

from app.documents.discovery.ignore import IgnoreMatcher
from app.documents.discovery.moves import match_moves
from app.documents.discovery.hashing import sha256_file
from app.documents.discovery.scan import scan_diff
from app.documents.discovery.walker import path_hash, walk
from app.documents.types import FsEntry, ManifestRow


def _write(root: Path, rel: str, text: str) -> Path:
    p = root.joinpath(*rel.split("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _matcher(root: Path, **kw) -> IgnoreMatcher:
    return IgnoreMatcher(str(root), excludes=kw.get("excludes", []), locked_excludes=[])


def _index(root: Path, **kw) -> dict[str, ManifestRow]:
    """What the index would hold after a full sync of ``root`` right now."""
    manifest: dict[str, ManifestRow] = {}
    for i, e in enumerate(walk(str(root), _matcher(root, **kw)), start=1):
        if e.is_dir:
            continue
        manifest[path_hash(e.rel_path)] = ManifestRow(
            id=i, path_hash=path_hash(e.rel_path), rel_path=e.rel_path, size=e.size,
            mtime_ns=e.mtime_ns, ino=e.ino, dev=e.dev, sha256=sha256_file(e.abs_path),
        )
    return manifest


def _rels(entries) -> set[str]:
    return {e.rel_path for e in entries}


def test_unchanged_tree_reports_nothing(tmp_path):
    _write(tmp_path, "a.txt", "a")
    _write(tmp_path, "d/b.txt", "b")
    res = scan_diff(str(tmp_path), _matcher(tmp_path), _index(tmp_path))
    assert (res.new, res.changed, res.missing, res.moves) == ([], [], [], [])
    assert res.seen_files == 2 and _rels(res.dirs) == {"d"} and not res.truncated


def test_rename_is_a_move_copy_is_new_edit_is_changed_delete_is_missing(tmp_path):
    _write(tmp_path, "rename_me.txt", "moving content")
    _write(tmp_path, "copy_me.txt", "copied content")
    edit = _write(tmp_path, "edit_me.txt", "v1")
    _write(tmp_path, "delete_me.txt", "bye")
    manifest = _index(tmp_path)

    os.rename(tmp_path / "rename_me.txt", tmp_path / "sub_renamed.txt")
    shutil.copy2(tmp_path / "copy_me.txt", tmp_path / "copy_of.txt")
    edit.write_text("version two", encoding="utf-8")
    os.remove(tmp_path / "delete_me.txt")

    res = scan_diff(str(tmp_path), _matcher(tmp_path), manifest)
    assert [(row.rel_path, e.rel_path) for row, e in res.moves] == [("rename_me.txt", "sub_renamed.txt")]
    assert _rels(res.new) == {"copy_of.txt"}
    assert [row.rel_path for row, _ in res.changed] == ["edit_me.txt"]
    assert [row.rel_path for row in res.missing] == ["delete_me.txt"]


def test_move_across_folders_matched_by_content_when_inode_differs(tmp_path):
    _write(tmp_path, "a/report.txt", "quarterly numbers")
    manifest = _index(tmp_path)
    # Copy + delete: a new inode, same content (as a cross-device move looks).
    (tmp_path / "b").mkdir()
    shutil.copyfile(tmp_path / "a" / "report.txt", tmp_path / "b" / "report.txt")
    os.remove(tmp_path / "a" / "report.txt")
    res = scan_diff(str(tmp_path), _matcher(tmp_path), manifest)
    assert [(row.rel_path, e.rel_path) for row, e in res.moves] == [("a/report.txt", "b/report.txt")]
    assert res.new == [] and res.missing == []


def test_ambiguous_content_is_not_matched(tmp_path):
    _write(tmp_path, "one.txt", "same")
    _write(tmp_path, "two.txt", "same")
    manifest = _index(tmp_path)
    for name in ("one.txt", "two.txt"):
        shutil.copyfile(tmp_path / name, tmp_path / f"new_{name}")
        os.remove(tmp_path / name)
    res = scan_diff(str(tmp_path), _matcher(tmp_path), manifest)
    assert res.moves == []
    assert _rels(res.new) == {"new_one.txt", "new_two.txt"}
    assert {r.rel_path for r in res.missing} == {"one.txt", "two.txt"}


def test_truncated_scan_claims_nothing_missing(tmp_path):
    for i in range(10):
        _write(tmp_path, f"f{i}.txt", str(i))
    manifest = _index(tmp_path)
    res = scan_diff(str(tmp_path), _matcher(tmp_path), manifest, max_entries=4)
    assert res.truncated and not res.stopped
    assert res.seen_files == 4 and res.missing == [] and res.moves == []


def test_stopped_scan_is_marked(tmp_path):
    _write(tmp_path, "a.txt", "a")
    stop = threading.Event()
    stop.set()
    res = scan_diff(str(tmp_path), _matcher(tmp_path), _index(tmp_path), stop=stop)
    assert res.truncated and res.stopped and res.missing == []


def test_unreadable_directory_protects_its_rows(tmp_path, monkeypatch):
    _write(tmp_path, "ok/a.txt", "a")
    _write(tmp_path, "flaky/b.txt", "b")
    _write(tmp_path, "gone.txt", "g")
    manifest = _index(tmp_path)
    os.remove(tmp_path / "gone.txt")
    real_scandir = os.scandir

    def scandir(path):
        if str(path).endswith("flaky"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    res = scan_diff(str(tmp_path), _matcher(tmp_path), manifest)
    assert [r.rel_path for r in res.missing] == ["gone.txt"]  # flaky/b.txt is unknown, not gone
    assert any(line.startswith("flaky") for line in res.errors)


def test_newly_excluded_rows_are_excluded_not_missing(tmp_path):
    _write(tmp_path, "keep.txt", "k")
    _write(tmp_path, "videos/a.mp4", "v")
    manifest = _index(tmp_path)
    res = scan_diff(str(tmp_path), _matcher(tmp_path, excludes=[{"pattern": "videos", "type": "dir", "mode": "skip"}]), manifest)
    assert [r.rel_path for r in res.excluded] == ["videos/a.mp4"]
    assert res.missing == []


def test_cremindignore_is_reread_on_every_scan(tmp_path):
    _write(tmp_path, "a.log", "x")
    m = _matcher(tmp_path)
    assert _rels(scan_diff(str(tmp_path), m, {}).new) == {"a.log"}
    _write(tmp_path, ".cremindignore", "*.log\n")
    assert scan_diff(str(tmp_path), m, {}).new == []


def test_returned_rows_are_reported(tmp_path):
    _write(tmp_path, "back.txt", "b")
    manifest = _index(tmp_path)
    row = next(iter(manifest.values()))
    row.status = "missing"
    res = scan_diff(str(tmp_path), _matcher(tmp_path), manifest)
    assert [(r.rel_path, e.rel_path) for r, e in res.returned] == [("back.txt", "back.txt")]
    assert res.changed == [] and res.new == []


def test_case_only_rename_on_case_insensitive_fs(tmp_path):
    _write(tmp_path, "Report.txt", "r")
    manifest = _index(tmp_path)
    os.rename(tmp_path / "Report.txt", tmp_path / "tmp.txt")
    os.rename(tmp_path / "tmp.txt", tmp_path / "report.txt")
    res = scan_diff(str(tmp_path), _matcher(tmp_path), manifest)
    assert [(r.rel_path, e.rel_path) for r, e in res.moves] == [("Report.txt", "report.txt")]
    assert res.missing == [] and res.new == []


def test_progress_callback(tmp_path):
    for i in range(3):
        _write(tmp_path, f"{i}.txt", "x")
    calls: list[int] = []
    scan_diff(str(tmp_path), _matcher(tmp_path), {}, on_progress=calls.append)
    assert calls and calls[-1] == 3


# ── match_moves directly ───────────────────────────────────────────────────


def _row(i, rel, size=10, mtime=1, ino=0, dev=0, sha=None):
    return ManifestRow(id=i, path_hash=path_hash(rel), rel_path=rel, size=size, mtime_ns=mtime, ino=ino, dev=dev, sha256=sha)


def _entry(rel, size=10, mtime=1, ino=0, dev=0, disposition="index", placeholder=False):
    return FsEntry(rel_path=rel, abs_path="/x/" + rel, is_dir=False, size=size, mtime_ns=mtime, ino=ino, dev=dev,
                   disposition=disposition, placeholder=placeholder)


def test_inode_fast_path_needs_no_hashing():
    hashed: list[str] = []
    pairs = match_moves([_row(7, "a", ino=42, dev=1)], [_entry("b", ino=42, dev=1)], hasher=lambda p: hashed.append(p))
    assert [(rid, e.rel_path) for rid, e in pairs] == [(7, "b")]
    assert hashed == []


def test_content_path_hashes_only_same_size_candidates():
    hashed: list[str] = []

    def hasher(p):
        hashed.append(p)
        return "h1"

    pairs = match_moves(
        [_row(1, "old", size=10, sha="h1"), _row(2, "nohash", size=99)],
        [_entry("new", size=10), _entry("other", size=11), _entry("big", size=99)],
        hasher=hasher,
    )
    assert [(rid, e.rel_path) for rid, e in pairs] == [(1, "new")]
    assert hashed == ["/x/new"]  # "other": no row of that size; "big": its row has no hash


def test_non_index_and_placeholder_entries_are_never_hashed():
    hashed: list[str] = []
    rows = [_row(1, "a", size=10, sha="h")]
    entries = [_entry("secret.pem", size=10, disposition="metadata_only"), _entry("cloud", size=10, placeholder=True)]
    assert match_moves(rows, entries, hasher=lambda p: hashed.append(p) or "h") == []
    assert hashed == []


def test_each_row_matched_once_and_inode_ambiguity_refused():
    rows = [_row(1, "a", ino=5), _row(2, "b", ino=5)]  # two rows claim one inode key
    assert match_moves(rows, [_entry("c", ino=5)], hasher=lambda p: None) == []
    rows = [_row(1, "a", ino=5)]
    assert match_moves(rows, [_entry("c", ino=5), _entry("d", ino=5)], hasher=lambda p: None) == []


def test_empty_files_are_never_content_matched():
    rows = [_row(1, "a", size=0, sha="e3b0")]
    assert match_moves(rows, [_entry("b", size=0)], hasher=lambda p: "e3b0") == []
