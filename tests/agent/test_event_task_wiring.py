"""The two seams that join the halves of the event-task feature.

Everything else is covered by the storage/dispatch/delivery suites; these are
the joins that would fail silently:

1. ``run_agent_to_bus``'s terminal finalize must invoke delivery — and it must
   do so for a run that parked as *pending* and only finished on a LATER turn,
   which arrives through a completely different call stack (a human reply).
2. ``enqueue_user_message`` must carry ``trigger_event`` through to
   ``run_agent_to_bus``. Without it the continuation lands as a plain user
   message, ``triggered_by_event`` stays False, and the registration gate
   silently fails OPEN — a standing automation would be allowed on a turn that
   should only accept one-shot tasks.
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
from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.event_run_storage import EventRunStorage  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages", "event_runs")


class _StubAgent:
    """Answers with one line of text and stops — no tools, no LLM.

    ``asks`` reproduces a ``request_user_input`` call: the real tool records the
    question in ``run_state`` under the turn's stream run id (from the context
    var) and the loop then ends the turn, which is what parks the run.
    """

    def __init__(self, answer: str = "CI passed on abc123.", asks: str | None = None) -> None:
        self.answer = answer
        self.asks = asks
        self.seen: list[dict] = []

    async def run(self, **kwargs):
        self.seen.append(kwargs)
        if self.asks:
            from app.agent.executor import current_task_id_var
            from app.events import run_state
            run_state.mark_pending(current_task_id_var.get(), self.asks)
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": self.asks}
        else:
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": self.answer}
        yield {
            "type": ChatCompletionTypeEnum.DONE,
            "input_tokens": 1, "output_tokens": 1, "finish_reason": "stop",
        }


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "wiring.db"))
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
    ers = EventRunStorage(provider)

    import app.storage as storage_pkg
    monkeypatch.setattr(storage_pkg, "get_event_run_storage", lambda *a, **k: ers)
    monkeypatch.setattr(storage_pkg, "get_conversation_storage", lambda *a, **k: cs)

    delivered: list[dict] = []

    async def _on_terminal(**kwargs):
        delivered.append(kwargs)
        return "delivered"

    monkeypatch.setattr(
        "app.events.event_task_delivery.on_run_terminal", _on_terminal, raising=False,
    )

    notified: list[dict] = []
    monkeypatch.setattr(
        sr, "_push_event_run_notification", lambda **kw: notified.append(kw),
    )
    return cs, ers, delivered, notified


async def _run_turn(cs, agent, conversation_id, *, event_run_id, **kw):
    """One real ``run_agent_to_bus`` turn against the given stub agent."""
    run_id = sr.make_run_id(conversation_id, kind="event")
    await sr.run_agent_to_bus(
        cremind_agent=agent,
        conversation_storage=cs,
        conversation_id=conversation_id,
        run_id=run_id,
        profile="p1",
        query="do the thing",
        history_messages=[],
        push_user_message=False,
        publish_notification=True,
        update_title_from_query=False,
        event_run_id=event_run_id,
        event_run=True,
        **kw,
    )


# ── seam 1: the terminal hook ───────────────────────────────────────────────


def test_a_finished_run_reaches_the_delivery_hook(tmp_path, monkeypatch):
    cs, ers, delivered, notified = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
        created = await ers.create(
            profile="p1", source_kind="skill_event", subscription_id="s1",
            conversation_id=conv["id"], label="L", action="a",
            origin_conversation_id="origin", deliver_to_origin=True,
        )
        await _run_turn(cs, _StubAgent(), conv["id"], event_run_id=created["run"]["id"])
        return created["run"]["id"]

    run_id = asyncio.run(_run())
    assert len(delivered) == 1
    assert delivered[0]["event_run_id"] == run_id
    assert delivered[0]["status"] == "completed"
    # The run's final answer — not a placeholder — is what gets handed over.
    assert "CI passed on abc123." in delivered[0]["final_text"]


def test_a_delivered_task_raises_no_second_notification(tmp_path, monkeypatch):
    """The origin conversation notifies from its own turn; two would read as a dupe."""
    cs, ers, _, notified = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
        created = await ers.create(
            profile="p1", source_kind="skill_event", subscription_id="s1",
            conversation_id=conv["id"], label="L", action="a",
            origin_conversation_id="origin", deliver_to_origin=True,
        )
        await _run_turn(cs, _StubAgent(), conv["id"], event_run_id=created["run"]["id"])

    asyncio.run(_run())
    assert notified == []


def test_an_ordinary_event_run_still_notifies(tmp_path, monkeypatch):
    """Non-task runs are unaffected — they have no other way to surface."""
    cs, ers, delivered, notified = _setup(tmp_path, monkeypatch)

    async def _not_task(**kwargs):
        return "not_task"
    monkeypatch.setattr(
        "app.events.event_task_delivery.on_run_terminal", _not_task, raising=False,
    )

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
        created = await ers.create(
            profile="p1", source_kind="schedule", subscription_id="s1",
            conversation_id=conv["id"], label="L", action="a",
        )
        await _run_turn(cs, _StubAgent(), conv["id"], event_run_id=created["run"]["id"])

    asyncio.run(_run())
    assert len(notified) == 1


def test_a_pending_run_is_not_delivered_until_it_actually_finishes(tmp_path, monkeypatch):
    """The park-then-answer path: delivery must wait for the LATER terminal turn.

    This is why the hook lives in the stream runner and not in the dispatcher —
    the finishing turn is started by a human reply, from a different call stack.
    """
    cs, ers, delivered, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
        created = await ers.create(
            profile="p1", source_kind="skill_event", subscription_id="s1",
            conversation_id=conv["id"], label="L", action="a",
            origin_conversation_id="origin", deliver_to_origin=True,
        )
        rid = created["run"]["id"]

        # Turn 1: the agent asked a question, so the run parks as pending.
        asking = _StubAgent(asks="Which environment should I deploy to?")
        await _run_turn(cs, asking, conv["id"], event_run_id=rid)
        parked = await ers.get(rid)
        after_park = list(delivered)

        # Turn 2, started by a human reply: no question this time → terminal.
        await _run_turn(cs, _StubAgent("Deployed to staging."), conv["id"], event_run_id=rid)
        return parked, after_park, await ers.get(rid)

    parked, after_park, run = asyncio.run(_run())
    assert parked["status"] == "pending"
    assert after_park == []                    # parked, nothing handed over yet
    assert len(delivered) == 1                 # ...and exactly once afterwards
    assert delivered[0]["status"] == "completed"
    assert "Deployed to staging." in delivered[0]["final_text"]
    assert run["status"] == "completed"


# ── seam 2: the queue carries the trigger ───────────────────────────────────


def test_the_queue_carries_trigger_event_through_to_the_agent(tmp_path, monkeypatch):
    """Without this the gate fails OPEN on continuation turns (see module docs)."""
    cs, _, _, _ = _setup(tmp_path, monkeypatch)

    seen: dict = {}

    async def _capture(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(sr, "run_agent_to_bus", _capture)
    monkeypatch.setattr("app.agent.stream_runner.run_agent_to_bus", _capture)

    from app.events import queue as event_queue
    from app.events import runner as event_runner
    monkeypatch.setattr(event_runner, "get_conversation_storage", lambda: cs)
    monkeypatch.setattr(event_runner, "get_cremind_agent", lambda: _StubAgent())

    trigger = {"kind": "event_task_result", "event_type": "x", "task_chain_depth": 2}

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="Chat")
        await event_queue.enqueue_user_message(
            conversation_id=conv["id"], run_id="r1", profile="p1", query="q",
            push_user_message=False, trigger_event=trigger,
        )
        # Let the per-conversation worker drain.
        for _ in range(50):
            await asyncio.sleep(0)
            if seen:
                break
        event_queue.discard_queue(conv["id"])

    asyncio.run(_run())
    assert seen.get("trigger_event") == trigger
    assert seen.get("push_user_message") is False


def test_a_trigger_turn_is_event_triggered_and_carries_its_chain_depth(tmp_path, monkeypatch):
    """``triggered_by_event`` is what the registration gate keys on."""
    cs, _, _, _ = _setup(tmp_path, monkeypatch)
    agent = _StubAgent()

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="Chat")
        await sr.run_agent_to_bus(
            cremind_agent=agent,
            conversation_storage=cs,
            conversation_id=conv["id"],
            run_id=sr.make_run_id(conv["id"], kind="event"),
            profile="p1",
            query="continue the flow",
            history_messages=[],
            push_user_message=False,
            trigger_event={
                "kind": "event_task_result", "event_type": "e",
                "action": "a", "content": "c", "task_chain_depth": 4,
            },
            update_title_from_query=False,
        )

    asyncio.run(_run())
    assert agent.seen[0]["triggered_by_event"] is True
    assert agent.seen[0]["task_chain_depth"] == 4
    # A normal chat turn is unaffected.
    assert agent.seen[0]["event_run"] is False
