"""EventRunStorage: status transitions, retention pruning, usage survival, boot recovery."""

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
from app.storage.usage_storage import UsageStorage  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages", "usage_records", "event_runs",
)


def _providers(tmp_path: Path):
    provider = SqliteDatabaseProvider(str(tmp_path / "er.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('pid','p1',0,0)"))
    return provider


async def _new_conv(cs: ConversationStorage, kind="event_run") -> str:
    conv = await cs.create_conversation(profile="p1", title="run", kind=kind)
    return conv["id"]


def test_status_transitions(tmp_path: Path) -> None:
    provider = _providers(tmp_path)
    cs = ConversationStorage(provider); cs._initialized = True
    ers = EventRunStorage(provider)

    async def run():
        conv = await _new_conv(cs)
        created = await ers.create(
            profile="p1", source_kind="schedule", subscription_id="s1",
            conversation_id=conv, label="L", action="a",
        )
        rid = created["run"]["id"]
        assert created["run"]["status"] == "running"

        await ers.update_status(rid, status="pending", pending_question="path?", run_id="event:x")
        got = await ers.get(rid)
        assert got["status"] == "pending"
        assert got["pending_question"] == "path?"
        assert got["finished_at"] is None

        # reply resumes → running (pending cleared), then completes
        await ers.update_status(rid, status="running", clear_pending=True, increment_turn=True)
        got = await ers.get(rid)
        assert got["status"] == "running" and got["pending_question"] is None
        await ers.update_status(rid, status="completed", increment_turn=True, mark_finished=True)
        got = await ers.get(rid)
        assert got["status"] == "completed"
        assert got["finished_at"] is not None
        assert got["turn_count"] == 2

    asyncio.run(run())


def test_retention_prunes_terminal_only_and_keeps_usage(tmp_path: Path) -> None:
    provider = _providers(tmp_path)
    cs = ConversationStorage(provider); cs._initialized = True
    ers = EventRunStorage(provider)
    us = UsageStorage(provider)

    async def run():
        conv_ids = []
        for i in range(6):
            conv = await _new_conv(cs)
            created = await ers.create(
                profile="p1", source_kind="skill_event", subscription_id="cap",
                conversation_id=conv, label="L", action="a", history_cap=3,
            )
            rid = created["run"]["id"]
            await ers.update_status(rid, status="completed", mark_finished=True)
            await us.add_usage_records(conv, "p1",
                [{"source_kind": "reasoning", "input_tokens": 10, "output_tokens": 5}],
                message_id=None, event_run_id=rid)
            conv_ids.append(conv)

        _, total = await ers.list(subscription_id="cap")
        # cap counts terminal runs; the just-inserted run is running at prune time,
        # so a small transient overshoot above the cap is expected but bounded.
        assert total <= 4

        # usage from ALL 6 runs survives pruning (rows outlive the run/conv).
        tot = await us.totals(profile="p1")
        assert tot["total_tokens"] == 6 * 15

    asyncio.run(run())


def test_never_prune_active_runs(tmp_path: Path) -> None:
    provider = _providers(tmp_path)
    cs = ConversationStorage(provider); cs._initialized = True
    ers = EventRunStorage(provider)

    async def run():
        # Many pending/running runs must never be pruned even past the cap.
        for _ in range(5):
            conv = await _new_conv(cs)
            created = await ers.create(
                profile="p1", source_kind="skill_event", subscription_id="act",
                conversation_id=conv, label="L", action="a", history_cap=2,
            )
            await ers.update_status(created["run"]["id"], status="pending", pending_question="q")
        _, total = await ers.list(subscription_id="act")
        assert total == 5, "pending runs must not be pruned"

    asyncio.run(run())


def test_boot_recovery(tmp_path: Path) -> None:
    provider = _providers(tmp_path)
    cs = ConversationStorage(provider); cs._initialized = True
    ers = EventRunStorage(provider)

    async def run():
        c1 = await _new_conv(cs)
        c2 = await _new_conv(cs)
        r_running = await ers.create(profile="p1", source_kind="schedule", subscription_id="s",
                                     conversation_id=c1, label="L", action="a")
        r_pending = await ers.create(profile="p1", source_kind="schedule", subscription_id="s",
                                     conversation_id=c2, label="L", action="a")
        await ers.update_status(r_pending["run"]["id"], status="pending", pending_question="q")

        fixed = await ers.recover_after_restart()
        assert fixed == 1
        assert (await ers.get(r_running["run"]["id"]))["status"] == "failed"
        pend = await ers.get(r_pending["run"]["id"])
        assert pend["status"] == "pending", "pending must survive restart"

    asyncio.run(run())
