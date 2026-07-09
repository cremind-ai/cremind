"""Tests for ScheduleCreateTool.run(): the sub-daily warn when Google is the
active provider, its allow_local_only escape hatch, and the stale-dtstart
re-anchor. The provider, feature flag, conversation resolve, and SSE publish are
all faked, so these are pure-logic tests (no DB, no network, no Google)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import app.calendar.provider as P
import app.tools.builtin.scheduler_actions as SA
from app.calendar import recurrence as R


class _FakeProvider:
    def __init__(self, name):
        self.name = name
        self.created = None

    def create_event(self, **kwargs):
        self.created = kwargs
        return {
            "id": "evt1", "schedule_kind": kwargs.get("schedule_kind"),
            "dtstart": kwargs["dtstart"], "next_fire_at": 123.0,
            "status": "active", "rrule": kwargs.get("rrule"),
        }


def _drive(arguments, provider, monkeypatch):
    monkeypatch.setattr(SA, "calendar_schedule_enabled", lambda profile=None: True)

    async def _fake_resolve(profile, context_id):
        return "conv1"

    monkeypatch.setattr(SA, "_resolve_conversation_id", _fake_resolve)
    monkeypatch.setattr(SA, "_publish_changed", lambda profile: None)
    monkeypatch.setattr(P, "get_calendar_provider", lambda profile: provider)
    # The self-containment gate is exercised in test_schedule_create_action_gate.py;
    # here always accept so these logic tests don't depend on a live LLM.
    async def _accept(**kw):
        return None
    monkeypatch.setattr("app.events.action_check.gate_registration_action", _accept)
    args = {"_profile": "p", "_context_id": "ctx", **arguments}
    return asyncio.run(SA.ScheduleCreateTool().run(args)).structured_content


def test_warns_on_subdaily_when_google_connected(monkeypatch):
    prov = _FakeProvider("google")
    out = _drive(
        {"title": "meds", "dtstart": "2026-06-20T14:30:00", "rrule": "FREQ=HOURLY;INTERVAL=2"},
        prov, monkeypatch,
    )
    assert out["ok"] is False
    assert out["error"] == "google_unsupported_recurrence"
    assert prov.created is None  # nothing created


def test_allow_local_only_bypasses_warning(monkeypatch):
    prov = _FakeProvider("google")
    out = _drive(
        {"title": "meds", "dtstart": "2026-06-20T14:30:00",
         "rrule": "FREQ=HOURLY;INTERVAL=2", "allow_local_only": True},
        prov, monkeypatch,
    )
    assert out["ok"] is True
    assert prov.created is not None
    assert prov.created["rrule"] == "FREQ=HOURLY;INTERVAL=2"


def test_subdaily_ok_when_google_not_connected(monkeypatch):
    prov = _FakeProvider("internal")
    out = _drive(
        {"title": "meds", "dtstart": "2026-06-20T14:30:00", "rrule": "FREQ=HOURLY;INTERVAL=2"},
        prov, monkeypatch,
    )
    assert out["ok"] is True
    assert prov.created is not None


def test_daily_recurrence_not_warned_when_google(monkeypatch):
    prov = _FakeProvider("google")
    out = _drive(
        {"title": "standup", "dtstart": "2026-07-10T09:00:00", "rrule": "FREQ=DAILY"},
        prov, monkeypatch,
    )
    assert out["ok"] is True
    assert prov.created is not None


def test_reanchors_stale_past_dtstart(monkeypatch):
    prov = _FakeProvider("internal")
    now_before = datetime.now().replace(microsecond=0)
    out = _drive(
        {"title": "meds", "dtstart": "2024-06-08T23:19:00", "rrule": "FREQ=DAILY"},
        prov, monkeypatch,
    )
    assert out["ok"] is True
    used = prov.created["dtstart"]
    assert used != "2024-06-08T23:19:00"  # not the stale 2024 anchor
    assert R.parse_local(used) >= now_before  # rolled forward to at/after now
