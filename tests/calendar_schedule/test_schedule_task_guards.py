"""Edits a schedule task must refuse, because they would break a promise.

A task is bound to a conversation that is blocked until it fires. Two ordinary
calendar edits would silently void that: adding a recurrence (recurring rules
never report back) and pausing (resume re-seeds the fire time from now, so a
moment that passed while paused just flips to 'completed' without firing).
Both are refused at the provider so every caller — API, CLI, UI — gets it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

from app.calendar.provider import InternalCalendarProvider  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.schedule_event_storage import ScheduleEventSubscriptionStorage  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "schedule_event_subscriptions")


class _NoopManager:
    def arm(self, row): return True
    def refresh(self, sub_id): return None
    def remove(self, sub_id): return None


def _provider(tmp_path: Path, monkeypatch):
    db = SqliteDatabaseProvider(str(tmp_path / "sched.db"))
    eng = db.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('pid','p1',0,0)"
        ))
        c.execute(text(
            "INSERT INTO conversations (id, profile, kind, title, "
            "compaction_watermark, created_at, updated_at) "
            "VALUES ('c1','p1','chat','Chat',-1,0,0)"
        ))
    provider = InternalCalendarProvider.__new__(InternalCalendarProvider)
    provider._store = ScheduleEventSubscriptionStorage(db)
    monkeypatch.setattr(provider, "_manager", lambda: _NoopManager(), raising=False)
    return provider


def _make(provider, *, task=True, rrule=None, dtstart="2099-01-01T09:00:00"):
    return provider.create_event(
        profile="p1", conversation_id="c1", title="check CI",
        action="report the result", source="agent",
        schedule_kind="recurrence" if rrule else "instant",
        dtstart=dtstart, duration_minutes=30, rrule=rrule, task=task,
    )


def test_a_future_one_shot_is_stamped_as_a_task(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch)
    assert _make(provider)["task"] is True


def test_a_past_one_shot_is_never_a_task(tmp_path, monkeypatch):
    """It is stored already completed, so nothing would ever be delivered."""
    provider = _provider(tmp_path, monkeypatch)
    row = _make(provider, dtstart="2000-01-01T09:00:00")
    assert row["status"] == "completed"
    assert row["task"] is False


def test_a_recurrence_is_never_a_task_even_if_asked(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch)
    assert _make(provider, rrule="FREQ=DAILY")["task"] is False


def test_adding_a_recurrence_to_a_task_is_refused(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch)
    row = _make(provider)
    with pytest.raises(ValueError, match="task_recurrence_conflict"):
        provider.update_event(row["id"], rrule="FREQ=DAILY")
    # Unchanged: still a one-shot that will report back.
    assert provider._store.get(row["id"])["rrule"] is None


def test_editing_a_non_task_into_a_recurrence_still_works(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch)
    row = _make(provider, task=False)
    provider.update_event(row["id"], rrule="FREQ=DAILY")
    assert provider._store.get(row["id"])["rrule"] == "FREQ=DAILY"


def test_pausing_a_task_is_refused(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch)
    row = _make(provider)
    with pytest.raises(ValueError, match="task_pause_unsupported"):
        provider.set_status(row["id"], "paused")
    assert provider._store.get(row["id"])["status"] == "active"


def test_cancelling_a_task_is_allowed(tmp_path, monkeypatch):
    """The user is entitled to abandon the wait — just not to suspend it."""
    provider = _provider(tmp_path, monkeypatch)
    row = _make(provider)
    provider.set_status(row["id"], "cancelled")
    assert provider._store.get(row["id"])["status"] == "cancelled"


def test_pausing_an_ordinary_schedule_still_works(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch)
    row = _make(provider, task=False, rrule="FREQ=DAILY")
    provider.set_status(row["id"], "paused")
    assert provider._store.get(row["id"])["status"] == "paused"


def test_claim_one_shot_consumes_a_row_once(tmp_path, monkeypatch):
    """The fire-time claim that stops a duplicate heap entry double-delivering."""
    provider = _provider(tmp_path, monkeypatch)
    row = _make(provider)
    store = provider._store
    assert store.claim_one_shot(row["id"], occurrences_fired=1) is True
    assert store.claim_one_shot(row["id"], occurrences_fired=2) is False
    after = store.get(row["id"])
    assert after["status"] == "completed"
    assert after["next_fire_at"] is None
    assert after["occurrences_fired"] == 1
