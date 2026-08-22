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
        def _boom(_items):
            raise asyncio.CancelledError()

        monkeypatch.setattr(etd, "build_read_result_text", _boom)
        with pytest.raises(asyncio.CancelledError):
            await etd.read_origin_inbox(conversation_id=origin["id"], profile="p1")

        monkeypatch.undo()
        return rid, await ers.list_pending_for_origin(origin["id"])

    rid, still_pending = asyncio.run(_run())
    assert [r["id"] for r in still_pending] == [rid], "the result must survive a Stop"
    assert asyncio.run(ers.get(rid))["origin_delivered_at"] is None


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
