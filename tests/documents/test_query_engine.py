"""The query engine over a hand-built index: degraded modes, stale vectors,
scope handling, grouping, the reader, and budgeted rendering.

The index is written directly (the real chunker and cards, no sync engine),
and the embedder and vector store are fakes whose answers the tests choose —
so each test can put the engine in exactly one awkward situation.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from app.documents.chunking import chunk_blocks, diff_chunks, make_file_card, make_folder_card
from app.documents.cite import TOKEN_RE
from app.documents.discovery.walker import path_hash
from app.documents.index import IndexDB
from app.documents.query import ReadError
from app.documents.query import vector as vector_module
from app.documents.query.engine import QueryEngine
from app.documents.query.filters import Filters, parse_filters
from app.documents.query.render import RenderContext, render_read, render_search
from app.documents.textnorm import fold
from app.documents.types import Block
from tests.documents.legal_samples import vietnamese_law_blocks

UTC = _dt.timezone.utc
T0 = _dt.datetime(2026, 9, 20, 12, tzinfo=UTC).timestamp()


class FakeEmbedder:
    model_key = "fake"
    dimension = 4

    def embed_search_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


class FakeStore:
    """Answers ``query_vectors`` from a scripted list; records every call."""

    def __init__(self, hits=None, exc=None):
        self.hits = list(hits or [])
        self.exc = exc
        self.calls = []

    def query_vectors(self, name, vector, k, filt=None):
        self.calls.append((k, filt))
        if self.exc:
            raise self.exc
        # Deliberately ignores the filter: the engine must check every hit
        # against the index itself, whatever the store claims.
        return self.hits[:k]


class Index:
    def __init__(self, tmp_path: Path):
        self.db = IndexDB.open(str(tmp_path / "index.db"), profile_uid="u1")
        self.folders: dict[str, int] = {}

    def folder(self, rel: str, *, project: bool = False) -> int | None:
        if not rel:
            return None
        if rel in self.folders:
            return self.folders[rel]
        parent = self.folder(rel.rsplit("/", 1)[0] if "/" in rel else "")
        name = rel.rsplit("/", 1)[-1]
        fid = self.db.upsert_folder("local", rel, path_hash(rel), name=name, name_folded=fold(name),
                                    parent_id=parent, depth=rel.count("/") + 1, status="live",
                                    is_project=1 if project else 0)
        self.folders[rel] = fid
        if project:
            card = make_folder_card(name=name, rel_path=rel, file_count=3, languages={"Python": 3},
                                    markers=["README.md"], deps=["opencv"], readme_head="motion tracking robot",
                                    top_files=["main.py"], activity_min_iso=None, activity_max_iso=None)
            self.db.apply_chunks(file_id=None, folder_id=fid, source="local", diff=diff_chunks([], [card]))
        return fid

    def add(self, rel: str, blocks: list[Block], *, kind: str = "text", mtime: float = T0,
            status: str = "indexed", **extra) -> dict:
        folder_id = self.folder(rel.rsplit("/", 1)[0] if "/" in rel else "")
        name = rel.rsplit("/", 1)[-1]
        row = self.db.insert_file("local", rel, path_hash(rel), name=name, name_folded=fold(name),
                                  ext="." + name.rsplit(".", 1)[-1].lower(), kind=kind, folder_id=folder_id,
                                  status="dirty", size=1000, mtime=mtime, mtime_ns=int(mtime * 1e9), **extra)
        card = make_file_card(name=name, rel_path=rel, kind=kind, size=1000, mtime_iso="2026-09-20",
                              summary_text=blocks[0].text if blocks else None)
        diff = diff_chunks([], [card] + chunk_blocks(blocks))
        self.db.apply_chunks(file_id=row["id"], folder_id=folder_id, source="local", diff=diff,
                             file_fields={"status": status})
        return self.db.get_file(row["id"])

    def chunks(self, file_id: int) -> list[dict]:
        return self.db.chunks_of_file(file_id)

    def activate_vectors(self, gen: int = 1, model_key: str = "fake", dim: int = 4) -> None:
        self.db.add_collection(gen, "ud_test", model_key=model_key, dim=dim, store_key="s", state="active")
        ids = [r["id"] for r in self.db.read_sql("SELECT id FROM chunks")]
        self.db.set_vec_gen(ids, gen)


def _para(i: int, words: int = 40) -> str:
    return " ".join(f"w{i}x{j}" for j in range(words)) + "."


@pytest.fixture
def ix(tmp_path):
    index = Index(tmp_path)
    index.ai = index.add("Reports/ai.md", [
        Block(text="AI challenges: hallucination and privacy are the main problems.",
              locator={"line_start": 1, "line_end": 2}),
        Block(text=_para(1), locator={"line_start": 4, "line_end": 4}),
    ])
    index.garden = index.add("Reports/garden.md", [Block(text="Tomatoes and basil in the garden.",
                                                         locator={"line_start": 1, "line_end": 1})])
    index.robot = index.add("Projects/robot/main.py", [Block(text="def track(): motion tracking loop",
                                                             locator={"line_start": 1, "line_end": 3})],
                            kind="code")
    index.folder("Projects/robot", project=True)
    yield index
    index.db.close()


def _engine(ix, *, store=None, embedder=None, snapshot=None, handles=True):
    emb = embedder or FakeEmbedder()
    return QueryEngine("p", ix.db, tz=UTC, snapshot=snapshot or {},
                       vector_handles=(lambda: (emb, store or FakeStore())) if handles else (lambda: None))


def _body_ids(ix, row) -> list[int]:
    return [c["id"] for c in ix.chunks(row["id"]) if c["ctype"] == "body"]


# ── modes ──────────────────────────────────────────────────────────────────


def test_hybrid_when_both_lists_run(ix):
    ix.activate_vectors()
    store = FakeStore(hits=[(_body_ids(ix, ix.ai)[0], 0.9)])
    out = _engine(ix, store=store).search("hallucination privacy")
    assert out.mode == "hybrid"
    assert out.groups[0].file["id"] == ix.ai["id"]
    assert out.groups[0].best.ranks.keys() >= {"lexical", "vector"}


def test_partial_vectors_are_hybrid_partial(ix):
    ix.activate_vectors()
    ix.db.set_vec_gen(_body_ids(ix, ix.garden), 0)
    out = _engine(ix, store=FakeStore()).search("garden")
    assert out.mode == "hybrid_partial" and "cover" in out.mode_reason
    assert out.groups and out.groups[0].file["id"] == ix.garden["id"]


def test_a_store_error_degrades_to_keywords_and_never_escapes(ix):
    ix.activate_vectors()
    out = _engine(ix, store=FakeStore(exc=RuntimeError("connection refused"))).search("garden")
    assert out.mode == "lexical_only"
    assert "did not answer" in out.mode_reason
    assert out.groups[0].file["id"] == ix.garden["id"]


def test_no_embedder_or_embedding_off_is_lexical_only(ix):
    ix.activate_vectors()
    assert _engine(ix, handles=False).search("garden").mode == "lexical_only"
    off = _engine(ix, snapshot={"tool_mode": "lexical_only", "state": "suspended", "reason": "embedding_off"})
    out = off.search("garden")
    assert out.mode == "lexical_only" and out.mode_reason == "Vector Embedding is off"
    assert any("Vector Embedding is off" in n for n in out.notes)


def test_no_collection_or_a_changed_model_is_lexical_only(ix):
    out = _engine(ix).search("garden")
    assert out.mode == "lexical_only" and "no vectors" in out.mode_reason
    ix.activate_vectors(model_key="old-model")
    out = _engine(ix).search("garden")
    assert out.mode == "lexical_only" and "model changed" in out.mode_reason


def test_without_fts5_vectors_carry_the_search(ix):
    ix.activate_vectors()
    ix.db._lexical = "like"
    store = FakeStore(hits=[(_body_ids(ix, ix.garden)[0], 0.8)])
    out = _engine(ix, store=store).search("garden")
    assert out.mode == "vector_only"
    assert out.groups[0].file["id"] == ix.garden["id"]


def test_without_fts5_or_vectors_a_keyword_scan_still_answers(ix):
    ix.db._lexical = "like"
    out = _engine(ix, handles=False).search("tomatoes")
    assert out.mode == "lexical_only" and "keyword scan" in out.mode_reason
    assert out.groups[0].file["id"] == ix.garden["id"]


def test_when_nothing_can_search_the_catalog_answers(ix, monkeypatch):
    ix.db._lexical = "like"
    monkeypatch.setattr(ix.db, "like_search", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = _engine(ix, handles=False).search("tomatoes")
    assert out.mode == "catalog_only"
    assert {g.file["id"] for g in out.groups} == {ix.ai["id"], ix.garden["id"], ix.robot["id"]}


# ── stale vectors, scope and visibility ────────────────────────────────────


def test_stale_and_orphan_vector_hits_are_dropped(ix):
    ix.activate_vectors(gen=2)
    good = _body_ids(ix, ix.garden)[0]
    stale = _body_ids(ix, ix.ai)[0]
    ix.db.set_vec_gen([stale], 1)          # written for an older generation
    orphan = 999_999                       # its chunk was deleted
    store = FakeStore(hits=[(orphan, 0.99), (stale, 0.95), (good, 0.5)])
    engine = _engine(ix, store=store)
    scope = engine.scope(Filters())
    res = vector_module.search_vector(ix.db, "q", scope=scope, k=10, accept=engine.acceptor(scope),
                                      handles=(FakeEmbedder(), store))
    assert [cid for cid, _ in res.hits] == [good]
    assert res.stale_dropped == 2


def test_a_payload_that_disagrees_with_the_row_is_stale(ix):
    ix.activate_vectors()
    other_file_chunk = _body_ids(ix, ix.ai)[0]
    store = FakeStore(hits=[(other_file_chunk, 0.9)])
    engine = _engine(ix, store=store)
    scope = engine.scope(parse_filters({"file_ids": [ix.garden["cite_id"]]}))
    assert scope.file_ids == [ix.garden["id"]]
    res = vector_module.search_vector(ix.db, "q", scope=scope, k=5, accept=engine.acceptor(scope),
                                      handles=(FakeEmbedder(), store))
    assert res.hits == [] and res.stale_dropped == 1
    k, filt = store.calls[0]
    assert filt.file_ids == [ix.garden["id"]]   # pushed down as a payload filter


def test_a_large_scope_overfetches_then_escalates_once(ix, monkeypatch):
    ix.activate_vectors()
    monkeypatch.setattr(vector_module, "PREFILTER_MAX_FILES", 0)
    garden = _body_ids(ix, ix.garden)[0]
    ai = _body_ids(ix, ix.ai)[0]
    engine = _engine(ix)
    scope = engine.scope(parse_filters({"file_ids": [ix.garden["cite_id"]]}))
    # The store answers with out-of-scope hits first: a full page survives
    # the filter with too few hits, so the engine asks once more, wider.
    store = FakeStore(hits=[(ai, 0.9)] * 39 + [(garden, 0.5)])
    res = vector_module.search_vector(ix.db, "q", scope=scope, k=8, accept=engine.acceptor(scope),
                                      handles=(FakeEmbedder(), store))
    assert [c[0] for c in store.calls] == [40, vector_module.ESCALATE_TO]
    assert store.calls[0][1] is None or store.calls[0][1].file_ids is None
    assert [cid for cid, _ in res.hits] == [garden]


def test_missing_and_tombstoned_files_are_never_served(ix):
    ix.activate_vectors()
    ix.db.update_file(ix.garden["id"], status="missing")
    store = FakeStore(hits=[(_body_ids(ix, ix.garden)[0], 0.99)])
    out = _engine(ix, store=store).search("tomatoes garden")
    assert all(g.file["id"] != ix.garden["id"] for g in out.groups)
    found = _engine(ix, store=store).find("garden")
    assert all(it.row["id"] != ix.garden["id"] for it in found.items)


def test_group_by_folder_counts_the_project_card(ix):
    ix.activate_vectors()
    out = _engine(ix).search("motion tracking robot", group_by="folder")
    assert out.groups[0].kind == "folder"
    assert out.groups[0].folder["rel_path"] == "Projects/robot"
    kinds = {h.chunk["ctype"] for h in out.groups[0].passages}
    assert kinds & {"folder_card", "body"}


# ── the reader ─────────────────────────────────────────────────────────────


@pytest.fixture
def docs(ix):
    ix.law = ix.add("Legal/law.txt", vietnamese_law_blocks())
    ix.pdf = ix.add("Reports/report.pdf", [
        Block(text=f"Page {p} " + _para(p, 120), locator={"page": p}) for p in range(1, 9)
    ], kind="pdf")
    ix.sheet = ix.add("Data/book.xlsx", [
        # Sheets are hard anchors, as the extractor emits them: never one chunk.
        Block(text="| a | b |\n|---|---|\n| 1 | 2 |", locator={"sheet": "Q3", "range": "A1:B40"}, anchor=2),
        Block(text="| c | d |\n|---|---|\n| 3 | 4 |", locator={"sheet": "Q4", "range": "A1:B10"}, anchor=2),
    ], kind="xlsx")
    ix.slides = ix.add("Talks/deck.pptx", [
        Block(text=f"Slide {s} " + _para(s, 20), locator={"slide": s}, anchor=2) for s in range(1, 5)
    ], kind="pptx")
    return ix


def test_read_by_pages_lines_sheet_rows_slide_and_around(docs):
    e = _engine(docs)
    pages = e.read(docs.pdf["cite_id"], pages="3-4")
    assert pages.selected and all(c["locator"]["page"] in (3, 4) for c in pages.selected)
    assert pages.selection == "pages=3-4"
    lines = e.read(docs.ai["cite_id"], lines="4")
    assert lines.selected
    assert all(c["locator"]["line_start"] <= 4 <= c["locator"]["line_end"] for c in lines.selected)
    sheet = e.read("Data/book.xlsx", sheet="q3", rows="2-5")
    assert [c["locator"]["sheet"] for c in sheet.selected] == ["Q3"]
    slide = e.read(docs.slides["cite_id"], slide="2")
    assert [c["locator"]["slide"] for c in slide.selected] == [2]
    near = e.read(docs.ai["cite_id"], around="hallucination and privacy")
    assert near.focus_id is not None and len(near.selected) <= 3


def test_a_chunk_token_opens_around_its_passage(docs):
    target = next(c for c in docs.chunks(docs.pdf["id"]) if c["ctype"] == "body" and c["locator"]["page"] == 5)
    out = _engine(docs).read(f"[doc:{docs.pdf['cite_id']}#{target['text_hash'][:8]}]")
    assert out.focus_id == target["id"]
    assert {c["locator"]["page"] for c in out.selected} <= {4, 5, 6}


def test_reader_errors_are_specific(docs):
    e = _engine(docs)
    with pytest.raises(ReadError) as err:
        e.read(docs.pdf["cite_id"], pages="x-y")
    assert err.value.code == "InvalidLocator"
    with pytest.raises(ReadError) as err:
        e.read("Data/book.xlsx", sheet="Budget")
    assert err.value.code == "SheetNotFound" and "Q3" in err.value.candidates
    with pytest.raises(ReadError) as err:
        e.read("zzzz-nothing.txt")
    assert err.value.code == "NotFound"
    folder_cite = docs.db.folder_by_path("local", path_hash("Projects/robot"))["cite_id"]
    with pytest.raises(ReadError) as err:
        e.read(folder_cite)
    assert err.value.code == "IsAFolder"


def test_the_toc_follows_the_files_structure(docs):
    e = _engine(docs)
    law = e.read(docs.law["cite_id"])
    assert [t.arg for t in law.toc if "section" in t.arg][:2] == [{"section": "Điều 1"}, {"section": "Điều 2"}]
    assert any(t.arg == {"sheet": "Q4"} for t in e.read("Data/book.xlsx").toc)
    assert any(t.arg == {"slide": "3"} for t in e.read(docs.slides["cite_id"]).toc)
    assert all("pages" in t.arg for t in e.read(docs.pdf["cite_id"]).toc)


def _ctx(limit):
    def tokens(text):
        return len(text) // 4

    def fit_lines(text, cap, *, split_long_line=True):
        if tokens(text) <= cap:
            return text, False
        kept, used = [], 0
        for line in text.split("\n"):
            if used + tokens(line) + 1 > cap:
                break
            kept.append(line)
            used += tokens(line) + 1
        return "\n".join(kept), True

    return RenderContext(limit=limit, tokens=tokens, fit_lines=fit_lines,
                         cut_tokens=lambda text, n: text[: n * 4], wrap=lambda s: f"<<<\n{s}\n>>>")


def test_a_long_file_comes_back_as_an_envelope_within_budget(docs):
    out = _engine(docs).read(docs.pdf["cite_id"], query="Page 7")
    rendered = render_read(out, _ctx(900))
    assert len(rendered.text) // 4 <= 900
    assert "## Contents" in rendered.text and 'pages="' in rendered.text
    assert "## Parts matching" in rendered.text
    printed = {m.group(0) for m in TOKEN_RE.finditer(rendered.text)}
    assert printed == {c.token for c in rendered.citations}


def test_a_selection_too_long_is_read_in_parts(docs):
    e = _engine(docs)
    first = render_read(e.read(docs.pdf["cite_id"], pages="1-8"), _ctx(700))
    assert "Part 1 of" in first.text and "page=2" in first.text
    second = render_read(e.read(docs.pdf["cite_id"], pages="1-8", page=2), _ctx(700))
    assert "Part 2 of" in second.text
    assert not ({c.token for c in first.citations if c.text_hash} & {c.token for c in second.citations if c.text_hash})


def test_search_render_shrinks_but_keeps_every_token_whole(docs):
    docs.activate_vectors()
    out = _engine(docs).search("w1x1 w2x2 w3x3 Page nội dung quy định", top_k=8, group_by="chunk")
    assert len(out.groups) >= 4
    full = render_search(out, _ctx(None))
    small = render_search(out, _ctx(400))
    assert len(small.text) // 4 <= 400 < len(full.text) // 4
    printed = {m.group(0) for m in TOKEN_RE.finditer(small.text)}
    assert printed == {c.token for c in small.citations}
    assert "call again with top_k=" in small.text or "call again with page=" in small.text
    assert small.text.startswith("[Documentation Search · search")
