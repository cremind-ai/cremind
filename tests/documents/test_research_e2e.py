"""Research end to end: the agent's tool leaf → the job runner → the real
compile pipeline over a real index → the real renderer → citations and
artifacts — with only the model scripted.

Each builder's own tests fake its neighbours; this one runs them together,
the way a chat turn does:

1. the agent asks to compile a folder that holds an unreadable file: the
   job stops and asks, and the tool shows the question (and claims it, so
   no extra turn is injected);
2. the agent continues with the user's answer: the job runs to completion
   inside the tool's long-poll; the answer carries the table, verified
   tokens registered for the conversation, and the CSV/Markdown files, which
   live in the profile's private exports folder;
3. dossier pages are readable as ``research:<id>``;
4. another profile cannot see the job.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from app.tools.builtin import documentation_search as tool  # noqa: E402
from app.documents.query.engine import QueryEngine  # noqa: E402
from app.documents.research import jobs  # noqa: E402
from app.documents.research.types import COMPLETE, NEEDS_CONFIRMATION  # noqa: E402

from .test_query_engine import UTC, Index  # noqa: E402
from .test_research_compile import QUESTION, FakeModel, build_mkt  # noqa: E402
from .test_research_jobs import env, settle  # noqa: E402,F401

# The real collaborators, captured before the jobs fixture replaces them.
REAL_PIPELINE_FOR = jobs._pipeline_for
REAL_RENDER = jobs._render
REAL_ACTIVITY = jobs._activity_module


@pytest.fixture
def world(env, tmp_path, monkeypatch):  # noqa: F811
    (tmp_path / "idx").mkdir()
    ix = build_mkt(Index(tmp_path / "idx"))
    root = tmp_path / "userroot"
    (root / "MKT-report").mkdir(parents=True)
    fake = FakeModel()
    monkeypatch.setattr(jobs, "_pipeline_for", REAL_PIPELINE_FOR)
    monkeypatch.setattr(jobs, "_render", REAL_RENDER)
    monkeypatch.setattr(jobs, "_activity_module", REAL_ACTIVITY)
    monkeypatch.setattr(jobs, "_create_llm", lambda group, profile: fake)
    def engine(profile):
        return QueryEngine(profile, ix.db, tz=UTC, root=str(root), vector_handles=lambda: None)

    import app.documents.query as query_pkg

    monkeypatch.setattr(jobs, "_open_engine", engine)
    # The tool resolves printed tokens against the index through the query
    # package (it has no running Documentation search service here).
    monkeypatch.setattr(query_pkg, "open_engine", lambda profile: SimpleNamespace(engine=engine(profile)))
    monkeypatch.setattr(jobs, "wait_cap", lambda: 20.0)
    env.ix, env.fake, env.root = ix, fake, root
    yield env
    ix.db.close()


def _call(leaf_cls, profile="alice", context="c-alice", **args):
    async def go():
        out = await leaf_cls().run({**args, "_profile": profile, "_context_id": context})
        await settle()
        return out
    return asyncio.run(go())


def _text(result) -> str:
    sc = result.structured_content or {}
    if sc.get("text"):
        return sc["text"]
    return "\n".join(c.get("text", "") for c in (result.content or []))


def test_compile_a_folder_through_the_agents_tool(world):
    # 1. Start: the encrypted PDF stops the job before anything is read.
    first = _call(tool.DocumentsResearchTool, question=QUESTION, mode="compile", domain="financial",
                  scope={"folder": ["MKT-report"]})
    text = _text(first)
    [row] = world.store.list("alice", limit=5)
    job_id = row["id"]
    assert row["status"] == NEEDS_CONFIRMATION and row["conversation_id"] == "c-alice"
    assert "secret.pdf" in text and "confirm" in text and job_id in text
    assert world.fake.calls == [], "nothing is read before the user answers"
    assert world.enqueued == [], "the tool showed the question: no extra turn"

    # 2. Continue with the answer: the job completes inside the long-poll.
    second = _call(tool.DocumentsResearchTool, continue_job=job_id, answers={"confirm": True})
    text = _text(second)
    view = jobs.get_job("alice", job_id)
    assert view.status == COMPLETE, text
    assert "Q2 2025" in text and "1,500" in text and "1,450" in text  # the conflict, both sides
    assert world.enqueued == [], "collected by the tool: nothing injected"

    # Every printed passage token was registered for the conversation, and
    # resolves to a real passage of a file in the folder.
    from app.storage.documents_citations_storage import get_documents_citations_storage

    printed = set(__import__("re").findall(r"\[doc:[0-9a-z]{8}#[0-9a-f]{8}\]", text))
    assert printed
    rows = get_documents_citations_storage().rows_for_cite_ids("alice", {t[5:13] for t in printed})
    issued = {r["token"] for r in rows if r.get("conversation_id") == "c-alice"}
    assert printed <= issued
    folder_ids = {world.ix.q1["cite_id"], world.ix.q2["cite_id"], world.ix.q3["cite_id"],
                  world.ix.summary["cite_id"]}
    assert {t[5:13] for t in printed} <= folder_ids

    # The table files ride along, in alice's private exports folder, and the
    # user's own folder is untouched.
    files = (second.structured_content or {}).get("_files") or []
    assert {f["name"] for f in files} == {f"compiled-{job_id}.csv", f"compiled-{job_id}.md"}
    exports = Path(world.sysdir) / "alice" / "exports" / "research" / job_id
    for f in files:
        assert Path(f["uri"]).parent == exports and Path(f["uri"]).is_file()
    assert list(world.root.rglob("*")) == [world.root / "MKT-report"]

    # Usage went to the usage table directly, tagged as document research.
    assert world.usage.calls and all(
        r["source_kind"] == "documents" and r["tool_id"] == "documentation_search"
        for c in world.usage.calls for r in c["records"])

    # 3. Later pages of the dossier, through read.
    page2 = _call(tool.DocumentsReadTool, file=f"research:{job_id}", page=2)
    assert "research" in _text(page2).lower()

    # 4. Bob cannot see alice's job.
    stolen = _call(tool.DocumentsResearchTool, profile="bob", context="c-bob", continue_job=job_id)
    assert (stolen.structured_content or {}).get("error") == "JobNotFound"
