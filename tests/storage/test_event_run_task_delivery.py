"""Delivery bookkeeping on ``event_runs`` for one-shot event tasks.

Three properties matter here, and each has a failure mode a user would feel:
- the delivery claim is exactly-once (else a chat gets the same result twice);
- an undelivered result is never pruned (else a flow silently stalls forever);
- an interrupted run stays deliverable across a restart (same).
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

_TABLES = ("profiles", "channels", "conversations", "messages", "event_runs")


def _setup(tmp_path: Path):
    provider = SqliteDatabaseProvider(str(tmp_path / "runs.db"))
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
    return provider, cs, EventRunStorage(provider)


async def _task_run(cs, ers, *, status="completed", sub_id="sub-1", origin=None, cap=50):
    """A finished task run bound to an origin conversation."""
    if origin is None:
        origin = (await cs.create_conversation(profile="p1", title="Chat"))["id"]
    conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
    created = await ers.create(
        profile="p1", source_kind="skill_event", subscription_id=sub_id,
        conversation_id=conv["id"], label="imap:new-mail", action="report the reply",
        origin_conversation_id=origin, deliver_to_origin=True, history_cap=cap,
    )
    rid = created["run"]["id"]
    await ers.update_status(rid, status=status, mark_finished=True)
    return rid, origin, created


def test_delivery_claim_is_exactly_once(tmp_path):
    _, cs, ers = _setup(tmp_path)

    async def _run():
        rid, _, _ = await _task_run(cs, ers)
        first = await ers.claim_delivery(rid)
        # A second terminal transition (someone replies inside a finished run's
        # mini-chat) must NOT re-deliver.
        second = await ers.claim_delivery(rid)
        row = await ers.get(rid)
        return first, second, row

    first, second, row = asyncio.run(_run())
    assert first is True
    assert second is False
    assert row["origin_delivered_at"] is not None


def test_non_task_run_is_never_claimable(tmp_path):
    _, cs, ers = _setup(tmp_path)

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
        created = await ers.create(
            profile="p1", source_kind="schedule", subscription_id="s2",
            conversation_id=conv["id"], label="L", action="a",
        )
        rid = created["run"]["id"]
        await ers.update_status(rid, status="completed", mark_finished=True)
        return await ers.claim_delivery(rid), await ers.list_undelivered_task_runs()

    claimed, undelivered = asyncio.run(_run())
    assert claimed is False
    assert undelivered == []


def test_running_task_run_is_not_deliverable_yet(tmp_path):
    """A pending run still owes an answer — delivering now would be premature."""
    _, cs, ers = _setup(tmp_path)

    async def _run():
        rid, _, _ = await _task_run(cs, ers, status="pending")
        return await ers.claim_delivery(rid), await ers.list_undelivered_task_runs()

    claimed, undelivered = asyncio.run(_run())
    assert claimed is False
    assert undelivered == []


def test_clear_claim_lets_a_failed_injection_retry(tmp_path):
    _, cs, ers = _setup(tmp_path)

    async def _run():
        rid, _, _ = await _task_run(cs, ers)
        await ers.claim_delivery(rid)
        await ers.clear_delivery_claim(rid)
        return await ers.claim_delivery(rid)

    assert asyncio.run(_run()) is True


def test_undelivered_task_result_survives_history_pruning(tmp_path):
    """The retention cap must never drop a result a conversation is waiting on."""
    _, cs, ers = _setup(tmp_path)

    async def _run():
        origin = (await cs.create_conversation(profile="p1", title="Chat"))["id"]
        # An undelivered task result, then enough terminal runs on the SAME rule
        # to push it past a cap of 1.
        owed, _, _ = await _task_run(cs, ers, origin=origin, cap=1)
        for _ in range(3):
            await _task_run(cs, ers, origin=origin, cap=1)
        return await ers.get(owed)

    assert asyncio.run(_run()) is not None


def test_delivered_runs_prune_normally(tmp_path):
    """Once handed over, a task run is ordinary history and may be pruned."""
    _, cs, ers = _setup(tmp_path)

    async def _run():
        origin = (await cs.create_conversation(profile="p1", title="Chat"))["id"]
        old, _, _ = await _task_run(cs, ers, origin=origin, cap=1)
        await ers.claim_delivery(old)
        for _ in range(3):
            newer, _, _ = await _task_run(cs, ers, origin=origin, cap=1)
            await ers.claim_delivery(newer)
        return await ers.get(old)

    assert asyncio.run(_run()) is None


def test_restart_leaves_an_interrupted_task_deliverable(tmp_path):
    """Boot recovery turns a killed run into a *failure the user hears about*."""
    _, cs, ers = _setup(tmp_path)

    async def _run():
        rid, _, _ = await _task_run(cs, ers, status="running")
        await ers.recover_after_restart()
        rows = await ers.list_undelivered_task_runs()
        return rid, rows

    rid, rows = asyncio.run(_run())
    assert [r["id"] for r in rows] == [rid]
    assert rows[0]["status"] == "failed"
    assert rows[0]["deliver_to_origin"] is True


def test_timed_out_task_run_needs_no_conversation(tmp_path):
    """A deadline produces no agent run, but still a deliverable record."""
    _, cs, ers = _setup(tmp_path)

    async def _run():
        origin = (await cs.create_conversation(profile="p1", title="Chat"))["id"]
        created = await ers.create(
            profile="p1", source_kind="file_watcher", subscription_id="fw-1",
            conversation_id=None, label="err-log", action="report the error",
            trigger_payload={"timed_out": True},
            origin_conversation_id=origin, deliver_to_origin=True,
            status="failed", error="deadline passed", finished=True,
        )
        return created["run"], await ers.list_undelivered_task_runs()

    run, undelivered = asyncio.run(_run())
    assert run["conversation_id"] is None
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert [r["id"] for r in undelivered] == [run["id"]]


def test_has_live_run_for_subscription(tmp_path):
    """Used by the boot reconcile to tell "still working" from "orphaned"."""
    _, cs, ers = _setup(tmp_path)

    async def _run():
        rid, _, _ = await _task_run(cs, ers, sub_id="sub-live")
        owed = await ers.has_live_run_for_subscription("skill_event", "sub-live")
        await ers.claim_delivery(rid)
        settled = await ers.has_live_run_for_subscription("skill_event", "sub-live")
        none_at_all = await ers.has_live_run_for_subscription("skill_event", "sub-x")
        return owed, settled, none_at_all

    owed, settled, none_at_all = asyncio.run(_run())
    assert owed is True        # terminal but not handed over yet
    assert settled is False
    assert none_at_all is False
