"""What a research result hands the chat agent, and page 1 under any budget.

- Page 1 of a settled dossier always opens with the status, the outcome in
  counts and the coverage totals — outside the document block, before it —
  however small the tool-result budget and however many notes come first;
  a settled analysis ends with the response contract.
- The typed :class:`ResearchDelivery` distinguishes the dossier's verified
  evidence from the tokens this page printed, and says which pages carry the
  rest; a failed call hands nothing over.
- A settled legal analysis with no verified finding carries the server's own
  insufficiency summary (in the question's language), which never asks the
  user to upload a document the index already holds.
"""

from __future__ import annotations

import pytest

from app.documents.research import render as R
from app.documents.research.handoff import (
    RESPONSE_CONTRACT,
    ResearchDelivery,
    build_delivery,
    failed_delivery,
    insufficiency_summary,
    needs_insufficiency,
)
from app.documents.research.types import (
    COMPLETE,
    FAILED,
    PARTIAL,
    RUNNING,
    Dossier,
    Evidence,
    Finding,
    Issue,
    JobView,
    Outcome,
)
from tests.documents.test_research_render import JOB, _ctx, _dossier, _ev, _printed, _view, index  # noqa: F401


def _outcome(**kw) -> Outcome:
    return Outcome(**{"reason": "evidenced", "detail": "every issue has verified findings", "queries": 6,
                      "candidates": 3, "selected": 1, "files_read": 1, "provisions_read": 4, "findings": 1, **kw})


def test_page_one_keeps_status_outcome_and_coverage_under_a_tiny_budget(index):
    db, files = index
    d = _dossier(files, status=PARTIAL, outcome=_outcome(reason="insufficient_evidence", unresolved=1,
                                                          detail="insufficient evidence: 1 of 2 issue(s) have no "
                                                                 "verified finding"))
    d.notes = [f"Note number {i}: " + "a long note about how the job went " * 6 for i in range(12)]
    view = _view(PARTIAL, d)
    text = R.render_job(view, ctx=_ctx(700), db=db).text
    head, _, _rest = text.partition("<<<")
    assert "Status: partial — insufficient evidence: 1 of 2 issue(s) have no verified finding." in head
    assert "Outcome: insufficient_evidence — 1 verified finding; 1 file and 4 provisions read" in head
    assert "6 discovery searches found 3 candidate documents, 1 selected; 1 unresolved" in head
    assert "Coverage: 3 files in scope (2 primary, 1 reference) — 2 read in full, 0 partly, 1 not read." in head
    assert RESPONSE_CONTRACT in text
    assert R.render_job(view, ctx=_ctx(700), db=db).data["pages"] > 1


def test_a_complete_analysis_says_every_issue_is_evidenced(index):
    db, files = index
    view = _view(COMPLETE, _dossier(files, outcome=_outcome()))
    text = R.render_job(view, ctx=_ctx(None), db=db).text
    assert "Status: complete — every issue has verified findings." in text
    assert "Never say an indexed document is missing or ask the user to upload it again" in text


def test_a_dossier_saved_before_outcomes_still_renders(index):
    db, files = index
    text = R.render_job(_view(COMPLETE, _dossier(files)), ctx=_ctx(1500), db=db).text
    assert "Status: complete." in text and "Outcome:" not in text and "Coverage: 3 files in scope" in text


def test_the_delivery_record_separates_dossier_evidence_from_this_page(index):
    db, files = index
    d = _dossier(files, outcome=_outcome())
    # A second finding, so the dossier holds evidence page 1 may not show.
    d.issues.append(Issue(title="Second issue", findings=[Finding(
        issue="Second issue", stance="procedure", text="The court applies its own procedure.",
        evidence=[_ev(files, "Luat/luat-dat-dai-2024.txt", 1)])]))
    d.notes = [f"Note {i}: " + "padding words for the page " * 10 for i in range(10)]
    view = _view(COMPLETE, d)
    rendered = R.render_job(view, ctx=_ctx(600), db=db)
    rec = build_delivery(view, text=rendered.text, page=1, pages=rendered.data["pages"],
                         evidence_pages=rendered.data["evidence_pages"], page_tokens=rendered.data["page_tokens"])
    assert rec.valid and rec.settled and rec.findings == 2 and len(rec.evidence) == 2
    assert rec.page_tokens == 600  # what the agent budgets its automatic page reads by
    assert set(rec.delivered) == _printed(rendered.text) & set(rec.evidence)
    assert rec.evidence_pages and all(1 <= p <= rec.pages for p in rec.evidence_pages)
    missing = set(rec.evidence) - set(rec.delivered)
    for p in rec.evidence_pages:
        page = R.render_job(view, ctx=_ctx(600), db=db, page=p).text
        missing -= _printed(page)
    assert not missing  # every piece of evidence is on a page the record names
    again = ResearchDelivery.from_dict(rec.to_dict())
    assert again == rec and rec.insufficiency is None


def test_a_failed_call_hands_nothing_over():
    rec = failed_delivery("ResearchBusy")
    assert not rec.valid and rec.error == "ResearchBusy"
    assert ResearchDelivery.from_dict({"not": "a record"}) is not None  # unknown keys ignored…
    assert not ResearchDelivery.from_dict({"not": "a record"}).valid  # …and it is no handoff
    assert ResearchDelivery.from_dict("text") is None
    # A record saved before page budgets were recorded, or a mangled one.
    assert ResearchDelivery.from_dict({"job_id": "j", "page_tokens": None}).page_tokens == 0
    assert ResearchDelivery.from_dict({"job_id": "j", "page_tokens": "lots"}).page_tokens == 0


def _empty_legal(status=PARTIAL, *, reason="candidates_rejected", question="Does the permit need reissuing?"):
    d = Dossier(job_id=JOB, mode="analyze", domain="legal", question=question, status=status,
                issues=[Issue(title="Reissuing a work permit"), Issue(title="Notifying the authority")],
                gaps=["Candidate documents were found, but none was a relevant legal document.",
                      "No provision with verified evidence was found for the issue: Reissuing a work permit"],
                outcome=Outcome(reason=reason, detail="candidate documents were found but none was a relevant "
                                                      "legal document", queries=6, candidates=4))
    return JobView(job_id=JOB, profile="alice", status=status, mode="analyze", domain="legal", question=question,
                   dossier=d)


def test_a_legal_analysis_without_evidence_carries_the_insufficiency_summary():
    view = _empty_legal()
    assert needs_insufficiency(view)
    rec = build_delivery(view, text="page", page=1, pages=1)
    text = rec.insufficiency
    assert text.startswith("I could not answer this from your documents")
    assert "not giving legal conclusions" in text and "6 search(es) found 4 candidate document(s)" in text
    assert "- Reissuing a work permit" in text and "- Notifying the authority" in text
    assert "upload" not in text.lower()
    assert "No provision with verified evidence" not in text  # the per-issue gaps are the list above


def test_the_summary_is_in_the_questions_language():
    vi = insufficiency_summary(_empty_legal(question="Có phải cấp lại giấy phép lao động không?"))
    assert vi.startswith("Tôi không thể trả lời câu hỏi này dựa trên tài liệu của bạn")
    assert "Bước tiếp theo:" in vi


@pytest.mark.parametrize("view, expected", [
    (_empty_legal(status=RUNNING), False),
    (_empty_legal(status=COMPLETE), True),  # an older job saved "complete" with nothing
    (_empty_legal(status=FAILED, reason="model_failed"), True),
])
def test_which_jobs_need_the_summary(view, expected):
    assert needs_insufficiency(view) is expected


def test_general_or_evidenced_jobs_never_carry_it(index):
    db, files = index
    assert not needs_insufficiency(_view(PARTIAL, _dossier(files, outcome=_outcome())))
    general = _empty_legal()
    general.domain = "general"
    assert not needs_insufficiency(general)


def test_no_candidates_is_the_one_case_that_suggests_adding_a_document():
    view = _empty_legal(reason="no_candidates")
    assert "add it to your documents folder" in insufficiency_summary(view)
    for reason in ("candidates_rejected", "no_verified_findings", "candidates_unreadable"):
        assert "add it" not in insufficiency_summary(_empty_legal(reason=reason))


def test_evidence_tokens_are_the_findings_own(index):
    _db, files = index
    d = _dossier(files, outcome=_outcome())
    tokens = [e.token for i in d.issues for f in i.findings for e in f.evidence]
    rec = build_delivery(_view(COMPLETE, d), text="", page=1, pages=1)
    assert list(rec.evidence) == tokens
    # Facts from the case files are not evidence for the answer.
    assert not set(rec.evidence) & {e.token for f in d.facts for e in f.evidence}
    assert isinstance(Evidence, type)
