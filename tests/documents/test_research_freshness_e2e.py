"""Research reads the index, and brings the index up to date first — with
the real sync engine (extractor subprocesses, scans, the in-memory vector
store) under a real compile job; only the model is scripted.

Polling mode is where the index trails the folder the most: a change waits
for the next scan, 30 seconds to 15 minutes away. Each test changes the
folder right after a scan, so the index is out of date when the job starts,
and checks what the job read:

- a file edited and a file added are indexed before the compile reads, so
  the table has the new figure and the model was never shown the old one;
  a deleted file drops out of the scope;
- with sync paused, nothing old is read either: the edited file is listed
  as not indexed yet and the job asks first. Once sync resumes, the same
  job reads the new text.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from .test_engine_e2e import _chunk_texts, _idle, _wait, env  # noqa: F401 — env is the fixture

from app.documents.research import compile as C  # noqa: E402
from app.documents.research.context import NeedsInput, ProgressSink, ResearchContext, ResearchLLM, ResearchSpec  # noqa: E402
from app.documents.research.types import COMPLETE, DOMAIN_FINANCIAL, MODE_COMPILE, Dossier  # noqa: E402

from .test_research_compile import QUESTION, FakeModel, table_rows  # noqa: E402

Q1_OLD = "# Q1 2025 results\n\nQ1 2025 revenue: 1,200\nQ1 2025 cost: 700\n"
Q1_NEW = "# Q1 2025 results\n\nQ1 2025 revenue: 13,500\nQ1 2025 cost: 700\n"


def _start_polling(env) -> object:
    env.storage.upsert_source("alice", "local", enabled=True, root_mode="custom",
                              root_path=os.path.realpath(env.alice), first_sync_confirmed_at=1.0,
                              options={"observer": {"mode": "poll"}})
    env.svc.start()
    _wait(lambda: env.svc.runtime("alice") is not None and env.svc.runtime("alice").active)
    rt = _idle(env, "alice")
    assert rt.watch_mode == "poll"
    # The next scan is at least 30 s away: what the tests change now is
    # invisible to the index until someone asks.
    assert rt.next_scan_at - time.time() > 20
    return rt


def _ctx(env, fake: FakeModel, tmp: Path) -> ResearchContext:
    from app.documents.query import open_engine

    access = open_engine("alice")
    assert access.engine is not None and access.engine.runtime is not None, access
    return ResearchContext(
        profile="alice", job_id="job1",
        spec=ResearchSpec(question=QUESTION, mode=MODE_COMPILE, domain=DOMAIN_FINANCIAL,
                          scope={"folder": ["MKT-report"]}),
        engine=access.engine, llm=ResearchLLM(fake, budget=250_000),
        dossier=Dossier(job_id="job1", mode=MODE_COMPILE, domain=DOMAIN_FINANCIAL, question=QUESTION,
                        status="running"),
        progress_sink=ProgressSink(), artifacts_dir=str(tmp / "artifacts"),
    )


def _shown(fake: FakeModel) -> str:
    return "\n".join(prompt or "" for _, prompt in fake.calls)


def test_compile_reads_what_the_folder_says_now_not_the_last_scan(env, tmp_path):
    mkt = env.alice / "MKT-report"
    mkt.mkdir()
    (mkt / "q1.md").write_text(Q1_OLD, encoding="utf-8")
    (mkt / "old.md").write_text("# Q4 2024 results\n\nQ4 2024 revenue: 999\n", encoding="utf-8")
    rt = _start_polling(env)

    (mkt / "q1.md").write_text(Q1_NEW, encoding="utf-8")
    (mkt / "q2.md").write_text("# Q2 2025 results\n\nQ2 2025 revenue: 1,500\n", encoding="utf-8")
    (mkt / "old.md").unlink()
    assert "13,500" not in _chunk_texts(rt), "the index is still at the last scan"

    fake = FakeModel()
    ctx = _ctx(env, fake, tmp_path)
    d = asyncio.run(C.run_compile(ctx))

    assert d.status == COMPLETE, (d.notes, d.gaps)
    values = {v for row in table_rows(d.compiled) for v in row}
    assert "13,500" in values and "1,500" in values
    assert "1,200" not in values and "999" not in values
    shown = _shown(fake)
    assert "13,500" in shown and "1,200" not in shown, "the old text was never shown to the model"
    assert {r.rel_path for r in d.coverage} == {"MKT-report/q1.md", "MKT-report/q2.md"}
    labels = [s["label"] for s in ctx.progress_sink.steps]
    assert "Checking the folder for new and changed files" in labels
    assert not any("last indexed" in n or "folder check" in n or "not checked" in n
                   for n in d.notes), d.notes


def test_with_sync_paused_the_old_text_is_not_read(env, tmp_path):
    mkt = env.alice / "MKT-report"
    mkt.mkdir()
    (mkt / "q1.md").write_text(Q1_OLD, encoding="utf-8")
    (mkt / "q2.md").write_text("# Q2 2025 results\n\nQ2 2025 revenue: 1,500\n", encoding="utf-8")
    rt = _start_polling(env)
    env.svc.control("alice", "pause")

    (mkt / "q1.md").write_text(Q1_NEW, encoding="utf-8")
    fake = FakeModel()
    ctx = _ctx(env, fake, tmp_path)
    with pytest.raises(NeedsInput) as asked:
        asyncio.run(C.run_compile(ctx))

    clar = asked.value.clarification
    assert [(c["rel_path"], c["reason"]) for c in clar.candidates] == [("MKT-report/q1.md", "not_indexed_yet")]
    assert fake.calls == [], "nothing is read before the user answers"
    assert any("sync is paused" in n for n in ctx.dossier.notes), ctx.dossier.notes

    # Sync resumes: the same job, run again, reads the new text.
    env.svc.control("alice", "resume")
    _idle(env, "alice")
    assert "13,500" in _chunk_texts(rt)
    fake = FakeModel()
    ctx = _ctx(env, fake, tmp_path)
    d = asyncio.run(C.run_compile(ctx))
    assert d.status == COMPLETE
    values = {v for row in table_rows(d.compiled) for v in row}
    assert "13,500" in values and "1,200" not in values
