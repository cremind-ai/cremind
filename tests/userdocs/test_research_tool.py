"""The ``research`` leaf of the User Documents tool, over a faked job layer.

The job runner (:mod:`app.userdocs.research.jobs`) is replaced by a small
in-memory fake that honours its contract — jobs keyed by profile, a job id
from another profile is ``JobNotFound``, ``wait_job`` returns the current
view — so what is pinned here is the leaf itself:

- start / continue / cancel / page call the right job functions with the
  caller's profile, conversation, run id and settings, and wait only while
  the job is still working;
- ``mark_collected`` is called exactly when the agent is shown a state it can
  act on (final, or waiting for the user) — never for a PRELIMINARY one;
- ``read`` of ``research:<id>`` serves the dossier's pages without opening a
  file in the index;
- job errors come back as structured observations; a bad scope is caught
  before a job starts;
- the printed tokens are registered, the artifacts attached;
- the leaf is registered, and the agent guidance switches to "legal and
  financial questions MUST go through research".
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.userdocs.research as research_pkg
from app.tools.builtin import documentation_search as ds
from app.tools.builtin import user_documents as tool
from app.userdocs import citations as citations_module
from app.userdocs import query as query_pkg
from app.userdocs.cite import TOKEN_RE, make_token
from app.userdocs.index import IndexDB
from app.userdocs.research.errors import InvalidRequest, JobNotFound, ResearchBusy, ResearchUnavailable
from app.userdocs.research.types import (
    ACTIVE,
    CANCELLED,
    COMPLETE,
    INTERRUPTED,
    NEEDS_CLARIFICATION,
    READ_FULL,
    RUNNING,
    Cell,
    Clarification,
    CompiledTable,
    CoverageRow,
    Dossier,
    Evidence,
    Finding,
    Issue,
    JobView,
)
from app.userdocs.types import Chunk, ChunkDiff
from app.utils.task_context import current_task_id_var

TEXT = "Tranh chấp đất đai mà đương sự có Giấy chứng nhận thì do Tòa án nhân dân giải quyết."


# ── the index the dossier's tokens point into ──────────────────────────────


@pytest.fixture
def index(tmp_path: Path):
    db = IndexDB.open(str(tmp_path / "uid-a" / "index.db"), profile_uid="uid-a")
    row = db.insert_file("local", "Luat/luat.txt", "h:luat", name="luat.txt", kind="text", status="indexed")
    h = hashlib.blake2b(TEXT.encode(), digest_size=16).hexdigest()
    db.apply_chunks(file_id=row["id"], folder_id=None, source="local", diff=ChunkDiff(add=[
        Chunk(ordinal=0, ctype="body", heading="", text=TEXT, text_hash=h, locator={"article": "203"})]))
    yield SimpleNamespace(db=db, fid=row["cite_id"], token=make_token(row["cite_id"], h))
    db.close()


# ── a fake job layer ───────────────────────────────────────────────────────


class FakeJobs:
    """The contract of ``app.userdocs.research.jobs``, in memory."""

    def __init__(self) -> None:
        self.jobs: dict[str, JobView] = {}
        self.calls: list[tuple] = []
        # (call, collect flag): the tool shows every outcome it waits for.
        self.collect: list[tuple[str, bool]] = []
        self.collected: list[tuple[str, str]] = []
        # What wait_job turns a job into (one step per call), by job id.
        self.after_wait: dict[str, list[JobView]] = {}
        self.fail_start: Exception | None = None
        self.seq = 0

    def module(self) -> types.ModuleType:
        mod = types.ModuleType("app.userdocs.research.jobs")
        for name in ("wait_cap", "start_job", "continue_job", "wait_job", "cancel_job", "get_job",
                     "mark_collected", "resolve_conversation_id"):
            setattr(mod, name, getattr(self, name))
        return mod

    def _own(self, profile: str, job_id: str) -> JobView:
        view = self.jobs.get(job_id)
        if view is None or view.profile != profile:
            raise JobNotFound(f"No research job {job_id!r} for this profile.")
        return view

    def wait_cap(self) -> float:
        return 42.0

    def resolve_conversation_id(self, profile, context_id):
        self.calls.append(("resolve", profile, context_id))
        return f"conv-of-{context_id}"

    async def start_job(self, *, profile, spec, conversation_id, run_id=None, variables=None, collect=False):
        self.calls.append(("start", profile, spec, conversation_id, run_id, variables))
        self.collect.append(("start", collect))
        if self.fail_start is not None:
            raise self.fail_start
        self.seq += 1
        view = JobView(job_id=f"job{self.seq:03d}", profile=profile, status=RUNNING, mode=spec.mode,
                       domain=spec.domain, question=spec.question, conversation_id=conversation_id,
                       budget=250_000, progress={"phase": "Planning", "done": 0, "total": 0, "steps": []})
        self.jobs[view.job_id] = view
        return view

    async def continue_job(self, *, profile, job_id, answers=None, variables=None, collect=False):
        self.calls.append(("continue", profile, job_id, answers, variables))
        self.collect.append(("continue", collect))
        view = self._own(profile, job_id)
        if view.status in (NEEDS_CLARIFICATION, INTERRUPTED):
            view.status = RUNNING
        return view

    async def wait_job(self, *, profile, job_id, timeout, collect=False):
        self.calls.append(("wait", profile, job_id, timeout))
        self.collect.append(("wait", collect))
        self._own(profile, job_id)
        queue = self.after_wait.get(job_id) or []
        if queue:
            self.jobs[job_id] = queue.pop(0)
        return self.jobs[job_id]

    async def cancel_job(self, *, profile, job_id):
        self.calls.append(("cancel", profile, job_id))
        view = self._own(profile, job_id)
        self.after_wait[job_id] = [_view(job_id, CANCELLED, profile=profile)]
        return view

    def get_job(self, profile, job_id):
        self.calls.append(("get", profile, job_id))
        return self._own(profile, job_id)

    def mark_collected(self, profile, job_id):
        self.collected.append((profile, job_id))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _dossier(job_id: str, token: str, *, status=COMPLETE, rows: int = 3) -> Dossier:
    fid = token[4:12]
    return Dossier(
        job_id=job_id, mode="analyze", domain="legal", question="Q", status=status,
        coverage=[CoverageRow(fid=fid, rel_path="Luat/luat.txt", kind="text", read=READ_FULL, chunks_read=1,
                              chunks_total=1)],
        issues=[Issue(title="Who decides", findings=[Finding(
            issue="Who decides", stance="supports", text="The court decides.", provision="Điều 203",
            evidence=[Evidence(token=token, quote=TEXT[:50], quote_status="exact", label="Điều 203",
                               rel_path="Luat/luat.txt")])])],
        compiled=CompiledTable(columns=["file", "value"],
                               rows=[{"file": Cell(f"f{i}.xlsx"), "value": Cell(str(i), [token])}
                                     for i in range(rows)],
                               artifacts=[{"uri": "/api/files/alice/exports/research/x/table.csv",
                                           "name": "table.csv", "mime_type": "text/csv"}]),
    )


def _view(job_id: str, status: str, *, profile="alice", dossier=None, **kw) -> JobView:
    return JobView(job_id=job_id, profile=profile, status=status, mode="analyze", domain="legal", question="Q",
                   tokens_in=100, tokens_out=20, budget=250_000, dossier=dossier, **kw)


@pytest.fixture
def env(monkeypatch, index):
    fake = FakeJobs()
    mod = fake.module()
    monkeypatch.setitem(sys.modules, "app.userdocs.research.jobs", mod)
    monkeypatch.setattr(research_pkg, "jobs", mod, raising=False)
    engine = SimpleNamespace(db=index.db, read=lambda *a, **k: pytest.fail("the index read must not run"))
    opened: list[str] = []

    def fake_open(profile):
        opened.append(profile)
        return SimpleNamespace(engine=engine, code=None, message=None, snapshot={})

    monkeypatch.setattr(query_pkg, "open_engine", fake_open)
    monkeypatch.setattr(ds, "_delivery_budget", lambda profile, reserved: 3000)
    issued: list[tuple] = []
    monkeypatch.setattr(citations_module, "issue", lambda p, c, items: issued.append((p, c, list(items))))
    return SimpleNamespace(jobs=fake, index=index, issued=issued, opened=opened, engine=engine,
                           open=fake_open, monkeypatch=monkeypatch)


def _run(profile: str = "alice", **args):
    async def go():
        token = current_task_id_var.set("run-7")
        try:
            return await tool.UserDocumentsResearchTool().run({
                "_profile": profile, "_context_id": "ctx-1",
                "_variables": {"RESEARCH_MODEL_GROUP": "low", "RESEARCH_TOKEN_BUDGET": 50_000}, **args})
        finally:
            current_task_id_var.reset(token)

    return asyncio.run(go())


def _read(profile: str = "alice", **args):
    return asyncio.run(tool.UserDocumentsReadTool().run({"_profile": profile, "_context_id": "ctx-1", **args}))


def _text(result) -> str:
    if result.content:
        return result.content[0]["text"]
    sc = result.structured_content or {}
    return sc.get("text") or sc.get("message") or ""


PRELIM = "PRELIMINARY — do not conclude from this; call user_documents__research again with continue_job={}."


# ── start ──────────────────────────────────────────────────────────────────


def test_start_waits_for_the_job_and_returns_its_dossier(env):
    env.jobs.after_wait["job001"] = [_view("job001", COMPLETE, dossier=_dossier("job001", env.index.token),
                                           conversation_id="conv-of-ctx-1")]
    result = _run(question="Ai giải quyết tranh chấp đất của ABC?", mode="analyze", domain="legal",
                  scope={"folder": ["ABC"]}, reference_scope={"folder": ["Luat"]})
    start = next(c for c in env.jobs.calls if c[0] == "start")
    _, profile, spec, conversation_id, run_id, variables = start
    assert profile == "alice" and conversation_id == "conv-of-ctx-1" and run_id == "run-7"
    assert spec.question.startswith("Ai giải quyết") and spec.mode == "analyze" and spec.domain == "legal"
    assert spec.scope == {"folder": ["ABC"]} and spec.reference_scope == {"folder": ["Luat"]}
    assert variables == {"RESEARCH_MODEL_GROUP": "low", "RESEARCH_TOKEN_BUDGET": 50_000}
    assert ("wait", "alice", "job001", 42.0) in env.jobs.calls
    # The tool shows the outcome itself, so it holds it from the start: a job
    # that settles at once is not also reported as an injected turn.
    assert env.jobs.collect == [("start", True), ("wait", True)]
    sc = result.structured_content
    assert sc["_files"][0]["name"] == "table.csv"
    assert "Status: complete." in sc["text"] and "Not legal advice" in sc["text"]
    assert result.token_usage is None
    assert env.jobs.collected == [("alice", "job001")]
    # The printed tokens are registered for the conversation the call came from.
    profile, ctx, items = env.issued[-1]
    assert (profile, ctx) == ("alice", "ctx-1")
    assert {c.token for c in items} == {m.group(0) for m in TOKEN_RE.finditer(sc["text"])}
    assert env.index.token in {c.token for c in items} and all(c.leaf == "research" for c in items)


def test_a_job_still_running_is_preliminary_and_not_collected(env):
    result = _run(question="Compile the business results in MKT-report", mode="compile",
                  scope={"folder": ["MKT-report"]})
    text = _text(result)
    assert text.splitlines()[-1] == PRELIM.format("job001")
    assert env.jobs.collected == []
    assert env.jobs.names() == ["resolve", "start", "wait"]


def test_defaults_are_analyze_and_general(env):
    _run(question="What changed in the Q3 policy?")
    spec = next(c for c in env.jobs.calls if c[0] == "start")[2]
    assert (spec.mode, spec.domain, spec.scope, spec.reference_scope) == ("analyze", "general", None, None)


def test_a_bad_scope_is_an_observation_before_any_job_starts(env):
    result = _run(question="x", scope={"types": ["spreadsheets"]})
    assert result.structured_content["error"] == "InvalidFilter"
    assert "start" not in env.jobs.names()


@pytest.mark.parametrize("args,code", [
    ({}, "InvalidRequest"),
    ({"cancel": True}, "InvalidRequest"),
    ({"continue_job": "job001", "answers": ["not", "an", "object"]}, "InvalidRequest"),
])
def test_malformed_calls_are_invalid_requests(env, args, code):
    env.jobs.jobs["job001"] = _view("job001", NEEDS_CLARIFICATION)
    assert _run(**args).structured_content["error"] == code


@pytest.mark.parametrize("err", [
    ResearchBusy("job009", "an earlier question"),
    InvalidRequest("unknown mode 'summarize'"),
    ResearchUnavailable("User Document Search is off for this profile.", status_code="disabled"),
])
def test_job_errors_come_back_as_structured_content(env, err):
    env.jobs.fail_start = err
    sc = _run(question="x").structured_content
    for k, v in err.to_dict().items():
        assert sc[k] == v
    if isinstance(err, ResearchUnavailable):
        assert "unavailable (disabled)" in sc["text"]
    if isinstance(err, ResearchBusy):
        assert sc["job_id"] == "job009"
    assert env.jobs.collected == []


# ── continue, answer, cancel, page ─────────────────────────────────────────


def test_answering_a_question_resumes_the_job_and_waits(env):
    d = _dossier("job001", env.index.token, status=NEEDS_CLARIFICATION)
    d.clarification = Clarification(kind="edition", question="Which edition?",
                                    candidates=[{"fid": env.index.fid, "title": "Luật Đất đai"}],
                                    answer_keys={"edition": "a fid from candidates"})
    env.jobs.jobs["job001"] = _view("job001", NEEDS_CLARIFICATION, dossier=d)
    env.jobs.after_wait["job001"] = [_view("job001", COMPLETE, dossier=_dossier("job001", env.index.token))]
    result = _run(continue_job="job001", answers={"edition": f" {env.index.fid} ", "confirm": True, "n": 2,
                                                  "skip": None})
    call = next(c for c in env.jobs.calls if c[0] == "continue")
    assert call[1:4] == ("alice", "job001", {"edition": env.index.fid, "confirm": True, "n": 2})
    assert call[4]["RESEARCH_MODEL_GROUP"] == "low"
    assert "wait" in env.jobs.names() and "Status: complete." in _text(result)
    assert env.jobs.collected == [("alice", "job001")]


def test_a_waiting_job_is_shown_with_its_question_and_collected(env):
    d = _dossier("job001", env.index.token, status=NEEDS_CLARIFICATION)
    d.clarification = Clarification(kind="scope", question="'ABC' matches 2 folders. Which one?",
                                    candidates=[{"rel_path": "Clients/ABC"}, {"rel_path": "Old/ABC"}],
                                    answer_keys={"scope_folder": "the rel_path of the folder to use"})
    env.jobs.jobs["job001"] = _view("job001", NEEDS_CLARIFICATION, dossier=d)
    fake = env.jobs

    async def stay(*, profile, job_id, answers=None, variables=None, collect=False):  # a job that keeps waiting
        fake.calls.append(("continue", profile, job_id, answers, variables))
        return fake._own(profile, job_id)

    env.monkeypatch.setattr(sys.modules["app.userdocs.research.jobs"], "continue_job", stay)
    text = _text(_run(continue_job="job001"))
    assert "matches 2 folders" in text and "Clients/ABC" in text and "scope_folder" in text
    assert "wait" not in env.jobs.names()  # nothing to wait for
    assert env.jobs.collected == [("alice", "job001")]


def test_cancel_cancels_waits_briefly_and_reports(env):
    env.jobs.jobs["job001"] = _view("job001", RUNNING)
    text = _text(_run(continue_job="job001", cancel=True))
    assert env.jobs.names() == ["cancel", "wait"]
    assert env.jobs.calls[-1][3] == min(42.0, tool.CANCEL_WAIT_S)
    assert "Status: cancelled" in text
    assert env.jobs.collected == [("alice", "job001")]


def test_cancel_works_while_the_index_is_unavailable(env):
    env.monkeypatch.setattr(query_pkg, "open_engine",
                            lambda p: SimpleNamespace(engine=None, code="disabled", message="off", snapshot={}))
    env.jobs.jobs["job001"] = _view("job001", RUNNING)
    result = _run(continue_job="job001", cancel=True)
    assert "Status: cancelled" in _text(result)
    assert env.issued == []  # no index: nothing to register


def test_a_page_of_a_finished_dossier(env):
    view = _view("job001", COMPLETE, dossier=_dossier("job001", env.index.token, rows=400))
    env.jobs.jobs["job001"] = view
    env.monkeypatch.setattr(ds, "_delivery_budget", lambda profile, reserved: 1200)
    first = _run(continue_job="job001")
    assert "Pages 2–" in first.structured_content["text"]
    second = _run(continue_job="job001", page=2)
    text = second.structured_content["text"]
    assert "Dossier page 2 of" in text and "wait" not in env.jobs.names()
    assert ds._tokens(text) <= 1200


# ── read research:<id> ─────────────────────────────────────────────────────


def test_read_of_a_research_ref_serves_the_dossier_not_a_file(env):
    env.jobs.jobs["job001"] = _view("job001", COMPLETE, dossier=_dossier("job001", env.index.token, rows=400))
    env.monkeypatch.setattr(ds, "_delivery_budget", lambda profile, reserved: 1200)
    result = _read(file="research:job001", page=3)
    text = result.structured_content["text"]
    assert "Dossier page 3 of" in text
    assert ("get", "alice", "job001") in env.jobs.calls
    assert env.jobs.collected == [("alice", "job001")]
    assert {c.token for c in env.issued[-1][2]} == {m.group(0) for m in TOKEN_RE.finditer(text)}


def test_read_of_a_running_research_ref_is_preliminary(env):
    env.jobs.jobs["job001"] = _view("job001", RUNNING)
    text = _text(_read(file="RESEARCH: job001"))
    assert text.splitlines()[-1] == PRELIM.format("job001")
    assert env.jobs.collected == []


def test_another_profiles_job_does_not_exist(env):
    env.jobs.jobs["job001"] = _view("job001", COMPLETE, dossier=_dossier("job001", env.index.token))
    assert _run("bob", continue_job="job001").structured_content["error"] == "JobNotFound"
    assert _run("bob", continue_job="job001", cancel=True).structured_content["error"] == "JobNotFound"
    assert _read("bob", file="research:job001").structured_content["error"] == "JobNotFound"
    assert _read("alice", file="research:").structured_content["error"] == "InvalidRequest"
    assert env.jobs.collected == [] and env.issued == []
    # The owner still reads it.
    assert "Status: complete." in _read("alice", file="research:job001").structured_content["text"]


@pytest.mark.parametrize("status,collected", [
    (RUNNING, False), ("queued", False), ("planning", False), (NEEDS_CLARIFICATION, True),
    ("needs_confirmation", True), (INTERRUPTED, True), (COMPLETE, True), ("partial", True), ("failed", True),
    (CANCELLED, True),
])
def test_collected_exactly_when_the_state_is_deliverable(env, status, collected):
    env.jobs.jobs["job001"] = _view("job001", status, dossier=None if status in ACTIVE else _dossier(
        "job001", env.index.token, status=status))
    _read(file="research:job001")
    assert bool(env.jobs.collected) is collected


# ── registration and guidance ──────────────────────────────────────────────


def test_the_leaf_is_registered_with_its_parameters():
    from app.tools.base import make_leaf_name
    from app.userdocs.research import render as RR

    leaves = {t.name: t for t in tool.get_tools({})}
    assert list(leaves) == ["find_files", "search", "read", "research"]
    leaf = leaves["research"]
    assert make_leaf_name(tool.TOOL_ID, leaf.name) == RR.FN_RESEARCH == "user_documents__research"
    assert make_leaf_name(tool.TOOL_ID, tool.LEAF_READ) == RR.FN_READ
    props = leaf.parameters["properties"]
    assert set(props) == {"question", "mode", "domain", "scope", "reference_scope", "continue_job", "answers",
                          "cancel", "page"}
    assert props["mode"]["enum"] == ["compile", "analyze"]
    assert props["domain"]["enum"] == ["legal", "financial", "general"]
    assert props["scope"]["properties"] == tool.FILTERS_SCHEMA["properties"]
    assert "research:" in leaves["read"].parameters["properties"]["file"]["description"]


def test_the_agent_guidance_now_sends_legal_questions_to_research():
    import app.agent.reasoning_agent as ra

    group = SimpleNamespace(tool_id="user_documents", config_name="user_documents",
                            skills=[SimpleNamespace(name=t.name) for t in tool.get_tools({})])
    guidance = ra._build_user_documents_guidance([group])
    assert "`user_documents__research`" in guidance and "MUST go through" in guidance
    assert "continue_job" in guidance


# ── the Research activity panel ────────────────────────────────────────────


class _Bus:
    def __init__(self) -> None:
        self.frames: list[tuple[str, str, dict]] = []

    async def publish(self, conversation_id, event_type, data):
        self.frames.append((conversation_id, event_type, data))


class _Storage:
    def __init__(self) -> None:
        self.patches: list[tuple[str, dict]] = []

    async def update_message_metadata(self, message_id, patch):
        self.patches.append((message_id, patch))


@pytest.fixture
def panel(monkeypatch):
    import app.events.runner as runner
    import app.events.stream_bus as stream_bus
    from app.userdocs.research import activity

    bus, storage = _Bus(), _Storage()
    monkeypatch.setattr(stream_bus, "get_event_stream_bus", lambda: bus)
    monkeypatch.setattr(runner, "get_conversation_storage", lambda: storage)
    monkeypatch.setattr(activity, "_COALESCE_SECONDS", 0.01)
    monkeypatch.setattr(activity, "_activities", {})
    return SimpleNamespace(mod=activity, bus=bus, storage=storage)


def test_the_panel_publishes_coalesced_full_snapshots(panel):
    A = panel.mod

    async def go():
        act = await A.ResearchActivity.start(conversation_id="c1", profile="alice", job_id="job001",
                                             title="Compile MKT-report", mode="compile")
        assert len(panel.bus.frames) == 1  # start publishes at once
        sink = A.ActivityProgressSink(act)
        sink.set_phase("Reading files")
        sid = sink.add_step(kind="read", label="Reading MKT/q1.xlsx (1/2)")
        sink.set_progress(1, 2)
        sink.update_usage(1200, 300, 250_000)
        sink.resolve_step(sid, status="done", detail_suffix=" · 4 rows")
        await asyncio.sleep(0.05)
        return act, sink

    act, sink = asyncio.run(go())
    assert len(panel.bus.frames) == 2  # five mutations, one coalesced publish
    conv, kind, snap = panel.bus.frames[-1]
    assert (conv, kind) == ("c1", "research_activity")
    assert set(snap) >= {"job_id", "status", "title", "mode", "phase", "started_at", "updated_at", "progress",
                         "steps", "total_steps", "usage", "summary", "error"}
    assert snap["job_id"] == "job001" and snap["status"] == "running" and snap["mode"] == "compile"
    assert snap["phase"] == "Reading files" and snap["progress"] == {"done": 1, "total": 2}
    assert snap["usage"] == {"tokens_in": 1200, "tokens_out": 300, "budget": 250_000}
    assert [(s["label"], s["status"], s["detail"]) for s in snap["steps"]] == [
        ("Reading MKT/q1.xlsx (1/2)", "done", "· 4 rows")]
    # The sink keeps its own record, which feeds JobView.progress.
    assert sink.phase == "Reading files" and (sink.done, sink.total) == (1, 2)
    assert sink.steps[0]["status"] == "done"
    assert A.get_snapshot("c1")["total_steps"] == 1


def test_finish_patches_the_saved_message_in_either_order(panel):
    A = panel.mod

    async def go():
        early = await A.ResearchActivity.start(conversation_id="c1", profile="alice", job_id="j1", title="Q",
                                               mode="analyze")
        A.set_persist_target("c1", "msg-1")  # the turn ended first
        assert panel.storage.patches == []  # still running: nothing to patch
        await early.finish(status="complete", summary="3 issues")
        await asyncio.sleep(0)
        late = await A.ResearchActivity.start(conversation_id="c2", profile="alice", job_id="j2", title="Q",
                                              mode="analyze")
        await late.finish(status="needs_clarification", summary="Which edition?")
        A.set_persist_target("c2", "msg-2")  # the job settled first
        await asyncio.sleep(0)

    asyncio.run(go())
    patched = {m: p["research_activity"] for m, p in panel.storage.patches}
    assert patched["msg-1"]["status"] == "complete" and patched["msg-1"]["summary"] == "3 issues"
    assert patched["msg-2"]["status"] == "needs_clarification"


def test_a_resumed_job_keeps_its_panel_and_patches_only_the_new_turn(panel):
    A = panel.mod

    async def go():
        act = await A.ResearchActivity.start(conversation_id="c1", profile="alice", job_id="j1", title="Q",
                                             mode="analyze")
        first = A.ActivityProgressSink(act)
        first.add_step(kind="step", label="Planning")
        await act.finish(status="needs_clarification")
        A.set_persist_target("c1", "msg-1")
        await asyncio.sleep(0)
        again = await A.ResearchActivity.start(conversation_id="c1", profile="alice", job_id="j1", title="Q",
                                               mode="analyze")
        assert again is act and act.status == "running"
        second = A.ActivityProgressSink(act)
        sid = second.add_step(kind="step", label="Reading the law")  # the new sink's s1…
        second.resolve_step(sid, status="done")  # …resolves the activity's s2
        await act.finish(status="complete")
        await asyncio.sleep(0)
        A.set_persist_target("c1", "msg-2")
        await asyncio.sleep(0)

    asyncio.run(go())
    snap = panel.mod.get_snapshot("c1")
    assert [(s["id"], s["label"], s["status"]) for s in snap["steps"]] == [
        ("s1", "Planning", "stopped"), ("s2", "Reading the law", "done")]
    assert [(m, p["research_activity"]["status"]) for m, p in panel.storage.patches] == [
        ("msg-1", "needs_clarification"), ("msg-2", "complete")]


def test_clear_forgets_the_panel(panel):
    A = panel.mod

    async def go():
        await A.ResearchActivity.start(conversation_id="c1", profile="alice", job_id="j1", title="Q", mode="analyze")

    asyncio.run(go())
    assert A.get_snapshot("c1") is not None
    A.clear("c1")
    assert A.get_snapshot("c1") is None and A.get_snapshot("c2") is None
    A.clear("never-there")


def test_the_research_activity_route_answers_the_owner_only(panel):
    import json

    from app.api.conversations import get_conversation_routes

    async def go():
        await panel.mod.ResearchActivity.start(conversation_id="c1", profile="alice", job_id="j1", title="Q",
                                               mode="analyze")

    asyncio.run(go())

    class _Convs:
        async def get_conversation(self, cid):
            return {"id": cid, "profile": "alice"} if cid in ("c1", "c2") else None

    route = next(r for r in get_conversation_routes(_Convs())
                 if r.path == "/api/conversations/{conversation_id}/research-activity")

    def call(user, cid):
        req = SimpleNamespace(user=SimpleNamespace(is_authenticated=bool(user), username=user),
                              path_params={"conversation_id": cid})
        resp = asyncio.run(route.endpoint(req))
        return resp.status_code, json.loads(resp.body)

    status, body = call("alice", "c1")
    assert status == 200 and body["activity"]["job_id"] == "j1"
    assert call("alice", "c2") == (200, {"activity": None})
    assert call("bob", "c1")[0] == 403
    assert call("alice", "nope")[0] == 404
    assert call("", "c1")[0] == 401
