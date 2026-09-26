"""The research job runner (``app.documents.research.jobs``).

A real main database (two profiles, alice and bob, each with conversations),
the real storage and delivery claim, and a fake pipeline per test standing in
for compile/analyze. What is pinned:

- a long-poll returns the running view and never cancels the job — neither on
  timeout nor when the waiter itself is cancelled;
- one running job per profile, never across profiles; a job id is invisible
  to any other profile;
- every pipeline outcome maps to its status, and a stop to ask resumes from
  the saved checkpoint with the answers merged;
- each deliverable state reaches its conversation exactly once: collected by
  the tool, injected at once when the conversation is idle, at the end of the
  live turn otherwise, or at boot — never twice, and never a cancellation;
- retention and purge.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

import app.events.queue as event_queue  # noqa: E402
import app.events.runner as event_runner  # noqa: E402
import app.storage as storage_pkg  # noqa: E402
import app.storage.documents_citations_storage as cit_module  # noqa: E402
import app.storage.documents_research_storage as research_storage_module  # noqa: E402
import app.storage.documents_storage as uds_module  # noqa: E402
from app.config.settings import BaseConfig  # noqa: E402
from app.constants import ChatCompletionTypeEnum as T  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.events import task_result_inbox  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.documents_citations_storage import DocumentCitationsStorage  # noqa: E402
from app.storage.documents_research_storage import DocumentResearchStorage  # noqa: E402
from app.storage.documents_storage import DocumentsStorage  # noqa: E402
from app.documents.research import context as C  # noqa: E402
from app.documents.research import jobs  # noqa: E402
from app.documents.research.context import (  # noqa: E402
    BudgetExceeded,
    NeedsInput,
    ResearchSpec,
)
from app.documents.research.errors import (  # noqa: E402
    InvalidRequest,
    JobNotFound,
    ResearchBusy,
    ResearchUnavailable,
)
from app.documents.research.types import (  # noqa: E402
    CANCELLED,
    COMPLETE,
    FAILED,
    INTERRUPTED,
    NEEDS_CONFIRMATION,
    PARTIAL,
    QUEUED,
    RUNNING,
    Clarification,
)

TABLES = (
    "profiles", "channels", "conversations", "messages",
    "document_sources", "document_captions", "document_vision_usage", "document_citations",
    "document_research_jobs",
)
TOOL = {"type": "function", "function": {"name": "note", "parameters": {"type": "object", "properties": {}}}}


class FakeLLM:
    """Answers every call by calling the requested function; reports 100 in /
    20 out per call."""

    provider_name = "fake"
    model_name = "fake-1"

    async def chat_completion(self, messages, **kwargs):
        name = kwargs["tools"][0]["function"]["name"]
        yield {"type": T.FUNCTION_CALLING, "data": {"function": [{"name": name, "arguments": {"ok": True}}]}}
        yield {"type": T.DONE, "input_tokens": 100, "output_tokens": 20}


class FakeUsage:
    def __init__(self):
        self.calls: list[dict] = []

    async def add_usage_records(self, **kwargs):
        self.calls.append(kwargs)
        return ["u"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    sysdir = tmp_path / "system"
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    provider = SqliteDatabaseProvider(str(tmp_path / "main.db"))
    eng = provider.sync_engine()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    now = time.time() * 1000
    with eng.begin() as c:
        for uid, name in (("uid-alice", "alice"), ("uid-bob", "bob")):
            c.execute(text("INSERT INTO profiles (id,name,created_at,updated_at) VALUES (:i,:n,:t,:t)"),
                      {"i": uid, "n": name, "t": now})
        for cid, profile, ctx in (
            ("c-alice", "alice", "c-alice"), ("c-alice2", "alice", "c-alice2"),
            ("c-chan", "alice", "tg:12345"), ("c-bob", "bob", "c-bob"),
        ):
            c.execute(text(
                "INSERT INTO conversations (id,profile,context_id,kind,title,compaction_watermark,"
                "created_at,updated_at) VALUES (:i,:p,:c,'chat','t',-1,:t,:t)"
            ), {"i": cid, "p": profile, "c": ctx, "t": now})

    monkeypatch.setattr(uds_module, "_instance", DocumentsStorage(provider))
    monkeypatch.setattr(cit_module, "_instance", DocumentCitationsStorage(provider))
    store = DocumentResearchStorage(provider)
    monkeypatch.setattr(research_storage_module, "_instance", store)
    cs = ConversationStorage(provider)
    cs._initialized = True
    monkeypatch.setattr(event_runner, "_conversation_storage", cs)

    e = SimpleNamespace(provider=provider, store=store, cs=cs, sysdir=sysdir, pipeline=None,
                        enqueued=[], usage=FakeUsage(), engine_error=None, fail_enqueue=0)

    def open_engine(profile):
        if e.engine_error is not None:
            raise e.engine_error
        return SimpleNamespace(profile=profile, db=None)

    async def enqueue(**kwargs):
        if e.fail_enqueue:
            e.fail_enqueue -= 1
            raise RuntimeError("queue is down")
        e.enqueued.append(kwargs)

    monkeypatch.setattr(jobs, "_open_engine", open_engine)
    monkeypatch.setattr(jobs, "_create_llm", lambda group, profile: FakeLLM())
    monkeypatch.setattr(jobs, "_configured_variables", lambda profile: {})
    monkeypatch.setattr(jobs, "_pipeline_for", lambda mode: e.pipeline)
    monkeypatch.setattr(jobs, "_activity_module", lambda: None)
    monkeypatch.setattr(jobs, "_render", lambda profile, view: (
        f"RENDERED {view.job_id} {view.status}", [], {"job_id": view.job_id, "status": view.status}))
    monkeypatch.setattr(jobs, "CANCEL_GRACE_S", 0.2)
    monkeypatch.setattr(event_queue, "enqueue_user_message", enqueue)
    monkeypatch.setattr(storage_pkg, "get_usage_storage", lambda *a, **k: e.usage)
    jobs._reset_for_tests()
    task_result_inbox.clear_all()
    yield e
    jobs._reset_for_tests()
    task_result_inbox.clear_all()


def spec(question="What does the lease say about deposits?", **kw) -> ResearchSpec:
    return ResearchSpec(question=question, **kw)


async def settle() -> None:
    """Let every job task and delivery finish."""
    for _ in range(50):
        pending = [t for t in list(jobs._background) if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def completing(record=None):
    """A pipeline that reads, checkpoints, and completes."""

    async def pipeline(ctx):
        if record is not None:
            record.append(dict(ctx.state))
        ctx.phase("Reading files")
        sid = ctx.step("Reading a.pdf (1/1)")
        await ctx.llm.call(system="s", user="u", tool=TOOL)
        ctx.state["read"] = ["a.pdf"]
        await ctx.save()
        ctx.done_step(sid)
        ctx.progress(1, 1)
        ctx.phase("Writing up")
        await ctx.llm.call(system="s", user="u", tool=TOOL)
        ctx.dossier.notes.append("found it")
        ctx.dossier.status = COMPLETE
        return ctx.dossier

    return pipeline


def gated(gate_box: dict, then=None):
    """A pipeline that checkpoints and then holds until ``gate_box['gate']``
    is set, checking for cancellation while it waits."""

    async def pipeline(ctx):
        ctx.phase("Reading files")
        sid = ctx.step("Reading a.pdf (1/2)")
        await ctx.llm.call(system="s", user="u", tool=TOOL)
        ctx.state["read"] = ["a.pdf"]
        await ctx.save()
        ctx.done_step(sid)
        ctx.progress(1, 2)
        while not gate_box["gate"].is_set():
            ctx.check()
            await asyncio.sleep(0.01)
        ctx.check()
        if then is not None:
            return await then(ctx)
        ctx.dossier.status = COMPLETE
        return ctx.dossier

    return pipeline


# ── long-poll ──────────────────────────────────────────────────────────────


def test_a_long_poll_returns_the_running_view_and_never_cancels_the_job(env):
    box: dict = {}
    env.pipeline = gated(box)

    async def scenario():
        box["gate"] = asyncio.Event()
        task_result_inbox.bind_run("turn-1", "c-alice")  # the agent's turn is live
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice", run_id="turn-1")
        assert view.status == QUEUED and len(view.job_id) == 12

        polled = await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=0.3)
        assert polled.status == RUNNING
        assert polled.progress["phase"] == "Reading files"
        assert (polled.progress["done"], polled.progress["total"]) == (1, 2)
        assert polled.progress["steps"][0]["label"] == "Reading a.pdf (1/2)"
        assert (polled.tokens_in, polled.tokens_out) == (100, 20)

        # A waiter that is itself cancelled (the user pressed Stop) …
        waiter = asyncio.create_task(jobs.wait_job(profile="alice", job_id=view.job_id, timeout=30))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        # … and one that timed out leave the job running.
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=0.05)
        assert jobs.get_job("alice", view.job_id).status == RUNNING

        box["gate"].set()
        done = await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        return view.job_id, done

    job_id, done = asyncio.run(scenario())
    assert done.status == COMPLETE
    assert done.dossier.status == COMPLETE
    assert done.finished_at is not None and done.progress == {}
    row = env.store.get("alice", job_id)
    assert row["state"] == {"read": ["a.pdf"]}
    assert row["run_id"] == "turn-1" and row["provider"] == "fake" and row["model"] == "fake-1"
    assert row["elapsed_s"] > 0


def test_wait_cap_stays_under_the_tool_timeout(monkeypatch):
    monkeypatch.setattr(BaseConfig, "MCP_TOOL_CALL_TIMEOUT", 300)
    assert jobs.wait_cap() == 240
    monkeypatch.setattr(BaseConfig, "MCP_TOOL_CALL_TIMEOUT", 120)
    assert jobs.wait_cap() == 75
    monkeypatch.setattr(BaseConfig, "MCP_TOOL_CALL_TIMEOUT", 30)
    assert jobs.wait_cap() == 5


# ── one job per profile, profiles apart ─────────────────────────────────────


def test_one_running_job_per_profile_but_profiles_are_independent(env):
    box: dict = {}
    env.pipeline = gated(box)

    async def scenario():
        box["gate"] = asyncio.Event()
        first = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        with pytest.raises(ResearchBusy) as busy:
            await jobs.start_job(profile="alice", spec=spec("Another question"), conversation_id="c-alice2")
        assert busy.value.to_dict()["job_id"] == first.job_id
        assert busy.value.status == 409
        other = await jobs.start_job(profile="bob", spec=spec("Bob's question"), conversation_id="c-bob")
        assert other.job_id != first.job_id
        box["gate"].set()
        await settle()
        # Free again once it finished.
        box["gate"] = asyncio.Event()
        box["gate"].set()
        again = await jobs.start_job(profile="alice", spec=spec("A third"), conversation_id=None)
        await settle()
        return again

    again = asyncio.run(scenario())
    assert jobs.get_job("alice", again.job_id).status == COMPLETE


def test_a_job_id_is_invisible_to_every_other_profile(env):
    env.pipeline = completing()

    async def scenario():
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        await settle()
        with pytest.raises(JobNotFound):
            await jobs.wait_job(profile="bob", job_id=view.job_id, timeout=0)
        with pytest.raises(JobNotFound):
            await jobs.continue_job(profile="bob", job_id=view.job_id, answers={"confirm": True})
        with pytest.raises(JobNotFound):
            await jobs.cancel_job(profile="bob", job_id=view.job_id)
        return view.job_id

    job_id = asyncio.run(scenario())
    with pytest.raises(JobNotFound) as nf:
        jobs.get_job("bob", job_id)
    assert nf.value.status == 404
    with pytest.raises(JobNotFound):
        jobs.mark_collected("bob", job_id)
    assert [v.job_id for v in jobs.list_jobs("alice")] == [job_id]
    assert jobs.list_jobs("bob") == []
    assert jobs.list_jobs("alice")[0].dossier is None  # the list omits dossiers


def test_bad_requests_and_an_unavailable_index(env):
    async def start(**kw):
        return await jobs.start_job(profile="alice", conversation_id=None, **kw)

    for bad in (spec("   "), spec(mode="summarize"), spec(domain="medical"),
                spec(scope={"types": ["not-a-type"]}), spec(reference_scope=["Luat"])):
        with pytest.raises(InvalidRequest):
            asyncio.run(start(spec=bad))
    env.engine_error = ResearchUnavailable("Documentation search is off.", status_code="disabled")
    with pytest.raises(ResearchUnavailable) as off:
        asyncio.run(start(spec=spec()))
    assert off.value.to_dict()["status_code"] == "disabled"
    assert env.store.count("alice") == 0


# ── outcomes ──────────────────────────────────────────────────────────────


def test_a_stop_to_ask_resumes_from_its_checkpoint_with_the_answers(env):
    seen: list[dict] = []

    async def pipeline(ctx):
        seen.append({"state": dict(ctx.state), "answers": dict(ctx.answers)})
        if "read" not in ctx.state:
            ctx.state["read"] = ["a.pdf", "b.pdf"]
            await ctx.save()
        if not ctx.answers.get("confirm"):
            raise NeedsInput(Clarification(
                kind="unread", question="2 scanned files could not be read. Continue without them?",
                candidates=[{"rel_path": "scan.jpg", "reason": "awaiting_vision"}],
                answer_keys={"confirm": "true to continue without them"},
            ), NEEDS_CONFIRMATION)
        ctx.dossier.gaps.append("scan.jpg was not read")
        ctx.dossier.status = COMPLETE
        return ctx.dossier

    env.pipeline = pipeline

    async def scenario():
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        asked = await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        resumed = await jobs.continue_job(profile="alice", job_id=view.job_id,
                                          answers={"confirm": True, "note": "fine"})
        done = await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        return asked, resumed, done

    asked, resumed, done = asyncio.run(scenario())
    assert asked.status == NEEDS_CONFIRMATION
    assert asked.clarification is not None and asked.clarification.kind == "unread"
    assert asked.clarification.answer_keys == {"confirm": "true to continue without them"}
    assert resumed.status == QUEUED
    assert done.status == COMPLETE and done.clarification is None
    assert done.dossier.gaps == ["scan.jpg was not read"]
    # The second run started from the first one's checkpoint, with the answers.
    assert seen[0] == {"state": {}, "answers": {}}
    assert seen[1] == {"state": {"read": ["a.pdf", "b.pdf"]}, "answers": {"confirm": True, "note": "fine"}}


def test_continue_leaves_a_running_or_finished_job_alone(env):
    box: dict = {}
    env.pipeline = gated(box)

    async def scenario():
        box["gate"] = asyncio.Event()
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id=None)
        running = await jobs.continue_job(profile="alice", job_id=view.job_id, answers={"confirm": True})
        box["gate"].set()
        await settle()
        finished = await jobs.continue_job(profile="alice", job_id=view.job_id, answers={"confirm": True})
        return running, finished

    running, finished = asyncio.run(scenario())
    assert running.status in (QUEUED, RUNNING)
    assert finished.status == COMPLETE
    assert env.store.get("alice", running.job_id)["answers"] == {}


def test_cancel_stops_a_running_job_and_keeps_its_dossier(env):
    box: dict = {}
    env.pipeline = gated(box)

    async def scenario():
        box["gate"] = asyncio.Event()
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=0.2)
        cancelled = await jobs.cancel_job(profile="alice", job_id=view.job_id)
        await settle()
        return cancelled

    cancelled = asyncio.run(scenario())
    assert cancelled.status == CANCELLED
    assert cancelled.error == "cancelled by the user"
    assert env.store.get("alice", cancelled.job_id)["state"] == {"read": ["a.pdf"]}
    assert env.enqueued == []  # whoever cancelled already knows


def test_cancel_lands_even_in_the_middle_of_a_model_call(env):
    async def pipeline(ctx):
        await asyncio.sleep(30)  # a slow model call: no check() for a long while

    env.pipeline = pipeline

    async def scenario():
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        await asyncio.sleep(0.1)
        started = time.monotonic()
        cancelled = await jobs.cancel_job(profile="alice", job_id=view.job_id)
        await settle()
        return cancelled, time.monotonic() - started

    cancelled, took = asyncio.run(scenario())
    assert cancelled.status == CANCELLED
    assert took < 5
    assert not jobs.has_live_run("alice")


def test_cancel_a_job_waiting_for_an_answer(env):
    async def pipeline(ctx):
        raise NeedsInput(Clarification(kind="scope", question="Which ABC?"))

    env.pipeline = pipeline

    async def scenario():
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id=None)
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        return await jobs.cancel_job(profile="alice", job_id=view.job_id)

    cancelled = asyncio.run(scenario())
    assert cancelled.status == CANCELLED
    assert cancelled.clarification is None


def test_the_token_budget_ends_a_job_as_partial(env):
    async def pipeline(ctx):
        await ctx.llm.call(system="s", user="u", tool=TOOL)
        ctx.dossier.gaps.append("b.pdf not reached")
        await ctx.save()
        # The next prompt would overrun the 5000-token budget.
        await ctx.llm.call(system="s", user="x" * 20_000, tool=TOOL)
        raise AssertionError("the budget should have stopped this call")

    env.pipeline = pipeline

    async def scenario():
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id=None,
                                    variables={jobs.VAR_TOKEN_BUDGET: "5000", jobs.VAR_MODEL_GROUP: "low"})
        return await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)

    done = asyncio.run(scenario())
    assert done.status == PARTIAL
    assert "stopped at the token budget" in done.dossier.notes
    assert done.dossier.gaps == ["b.pdf not reached"]
    assert done.budget == 5000 and (done.tokens_in, done.tokens_out) == (100, 20)
    assert env.store.get("alice", done.job_id)["model_group"] == "low"


def test_a_pipeline_raising_budget_exceeded_itself_is_partial_too(env):
    async def pipeline(ctx):
        raise BudgetExceeded("estimate")

    env.pipeline = pipeline
    done = asyncio.run(_run_to_end(env))
    assert done.status == PARTIAL and "stopped at the token budget" in done.dossier.notes


def test_the_time_limit_ends_a_job_as_partial(env, monkeypatch):
    monkeypatch.setattr(C, "MAX_JOB_SECONDS", 0)

    async def pipeline(ctx):
        ctx.dossier.facts = []
        ctx.check()
        raise AssertionError("the time limit should have stopped the job")

    env.pipeline = pipeline
    done = asyncio.run(_run_to_end(env))
    assert done.status == PARTIAL
    assert "stopped at the time limit" in done.dossier.notes


def test_a_failure_keeps_what_the_job_found(env):
    async def pipeline(ctx):
        ctx.dossier.gaps.append("c.xlsx: sheet 2 unreadable")
        await ctx.save()
        raise RuntimeError("the model returned garbage")

    env.pipeline = pipeline
    done = asyncio.run(_run_to_end(env))
    assert done.status == FAILED
    assert "RuntimeError: the model returned garbage" in done.error
    assert done.dossier.gaps == ["c.xlsx: sheet 2 unreadable"]
    assert done.finished_at is not None


def test_an_invalid_pipeline_status_fails_the_job_instead_of_completing_it(env):
    async def pipeline(ctx):
        ctx.dossier.status = RUNNING  # a pipeline bug: never read as success
        return ctx.dossier

    env.pipeline = pipeline
    done = asyncio.run(_run_to_end(env))
    assert done.status == FAILED and "invalid status 'running'" in done.error
    assert done.dossier.outcome.reason == "execution_failed"


def test_every_ending_leaves_an_outcome_that_says_why(env):
    from app.documents.research.types import Outcome

    async def over_budget(ctx):
        ctx.dossier.outcome = Outcome(reason="running", queries=4, candidates=3, selected=1)
        await ctx.save()
        raise BudgetExceeded("estimate")

    async def failed_itself(ctx):
        ctx.dossier.outcome = Outcome(reason="model_failed", detail="the research model's answers failed")
        ctx.dossier.status = FAILED
        return ctx.dossier

    env.pipeline = over_budget
    done = asyncio.run(_run_to_end(env))
    # The counts the last checkpoint had are kept; the reason is the stop's.
    assert (done.status, done.dossier.outcome.reason, done.dossier.outcome.stopped_early) == (PARTIAL, "budget", True)
    assert (done.dossier.outcome.queries, done.dossier.outcome.selected) == (4, 1)
    env.pipeline = failed_itself
    done = asyncio.run(_run_to_end(env))
    assert done.status == FAILED and done.error == "the research model's answers failed"
    assert done.dossier.outcome.reason == "model_failed"


def test_the_completion_log_is_counts_only():
    from app.documents.research.types import Dossier, Outcome

    d = Dossier(job_id="j", mode="analyze", domain="legal", question="Secret question about ACME", status=PARTIAL,
                outcome=Outcome(reason="no_candidates", queries=6, detail="the searches found no candidate"))
    line = jobs._completion_log("j", "alice", PARTIAL, d, 12.0)
    assert "reason=no_candidates" in line and "queries=6" in line and "candidates=0" in line
    assert "ACME" not in line and "Secret" not in line


def test_no_model_configured_fails_the_job_with_a_reason(env, monkeypatch):
    def no_model(group, profile):
        raise ValueError("no provider")

    monkeypatch.setattr(jobs, "_create_llm", no_model)
    env.pipeline = completing()
    done = asyncio.run(_run_to_end(env))
    assert done.status == FAILED and "no model is configured" in done.error


def test_the_time_limit_counts_running_time_across_resumes(env):
    async def pipeline(ctx):
        await asyncio.sleep(0.2)
        if not ctx.answers.get("confirm"):
            raise NeedsInput(Clarification(kind="budget", question="Spend more?"), NEEDS_CONFIRMATION)
        await asyncio.sleep(0.2)
        ctx.dossier.status = COMPLETE
        return ctx.dossier

    env.pipeline = pipeline

    async def scenario():
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id=None)
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        first = env.store.get("alice", view.job_id)["elapsed_s"]
        await asyncio.sleep(0.3)  # waiting for the answer is not running time
        await jobs.continue_job(profile="alice", job_id=view.job_id, answers={"confirm": True})
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        return first, env.store.get("alice", view.job_id)["elapsed_s"]

    first, total = asyncio.run(scenario())
    assert 0.15 < first < 1.5
    assert first + 0.15 < total < first + 1.5


async def _run_to_end(env, profile="alice", conversation_id=None):
    view = await jobs.start_job(profile=profile, spec=spec(), conversation_id=conversation_id)
    done = await jobs.wait_job(profile=profile, job_id=view.job_id, timeout=5)
    await settle()
    return done


# ── usage ─────────────────────────────────────────────────────────────────


def test_usage_is_recorded_per_phase_against_the_conversation(env):
    env.pipeline = completing()
    asyncio.run(_run_to_end(env, conversation_id="c-alice"))
    records = [r for call in env.usage.calls for r in call["records"]]
    assert {call["conversation_id"] for call in env.usage.calls} == {"c-alice"}
    assert {call["profile"] for call in env.usage.calls} == {"alice"}
    assert len(records) == 2  # one per phase: "Reading files", then the rest at the end
    for r in records:
        assert (r["source_kind"], r["tool_id"], r["label"]) == ("documents", "documentation_search",
                                                                "Document research: analyze")
        assert (r["provider"], r["model"], r["model_group"]) == ("fake", "fake-1", "high")
    assert sum(r["input_tokens"] for r in records) == 200
    assert sum(r["output_tokens"] for r in records) == 40


# ── delivery ──────────────────────────────────────────────────────────────


def test_an_idle_conversation_gets_the_result_once(env):
    env.pipeline = completing()

    async def scenario():
        done = await _run_to_end(env, conversation_id="c-alice")
        # Every later hook finds nothing owed.
        await jobs.on_turn_end(conversation_id="c-alice", profile="alice")
        await jobs.boot_recover()
        return done

    done = asyncio.run(scenario())
    assert len(env.enqueued) == 1
    call = env.enqueued[0]
    assert call["conversation_id"] == "c-alice" and call["profile"] == "alice"
    assert call["push_user_message"] is False and call["update_title_from_query"] is False
    assert call["run_id"].startswith("research:c-alice:")
    assert call["trigger_event"]["kind"] == "research_result"
    assert call["trigger_event"]["job_id"] == done.job_id
    assert call["user_message_metadata"]["trigger"] is True
    assert call["agent_message_metadata"]["source"] == "research_result"
    assert f"RENDERED {done.job_id} complete" in call["query"]
    assert "Present these research results to the user" in call["query"]
    # The same response contract as the tool's pages, and the record of what
    # the delivery shows, for the turn's agent (never text it reads).
    assert "Never say an indexed document is missing" in call["query"]
    assert call["trigger_event"]["research_delivery"] == {"job_id": done.job_id, "status": "complete"}
    row = env.store.get("alice", done.job_id)
    assert row["delivered_rev"] == row["rev"] == 1


def test_a_result_the_live_turn_collected_is_never_injected(env):
    env.pipeline = completing()

    async def scenario():
        task_result_inbox.bind_run("turn-1", "c-alice")
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        done = await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        jobs.mark_collected("alice", view.job_id)  # the tool returned it to the agent
        task_result_inbox.unbind_run("turn-1")
        await jobs.on_turn_end(conversation_id="c-alice", profile="alice")
        return done

    asyncio.run(scenario())
    assert env.enqueued == []


def test_a_result_the_live_turn_did_not_collect_arrives_when_it_ends(env):
    env.pipeline = completing()

    async def scenario():
        task_result_inbox.bind_run("turn-1", "c-alice")
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        assert env.enqueued == []  # not while the turn runs
        task_result_inbox.unbind_run("turn-1")
        await jobs.on_turn_end(conversation_id="c-alice", profile="alice")
        await jobs.on_turn_end(conversation_id="c-alice", profile="alice")
        await jobs.boot_recover()

    asyncio.run(scenario())
    assert len(env.enqueued) == 1


def test_an_outcome_the_tool_is_waiting_for_is_not_also_injected(env):
    """The tool starts a job and long-polls it (``collect=True``). A job that
    stops to ask at once — even in a conversation with no bound turn — is the
    tool's to show; injecting it too would ask the user the same question
    twice."""
    async def pipeline(ctx):
        raise NeedsInput(Clarification(kind="unread", question="Go on without secret.pdf?",
                                       answer_keys={"confirm": "true"}), NEEDS_CONFIRMATION)

    env.pipeline = pipeline

    async def scenario():
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice", collect=True)
        asked = await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5, collect=True)
        await settle()
        assert env.enqueued == []
        jobs.mark_collected("alice", view.job_id)  # the tool showed it
        await jobs.on_turn_end(conversation_id="c-alice", profile="alice")
        return asked

    asked = asyncio.run(scenario())
    assert asked.status == NEEDS_CONFIRMATION
    assert env.enqueued == []


def test_a_collector_that_never_claims_is_covered_by_the_turn_end(env):
    """The hold only defers the report: if the tool fell over before it
    claimed the outcome, the turn's end reports it — once."""
    env.pipeline = completing()

    async def scenario():
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice", collect=True)
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5, collect=True)
        await settle()
        assert env.enqueued == []
        await jobs.on_turn_end(conversation_id="c-alice", profile="alice")
        await jobs.on_turn_end(conversation_id="c-alice", profile="alice")

    asyncio.run(scenario())
    assert len(env.enqueued) == 1


def test_a_question_is_relayed_and_its_answer_is_owed_to_nobody(env):
    async def pipeline(ctx):
        if not ctx.answers.get("edition"):
            raise NeedsInput(Clarification(kind="edition", question="Which edition of the Land Law?",
                                           answer_keys={"edition": "a fid from candidates"}))
        ctx.dossier.status = COMPLETE
        return ctx.dossier

    env.pipeline = pipeline

    async def scenario():
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        asked = list(env.enqueued)
        await jobs.continue_job(profile="alice", job_id=view.job_id, answers={"edition": "k7m2xq9a"})
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        return asked

    asked = asyncio.run(scenario())
    assert len(asked) == 1 and "Ask the user" in asked[0]["query"]
    assert 'continue_job="' in asked[0]["query"]
    # The question, then the result: two states, two turns, never a repeat.
    assert len(env.enqueued) == 2
    assert "Present these research results" in env.enqueued[1]["query"]


def test_a_failed_injection_is_retried_by_the_next_hook(env):
    env.pipeline = completing()
    env.fail_enqueue = 1

    async def scenario():
        done = await _run_to_end(env, conversation_id="c-alice")
        assert env.enqueued == []
        row = env.store.get("alice", done.job_id)
        assert row["delivered_rev"] < row["rev"]  # the claim was released
        await jobs.on_turn_end(conversation_id="c-alice", profile="alice")
        await jobs.on_turn_end(conversation_id="c-alice", profile="alice")

    asyncio.run(scenario())
    assert len(env.enqueued) == 1


def test_racing_claims_deliver_once(env):
    env.pipeline = completing()

    async def scenario():
        task_result_inbox.bind_run("turn-1", "c-alice")
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=5)
        await settle()
        task_result_inbox.unbind_run("turn-1")
        await asyncio.gather(
            jobs.on_turn_end(conversation_id="c-alice", profile="alice"),
            jobs.boot_recover(),
            jobs.boot_recover(),
        )

    asyncio.run(scenario())
    assert len(env.enqueued) == 1


def test_a_rest_job_is_never_injected_anywhere(env):
    env.pipeline = completing()

    async def scenario():
        await _run_to_end(env, conversation_id=None)
        await jobs.boot_recover()

    asyncio.run(scenario())
    assert env.enqueued == []


def test_a_restart_interrupts_the_job_tells_the_conversation_and_continue_resumes(env):
    box: dict = {}
    seen: list[dict] = []

    async def finish(ctx):
        ctx.dossier.status = COMPLETE
        return ctx.dossier

    async def pipeline(ctx):
        seen.append(dict(ctx.state))
        if ctx.state.get("read"):
            return await finish(ctx)
        return await gated(box, then=finish)(ctx)

    env.pipeline = pipeline

    async def before_restart():
        box["gate"] = asyncio.Event()
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=0.3)
        # The process dies: the loop cancels the task, nobody asked to cancel.
        jobs._runs[view.job_id].task.cancel()
        await settle()
        return view.job_id

    job_id = asyncio.run(before_restart())
    assert env.store.get("alice", job_id)["status"] == RUNNING  # left for the next boot
    jobs._reset_for_tests()

    async def after_restart():
        await jobs.boot_recover()
        interrupted = await asyncio.to_thread(jobs.get_job, "alice", job_id)
        await jobs.continue_job(profile="alice", job_id=job_id)
        done = await jobs.wait_job(profile="alice", job_id=job_id, timeout=5)
        await settle()
        return interrupted, done

    interrupted, done = asyncio.run(after_restart())
    assert interrupted.status == INTERRUPTED
    # Told once that it stopped, then once that it finished.
    assert [c["trigger_event"]["status"] for c in env.enqueued] == [INTERRUPTED, COMPLETE]
    assert "was cut short by a server restart" in env.enqueued[0]["query"]
    assert done.status == COMPLETE
    assert seen[-1] == {"read": ["a.pdf"]}  # resumed from the checkpoint


def test_a_stale_running_row_does_not_block_the_profile(env):
    """A row left running by a process that died before any boot sweep."""
    env.store.create(job_id="deadbeef0001", profile="alice", status=RUNNING, mode="analyze",
                     domain="general", question="old", conversation_id="c-alice")
    env.pipeline = completing()
    done = asyncio.run(_run_to_end(env))
    assert done.status == COMPLETE
    stale = env.store.get("alice", "deadbeef0001")
    assert stale["status"] == INTERRUPTED and stale["delivered_rev"] == stale["rev"]


def test_a_deleted_conversation_stops_its_job_without_a_word(env):
    box: dict = {}

    async def then(ctx):
        await ctx.save()
        raise AssertionError("the save should have noticed the row is gone")

    env.pipeline = gated(box, then=then)

    async def scenario():
        box["gate"] = asyncio.Event()
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=0.2)
        with env.provider.sync_engine().begin() as c:
            c.execute(text("DELETE FROM conversations WHERE id='c-alice'"))
        box["gate"].set()
        await settle()
        return view.job_id

    job_id = asyncio.run(scenario())
    assert env.store.get_by_id(job_id) is None
    assert env.enqueued == []
    assert not jobs.has_live_run("alice")


# ── activity panel ────────────────────────────────────────────────────────


def test_the_activity_panel_follows_the_job(env, monkeypatch):
    calls: list[tuple] = []

    class Activity:
        @classmethod
        async def start(cls, **kw):
            calls.append(("start", kw))
            return cls()

        def update_usage(self, tokens_in, tokens_out, budget):
            calls.append(("usage", tokens_in, tokens_out, budget))

        async def finish(self, **kw):
            calls.append(("finish", kw))

    from app.documents.research.context import ProgressSink

    class Sink(ProgressSink):
        def __init__(self, activity):
            super().__init__()
            self.activity = activity

    monkeypatch.setattr(jobs, "_activity_module", lambda: SimpleNamespace(
        ResearchActivity=Activity, ActivityProgressSink=Sink))
    env.pipeline = completing()
    asyncio.run(_run_to_end(env, conversation_id="c-alice"))
    start = calls[0]
    assert start[0] == "start" and start[1]["conversation_id"] == "c-alice"
    assert start[1]["profile"] == "alice" and start[1]["mode"] == "analyze"
    assert ("usage", 100, 20, jobs.DEFAULT_BUDGET) in calls
    assert calls[-1][0] == "finish" and calls[-1][1]["status"] == COMPLETE


# ── retention and purge ───────────────────────────────────────────────────


def test_retention_keeps_the_newest_and_never_a_running_or_waiting_job(env):
    store = env.store
    for i in range(55):
        store.create(job_id=f"job{i:03d}", profile="alice", status=COMPLETE, mode="analyze",
                     domain="general", question=f"q{i}")
    store.create(job_id="waiting", profile="alice", status=NEEDS_CONFIRMATION, mode="analyze",
                 domain="general", question="w")
    store.create(job_id="bobs", profile="bob", status=COMPLETE, mode="analyze", domain="general", question="b")
    with env.provider.sync_engine().begin() as c:
        c.execute(text("UPDATE document_research_jobs SET created_at = CAST(SUBSTR(id, 4) AS INTEGER) "
                       "WHERE id LIKE 'job%'"))
        c.execute(text("UPDATE document_research_jobs SET created_at = -1 WHERE id = 'waiting'"))
    old_dir = Path(jobs.artifacts_dir("alice", "job000"))
    old_dir.mkdir(parents=True)
    (old_dir / "table.csv").write_text("a,b\n", encoding="utf-8")

    env.pipeline = completing()
    asyncio.run(_run_to_end(env))  # starting a job prunes

    ids = {r["id"] for r in store.list("alice", limit=200)}
    assert "waiting" in ids
    assert "job000" not in ids and "job054" in ids
    assert len(ids) == 50 + 1  # the newest 50, plus the waiting one
    assert not old_dir.exists()
    assert store.get("bob", "bobs") is not None


def test_purge_deletes_the_profiles_jobs_and_artifacts_and_stops_a_running_one(env):
    box: dict = {}
    env.pipeline = gated(box)
    env.store.create(job_id="bobs", profile="bob", status=COMPLETE, mode="analyze", domain="general", question="b")

    async def scenario():
        box["gate"] = asyncio.Event()
        view = await jobs.start_job(profile="alice", spec=spec(), conversation_id="c-alice")
        await jobs.wait_job(profile="alice", job_id=view.job_id, timeout=0.2)
        art = Path(jobs.artifacts_dir("alice", view.job_id))
        art.mkdir(parents=True)
        (art / "table.md").write_text("|a|", encoding="utf-8")
        # The Documentation search engine purges from its own thread.
        removed = await asyncio.to_thread(jobs.purge_profile, "alice")
        await settle()
        return view.job_id, removed

    job_id, removed = asyncio.run(scenario())
    assert removed == 1
    assert env.store.get_by_id(job_id) is None
    assert not os.path.exists(jobs.artifacts_root("alice"))
    assert env.store.get("bob", "bobs") is not None
    assert env.enqueued == [] and not jobs.has_live_run("alice")


def test_cancel_profile_jobs_stops_only_that_profile(env):
    box: dict = {}
    env.pipeline = gated(box)

    async def scenario():
        box["gate"] = asyncio.Event()
        a = await jobs.start_job(profile="alice", spec=spec(), conversation_id=None)
        b = await jobs.start_job(profile="bob", spec=spec(), conversation_id=None)
        n = await jobs.cancel_profile_jobs("alice", "Documentation search was turned off")
        a_view = await asyncio.to_thread(jobs.get_job, "alice", a.job_id)
        b_view = await asyncio.to_thread(jobs.get_job, "bob", b.job_id)
        box["gate"].set()
        await settle()
        return n, a_view, b_view

    n, a_view, b_view = asyncio.run(scenario())
    assert n == 1
    assert a_view.status == CANCELLED and a_view.error == "Documentation search was turned off"
    assert b_view.status in (QUEUED, RUNNING)
    assert jobs.get_job("bob", b_view.job_id).status == COMPLETE


# ── the hooks, pinned as text ─────────────────────────────────────────────


def test_the_turn_end_hook_sits_after_the_unbind_and_before_the_task_flush():
    import app.agent.stream_runner as sr

    src = inspect.getsource(sr.run_agent_to_bus)
    block = src[src.rindex("\n    finally:"):]
    assert "research_jobs.on_turn_end(" in block
    assert block.index("unbind_run(run_id)") < block.index("research_jobs.on_turn_end(")
    assert block.index("bus.end_run") < block.index("research_jobs.on_turn_end(")
    assert block.index("research_jobs.on_turn_end(") < block.index("flush_origin_inbox")
    body = src[:src.rindex("\n    finally:")]
    assert body.index('"agent_activity": activity_snapshot') < body.index('"research_activity": research_snapshot')
    assert body.index("assistant_msg = await conversation_storage.add_message") < body.index(
        "research_activity.set_persist_target(conversation_id, assistant_msg_id)")


def test_boot_recovery_runs_after_the_channels_and_the_event_sweep():
    from app import server

    src = Path(server.__file__).read_text(encoding="utf-8")
    start = src.index("async def boot_storage_and_post_storage")
    block = src[start:src.index("\ndef _build_spa_components", start)]
    assert block.index("start_service()") < block.index("research_jobs.boot_recover()")
    assert block.index("start_all_enabled()") < block.index("research_jobs.boot_recover()")
    assert block.index("await sweep_undelivered()") < block.index("research_jobs.boot_recover()")
