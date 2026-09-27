"""Research over a document the question names by number — the three
reported conversations, reproduced with English fixtures.

- "Summarize Decree 165" over a scanned decree whose only stored chunk is its
  file card: the decree is FOUND and reported as waiting for OCR — never
  rejected as "not a legal document", never "not among the documents".
- "Summarize Decree 219" with the decree itself as the case scope: the same
  file is read as the case and as the authority, so legal findings come out.
  A decree that disappears from the index while the job runs is reported as
  read earlier and unavailable now, not as never indexed.
- "Read Decree 165: may traffic police stop vehicles to check alcohol?" with a
  filename filter that matches nothing and a relevance screen that rejects
  the decree: the decree is identified by its number (165 → 165/2024/ND-CP,
  never 1650), read, and the answer rests on what its provisions say —
  including that they say nothing about it.

Also: an ambiguous number asks which document; the bounded recovery pass;
the label is never "Decree 165 165"; two profiles never see each other's.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from app.documents import content as C
from app.documents import identity as ID
from app.documents.chunking import make_file_card
from app.documents.discovery.walker import path_hash
from app.documents.research import analyze as analyze_module
from app.documents.research.analyze import run_analyze
from app.documents.research.context import NeedsInput
from app.documents.research.types import NEEDS_CLARIFICATION
from app.documents.textnorm import fold
from tests.documents.test_research_analyze import T0, FakeLLM, Index, _engine, make_ctx, passages, resume

DECREE_165 = [
    "GOVERNMENT", "No.: 165/2024/ND-CP", "Hanoi, December 26, 2024",
    "DECREE", "On the handling of administrative violations in road traffic",
    "Article 1. Scope",
    "This Decree provides for administrative violations in road traffic and the sanctions for them.",
    "Article 2. Subjects of application",
    "This Decree applies to drivers, vehicle owners and the officers who handle violations.",
    "Article 3. Interpretation of terms",
    "Alcohol concentration means the amount of alcohol in the blood or breath of a driver.",
    "Article 4. Patrol and control",
    "1. Traffic police may stop vehicles to check the alcohol concentration of drivers during patrol and control.",
    "2. The stop must be made at a place that does not obstruct traffic.",
    "Article 5. Sanctions for alcohol",
    "1. A driver whose breath contains alcohol shall be fined, except where the concentration is below the "
    "detection threshold of the device.",
    "Article 6. Effect",
    "This Decree takes effect on January 1, 2025.",
]
# The same decree without the patrol provision: nothing in it says whether
# police may stop vehicles.
DECREE_165_SILENT = [x for x in DECREE_165 if "stop" not in x and not x.startswith("Article 4")]
APPENDIX_165 = [
    "APPENDIX", "Forms issued with Decree No. 165/2024/ND-CP",
    "Form 01. Record of administrative violation",
    "Form 02. Decision on sanction",
]
DECREE_1650 = [
    "GOVERNMENT", "No.: 1650/2024/ND-CP", "DECREE", "On fisheries",
    *[f"Article {i}. Fishing zone {i}\nVessels may fish in zone {i}." for i in range(1, 7)],
]
DECREE_219 = [
    "GOVERNMENT", "No.: 219/2025/ND-CP", "DECREE", "On foreign workers in Vietnam",
    "Article 1. Scope", "This Decree provides for work permits of foreign workers in Vietnam.",
    "Article 2. Work permit",
    "1. A foreign worker must hold a work permit issued by the provincial labour authority.",
    "Article 3. Exemptions",
    "1. A foreign worker is exempt from a work permit when working in Vietnam for less than 30 days.",
    "Article 4. Reissuance", "1. A work permit is reissued when it is lost.",
    "Article 5. Effect", "This Decree takes effect on August 7, 2025.",
]
TRAFFIC_NOTE = ["Notes from the driving course", "The instructor talked about alcohol checks by the police."]

MAIN = "Law/ND-165-2024-CP.txt"
ANNEX = "Law/ND-165-2024-CP_Appendix.txt"
NEAR = "Law/ND-1650-2024-CP.txt"
NOTE = "Notes/course.txt"
D219 = "Law/ND-219-2025-CP.txt"
SCAN = "Law/ND-165-2024-CP.pdf"

Q_ALCOHOL = "Read Decree 165 and tell me: may traffic police stop vehicles to check alcohol concentration?"
Q_SUMMARY_219 = "Summarize what Decree 219 regulates."

ISSUE_ALCOHOL = [{"title": "Authority to stop vehicles for alcohol checks",
                  "description": "Whether traffic police may stop vehicles to check drivers' alcohol concentration.",
                  "search_queries": ["traffic police stop vehicles alcohol concentration", "patrol and control"]}]
ISSUE_219 = [{"title": "What Decree 219 regulates",
              "description": "The subjects Decree 219 governs: work permits, exemptions, reissuance.",
              "search_queries": ["work permit foreign worker", "exempt work permit", "reissued work permit"]}]


def planner(instruments, issues):
    def handler(_system, _user):
        return {"instruments": instruments, "issues": issues}
    return handler


def reject_everything(_system, user):
    """A relevance screen that (wrongly) calls every candidate irrelevant —
    what rejected the decree in the reported conversation."""
    return {"documents": [{"fid": b[:8], "relevant": False, "why": "scripted"}
                          for b in user.split("### Candidate fid=")[1:]]}


def findings_for(*phrases):
    def handler(_system, user):
        out = []
        for token, text in passages(user).items():
            for phrase in phrases:
                if phrase in text:
                    out.append({"stance": "supports", "provision": "?", "text": f"The decree says: {phrase}",
                                "evidence": [{"token": token, "quote": phrase}]})
        return {"findings": out, "open_questions": []}
    return handler


def case_reader(_system, user):
    facts = []
    for token, text in passages(user).items():
        if "must hold a work permit" in text:
            facts.append({"text": "Foreign workers need a work permit.",
                          "evidence": [{"token": token, "quote": "must hold a work permit"}]})
    return {"facts": facts, "parties": [], "dates": [], "instruments": [
        {"name": "Decree", "number": "219/2025/ND-CP", "year": "2025"}], "issues": ISSUE_219}


I_165 = [{"name": "Decree 165", "aliases": ["Nghị định 165", "Decree No. 165"], "number": "165", "year": ""}]
I_219 = [{"name": "Decree 219", "aliases": ["Nghị định 219"], "number": "219", "year": ""}]


@pytest.fixture
def ix(tmp_path):
    index = Index(tmp_path / "alice.db", "uid-alice")
    yield index
    index.db.close()


def _text_of(d) -> str:
    return json.dumps(d.to_dict(), ensure_ascii=False)


def _add_scan(ix: Index, rel: str, *, pages: int = 8, reason: str = "awaiting_consent") -> dict:
    """A scanned PDF as the old pipeline left it: its file card and nothing
    else, every page waiting for OCR."""
    name = rel.rsplit("/", 1)[-1]
    rec = C.coverage_record(pages=pages, read_pages=pages, scanned=range(1, pages + 1), ocr={}, ocr_reason=reason)
    row = ix.db.insert_file("local", rel, path_hash(rel), name=name, name_folded=fold(name), ext=".pdf", kind="pdf",
                            status="dirty", size=1000, mtime=T0, mtime_ns=int(T0 * 1e9),
                            sha256=hashlib.sha256(rel.encode()).hexdigest(), doc_meta={"extraction": rec},
                            caption_state=reason)
    from app.documents.chunking import diff_chunks

    card = make_file_card(name=name, rel_path=rel, kind="pdf", size=1000, mtime_iso="2026-09-20")
    ix.db.apply_chunks(file_id=row["id"], folder_id=None, source="local", diff=diff_chunks([], [card]),
                       file_fields={"status": "indexed"})
    ix.rows[rel] = ix.db.get_file(row["id"])
    return ix.rows[rel]


# ── c50e6782: a named decree the screen rejects, a filename filter that misses ──


def test_the_named_decree_is_identified_and_read_although_the_screen_rejects_it(ix):
    ix.add(MAIN, DECREE_165)
    ix.add(ANNEX, APPENDIX_165)
    ix.add(NEAR, DECREE_1650)
    ix.add(NOTE, TRAFFIC_NOTE)
    fake = FakeLLM({"record_question": planner(I_165, ISSUE_ALCOHOL), "record_relevance": reject_everything,
                    "record_findings": findings_for("Traffic police may stop vehicles to check the alcohol")})
    # The agent's filename filter matches nothing: the job goes on from the question.
    ctx = make_ctx(_engine(ix), fake, question=Q_ALCOHOL, scope={"name_query": "Decree 165"})
    d = asyncio.run(run_analyze(ctx))
    trace = d.outcome.trace
    assert trace["identity"]["Decree 165"] == {"status": "resolved", "number": "165/2024/ND-CP",
                                               "files": [ix.fid(MAIN), ix.fid(ANNEX)]}
    assert trace["selected"][ix.fid(MAIN)] == "identity" and trace["selected"][ix.fid(ANNEX)] == "identity"
    assert ix.fid(NEAR) not in trace["selected"]          # 165 is not 1650
    assert d.outcome.findings >= 1 and d.outcome.files_read >= 1
    tokens = {e.token.split(":", 1)[1][:8] for i in d.issues for f in i.findings for e in f.evidence}
    assert tokens == {ix.fid(MAIN)}
    text = _text_of(d)
    assert "165 165" not in text
    assert "not among the reference documents" not in text


def test_a_named_decree_that_does_not_support_the_conclusion_is_read_and_says_so(ix):
    ix.add(MAIN, DECREE_165_SILENT)
    ix.add(NOTE, TRAFFIC_NOTE)
    fake = FakeLLM({"record_question": planner(I_165, ISSUE_ALCOHOL), "record_relevance": reject_everything,
                    "record_findings": findings_for("this phrase is not in the decree")})
    d = asyncio.run(run_analyze(make_ctx(_engine(ix), fake, question=Q_ALCOHOL)))
    # Read, and nothing verified: not "no relevant document".
    assert d.outcome.reason == "no_verified_findings", d.outcome
    assert d.outcome.files_read == 1 and d.outcome.provisions_read >= 1
    assert d.outcome.trace["selected"] == {ix.fid(MAIN): "identity"}


# ── 8791b02c: a scanned decree whose pages wait for OCR ─────────────────────


def test_a_scanned_decree_awaiting_ocr_is_found_not_rejected(ix):
    scan = _add_scan(ix, SCAN)
    ix.add(NOTE, TRAFFIC_NOTE)
    fake = FakeLLM({"record_question": planner(I_165, ISSUE_ALCOHOL), "record_relevance": reject_everything,
                    "record_findings": findings_for("anything")})
    d = asyncio.run(run_analyze(make_ctx(_engine(ix), fake, question="What does Decree 165 resolve?")))
    trace = d.outcome.trace
    assert trace["identity"]["Decree 165"]["files"] == [scan["cite_id"]]
    assert trace["rejected"][scan["cite_id"]] == "unreadable:awaiting_ocr"   # never "not_legal"
    assert trace["candidates"][scan["cite_id"]]["readiness"] == "awaiting_ocr"
    assert d.outcome.reason == "content_unavailable"
    gap = next(g for g in d.gaps if SCAN in g)
    assert "8 of 8 scanned pages not transcribed" in gap and "awaiting consent" in gap
    assert "not among" not in _text_of(d)


def test_a_partly_transcribed_scan_is_read_and_its_gap_reported(ix):
    # Pages 1-4 transcribed (merged, chunked by article like native text),
    # 5-8 still waiting: research reads what there is, and says what is not.
    rows = ix.add(MAIN, DECREE_165)
    rec = C.coverage_record(pages=8, read_pages=8, scanned=range(1, 9),
                            ocr={p: C.OCR_DONE for p in range(1, 5)}, ocr_reason="over_cap")
    ix.db.update_file(rows["id"], doc_meta={**(rows.get("doc_meta") or {}), "extraction": rec}, kind="pdf")
    ix.rows[MAIN] = ix.db.get_file(rows["id"])
    fake = FakeLLM({"record_question": planner(I_165, ISSUE_ALCOHOL),
                    "record_findings": findings_for("Traffic police may stop vehicles to check the alcohol")})
    ctx = make_ctx(_engine(ix), fake, question=Q_ALCOHOL, reference_scope={"folder": ["Law"]})
    with pytest.raises(NeedsInput) as ei:
        asyncio.run(run_analyze(ctx))
    clar = ei.value.clarification
    assert clar.kind == "unread" and clar.candidates[0]["reason"] == "ocr_incomplete"
    d = asyncio.run(run_analyze(resume(ctx, fake, answers={"confirm": True})))
    assert d.outcome.findings >= 1
    assert any("ocr_incomplete" in g for g in d.gaps)


# ── 0e02c861: the decree is the case; it is also the authority ─────────────


def test_a_decree_given_as_the_case_is_also_read_as_its_authority(ix):
    ix.add(D219, DECREE_219)
    fake = FakeLLM({"record_question": planner(I_219, ISSUE_219), "record_case": case_reader,
                    "record_findings": findings_for("must hold a work permit", "exempt from a work permit")})
    ctx = make_ctx(_engine(ix), fake, question=Q_SUMMARY_219, scope={"file_ids": [ix.fid(D219)]})
    d = asyncio.run(run_analyze(ctx))
    assert d.facts, "the case facts are still recorded"
    assert d.outcome.trace["selected"] == {ix.fid(D219): "identity"}
    assert d.outcome.findings >= 2 and d.outcome.reason == "evidenced"
    assert [a.fid for a in d.authorities if a.used] == [ix.fid(D219)]


def test_a_decree_that_disappears_during_the_job_is_reported_as_changed(ix):
    ix.add(D219, DECREE_219)
    row = ix.rows[D219]
    gone = {"done": False}

    def on_call(name, _user):
        # The decree leaves the index after its provisions were read.
        if name == "record_findings" and not gone["done"]:
            ix.db.delete_file(row["id"])
            gone["done"] = True

    fake = FakeLLM({"record_question": planner(I_219, ISSUE_219),
                    "record_findings": findings_for("must hold a work permit")}, on_call=on_call)
    d = asyncio.run(run_analyze(make_ctx(_engine(ix), fake, question=Q_SUMMARY_219)))
    assert d.outcome.reason == "document_changed", d.outcome
    assert d.outcome.findings == 0  # its evidence points at text the index no longer holds
    assert any("was selected earlier in this job but is no longer in the index" in g for g in d.gaps)
    assert str(row["id"]) in d.outcome.trace["vanished"]
    assert "not among" not in _text_of(d)


def test_a_resumed_job_whose_decree_went_away_says_so(ix):
    ix.add(D219, DECREE_219)
    ix.add(NOTE, TRAFFIC_NOTE)
    fake = FakeLLM({"record_question": planner(I_219, ISSUE_219), "record_findings": findings_for("nothing")})
    ctx = make_ctx(_engine(ix), fake, question=Q_SUMMARY_219)
    asyncio.run(run_analyze(ctx))
    assert ctx.state["analyze"]["legal"]["discovery"]["selected"] == [ix.rows[D219]["id"]]
    # Re-opened later (a question answered, a restart): the decree is gone.
    ix.db.delete_file(ix.rows[D219]["id"])
    for spec in ctx.state["analyze"]["issues"]:
        spec.update(done=False, round=0, pending=[], asked=[], open=[])
    d = asyncio.run(run_analyze(resume(ctx, fake)))
    assert any("was selected earlier in this job but is no longer in the index" in g for g in d.gaps)
    assert d.outcome.reason == "document_changed"


# ── identity: ambiguity, recovery, isolation ───────────────────────────────


def test_two_decrees_under_one_number_ask_which(ix):
    ix.add(MAIN, DECREE_165)
    old = [x.replace("165/2024/ND-CP", "165/2013/ND-CP").replace("2024", "2013") for x in DECREE_165]
    ix.add("Law/ND-165-2013-CP.txt", old)
    fake = FakeLLM({"record_question": planner(I_165, ISSUE_ALCOHOL),
                    "record_findings": findings_for("Traffic police may stop vehicles to check the alcohol")})
    ctx = make_ctx(_engine(ix), fake, question=Q_ALCOHOL)
    with pytest.raises(NeedsInput) as ei:
        asyncio.run(run_analyze(ctx))
    assert ei.value.status == NEEDS_CLARIFICATION
    clar = ei.value.clarification
    assert clar.kind == "document" and "document" in clar.answer_keys
    assert sorted(c["number"] for c in clar.candidates) == ["165/2013/ND-CP", "165/2024/ND-CP"]
    d = asyncio.run(run_analyze(resume(ctx, fake, answers={"document": "165/2024/ND-CP"})))
    assert d.outcome.trace["identity"]["Decree 165"]["files"] == [ix.fid(MAIN)]
    assert d.outcome.findings >= 1


def test_the_recovery_pass_adds_a_decree_that_became_readable(ix, monkeypatch):
    ix.add(MAIN, DECREE_165, status="dirty")   # still being indexed when the job looks
    ix.add(NOTE, TRAFFIC_NOTE)
    real_recover = analyze_module._Analyze.recover

    async def recover(self, references):
        # It finished indexing before the second look.
        from app.documents.chunking import chunk_blocks, diff_chunks
        from app.documents.types import Block

        blocks = [Block(text=x, locator={"line_start": i + 1, "line_end": i + 1}) for i, x in enumerate(DECREE_165)]
        rid = ix.rows[MAIN]["id"]
        ix.db.apply_chunks(file_id=rid, folder_id=None, source="local",
                           diff=diff_chunks(ix.db.get_chunks(rid), chunk_blocks(blocks)),
                           file_fields={"status": "indexed", "doc_meta": {"legal": {"number": "165/2024/ND-CP"}}})
        return await real_recover(self, references)

    monkeypatch.setattr(analyze_module._Analyze, "recover", recover)
    fake = FakeLLM({"record_question": planner(I_165, ISSUE_ALCOHOL),
                    "record_findings": findings_for("Traffic police may stop vehicles to check the alcohol")})
    d = asyncio.run(run_analyze(make_ctx(_engine(ix), fake, question=Q_ALCOHOL)))
    assert d.outcome.trace["recovery"]["added"] == [ix.fid(MAIN)]
    assert d.outcome.findings >= 1 and d.outcome.reason == "evidenced"
    assert not any("cannot be read yet" in g for g in d.gaps)


def test_identity_never_crosses_profiles(tmp_path):
    alice, bob = Index(tmp_path / "a.db", "uid-alice"), Index(tmp_path / "b.db", "uid-bob")
    try:
        alice.add(MAIN, DECREE_165)
        bob.add(NOTE, TRAFFIC_NOTE)
        assert ID.resolve(bob.db, "Decree 165")[0].status == ID.NOT_FOUND
        fake = FakeLLM({"record_question": planner(I_165, ISSUE_ALCOHOL), "record_findings": findings_for("x")})
        d = asyncio.run(run_analyze(make_ctx(_engine(bob, "bob"), fake, question=Q_ALCOHOL, profile="bob",
                                             job_id="jb")))
        assert alice.fid(MAIN) not in _text_of(d)
        assert d.outcome.trace["identity"]["Decree 165"]["status"] == "not_found"
    finally:
        alice.db.close()
        bob.db.close()


def test_unaccented_vietnamese_names_resolve_too(ix):
    ix.add(MAIN, DECREE_165)
    assert ID.resolve(ix.db, "doc nghi dinh 165 giup toi")[0].status == ID.RESOLVED
    assert ID.resolve(ix.db, "Nghị định số 165/2024/NĐ-CP")[0].status == ID.RESOLVED
