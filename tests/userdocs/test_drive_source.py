"""The Google Drive source (PR7): sync, holds and the per-file pipeline.

The Drive client is an in-memory fake with the client contract's shapes
(``Listing``, ``ChangePage``, ``Content``, errors carrying a ``kind``); the
index is a real :class:`~app.userdocs.index.IndexDB`; the runtime is a stub
whose ``index_content`` writes a file card and the body the way the engine
does. Clock, token file, citations and the admin gate are module seams.
"""

from __future__ import annotations

import copy
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.userdocs import types as t
from app.userdocs.index import IndexDB
from app.userdocs.progress import SyncProgress
from app.userdocs.sources import drive as drv
from app.userdocs.sources.drive import DriveSource, drive_key, drive_rel_path, is_whole_drive

MIME_DOC = "application/vnd.google-apps.document"
MIME_FORM = "application/vnd.google-apps.form"
MIME_FOLDER = drv.MIME_FOLDER
MIME_SHORTCUT = drv.MIME_SHORTCUT
FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
WHOLE_SCOPE = "https://www.googleapis.com/auth/drive"
DAY = 86400.0
_T0 = datetime(2025, 3, 1, tzinfo=timezone.utc)


# ── the fake Drive ──────────────────────────────────────────────────────────


class FakeError(Exception):
    def __init__(self, kind: str, message: str = "", status: int | None = None):
        super().__init__(message or kind)
        self.kind = kind
        self.status = status
        self.reason = None


@dataclass
class Listing:
    files: list[dict[str, Any]]
    complete: bool = True
    reason: str | None = None
    missing: list[str] = field(default_factory=list)


@dataclass
class ChangePage:
    changes: list[dict[str, Any]]
    drive_removals: list[str]
    resume_token: str
    last: bool


@dataclass
class Content:
    data: bytes
    ext: str
    export_mime: str | None
    kind: str | None


class FakeDrive:
    def __init__(self, ident: str | None):
        self.ident = ident
        self.identities: list[str | None] = []
        self.items: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}
        self.log: list[dict[str, Any]] = []
        self.removals: dict[int, list[str]] = {}
        self.calls: list[tuple] = []
        self.fail: dict[str, Exception] = {}
        self.fail_ids: dict[tuple[str, str], Exception] = {}
        self.fail_page: int | None = None
        self.page_size = 100
        self.complete = True
        self.on_list = None
        self.tick = 0

    # mutations (each logs a change, like Drive's feed)

    def _stamp(self) -> str:
        self.tick += 1
        return (_T0 + timedelta(seconds=self.tick)).isoformat().replace("+00:00", ".000Z")

    def _log(self, fid: str, *, removed: bool = False) -> None:
        self.log.append({"fileId": fid, "removed": removed,
                         "file": None if removed else copy.deepcopy(self.items[fid])})

    def put(self, fid, name, *, parents=("root",), mime="text/plain", data=b"", **extra):
        stamp = self._stamp()
        meta = {
            "id": fid, "name": name, "mimeType": mime, "parents": list(parents),
            "modifiedTime": stamp, "createdTime": stamp, "version": "1",
            "webViewLink": f"https://drive.google.com/file/d/{fid}/view",
            "capabilities": {"canDownload": True}, **extra,
        }
        if mime != MIME_FOLDER and not mime.startswith(drv.NATIVE_PREFIX):
            meta["md5Checksum"] = hashlib.md5(data).hexdigest()
            meta["size"] = str(len(data))
        self.items[fid] = meta
        self.blobs[fid] = data
        self._log(fid)
        return meta

    def folder(self, fid, name, parents=("root",)):
        return self.put(fid, name, parents=parents, mime=MIME_FOLDER)

    def _bump(self, fid: str) -> dict[str, Any]:
        meta = self.items[fid]
        meta["version"] = str(int(meta["version"]) + 1)
        return meta

    def edit(self, fid, data: bytes) -> None:
        meta = self._bump(fid)
        self.blobs[fid] = data
        meta["modifiedTime"] = self._stamp()
        if "md5Checksum" in meta:
            meta["md5Checksum"] = hashlib.md5(data).hexdigest()
            meta["size"] = str(len(data))
        self._log(fid)

    def touch(self, fid) -> None:
        self._bump(fid)["modifiedTime"] = self._stamp()
        self._log(fid)

    def rename(self, fid, name) -> None:
        self._bump(fid)["name"] = name
        self._log(fid)

    def move(self, fid, parents) -> None:
        self._bump(fid)["parents"] = list(parents)
        self._log(fid)

    def trash(self, fid) -> None:
        self._bump(fid)["trashed"] = True
        self._log(fid)

    def delete(self, fid, *, log: bool = True) -> None:
        self.items.pop(fid, None)
        if log:
            self._log(fid, removed=True)

    def drop_drive(self, drive_id: str) -> None:
        self.removals.setdefault(len(self.log), []).append(drive_id)
        for fid in [k for k, m in self.items.items() if m.get("driveId") == drive_id]:
            self.items.pop(fid)

    # the client surface

    _API = ("about", "start_page_token", "list_all", "list_folders_tree", "get", "changes", "fetch")

    def down(self, kind: str) -> None:
        """Every call fails the same way — what a revoked grant or an outage
        looks like from here."""
        for method in self._API:
            self.fail[method] = FakeError(kind)

    def up(self) -> None:
        self.fail.clear()

    def _check(self, method: str, fid: str | None = None) -> None:
        err = self.fail.get(method) or (self.fail_ids.get((method, fid)) if fid else None)
        if err is not None:
            raise err

    def identity(self):
        self.calls.append(("identity",))
        if self.identities:
            return self.identities.pop(0)
        return self.ident

    def about(self):
        self.calls.append(("about",))
        self._check("about")
        return {"user": {"permissionId": "p1", "emailAddress": "x@example.com"}}

    def start_page_token(self):
        self.calls.append(("start_page_token",))
        self._check("start_page_token")
        return str(len(self.log))

    def _visible(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(m) for m in self.items.values() if not m.get("trashed")]

    def list_all(self, *, q="trashed = false", max_files=50_000):
        self.calls.append(("list_all",))
        self._check("list_all")
        files = self._visible()
        if self.on_list:
            self.on_list()
        return Listing(files=files, complete=self.complete, reason=None if self.complete else "truncated")

    def list_folders_tree(self, folder_ids, *, max_files=50_000):
        self.calls.append(("list_folders_tree", tuple(folder_ids)))
        self._check("list_folders_tree")
        out, frontier, seen = [], [], set()
        for fid in folder_ids:
            m = self.items.get(fid)
            if m and not m.get("trashed"):
                out.append(copy.deepcopy(m))
                seen.add(fid)
                if m["mimeType"] == MIME_FOLDER:
                    frontier.append(fid)
        while frontier:
            p = frontier.pop()
            for m in self._visible():
                if p in m.get("parents", []) and m["id"] not in seen:
                    seen.add(m["id"])
                    out.append(m)
                    if m["mimeType"] == MIME_FOLDER:
                        frontier.append(m["id"])
        return Listing(files=out, complete=self.complete)

    def get(self, file_id):
        self.calls.append(("get", file_id))
        self._check("get", file_id)
        if file_id not in self.items:
            raise FakeError("not_found", status=404)
        return copy.deepcopy(self.items[file_id])

    def changes(self, page_token):
        self.calls.append(("changes", page_token))
        self._check("changes")
        pos, n, page = int(page_token), len(self.log), 0
        while True:
            if self.fail_page is not None and page == self.fail_page:
                raise FakeError("unreachable")
            end = min(n, pos + self.page_size)
            last = end >= n
            removals = [d for i, ds in self.removals.items() if pos <= i < end or (last and i == n) for d in ds]
            yield ChangePage([copy.deepcopy(e) for e in self.log[pos:end]], removals, str(end), last)
            if last:
                return
            pos, page = end, page + 1

    def fetch_content(self, meta, *, max_bytes):
        fid = meta["id"]
        self.calls.append(("fetch", fid))
        self._check("fetch", fid)
        if fid not in self.items:
            raise FakeError("not_found", status=404)
        mime = meta.get("mimeType") or ""
        if mime == MIME_DOC:
            return Content(self.blobs[fid], ".md", "text/markdown", t.KIND_MARKDOWN)
        if mime.startswith(drv.NATIVE_PREFIX):
            return None
        data = self.blobs[fid]
        if len(data) > max_bytes:
            raise FakeError("too_large")
        return Content(data, "", None, None)

    def close(self):
        self.calls.append(("close",))

    def called(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]


# ── the stub runtime ────────────────────────────────────────────────────────


class StubService:
    def __init__(self):
        self.requests: list[t.ExtractRequest] = []
        self.cap = 100 * 1024 * 1024
        self.results: dict[str, t.ExtractResult] = {}

    def extract(self, req, *, size):
        self.requests.append(req)
        if req.name in self.results:
            return self.results[req.name]
        text = (req.data or b"").decode("utf-8", "replace")
        blocks = [t.Block(text=p, locator={"para": [i + 1, i + 1]})
                  for i, p in enumerate(x for x in text.split("\n\n") if x.strip())]
        return t.ExtractResult(status=t.EXTRACT_OK, kind=req.kind, blocks=blocks)

    def max_file_bytes(self):
        return self.cap

    def extract_limits(self):
        return {"max_pages": 500}

    def wake(self):
        pass

    def wake_embedder(self):
        pass


class StubRuntime:
    """What DriveSource asks of ProfileRuntime; ``index_content`` does what
    the engine's content tail does, minus captions and OCR."""

    def __init__(self, profile: str, db: IndexDB, service: StubService):
        self.profile = profile
        self.db = db
        self.service = service
        self.progress = SyncProgress(profile, publish=lambda p: None)
        self.paused_user = False
        self.level = "ok"
        self.vector_deletes: list[int] = []
        self.queued: list[tuple[int, str]] = []
        self.index_calls: list[dict[str, Any]] = []

    def ensure_db(self):
        return self.db

    def queue_vector_deletes(self, ids):
        self.vector_deletes.extend(int(i) for i in ids)

    def note_queued(self, n, label):
        self.queued.append((n, label))

    def index_content(self, *, row, name, rel, kind, mime, sha, size, result, status, reason, error, image,
                      exif, image_bytes, taken_ts, created_ts, extra, source, drive_fields):
        from app.userdocs.chunking import CHUNKER_VERSION, chunk_blocks, diff_chunks, make_file_card
        from app.userdocs.extract import EXTRACTOR_VERSION
        from app.userdocs.runtime import iso_local

        self.index_calls.append(dict(
            row=row, name=name, rel=rel, kind=kind, mime=mime, sha=sha, size=size, result=result,
            status=status, reason=reason, error=error, image=image, exif=exif, image_bytes=image_bytes,
            taken_ts=taken_ts, created_ts=created_ts, extra=extra, source=source, drive_fields=drive_fields,
        ))
        fid = int(row["id"])
        body = chunk_blocks(result.blocks) if (result is not None and status == "indexed") else []
        card = make_file_card(
            name=name, rel_path=rel, kind=kind, size=int(size or 0),
            mtime_iso=iso_local(drive_fields.get("mtime")) or "",
            summary_text=body[0].text if body else None, extra=extra,
        )
        diff = diff_chunks(self.db.get_chunks(fid), [card] + body)
        fields = {
            "kind": kind, "mime": mime, "sha256": sha, "exif": exif, "taken_at": taken_ts,
            "doc_created_at": created_ts, "extractor_version": EXTRACTOR_VERSION,
            "chunker_version": CHUNKER_VERSION, "size": size, **drive_fields,
        }
        self.db.apply_chunks(file_id=fid, folder_id=row.get("folder_id"), source=source, diff=diff,
                             file_fields=fields)
        self.queue_vector_deletes(diff.remove)
        self.db.update_file(fid, if_queued_at=row.get("queued_at"), status=status, status_reason=reason,
                            error=error, attempts=0, next_attempt_at=None, indexed_at=time.time())
        if status == "metadata_only":
            return "metadata_only", f"{name} indexed by name and details only ({reason})", reason
        return ("added" if not row.get("sha256") else "updated"), f"{name} indexed", None


class Clock:
    def __init__(self):
        self.t = 1_760_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class World:
    def __init__(self, tmp_path: Path, profile: str, ident: str, state: SimpleNamespace):
        self.profile = profile
        self.state = state
        self.clock = state.clock
        self.db = IndexDB.open(str(tmp_path / profile / "index.db"), profile_uid=f"uid-{profile}")
        self.fake = FakeDrive(ident)
        self.service = StubService()
        self.rt = StubRuntime(profile, self.db, self.service)
        self.stops: list[Any] = []
        self.src = DriveSource(self.rt, client_factory=self._factory)
        self.rt.drive = self.src

    def _factory(self, profile, stop):
        assert profile == self.profile
        self.stops.append(stop)
        return self.fake

    def enable(self, **options: Any) -> None:
        self.src.configure({"enabled": True, "options": options})

    def sync(self, *, full: bool = False) -> None:
        self.src.run_sync(full=full)

    def work(self) -> list[tuple[str, tuple]]:
        """What the service's pipeline workers do for Drive rows."""
        done, seen = [], set()
        while True:
            rows = self.db.next_work(limit=1, now=self.clock(), exclude_ids=seen, sources=["drive"])
            if not rows:
                return done
            seen.add(int(rows[0]["id"]))
            done.append((rows[0]["drive_file_id"], self.src.process(rows[0])))

    def rows(self) -> dict[str, dict[str, Any]]:
        return {r["drive_file_id"]: r for r in self.db.list_files(source="drive", limit=100_000)}

    def state_row(self) -> dict[str, Any]:
        return self.db.get_source_state("drive")

    def activity(self) -> list[str]:
        return [a["message"] for a in self.db.list_activity(limit=200)]

    def body_ids(self, fid: int) -> set[int]:
        return {r["id"] for r in self.db.chunks_of_file(fid) if r["ctype"] != t.CTYPE_FILE_CARD}

    def card(self, fid: int) -> dict[str, Any]:
        return next(r for r in self.db.chunks_of_file(fid) if r["ctype"] == t.CTYPE_FILE_CARD)

    def unlink(self) -> None:
        self.state.tokens[self.profile] = None
        self.fake.ident = None

    def close(self) -> None:
        self.src.close()
        self.rt.progress.close()
        self.db.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = Clock()
    state = SimpleNamespace(tokens={}, forgot=[], citations=[], gate=(True, None), clock=clock)
    monkeypatch.setattr(drv, "_now", clock)
    monkeypatch.setattr(drv, "UNLINK_RECHECK_S", 0.0)
    monkeypatch.setattr(drv, "_feature_gate", lambda: state.gate)
    monkeypatch.setattr(drv, "_token_present", lambda p: state.tokens.get(p) is not None)
    monkeypatch.setattr(drv, "_token_info", lambda p: state.tokens.get(p))
    monkeypatch.setattr(drv, "_forget_access_token", lambda p: state.forgot.append(p))
    monkeypatch.setattr(drv, "_purge_citations", lambda p: state.citations.append(p) or 0)
    worlds: list[World] = []

    def make(profile: str = "alice", *, scopes: list[str] | None = None) -> World:
        w = World(tmp_path, profile, f"cid|{profile}", state)
        state.tokens[profile] = {"email": f"{profile}@example.com", "scopes": scopes or ["openid", FILE_SCOPE]}
        worlds.append(w)
        return w

    state.make = make
    yield state
    for w in worlds:
        w.close()


def _basic(fake: FakeDrive) -> None:
    """Work/ (a.txt, Sub/c.md), a loose b.txt, a Google Doc, all under a
    My Drive root this account cannot see."""
    fake.folder("F", "Work")
    fake.folder("S", "Sub", parents=["F"])
    fake.put("a", "a.txt", parents=["F"], data=b"Alpha one.\n\nAlpha two.")
    fake.put("b", "b.txt", data=b"Bravo.")
    fake.put("c", "c.md", parents=["S"], data=b"# Charlie\n\nCharlie body.")
    fake.put("d", "Plan", mime=MIME_DOC, data=b"# Plan\n\nShip it\\.")


def _synced(env, profile: str = "alice", **options) -> World:
    w = env.make(profile)
    _basic(w.fake)
    w.enable(**options)
    w.sync()
    w.work()
    return w


# ── keys, paths, scopes ─────────────────────────────────────────────────────


def test_keys_paths_and_scopes():
    assert drive_key("abc") == hashlib.blake2b(b"drive:abc", digest_size=16).hexdigest()
    assert drive_key("aBc") != drive_key("abc")  # Drive ids are case-sensitive
    assert drive_key("folder:x") != drive_key("x")
    assert drive_rel_path(["Work", "Q3", "plan.docx"]) == "Drive/Work/Q3/plan.docx"
    assert drive_rel_path(["a/b"]) == "Drive/a∕b"
    assert drive_rel_path([]) == "Drive"
    assert is_whole_drive([WHOLE_SCOPE]) and is_whole_drive([WHOLE_SCOPE + ".readonly"])
    assert not is_whole_drive(["openid", FILE_SCOPE]) and not is_whole_drive([])


# ── index helpers ───────────────────────────────────────────────────────────


def test_index_drive_helpers(tmp_path):
    db = IndexDB.open(str(tmp_path / "u" / "index.db"), profile_uid="u")
    try:
        root = db.upsert_folder("drive", "Drive/R", drive_key("folder:R"), drive_id="R")
        child = db.upsert_folder("drive", "Drive/R/C", drive_key("folder:C"), drive_id="C", parent_id=root)
        other = db.upsert_folder("drive", "Drive/O", drive_key("folder:O"), drive_id="O")
        a = db.insert_file("drive", "Drive/R/C/a", drive_key("A"), drive_file_id="A", drive_md5="m", size=3,
                           drive_version="4", drive_modified="2025-01-01T00:00:00.000Z", status="indexed",
                           folder_id=child)
        b = db.insert_file("drive", "Drive/O/b", drive_key("a"), drive_file_id="a", status="dirty", folder_id=other)
        loose = db.insert_file("drive", "Drive/l", drive_key("L"), drive_file_id="L", status="dirty")
        local = db.insert_file("local", "x.txt", "hx", status="dirty")

        assert db.file_by_drive_id("A")["id"] == a["id"]
        assert db.file_by_drive_id("a")["id"] == b["id"]
        assert db.file_by_drive_id("zz") is None
        assert set(db.files_by_drive_ids(["A", "a", "zz"])) == {"A", "a"}
        m = db.drive_manifest()
        assert set(m) == {"A", "a", "L"}
        assert {"id", "drive_md5", "size", "drive_version", "drive_modified", "status", "folder_id"} <= set(m["A"])
        assert (m["A"]["drive_md5"], m["A"]["size"], m["A"]["folder_id"]) == ("m", 3, child)

        now = time.time() + 1
        assert {r["source"] for r in db.next_work(limit=10, now=now)} == {"drive", "local"}
        assert {r["id"] for r in db.next_work(limit=10, now=now, sources=["drive"])} == {b["id"], loose["id"]}
        assert [r["id"] for r in db.next_work(limit=10, now=now, sources=["local"])] == [local["id"]]
        assert db.next_work(limit=10, now=now, sources=[]) == []

        assert db.query_overview()["files"] == 4
        assert db.query_overview(hidden_sources={"drive"}) == {
            "files": 1, "pending": 1, "awaiting_captions": 0, "unreadable": 0}

        assert db.drive_files_outside(set()) == 0  # no narrowing
        assert sorted(db.drive_file_ids_outside({"R"})) == sorted([b["id"], loose["id"]])
        assert db.drive_files_outside({"C"}) == 2
        assert db.drive_files_outside({"R", "O"}) == 1
    finally:
        db.close()


# ── first sync ──────────────────────────────────────────────────────────────


def test_first_sync_lists_after_taking_the_cursor_and_keys_rows_by_id(env):
    w = env.make()
    _basic(w.fake)
    # A file created while the listing runs (after its page was read) must
    # still arrive, through the feed.
    w.fake.on_list = lambda: w.fake.put("e", "late.txt", data=b"Created during the listing.")
    w.enable()
    w.sync()

    names = [c[0] for c in w.fake.calls]
    assert names.index("start_page_token") < names.index("list_all")
    rows = w.rows()
    assert set(rows) == {"a", "b", "c", "d"}
    assert {k: r["rel_path"] for k, r in rows.items()} == {
        "a": "Drive/Work/a.txt", "b": "Drive/b.txt", "c": "Drive/Work/Sub/c.md", "d": "Drive/Plan",
    }
    assert all(r["path_hash"] == drive_key(k) and r["status"] == "dirty" for k, r in rows.items())
    work = w.db.folder_by_path("drive", drive_key("folder:F"))
    sub = w.db.folder_by_path("drive", drive_key("folder:S"))
    assert (work["rel_path"], work["drive_id"], sub["rel_path"], sub["parent_id"]) == (
        "Drive/Work", "F", "Drive/Work/Sub", work["id"])
    assert rows["a"]["folder_id"] == work["id"] and rows["b"]["folder_id"] is None
    assert rows["a"]["drive_web_link"] == "https://drive.google.com/file/d/a/view"
    st = w.state_row()
    assert st["drive_cursor"] == "6" and st["drive_account_key"] == "cid|alice" and st["state"] == "live"
    assert w.rt.queued == [(4, "initial_sync")]

    w.work()
    w.sync()  # the feed brings the file the listing could not see
    assert w.rows()["e"]["status"] == "dirty" and w.rows()["e"]["rel_path"] == "Drive/late.txt"
    assert w.state_row()["drive_cursor"] == "7"


def test_display_path_reaches_as_high_as_drive_shows(env):
    w = env.make()
    w.fake.folder("root0", "My Drive", parents=[])
    w.fake.folder("F", "Work", parents=["root0"])
    w.fake.put("a", "a.txt", parents=["F"], data=b"x")
    w.fake.put("z", "z.txt", parents=["gone"], data=b"z")  # parent not visible
    w.enable(include_folders=["F"])
    w.sync()
    rows = w.rows()
    assert rows["a"]["rel_path"] == "Drive/My Drive/Work/a.txt"
    assert "z" not in rows  # outside the chosen folder
    assert w.fake.called("list_folders_tree") == [("list_folders_tree", ("F",))]


# ── the pipeline ────────────────────────────────────────────────────────────


def test_process_downloads_binaries_and_exports_native_files(env):
    w = env.make()
    _basic(w.fake)
    w.enable()
    w.sync()
    outcomes = dict(w.work())
    assert {k: v[0] for k, v in outcomes.items()} == {"a": "added", "b": "added", "c": "added", "d": "added"}

    reqs = {r.name: r for r in w.service.requests}
    assert set(reqs) == {"a.txt", "b.txt", "c.md", "Plan.md"}
    assert reqs["a.txt"].data == b"Alpha one.\n\nAlpha two." and reqs["a.txt"].path is None
    assert reqs["a.txt"].limits == {"max_pages": 500}
    # A Google Doc's Markdown export is located by headings and unescaped.
    assert reqs["Plan.md"].kind == t.KIND_MARKDOWN
    assert reqs["Plan.md"].limits == {"max_pages": 500, "md_locators": "structure", "md_export": True}

    call = next(c for c in w.rt.index_calls if c["name"] == "a.txt")
    assert call["sha"] == hashlib.sha256(b"Alpha one.\n\nAlpha two.").hexdigest()
    assert call["source"] == "drive" and call["size"] == 22 and call["status"] == "indexed"
    assert call["drive_fields"]["drive_file_id"] == "a"
    assert call["drive_fields"]["drive_md5"] == hashlib.md5(b"Alpha one.\n\nAlpha two.").hexdigest()
    assert call["extra"] == {"source": "Google Drive"}
    doc = next(c for c in w.rt.index_calls if c["name"] == "Plan")
    assert doc["size"] == len(b"# Plan\n\nShip it\\.") and doc["drive_fields"]["drive_md5"] is None
    assert {r["status"] for r in w.rows().values()} == {"indexed"}


def test_rename_and_move_change_the_row_and_card_only(env):
    w = _synced(env)
    a = w.rows()["a"]
    body, card = w.body_ids(a["id"]), w.card(a["id"])
    fetched = len(w.fake.called("fetch"))

    w.fake.rename("a", "alpha.txt")
    w.fake.move("b", ["F"])
    w.sync()

    rows = w.rows()
    assert rows["a"]["rel_path"] == "Drive/Work/alpha.txt" and rows["a"]["name"] == "alpha.txt"
    assert rows["b"]["rel_path"] == "Drive/Work/b.txt"
    assert rows["b"]["folder_id"] == w.db.folder_by_path("drive", drive_key("folder:F"))["id"]
    assert rows["a"]["status"] == rows["b"]["status"] == "indexed"  # not re-queued
    assert w.body_ids(a["id"]) == body
    new_card = w.card(a["id"])
    assert new_card["id"] != card["id"] and "Drive/Work/alpha.txt" in new_card["text"]
    assert card["id"] in w.rt.vector_deletes
    assert w.work() == [] and len(w.fake.called("fetch")) == fetched


def test_folder_rename_rewrites_every_descendant(env):
    w = _synced(env)
    c = w.rows()["c"]
    body = w.body_ids(c["id"])
    w.fake.rename("F", "Projects")
    w.sync()
    assert w.db.folder_by_path("drive", drive_key("folder:F"))["rel_path"] == "Drive/Projects"
    assert w.db.folder_by_path("drive", drive_key("folder:S"))["rel_path"] == "Drive/Projects/Sub"
    rows = w.rows()
    assert rows["a"]["rel_path"] == "Drive/Projects/a.txt"
    assert rows["c"]["rel_path"] == "Drive/Projects/Sub/c.md"
    assert rows["c"]["status"] == "indexed" and w.body_ids(c["id"]) == body
    assert "Drive/Projects/Sub/c.md" in w.card(c["id"])["text"]


def test_binary_edit_is_reread(env):
    w = _synced(env)
    w.service.requests.clear()
    w.fake.edit("a", b"Alpha one.\n\nAlpha three.")
    w.sync()
    assert w.rows()["a"]["status"] == "dirty"
    assert [k for k, _ in w.work()] == ["a"]
    assert [r.name for r in w.service.requests] == ["a.txt"]
    assert w.rows()["a"]["sha256"] == hashlib.sha256(b"Alpha one.\n\nAlpha three.").hexdigest()


def test_native_file_with_an_identical_export_is_unchanged(env):
    w = _synced(env)
    w.service.requests.clear()
    d = w.rows()["d"]
    body = w.body_ids(d["id"])
    w.fake.touch("d")  # modifiedTime and version move, the text does not
    w.sync()
    assert w.rows()["d"]["status"] == "dirty"
    assert w.work() == [("d", ("unchanged", "Plan is unchanged", None))]
    assert w.service.requests == []  # exported and compared, never re-extracted
    row = w.rows()["d"]
    assert row["status"] == "indexed" and w.body_ids(d["id"]) == body
    assert row["drive_version"] == "2"

    w.fake.edit("d", b"# Plan\n\nShip it later.")
    w.sync()
    w.work()
    assert [r.name for r in w.service.requests] == ["Plan.md"]


def test_metadata_only_is_decided_before_any_download(env):
    w = env.make()
    w.service.cap = 50
    w.fake.put("doc", "Target", mime=MIME_DOC, data=b"# T")
    w.fake.put("v1", "clip", mime="video/mp4", data=b"\x00" * 10)
    w.fake.put("v2", "movie.mkv", mime="application/octet-stream", data=b"\x00" * 10)
    w.fake.put("zip", "bundle", mime="application/zip", data=b"PK")
    w.fake.put("big", "big.txt", data=b"x" * 100)
    w.fake.put("locked", "locked.txt", data=b"secret", capabilities={"canDownload": False})
    w.fake.put("sc", "Link to Target", mime=MIME_SHORTCUT,
               shortcutDetails={"targetId": "doc", "targetMimeType": MIME_DOC})
    w.fake.put("form", "Survey", mime=MIME_FORM)
    w.enable()
    w.sync()
    w.work()
    fetched = {c[1] for c in w.fake.called("fetch")}
    assert fetched == {"doc", "form"}
    calls = {c["drive_fields"]["drive_file_id"]: c for c in w.rt.index_calls}
    reasons = {k: (c["status"], c["reason"], c["kind"]) for k, c in calls.items() if k != "doc"}
    assert reasons == {
        "v1": ("metadata_only", "video", "video"),
        "v2": ("metadata_only", "video", "video"),
        "zip": ("metadata_only", "archive", "archive"),
        "big": ("metadata_only", "too_large", "text"),
        "locked": ("metadata_only", "not_downloadable", "text"),
        "sc": ("metadata_only", "shortcut", "other"),
        "form": ("metadata_only", "google_type", "other"),
    }
    assert calls["sc"]["extra"] == {"source": "Google Drive", "status": drv.METADATA_ONLY_NOTE, "target": "Target"}
    assert all(c["sha"] is None and c["result"] is None for k, c in calls.items() if k != "doc")
    assert w.rows()["locked"]["status"] == "metadata_only"


def test_image_dates_and_camera_come_from_drive_metadata(env):
    w = env.make()
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    w.fake.put("p", "IMG_1.jpg", mime="image/jpeg", data=jpeg, imageMediaMetadata={
        "time": "2024:07:01 10:00:00", "cameraMake": "Canon", "cameraModel": "R5",
        "location": {"latitude": 10.5, "longitude": 106.7}, "width": 4000, "height": 3000,
    })
    w.service.results["IMG_1.jpg"] = t.ExtractResult(
        status=t.EXTRACT_OK, kind=t.KIND_IMAGE, exif={"model": "EOS R5", "width": 4000, "height": 3000},
        image={"width": 4000, "height": 3000, "is_camera_photo": True},
    )
    w.enable()
    w.sync()
    w.work()
    call = w.rt.index_calls[0]
    assert call["kind"] == t.KIND_IMAGE and call["image_bytes"] == jpeg
    assert call["exif"] == {"taken_at": "2024-07-01T10:00:00", "make": "Canon", "model": "EOS R5",
                            "gps": {"lat": 10.5, "lon": 106.7}, "width": 4000, "height": 3000}
    assert call["taken_ts"] == datetime(2024, 7, 1, 10, 0, 0).timestamp()


@pytest.mark.parametrize("kind,expect", [
    ("not_downloadable", ("metadata_only", "not_downloadable")),
    ("too_large", ("metadata_only", "too_large")),
    ("export_too_large", ("metadata_only", "too_large")),
])
def test_content_refusals_index_details_only(env, kind, expect):
    w = env.make()
    w.fake.put("a", "a.txt", data=b"text")
    w.enable()
    w.sync()
    w.fake.fail_ids[("fetch", "a")] = FakeError(kind)
    w.work()
    row = w.rows()["a"]
    assert (row["status"], row["status_reason"]) == expect  # never purged


def test_throttled_file_is_retried_with_backoff(env):
    w = env.make()
    w.fake.put("a", "a.txt", data=b"text")
    w.enable()
    w.sync()
    w.fake.fail_ids[("fetch", "a")] = FakeError("rate_limited")
    assert w.work() == [("a", ("failed", "a.txt: Google Drive is rate-limiting requests; retrying later",
                               "rate_limited"))]
    row = w.rows()["a"]
    assert (row["status"], row["attempts"]) == ("error", 1)
    assert row["next_attempt_at"] == pytest.approx(env.clock() + 60.0)
    assert w.src.work_allowed()  # a row problem, not a source hold


def test_a_file_is_purged_only_when_get_confirms_it_is_gone(env):
    w = env.make()
    w.fake.put("a", "a.txt", data=b"aaa")
    w.fake.put("b", "b.txt", data=b"bbb")
    w.enable()
    w.sync()
    # b: the download says 404 but files.get still finds it — not gone.
    w.fake.fail_ids[("fetch", "b")] = FakeError("not_found")
    w.fake.delete("a", log=False)  # a: gone without a change in the feed
    out = dict(w.work())
    assert out["a"][0] == "removed"
    assert out["b"][0] == "failed" and out["b"][2] == "file_unavailable"
    rows = w.rows()
    assert "a" not in rows and rows["b"]["status"] == "error"
    assert rows["b"]["next_attempt_at"] is not None


def test_many_files_vanishing_at_once_holds_instead_of_purging(env):
    w = env.make()
    for i in range(51):
        w.fake.put(f"f{i}", f"f{i}.txt", data=b"x")
    w.enable()
    w.sync()
    for i in range(51):
        w.fake.delete(f"f{i}", log=False)
    out = w.work()
    assert [o[0] for _, o in out].count("removed") == 50
    assert out[-1][1][2] == "many_unavailable"
    assert len(w.rows()) == 1
    st = w.state_row()
    assert (st["state"], st["reason"]) == ("hold", "drive_unreachable")
    assert st["detail"]["message"] == "many files became unavailable" and st["purge_after"] is None
    assert not w.src.work_allowed()

    # The retry is a full reconcile: the listing, under the mass guard, decides.
    env.clock.advance(61)
    assert w.src.sync_due(env.clock())
    w.sync()
    assert w.rows() == {} and w.state_row()["state"] == "live"


# ── change feed ─────────────────────────────────────────────────────────────


def test_trash_and_delete_leave_the_index(env):
    w = _synced(env)
    w.fake.trash("b")
    w.fake.delete("c")
    w.sync()
    assert set(w.rows()) == {"a", "d"}
    assert w.rt.vector_deletes
    assert any("Removed 2 files" in m for m in w.activity())


def test_the_last_change_of_a_file_in_a_page_wins(env):
    w = _synced(env)
    fetched = len(w.fake.called("fetch"))
    w.fake.edit("a", b"new")
    w.fake.trash("a")
    w.sync()
    assert "a" not in w.rows()
    assert len(w.fake.called("fetch")) == fetched


def test_the_cursor_is_committed_after_every_page(env):
    w = _synced(env)
    start = int(w.state_row()["drive_cursor"])
    w.fake.page_size = 1
    w.fake.edit("a", b"one")
    w.fake.edit("b", b"two")
    w.fake.fail_page = 1
    env.clock.advance(301)
    w.sync()
    assert int(w.state_row()["drive_cursor"]) == start + 1  # page 0 kept
    assert w.rows()["a"]["status"] == "dirty" and w.rows()["b"]["status"] == "indexed"
    assert w.state_row()["reason"] == "drive_unreachable"

    w.fake.fail_page = None
    w.sync()
    assert int(w.state_row()["drive_cursor"]) == start + 2
    assert w.rows()["b"]["status"] == "dirty" and w.state_row()["state"] == "live"


def test_bad_cursor_and_lost_shared_drive_trigger_a_full_reconcile(env):
    w = _synced(env)
    w.fake.put("s1", "shared.txt", data=b"s", driveId="team")
    w.sync()
    w.work()
    assert "s1" in w.rows()
    lists = len(w.fake.called("list_all"))

    w.fake.drop_drive("team")
    w.sync()
    assert len(w.fake.called("list_all")) == lists + 1
    assert "s1" not in w.rows()
    assert any("shared drive" in m for m in w.activity())

    w.fake.fail["changes"] = FakeError("bad_cursor", status=410)
    w.sync()
    assert len(w.fake.called("list_all")) == lists + 2
    assert w.state_row()["state"] == "live"


def test_include_folders_scope_the_change_feed(env):
    w = env.make()
    w.fake.folder("root0", "My Drive", parents=[])
    w.fake.folder("F", "Work", parents=["root0"])
    w.fake.put("a", "a.txt", parents=["F"], data=b"a")
    w.fake.folder("X", "Elsewhere", parents=["root0"])
    w.fake.put("x1", "x1.txt", parents=["X"], data=b"x")
    w.enable(include_folders=["F"])
    w.sync()
    assert set(w.rows()) == {"a"}

    w.fake.put("o", "outside.txt", parents=["root0"], data=b"o")  # not in scope
    w.fake.put("n", "new.txt", parents=["F"], data=b"n")          # in scope
    w.sync()
    assert set(w.rows()) == {"a", "n"}

    w.fake.move("a", ["root0"])  # leaves the scope
    w.fake.move("X", ["F"])      # enters it, with a file the feed never mentions
    w.sync()
    rows = w.rows()
    assert set(rows) == {"n", "x1"}
    assert rows["x1"]["rel_path"] == "Drive/My Drive/Work/Elsewhere/x1.txt"
    assert w.fake.called("list_folders_tree")[-1] == ("list_folders_tree", ("X",))


def test_narrowing_include_folders_removes_what_left_without_asking(env):
    w = env.make()
    w.fake.folder("F", "Work")
    w.fake.folder("S", "Sub", parents=["F"])
    for i in range(300):
        w.fake.put(f"w{i}", f"w{i}.txt", parents=["F"], data=b"w")
    w.fake.put("keep", "keep.txt", parents=["S"], data=b"k")
    w.enable(include_folders=["F"])
    w.sync()
    assert len(w.rows()) == 301

    w.enable(include_folders=["S"])  # the user confirmed this plan already
    w.sync()
    assert set(w.rows()) == {"keep"}
    assert w.src.view()["confirmation"] is None
    assert w.db.folder_by_path("drive", drive_key("folder:F")) is None
    assert w.rows()["keep"]["rel_path"] == "Drive/Work/Sub/keep.txt"


def test_a_chosen_folder_that_is_not_a_drive_id_holds_for_new_settings(env):
    w = env.make()
    err = FakeError("http")
    err.reason = "invalid_id"
    w.fake.fail["list_folders_tree"] = err
    w.enable(include_folders=["not an id"])
    w.sync()
    st = w.state_row()
    assert (st["reason"], st["detail"]["why"], st["purge_after"]) == (
        "drive_misconfigured", "folder_unavailable", None)


def test_whole_drive_account_needs_chosen_folders(env):
    w = env.make(scopes=["openid", WHOLE_SCOPE])
    w.fake.put("a", "a.txt", data=b"a")
    w.enable()
    w.sync()
    st = w.state_row()
    assert (st["state"], st["reason"], st["detail"]["why"]) == ("hold", "drive_misconfigured", "folders_required")
    assert w.fake.called("list_all") == [] and w.rows() == {}
    assert w.src.view()["whole_drive"] is True


# ── mass removal ────────────────────────────────────────────────────────────


def _mass(env) -> World:
    w = env.make()
    for i in range(250):
        w.fake.put(f"f{i}", f"f{i}.txt", data=b"x")
    w.enable()
    w.sync()
    for i in range(200):
        w.fake.delete(f"f{i}", log=False)
    w.sync(full=True)
    return w


def test_a_reconcile_that_would_remove_half_asks_first(env):
    w = _mass(env)
    view = w.src.view()
    assert view["confirmation"] == {"kind": "mass_delete", "source": "drive", "missing": 200, "total": 250}
    assert view["state"] == "awaiting_confirmation"
    assert sum(1 for r in w.rows().values() if r["status"] == "missing") == 200
    assert not w.src.work_allowed() and not w.src.sync_due(env.clock() + 10_000)

    assert w.src.confirm_deletions() == 200
    assert len(w.rows()) == 50 and w.src.view()["confirmation"] is None
    assert w.src.work_allowed()


def test_rejected_removals_stay_hidden_then_expire(env):
    w = _mass(env)
    assert w.src.reject_deletions() == 200
    assert w.src.view()["confirmation"] is None
    w.sync(full=True)  # not asked again
    assert w.src.view()["confirmation"] is None
    assert sum(1 for r in w.rows().values() if r["status"] == "missing") == 200
    env.clock.advance(15 * DAY)
    w.sync(full=True)
    assert len(w.rows()) == 50


def test_an_incomplete_listing_removes_nothing(env):
    w = _synced(env)
    w.fake.delete("b", log=False)
    w.fake.complete = False
    w.sync(full=True)
    assert "b" in w.rows()
    assert any("incomplete listing" in m for m in w.activity())


def test_a_pending_confirmation_survives_a_restart(env):
    w = _mass(env)
    again = DriveSource(w.rt, client_factory=w._factory)
    again.configure({"enabled": True, "options": {}})
    assert again.view()["confirmation"]["missing"] == 200
    again.close()


# ── holds ───────────────────────────────────────────────────────────────────


def test_revocation_hides_drive_and_purges_after_seven_days_reconfirmed(env):
    w = _synced(env)
    w.fake.down("auth_revoked")
    env.clock.advance(301)
    first = env.clock()
    w.sync()
    st = w.state_row()
    assert (st["state"], st["reason"]) == ("hold", "auth_revoked")
    assert st["purge_after"] == pytest.approx(first + 7 * DAY)
    assert st["detail"]["purge_at"] == pytest.approx((first + 7 * DAY) * 1000)
    assert not w.src.work_allowed() and w.src.view()["state"] == "hold"

    env.clock.advance(DAY)
    w.sync()  # still revoked: the timer does not restart
    assert w.state_row()["purge_after"] == pytest.approx(first + 7 * DAY)
    assert w.state_row()["hold_since"] == pytest.approx(first)
    assert set(w.rows()) == {"a", "b", "c", "d"}

    # An outage during the hold proves nothing: the revocation hold stays.
    w.fake.down("unreachable")
    w.sync()
    assert w.state_row()["reason"] == "auth_revoked"
    assert w.state_row()["purge_after"] == pytest.approx(first + 7 * DAY)

    # Due, but the re-confirmation cannot be made: nothing goes.
    env.clock.advance(6 * DAY + 1)
    w.src.housekeeping(env.clock())
    assert w.src.sync_due(env.clock())
    w.sync()
    assert set(w.rows()) == {"a", "b", "c", "d"} and env.citations == []

    w.fake.down("auth_revoked")
    w.src.housekeeping(env.clock())
    w.sync()
    assert ("about",) in w.fake.calls
    assert w.rows() == {}
    assert w.db.folder_by_path("drive", drive_key("folder:F")) is None
    assert env.citations == ["alice"] and w.rt.vector_deletes
    st = w.state_row()
    assert (st["state"], st["reason"], st["purge_after"]) == ("hold", "auth_revoked", None)
    assert st["detail"]["purged_at"] and st["detail"]["purge_at"] is None

    w.sync()  # still revoked afterwards: no new timer for an empty index
    assert w.state_row()["purge_after"] is None


def test_a_revocation_undone_before_the_purge_keeps_everything(env):
    w = _synced(env)
    w.fake.down("auth_revoked")
    w.sync()
    assert w.state_row()["reason"] == "auth_revoked"
    env.clock.advance(8 * DAY)
    w.fake.up()  # re-linked: about() and the feed answer again
    w.src.housekeeping(env.clock())
    w.sync()
    assert set(w.rows()) == {"a", "b", "c", "d"}
    assert w.state_row()["state"] == "live" and w.state_row()["purge_after"] is None
    assert env.citations == []


def test_an_unreachable_drive_is_never_purged_and_recovers_by_itself(env):
    w = _synced(env)
    w.fake.down("unreachable")
    env.clock.advance(301)
    w.sync()
    st = w.state_row()
    assert (st["state"], st["reason"], st["purge_after"]) == ("hold", "drive_unreachable", None)
    assert st["detail"]["purge_at"] is None
    assert w.src.view()["state"] == "hold"
    for _ in range(3):
        env.clock.advance(10 * DAY)
        w.src.housekeeping(env.clock())
        w.sync()
    assert set(w.rows()) == {"a", "b", "c", "d"} and env.citations == []
    w.fake.up()
    w.sync()
    assert w.state_row()["state"] == "live" and w.state_row()["detail"] is None
    assert w.src.work_allowed()


def test_holds_are_retried_after_1_5_then_15_minutes(env):
    w = _synced(env)
    w.fake.down("unreachable")
    for delay in (60.0, 300.0, 900.0, 900.0):
        env.clock.advance(1000)
        w.sync()
        now = env.clock()
        assert not w.src.sync_due(now + delay - 1)
        assert w.src.sync_due(now + delay)


@pytest.mark.parametrize("kind,reason", [
    ("auth_failed", "drive_misconfigured"),
    ("auth_misconfigured", "drive_misconfigured"),
    ("account_forbidden", "drive_unreachable"),
])
def test_other_account_problems_hold_without_a_timer(env, kind, reason):
    w = _synced(env)
    w.fake.down(kind)
    w.sync()
    st = w.state_row()
    assert (st["state"], st["reason"], st["purge_after"]) == ("hold", reason, None)
    assert st["detail"]["kind"] == kind


def test_a_hold_and_its_timer_survive_a_restart(env):
    w = _synced(env)
    w.fake.down("auth_revoked")
    w.sync()
    purge_after = w.state_row()["purge_after"]
    again = DriveSource(w.rt, client_factory=w._factory)
    again.configure({"enabled": True, "options": {}})
    view = again.view()
    assert (view["state"], view["reason"]) == ("hold", "auth_revoked")
    assert view["detail"]["purge_at"] == pytest.approx(purge_after * 1000)
    assert not again.work_allowed()
    env.clock.advance(DAY)
    again.run_sync()
    assert w.state_row()["purge_after"] == pytest.approx(purge_after)
    again.close()


def test_unlinked_needs_two_reads_without_a_token_file(env):
    w = _synced(env)
    # A read that lands mid-swap is not an unlink.
    w.fake.identities = [None]
    w.sync()
    assert w.state_row()["state"] == "live"

    w.unlink()
    w.sync()
    st = w.state_row()
    assert (st["state"], st["reason"]) == ("hold", "drive_unlinked")
    assert st["purge_after"] == pytest.approx(env.clock() + 7 * DAY)
    assert sum(1 for c in w.fake.calls if c == ("identity",)) >= 4

    env.clock.advance(7 * DAY + 1)
    w.src.housekeeping(env.clock())
    w.sync()  # the two reads re-confirm; then set D goes
    assert w.rows() == {} and env.citations == ["alice"]


def test_an_unreadable_token_file_is_not_an_unlink(env):
    w = _synced(env)
    w.fake.ident = None  # the file is there but cannot be read
    w.sync()
    st = w.state_row()
    assert (st["reason"], st["detail"]["why"], st["purge_after"]) == (
        "drive_misconfigured", "token_unreadable", None)


def test_a_worker_seeing_no_token_pauses_drive_until_rechecked(env):
    w = env.make()
    w.fake.put("a", "a.txt", data=b"a")
    w.enable()
    w.sync()
    w.fake.fail_ids[("fetch", "a")] = FakeError("unlinked")
    assert w.work()[0][1][0] == "skipped"
    assert not w.src.work_allowed()
    assert w.rows()["a"]["status"] == "dirty"  # untouched
    assert w.src.sync_due(env.clock())
    w.fake.fail_ids.clear()
    w.sync()  # the token is still there: carry on
    assert w.src.work_allowed() and w.state_row()["state"] == "live"


def test_account_switch_purges_and_reindexes(env):
    w = _synced(env)
    w.fake.ident = "cid|someone-else"
    w.fake.items.clear()
    w.fake.put("z", "theirs.txt", data=b"z")
    w.sync()
    assert env.forgot == ["alice"] and env.citations == ["alice"]
    assert set(w.rows()) == {"z"}
    assert w.state_row()["drive_account_key"] == "cid|someone-else"
    assert "Drive account changed — re-indexing" in w.activity()
    assert w.rt.vector_deletes


def test_suspend_stops_work_until_the_token_is_rechecked(env):
    w = _synced(env)
    w.fake.edit("a", b"changed")
    w.sync()
    w.src.suspend()
    assert not w.src.work_allowed() and w.src.view()["state"] == "suspended"
    assert w.stops[-1].is_set()  # the running client was told to stop
    assert w.work()[0][1][0] == "skipped"
    assert w.src.sync_due(env.clock())
    w.sync()
    assert w.src.work_allowed()


def test_close_stops_and_closes_the_client(env):
    w = _synced(env)
    w.src.close()
    assert w.stops[-1].is_set() and ("close",) in w.fake.calls
    assert not w.src.work_allowed() and not w.src.sync_due(env.clock() + DAY)


def test_disabled_or_gated_drive_does_nothing(env):
    w = _synced(env)
    w.src.configure({"enabled": False})
    assert not w.src.work_allowed() and not w.src.sync_due(env.clock() + DAY)
    assert w.src.view()["state"] == "disabled"
    env.gate = (False, "embedding_disabled")
    w.enable()
    assert w.src.view()["state"] == "suspended" and w.src.view()["reason"] == "embedding_disabled"
    assert set(w.rows()) == {"a", "b", "c", "d"}  # the index is kept


def test_view_reports_the_drive_half(env):
    w = _synced(env, include_folders=[])
    w.src.refresh_counts()
    view = w.src.view()
    assert view == {
        "enabled": True, "state": "idle", "reason": None, "detail": None,
        "account_email": "alice@example.com", "identity_set": True, "whole_drive": False,
        "include_folders": [], "last_sync_at": pytest.approx(env.clock() * 1000),
        "last_full_at": pytest.approx(env.clock() * 1000),
        "counts": {"indexed": 4, "pending": 0, "error": 0, "metadata_only": 0},
        "confirmation": None,
    }


def test_a_purge_from_another_thread_stops_the_running_sync_first(env):
    import threading

    w = env.make()
    _basic(w.fake)
    w.enable()
    listing = threading.Event()

    def slow_listing():
        listing.set()
        assert w.stops[-1].wait(5)  # released only by the purge's abort

    w.fake.on_list = slow_listing
    th = threading.Thread(target=w.sync)
    th.start()
    assert listing.wait(5)
    assert w.src.purge_index(keep_hold=False) == 0
    th.join(5)
    assert not th.is_alive()
    assert w.rows() == {} and w.state_row()["drive_cursor"] is None  # the aborted sync wrote nothing after
    assert w.state_row()["state"] is None


def test_purge_index_deletes_set_d_only(env):
    w = _synced(env)
    local = w.db.insert_file("local", "notes.txt", "h-notes", status="indexed")
    n = w.src.purge_index(keep_hold=False)
    assert n == 4 and w.rows() == {}
    assert w.db.get_file(local["id"]) is not None
    assert w.state_row()["drive_cursor"] is None and env.citations == ["alice"]
    assert w.src.view()["identity_set"] is False


# ── profiles ────────────────────────────────────────────────────────────────


def test_two_profiles_are_independent(env):
    alice = env.make("alice")
    bob = env.make("bob")
    _basic(alice.fake)
    bob.fake.put("q", "bob.txt", data=b"Bob's only file.")
    for w in (alice, bob):
        w.enable()
        w.sync()
        w.work()
    assert set(alice.rows()) == {"a", "b", "c", "d"} and set(bob.rows()) == {"q"}

    alice.fake.down("auth_revoked")
    env.clock.advance(301)
    alice.sync()
    bob.sync()
    assert alice.state_row()["state"] == "hold" and bob.state_row()["state"] == "live"
    assert not alice.src.work_allowed() and bob.src.work_allowed()

    alice.fake.ident = "cid|new-alice"
    alice.fake.up()
    alice.sync()
    assert env.forgot == ["alice"] and env.citations == ["alice"]
    assert set(bob.rows()) == {"q"} and bob.state_row()["drive_account_key"] == "cid|bob"


# ── citations storage ───────────────────────────────────────────────────────


def test_delete_source_kind_removes_one_profiles_drive_citations(tmp_path, monkeypatch):
    pytest.importorskip("a2a")
    from ._citations_env import build, close

    env = build(tmp_path, monkeypatch)

    def row(token, kind):
        return {"token": token, "cite_id": token[4:12], "target": "file", "ref_id": 1, "source_kind": kind,
                "label": "", "rel_path": "x", "snippet": "", "leaf": "search"}

    try:
        env.cit.issue("alice", "c-web", [row("[ud:aaaaaaaa]", "local"), row("[ud:bbbbbbbb]", "drive"),
                                         row("[ud:cccccccc]", "drive")])
        env.cit.issue("bob", "c-bob", [row("[ud:dddddddd]", "drive")])
        assert env.cit.delete_source_kind("alice", "drive") == 2
        assert env.cit.count("alice") == 1 and env.cit.count("bob") == 1
        assert [r["source_kind"] for r in env.cit.rows_for_cite_ids("alice", ["aaaaaaaa", "bbbbbbbb"])] == ["local"]
    finally:
        close(env)
