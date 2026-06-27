"""Live integration test for the ScheduleManager fire/advance loop.

Drives the real manager on a real event loop against real storage: a due event
fires (enqueues an agent run), then the rolling ``next_fire_at`` pointer advances
to the next occurrence — or the series completes for a bounded rule. The agent
queue + admin-SSE publish are stubbed so the test needs neither an LLM nor the
API layer.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from a2a.server.models import Base
import app.storage.models  # noqa: F401 — registers tables on Base.metadata
import app.events.schedule_manager as sm
from app.calendar import recurrence as R
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.schedule_event_storage import ScheduleEventSubscriptionStorage

_TABLES = ("profiles", "channels", "conversations", "schedule_event_subscriptions")


def _make_store(tmp_path: Path) -> ScheduleEventSubscriptionStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "sm.db"))
    engine = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)
    return ScheduleEventSubscriptionStorage(provider)


def _seed(store: ScheduleEventSubscriptionStorage, *, profile="admin", conv="c1") -> None:
    from sqlalchemy import text

    now = time.time()
    with store._engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('p', :profile, :now, :now)"
        ), {"profile": profile, "now": now})
        conn.execute(text(
            "INSERT INTO conversations (id, profile, title, created_at, updated_at) "
            "VALUES (:conv, :profile, 't', :now, :now)"
        ), {"conv": conv, "profile": profile, "now": now})


def _wire(monkeypatch, store):
    """Point the manager at our temp store, force feature on, capture enqueues."""
    recorded: list[dict] = []

    async def fake_enqueue(**kw):
        recorded.append(kw)

    monkeypatch.setattr(sm, "get_schedule_event_storage", lambda *a, **k: store)
    monkeypatch.setattr(sm, "feature_enabled", lambda profile: True)
    monkeypatch.setattr(sm.event_queue, "enqueue_schedule_event", fake_enqueue)
    monkeypatch.setattr(sm.ScheduleManager, "_publish_admin_changed", staticmethod(lambda profile: None))
    return recorded


def test_manager_fires_and_advances_recurrence(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    _seed(store)
    recorded = _wire(monkeypatch, store)

    now = time.time()
    fire_at = now + 0.3
    dtstart = R.format_local(R.from_epoch(fire_at))
    row = store.insert(
        conversation_id="c1", profile="admin", title="daily job", action="do the thing",
        schedule_kind="recurrence", dtstart=dtstart,
        duration_minutes=30, next_fire_at=fire_at, rrule="FREQ=DAILY",
        recurrence_end_type="never",
    )

    async def run():
        loop = asyncio.get_running_loop()
        mgr = sm.ScheduleManager()
        mgr.start(loop)
        await asyncio.sleep(0.9)
        mgr.stop()

    asyncio.run(run())

    # Fired exactly once (no burst), then advanced ~1 day ahead and stayed active.
    assert len(recorded) == 1
    assert recorded[0]["subscription_id"] == row["id"]
    assert recorded[0]["action"] == "do the thing"
    updated = store.get(row["id"])
    assert updated["status"] == "active"
    assert updated["occurrences_fired"] == 1
    assert updated["next_fire_at"] is not None
    assert updated["next_fire_at"] > now + 3600  # next day, not an immediate re-fire


def test_manager_completes_one_shot(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    _seed(store)
    recorded = _wire(monkeypatch, store)

    now = time.time()
    fire_at = now + 0.3
    dtstart = R.format_local(R.from_epoch(fire_at))
    row = store.insert(
        conversation_id="c1", profile="admin", title="one shot", action="ping once",
        schedule_kind="instant", dtstart=dtstart,
        duration_minutes=30, next_fire_at=fire_at, rrule=None,
    )

    async def run():
        loop = asyncio.get_running_loop()
        mgr = sm.ScheduleManager()
        mgr.start(loop)
        await asyncio.sleep(0.9)
        mgr.stop()

    asyncio.run(run())

    assert len(recorded) == 1
    updated = store.get(row["id"])
    assert updated["status"] == "completed"
    assert updated["next_fire_at"] is None
    assert updated["occurrences_fired"] == 1


def test_manager_runs_action_falling_back_to_title(tmp_path, monkeypatch):
    # Reminder mode removed: an event with NO explicit action still RUNS — the
    # title is used as the command (so a bare "tắt đèn hiên" executes).
    store = _make_store(tmp_path)
    _seed(store)
    recorded = _wire(monkeypatch, store)

    now = time.time()
    fire_at = now + 0.3
    dtstart = R.format_local(R.from_epoch(fire_at))
    row = store.insert(
        conversation_id="c1", profile="admin", title="tắt đèn hiên", action="",
        schedule_kind="instant", dtstart=dtstart,
        duration_minutes=30, next_fire_at=fire_at, rrule=None,
    )

    async def run():
        loop = asyncio.get_running_loop()
        mgr = sm.ScheduleManager()
        mgr.start(loop)
        await asyncio.sleep(0.9)
        mgr.stop()

    asyncio.run(run())

    # Enqueued an agent run in the bound conversation, with action == title.
    assert len(recorded) == 1
    assert recorded[0]["conversation_id"] == "c1"
    assert recorded[0]["subscription_id"] == row["id"]
    assert recorded[0]["action"] == "tắt đèn hiên"


def test_manager_skips_disabled_profile(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    _seed(store)
    recorded: list[dict] = []

    async def fake_enqueue(**kw):
        recorded.append(kw)

    monkeypatch.setattr(sm, "get_schedule_event_storage", lambda *a, **k: store)
    monkeypatch.setattr(sm, "feature_enabled", lambda profile: False)  # feature OFF
    monkeypatch.setattr(sm.event_queue, "enqueue_schedule_event", fake_enqueue)
    monkeypatch.setattr(sm.ScheduleManager, "_publish_admin_changed", staticmethod(lambda profile: None))

    fire_at = time.time() + 0.2
    store.insert(
        conversation_id="c1", profile="admin", title="should not fire", action="nope",
        schedule_kind="instant",
        dtstart=R.format_local(R.from_epoch(fire_at)), duration_minutes=30,
        next_fire_at=fire_at, rrule=None,
    )

    async def run():
        loop = asyncio.get_running_loop()
        mgr = sm.ScheduleManager()
        mgr.start(loop)
        await asyncio.sleep(0.6)
        mgr.stop()

    asyncio.run(run())
    assert recorded == []  # disabled profile: nothing armed, nothing fired
