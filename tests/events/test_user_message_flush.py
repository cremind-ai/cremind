"""Persisting, parking and flushing a user message that arrived mid-turn.

The promise is that a sent message is always answered exactly once. Three things
carry it, and this file pins all three against a real SQLite store:

* the row is persisted BEFORE the park, so the message survives whatever happens
  next — including losing the race to the turn's end;
* the flush RELEASES the rows before it enqueues anything, so an enqueue that
  fails degrades to "answered on the user's next turn", never to a lost message;
* the follow-up turn does not repeat the text — the released rows are already the
  newest user messages in the history it is handed, so repeating would double-feed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402

from app.events import task_result_inbox  # noqa: E402
from app.events import user_message_delivery as umd  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages", "event_runs")
_RUN = "msg:conv-1:abc"


class _Enqueued(list):
    async def __call__(self, **kwargs):
        self.append(kwargs)


class _Forwards(list):
    async def __call__(self, *args, **kwargs):
        self.append(args)


@pytest.fixture(autouse=True)
def _clean_inbox():
    task_result_inbox.clear_all()
    yield
    task_result_inbox.clear_all()


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "usermsg.db"))
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
    from app.events import runner as event_runner
    monkeypatch.setattr(event_runner, "get_conversation_storage", lambda: cs)

    # The bus is not under test; swallow the publish.
    class _Bus:
        published: list = []

        async def publish(self, *a, **k):
            _Bus.published.append((a, k))

    _Bus.published = []
    from app.events import stream_bus
    monkeypatch.setattr(stream_bus, "get_event_stream_bus", lambda: _Bus())

    forwards = _Forwards()
    from app.events import run_dispatcher
    monkeypatch.setattr(run_dispatcher, "_maybe_forward_to_channel", forwards)

    queued = _Enqueued()
    from app.events import queue as event_queue
    monkeypatch.setattr(event_queue, "enqueue_user_message", queued)

    return cs, queued, forwards, _Bus


async def _conv(cs, *, kind="chat"):
    conv = await cs.create_conversation(profile="p1", title="c", kind=kind)
    return conv["id"]


def _state(row: dict) -> str | None:
    return ((row.get("metadata") or {}).get("mid_turn") or {}).get("state")


# ── parking ─────────────────────────────────────────────────────────────────


def test_an_idle_conversation_is_left_completely_alone(tmp_path, monkeypatch):
    """No binding → no row, no frame, nothing: the caller's normal path runs."""
    async def scenario():
        cs, _queued, _fw, bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)

        out = await umd.try_park_user_message(
            conversation_id=cid, profile="p1", query="hello",
        )

        assert out is None
        assert await cs.get_messages(cid) == []
        assert bus.published == []

    asyncio.run(scenario())


def test_a_mid_turn_message_is_persisted_parked_and_announced(tmp_path, monkeypatch):
    async def scenario():
        cs, _queued, _fw, bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        task_result_inbox.bind_run(_RUN, cid)

        out = await umd.try_park_user_message(
            conversation_id=cid, profile="p1", query="use staging",
        )

        assert out is not None and out.injected and out.run_id == _RUN
        rows = await cs.get_messages(cid)
        assert len(rows) == 1
        assert rows[0]["role"] == "user" and rows[0]["content"] == "use staging"
        assert _state(rows[0]) == "pending"
        # Announced on the same frame a normal turn uses, flagged as injected.
        (args, _kw) = bus.published[0]
        assert args[1] == "user_message" and args[2]["injected"] is True
        assert args[2]["id"] == out.message_id
        # And it is queued for the running turn.
        assert task_result_inbox.has_unconsumed_user_messages(cid)

    asyncio.run(scenario())


def test_losing_the_race_releases_the_row_instead_of_dropping_it(
    tmp_path, monkeypatch,
):
    """The turn ends between the pre-check and the park: the row still exists,
    so it must be released for the caller to run as its own turn."""
    async def scenario():
        cs, _queued, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        task_result_inbox.bind_run(_RUN, cid)

        real_park = task_result_inbox.park_user_message_if_bound

        def _park_after_turn_ends(conversation_id, payload):
            task_result_inbox.unbind_run(_RUN)      # the turn ends right here
            return real_park(conversation_id, payload)

        monkeypatch.setattr(
            task_result_inbox, "park_user_message_if_bound", _park_after_turn_ends,
        )

        out = await umd.try_park_user_message(
            conversation_id=cid, profile="p1", query="too late",
        )

        assert out is not None and not out.injected
        rows = await cs.get_messages(cid)
        assert len(rows) == 1 and _state(rows[0]) == "released"
        assert not task_result_inbox.has_unconsumed_user_messages(cid)

    asyncio.run(scenario())


def test_the_agent_text_resolves_tokens_and_notes_attachments(tmp_path, monkeypatch):
    async def scenario():
        cs, _queued, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        task_result_inbox.bind_run(_RUN, cid)

        out = await umd.try_park_user_message(
            conversation_id=cid, profile="p1", query="read this",
            attachments=[{"name": "a.txt", "path": "/tmp/up/a.txt"}],
        )

        assert out is not None and "/tmp/up/a.txt" in out.agent_text
        # The persisted row keeps what the user typed, not the agent's rendering.
        rows = await cs.get_messages(cid)
        assert rows[0]["content"] == "read this"

    asyncio.run(scenario())


# ── flushing ────────────────────────────────────────────────────────────────


async def _park_two(cs, cid):
    task_result_inbox.bind_run(_RUN, cid)
    a = await umd.try_park_user_message(
        conversation_id=cid, profile="p1", query="first extra",
    )
    b = await umd.try_park_user_message(
        conversation_id=cid, profile="p1", query="second extra",
    )
    return a, b


def test_flush_releases_the_rows_then_queues_one_turn(tmp_path, monkeypatch):
    async def scenario():
        cs, queued, forwards, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _park_two(cs, cid)

        assert await umd.flush_user_inbox(conversation_id=cid, profile="p1")

        rows = await cs.get_messages(cid)
        assert [_state(r) for r in rows] == ["released", "released"]
        # ONE follow-up turn for both messages, not one each.
        assert len(queued) == 1
        item = queued[0]
        assert item["push_user_message"] is False       # rows already exist
        assert item["update_title_from_query"] is False
        # A channel-backed origin needs a forwarder for the new run.
        assert len(forwards) == 1

    asyncio.run(scenario())


def test_the_followup_prompt_does_not_repeat_the_message_text(tmp_path, monkeypatch):
    """The released rows are already in the history it is handed; repeating the
    text would feed the model the same words twice."""
    async def scenario():
        cs, queued, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _park_two(cs, cid)

        await umd.flush_user_inbox(conversation_id=cid, profile="p1")

        query = queued[0]["query"]
        assert "first extra" not in query and "second extra" not in query
        assert "2 messages" in query
        # ...and the history it carries DOES contain them.
        contents = [m.get("content") for m in queued[0]["history_messages"]]
        assert "first extra" in contents and "second extra" in contents

    asyncio.run(scenario())


def test_flush_is_a_no_op_when_the_turn_absorbed_everything(tmp_path, monkeypatch):
    async def scenario():
        cs, queued, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _park_two(cs, cid)
        task_result_inbox.drain_user_messages(_RUN)
        task_result_inbox.commit_user_messages(cid)      # the turn's trace persisted

        assert not await umd.flush_user_inbox(conversation_id=cid, profile="p1")
        assert queued == []

    asyncio.run(scenario())


def test_a_message_injected_but_uncommitted_is_flushed(tmp_path, monkeypatch):
    """Cancelled or errored turn: drained, but no trace persisted, so the
    delivery never became final and must be redone."""
    async def scenario():
        cs, queued, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _park_two(cs, cid)
        task_result_inbox.drain_user_messages(_RUN)       # handed over, uncommitted

        assert await umd.flush_user_inbox(conversation_id=cid, profile="p1")
        assert len(queued) == 1

    asyncio.run(scenario())


def test_an_enqueue_failure_still_leaves_the_rows_visible(tmp_path, monkeypatch):
    """Degrades to "answered next turn", never to a lost message."""
    async def scenario():
        cs, _queued, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _park_two(cs, cid)

        async def _boom(**kwargs):
            raise RuntimeError("queue down")

        from app.events import queue as event_queue
        monkeypatch.setattr(event_queue, "enqueue_user_message", _boom)

        assert not await umd.flush_user_inbox(conversation_id=cid, profile="p1")

        rows = await cs.get_messages(cid)
        assert [_state(r) for r in rows] == ["released", "released"]

    asyncio.run(scenario())


def test_plan_mode_is_not_restarted_by_a_continuation(tmp_path, monkeypatch):
    async def scenario():
        cs, queued, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        task_result_inbox.bind_run(_RUN, cid)
        await umd.try_park_user_message(
            conversation_id=cid, profile="p1", query="hi", mode="plan",
        )

        await umd.flush_user_inbox(conversation_id=cid, profile="p1")

        assert queued[0]["mode"] == "reasoning"

    asyncio.run(scenario())


def test_an_event_run_reply_keeps_its_run_flags(tmp_path, monkeypatch):
    async def scenario():
        cs, queued, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs, kind="event_run")
        task_result_inbox.bind_run(_RUN, cid)

        class _Store:
            async def get_by_conversation(self, conversation_id):
                return {"id": "run-7"}

            async def update_status(self, *a, **k):
                return None

        import app.storage as storage_pkg
        monkeypatch.setattr(storage_pkg, "get_event_run_storage", lambda *a, **k: _Store())

        await umd.try_park_user_message(
            conversation_id=cid, profile="p1", query="the answer is 4",
            event_run=True,
        )
        await umd.flush_user_inbox(conversation_id=cid, profile="p1")

        assert queued[0]["event_run"] is True
        assert queued[0]["event_run_id"] == "run-7"

    asyncio.run(scenario())


# ── boot sweep ──────────────────────────────────────────────────────────────


def test_the_boot_sweep_rescues_rows_a_crash_stranded(tmp_path, monkeypatch):
    """In-memory parking is lost on restart; the rows would otherwise stay
    'pending' — invisible to history — forever."""
    async def scenario():
        cs, queued, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await _park_two(cs, cid)
        task_result_inbox.clear_all()                 # the crash

        swept = await umd.sweep_stranded_mid_turn_messages()

        assert swept == 2
        rows = await cs.get_messages(cid)
        assert [_state(r) for r in rows] == ["released", "released"]
        assert len(queued) == 1                        # one turn answers both

    asyncio.run(scenario())


def test_the_boot_sweep_is_a_no_op_on_a_clean_boot(tmp_path, monkeypatch):
    async def scenario():
        cs, queued, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        await cs.add_message(conversation_id=cid, role="user", content="plain")

        assert await umd.sweep_stranded_mid_turn_messages() == 0
        assert queued == []

    asyncio.run(scenario())


# ── "was this for me?", answered before the pause ───────────────────────────


def test_a_routed_room_post_is_marked_addressed(tmp_path, monkeypatch):
    """The room decides who a post is for and the row records it. Read at the
    pause, that is what tells "the agent declined to answer me" apart from "the
    agent was never asked" — the two need different handling and look identical
    otherwise."""
    async def scenario():
        cs, _q, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        task_result_inbox.bind_run(_RUN, cid)

        await umd.try_park_user_message(
            conversation_id=cid, profile="p1", query="Operator (user): status?",
            user_message_metadata={"group": {"group_id": "g", "routed_away": False}},
        )

        (payload,) = task_result_inbox.take_unconsumed_user_messages(cid)
        assert payload["addressed"] is True

    asyncio.run(scenario())


def test_a_post_routed_elsewhere_is_not_marked_addressed(tmp_path, monkeypatch):
    """It reaches the seat only as context. Insisting on an answer to somebody
    else's question is exactly the chorus the room design avoids."""
    async def scenario():
        cs, _q, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        task_result_inbox.bind_run(_RUN, cid)

        await umd.try_park_user_message(
            conversation_id=cid, profile="p1", query="Operator (user): Mia, status?",
            user_message_metadata={"group": {"group_id": "g", "routed_away": True}},
        )

        (payload,) = task_result_inbox.take_unconsumed_user_messages(cid)
        assert payload["addressed"] is False

    asyncio.run(scenario())


def test_a_platform_group_mention_is_addressed(tmp_path, monkeypatch):
    async def scenario():
        cs, _q, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        task_result_inbox.bind_run(_RUN, cid)

        for mentioned, expected in ((True, True), (False, False)):
            task_result_inbox.clear_all()
            task_result_inbox.bind_run(_RUN, cid)
            await umd.try_park_user_message(
                conversation_id=cid, profile="p1", query="Hà: @Sammie status?",
                user_message_metadata={
                    "channel_group": {"group_id": "g", "mentioned": mentioned},
                },
            )
            (payload,) = task_result_inbox.take_unconsumed_user_messages(cid)
            assert payload["addressed"] is expected

    asyncio.run(scenario())


def test_a_one_to_one_message_carries_no_room_verdict(tmp_path, monkeypatch):
    """There is no router in a DM, and nothing reads the flag there — the
    insisting retry is a room-only path."""
    async def scenario():
        cs, _q, _fw, _bus = _setup(tmp_path, monkeypatch)
        cid = await _conv(cs)
        task_result_inbox.bind_run(_RUN, cid)

        await umd.try_park_user_message(
            conversation_id=cid, profile="p1", query="actually, stop",
        )

        (payload,) = task_result_inbox.take_unconsumed_user_messages(cid)
        assert payload["addressed"] is False

    asyncio.run(scenario())
