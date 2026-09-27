"""Resolving a document the user names by number ("Decree 165") to the
indexed file that is that document — deterministically, before any relevance
judgement.

English fixtures first; Vietnamese stays part of the multilingual coverage
(the users' decrees are Vietnamese: "Nghị định 165", "165/2024/NĐ-CP").
"""

from __future__ import annotations

import pytest

from app.documents import identity as I
from app.documents.index import IndexDB
from app.documents.textnorm import text_hash
from app.documents.types import Chunk, ChunkDiff


def _refs(text):
    return [(r.parts.num, r.parts.year, r.parts.issuer, r.parts.kind) for r in I.parse_refs(text)]


def test_parse_short_and_complete_identifiers():
    assert _refs("What does Decree 165 cover?") == [(165, None, None, "decree")]
    assert _refs("Decree No. 165 of 2024") == [(165, 2024, None, "decree")]
    assert _refs("see 165/2024/NĐ-CP and Circular 15/2023/TT-BTC") == [
        (165, 2024, "ND-CP", "decree"), (15, 2023, "TT-BTC", "circular")]
    assert _refs("hãy đọc nghị định 165 và cho tôi biết") == [(165, None, None, "decree")]
    assert _refs("Nghi dinh so 165 nam 2024") == [(165, 2024, None, "decree")]
    assert _refs("ND 165") == [(165, None, None, "decree")]
    # No cue, no identity: a bare number, an article, a year after "Law".
    assert _refs("section 165") == []
    assert _refs("Article 165 of the code") == []
    assert _refs("the Land Law 2024") == []
    assert _refs("Law No. 2024") == [(2024, None, None, "law")]


def _ident(**kw):
    row = {"id": kw.pop("id", 1), "cite_id": kw.pop("fid", "aaaaaaaa"), "rel_path": kw.get("name", "x.pdf"),
           "name": kw.pop("name", "x.pdf"), "doc_meta": kw.pop("doc_meta", {})}
    return I.file_identity(row, kw.pop("head", None))


def test_file_identity_sources():
    a = _ident(doc_meta={"legal": {"number": "165/2024/NĐ-CP"}, "title": "Decree 165/2024/NĐ-CP"})
    assert a.parts.id == "165/2024/ND-CP" and a.source == "legal_meta" and a.parts.kind == "decree"
    assert a.parts.display() == "165/2024/NĐ-CP" and not a.appendix
    b = _ident(name="ND-165-2024-CP_Phu-luc.pdf")
    assert b.parts.id == "165/2024/ND-CP" and b.source == "filename" and b.appendix
    c = _ident(name="scan.pdf", head="CHÍNH PHỦ\nSố: 165/2024/NĐ-CP\nNGHỊ ĐỊNH")
    assert c.parts.id == "165/2024/ND-CP" and c.source == "header"
    d = _ident(name="annex-decree-219.pdf", doc_meta={"title": "Annex to Decree 219/2025/ND-CP"})
    assert d.appendix and d.parts.num == 219


def test_matching_rules():
    decree = _ident(doc_meta={"legal": {"number": "165/2024/NĐ-CP"}})
    other_year = _ident(doc_meta={"legal": {"number": "165/2013/NĐ-CP"}})
    near = _ident(doc_meta={"legal": {"number": "1650/2024/NĐ-CP"}})
    circular = _ident(doc_meta={"legal": {"number": "165/2024/TT-BTC"}})
    ref = I.parse_refs("Decree 165")[0]
    assert I.matches(ref, decree) and I.matches(ref, other_year)
    assert not I.matches(ref, near)            # 165 is not 1650
    assert not I.matches(ref, circular)        # a decree is not a circular
    ref_year = I.parse_refs("Decree 165 of 2024")[0]
    assert I.matches(ref_year, decree) and not I.matches(ref_year, other_year)
    full = I.parse_refs("165/2024/NĐ-CP")[0]
    assert I.matches(full, decree) and not I.matches(full, other_year) and not I.matches(full, circular)


def test_grouping_main_and_appendix_and_ambiguity():
    main = _ident(id=1, fid="main0001", name="ND-165-2024-CP.pdf",
                  doc_meta={"legal": {"number": "165/2024/NĐ-CP"}, "title": "Nghị định 165/2024/NĐ-CP"})
    annex = _ident(id=2, fid="annex001", name="ND-165-2024-CP_Phu-luc.pdf",
                   doc_meta={"legal": {"number": "165/2024/NĐ-CP"}, "title": "Phụ lục Nghị định 165/2024/NĐ-CP"})
    res = I.group(I.parse_refs("Nghị định 165")[0], [main, annex])
    assert res.status == I.RESOLVED and res.number == "165/2024/NĐ-CP"
    assert res.label == "Nghị định 165/2024/NĐ-CP"
    assert [f.fid for f in res.files] == ["main0001", "annex001"] and [f.fid for f in res.main] == ["main0001"]
    old = _ident(id=3, fid="old00001", name="ND-165-2013-CP.pdf", doc_meta={"legal": {"number": "165/2013/NĐ-CP"}})
    amb = I.group(I.parse_refs("Decree 165")[0], [main, annex, old])
    assert amb.status == I.AMBIGUOUS and len(amb.groups) == 2
    # The year disambiguates.
    res = I.group(I.parse_refs("Decree 165 of 2013")[0], [main, annex, old])
    assert res.status == I.RESOLVED and [f.fid for f in res.files] == ["old00001"]
    assert I.group(I.parse_refs("Decree 16")[0], [main, annex, old]).status == I.NOT_FOUND


@pytest.fixture
def db(tmp_path):
    d = IndexDB.open(str(tmp_path / "index.db"), profile_uid="u1")
    yield d
    d.close()


def _add(db, rel, *, doc_meta=None, texts=(), status="indexed"):
    row = db.insert_file("local", rel, f"h-{rel}", name=rel.rsplit("/", 1)[-1], kind="pdf", status=status,
                         doc_meta=doc_meta)
    chunks = [Chunk(ordinal=i, ctype="body", heading="", text=x, text_hash=text_hash("", x), locator={"page": 1})
              for i, x in enumerate(texts)]
    if chunks:
        db.apply_chunks(file_id=row["id"], folder_id=None, source="local", diff=ChunkDiff(add=chunks))
    return db.get_file(row["id"])


def test_resolve_against_the_index(db):
    main = _add(db, "Law/ND-165-2024-CP.pdf", doc_meta={"legal": {"number": "165/2024/NĐ-CP"}},
                texts=["GOVERNMENT\nNo.: 165/2024/NĐ-CP\nDECREE on road traffic"])
    annex = _add(db, "Law/ND-165-2024-CP_Phu-luc.pdf", texts=["APPENDIX"])
    # A scan with only its card still resolves by its file name.
    scan = _add(db, "Law/scan-decree-165.pdf", texts=["Số: 165/2024/NĐ-CP"])
    _add(db, "Law/ND-1650-2024-CP.pdf", doc_meta={"legal": {"number": "1650/2024/NĐ-CP"}})
    _add(db, "Notes/report-165.pdf", texts=["The report mentions 165 cases."])
    res = I.resolve(db, "Summarize Decree 165")
    assert len(res) == 1 and res[0].status == I.RESOLVED
    got = {f.fid for f in res[0].files}
    assert got == {main["cite_id"], annex["cite_id"], scan["cite_id"]}
    assert res[0].number == "165/2024/NĐ-CP"
    # Scope: only the files allowed.
    res = I.resolve(db, "Decree 165", file_ids=[annex["id"]])
    assert {f.fid for f in res[0].files} == {annex["cite_id"]}
    # Nothing named by number: nothing to resolve.
    assert I.resolve(db, "what about road traffic fines?") == []


def test_find_files_lists_the_identified_document_first(db):
    import datetime as _dt

    from app.documents.query.catalog import find
    from app.documents.query.engine import QueryEngine
    from app.documents.query.render import render_find
    from app.tools.builtin.documentation_search import render_context

    main = _add(db, "Law/ND-165-2024-CP.pdf", doc_meta={"legal": {"number": "165/2024/NĐ-CP"},
                                                         "title": "Nghị định 165/2024/NĐ-CP"},
                texts=["No.: 165/2024/NĐ-CP"])
    annex = _add(db, "Law/ND-165-2024-CP_Phu-luc.pdf", texts=["PHỤ LỤC"])
    _add(db, "Notes/decree-notes.pdf", texts=["Notes about many decrees and 165 other things."])
    engine = QueryEngine("alice", db, tz=_dt.timezone.utc, snapshot={}, vector_handles=lambda: None)
    out = find(engine, "Decree 165")
    assert [it.row["cite_id"] for it in out.items[:2]] == [main["cite_id"], annex["cite_id"]]
    assert out.items[0].reasons[0] == "identified as Nghị định 165/2024/NĐ-CP"
    assert out.items[1].reasons[0] == "appendix of Nghị định 165/2024/NĐ-CP"
    assert out.identity[0]["status"] == I.RESOLVED
    assert any("identified by its document number" in n for n in out.notes)
    rendered = render_find(out, render_context(None, budgeted=False))
    assert "identified as Nghị định 165/2024/NĐ-CP" in rendered.text
    assert rendered.data["identity"][0]["number"] == "165/2024/NĐ-CP"
    # A strict filename filter stays strict: "Decree 165" is no file name.
    none = find(engine, None, filters={"name_query": "Decree 165"})
    assert none.items == []
