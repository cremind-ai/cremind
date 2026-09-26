"""The turn clock: what "First token: 4.8s" is measured from, and what survives.

Latency used to be timed entirely in the browser, against a baseline the client
set when the FIRST frame created the assistant bubble. For a turn whose first
frame was a token that made the baseline and the milestone the same instant, so
the bubble reported "First token: 0ms" and a total that left out everything
before that frame. And because nothing was stored, reopening the conversation
showed no timings at all.

So the runner times the turn itself: one monotonic clock started when the
request arrived, every step stamped against it, and the milestones persisted on
the row — which is what makes a reloaded bubble able to say the same thing.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

import app.agent.stream_runner as sr  # noqa: E402
from app.constants import ChatCompletionTypeEnum as T  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages", "event_runs")

# Long enough that a slow machine can't confuse it with zero, short enough that
# the suite doesn't notice.
_PAUSE = 0.06
_PAUSE_MS = int(_PAUSE * 1000 * 0.8)  # leave room for a coarse clock


class _SlowAgent:
    """Replays chunks, pausing before each one that asks for it.

    The pauses are the point: they stand in for the model taking its time, so
    the milestones have something to measure.
    """

    def __init__(self, chunks):
        self.chunks = chunks

    async def run(self, **kwargs):
        for pause, chunk in self.chunks:
            if pause:
                await asyncio.sleep(pause)
            yield chunk


def _thinking(step: int, call_id: str):
    return {"type": T.THINKING_ARTIFACT, "data": {
        "Step": step, "Call_Id": call_id, "Tool": "exec_shell", "Tool_Input": "{}",
    }}


def _text(s: str):
    return {"type": T.CONTENT, "data": s}


_DONE = {"type": T.DONE, "input_tokens": 1, "output_tokens": 1, "finish_reason": "stop"}


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "latency.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('pid','p1',0,0)"
        ))
    cs = ConversationStorage(provider)
    cs._initialized = True

    import app.storage as storage_pkg
    monkeypatch.setattr(storage_pkg, "get_conversation_storage", lambda *a, **k: cs)

    published: list[tuple] = []

    class _Bus:
        async def start_run(self, *a, **k):
            return None

        async def end_run(self, *a, **k):
            return None

        def is_active(self, *a, **k):
            return False

        async def publish(self, conversation_id, event_type, data=None):
            published.append((event_type, data))

    monkeypatch.setattr(sr, "get_event_stream_bus", lambda: _Bus())
    return cs, published


async def _run(cs, chunks, conversation_id, queued_at=None):
    await sr.run_agent_to_bus(
        cremind_agent=_SlowAgent(chunks),
        conversation_storage=cs,
        conversation_id=conversation_id,
        run_id=sr.make_run_id(conversation_id, kind="msg"),
        profile="p1",
        query="how long does this take?",
        history_messages=[],
        push_user_message=False,
        update_title_from_query=False,
        queued_at=queued_at,
    )


async def _conv(cs):
    conv = await cs.create_conversation(profile="p1", title="c", kind="chat")
    return conv["id"]


def _agent_row(rows):
    return next(r for r in rows if r["role"] == "agent")


def test_first_token_is_measured_from_the_turn_not_from_itself(tmp_path, monkeypatch):
    """The regression: a turn whose first frame is a token still waited for it."""
    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        # No steps at all — the shape that produced "First token: 0ms".
        await _run(cs, [(_PAUSE, _text("Hello! How can I help today?")), (0, _DONE)], cid)

        latency = _agent_row(await cs.get_messages(cid))["metadata"]["latency"]
        assert latency["first_token_ms"] >= _PAUSE_MS
        assert latency["total_ms"] >= latency["first_token_ms"]
        # Nothing called a tool, so there is no first step to report.
        assert latency["first_step_ms"] is None

    asyncio.run(scenario())


def test_a_non_streaming_provider_still_reports_a_first_token(tmp_path, monkeypatch):
    """Its whole answer arrives on the terminal chunk; that is its first token."""
    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(cs, [(_PAUSE, {**_DONE, "data": "All of it at once."})], cid)

        latency = _agent_row(await cs.get_messages(cid))["metadata"]["latency"]
        assert latency["first_token_ms"] >= _PAUSE_MS

    asyncio.run(scenario())


def test_every_step_carries_where_it_fell_in_the_turn(tmp_path, monkeypatch):
    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(cs, [
            (_PAUSE, _thinking(1, "c1")),
            (_PAUSE, _thinking(2, "c2")),
            (_PAUSE, _text("Done.")),
            (0, _DONE),
        ], cid)

        row = _agent_row(await cs.get_messages(cid))
        elapsed = [s["elapsed_ms"] for s in row["thinking_steps"]]
        assert len(elapsed) == 2
        # Stamped against one clock, so they only ever move forward — which is
        # what lets the timeline show each step as a delta from the one before.
        assert elapsed[0] >= _PAUSE_MS
        assert elapsed[1] - elapsed[0] >= _PAUSE_MS

        latency = row["metadata"]["latency"]
        assert latency["first_step_ms"] == elapsed[0]
        assert latency["first_token_ms"] >= elapsed[1]

    asyncio.run(scenario())


def test_the_wait_in_the_queue_counts(tmp_path, monkeypatch):
    """A message that sat behind another turn was still a message being waited on."""
    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(
            cs, [(0, _text("Sorry for the wait.")), (0, _DONE)], cid,
            queued_at=time.monotonic() - 2.0,
        )

        latency = _agent_row(await cs.get_messages(cid))["metadata"]["latency"]
        assert latency["first_token_ms"] >= 2000
        assert latency["total_ms"] >= 2000

    asyncio.run(scenario())


def test_live_clients_get_the_same_numbers_on_the_frames(tmp_path, monkeypatch):
    """Live and reloaded must agree, or the bubble changes its story on refresh."""
    async def scenario():
        cs, published = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(cs, [
            (_PAUSE, _thinking(1, "c1")),
            (_PAUSE, _text("Done.")),
            (0, _DONE),
        ], cid)

        row = _agent_row(await cs.get_messages(cid))

        thinking = [d for t, d in published if t == "thinking"]
        assert len(thinking) == 1
        assert thinking[0]["Elapsed_Ms"] == row["thinking_steps"][0]["elapsed_ms"]
        # The agent's own chunk payload is left alone; the stamp rides a copy.
        assert thinking[0]["Tool"] == "exec_shell"

        complete = next(d for t, d in published if t == "complete")
        assert complete["latency"] == row["metadata"]["latency"]

    asyncio.run(scenario())


def test_a_turn_that_blew_up_still_says_how_long_it_ran(tmp_path, monkeypatch):
    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)

        class _Exploding:
            async def run(self, **kwargs):
                await asyncio.sleep(_PAUSE)
                raise RuntimeError("boom")
                yield  # pragma: no cover - makes this an async generator

        await sr.run_agent_to_bus(
            cremind_agent=_Exploding(),
            conversation_storage=cs,
            conversation_id=cid,
            run_id=sr.make_run_id(cid, kind="msg"),
            profile="p1",
            query="go",
            history_messages=[],
            push_user_message=False,
            update_title_from_query=False,
        )

        latency = _agent_row(await cs.get_messages(cid))["metadata"]["latency"]
        assert latency["total_ms"] >= _PAUSE_MS
        # It never got as far as speaking or calling anything.
        assert latency["first_token_ms"] is None
        assert latency["first_step_ms"] is None

    asyncio.run(scenario())


@pytest.fixture(autouse=True)
def _workspaces_in_tmp(tmp_path, monkeypatch):
    """Each profile's working directory resolves under the workspaces root;
    keep it in this test's tmp dir, never the developer's ~/.cremind."""
    monkeypatch.setenv("CREMIND_WORKSPACES_DIR", str(tmp_path / "workspaces"))
