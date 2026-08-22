"""The inbox query, and the four predicates it must NOT have disturbed.

A conversation's pending-results inbox is not a structure — it is
``list_pending_for_origin``, the boot sweep's own predicate sliced by origin.
That is what keeps this feature cheap: retention pruning, the sweep's work list,
the "is this rule still busy" check and restart recovery all keep working
untouched. Half of this file exists to prove they were left alone.
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
from app.storage.event_run_storage import EventRunStorage  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages", "event_runs")


def _store(tmp_path: Path) -> EventRunStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "inbox.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('pid','p1',0,0)"
        ))
    return EventRunStorage(provider)


async def _run(
    store, *, origin="origin-1", status="completed", task=True, sub="sub-1",
):
    created = await store.create(
        profile="p1", source_kind="skill_event", subscription_id=sub,
        conversation_id=None, label="lbl", action="act",
        origin_conversation_id=origin, deliver_to_origin=task,
    )
    rid = created["run"]["id"]
    await store.update_status(rid, status=status, mark_finished=True)
    return rid


# ── the inbox query ─────────────────────────────────────────────────────────


def test_it_returns_this_origin_s_unhanded_results_oldest_first(tmp_path):
    store = _store(tmp_path)

    async def _go():
        first = await _run(store)
        second = await _run(store, sub="sub-2")
        return first, second, await store.list_pending_for_origin("origin-1")

    first, second, rows = asyncio.run(_go())
    assert [r["id"] for r in rows] == [first, second]


def test_a_cancelled_run_is_never_offered_to_read(tmp_path):
    """Cancelling from the Events page is a deliberate kill, not an outcome.

    Surfacing "your task was cancelled" would be noise piled on an action the
    user just took there — v1 closed such runs out quietly and so must this.
    """
    store = _store(tmp_path)

    async def _go():
        await _run(store, status="cancelled")
        return await store.list_pending_for_origin("origin-1")

    assert asyncio.run(_go()) == []


def test_live_runs_other_origins_and_non_tasks_are_excluded(tmp_path):
    store = _store(tmp_path)

    async def _go():
        await _run(store, status="running")            # not finished
        await _run(store, origin="origin-2", sub="s2")  # someone else's
        await _run(store, task=False, sub="s3")         # an ordinary event run
        return await store.list_pending_for_origin("origin-1")

    assert asyncio.run(_go()) == []


def test_a_handed_over_result_leaves_the_inbox(tmp_path):
    store = _store(tmp_path)

    async def _go():
        rid = await _run(store)
        assert await store.claim_delivery(rid)
        await store.set_delivery_mode(rid, "read")
        return await store.list_pending_for_origin("origin-1"), await store.get(rid)

    rows, row = asyncio.run(_go())
    assert rows == []
    assert row["origin_delivery_mode"] == "read"


def test_the_claim_is_the_arbiter_between_two_readers(tmp_path):
    """Whoever wins the conditional UPDATE owns the hand-over. Exactly one does."""
    store = _store(tmp_path)

    async def _go():
        rid = await _run(store)
        return [await store.claim_delivery(rid) for _ in range(3)]

    assert asyncio.run(_go()) == [True, False, False]


def test_releasing_a_claim_puts_the_result_back_and_clears_the_mode(tmp_path):
    """The read path's rollback when a turn is stopped mid-hand-over."""
    store = _store(tmp_path)

    async def _go():
        rid = await _run(store)
        await store.claim_delivery(rid)
        await store.set_delivery_mode(rid, "read")
        await store.clear_delivery_claim(rid)
        return await store.get(rid), await store.list_pending_for_origin("origin-1")

    row, rows = asyncio.run(_go())
    assert row["origin_delivered_at"] is None
    assert row["origin_delivery_mode"] is None, "a released claim leaves no stale mode"
    assert [r["id"] for r in rows] == [row["id"]]


# ── the predicates this feature must not have disturbed ─────────────────────


def test_an_unread_result_is_never_pruned_by_retention(tmp_path):
    """Pruning a waiting result would strand the flow that asked for it."""
    store = _store(tmp_path)

    async def _go():
        waiting = await _run(store)
        # Push well past the cap with ordinary terminal runs on the same rule.
        for _ in range(5):
            created = await store.create(
                profile="p1", source_kind="skill_event", subscription_id="sub-1",
                conversation_id=None, label="l", action="a", history_cap=2,
            )
            await store.update_status(created["run"]["id"], status="completed")
        return await store.get(waiting)

    assert asyncio.run(_go()) is not None


def test_a_waiting_result_still_counts_as_live_work_for_its_rule(tmp_path):
    """So the boot reconciler cannot close a task whose result is unread."""
    store = _store(tmp_path)

    async def _go():
        rid = await _run(store)
        before = await store.has_live_run_for_subscription("skill_event", "sub-1")
        await store.claim_delivery(rid)
        after = await store.has_live_run_for_subscription("skill_event", "sub-1")
        return before, after

    before, after = asyncio.run(_go())
    assert before is True
    assert after is False


def test_the_boot_sweep_still_finds_a_waiting_result(tmp_path):
    """"Stranded by a crash" and "parked while busy" are the same query."""
    store = _store(tmp_path)

    async def _go():
        rid = await _run(store)
        return rid, [r["id"] for r in await store.list_undelivered_task_runs()]

    rid, undelivered = asyncio.run(_go())
    assert undelivered == [rid]
