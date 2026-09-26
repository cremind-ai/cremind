"""How a research job's answers from the model are checked, and how the job
says why it ended as it did.

- A structured call names its function; an answer that is not a usable call
  of it — no call, another function, arguments that are not JSON, a missing
  field, a wrong type — is an error, retried once, never an empty result,
  and its tokens still count.
- A planner that fails twice leaves a job that searches the question itself
  and says so; a job whose findings calls all fail has failed.
- A checkpoint saved by the version that searched only for named laws (the
  reported jobs: every issue closed having read nothing) is brought up to
  date: its discovery is redone and the issues it closed are reopened.
- A resumed job keeps the work whose evidence still points at a passage the
  index holds, and reads again what re-chunking moved.
- The Research activity panel's one-line summary counts what an analysis
  read — files and provisions — not files read in full, which it never does
  for a reference (the reported panel said "0/1 files read" of a job that
  read 32 provisions of its decree).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.constants import ChatCompletionTypeEnum as CT
from app.documents.research import analyze as A
from app.documents.research.analyze import run_analyze
from app.documents.research.context import ResearchLLM, StructuredOutputError, schema_errors
from app.documents.research import jobs
from app.documents.research.types import (
    COMPLETE,
    FAILED,
    PARTIAL,
    READ_FULL,
    READ_NONE,
    READ_PARTIAL,
    ROLE_REFERENCE,
    CoverageRow,
    Dossier,
    Evidence,
    Finding,
    Issue,
    Outcome,
    dossier_from_dict,
)
from tests.documents._foreign_worker_regs import DECREE_EN, FOOD_REG, NEWS_NOTE
from tests.documents.test_research_analyze import Index, _engine, make_ctx
from tests.documents.test_research_discovery import DECREE, FOOD, NEWS, Q_MOVE, llm

TOOL = A.FINDINGS_TOOL


class Scripted:
    """A provider that answers each call with the next scripted response:
    ``("call", name, arguments)`` or ``("text", "…")``; records the kwargs."""

    provider_name, model_name = "fake", "fake-structured"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.kwargs: list[dict] = []

    async def chat_completion(self, *, messages, **kw):
        self.kwargs.append(kw)
        kind, *rest = self.responses.pop(0)
        if kind == "call":
            name, args = rest
            yield {"type": CT.FUNCTION_CALLING, "data": {"function": [{"name": name, "arguments": args}]}}
        else:
            yield {"type": CT.CONTENT, "data": rest[0]}
        yield {"type": CT.DONE, "input_tokens": 50, "output_tokens": 10}


def _call(llm, tool=TOOL):
    return asyncio.run(ResearchLLM(llm, budget=100_000).call(system="s", user="u", tool=tool))


GOOD = {"findings": [], "open_questions": []}


def test_a_structured_call_names_its_function_and_returns_checked_arguments():
    fake = Scripted(("call", "record_findings", json.dumps(GOOD)))
    assert _call(fake) == GOOD
    assert fake.kwargs[0]["tool_choice"] == {"type": "function", "function": {"name": "record_findings"}}


@pytest.mark.parametrize("response, words", [
    (("text", "Here are the findings: none."), "did not call record_findings"),
    (("call", "record_question", json.dumps(GOOD)), "(it called record_question)"),
    (("call", "record_findings", "{not json"), "not valid JSON"),
    (("call", "record_findings", json.dumps({"findings": []})), "open_questions: missing"),
    (("call", "record_findings", json.dumps({"findings": "none", "open_questions": []})), "expected array"),
    (("call", "record_findings", json.dumps([1, 2])), "not an object"),
])
def test_anything_but_a_usable_call_is_an_error_and_its_tokens_count(response, words):
    llm = ResearchLLM(Scripted(response), budget=100_000)
    with pytest.raises(StructuredOutputError) as ei:
        asyncio.run(llm.call(system="s", user="u", tool=TOOL))
    assert words in str(ei.value)
    assert llm.spent.total == 60  # the provider's own count, error or not


def test_one_bad_item_does_not_cost_the_whole_answer():
    args = {"findings": [{"stance": "supports", "text": "no evidence field"}], "open_questions": []}
    assert schema_errors(TOOL["function"]["parameters"], args) == []
    assert _call(Scripted(("call", "record_findings", args))) == args


def test_the_job_retries_a_malformed_answer_once(tmp_path):
    ix = Index(tmp_path / "a.db", "uid-a")
    try:
        ctx = make_ctx(_engine(ix, "a"), Scripted(("text", "prose"), ("call", "record_findings", GOOD)),
                       question="q", profile="a")
        out = asyncio.run(A._Analyze(ctx).ask(system="s", user="u", tool=TOOL))
        assert out == GOOD and ctx.llm.calls == 2
        ctx = make_ctx(_engine(ix, "a"), Scripted(("text", "prose"), ("text", "again")), question="q", profile="a")
        with pytest.raises(A._ModelFailed):
            asyncio.run(A._Analyze(ctx).ask(system="s", user="u", tool=TOOL))
    finally:
        ix.db.close()


@pytest.fixture
def alice(tmp_path):
    ix = Index(tmp_path / "alice.db", "uid-alice")
    ix.add(DECREE, DECREE_EN)
    ix.add(NEWS, NEWS_NOTE)
    ix.add(FOOD, FOOD_REG)
    yield ix
    ix.db.close()


def test_a_planner_that_fails_leaves_a_job_that_searches_the_question(alice):
    def broken(_s, _u):
        return None  # no function call at all

    fake = llm(record_question=broken)
    ctx = make_ctx(_engine(alice), fake, question=Q_MOVE)
    d = asyncio.run(run_analyze(ctx))
    plan = ctx.state["analyze"]["plan"]
    assert plan == {"instruments": [], "issues": [], "degraded": True}
    assert fake.names().count("record_question") == 2  # retried once
    assert any("could not be analysed by the model" in n for n in d.notes)
    # The question itself was the topic, and it found the decree.
    queries = [q["q"] for q in d.outcome.trace["queries"]]
    assert queries == [" ".join(Q_MOVE.split())[:300]]
    assert d.outcome.trace["selected"] == {alice.fid(DECREE): "topic"}


def test_findings_calls_that_all_fail_fail_the_job(alice):
    fake = llm(record_findings=lambda _s, _u: None)
    d = asyncio.run(run_analyze(make_ctx(_engine(alice), fake, question=Q_MOVE)))
    assert d.status == FAILED and d.outcome.reason == "model_failed" and d.outcome.findings == 0
    assert any("Could not analyse the provisions" in g for g in d.gaps)


def test_a_version_1_checkpoint_of_the_reported_kind_is_brought_up_to_date(alice):
    """The state the reported jobs saved: a plan naming no law, every issue
    closed having read nothing, the old path's gaps and note."""
    old_state = {"analyze": {
        "estimate": 101500, "budget_ok": True,
        "plan": {"instruments": [], "issues": [{"title": "Work in another province", "description": "d",
                                                "terms_vi": [], "terms_en": ["foreign worker work permit provinces"]}]},
        "issues": [{"title": "Work in another province", "description": "d", "terms_vi": [],
                    "terms_en": ["foreign worker work permit provinces notify"], "round": 0, "done": True,
                    "seen": [], "pending": [], "asked": [], "open": []}],
        "legal": {"found": [], "docs": {}, "chosen": {}, "why": {}, "search": []},
        "read": {}, "xrefs": 0,
    }}
    old = Dossier(job_id="job1", mode="analyze", domain="legal", question=Q_MOVE, status="interrupted",
                  issues=[Issue(title="Work in another province")],
                  gaps=["No legal document matching the instruments named was found among the indexed documents.",
                        "No reference documents to search for the issue: Work in another province",
                        "No provision with verified evidence was found for the issue: Work in another province"],
                  notes=["No reference scope was given: the authorities were found by searching all indexed "
                         "documents for (nothing named) — 0 document(s)."])
    dossier = dossier_from_dict(json.loads(json.dumps(old.to_dict())))
    assert dossier.outcome is None  # a dossier from before outcomes existed still loads
    fake = llm()
    ctx = make_ctx(_engine(alice), fake, question=Q_MOVE, state=old_state, dossier=dossier)
    d = asyncio.run(run_analyze(ctx))
    assert ctx.state["analyze"]["version"] == A.STATE_VERSION
    assert "found" not in ctx.state["analyze"]["legal"]
    assert d.status == COMPLETE and d.issues[0].findings
    assert not any(g.startswith("No reference documents to search") for g in d.gaps)
    assert not any("(nothing named)" in n for n in d.notes)
    # Nothing before the issues ran again (the plan is kept).
    assert "record_question" not in fake.names()


def test_a_resumed_job_keeps_valid_work_and_rereads_what_moved(alice):
    """A finding whose evidence points at a passage the index no longer has
    (the file was re-chunked) is dropped and its issue read again; a valid
    one is kept as it is."""
    fake = llm()
    ctx = make_ctx(_engine(alice), fake, question=Q_MOVE)
    d = asyncio.run(run_analyze(ctx))
    assert d.status == COMPLETE
    decree = alice.fid(DECREE)
    stale = Finding(issue=d.issues[0].title, stance="supports", text="An old reading.",
                    evidence=[Evidence(token=f"[doc:{decree}#deadbeef]", quote="a passage that moved",
                                       quote_status="exact")])
    d.issues[0].findings.insert(0, stale)
    spec = ctx.state["analyze"]["issues"][0]
    kept = [f.text for f in d.issues[1].findings]
    state = json.loads(json.dumps(ctx.state))
    state["analyze"]["issues"][0].update(done=True)
    fake2 = llm()
    ctx2 = make_ctx(_engine(alice), fake2, question=Q_MOVE, state=state,
                    dossier=dossier_from_dict(json.loads(json.dumps(d.to_dict()))))
    d2 = asyncio.run(run_analyze(ctx2))
    assert not any(f.text == "An old reading." for i in d2.issues for f in i.findings)
    assert any("pointed at passages that changed" in n for n in d2.notes)
    assert "record_findings" in fake2.names()  # the affected issue was read again
    assert [f.text for f in d2.issues[1].findings] == kept  # the other issue untouched
    assert spec["title"] == d2.issues[0].title and d2.issues[0].findings


def test_zero_authorities_is_never_a_complete_job(tmp_path):
    ix = Index(tmp_path / "empty.db", "uid-e")
    try:
        ix.add(NEWS, NEWS_NOTE)
        d = asyncio.run(run_analyze(make_ctx(_engine(ix, "e"), llm(), question=Q_MOVE, profile="e")))
        assert d.status == PARTIAL and d.outcome.reason == "candidates_rejected"
        assert d.authorities == [] and d.outcome.findings == 0 and d.outcome.candidates == 1
        ix2 = Index(tmp_path / "none.db", "uid-n")
        try:
            d = asyncio.run(run_analyze(make_ctx(_engine(ix2, "n"), llm(), question=Q_MOVE, profile="n")))
            assert d.status == PARTIAL and d.outcome.reason == "no_candidates" and d.outcome.queries >= 1
        finally:
            ix2.db.close()
    finally:
        ix.db.close()


def _panel(mode: str, coverage: list[CoverageRow], outcome: Outcome | None) -> str:
    findings = [Finding(issue="Moving a worker", stance="procedure", text=f"Finding {i}.",
                        evidence=[Evidence(token=f"[doc:aaaaaaaa#0000000{i}]", quote="q", quote_status="exact")])
                for i in range(3)]
    return jobs._summary(Dossier(job_id="job1", mode=mode, domain="legal", question=Q_MOVE, status=COMPLETE,
                                 coverage=coverage, issues=[Issue(title="Moving a worker", findings=findings)],
                                 outcome=outcome))


def test_the_panel_counts_what_an_analysis_read():
    decree = CoverageRow(fid="4px995dj", rel_path="ND-219-2025-CP.pdf", kind="pdf", role=ROLE_REFERENCE,
                         read=READ_PARTIAL)
    outcome = Outcome(reason="evidenced", detail="every issue has verified findings", files_read=1,
                      provisions_read=32, findings=3)
    assert _panel("analyze", [decree], outcome) == "1 file and 32 provisions read, 3 findings"
    stopped = Outcome(reason="insufficient_evidence", detail="insufficient evidence: 1 of 2 issue(s) have no "
                      "verified finding", files_read=2, provisions_read=1, findings=3)
    assert _panel("analyze", [decree], stopped) == ("2 files and 1 provision read, 3 findings — insufficient "
                                                    "evidence: 1 of 2 issue(s) have no verified finding")


def test_the_panel_still_counts_files_read_in_full_for_a_compilation():
    rows = [CoverageRow(fid=f"f{i}", rel_path=f"MKT/{i}.xlsx", kind="spreadsheet", read=r)
            for i, r in enumerate((READ_FULL, READ_FULL, READ_NONE))]
    outcome = Outcome(reason="compiled", detail="", files_read=2)
    assert _panel("compile", rows, outcome).startswith("2/3 files read")
    # A dossier saved before outcomes existed keeps the old count.
    assert _panel("analyze", rows, None).startswith("2/3 files read")
