"""The flow break as it reaches the UI: one bus frame, and offsets that survive.

A turn interrupted by a mid-turn message is watched as several bubbles but
persisted as ONE agent row — the model saw one continuous chain. What makes a
reload show the same thing the user watched is `metadata.mid_turn_breaks`:
offsets into that row's text and thinking steps marking where the flow was cut.

The offsets are taken as each break passes, not on the terminal chunk, because a
cancelled turn never sends one and its partial answer is persisted anyway.
"""

from __future__ import annotations

import asyncio
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


class _ScriptedAgent:
    """Replays a fixed chunk sequence — no LLM, no tools."""

    def __init__(self, chunks):
        self.chunks = chunks

    async def run(self, **kwargs):
        for c in self.chunks:
            yield c


def _thinking(step: int, call_id: str):
    return {"type": T.THINKING_ARTIFACT, "data": {
        "Step": step, "Call_Id": call_id, "Tool": "exec_shell", "Tool_Input": "{}",
    }}


def _text(s: str):
    return {"type": T.CONTENT, "data": s}


def _break(ids, step: int):
    return {"type": T.FLOW_BREAK, "data": {"message_ids": ids, "step": step}}


_DONE = {"type": T.DONE, "input_tokens": 1, "output_tokens": 1, "finish_reason": "stop"}


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "flow.db"))
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
    real_bus = sr.get_event_stream_bus()

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
    assert real_bus is not None  # sanity: the real one exists, we just bypass it
    return cs, published


async def _run(cs, chunks, conversation_id):
    await sr.run_agent_to_bus(
        cremind_agent=_ScriptedAgent(chunks),
        conversation_storage=cs,
        conversation_id=conversation_id,
        run_id=sr.make_run_id(conversation_id, kind="msg"),
        profile="p1",
        query="install it",
        history_messages=[],
        push_user_message=False,
        update_title_from_query=False,
    )


async def _conv(cs):
    conv = await cs.create_conversation(profile="p1", title="c", kind="chat")
    return conv["id"]


def _agent_row(rows):
    return next(r for r in rows if r["role"] == "agent")


def test_the_break_is_published_for_live_clients(tmp_path, monkeypatch):
    async def scenario():
        cs, published = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(cs, [
            _thinking(1, "c1"),
            _break(["m1"], 2),
            _text("Not yet."),
            _DONE,
        ], cid)

        frames = [(t, d) for t, d in published if t == "flow_break"]
        assert len(frames) == 1
        assert frames[0][1] == {"message_ids": ["m1"], "step": 2}

    asyncio.run(scenario())


def test_the_offsets_cut_where_the_break_happened(tmp_path, monkeypatch):
    """Everything before the break belongs to the bubble the user was watching;
    the reply that follows opens the next one."""
    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(cs, [
            _thinking(1, "c1"),
            _thinking(2, "c2"),
            _break(["m1"], 3),
            _text("Not yet — two of four done.\n\n"),
            _thinking(3, "c3"),
            _text("Done."),
            _DONE,
        ], cid)

        row = _agent_row(await cs.get_messages(cid))
        breaks = row["metadata"]["mid_turn_breaks"]
        assert len(breaks) == 1
        # Two steps and no text had been produced when the message landed.
        assert breaks[0]["thinking_offset"] == 2
        assert breaks[0]["content_offset"] == 0
        assert breaks[0]["message_ids"] == ["m1"]
        assert breaks[0]["step"] == 3
        # The reply is on the far side of the cut, with the rest of the work.
        assert row["content"].startswith("Not yet")
        assert len(row["thinking_steps"]) == 3

    asyncio.run(scenario())


def test_text_before_a_break_stays_with_the_earlier_bubble(tmp_path, monkeypatch):
    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(cs, [
            _text("Working on it. "),
            _break(["m1"], 2),
            _text("Not yet."),
            _DONE,
        ], cid)

        row = _agent_row(await cs.get_messages(cid))
        brk = row["metadata"]["mid_turn_breaks"][0]
        assert brk["content_offset"] == len("Working on it. ")
        assert row["content"][:brk["content_offset"]] == "Working on it. "

    asyncio.run(scenario())


def test_two_interruptions_in_one_turn(tmp_path, monkeypatch):
    async def scenario():
        cs, published = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(cs, [
            _thinking(1, "c1"),
            _break(["m1"], 2),
            _text("One moment. "),
            _thinking(2, "c2"),
            _break(["m2", "m3"], 3),
            _text("Still going. "),
            _thinking(3, "c3"),
            _text("Done."),
            _DONE,
        ], cid)

        row = _agent_row(await cs.get_messages(cid))
        breaks = row["metadata"]["mid_turn_breaks"]
        assert [b["thinking_offset"] for b in breaks] == [1, 2]
        assert [b["content_offset"] for b in breaks] == [0, len("One moment. ")]
        assert breaks[1]["message_ids"] == ["m2", "m3"]
        assert len([t for t, _ in published if t == "flow_break"]) == 2

    asyncio.run(scenario())


def test_the_reply_is_fenced_off_as_a_segment_of_its_own(tmp_path, monkeypatch):
    """The agent brackets a spoken reply with two breaks, so it renders as its
    own message instead of the final answer growing out of it. The offsets
    between the pair must isolate exactly the reply text."""
    async def scenario():
        cs, published = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        ack = "Not yet — two of four done.\n\n"
        await _run(cs, [
            _thinking(1, "c1"),
            _break(["m1"], 2),          # the message arrived
            _text(ack),                 # ...the reply to it
            _break([], 2),              # ...and the reply is closed off
            _thinking(2, "c2"),
            _text("All four are installed."),
            _DONE,
        ], cid)

        row = _agent_row(await cs.get_messages(cid))
        breaks = row["metadata"]["mid_turn_breaks"]
        assert len(breaks) == 2
        # The slice between the two breaks is the reply, and nothing else.
        assert row["content"][breaks[0]["content_offset"]:breaks[1]["content_offset"]] == ack
        # It carries no thinking of its own — the work resumes after it.
        assert breaks[0]["thinking_offset"] == breaks[1]["thinking_offset"] == 1
        # Only the first names a row; the closing break has none to interleave.
        assert breaks[0]["message_ids"] == ["m1"]
        assert breaks[1]["message_ids"] == []
        assert [d["message_ids"] for t, d in published if t == "flow_break"] == [["m1"], []]

    asyncio.run(scenario())


def test_a_turn_with_no_interruption_stamps_nothing(tmp_path, monkeypatch):
    """The key stays absent so the reload splitter leaves ordinary turns alone."""
    async def scenario():
        cs, published = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(cs, [_thinking(1, "c1"), _text("Done."), _DONE], cid)

        row = _agent_row(await cs.get_messages(cid))
        assert "mid_turn_breaks" not in (row["metadata"] or {})
        assert not [t for t, _ in published if t == "flow_break"]

    asyncio.run(scenario())


def test_breaks_survive_a_turn_that_never_completes(tmp_path, monkeypatch):
    """No DONE chunk — cancelled or crashed. The partial answer is persisted, so
    its segments have to be too; this is why the offsets are collected as they
    happen rather than carried on the terminal chunk."""
    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)

        class _Dies:
            async def run(self, **kwargs):
                yield _thinking(1, "c1")
                yield _break(["m1"], 2)
                yield _text("Not yet.")
                raise RuntimeError("provider died")

        await sr.run_agent_to_bus(
            cremind_agent=_Dies(),
            conversation_storage=cs,
            conversation_id=cid,
            run_id=sr.make_run_id(cid, kind="msg"),
            profile="p1",
            query="install it",
            history_messages=[],
            push_user_message=False,
            update_title_from_query=False,
        )

        row = _agent_row(await cs.get_messages(cid))
        assert row["metadata"]["mid_turn_breaks"][0]["message_ids"] == ["m1"]

    asyncio.run(scenario())


# ── the room hears it as it happens ─────────────────────────────────────────


def _seat_conv(cs, gid="g-1"):
    return cs.create_conversation(
        profile="p1",
        context_id=f"group:{gid}:p1",
        title="Group: Ops",
        kind="group_chat",
    )


def test_a_seat_posts_each_closed_segment_as_the_break_passes(tmp_path, monkeypatch):
    """A break is where a reply to an interruption ends, so it is where the room
    hears it. Waiting for the turn to finish would delay it by exactly the work
    the sender interrupted."""
    calls: list[dict] = []

    async def fake_segment(**kwargs):
        calls.append(kwargs)
        return []

    import app.groups.hooks as hooks
    monkeypatch.setattr(hooks, "on_shadow_turn_segment", fake_segment)

    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = (await _seat_conv(cs))["id"]
        await _run(cs, [
            _text("Not yet."), _break(["m1"], 2), _text("Done."), _DONE,
        ], cid)

        assert len(calls) == 1
        call = calls[0]
        assert call["profile"] == "p1"
        assert call["context_id"] == f"group:g-1:p1"
        # The text so far, and the break that closes it — the hook cuts there.
        assert call["raw_text"] == "Not yet."
        assert call["mid_turn_breaks"][0]["content_offset"] == len("Not yet.")

    asyncio.run(scenario())


def test_the_run_id_rides_the_row_so_the_boot_sweep_knows_what_was_said(
    tmp_path, monkeypatch,
):
    """A seat's interim posts are owned by the run until the turn's message
    exists. If a crash strands the turn between the two, the sweep has to be
    able to recognise them — it reads the run id off the row it is re-posting."""
    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(cs, [_text("Not yet."), _break(["m1"], 2), _text("Done."), _DONE], cid)

        row = _agent_row(await cs.get_messages(cid))
        assert row["metadata"]["run_id"].startswith("msg:")

    asyncio.run(scenario())


def test_an_ordinary_conversation_posts_nothing_to_any_room(tmp_path, monkeypatch):
    """Not every interruption happens in a group."""
    calls: list[dict] = []

    async def fake_segment(**kwargs):
        calls.append(kwargs)
        return []

    import app.groups.hooks as hooks
    monkeypatch.setattr(hooks, "on_shadow_turn_segment", fake_segment)

    async def scenario():
        cs, _pub = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _run(cs, [_text("Not yet."), _break(["m1"], 2), _DONE], cid)
        assert calls == []

    asyncio.run(scenario())
