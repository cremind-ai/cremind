"""Tests for the schedule-event storage + the rolling-pointer recurrence model.

The headline guarantee of the Calendar & Schedule engine: an open-ended
recurrence is ONE durable row whose ``next_fire_at`` pointer advances after each
fire — it never materializes (and never "registers") an unbounded set of
occurrences. These tests simulate the ScheduleManager's fire/advance step
against real storage + the dateutil-backed recurrence math, so the contract the
manager relies on is locked in without spinning the async loop.

Harness mirrors ``tests/storage/test_memory_storage.py``: an on-disk SQLite
provider with the relevant tables created from the ORM metadata.
"""

from __future__ import annotations

import time
from pathlib import Path

from a2a.server.models import Base
import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.calendar import recurrence as R
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.schedule_event_storage import ScheduleEventSubscriptionStorage

_TABLES = ("profiles", "channels", "conversations", "schedule_event_subscriptions")


def _make_store(tmp_path: Path) -> ScheduleEventSubscriptionStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "sched.db"))
    engine = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)
    return ScheduleEventSubscriptionStorage(provider)


def _seed(store: ScheduleEventSubscriptionStorage, *, profile="admin", conv="c1") -> None:
    from sqlalchemy import text

    now = time.time()
    with store._engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at, skill_mode) "
            "VALUES ('p', :profile, :now, :now, 'manual')"
        ), {"profile": profile, "now": now})
        conn.execute(text(
            "INSERT INTO conversations (id, profile, title, created_at, updated_at, memory_watermark) "
            "VALUES (:conv, :profile, 't', :now, :now, 0)"
        ), {"conv": conv, "profile": profile, "now": now})


def _advance_once(store, sub):
    """Mimic ScheduleManager._fire's pointer step (without the agent run)."""
    occ_dt = R.from_epoch(float(sub["next_fire_at"]))
    until = sub.get("recurrence_end_value") if sub.get("recurrence_end_type") == "until" else None
    nxt = R.next_occurrence_after(
        rrule=sub.get("rrule"), dtstart=sub["dtstart"], after=occ_dt, until=until,
    )
    fired = int(sub.get("occurrences_fired", 0)) + 1
    if nxt is not None:
        store.update_next_fire(sub["id"], next_fire_at=R.to_epoch(nxt), occurrences_fired=fired)
    else:
        store.set_status(sub["id"], "completed", next_fire_at=None)
        store.update_next_fire(sub["id"], next_fire_at=None, occurrences_fired=fired)
    return occ_dt, nxt


def test_open_ended_recurrence_is_one_row_with_advancing_pointer(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = store.insert(
        conversation_id="c1", profile="admin", title="standup", action="check email",
        is_reminder_only=False, schedule_kind="recurrence", dtstart="2026-06-22T09:00:00",
        duration_minutes=30, next_fire_at=R.to_epoch(R.parse_local("2026-06-22T09:00:00")),
        rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", recurrence_end_type="never",
    )
    assert len(store.list_active()) == 1

    fires = []
    sub = store.get(row["id"])
    for _ in range(20):  # far more than a week — proves it never exhausts/explodes
        occ, nxt = _advance_once(store, sub)
        fires.append(occ)
        assert nxt is not None  # open-ended: always another occurrence
        sub = store.get(row["id"])

    # The crux: still exactly ONE row after 20 fires — no infinite registration.
    assert len(store.list_all()) == 1
    assert sub["occurrences_fired"] == 20
    # Every fire is a weekday, strictly increasing (no weekends, no duplicates).
    assert all(f.weekday() < 5 for f in fires)
    assert fires == sorted(fires)
    assert len(set(fires)) == len(fires)


def test_count_bounded_recurrence_completes(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = store.insert(
        conversation_id="c1", profile="admin", title="3-day reminder", action="ping",
        is_reminder_only=False, schedule_kind="recurrence", dtstart="2026-06-22T09:00:00",
        duration_minutes=30, next_fire_at=R.to_epoch(R.parse_local("2026-06-22T09:00:00")),
        rrule="FREQ=DAILY;COUNT=3", recurrence_end_type="count", recurrence_end_value="3",
    )
    sub = store.get(row["id"])
    fires = []
    for _ in range(3):
        occ, nxt = _advance_once(store, sub)
        fires.append(occ)
        sub = store.get(row["id"])
    assert len(fires) == 3
    # After the 3rd fire the series is exhausted: completed, no pending pointer.
    assert sub["status"] == "completed"
    assert sub["next_fire_at"] is None
    assert store.list_active() == []


def test_until_bounded_recurrence_completes(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = store.insert(
        conversation_id="c1", profile="admin", title="until-fri", action="ping",
        is_reminder_only=False, schedule_kind="recurrence", dtstart="2026-06-22T09:00:00",
        duration_minutes=30, next_fire_at=R.to_epoch(R.parse_local("2026-06-22T09:00:00")),
        rrule="FREQ=DAILY", recurrence_end_type="until", recurrence_end_value="2026-06-24T09:00:00",
    )
    sub = store.get(row["id"])
    fires = []
    for _ in range(5):
        if sub["next_fire_at"] is None:
            break
        occ, nxt = _advance_once(store, sub)
        fires.append(occ)
        sub = store.get(row["id"])
    # Mon, Tue, Wed (22/23/24) then stop — the UNTIL cutoff is inclusive of the 24th.
    assert [f.strftime("%Y-%m-%d") for f in fires] == ["2026-06-22", "2026-06-23", "2026-06-24"]
    assert sub["status"] == "completed"


def test_one_shot_does_not_advance(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = store.insert(
        conversation_id="c1", profile="admin", title="dentist", action="",
        is_reminder_only=True, schedule_kind="instant", dtstart="2026-06-22T09:00:00",
        duration_minutes=30, next_fire_at=R.to_epoch(R.parse_local("2026-06-22T09:00:00")),
        rrule=None,
    )
    sub = store.get(row["id"])
    occ, nxt = _advance_once(store, sub)
    sub = store.get(row["id"])
    assert nxt is None
    assert sub["status"] == "completed"
    assert sub["occurrences_fired"] == 1


def test_all_day_multiday_round_trips(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = store.insert(
        conversation_id="c1", profile="admin", title="trip", action="",
        is_reminder_only=True, all_day=True, schedule_kind="interval",
        dtstart="2026-06-22T00:00:00", duration_minutes=3 * 1440,
        next_fire_at=R.to_epoch(R.parse_local("2026-06-22T00:00:00")),
    )
    assert row["all_day"] is True
    got = store.get(row["id"])
    assert got["all_day"] is True
    assert got["duration_minutes"] == 3 * 1440
    # editable via update_fields
    store.update_fields(row["id"], all_day=False)
    assert store.get(row["id"])["all_day"] is False


def test_crud_update_and_delete(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = store.insert(
        conversation_id="c1", profile="admin", title="t", action="a",
        is_reminder_only=False, schedule_kind="instant", dtstart="2026-06-22T09:00:00",
        duration_minutes=30, next_fire_at=R.to_epoch(R.parse_local("2026-06-22T09:00:00")),
    )
    updated = store.update_fields(row["id"], title="renamed", duration_minutes=60)
    assert updated["title"] == "renamed"
    assert updated["duration_minutes"] == 60
    store.set_status(row["id"], "paused", next_fire_at=None)
    assert store.get(row["id"])["status"] == "paused"
    assert store.list_active() == []
    assert store.delete(row["id"]) is True
    assert store.get(row["id"]) is None
