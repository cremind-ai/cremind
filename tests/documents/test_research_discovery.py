"""Legal research without a reference scope: the authorities are discovered.

The reported failures, in the incident's own shape but in English: a
question about a foreign worker moved to a branch in another province, over
an index that holds the decree that governs it — titled only "DECREE" (its
subject on the next line) — next to a note about work permits and an
unrelated regulation.

- Asked without naming any law, the job used to search for nothing and
  finish "complete" with no authority, no read and no finding. It must
  search the issues as topics, read the decree and cite it.
- Asked with the planner mistaking "work permit" for a law's name, the job
  used to reject the decree because its title does not say "work permit".
- A number alone ("No. 12/2030/GOV") is looked up as a number.
- Background documents never become authorities by ranking alone; an
  explicit reference scope stays restrictive; a law the question names that
  is not indexed stays an open requirement (never stood in for); a named
  edition that is not there is asked about.
- Generic "DECREE" headings never make two decrees one instrument.
- A provision longer than one reading is read to its end, or reported read
  in part.
- A small multilingual set: the reported Vietnamese passages, unaccented
  queries, and an English question over a Vietnamese decree.
- Two profiles never see each other's candidates, traces or citations.

The index is written directly (the real chunker and legal overlay) and
searched lexically; the model is scripted from the prompts it is shown, so
every token it cites is one the job really printed. Retrieval is keyword
only here — cross-language semantic recall is verified against the real
embedding model separately, not claimed by these tests.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from app.documents.research import analyze as analyze_module
from app.documents.research import legal as L
from app.documents.research.analyze import run_analyze
from app.documents.research.context import NeedsInput
from app.documents.research.types import COMPLETE, NEEDS_CLARIFICATION, PARTIAL, SUPERSEDED
from tests.documents._foreign_worker_regs import (
    DECREE_EN,
    DECREE_VI,
    FOOD_REG,
    NEWS_NOTE,
    act_edition,
    generic_decree,
    long_article_decree,
)
from tests.documents.test_research_analyze import FakeLLM, Index, _engine, make_ctx, passages

DECREE = "Regs/decree-foreign-workers.txt"
NEWS = "Notes/work-permit-tips.txt"
FOOD = "Food/food-safety.txt"

Q_MOVE = ("An employer moved a foreign worker who holds a work permit to its branch in another province. Does the "
          "work permit have to be issued again, and what must the employer do?")

ISSUES_EN = [
    {"title": "Work in another province",
     "description": "Whether moving the worker to a branch in another province needs a new or reissued work permit, "
                    "and what the employer must do.",
     "search_queries": ["foreign worker work permit several provinces notify",
                        "work permit same employer another province"]},
    {"title": "Reissuing a work permit",
     "description": "When a work permit must be reissued, and which documents the dossier needs.",
     "search_queries": ["reissue work permit cases change", "reissuance dossier documents work permit"]},
]

# Phrase -> stance: what the scripted model finds in a passage it is shown.
FINDINGS_EN = [
    ("notify the labour authority of each province", "procedure"),
    ("no new work permit is required", "supports"),
    ("A particular stated in the work permit changes", "condition"),
    ("The documents proving the change referred to in", "procedure"),
    ("The existing work permit, except where it was lost", "exception"),
    ("shall submit the reissuance dossier", "procedure"),
]


def question(instruments: list[dict[str, Any]] | None = None, issues=ISSUES_EN):
    def handler(_system: str, _user: str) -> dict:
        return {"instruments": instruments or [], "issues": issues}
    return handler


def screen(*words: str):
    """The scripted model's relevance judgement: a candidate is relevant when
    its identity and passages mention every one of ``words``."""
    def handler(_system: str, user: str) -> dict:
        docs = []
        for block in user.split("### Candidate fid=")[1:]:
            text = block.lower()
            docs.append({"fid": block[:8], "relevant": all(w.lower() in text for w in words), "why": "scripted"})
        return {"documents": docs}
    return handler


def findings(pairs=FINDINGS_EN):
    def handler(_system: str, user: str) -> dict:
        out = []
        for token, text in passages(user).items():
            for phrase, stance in pairs:
                if phrase in text:
                    out.append({"stance": stance, "provision": "?", "text": f"{stance}: {phrase}",
                                "evidence": [{"token": token, "quote": phrase}]})
        return {"findings": out, "open_questions": []}
    return handler


def llm(**handlers) -> FakeLLM:
    base = {"record_question": question(), "record_relevance": screen("foreign worker", "work permit"),
            "record_findings": findings()}
    return FakeLLM({**base, **handlers})


@pytest.fixture
def alice(tmp_path):
    ix = Index(tmp_path / "alice.db", "uid-alice")
    ix.add(DECREE, DECREE_EN)
    ix.add(NEWS, NEWS_NOTE)
    ix.add(FOOD, FOOD_REG)
    yield ix
    ix.db.close()


def _cite(token: str) -> str:
    return token.split(":", 1)[1].split("#", 1)[0]


def _trace(d) -> dict:
    return d.outcome.trace or {}


# ── the two reported paths ─────────────────────────────────────────────────


def test_no_law_named_still_finds_reads_and_cites_the_governing_decree(alice):
    fake = llm()
    ctx = make_ctx(_engine(alice), fake, question=Q_MOVE)
    d = asyncio.run(run_analyze(ctx))
    decree = alice.fid(DECREE)
    assert d.status == COMPLETE and d.outcome.reason == "evidenced"
    # Discovery searched the issues as topics and selected the decree.
    trace = _trace(d)
    assert trace["selected"] == {decree: "topic"}
    assert {q["kind"] for q in trace["queries"]} == {"topic"}
    assert d.outcome.queries == len(trace["queries"]) >= 4 and d.outcome.selected == 1
    assert [a.fid for a in d.authorities if a.used] == [decree]
    # Every issue has verified findings citing the decree's own passages.
    assert all(i.findings for i in d.issues)
    tokens = [e.token for i in d.issues for f in i.findings for e in f.evidence]
    assert tokens and {_cite(t) for t in tokens} == {decree}
    notify = next(f for i in d.issues for f in i.findings if f.stance == "procedure"
                  and "each province" in f.evidence[0].quote)
    assert notify.provision.startswith("Article 22") and "12/2030/GOV" in notify.provision
    # Articles 22-25 were read, and the decree is named by its subject, not
    # by the bare heading "DECREE".
    assert d.outcome.files_read == 1 and d.outcome.provisions_read >= 3
    auth = next(a for a in d.authorities if a.fid == decree)
    assert auth.title.startswith("Decree on the employment of foreign workers")


def test_a_topic_the_planner_took_for_a_law_does_not_exclude_the_decree(alice):
    for shape in ({"name": "work permit", "aliases": ["giấy phép lao động"], "number": "", "year": ""},
                  {"name": "Work permit (GPLĐ)", "name_vi": "Giấy phép lao động", "name_en": "Work permit",
                   "number": "", "year": ""}):  # the shape older checkpoints hold
        fake = llm(record_question=question([shape]))
        d = asyncio.run(run_analyze(make_ctx(_engine(alice), fake, question=Q_MOVE, job_id=f"j{len(shape)}")))
        assert d.status == COMPLETE, (shape, d.gaps)
        assert _trace(d)["selected"] == {alice.fid(DECREE): "topic"}
        # The topic was searched as a topic, and is no missing law.
        assert any(q["q"].lower().startswith("work permit") and q["kind"] == "topic" for q in _trace(d)["queries"])
        assert d.outcome.unresolved == 0 and not any("names work permit" in g.lower() for g in d.gaps)


def test_a_bare_number_is_looked_up_as_a_number(alice):
    fake = llm(record_question=question([]))
    q = "Under No. 12/2030/GOV, which documents does a reissuance dossier need?"
    d = asyncio.run(run_analyze(make_ctx(_engine(alice), fake, question=q)))
    trace = _trace(d)
    assert {"q": "12/2030/GOV", "kind": "named"}.items() <= next(
        x for x in trace["queries"] if x["q"] == "12/2030/GOV").items()
    assert trace["selected"] == {alice.fid(DECREE): "named"}
    assert d.outcome.findings > 0


def test_background_documents_never_become_authorities(alice):
    d = asyncio.run(run_analyze(make_ctx(_engine(alice), llm(), question=Q_MOVE)))
    rejected = _trace(d)["rejected"]
    # The note mentions work permits everywhere, but is no legal document; the
    # food regulation is legal, and about something else.
    assert rejected[alice.fid(NEWS)] == "not_legal"
    assert rejected[alice.fid(FOOD)] == "not_relevant"
    assert {a.fid for a in d.authorities} == {alice.fid(DECREE)}
    assert {c.fid for c in d.coverage} == {alice.fid(DECREE)}


# ── what must stay restrictive ─────────────────────────────────────────────


def test_an_explicit_reference_scope_stays_restrictive(alice):
    fake = llm()
    ctx = make_ctx(_engine(alice), fake, question=Q_MOVE, reference_scope={"folder": ["Food"]},
                   answers={"confirm": True})
    d = asyncio.run(run_analyze(ctx))
    assert d.status == PARTIAL and d.outcome.reason == "no_verified_findings"
    assert d.outcome.trace is None  # nothing was discovered: the scope was given
    shown = " ".join(u for n, u in fake.log if n == "record_findings")
    assert "foreign workers" not in shown and alice.fid(DECREE) not in shown
    assert {c.fid for c in d.coverage} == {alice.fid(FOOD)}


def test_an_empty_reference_scope_is_reported_as_such(alice):
    ctx = make_ctx(_engine(alice), llm(), question=Q_MOVE, reference_scope={"path_glob": ["Nowhere/**"]})
    d = asyncio.run(run_analyze(ctx))
    assert d.status == PARTIAL and d.outcome.reason == "empty_scope"
    assert "The reference scope matched no files." in d.gaps
    assert not any("upload" in g.lower() for g in d.gaps)


def test_a_named_law_that_is_not_indexed_stays_an_open_requirement(alice):
    fake = llm(record_question=question([{"name": "Seafarers Act", "aliases": [], "number": "", "year": ""}]))
    q = "Under the Seafarers Act, does a foreign worker moved to another province need a new work permit?"
    d = asyncio.run(run_analyze(make_ctx(_engine(alice), fake, question=q)))
    # The decree answers the topic, but does not stand in for the Act.
    assert d.status == PARTIAL and d.outcome.reason == "unresolved_instrument"
    assert "Seafarers Act" in d.outcome.detail and d.outcome.unresolved == 1
    assert any("Seafarers Act" in g and "no other document stands in for it" in g for g in d.gaps)
    assert d.outcome.findings > 0
    assert not any("Seafarers" in (a.why or "") for a in d.authorities)


def test_a_named_edition_that_is_not_there_is_asked_about(tmp_path):
    ix = Index(tmp_path / "acts.db", "uid-acts")
    try:
        ix.add("Acts/lma-2020.txt", act_edition(2020, "3/2020/NA", "1 July 2020"))
        ix.add("Acts/lma-2030.txt", act_edition(2030, "9/2030/NA", "1 July 2030", extra=" Late registration is "
                                                                                          "fined."))
        inst = [{"name": "Labour Migration Act 2025", "aliases": [], "number": "", "year": "2025"}]
        fake = llm(record_question=question(inst, issues=[{
            "title": "Registration after a move", "description": "Must a worker who moves register?",
            "search_queries": ["worker moves province register"]}]))
        q = "Under the Labour Migration Act 2025, must a worker who moves to another province register?"
        ctx = make_ctx(_engine(ix, "acts"), fake, question=q, profile="acts")
        with pytest.raises(NeedsInput) as ei:
            asyncio.run(run_analyze(ctx))
        assert ei.value.status == NEEDS_CLARIFICATION and ei.value.clarification.kind == "edition"
        assert "2025" in ei.value.clarification.question
        assert {c["number"] for c in ei.value.clarification.candidates} == {"3/2020/NA", "9/2030/NA"}
        assert "record_findings" not in fake.names()  # nothing concluded from a substitute
    finally:
        ix.db.close()


# ── identity: generic headings ─────────────────────────────────────────────


def test_generic_decree_headings_never_make_one_instrument(tmp_path):
    ix = Index(tmp_path / "gen.db", "uid-gen")
    try:
        a = ix.add("Decrees/a.txt", generic_decree("30/2031/GOV", "road traffic fines", "1 January 2031"))
        b = ix.add("Decrees/b.txt", generic_decree("41/2032/GOV", "food labelling", "1 January 2032"))
        docs = {r["cite_id"]: L.doc_from_index(r, ix.db.chunks_of_file(r["id"])) for r in (a, b)}
        da, db_ = docs[a["cite_id"]], docs[b["cite_id"]]
        assert da.title == db_.title == "DECREE"
        assert (da.family, db_.family) == ("number:30/2031/GOV", "number:41/2032/GOV")
        # The later decree does not replace the earlier one.
        status = L.judge_status(docs, "2033-01-01")
        assert status[da.fid].status != SUPERSEDED and status[db_.fid].status != SUPERSEDED
        sel = L.select_editions(docs, [], case_day=None, today="2033-01-01", answers={})
        assert sorted(sel.chosen.values()) == sorted([[da.fid], [db_.fid]]) and sel.clarification is None
        # "Nghị định"/"Decree" alone matches no generic family by words;
        # the exact number still does.
        fams = L.families(docs)
        assert L.match_family(L.Named(names=["Decree"]), fams, loose=set()) is None
        assert L.match_family(L.Named(names=[], number="41/2032/GOV"), fams) == "number:41/2032/GOV"
    finally:
        ix.db.close()


def test_a_decree_with_a_subject_line_is_named_by_it(tmp_path):
    ix = Index(tmp_path / "vi.db", "uid-vi")
    try:
        row = ix.add("Decrees/nd219.txt", DECREE_VI)
        doc = L.doc_from_index(row, ix.db.chunks_of_file(row["id"]))
        assert doc.title == "Nghị định quy định về người lao động nước ngoài làm việc tại Việt Nam"
        assert doc.number == "219/2025/NĐ-CP" and not L.is_identity_key(doc.family)
        assert doc.label.endswith("219/2025/NĐ-CP")
        en = ix.add("Decrees/en.txt", DECREE_EN)
        assert L.doc_from_index(en, ix.db.chunks_of_file(en["id"])).title == \
            "Decree on the employment of foreign workers"
    finally:
        ix.db.close()


def test_explicit_instruments_and_topics_are_told_apart():
    assert L.is_explicit_instrument(["Luật Đất đai"], None) and L.is_explicit_instrument(["Labour Code"], None)
    assert L.is_explicit_instrument([], "12/2030/GOV")
    assert not L.is_explicit_instrument(["work permit", "Giấy phép lao động"], None)
    assert not L.is_explicit_instrument(["employment contract"], None)  # "Act" is no part of "contract"
    q = "Under the Seafarers Act and No. 12/2030/GOV, is a work permit needed?"
    items = [{"name": "Seafarers Act", "aliases": [], "number": "", "year": ""},
             {"name": "work permit", "aliases": [], "number": "", "year": ""},
             {"name": "Labour Code", "aliases": [], "number": "", "year": ""},  # not in the question
             {"name": "Decree", "aliases": [], "number": "12/2030/GOV", "year": ""}]
    named = L.named_from_plan(q, items)
    assert [(n.names[:1], n.number) for n in named] == [(["Seafarers Act"], None), (["Decree"], "12/2030/GOV")]
    assert L.topics_from_plan(q, items) == ["work permit", "Labour Code"]


# ── reading a provision to its end ─────────────────────────────────────────


def _long_env(tmp_path):
    ix = Index(tmp_path / "long.db", "uid-long")
    ix.add("Regs/records.txt", long_article_decree())
    art7 = [c for c in ix.db.chunks_of_file(ix.rows["Regs/records.txt"]["id"])
            if str(c.get("section_key") or "").split("/")[0] == "art:7"]
    issues = [{"title": "Assignment records", "description": "What records must the employer keep?",
               "search_queries": ["employer record foreign worker assignment province branch"]}]
    fake = llm(record_question=question([], issues=issues),
               record_relevance=screen("assignment record"),
               record_findings=findings([("shall keep record 1 of the foreign worker", "procedure")]))
    return ix, art7, fake


def test_a_long_provision_is_read_to_its_end(tmp_path, monkeypatch):
    # One reading per round: the later parts can only arrive as continuations.
    monkeypatch.setattr(analyze_module, "UNIT_MAX_TOKENS", 250)
    monkeypatch.setattr(analyze_module, "PROVISION_TOKENS", 1)
    monkeypatch.setattr(analyze_module, "MAX_CONTINUATION_ROUNDS", 10)
    ix, art7, fake = _long_env(tmp_path)
    try:
        assert len(art7) >= 3
        ctx = make_ctx(_engine(ix, "long"), fake, question="Which assignment records must an employer keep?",
                       profile="long")
        d = asyncio.run(run_analyze(ctx))
        read = set(ctx.state["analyze"]["read"][str(ix.rows["Regs/records.txt"]["id"])])
        assert {int(c["id"]) for c in art7} <= read
        assert d.outcome.incomplete_provisions == []
        shown = [u for n, u in fake.log if n == "record_findings"]
        assert any("(part 2 of" in u for u in shown)
    finally:
        ix.db.close()


def test_a_long_provision_not_finished_is_reported_read_in_part(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze_module, "UNIT_MAX_TOKENS", 250)
    monkeypatch.setattr(analyze_module, "PROVISION_TOKENS", 1)
    monkeypatch.setattr(analyze_module, "MAX_ROUNDS", 1)
    monkeypatch.setattr(analyze_module, "MAX_CONTINUATION_ROUNDS", 0)
    ix, art7, fake = _long_env(tmp_path)
    try:
        ctx = make_ctx(_engine(ix, "long"), fake, question="Which assignment records must an employer keep?",
                       profile="long")
        d = asyncio.run(run_analyze(ctx))
        assert d.outcome.incomplete_provisions and d.outcome.incomplete_provisions[0].startswith("Article 7")
        assert any(g.startswith("Provision read only in part: Article 7") for g in d.gaps)
        assert d.outcome.stopped_early
    finally:
        ix.db.close()


# ── multilingual ───────────────────────────────────────────────────────────

ISSUES_VI = [{"title": "Làm việc tại tỉnh khác",
              "description": "Chuyển người lao động nước ngoài sang chi nhánh tỉnh khác có phải cấp lại giấy phép "
                             "lao động không.",
              "search_queries": ["giấy phép lao động làm việc tại nhiều tỉnh thông báo",
                                 "cấp lại giấy phép lao động thay đổi nội dung"]}]
FINDINGS_VI = [("người sử dụng lao động phải thông báo cho cơ quan có thẩm quyền", "procedure"),
               ("Giấy tờ chứng minh thay đổi nội dung", "procedure")]
Q_VI = ("Người lao động nước ngoài đã có giấy phép lao động, nay chuyển sang làm việc tại chi nhánh ở tỉnh khác. "
        "Có phải cấp lại giấy phép lao động không?")


@pytest.mark.parametrize("q, issues", [
    (Q_VI, ISSUES_VI),
    # Typed without accents: the folded copy of the index still matches.
    (Q_VI, [{**ISSUES_VI[0], "search_queries": ["giay phep lao dong lam viec tai nhieu tinh thong bao"]}]),
    # An English question over the Vietnamese decree: the planner gives the
    # regulation's own (Vietnamese) words among its queries.
    (Q_MOVE, [{"title": "Work in another province", "description": "Is a reissued work permit needed?",
               "search_queries": ["work permit another province", "giấy phép lao động làm việc tại nhiều tỉnh"]}]),
])
def test_the_reported_vietnamese_decree_is_found_from_any_wording(tmp_path, q, issues):
    ix = Index(tmp_path / "vi.db", "uid-vi")
    try:
        ix.add("Decrees/ND-219-2025-CP.txt", DECREE_VI)
        ix.add("Notes/ghi-chu.txt", ["Ghi chú về giấy phép lao động", "Nhiều người hỏi về giấy phép lao động."])
        fake = llm(record_question=question([], issues=issues),
                   record_relevance=screen("người lao động nước ngoài", "giấy phép lao động"),
                   record_findings=findings(FINDINGS_VI))
        d = asyncio.run(run_analyze(make_ctx(_engine(ix, "vi"), fake, question=q, profile="vi")))
        decree = ix.fid("Decrees/ND-219-2025-CP.txt")
        assert _trace(d)["selected"] == {decree: "topic"}
        assert d.status == COMPLETE and d.outcome.findings >= 1
        assert {_cite(e.token) for i in d.issues for f in i.findings for e in f.evidence} == {decree}
    finally:
        ix.db.close()


# ── profiles ───────────────────────────────────────────────────────────────


def test_two_profiles_discover_only_their_own_documents(alice, tmp_path):
    bob = Index(tmp_path / "bob.db", "uid-bob")
    try:
        bob.add(NEWS, NEWS_NOTE)
        bob.add(FOOD, FOOD_REG)
        fa, fb = llm(), llm()
        ca = make_ctx(_engine(alice), fa, question=Q_MOVE, profile="alice", job_id="ja")
        cb = make_ctx(_engine(bob, "bob"), fb, question=Q_MOVE, profile="bob", job_id="jb")

        async def both():
            return await asyncio.gather(run_analyze(ca), run_analyze(cb))

        da, db_ = asyncio.run(both())
        bob_ids = {r["cite_id"] for r in bob.db.list_files(source="local", limit=100)}
        alice_ids = {r["cite_id"] for r in alice.db.list_files(source="local", limit=100)}
        tb = _trace(db_)
        seen_b = {f for q in tb["queries"] for f in q.get("found", [])} | set(tb["rejected"]) | set(tb["selected"])
        assert seen_b <= bob_ids and not seen_b & alice_ids
        assert db_.status == PARTIAL and db_.outcome.reason == "candidates_rejected"
        assert not any(re.search(r"12/2030|foreign workers", u) for _n, u in fb.log if _n != "record_question")
        assert {_cite(e.token) for i in da.issues for f in i.findings for e in f.evidence} <= alice_ids
    finally:
        bob.db.close()
