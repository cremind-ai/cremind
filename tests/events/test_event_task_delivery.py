"""Handing a one-shot task's result back to the conversation that waited for it.

This is the feature's payoff and its riskiest surface: the continuation turn is
injected into a real user conversation, so "deliver twice", "deliver the wrong
thing", and "never deliver" are all visible failures. Each is pinned here.
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
from app.storage.event_run_storage import EventRunStorage  # noqa: E402
from app.storage.event_subscription_storage import EventSubscriptionStorage  # noqa: E402
from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage  # noqa: E402

from app.events import event_task_delivery as etd  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages", "event_runs",
    "skill_event_subscriptions", "file_watcher_subscriptions",
)


class _Enqueued(list):
    """Captures what would have been queued onto the origin conversation."""

    async def __call__(self, **kwargs):
        self.append(kwargs)


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "delivery.db"))
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
    fws = FileWatcherSubscriptionStorage(provider)

    import app.storage as storage_pkg
    monkeypatch.setattr(storage_pkg, "get_conversation_storage", lambda *a, **k: cs)
    monkeypatch.setattr(storage_pkg, "get_event_run_storage", lambda *a, **k: ers)
    monkeypatch.setattr(storage_pkg, "get_event_subscription_storage", lambda *a, **k: subs)
    monkeypatch.setattr(storage_pkg, "get_file_watcher_storage", lambda *a, **k: fws)

    from app.events import runner as event_runner
    monkeypatch.setattr(event_runner, "get_conversation_storage", lambda: cs)

    # The channel mirror needs a live registry; delivery must not depend on it.
    from app.events import run_dispatcher
    async def _no_forward(*a, **k):
        return None
    monkeypatch.setattr(run_dispatcher, "_maybe_forward_to_channel", _no_forward)

    queued = _Enqueued()
    from app.events import queue as event_queue
    monkeypatch.setattr(event_queue, "enqueue_user_message", queued)

    return cs, ers, subs, fws, queued


async def _scenario(cs, ers, subs, *, status="completed", trigger_payload=None):
    """An armed, claimed task whose run has just reached ``status``."""
    origin = await cs.create_conversation(profile="p1", title="Customer ABC")
    sub = subs.insert(
        conversation_id=origin["id"], profile="p1", skill_name="imap-email",
        event_type="new-mail", action="report the customer's decision", task=True,
    )
    subs.claim_task_fire(sub["id"])
    run_conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
    created = await ers.create(
        profile="p1", source_kind="skill_event", subscription_id=sub["id"],
        conversation_id=run_conv["id"], label="imap-email:new-mail",
        action="report the customer's decision", trigger_payload=trigger_payload,
        origin_conversation_id=origin["id"], deliver_to_origin=True,
    )
    rid = created["run"]["id"]
    await ers.update_status(rid, status=status, mark_finished=True)
    return rid, sub, origin["id"]


# ── the happy path ──────────────────────────────────────────────────────────


def test_completed_task_result_lands_in_the_origin_conversation(tmp_path, monkeypatch):
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        rid, sub, origin_id = await _scenario(cs, ers, subs)
        outcome = await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed",
            final_text="They approved the quote, delivery on the 21st.",
        )
        return outcome, sub, origin_id

    outcome, sub, origin_id = asyncio.run(_run())
    assert outcome == etd.DELIVERED
    assert len(queued) == 1

    item = queued[0]
    assert item["conversation_id"] == origin_id
    # A trigger bubble, not a fake user message — the turn was system-injected.
    assert item["push_user_message"] is False
    assert item["trigger_event"]["kind"] == "event_task_result"
    assert item["trigger_event"]["status"] == "completed"
    assert "approved the quote" in item["trigger_event"]["content"]
    assert item["user_message_metadata"]["source"] == "event_task_result"
    assert item["update_title_from_query"] is False
    # It is a normal chat turn, not another event run.
    assert not item.get("event_run")
    assert item.get("event_run_id") is None
    # And the model is told what to do with it.
    assert "Continue the original flow" in item["query"]
    assert "They approved the quote" in item["query"]

    # The one-shot has terminated.
    assert subs.get(sub["id"])["task_status"] == "completed"


def test_delivery_happens_once_even_if_the_run_goes_terminal_twice(tmp_path, monkeypatch):
    """A late reply into a finished run must not replay the continuation."""
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        rid, _, _ = await _scenario(cs, ers, subs)
        first = await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="done",
        )
        second = await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="done again",
        )
        return first, second

    first, second = asyncio.run(_run())
    assert first == etd.DELIVERED
    assert second == etd.ALREADY_DELIVERED
    assert len(queued) == 1


def test_ordinary_event_runs_are_untouched(tmp_path, monkeypatch):
    """The hook runs for every terminal event run; non-tasks must be a no-op."""
    cs, ers, _, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
        created = await ers.create(
            profile="p1", source_kind="schedule", subscription_id="s1",
            conversation_id=conv["id"], label="Daily", action="a",
        )
        rid = created["run"]["id"]
        await ers.update_status(rid, status="completed", mark_finished=True)
        return await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="x",
        )

    assert asyncio.run(_run()) == etd.NOT_TASK
    assert queued == []


# ── failure, cancellation, timeout ──────────────────────────────────────────


def test_failed_task_reports_the_failure_rather_than_hanging(tmp_path, monkeypatch):
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        rid, sub, _ = await _scenario(cs, ers, subs, status="failed")
        outcome = await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="failed",
            error="the mailbox credentials expired",
        )
        return outcome, sub

    outcome, sub = asyncio.run(_run())
    assert outcome == etd.DELIVERED
    assert queued[0]["trigger_event"]["status"] == "failed"
    assert "credentials expired" in queued[0]["query"]
    assert "Report the failure" in queued[0]["query"]
    assert subs.get(sub["id"])["task_status"] == "completed"


def test_cancelled_task_terminates_quietly(tmp_path, monkeypatch):
    """The user killed it from the Events page; echoing that into chat is noise."""
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        rid, sub, _ = await _scenario(cs, ers, subs, status="cancelled")
        outcome = await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="cancelled",
        )
        return outcome, sub

    outcome, sub = asyncio.run(_run())
    assert outcome == etd.SKIPPED_CANCELLED
    assert queued == []
    assert subs.get(sub["id"])["task_status"] == "cancelled"


def test_timed_out_task_says_the_event_never_happened(tmp_path, monkeypatch):
    """The model must not read a timeout as "the thing I waited for occurred"."""
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        rid, _, _ = await _scenario(
            cs, ers, subs, status="failed", trigger_payload={"timed_out": True},
        )
        return await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="failed",
            final_text="The awaited event never fired before the deadline.",
        )

    assert asyncio.run(_run()) == etd.DELIVERED
    query = queued[0]["query"]
    assert "Status: timed out" in query
    assert "do NOT assume the outcome happened" in query


def test_deliver_timeout_records_a_run_and_reports_it(tmp_path, monkeypatch):
    """An expired task produces a visible record AND a message to the chat."""
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Debug --disk")
        sub = subs.insert(
            conversation_id=origin["id"], profile="p1", skill_name="imap-email",
            event_type="new-mail", action="report the reply", task=True,
            timeout_at=1.0,
        )
        subs.claim_task_timeout(sub["id"])
        outcome = await etd.deliver_timeout("skill_event", subs.get(sub["id"]))
        runs, _ = await ers.list(profile="p1")
        return outcome, runs, subs.get(sub["id"])

    outcome, runs, sub_row = asyncio.run(_run())
    assert outcome == etd.DELIVERED
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["conversation_id"] is None       # no agent run ever happened
    assert runs[0]["origin_delivered_at"] is not None
    assert "Status: timed out" in queued[0]["query"]
    # Delivery must not rewrite an expired task as "completed" — `timed_out` is
    # already terminal and is the honest record of what happened.
    assert sub_row["task_status"] == "timed_out"


# ── edge cases ──────────────────────────────────────────────────────────────


def test_deleted_origin_conversation_degrades_to_no_delivery(tmp_path, monkeypatch):
    """Nothing to continue — and the claim is kept so no sweep retries forever."""
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        rid, _, origin_id = await _scenario(cs, ers, subs)
        await cs.delete_conversation(origin_id)
        outcome = await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="done",
        )
        return outcome, await ers.list_undelivered_task_runs()

    outcome, undelivered = asyncio.run(_run())
    assert outcome == etd.ORIGIN_GONE
    assert queued == []
    assert undelivered == []


def test_empty_final_text_still_delivers_something(tmp_path, monkeypatch):
    """Silence is not an acceptable answer to a conversation that is blocked."""
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        rid, _, _ = await _scenario(cs, ers, subs)
        return await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="   ",
        )

    assert asyncio.run(_run()) == etd.DELIVERED
    assert "no output" in queued[0]["trigger_event"]["content"]


def test_chain_depth_increments_across_deliveries(tmp_path, monkeypatch):
    """Each hop is counted so a wait-continue-wait loop cannot run away."""
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        rid, _, _ = await _scenario(
            cs, ers, subs, trigger_payload={"task_chain_depth": 3},
        )
        await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="ok",
        )

    asyncio.run(_run())
    assert queued[0]["trigger_event"]["task_chain_depth"] == 4


# ── boot sweep ──────────────────────────────────────────────────────────────


def test_boot_sweep_delivers_a_result_a_crash_stranded(tmp_path, monkeypatch):
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        # A run that was live when the process died.
        rid, sub, _ = await _scenario(cs, ers, subs, status="running")
        await ers.recover_after_restart()
        delivered = await etd.sweep_undelivered()
        # A second boot must not re-deliver.
        again = await etd.sweep_undelivered()
        return delivered, again, sub, rid

    delivered, again, sub, _ = asyncio.run(_run())
    assert delivered == 1
    assert again == 0
    assert len(queued) == 1
    assert "Interrupted by server restart" in queued[0]["query"]
    assert subs.get(sub["id"])["task_status"] == "completed"


def test_boot_sweep_recovers_a_completed_run_s_answer(tmp_path, monkeypatch):
    """The final text is only in memory, so the sweep re-reads the transcript."""
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        rid, _, _ = await _scenario(cs, ers, subs)
        run = await ers.get(rid)
        await cs.add_message(
            conversation_id=run["conversation_id"], role="agent",
            content="CI passed on commit abc123.",
        )
        return await etd.sweep_undelivered()

    assert asyncio.run(_run()) == 1
    assert "CI passed on commit abc123" in queued[0]["query"]


def test_boot_reconcile_closes_a_task_stuck_mid_claim(tmp_path, monkeypatch):
    """Claimed, then the process died before its run row existed."""
    cs, ers, subs, _, queued = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Chat")
        sub = subs.insert(
            conversation_id=origin["id"], profile="p1", skill_name="imap-email",
            event_type="new-mail", action="report it", task=True,
        )
        subs.claim_task_fire(sub["id"])          # ...and nothing else happened
        await etd.sweep_undelivered()
        return sub

    sub = asyncio.run(_run())
    assert subs.get(sub["id"])["task_status"] == "completed"
    assert queued == []
