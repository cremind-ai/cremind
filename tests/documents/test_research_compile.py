"""Compile mode end to end, over a real index and a scripted model.

The index is written directly, with the real chunker and cards (as in
test_query_engine). The model is a fake that answers each function call by
reading its prompt, the way a careful model would, plus scripted misbehaviour.

What must hold, in the user's words ("compile the business results in the
MKT-report folder"):

- every file in the folder is listed; an unreadable one is named with its
  reason, and the job asks before compiling without it;
- the table has one row per quarter and metric, each value citing its
  passage;
- two files disagreeing on Q2 revenue is shown as a conflict with both
  sources, never silently resolved;
- a quote the model made up, or a passage it was not shown, is rejected and
  counted;
- the CSV/Markdown files land in the job's folder, never the user's;
- a job stopped halfway resumes without paying for what it already read;
- a job that cannot afford to read everything asks first, and one that runs
  out on the way says which files it did not read;
- two profiles share nothing.
"""

from __future__ import annotations

import asyncio
import copy
import csv
import json
import re
from pathlib import Path

import pytest

from app.constants import ChatCompletionTypeEnum
from app.documents.cite import make_token
from app.documents.query.engine import QueryEngine
from app.documents.research import artifacts
from app.documents.research import compile as C
from app.documents.research.context import NeedsInput, ProgressSink, ResearchContext, ResearchLLM, ResearchSpec, TimeUp
from app.documents.research.types import (
    COMPLETE,
    DOMAIN_FINANCIAL,
    MODE_COMPILE,
    NEEDS_CLARIFICATION,
    NEEDS_CONFIRMATION,
    PARTIAL,
    READ_FULL,
    READ_NONE,
    Cell,
    CompiledTable,
    Dossier,
    dossier_from_dict,
)
from app.documents.types import Block

from .test_query_engine import UTC, Index

QUESTION = "Compile the business results (revenue and cost per quarter) in the MKT-report folder"
TOKEN_RE = re.compile(r"(\[doc:[0-9a-z]{8}#[0-9a-f]{8}\])[^\n]*\n(.*?)(?=\n\[doc:|\n<<<|\Z)", re.S)
FIGURE_RE = re.compile(r"(Q[1-4] \d{4})\W+(revenue|cost)\W+([\d.,]*\d)")
PLAN = {
    "title": "Business results",
    "columns": [
        {"name": "period", "description": "The quarter, written as 'Q1 2025'."},
        {"name": "metric", "description": "revenue or cost"},
        {"name": "value", "description": "The amount, as written."},
    ],
    "row_key": ["period", "metric"],
}
EXPECTED = [
    ("Q1 2025", "cost", "700"), ("Q1 2025", "revenue", "1,200"),
    ("Q2 2025", "cost", "900"), ("Q2 2025", "revenue", "1,500"),
    ("Q3 2025", "cost", "1,000"), ("Q3 2025", "revenue", "1,800"),
]


class FakeModel:
    """Answers plan_table with :data:`PLAN`; record_rows with every
    "Qn yyyy revenue/cost: value" figure in the passages it was shown, each
    quoting its own line; summarize_table with one finding over the revenue
    rows it was shown.

    Scripted misbehaviour:
    - on q3.csv it also invents a Q4 row with a made-up quote;
    - with ``foreign`` set, it cites a passage of another file;
    - with ``outside`` set, the summary cites a passage from outside the table.
    """

    provider_name, model_name = "fake", "fake-1"

    def __init__(self, *, usage=(400, 80), fail_after=None, delay=0.0, foreign=None, outside=None, mute=(),
                 broken=()):
        self.usage = usage
        self.fail_after = fail_after
        self.delay = delay
        self.foreign = foreign
        self.outside = outside
        # record_rows prompts naming one of these files get a text answer,
        # not a function call; ones naming a ``broken`` file fail outright.
        self.mute = mute
        self.broken = broken
        self.calls: list[tuple[str, str]] = []
        self.systems: dict[str, str] = {}
        self.inflight = self.peak = 0

    def names(self):
        return [n for n, _ in self.calls]

    def sent(self, name="record_rows"):
        return [u for n, u in self.calls if n == name]

    async def chat_completion(self, *, messages, tools, **kw):
        name = tools[0]["function"]["name"]
        user = messages[-1]["content"]
        self.calls.append((name, user))
        self.systems[name] = messages[0]["content"]
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise TimeUp()
        if name == "record_rows" and any(f"File: MKT-report/{b}" in user for b in self.broken):
            raise RuntimeError("provider 429: rate limited")
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.inflight -= 1
        if name == "record_rows" and any(f"File: MKT-report/{m}" in user for m in self.mute):
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": "I could not find anything relevant."}
        else:
            args = getattr(self, name)(user, tools[0])
            yield {"type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                   "data": {"function": [{"name": name, "arguments": json.dumps(args)}]}}
        yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": self.usage[0], "output_tokens": self.usage[1]}

    def plan_table(self, user, tool):
        return PLAN

    def record_rows(self, user, tool):
        props = tool["function"]["parameters"]["properties"]["rows"]["items"]["properties"]["cells"]["properties"]
        ids = {spec["description"].split(":")[0].replace(" (identifies the row)", ""): cid
               for cid, spec in props.items()}

        def row(period, metric, value, token, quote):
            return {"cells": {ids["period"]: period, ids["metric"]: metric, ids["value"]: value},
                    "evidence": [{"token": token, "quote": quote}]}

        passages = TOKEN_RE.findall(user)
        rows = [row(*m.groups(), token, m.group(0)) for token, text in passages for m in FIGURE_RE.finditer(text)]
        if "File: MKT-report/q3.csv" in user:
            rows.append(row("Q4 2025", "revenue", "9,999", passages[0][0], "Q4 2025 revenue: 9,999"))
            if self.foreign:
                rows.append(row("Q1 2025", "cost", "650", *self.foreign))
        return {"rows": rows}

    def summarize_table(self, user, tool):
        shown = re.findall(r'source (\[doc:[0-9a-z]{8}#[0-9a-f]{8}\]) "([^"]+)"', user)
        revenue = [{"token": t, "quote": q} for t, q in shown if "revenue" in q]
        findings = [{"text": "Revenue grew every quarter of 2025.", "evidence": revenue[:3]}]
        if self.outside:
            findings.append({"text": "Other notes put Q1 revenue at 5,000.",
                             "evidence": [{"token": self.outside[0], "quote": self.outside[1]}]})
        return {"findings": findings}


# ── the corpus ──────────────────────────────────────────────────────────────


def _md(heading: str, body: str) -> list[Block]:
    return [Block(text=heading, role="heading", level=1, locator={"line_start": 1, "line_end": 1}),
            Block(text=body, locator={"line_start": 3, "line_end": 3 + body.count("\n")})]


def build_mkt(ix: Index, *, prefix: str = "MKT-report") -> Index:
    ix.q1 = ix.add(f"{prefix}/q1.md", _md("Q1 2025 results", "Q1 2025 revenue: 1,200\nQ1 2025 cost: 700"),
                   kind="markdown", sha256="sha-q1")
    ix.q2 = ix.add(f"{prefix}/q2.docx", _md("Second quarter", "Q2 2025 revenue: 1,500. Q2 2025 cost: 900."),
                   kind="docx", sha256="sha-q2")
    ix.q3 = ix.add(f"{prefix}/q3.csv", [Block(
        text="| period | metric | value |\n|---|---|---|\n| Q3 2025 | revenue | 1,800 |\n| Q3 2025 | cost | 1,000 |",
        locator={"sheet": "q3", "range": "A1:C3"}, anchor=2)], kind="csv", sha256="sha-q3")
    ix.summary = ix.add(f"{prefix}/summary.md", _md("Year so far", "Q1 2025 revenue: 1,200\nQ2 2025 revenue: 1,450"),
                        kind="markdown", sha256="sha-sum")
    ix.secret = ix.add(f"{prefix}/secret.pdf", [], kind="pdf", status="metadata_only", status_reason="encrypted",
                       sha256="sha-secret")
    return ix


def body_token(ix: Index, row: dict) -> tuple[str, str]:
    chunk = next(c for c in ix.db.chunks_of_file(row["id"]) if c["ctype"] == "body")
    return make_token(row["cite_id"], chunk["text_hash"]), chunk["text"]


@pytest.fixture
def corpus(tmp_path):
    (tmp_path / "idx").mkdir()
    ix = build_mkt(Index(tmp_path / "idx"))
    ix.notes = ix.add("Other/notes.md", _md("Notes", "Q1 2025 revenue: 5,000"), kind="markdown", sha256="sha-n")
    # The user's real folder: research must never write into it.
    ix.root = tmp_path / "userroot"
    (ix.root / "MKT-report").mkdir(parents=True)
    (ix.root / "MKT-report" / "q1.md").write_text("# Q1 2025 results\n\nQ1 2025 revenue: 1,200\n", encoding="utf-8")
    ix.exports = tmp_path / "system" / "alice" / "exports" / "research" / "job1"
    yield ix
    ix.db.close()


def make_ctx(ix: Index, fake: FakeModel, *, scope=None, budget=250_000, answers=None, state=None, dossier=None,
             saver=None, artifacts_dir=None, job_id="job1", profile="alice", question=QUESTION) -> ResearchContext:
    root = getattr(ix, "root", None)
    engine = QueryEngine(profile, ix.db, tz=UTC, root=str(root) if root else None, vector_handles=lambda: None)
    spec = ResearchSpec(question=question, mode=MODE_COMPILE, domain=DOMAIN_FINANCIAL,
                        scope=scope if scope is not None else {"folder": ["MKT-report"]})
    return ResearchContext(
        profile=profile, job_id=job_id, spec=spec, engine=engine, llm=ResearchLLM(fake, budget=budget),
        dossier=dossier or Dossier(job_id=job_id, mode=MODE_COMPILE, domain=DOMAIN_FINANCIAL, question=question,
                                   status="running"),
        state=state if state is not None else {}, answers=dict(answers or {}), progress_sink=ProgressSink(),
        saver=saver, artifacts_dir=str(artifacts_dir) if artifacts_dir else None,
    )


def run(ctx: ResearchContext) -> Dossier:
    return asyncio.run(C.run_compile(ctx))


def table_rows(table: CompiledTable) -> list[tuple[str, ...]]:
    return [tuple(r[c].value if c in r else "" for c in table.columns) for r in table.rows]


def coverage_of(d: Dossier) -> dict[str, tuple[str, str | None]]:
    return {r.rel_path: (r.read, r.reason) for r in d.coverage}


def snapshot(root: Path) -> list[tuple[str, int]]:
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*"))


# ── golden query 3: compile the MKT-report folder ───────────────────────────


def test_compile_the_mkt_report_folder(corpus):
    ix = corpus
    fake = FakeModel(foreign=(body_token(ix, ix.q1)[0], "Q1 2025 cost: 700"),
                     outside=(body_token(ix, ix.notes)[0], "Q1 2025 revenue: 5,000"))
    before = snapshot(ix.root)
    ctx = make_ctx(ix, fake, artifacts_dir=ix.exports)

    # 1. The encrypted PDF cannot be read: ask before compiling without it.
    with pytest.raises(NeedsInput) as asked:
        run(ctx)
    clar = asked.value.clarification
    assert asked.value.status == NEEDS_CONFIRMATION and clar.kind == "unread"
    assert [(c["rel_path"], c["reason"]) for c in clar.candidates] == [("MKT-report/secret.pdf", "encrypted")]
    assert "confirm" in clar.answer_keys
    assert fake.calls == [], "nothing is read before the user answers"
    assert set(coverage_of(ctx.dossier)) == {"MKT-report/q1.md", "MKT-report/q2.docx", "MKT-report/q3.csv",
                                             "MKT-report/summary.md", "MKT-report/secret.pdf"}

    # 2. The user says go on.
    ctx.answers = {"confirm": True}
    d = run(ctx)
    assert d.status == COMPLETE and d.clarification is None

    # Coverage: every file in the folder, the unreadable one with its reason;
    # nothing from outside the folder.
    assert coverage_of(d) == {
        "MKT-report/q1.md": (READ_FULL, None), "MKT-report/q2.docx": (READ_FULL, None),
        "MKT-report/q3.csv": (READ_FULL, None), "MKT-report/summary.md": (READ_FULL, None),
        "MKT-report/secret.pdf": (READ_NONE, "encrypted"),
    }
    assert any("secret.pdf" in g and "encrypted" in g for g in d.gaps)

    # One row per quarter and metric, in time order; the invented Q4 row and
    # the row citing another file's passage are gone.
    table = d.compiled
    assert table.columns == ["period", "metric", "value"]
    assert table_rows(table) == EXPECTED
    tok = {name: body_token(ix, getattr(ix, name))[0] for name in ("q1", "q2", "q3", "summary")}
    for r in table.rows:
        assert all(cell.tokens for cell in r.values()), "every value cites its passage"
    q1_revenue = next(r for r in table.rows if r["period"].value == "Q1 2025" and r["metric"].value == "revenue")
    assert set(q1_revenue["value"].tokens) == {tok["q1"], tok["summary"]}, "the same figure in two files cites both"

    # Q2 revenue: 1,500 in q2.docx, 1,450 in summary.md — kept side by side.
    assert len(table.conflicts) == 1
    conflict = table.conflicts[0]
    assert conflict.key == "Q2 2025 · revenue — value"
    assert [(c.value, c.tokens) for c in conflict.values] == [("1,500", [tok["q2"]]), ("1,450", [tok["summary"]])]
    assert any("differ between sources" in n for n in d.notes)

    # Rejected: the made-up Q4 quote, the other file's passage, and the
    # summary's citation of a passage outside the table.
    assert d.rejected_quotes == 3

    # The summary cites only passages in the table.
    assert len(d.facts) == 1 and d.facts[0].stance == "fact"
    table_tokens = {t for r in table.rows for c in r.values() for t in c.tokens}
    assert {e.token for e in d.facts[0].evidence} <= table_tokens and d.facts[0].evidence

    # Artifacts: in the job's folder only; the user's folder is untouched.
    assert [a["name"] for a in table.artifacts] == ["compiled-job1.csv", "compiled-job1.md"]
    for a in table.artifacts:
        assert a["origin"] == "created" and "\\" not in a["uri"]
        assert Path(a["uri"]).parent == ix.exports and Path(a["uri"]).is_file()
    assert snapshot(ix.root) == before
    with open(ix.exports / "compiled-job1.csv", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["period", "metric", "value", "sources"]
    assert rows[4][:3] == ["Q2 2025", "revenue", "1,500"] and tok["q2"] in rows[4][3] and "q2.docx" in rows[4][3]
    md = (ix.exports / "compiled-job1.md").read_text(encoding="utf-8")
    assert "## Conflicts" in md and "1,450" in md and "summary.md" in md

    # Progress named each file with its position.
    labels = [s["label"] for s in ctx.progress_sink.steps]
    assert "Reading MKT-report/q3.csv (3/4)" in labels
    assert (ctx.progress_sink.done, ctx.progress_sink.total) == (4, 4)

    # The dossier is the checkpoint: it survives a JSON round trip.
    assert dossier_from_dict(json.loads(json.dumps(d.to_dict()))) == d


def test_map_prompts_carry_the_window_as_untrusted_data(corpus):
    fake = FakeModel()
    run(make_ctx(corpus, fake, answers={"confirm": True}))
    prompts = fake.sent()
    assert len(prompts) == 4
    for p in prompts:
        assert "<<<" in p and "c1 · period (identifies the row)" in p
    q3 = next(p for p in prompts if "q3.csv" in p)
    # The q3 window shows only q3's passage.
    assert set(re.findall(r"\[doc:([0-9a-z]{8})#", q3)) == {corpus.q3["cite_id"]}


# ── resuming ────────────────────────────────────────────────────────────────


def test_a_stop_mid_map_resumes_without_reading_anything_twice(corpus, monkeypatch):
    monkeypatch.setattr(C, "MAP_CONCURRENCY", 1)
    saved: list[dict] = []

    async def saver(ctx):
        saved.append(json.loads(json.dumps({"state": ctx.state, "dossier": ctx.dossier.to_dict()})))

    # plan, q1, q2 — then the fourth call (q3) hits the time limit.
    fake = FakeModel(fail_after=3)
    ctx = make_ctx(corpus, fake, answers={"confirm": True}, saver=saver)
    with pytest.raises(TimeUp):
        run(ctx)
    last = saved[-1]
    files = last["state"]["compile"]["files"]
    done = {cite for cite, fs in files.items() if len(fs["done"]) == fs["windows"]}
    assert done == {corpus.q1["cite_id"], corpus.q2["cite_id"]}
    cov = {r["rel_path"]: (r["read"], r["reason"]) for r in last["dossier"]["coverage"]}
    assert cov["MKT-report/q1.md"] == (READ_FULL, None)
    assert cov["MKT-report/q3.csv"] == (READ_NONE, "time")
    partial = dossier_from_dict(last["dossier"])
    assert table_rows(partial.compiled) == [r for r in EXPECTED if r[0] in ("Q1 2025", "Q2 2025")]

    # Resume from the checkpoint: no new plan, and q1/q2 are not sent again.
    fake2 = FakeModel()
    ctx2 = make_ctx(corpus, fake2, answers={"confirm": True}, state=copy.deepcopy(last["state"]),
                    dossier=dossier_from_dict(last["dossier"]))
    d = run(ctx2)
    assert d.status == COMPLETE
    assert "plan_table" not in fake2.names()
    sent = fake2.sent()
    assert len(sent) == 2 and all("q1.md" not in p and "q2.docx" not in p for p in sent)
    assert table_rows(d.compiled) == EXPECTED
    assert all(r.read == READ_FULL for r in d.coverage if r.reason != "encrypted")


def test_a_window_the_model_did_not_answer_is_marked_and_retried_on_resume(corpus):
    fake = FakeModel(mute=("q2.docx",))
    ctx = make_ctx(corpus, fake, answers={"confirm": True})
    d = run(ctx)
    assert d.status == COMPLETE
    assert len([p for p in fake.sent() if "q2.docx" in p]) == 2, "one retry, then give up"
    assert coverage_of(d)["MKT-report/q2.docx"] == (READ_NONE, "error")
    assert any("no usable answer for 1 part(s) of 1 file(s)" in n for n in d.notes)
    assert any("q2.docx" in g for g in d.gaps)
    assert ("Q2 2025", "cost", "900") not in table_rows(d.compiled)

    # Nothing was cached for it, so a resumed job asks again, and only for it.
    again = FakeModel()
    d2 = run(make_ctx(corpus, again, answers={"confirm": True}, state=copy.deepcopy(ctx.state)))
    assert [p for p in again.sent()] and all("q2.docx" in p for p in again.sent())
    assert table_rows(d2.compiled) == EXPECTED


def test_a_flaky_provider_fails_one_window_not_the_job(corpus, monkeypatch):
    monkeypatch.setattr(C, "RETRY_DELAY_S", 0)
    fake = FakeModel(broken=("q2.docx",))
    d = run(make_ctx(corpus, fake, answers={"confirm": True}))
    assert d.status == COMPLETE
    assert coverage_of(d)["MKT-report/q2.docx"] == (READ_NONE, "error")
    assert coverage_of(d)["MKT-report/q3.csv"] == (READ_FULL, None)


def test_a_provider_that_is_down_stops_the_job_with_what_was_read_saved(corpus, monkeypatch):
    monkeypatch.setattr(C, "RETRY_DELAY_S", 0)
    monkeypatch.setattr(C, "MAP_CONCURRENCY", 1)
    saved: list[dict] = []

    async def saver(ctx):
        saved.append(ctx.dossier.to_dict())

    fake = FakeModel(broken=("q2.docx", "q3.csv", "summary.md"))
    with pytest.raises(RuntimeError, match="429"):
        run(make_ctx(corpus, fake, answers={"confirm": True}, saver=saver))
    last = dossier_from_dict(saved[-1])
    assert coverage_of(last)["MKT-report/q1.md"] == (READ_FULL, None)
    assert ("Q1 2025", "revenue", "1,200") in table_rows(last.compiled)


def test_a_vietnamese_question_gets_a_vietnamese_plan_and_summary(corpus):
    fake = FakeModel()
    ctx = make_ctx(corpus, fake, answers={"confirm": True},
                   question="Tổng hợp kết quả kinh doanh theo quý trong thư mục MKT-report")
    run(ctx)
    assert ctx.state["compile"]["language"] == "vi"
    assert "in Vietnamese" in fake.systems["plan_table"]
    assert "in Vietnamese" in fake.systems["summarize_table"]


def test_above_sixty_files_the_summary_goes_per_folder_then_overall(tmp_path):
    (tmp_path / "idx").mkdir()
    ix = Index(tmp_path / "idx")
    try:
        for i in range(62):
            folder = "A" if i < 31 else "B"
            ix.add(f"Big/{folder}/f{i:02d}.md", _md(f"File {i}", f"Q{i % 4 + 1} 2025 revenue: {i + 1},000"),
                   kind="markdown")
        fake = FakeModel()
        d = run(make_ctx(ix, fake, scope={"folder": ["Big"]}))
        assert d.status == COMPLETE
        summaries = fake.sent("summarize_table")
        assert len(summaries) == 3
        assert "folder 'A'" in summaries[0] and "folder 'B'" in summaries[1]
        assert "Findings per folder" in summaries[2]
        # The overall findings cite only what the per-folder findings cited,
        # which cite only rows of the table.
        assert d.facts and all(f.issue == "" for f in d.facts)
        table_tokens = {t for r in d.compiled.rows for c in r.values() for t in c.tokens} | {
            t for c in d.compiled.conflicts for v in c.values for t in v.tokens}
        assert {e.token for f in d.facts for e in f.evidence} <= table_tokens
    finally:
        ix.db.close()


def test_a_second_job_over_the_same_files_reads_from_the_cache(corpus):
    first = FakeModel()
    d1 = run(make_ctx(corpus, first, answers={"confirm": True}))
    cached = corpus.db.read_sql("SELECT purpose, file_id FROM llm_cache")
    assert {r["purpose"] for r in cached} == {"map_extract"} and len(cached) == 4

    second = FakeModel()
    d2 = run(make_ctx(corpus, second, answers={"confirm": True}, job_id="job2"))
    assert second.names() == ["plan_table", "summarize_table"], "every window came from the cache"
    assert table_rows(d2.compiled) == table_rows(d1.compiled)
    # Cached answers are checked again, not trusted: the same rejections.
    assert d2.rejected_quotes == d1.rejected_quotes == 1


# ── budget ──────────────────────────────────────────────────────────────────


def test_a_budget_too_small_for_the_folder_asks_first(corpus):
    fake = FakeModel()
    ctx = make_ctx(corpus, fake, budget=1000, answers={"confirm": True})
    with pytest.raises(NeedsInput) as asked:
        run(ctx)
    assert asked.value.status == NEEDS_CONFIRMATION
    clar = asked.value.clarification
    assert clar.kind == "budget" and "confirm_budget" in clar.answer_keys
    assert "1,000-token budget" in clar.question
    assert fake.calls == []


def test_the_soft_limit_stops_reading_and_says_what_was_not_read(corpus):
    # Each call costs 9,000 tokens of a 20,000 budget: the plan and one file
    # fit, then the job stops starting new work.
    fake = FakeModel(usage=(9000, 0))
    ctx = make_ctx(corpus, fake, budget=20_000, answers={"confirm": True, "confirm_budget": True},
                   artifacts_dir=corpus.exports)
    d = run(ctx)
    assert d.status == PARTIAL
    assert fake.names() == ["plan_table", "record_rows"]
    assert coverage_of(d) == {
        "MKT-report/q1.md": (READ_FULL, None), "MKT-report/q2.docx": (READ_NONE, "budget"),
        "MKT-report/q3.csv": (READ_NONE, "budget"), "MKT-report/summary.md": (READ_NONE, "budget"),
        "MKT-report/secret.pdf": (READ_NONE, "encrypted"),
    }
    assert any("3 of 4 readable file(s) were not read" in n for n in d.notes)
    assert table_rows(d.compiled) == [r for r in EXPECTED if r[0] == "Q1 2025"]
    assert d.facts == []
    assert d.compiled.artifacts, "what was read is still written out"


# ── scope ───────────────────────────────────────────────────────────────────


def test_an_empty_scope_completes_with_a_note(corpus):
    fake = FakeModel()
    d = run(make_ctx(corpus, fake, scope={"folder": ["MKT-report"], "types": ["presentation"]}))
    assert d.status == COMPLETE and d.coverage == [] and fake.calls == []
    assert any("nothing to compile" in n for n in d.notes)


def test_declining_to_go_on_without_unreadable_files_reads_nothing(corpus):
    fake = FakeModel()
    d = run(make_ctx(corpus, fake, answers={"confirm": "false"}))
    assert d.status == PARTIAL and fake.calls == [] and d.compiled is None
    assert any("chose not to continue" in n for n in d.notes)


def test_an_ambiguous_folder_is_asked_about_then_used(tmp_path):
    (tmp_path / "idx").mkdir()
    ix = Index(tmp_path / "idx")
    try:
        build_mkt(ix, prefix="A/MKT-report")
        ix.add("B/MKT-report/other.md", _md("Other", "Q1 2025 revenue: 3"), kind="markdown")
        fake = FakeModel()
        ctx = make_ctx(ix, fake, answers={"confirm": True})
        with pytest.raises(NeedsInput) as asked:
            run(ctx)
        assert asked.value.status == NEEDS_CLARIFICATION and asked.value.clarification.kind == "scope"
        ctx.answers["scope_folder"] = "A/MKT-report"
        d = run(ctx)
        assert d.status == COMPLETE
        assert all(r.rel_path.startswith("A/MKT-report/") for r in d.coverage)
        assert table_rows(d.compiled) == EXPECTED
    finally:
        ix.db.close()


def test_artifacts_are_never_written_into_the_users_folder(corpus):
    before = snapshot(corpus.root)
    d = run(make_ctx(corpus, FakeModel(), answers={"confirm": True}, artifacts_dir=corpus.root / "exports"))
    assert d.status == COMPLETE and d.compiled.artifacts == []
    assert any("not written" in n for n in d.notes)
    assert snapshot(corpus.root) == before


# ── concurrency and isolation ───────────────────────────────────────────────


def test_windows_are_read_at_most_four_at_a_time(tmp_path):
    (tmp_path / "idx").mkdir()
    ix = Index(tmp_path / "idx")
    try:
        for i in range(10):
            ix.add(f"Many/f{i:02d}.md", _md(f"File {i}", f"Q{i % 4 + 1} 2025 revenue: {i + 1},000"),
                   kind="markdown")
        fake = FakeModel(delay=0.05)
        d = run(make_ctx(ix, fake, scope={"folder": ["Many"]}))
        assert d.status == COMPLETE and len(fake.sent()) == 10
        assert 1 < fake.peak <= C.MAP_CONCURRENCY
    finally:
        ix.db.close()


def test_two_profiles_share_no_files_and_no_cache(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    alice, bob = Index(tmp_path / "a"), Index(tmp_path / "b")
    try:
        build_mkt(alice)
        build_mkt(bob)
        bob.add("MKT-report/q4.md", _md("Q4", "Q4 2025 revenue: 2,000"), kind="markdown", sha256="sha-q4")
        da = run(make_ctx(alice, FakeModel(), answers={"confirm": True}, profile="alice"))
        fake_b = FakeModel()
        db_ = run(make_ctx(bob, fake_b, answers={"confirm": True}, profile="bob"))
        # Bob's identical files are read for Bob: Alice's cache is not his.
        assert len(fake_b.sent()) == 5
        assert "MKT-report/q4.md" not in coverage_of(da) and "MKT-report/q4.md" in coverage_of(db_)
        assert ("Q4 2025", "revenue", "2,000") not in table_rows(da.compiled)
        assert ("Q4 2025", "revenue", "2,000") in table_rows(db_.compiled)
    finally:
        alice.db.close()
        bob.db.close()


# ── units ───────────────────────────────────────────────────────────────────


def test_language_detection():
    assert C.detect_language("Tổng hợp kết quả kinh doanh trong thư mục MKT-report") == "vi"
    assert C.detect_language("tong hop ket qua kinh doanh") == "vi"
    assert C.detect_language(QUESTION) == "en"


def test_values_merge_across_formats_and_periods_sort_by_time():
    assert C.norm_value("1,200") == C.norm_value("1.200") == C.norm_value("1200")
    assert C.norm_value("1.2") != C.norm_value("12")
    plan = C.clean_plan(PLAN, "en")
    rows = [{"cells": {"period": p, "metric": "revenue", "value": v},
             "evidence": [{"token": f"[doc:aaaaaaaa#{i:08x}]", "quote": "q", "quote_status": "exact"}]}
            for i, (p, v) in enumerate([("Q1 2026", "5"), ("Quý II/2025", "2"), ("2025", "9"), ("Q1 2025", "1"),
                                        ("q1 2025", "1.0")])]
    table, _ = C.reduce_rows(plan, [({}, rows)])
    assert [r["period"].value for r in table.rows] == ["Q1 2025", "Quý II/2025", "2025", "Q1 2026"]
    # "1" and "1.0" are different values as written: a conflict, not a guess.
    assert [c.key for c in table.conflicts] == ["Q1 2025 · revenue — value"]


def test_a_plan_is_validated():
    plan = C.clean_plan({"columns": [{"name": "A", "description": "x"}, {"name": "a", "description": "dup"}] +
                         [{"name": f"c{i}", "description": ""} for i in range(20)], "row_key": ["nope"]}, "en")
    assert len(plan["columns"]) == C.MAX_COLUMNS and plan["row_key"] == ["A"]
    assert C.clean_plan({"columns": []}, "en") is None
    assert C.fallback_plan("vi")["columns"][0]["name"] == "hạng mục"


def test_the_csv_defuses_formulas_but_keeps_numbers():
    table = CompiledTable(columns=["item", "value"], rows=[
        {"item": Cell("=HYPERLINK(\"http://x\")", ["[doc:aaaaaaaa#00000001]"]), "value": Cell("-1,200")},
    ])
    out = artifacts.table_csv(table, {"aaaaaaaa": "Docs/a.csv"})
    row = list(csv.reader(out.splitlines()))[1]
    assert row == ["'=HYPERLINK(\"http://x\")", "-1,200", "[doc:aaaaaaaa#00000001] Docs/a.csv"]
