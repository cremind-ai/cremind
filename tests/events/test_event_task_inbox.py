"""The two paths a finished task result can take back to its conversation.

Idle origin: the result is injected as a turn (v1's behaviour), now sweeping up
any sibling still waiting so N results cost one turn instead of N.

Busy origin: the result PARKS — no DB write at all, because the terminal row is
already the inbox entry — and a notice waits for the agent's next tool result.
Whatever the agent did not read is injected when the turn ends.

The invariants that make that safe, and that this file exists to pin:

* a result ends up in exactly one place, never both and never neither;
* a row a sibling flush claimed must read as ALREADY_DELIVERED, never FAILED —
  the FAILED path releases the claim, which would re-deliver it on every boot;
* parking is decided and performed in one synchronous call, so nothing can slip
  between the liveness check and the park.
"""

from __future__ import annotations

import asyncio
import inspect
import time
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
from app.events import task_result_inbox  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages", "event_runs",
    "skill_event_subscriptions", "file_watcher_subscriptions",
)


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
    provider = SqliteDatabaseProvider(str(tmp_path / "inbox.db"))
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

    forwards = _Forwards()
    from app.events import run_dispatcher
    monkeypatch.setattr(run_dispatcher, "_maybe_forward_to_channel", forwards)

    queued = _Enqueued()
    from app.events import queue as event_queue
    monkeypatch.setattr(event_queue, "enqueue_user_message", queued)

    return cs, ers, subs, fws, queued, forwards


async def _task_run(
    cs, ers, subs, origin_id, *, label="imap-email:new-mail", status="completed",
    trigger_payload=None, answer=None,
):
    """One armed, claimed task whose run has just gone terminal.

    ``answer`` is persisted into the run's own conversation, mirroring the real
    ordering: the stream runner writes the assistant message before it writes
    the terminal status, so by the time a result is deliverable its text is
    already durable. That matters here because a PARKED result keeps nothing in
    memory — whoever reads it later recovers the text from this conversation.
    """
    sub = subs.insert(
        conversation_id=origin_id, profile="p1", skill_name="imap-email",
        event_type="new-mail", action="report the decision", task=True,
    )
    subs.claim_task_fire(sub["id"])
    run_conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
    if answer:
        await cs.add_message(
            conversation_id=run_conv["id"], role="agent", content=answer,
        )
    created = await ers.create(
        profile="p1", source_kind="skill_event", subscription_id=sub["id"],
        conversation_id=run_conv["id"], label=label, action="report the decision",
        trigger_payload=trigger_payload, origin_conversation_id=origin_id,
        deliver_to_origin=True,
    )
    rid = created["run"]["id"]
    await ers.update_status(rid, status=status, mark_finished=True)
    return rid, sub


# ── the busy path ───────────────────────────────────────────────────────────


def test_a_result_landing_mid_turn_parks_instead_of_queueing_a_turn(tmp_path, monkeypatch):
    """The whole point: don't queue behind the turn that is waiting for you."""
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Customer ABC")
        task_result_inbox.bind_run("msg:origin:1", origin["id"])
        rid, sub = await _task_run(cs, ers, subs, origin["id"])
        outcome = await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="approved",
        )
        return outcome, rid, sub, origin["id"]

    outcome, rid, sub, origin_id = asyncio.run(_run())
    assert outcome == etd.PARKED
    assert queued == [], "a parked result must not queue a turn behind the live one"

    # Parking writes NOTHING: the terminal row is already the inbox entry.
    row = asyncio.run(ers.get(rid))
    assert row["origin_delivered_at"] is None
    assert row["origin_delivery_mode"] is None
    # ...so the row is still pending, and the rule is still mid-flight.
    assert [r["id"] for r in asyncio.run(ers.list_pending_for_origin(origin_id))] == [rid]
    assert subs.get(sub["id"])["task_status"] == "triggered"

    # And the agent gets told, once, with no result text in the notice.
    notices = task_result_inbox.drain_notices("msg:origin:1")
    assert len(notices) == 1
    assert notices[0]["label"] == "imap-email:new-mail"
    assert notices[0]["status_word"] == "completed"
    assert "approved" not in str(notices[0])
    assert task_result_inbox.drain_notices("msg:origin:1") == [], "drain-once"


def test_parked_results_are_injected_as_one_turn_when_the_turn_ends(tmp_path, monkeypatch):
    """The guarantee that lets the agent safely ignore a notice."""
    cs, ers, subs, _, queued, forwards = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Customer ABC")
        task_result_inbox.bind_run("msg:origin:1", origin["id"])
        r1, s1 = await _task_run(cs, ers, subs, origin["id"], label="CI pipeline")
        r2, s2 = await _task_run(cs, ers, subs, origin["id"], label="staging deploy")
        for rid in (r1, r2):
            await etd.on_run_terminal(
                event_run_id=rid, profile="p1", status="completed", final_text=f"done {rid[:4]}",
            )
        assert queued == []
        # The turn ends: unbind, then flush (stream_runner's finally order).
        task_result_inbox.unbind_run("msg:origin:1")
        result = await etd.flush_origin_inbox(
            conversation_id=origin["id"], profile="p1", reason="turn_end",
        )
        return result, r1, r2, s1, s2, origin["id"]

    result, r1, r2, s1, s2, origin_id = asyncio.run(_run())
    assert result.claimed == {r1, r2}

    # TWO results, ONE turn — and therefore one platform message.
    assert len(queued) == 1
    assert len(forwards) == 1
    item = queued[0]
    assert item["conversation_id"] == origin_id
    assert "CI pipeline" in item["query"] and "staging deploy" in item["query"]
    assert "2 one-shot tasks" in item["query"]
    assert item["trigger_event"]["kind"] == "event_task_result"
    assert set(item["trigger_event"]["event_run_ids"]) == {r1, r2}
    # Depth is the chain LENGTH, so it takes the max and adds one — not the sum.
    assert item["trigger_event"]["task_chain_depth"] == 1

    for rid in (r1, r2):
        row = asyncio.run(ers.get(rid))
        assert row["origin_delivered_at"] is not None
        assert row["origin_delivery_mode"] == etd.MODE_INJECTED
    for sub in (s1, s2):
        assert subs.get(sub["id"])["task_status"] == "completed"


def test_a_result_the_agent_read_is_never_injected_again(tmp_path, monkeypatch):
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Customer ABC")
        task_result_inbox.bind_run("msg:origin:1", origin["id"])
        rid, sub = await _task_run(cs, ers, subs, origin["id"], answer="approved")
        await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="approved",
        )
        text_out, depths = await etd.read_origin_inbox(
            conversation_id=origin["id"], profile="p1",
        )
        # Turn ends afterwards: nothing left to flush.
        task_result_inbox.unbind_run("msg:origin:1")
        flushed = await etd.flush_origin_inbox(
            conversation_id=origin["id"], profile="p1", reason="turn_end",
        )
        return text_out, depths, flushed, rid, sub

    text_out, depths, flushed, rid, sub = asyncio.run(_run())
    assert "approved" in text_out
    assert "Continue the original flow" in text_out
    assert depths == [0]
    assert queued == [], "a result already read must not also arrive as a turn"
    assert flushed.claimed == set()

    row = asyncio.run(ers.get(rid))
    assert row["origin_delivery_mode"] == etd.MODE_READ
    assert subs.get(sub["id"])["task_status"] == "completed"


def test_reading_twice_is_harmless(tmp_path, monkeypatch):
    """The model will probe. An empty inbox must never look like an error."""
    cs, ers, subs, _, _, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Customer ABC")
        task_result_inbox.bind_run("msg:origin:1", origin["id"])
        rid, _ = await _task_run(cs, ers, subs, origin["id"], answer="approved")
        await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="approved",
        )
        first, _ = await etd.read_origin_inbox(conversation_id=origin["id"], profile="p1")
        second, depths = await etd.read_origin_inbox(
            conversation_id=origin["id"], profile="p1",
        )
        return first, second, depths

    first, second, depths = asyncio.run(_run())
    assert "approved" in first
    assert "No task results are waiting" in second
    assert depths == []


# ── the idle path ───────────────────────────────────────────────────────────


def test_an_idle_origin_still_gets_the_result_as_a_turn(tmp_path, monkeypatch):
    """No binding = nobody to notice = deliver it the old way."""
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Customer ABC")
        rid, _ = await _task_run(cs, ers, subs, origin["id"])
        outcome = await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="approved",
        )
        return outcome

    assert asyncio.run(_run()) == etd.DELIVERED
    assert len(queued) == 1
    # A single result reads exactly as it did in v1 — same header, same body.
    assert queued[0]["query"].startswith("[Event task result] The one-shot task")
    assert queued[0]["trigger_event"]["event_run_id"]


def test_an_idle_delivery_sweeps_up_siblings_that_were_still_waiting(tmp_path, monkeypatch):
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Customer ABC")
        # One parked earlier (origin was busy), one landing now (origin idle).
        task_result_inbox.bind_run("msg:origin:1", origin["id"])
        r1, _ = await _task_run(cs, ers, subs, origin["id"], label="first")
        await etd.on_run_terminal(
            event_run_id=r1, profile="p1", status="completed", final_text="one",
        )
        task_result_inbox.unbind_run("msg:origin:1")

        r2, _ = await _task_run(cs, ers, subs, origin["id"], label="second")
        outcome = await etd.on_run_terminal(
            event_run_id=r2, profile="p1", status="completed", final_text="two",
        )
        return outcome

    assert asyncio.run(_run()) == etd.DELIVERED
    assert len(queued) == 1
    assert "first" in queued[0]["query"] and "second" in queued[0]["query"]


# ── claim discipline ────────────────────────────────────────────────────────


def test_a_row_a_sibling_flush_claimed_reads_as_already_delivered(tmp_path, monkeypatch):
    """The boot-sweep trap: FAILED here would re-deliver the row forever.

    The sweep walks rows one by one, but delivering row 1 claims row 2 as well.
    Row 2's own call must therefore report ALREADY_DELIVERED — the FAILED branch
    releases the claim, and the row would come back on every single boot.
    """
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Customer ABC")
        r1, _ = await _task_run(cs, ers, subs, origin["id"], label="first")
        r2, _ = await _task_run(cs, ers, subs, origin["id"], label="second")

        first = await etd.on_run_terminal(
            event_run_id=r1, profile="p1", status="completed", final_text="one",
        )
        second = await etd.on_run_terminal(
            event_run_id=r2, profile="p1", status="completed", final_text="two",
        )
        after = await ers.get(r2)
        # A second sweep must find nothing at all.
        again = await ers.list_undelivered_task_runs()
        return first, second, after, again

    first, second, after, again = asyncio.run(_run())
    assert first == etd.DELIVERED
    assert second == etd.ALREADY_DELIVERED
    assert len(queued) == 1, "both results went out in row 1's coalesced turn"
    assert after["origin_delivered_at"] is not None, "the claim must survive"
    assert again == [], "nothing is left for the next boot to re-deliver"


def test_a_cancelled_run_is_closed_out_quietly_and_never_enters_the_inbox(
    tmp_path, monkeypatch,
):
    """Cancelling from the Events page is a deliberate kill, not an outcome."""
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Customer ABC")
        task_result_inbox.bind_run("msg:origin:1", origin["id"])  # even while busy
        rid, sub = await _task_run(cs, ers, subs, origin["id"], status="cancelled")
        outcome = await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="cancelled",
        )
        pending = await ers.list_pending_for_origin(origin["id"])
        return outcome, rid, sub, pending

    outcome, rid, sub, pending = asyncio.run(_run())
    assert outcome == etd.SKIPPED_CANCELLED
    assert queued == []
    assert task_result_inbox.drain_notices("msg:origin:1") == []
    assert pending == [], "a cancelled run is not something to read"
    assert asyncio.run(ers.get(rid))["origin_delivery_mode"] == etd.MODE_SKIPPED
    assert subs.get(sub["id"])["task_status"] == "cancelled"


def test_a_read_cancelled_midway_releases_its_claims(tmp_path, monkeypatch):
    """Stop arrives as CancelledError, which the leaf runner does not catch.

    Claiming before the text reaches the model would destroy the result: the row
    reads as delivered, but nothing was ever shown. So the claims must come back.
    """
    cs, ers, subs, _, _, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Customer ABC")
        rid, _ = await _task_run(cs, ers, subs, origin["id"])
        task_result_inbox.bind_run("msg:origin:1", origin["id"])
        await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="approved",
        )

        # Blow up after the claims are taken, while the text is being built.
        def _boom(_items, **_kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(etd, "build_read_result_text", _boom)
        with pytest.raises(asyncio.CancelledError):
            await etd.read_origin_inbox(conversation_id=origin["id"], profile="p1")

        monkeypatch.undo()
        return rid, await ers.list_pending_for_origin(origin["id"])

    rid, still_pending = asyncio.run(_run())
    assert [r["id"] for r in still_pending] == [rid], "the result must survive a Stop"
    assert asyncio.run(ers.get(rid))["origin_delivered_at"] is None


async def _standing_run(
    cs, ers, subs, origin_id, *, label="imap-email:new-mail", answer=None,
    finished_at=None,
):
    """A STANDING rule's run that has just gone terminal.

    No claim: a standing rule is never spent, which is exactly what makes it
    able to report again tomorrow.
    """
    sub = subs.insert(
        conversation_id=origin_id, profile="p1", skill_name="imap-email",
        event_type="new-mail", action="summarize it", task=False,
    )
    run_conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
    if answer:
        await cs.add_message(
            conversation_id=run_conv["id"], role="agent", content=answer,
        )
    created = await ers.create(
        profile="p1", source_kind="skill_event", subscription_id=sub["id"],
        conversation_id=run_conv["id"], label=label, action="summarize it",
        trigger_payload={"once": False}, origin_conversation_id=origin_id,
        deliver_to_origin=True,
    )
    rid = created["run"]["id"]
    await ers.update_status(rid, status="completed", mark_finished=True)
    if finished_at is not None:
        await _set_finished_at(ers, rid, finished_at)
    return rid, sub


async def _set_finished_at(ers, run_id: str, value: float) -> None:
    """Backdate a run so the age bound has something to act on."""
    from sqlalchemy import text as _text

    async with ers.async_session_maker.begin() as session:
        await session.execute(
            _text("UPDATE event_runs SET finished_at = :v WHERE id = :i"),
            {"v": value, "i": run_id},
        )


# ── volume bounds ───────────────────────────────────────────────────────────


def test_only_the_newest_standing_results_are_reported(tmp_path, monkeypatch):
    """A rule that fires faster than the chat answers must not flood it.

    The newest results are worth reading; the backlog behind them is not. What
    is dropped is still named in the turn, because a result vanishing without
    trace is worse than a slightly longer message.
    """
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.events.run_config.max_results_per_delivery", lambda: 2,
    )

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Mail")
        now_ms = time.time() * 1000
        ids = []
        for i in range(4):
            rid, _ = await _standing_run(
                cs, ers, subs, origin["id"], label=f"rule-{i}",
                answer=f"summary {i}", finished_at=now_ms + i,
            )
            ids.append(rid)
        outcome = await etd.flush_origin_inbox(
            conversation_id=origin["id"], profile="p1", reason="idle",
        )
        rows = [await ers.get(r) for r in ids]
        return ids, outcome, rows

    ids, outcome, rows = asyncio.run(_run())

    assert len(queued) == 1, "one turn, however deep the backlog"
    query = queued[0]["query"]
    assert "summary 2" in query and "summary 3" in query
    assert "summary 0" not in query and "summary 1" not in query
    # The dropped ones are named, not just counted.
    assert "rule-0" in query and "rule-1" in query
    assert "dropped without being reported" in query

    assert outcome.claimed == {ids[2], ids[3]}
    assert outcome.dropped == frozenset({ids[0], ids[1]})
    assert [r["origin_delivery_mode"] for r in rows[:2]] == ["skipped", "skipped"]
    assert [r["origin_delivery_mode"] for r in rows[2:]] == ["injected", "injected"]


def test_one_shot_results_are_never_dropped_by_the_cap(tmp_path, monkeypatch):
    """Each was explicitly awaited by a flow that cannot continue without it."""
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.events.run_config.max_results_per_delivery", lambda: 2,
    )

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Mail")
        for i in range(4):
            await _task_run(
                cs, ers, subs, origin["id"], label=f"task-{i}",
                answer=f"outcome {i}", trigger_payload={"once": True},
            )
        return await etd.flush_origin_inbox(
            conversation_id=origin["id"], profile="p1", reason="idle",
        )

    outcome = asyncio.run(_run())
    assert len(outcome.claimed) == 4
    assert outcome.dropped == frozenset()
    assert all(f"outcome {i}" in queued[0]["query"] for i in range(4))


def test_results_older_than_the_age_bound_are_closed_out(tmp_path, monkeypatch):
    """After a week of downtime, "here is today's news" x7 is noise."""
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.events.run_config.undelivered_max_age_hours", lambda: 1,
    )

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Mail")
        now_ms = time.time() * 1000
        old, _ = await _standing_run(
            cs, ers, subs, origin["id"], label="yesterday", answer="stale news",
            finished_at=now_ms - 5 * 3600 * 1000,
        )
        fresh, _ = await _standing_run(
            cs, ers, subs, origin["id"], label="today", answer="fresh news",
            finished_at=now_ms - 60 * 1000,
        )
        outcome = await etd.flush_origin_inbox(
            conversation_id=origin["id"], profile="p1", reason="idle",
        )
        return old, fresh, outcome, await ers.get(old)

    old, fresh, outcome, old_row = asyncio.run(_run())
    assert outcome.claimed == {fresh}
    assert outcome.dropped == frozenset({old})
    assert "fresh news" in queued[0]["query"]
    assert "stale news" not in queued[0]["query"]
    assert old_row["origin_delivery_mode"] == "skipped"


def test_a_one_shot_result_outlives_the_age_bound(tmp_path, monkeypatch):
    """Its deadline is ``timeout_minutes``; dropping it would strand the flow."""
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.events.run_config.undelivered_max_age_hours", lambda: 1,
    )

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Deploy")
        rid, _ = await _task_run(
            cs, ers, subs, origin["id"], answer="CI is green",
            trigger_payload={"once": True},
        )
        await _set_finished_at(ers, rid, time.time() * 1000 - 30 * 24 * 3600 * 1000)
        return rid, await etd.flush_origin_inbox(
            conversation_id=origin["id"], profile="p1", reason="idle",
        )

    rid, outcome = asyncio.run(_run())
    assert outcome.claimed == {rid}
    assert outcome.dropped == frozenset()
    assert "CI is green" in queued[0]["query"]


def test_a_dropped_result_reports_as_stale_not_delivered(tmp_path, monkeypatch):
    """DELIVERED suppresses the run's own notification; a dropped one must not."""
    cs, ers, subs, _, _, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.events.run_config.undelivered_max_age_hours", lambda: 1,
    )

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Mail")
        rid, _ = await _standing_run(
            cs, ers, subs, origin["id"], answer="old news",
            finished_at=time.time() * 1000 - 9 * 3600 * 1000,
        )
        return await etd.on_run_terminal(
            event_run_id=rid, profile="p1", status="completed", final_text="old news",
        )

    assert asyncio.run(_run()) == etd.SKIPPED_STALE
    assert etd.SKIPPED_STALE not in etd.SUPPRESSES_RUN_NOTIFICATION


def test_an_all_stale_flush_still_says_something(tmp_path, monkeypatch):
    """The runs' own notifications were suppressed when they parked.

    If everything owed is then dropped, this notification is the only thing
    left that keeps that promise.
    """
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.events.run_config.undelivered_max_age_hours", lambda: 1,
    )
    pushed = []

    class _Buffer:
        def push(self, **kwargs):
            pushed.append(kwargs)
            return kwargs

    monkeypatch.setattr("app.events.get_event_notifications", lambda: _Buffer())

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Mail")
        await _standing_run(
            cs, ers, subs, origin["id"], label="nightly", answer="old",
            finished_at=time.time() * 1000 - 9 * 3600 * 1000,
        )
        return await etd.flush_origin_inbox(
            conversation_id=origin["id"], profile="p1", reason="idle",
        )

    outcome = asyncio.run(_run())
    assert queued == [], "nothing worth a turn"
    assert len(outcome.dropped) == 1
    assert len(pushed) == 1
    assert "nightly" in pushed[0]["message_preview"]


def test_delivery_order_follows_when_runs_finished(tmp_path, monkeypatch):
    """A run that parked on a question finishes after ones that fired later."""
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Mail")
        now_ms = time.time() * 1000
        first, _ = await _standing_run(
            cs, ers, subs, origin["id"], label="asked-first", answer="slow answer",
            finished_at=now_ms,
        )
        second, _ = await _standing_run(
            cs, ers, subs, origin["id"], label="asked-second", answer="quick answer",
            finished_at=now_ms - 60_000,
        )
        await etd.flush_origin_inbox(
            conversation_id=origin["id"], profile="p1", reason="idle",
        )
        return first, second

    asyncio.run(_run())
    query = queued[0]["query"]
    assert query.index("quick answer") < query.index("slow answer")


def test_a_room_origin_is_told_it_is_speaking_to_a_room(tmp_path, monkeypatch):
    """And raises no notification: the post itself is how the room finds out."""
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        seat = await cs.create_conversation(
            profile="p1", title="seat", kind="group_chat", context_id="group:g1:p1",
        )
        plain = await cs.create_conversation(profile="p1", title="chat")
        await _standing_run(cs, ers, subs, seat["id"], answer="digest")
        await _standing_run(cs, ers, subs, plain["id"], answer="digest")
        await etd.flush_origin_inbox(
            conversation_id=seat["id"], profile="p1", reason="idle",
        )
        await etd.flush_origin_inbox(
            conversation_id=plain["id"], profile="p1", reason="idle",
        )

    asyncio.run(_run())
    seat_turn, plain_turn = queued[0], queued[1]
    assert seat_turn["publish_notification"] is False
    assert "posted to everyone in it" in seat_turn["query"]
    assert plain_turn["publish_notification"] is True
    assert "posted to everyone in it" not in plain_turn["query"]


def test_the_trigger_bubble_is_marked_but_the_answer_is_not(tmp_path, monkeypatch):
    """Both rows carry ``source``; only the machine-written block is a trigger.

    Everything that renders or filters an automation result keys on this, so
    without it the agent's own reply is labelled as the automation's output.
    """
    cs, ers, subs, _, queued, _ = _setup(tmp_path, monkeypatch)

    async def _run():
        origin = await cs.create_conversation(profile="p1", title="Mail")
        await _standing_run(cs, ers, subs, origin["id"], answer="digest")
        await etd.flush_origin_inbox(
            conversation_id=origin["id"], profile="p1", reason="idle",
        )

    asyncio.run(_run())
    trigger_meta = queued[0]["user_message_metadata"]
    answer_meta = queued[0]["agent_message_metadata"]
    assert trigger_meta["source"] == "event_task_result"
    assert trigger_meta["trigger"] is True
    assert trigger_meta["once"] is False
    assert trigger_meta["label"]
    assert answer_meta["source"] == "event_task_result"
    assert "trigger" not in answer_meta


def test_deciding_to_park_cannot_be_split_by_an_await(tmp_path, monkeypatch):
    """A structural guard, because no runtime test can catch the regression.

    The turn-end handoff is sound only because the liveness check and the park
    happen with nothing in between: an ``await`` there lets the turn end (and
    its flush query run) after the check but before the park, stranding the row
    until the next turn or the next boot. Keeping both inside ONE synchronous
    function is what makes that impossible to write by accident.
    """
    src = inspect.getsource(task_result_inbox.park_if_bound)
    # Strip the docstring — it *discusses* awaits; only the code matters.
    head, _, rest = src.partition('"""')
    code = head + rest.partition('"""')[2]
    assert "async def" not in code
    assert "await" not in code
    # ...and the fork calls that one function rather than rolling its own pair.
    fork = inspect.getsource(etd.on_run_terminal)
    assert "park_if_bound" in fork
    assert "is_active" not in fork
