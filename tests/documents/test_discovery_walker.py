"""The walker, the manifest key and the file readers.

What matters: pruned trees are never entered, a link never leads the walk (or
the agent) outside the root or around layer 1, placeholders come back flagged
and unread, and one file always gets one manifest key however its name was
spelled.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import types
import unicodedata
from pathlib import Path

import pytest

from app.documents.discovery import hashing, walker
from app.documents.discovery.hashing import fs_path, is_placeholder, quick_hash, sha256_file
from app.documents.discovery.ignore import INDEX, METADATA_ONLY, IgnoreMatcher
from app.documents.discovery.walker import fit_i63, path_hash, walk


def _tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _walk(root: Path, **kw) -> dict[str, object]:
    m = IgnoreMatcher(str(root), excludes=kw.pop("excludes", []), locked_excludes=[], include_hidden=kw.pop("include_hidden", False))
    return {e.rel_path: e for e in walk(str(root), m, **kw)}


def test_walk_prunes_skips_and_lists_dirs(tmp_path):
    _tree(tmp_path, {
        "a.txt": "a",
        "docs/report.md": "r",
        "docs/.hidden.md": "h",
        "docs/~$report.docx": "lock",
        "node_modules/pkg/index.js": "x",
        ".git/HEAD": "ref",
        "proj/src/main.py": "print()",
        "keys/server.pem": "-----BEGIN",
    })
    seen = _walk(tmp_path)
    assert set(seen) == {"a.txt", "docs", "docs/report.md", "proj", "proj/src", "proj/src/main.py", "keys", "keys/server.pem"}
    assert seen["docs"].is_dir and not seen["a.txt"].is_dir
    assert seen["keys/server.pem"].disposition == METADATA_ONLY
    assert seen["a.txt"].disposition == INDEX
    assert seen["a.txt"].size == 1 and seen["a.txt"].mtime_ns > 0
    assert seen["a.txt"].abs_path == str(tmp_path / "a.txt")
    assert "/" in next(k for k in seen if k.startswith("proj/src/"))


def test_walk_yields_real_inode_and_birthtime(tmp_path):
    _tree(tmp_path, {"a.txt": "a"})
    (entry,) = [e for e in _walk(tmp_path).values() if not e.is_dir]
    st = os.stat(tmp_path / "a.txt")
    assert entry.ino == fit_i63(st.st_ino) and entry.ino != 0  # Windows needs the extra stat
    assert entry.dev == fit_i63(st.st_dev)
    if sys.platform.startswith("linux"):
        assert entry.birthtime is None
    else:
        assert entry.birthtime is not None


def test_walk_order_is_deterministic(tmp_path):
    # A folder's entries in name order, then its subfolders in name order.
    _tree(tmp_path, {"b/2.txt": "", "a/1.txt": "", "c.txt": ""})
    m = IgnoreMatcher(str(tmp_path), excludes=[], locked_excludes=[])
    assert [e.rel_path for e in walk(str(tmp_path), m)] == ["a", "b", "c.txt", "a/1.txt", "b/2.txt"]


def test_hidden_included_on_request(tmp_path):
    _tree(tmp_path, {".notes/today.md": "x"})
    assert ".notes/today.md" in _walk(tmp_path, include_hidden=True)
    assert ".notes/today.md" not in _walk(tmp_path)


def test_bundle_is_one_metadata_only_entry(tmp_path):
    _tree(tmp_path, {"Apps/Tool.app/Contents/Info.plist": "<plist/>", "Apps/Tool.app/Contents/MacOS/tool": "bin"})
    seen = _walk(tmp_path)
    assert set(seen) == {"Apps", "Apps/Tool.app"}
    bundle = seen["Apps/Tool.app"]
    assert not bundle.is_dir and bundle.disposition == METADATA_ONLY


def test_max_entries_and_stop(tmp_path):
    _tree(tmp_path, {f"f{i:02d}.txt": "" for i in range(20)})
    m = IgnoreMatcher(str(tmp_path), excludes=[], locked_excludes=[])
    assert len(list(walk(str(tmp_path), m, max_entries=5))) == 5
    stop = threading.Event()
    stop.set()
    assert list(walk(str(tmp_path), m, stop=stop)) == []


def test_unlistable_directory_is_reported_not_fatal(tmp_path, monkeypatch):
    _tree(tmp_path, {"ok/a.txt": "a", "locked/b.txt": "b"})
    real_scandir = os.scandir

    def scandir(path):
        if str(path).endswith("locked"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    errors: list[tuple[str, BaseException]] = []
    m = IgnoreMatcher(str(tmp_path), excludes=[], locked_excludes=[])
    rels = [e.rel_path for e in walk(str(tmp_path), m, on_error=lambda r, e: errors.append((r, e)))]
    assert "ok/a.txt" in rels and "locked" in rels and "locked/b.txt" not in rels
    assert [r for r, _ in errors] == ["locked"]
    assert isinstance(errors[0][1], PermissionError)


def _symlink(link: Path, target: Path, *, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:  # Windows without Developer Mode
        pytest.skip(f"cannot create symlinks here: {exc}")


def test_symlinks_never_escape_the_root(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    _tree(root, {"real.txt": "in", ".ssh/id_rsa": "KEY", "docs/a.txt": "a"})
    _tree(outside, {"secret.txt": "out", "sub/x.txt": "x"})
    _symlink(root / "escape.txt", outside / "secret.txt")
    _symlink(root / "inside.txt", root / "real.txt")
    _symlink(root / "sneaky.txt", root / ".ssh" / "id_rsa")
    _symlink(root / "dirlink", outside / "sub", is_dir=True)
    _symlink(root / "loop", root, is_dir=True)
    seen = _walk(root)
    assert "escape.txt" not in seen
    assert "sneaky.txt" not in seen  # layer 1 cannot be bypassed through a link
    assert "dirlink" not in seen and "dirlink/x.txt" not in seen
    assert "loop" not in seen
    assert seen["inside.txt"].size == len("in")


class _FakeLinkEntry:
    """A DirEntry for a symlink, for hosts where creating one needs privileges."""

    def __init__(self, *, to_dir: bool = False, to_file: bool = True):
        self._to_dir, self._to_file = to_dir, to_file

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return self._to_dir

    def is_file(self, follow_symlinks: bool = True) -> bool:
        return self._to_file


def test_link_rules_without_real_symlinks(tmp_path, monkeypatch):
    root, outside = tmp_path / "root", tmp_path / "outside"
    _tree(root, {"real.txt": "in", ".ssh/id_rsa": "KEY", "escape.txt": "", "inside.txt": "", "sneaky.txt": ""})
    _tree(outside, {"secret.txt": "out"})
    root_real = walker._norm_real(str(root))
    targets = {
        str(root / "escape.txt"): str(outside / "secret.txt"),
        str(root / "inside.txt"): str(root / "real.txt"),
        str(root / "sneaky.txt"): str(root / ".ssh" / "id_rsa"),
    }
    real_realpath = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p, **k: targets.get(str(p)) or real_realpath(p, **k))
    m = IgnoreMatcher(str(root), excludes=[], locked_excludes=[])

    def visit(name, entry=None):
        return walker._visit_link(entry or _FakeLinkEntry(), name, str(root / name), name, m, root_real)

    assert visit("escape.txt") is None
    assert visit("sneaky.txt") is None
    kept = visit("inside.txt")
    assert kept is not None and kept.rel_path == "inside.txt" and kept.disposition == INDEX
    assert visit("inside.txt", _FakeLinkEntry(to_dir=True)) is None  # a link to a directory
    assert visit("inside.txt", _FakeLinkEntry(to_file=False)) is None  # dangling


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_junctions_are_not_followed(tmp_path):
    import _winapi

    root, outside = tmp_path / "root", tmp_path / "outside"
    _tree(root, {"a.txt": "a"})
    _tree(outside, {"x.txt": "x"})
    _winapi.CreateJunction(str(outside), str(root / "junction"))
    assert set(_walk(root)) == {"a.txt"}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a filesystem that stores raw bytes")
def test_undecodable_names_are_reported_and_skipped(tmp_path):
    bad = os.fsencode(str(tmp_path)) + b"/bad\xff.txt"
    with open(bad, "wb") as fh:
        fh.write(b"x")
    _tree(tmp_path, {"good.txt": "g"})
    errors = []
    m = IgnoreMatcher(str(tmp_path), excludes=[], locked_excludes=[])
    rels = [e.rel_path for e in walk(str(tmp_path), m, on_error=lambda r, e: errors.append(r))]
    assert rels == ["good.txt"]
    assert len(errors) == 1


def test_icloud_stub_is_a_placeholder(tmp_path):
    _tree(tmp_path, {"Docs/.Report.pdf.icloud": "stub"})
    e = _walk(tmp_path)["Docs/.Report.pdf.icloud"]
    assert e.placeholder and e.disposition == METADATA_ONLY


def test_placeholders_are_flagged_from_stat(tmp_path, monkeypatch):
    _tree(tmp_path, {"cloud.docx": "x", "local.docx": "y"})
    monkeypatch.setattr(walker, "is_placeholder", lambda name, st: name == "cloud.docx")
    seen = _walk(tmp_path)
    assert seen["cloud.docx"].placeholder and seen["cloud.docx"].disposition == METADATA_ONLY
    assert not seen["local.docx"].placeholder and seen["local.docx"].disposition == INDEX


@pytest.mark.parametrize("fields,expected", [
    ({"st_file_attributes": 0x400000}, True),   # RECALL_ON_DATA_ACCESS
    ({"st_file_attributes": 0x40000}, True),    # RECALL_ON_OPEN
    ({"st_file_attributes": 0x1000}, True),     # OFFLINE
    ({"st_file_attributes": 0x20}, False),      # ARCHIVE
    ({"st_flags": 0x40000000}, True),           # SF_DATALESS
    ({"st_flags": 0x8000}, False),
    ({}, False),
])
def test_is_placeholder_bits(fields, expected):
    assert is_placeholder("f.docx", types.SimpleNamespace(**fields)) is expected


def test_hashing_never_reads_a_placeholder(tmp_path, monkeypatch):
    p = tmp_path / "cloud.bin"
    p.write_bytes(b"data")
    monkeypatch.setattr(hashing, "is_placeholder", lambda name, st: True)
    opened = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda *a, **k: opened.append(a[0]) or real_open(*a, **k))
    assert sha256_file(str(p)) is None
    assert quick_hash(str(p), 4) is None
    assert opened == []


# ── path_hash ──────────────────────────────────────────────────────────────


def test_path_hash_is_nfc_and_separator_insensitive():
    nfc = unicodedata.normalize("NFC", "Tài liệu/báo cáo.docx")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert path_hash(nfc) == path_hash(nfd) == path_hash(nfc.replace("/", "\\"))
    assert len(path_hash(nfc)) == 32
    expected = hashlib.blake2b(nfc.lower().encode(), digest_size=16).hexdigest()
    assert path_hash(nfc, case_insensitive=True) == expected


def test_path_hash_case_follows_the_platform():
    assert path_hash("A/Report.PDF", case_insensitive=True) == path_hash("a/report.pdf", case_insensitive=True)
    assert path_hash("A/Report.PDF", case_insensitive=False) != path_hash("a/report.pdf", case_insensitive=False)
    ci = os.name == "nt" or sys.platform == "darwin"
    assert (path_hash("X.txt") == path_hash("x.txt")) is ci


def test_path_hash_survives_surrogates():
    assert len(path_hash("bad\udcff.txt")) == 32


def test_fit_i63():
    assert fit_i63(0) == 0 and fit_i63(None) == 0 and fit_i63(12345) == 12345
    big = (1 << 127) + 99
    assert 0 <= fit_i63(big) < (1 << 63)
    assert fit_i63(big) == fit_i63(big) and fit_i63(big) != fit_i63(big + 1)


# ── hashing ────────────────────────────────────────────────────────────────


def test_sha256_file_streams_and_stops(tmp_path):
    p = tmp_path / "f.bin"
    data = os.urandom(3 * 1024 + 7)
    p.write_bytes(data)
    assert sha256_file(str(p), block=1024) == hashlib.sha256(data).hexdigest()
    stop = threading.Event()
    stop.set()
    assert sha256_file(str(p), stop=stop) is None
    assert sha256_file(str(tmp_path / "missing")) is None
    assert sha256_file(str(tmp_path)) is None  # a directory


def test_quick_hash_reads_head_and_tail(tmp_path):
    p = tmp_path / "big.bin"
    edge = hashing.QUICK_HASH_EDGE
    data = b"A" * edge + b"middle" + b"Z" * edge
    p.write_bytes(data)
    expected = hashlib.sha256(str(len(data)).encode() + data[:edge] + data[-edge:]).hexdigest()
    assert quick_hash(str(p), len(data)) == expected
    # The middle is not part of it: changing it keeps the hash.
    p.write_bytes(b"A" * edge + b"MIDDLE" + b"Z" * edge)
    assert quick_hash(str(p), len(data)) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH")
def test_long_windows_paths_walk_and_hash(tmp_path):
    deep = str(tmp_path)
    while len(deep) < 300:
        deep = os.path.join(deep, "a_rather_long_folder_name")
    os.makedirs(fs_path(deep))
    target = os.path.join(deep, "report.txt")
    with open(fs_path(target), "w", encoding="utf-8") as fh:
        fh.write("deep")
    files = [e for e in _walk(tmp_path).values() if not e.is_dir]
    assert len(files) == 1 and files[0].abs_path == target and files[0].size == 4
    assert sha256_file(target) == hashlib.sha256(b"deep").hexdigest()
