"""Which schedule events become one-shot EVENT TASKS, and which never can.

An agent-created one-time event *is* a wait — the user asked for "do this at
that moment and tell me" — so its result comes back to the conversation. The
exclusions are what stop a conversation from waiting on something that will
never report: recurrences, calendar blocks, past moments, and rows with no
conversation to return to.
"""

from __future__ import annotations

import asyncio

import pytest

import app.calendar.provider as P
import app.tools.builtin.scheduler_actions as SA


class _FakeProvider:
    def __init__(self):
        self.name = "internal"
        self.created = None

    def create_event(self, **kwargs):
        self.created = kwargs
        # Mirror the provider's own rule: a row that cannot fire is not a task.
        task = bool(kwargs.get("task")) and not kwargs.get("rrule")
        return {
            "id": "evt1", "schedule_kind": kwargs.get("schedule_kind"),
            "dtstart": kwargs["dtstart"], "next_fire_at": 123.0,
            "status": "active", "rrule": kwargs.get("rrule"), "task": task,
        }


def _drive(arguments, provider, monkeypatch, *, context_id="ctx"):
    monkeypatch.setattr(SA, "calendar_schedule_enabled", lambda profile=None: True)

    async def _fake_resolve(profile, cid):
        return "conv1"

    monkeypatch.setattr(SA, "_resolve_conversation_id", _fake_resolve)
    monkeypatch.setattr(SA, "_publish_changed", lambda profile: None)
    monkeypatch.setattr(P, "get_calendar_provider", lambda profile: provider)

    async def _accept(**kw):
        return None
    monkeypatch.setattr("app.events.action_check.gate_registration_action", _accept)

    args = {"_profile": "p", "_context_id": context_id, **arguments}
    return asyncio.run(SA.ScheduleCreateTool().run(args)).structured_content


_BASE = {"title": "check CI", "dtstart": "2026-07-10T09:00:00", "action": "report the result"}


def test_a_one_time_event_is_a_task(monkeypatch):
    prov = _FakeProvider()
    out = _drive(dict(_BASE), prov, monkeypatch)
    assert prov.created["task"] is True
    assert prov.created["schedule_kind"] == "instant"
    assert out["task"] is True
    # The model must know the outcome arrives later, and not wait for it.
    assert "back into THIS conversation" in out["message"]
    assert "Do NOT wait, sleep, or poll" in out["message"]


def test_a_recurring_event_is_not_a_task_but_still_reports_back(monkeypatch):
    """``task`` means ONE-SHOT, and a recurrence is not one.

    It still reports, though — every firing comes back to the chat that created
    it — so the confirmation has to say that, or the model tells the user their
    "every day at 8pm" digest will only show up on the Events page.
    """
    prov = _FakeProvider()
    out = _drive({**_BASE, "rrule": "FREQ=DAILY"}, prov, monkeypatch)
    assert prov.created["task"] is False
    assert out["task"] is False
    assert "reports the result back into THIS conversation" in out["message"]
    assert "do NOT come back to this chat" not in out["message"]
    assert "do NOT wait, sleep, or poll" in out["message"]


@pytest.mark.parametrize("extra", [
    {"all_day": True},
    {"duration_minutes": 120},
    {"end": "2026-07-10T18:00:00"},
])
def test_calendar_blocks_are_not_tasks(extra, monkeypatch):
    """A trip or a long block is an entry in a calendar, not an awaited outcome."""
    prov = _FakeProvider()
    out = _drive({**_BASE, **extra}, prov, monkeypatch)
    assert prov.created["task"] is False
    assert prov.created["schedule_kind"] == "interval"
    assert out["task"] is False


def test_an_event_with_no_conversation_is_not_a_task(monkeypatch):
    """The reserved __schedule__ owner has nowhere to deliver a result."""
    prov = _FakeProvider()
    out = _drive(dict(_BASE), prov, monkeypatch, context_id="")
    assert prov.created["task"] is False
    assert out["task"] is False


def test_the_result_reports_what_the_row_actually_says(monkeypatch):
    """Task-ness is read back from storage, never assumed from the request.

    The provider refuses to stamp an event that can never fire; if the tool
    trusted its own guess it would promise a result that never arrives.
    """
    class _RefusingProvider(_FakeProvider):
        def create_event(self, **kwargs):
            super().create_event(**kwargs)
            return {
                "id": "evt1", "schedule_kind": "instant",
                "dtstart": kwargs["dtstart"], "next_fire_at": None,
                "status": "completed", "rrule": None, "task": False,
            }

    out = _drive(dict(_BASE), _RefusingProvider(), monkeypatch)
    assert out["task"] is False
    # A row that can never fire promises nothing — the one failure this must
    # not have is telling the user to expect a report that never arrives.
    assert "will never fire" in out["message"]
    assert "nothing will be reported back" in out["message"]
