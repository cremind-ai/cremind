"""The whole event-task loop, with every real component except the LLM.

Example 2 from the feature's motivating cases: the assistant replies to a
customer, registers a one-shot task for their answer, and ends its turn. When
the mail arrives the task fires once, runs in a hidden conversation, hands the
outcome back to the original chat as a new turn, and terminates — and a second
mail must not fire it again.

Real: storages, run dispatcher (per-rule FIFO + claim), stream runner, delivery,
per-conversation queue, SQLite. Stubbed: only the agent's answer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.events import run_dispatcher  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.event_run_storage import EventRunStorage  # noqa: E402
from app.storage.event_subscription_storage import EventSubscriptionStorage  # noqa: E402
from app.tools.builtin.register_skill_event import register_skill_events  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages", "event_runs",
    "skill_event_subscriptions",
)

_ANSWER = "The customer approved the quote and asked for delivery on the 21st."


class _StubAgent:
    """Replies with a fixed line. Records the query each run was given."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def low_performance_llm(self, profile):
        # The skill-event matching gate asks for this. Returning None makes the
        # gate fail open (its documented no-LLM behaviour), so every event
        # reaches the dispatcher — which is what this test is about.
        return None

    async def run(self, **kwargs):
        self.queries.append(kwargs.get("query", ""))
        yield {"type": ChatCompletionTypeEnum.CONTENT, "data": _ANSWER}
        yield {
            "type": ChatCompletionTypeEnum.DONE,
            "input_tokens": 1, "output_tokens": 1, "finish_reason": "stop",
        }


@pytest.fixture(autouse=True)
def _isolate_queues():
    """Both queue registries are process-global; don't leak workers between tests."""
    from app.events import queue as event_queue

    def _clear():
        for registry in (
            run_dispatcher._sub_queues, run_dispatcher._sub_workers,
            event_queue._queues, event_queue._workers,
        ):
            registry.clear()

    _clear()
    yield
    _clear()


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "e2e.db"))
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
    subs = EventSubscriptionStorage(provider)
    agent = _StubAgent()

    import app.storage as storage_pkg
    for name, value in (
        ("get_conversation_storage", cs),
        ("get_event_run_storage", ers),
        ("get_event_subscription_storage", subs),
    ):
        monkeypatch.setattr(storage_pkg, name, lambda *a, _v=value, **k: _v)

    import app.tools.builtin.register_skill_event as RSE
    monkeypatch.setattr(RSE, "get_event_subscription_storage", lambda: subs)
    monkeypatch.setattr(RSE, "_read_events_metadata", lambda src: [{"name": "new-mail"}])

    from app.events import runner as event_runner
    monkeypatch.setattr(event_runner, "get_conversation_storage", lambda: cs)
    monkeypatch.setattr(event_runner, "get_cremind_agent", lambda: agent)

    # The self-containment gate needs an LLM; fail-open is its documented
    # behaviour when none is wired, which is what we want here.
    async def _accept(**kw):
        return None
    monkeypatch.setattr("app.events.action_check.gate_registration_action", _accept)

    # No external channel bound to the origin conversation.
    async def _no_forward(*a, **k):
        return None
    monkeypatch.setattr(run_dispatcher, "_maybe_forward_to_channel", _no_forward)

    return cs, ers, subs, agent


async def _deliver_mail(subs, body: str) -> None:
    """What EventManager._dispatch does when a mail lands in the events folder."""
    for sub in subs.list_by_event(profile="p1", skill_name="imap-email", event_type="new-mail"):
        if sub.get("paused"):
            continue
        if sub.get("task") and sub.get("task_status") != "active":
            continue  # spent one-shot
        await run_dispatcher.dispatch_skill_event(sub=sub, content=body)


async def _drain() -> None:
    """Wait for the per-rule dispatch worker and the per-conversation queue.

    Deterministic rather than sleep-based: the dispatcher hands off to a
    background worker, whose run may in turn queue a continuation on the origin
    conversation, so both queues have to be joined — and the second one only
    exists after the first has run.
    """
    from app.events import queue as event_queue

    for _ in range(6):
        for queues in (run_dispatcher._sub_queues, event_queue._queues):
            for q in list(queues.values()):
                await asyncio.wait_for(q.join(), timeout=10)
        await asyncio.sleep(0)


def test_a_task_waits_once_then_hands_its_result_back(tmp_path, monkeypatch):
    cs, ers, subs, agent = _setup(tmp_path, monkeypatch)

    async def _scenario():
        # The user's chat, mid-flow: the assistant just replied to the customer.
        chat = await cs.get_or_create_conversation(profile="p1", context_id="ctx-1")
        await cs.add_message(
            conversation_id=chat["id"], role="user",
            content="Reply to ABC and let me know what they say.",
        )

        # It registers a one-shot task for the answer and ends its turn.
        confirmation = await register_skill_events(
            profile="p1", context_id="ctx-1", skill_id="imap-email",
            skill_source=str(tmp_path / "imap-email"), triggers=["new-mail"],
            action="read the reply and report the customer's decision",
            request_context="", task=True, timeout_minutes=1440,
        )
        assert "one-shot TASK" in confirmation

        before = len(await cs.get_messages(chat["id"]))

        # The customer replies. The task fires.
        await _deliver_mail(subs, "From: ABC\n\nApproved — please deliver on the 21st.")
        await _drain()

        # A second mail arrives after the flow moved on: it must NOT fire again.
        await _deliver_mail(subs, "From: ABC\n\nOne more thing…")
        await _drain()

        runs, _ = await ers.list(profile="p1")
        messages = await cs.get_messages(chat["id"])
        sub = subs.list_by_profile("p1")[0]
        return chat["id"], before, runs, messages, sub

    chat_id, before, runs, messages, sub = asyncio.run(_scenario())

    # Fired exactly once, in its own hidden conversation, owing its result here.
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["conversation_id"] != chat_id
    assert runs[0]["origin_conversation_id"] == chat_id
    assert runs[0]["deliver_to_origin"] is True
    assert runs[0]["origin_delivered_at"] is not None

    # The hidden run got the task action + the mail; the chat got the outcome.
    assert "report the customer's decision" in agent.queries[0]
    assert "Approved — please deliver on the 21st." in agent.queries[0]

    new_messages = messages[before:]
    assert new_messages, "the waiting conversation received no continuation turn"
    bubble = new_messages[0]
    # Rendered as a structured agent bubble, not a fake user message.
    assert bubble["role"] == "agent"
    assert "event task completed" in bubble["content"]
    assert _ANSWER in bubble["content"]
    assert (bubble.get("metadata") or {}).get("source") == "event_task_result"

    # ...and the model was handed the outcome plus what to do with it.
    assert _ANSWER in agent.queries[1]
    assert "Continue the original flow" in agent.queries[1]

    # The one-shot has terminated and cannot fire again.
    assert sub["task"] is True
    assert sub["task_status"] == "completed"
    assert sub["completed_at"] is not None


def test_a_standing_subscription_keeps_firing_and_reports_every_run(tmp_path, monkeypatch):
    """"Summarize every new email": fires forever, reports each result back.

    The other half of the feature. Unlike a one-shot the rule is never spent,
    and unlike the old behaviour its runs do not vanish into the notification
    list — each one comes back to the chat that asked for it, worded as a rule
    reporting rather than as a flow to continue.
    """
    cs, ers, subs, agent = _setup(tmp_path, monkeypatch)

    async def _scenario():
        chat = await cs.get_or_create_conversation(profile="p1", context_id="ctx-1")
        await register_skill_events(
            profile="p1", context_id="ctx-1", skill_id="imap-email",
            skill_source=str(tmp_path / "imap-email"), triggers=["new-mail"],
            action="notify me about important mail", request_context="",
        )
        before = len(await cs.get_messages(chat["id"]))

        await _deliver_mail(subs, "mail one")
        await _drain()
        await _deliver_mail(subs, "mail two")
        await _drain()

        runs, _ = await ers.list(profile="p1")
        return before, runs, await cs.get_messages(chat["id"]), subs.list_by_profile("p1")[0]

    before, runs, messages, sub = asyncio.run(_scenario())

    assert len(runs) == 2                            # fires every time
    assert all(r["deliver_to_origin"] is True for r in runs)
    assert all(r["origin_delivered_at"] is not None for r in runs)
    assert all(r["origin_delivery_mode"] == "injected" for r in runs)

    # Two reports arrived: each is a trigger bubble plus the agent's answer.
    assert len(messages) == before + 4
    triggers = [
        m for m in messages
        if (m.get("metadata") or {}).get("source") == "event_task_result"
        and (m.get("metadata") or {}).get("trigger")
    ]
    assert len(triggers) == 2
    assert all(t["metadata"]["once"] is False for t in triggers)
    assert all(t["metadata"].get("label") for t in triggers)

    # Worded as a rule reporting back, not as a one-shot flow continuing.
    reports = [q for q in agent.queries if q.startswith("[Event result]")]
    assert len(reports) == 2
    assert "do NOT re-register it" in reports[0]

    # And the rule is still armed for the next mail.
    assert sub["task"] is False
    assert sub["task_status"] is None


def test_a_result_landing_mid_turn_waits_and_then_arrives_when_the_turn_ends(
    tmp_path, monkeypatch,
):
    """Example 3, the mid-flight case: the chat is busy when the task finishes.

    The old behaviour queued the continuation behind the running turn, where the
    agent could not see it. Now it parks — no claim, no queued turn — a notice
    waits for the agent's next tool result, and whatever it did not read is
    injected the moment the turn ends. Both halves are checked here with the
    real dispatcher, storages and delivery module.
    """
    cs, ers, subs, agent = _setup(tmp_path, monkeypatch)
    from app.events import task_result_inbox

    async def _scenario():
        chat = await cs.get_or_create_conversation(profile="p1", context_id="ctx-1")
        await register_skill_events(
            profile="p1", context_id="ctx-1", skill_id="imap-email",
            skill_source=str(tmp_path / "imap-email"), triggers=["new-mail"],
            action="read the reply and report the customer's decision",
            request_context="", task=True,
        )
        before = len(await cs.get_messages(chat["id"]))

        # The chat is mid-turn when the mail lands.
        task_result_inbox.bind_run("msg:chat:live", chat["id"])
        await _deliver_mail(subs, "From: ABC\n\nApproved — deliver on the 21st.")
        await _drain()

        runs, _ = await ers.list(profile="p1")
        parked = {
            "queued_nothing": len(await cs.get_messages(chat["id"])) == before,
            "unclaimed": runs[0]["origin_delivered_at"] is None,
            "notices": task_result_inbox.drain_notices("msg:chat:live"),
            "pending": await ers.list_pending_for_origin(chat["id"]),
        }

        # The turn ends without the agent having read it.
        task_result_inbox.unbind_run("msg:chat:live")
        from app.events.event_task_delivery import flush_origin_inbox
        await flush_origin_inbox(
            conversation_id=chat["id"], profile="p1", reason="turn_end",
        )
        await _drain()

        return parked, await cs.get_messages(chat["id"]), before, subs.list_by_profile("p1")[0]

    parked, messages, before, sub = asyncio.run(_scenario())

    # While the turn ran: nothing queued, nothing claimed, but the agent was told.
    assert parked["queued_nothing"], "a result must not queue behind the live turn"
    assert parked["unclaimed"], "parking takes no claim — the row IS the inbox entry"
    assert len(parked["notices"]) == 1
    assert parked["notices"][0]["status_word"] == "completed"
    assert len(parked["pending"]) == 1

    # After it ended: the result arrived as its own turn, and the task is spent.
    new_messages = messages[before:]
    assert new_messages, "the parked result never arrived"
    assert _ANSWER in new_messages[0]["content"]
    assert _ANSWER in agent.queries[1]
    assert sub["task_status"] == "completed"
