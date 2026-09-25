"""A small, real Documentation search world for the citation tests.

Two profiles (alice, bob) in a throwaway main database with the citation
registry, a few conversations, and an index file for alice holding one PDF
and one Markdown note. The engine is replaced by a stub whose runtimes hand
out that real :class:`~app.documents.index.IndexDB`, which is all the citation
code asks of it.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from a2a.server.models import Base
import app.storage.models  # noqa: F401 — registers the tables on Base
from sqlalchemy import text

import app.storage.documents_citations_storage as cit_module
import app.storage.documents_storage as uds_module
from app.config.settings import BaseConfig
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.documents_citations_storage import DocumentCitationsStorage
from app.storage.documents_storage import DocumentsStorage
from app.documents import service as svc_module
from app.documents.cite import IssuedCitation, locator_label, make_token
from app.documents.index import IndexDB, index_path
from app.documents.textnorm import text_hash
from app.documents.types import Chunk, ChunkDiff

TABLES = (
    "profiles", "channels", "conversations", "messages",
    "document_sources", "document_captions", "document_vision_usage", "document_citations",
)

LAW_1 = (
    "Điều 203. Thẩm quyền giải quyết tranh chấp đất đai. Tranh chấp đất đai mà "
    "đương sự có Giấy chứng nhận thì do Tòa án nhân dân giải quyết."
)
LAW_2 = (
    "Tranh chấp đất đai mà đương sự không có Giấy chứng nhận thì đương sự chỉ được "
    "lựa chọn một trong hai hình thức giải quyết tranh chấp đất đai theo Luật số 45/2013/QH13."
)
LAW_3 = "Điều 204. Giải quyết khiếu nại, khiếu kiện về đất đai trong thời hạn 30 ngày."
NOTE = "Revenue grew 12% in Q3 2025, driven by the new enterprise plan."


class FakeRuntime:
    def __init__(self, uid: str, db: IndexDB | None, root: str | None):
        self.uid = uid
        self.db = db
        self.root = root

    def ensure_db(self):
        if self.db is None or self.db.closed:
            self.db = IndexDB.open(index_path(self.uid), profile_uid=self.uid)
        return self.db


class FakeService:
    def __init__(self):
        self.runtimes: dict[str, FakeRuntime] = {}

    def runtime(self, profile, *, create=False):
        return self.runtimes.get(profile)


def _chunks(texts, *, first_page=1):
    out = []
    for i, t in enumerate(texts):
        out.append(Chunk(
            ordinal=i, ctype="body", heading="", text=t, text_hash=text_hash("", t),
            locator={"page": first_page + i},
        ))
    return out


def build(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    sysdir = tmp_path / "system"
    root = tmp_path / "docs"
    (root / "Luat").mkdir(parents=True)
    (root / "Notes").mkdir(parents=True)
    (root / "Luat" / "luat-dat-dai.pdf").write_bytes(b"%PDF-1.4 fake")
    (root / "Notes" / "q3.md").write_text(NOTE, encoding="utf-8")
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))

    provider = SqliteDatabaseProvider(str(tmp_path / "main.db"))
    eng = provider.sync_engine()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    now = time.time() * 1000
    with eng.begin() as c:
        for uid, name in (("uid-alice", "alice"), ("uid-bob", "bob")):
            c.execute(text(
                "INSERT INTO profiles (id,name,created_at,updated_at) VALUES (:i,:n,:t,:t)"
            ), {"i": uid, "n": name, "t": now})
        # c-web: a web conversation (context id == its id). c-chan: a channel
        # conversation whose context id is the platform's chat id.
        for cid, profile, ctx in (
            ("c-web", "alice", "c-web"),
            ("c-other", "alice", "c-other"),
            ("c-chan", "alice", "tg:12345"),
            ("c-bob", "bob", "c-bob"),
        ):
            c.execute(text(
                "INSERT INTO conversations (id,profile,context_id,kind,title,compaction_watermark,"
                "created_at,updated_at) VALUES (:i,:p,:c,'chat','t',-1,:t,:t)"
            ), {"i": cid, "p": profile, "c": ctx, "t": now})

    storage = DocumentsStorage(provider)
    monkeypatch.setattr(uds_module, "_instance", storage)
    cit = DocumentCitationsStorage(provider)
    monkeypatch.setattr(cit_module, "_instance", cit)
    storage.upsert_source("alice", "local", enabled=True, root_mode="custom", root_path=str(root))

    db = IndexDB.open(index_path("uid-alice"), profile_uid="uid-alice")
    law = db.insert_file(
        "local", "Luat/luat-dat-dai.pdf", "h-law", name="luat-dat-dai.pdf", kind="pdf",
        status="indexed", mime="application/pdf",
    )
    law_chunks = _chunks([LAW_1, LAW_2, LAW_3])
    db.apply_chunks(file_id=law["id"], folder_id=None, source="local", diff=ChunkDiff(add=law_chunks))
    note = db.insert_file(
        "local", "Notes/q3.md", "h-note", name="q3.md", kind="markdown", status="indexed",
    )
    note_chunks = [Chunk(
        ordinal=0, ctype="body", heading="", text=NOTE, text_hash=text_hash("", NOTE),
        locator={"line_start": 1, "line_end": 1},
    )]
    db.apply_chunks(file_id=note["id"], folder_id=None, source="local", diff=ChunkDiff(add=note_chunks))
    folder_id = db.upsert_folder("local", "Luat", "hf-luat", name="Luat")
    folder = db.get_folder(folder_id)

    svc = FakeService()
    svc.runtimes["alice"] = FakeRuntime("uid-alice", db, str(root))
    monkeypatch.setattr(svc_module, "_service", svc)

    def token(rec, chunk=None):
        return make_token(rec["cite_id"], chunk.text_hash if chunk else None)

    def issued(rec, chunk=None, *, leaf="search", target="file"):
        return IssuedCitation(
            token=token(rec, chunk), cite_id=rec["cite_id"], target=target, ref_id=rec["id"],
            text_hash=chunk.text_hash if chunk else None, source_kind="local",
            locator=dict(chunk.locator) if chunk else {},
            label=locator_label(chunk.locator) if chunk else "",
            rel_path=rec["rel_path"], snippet=(chunk.text if chunk else "")[:800], leaf=leaf,
        )

    return SimpleNamespace(
        provider=provider, storage=storage, cit=cit, db=db, svc=svc, root=root, sysdir=sysdir,
        law=law, law_chunks=law_chunks, note=note, note_chunks=note_chunks, folder=folder,
        token=token, issued=issued,
    )


def close(env: SimpleNamespace) -> None:
    for rt in env.svc.runtimes.values():
        if rt.db is not None:
            rt.db.close()
