"""ScheduleCreateTool.run() applies the self-containment gate before persisting.

A rejection returns ``action_not_self_contained`` and creates nothing; a pass
(or fail-open when no LLM is wired) creates the event as before.
"""

from __future__ import annotations

import asyncio

import app.calendar.provider as P
import app.events.action_check as ac
import app.tools.builtin.scheduler_actions as SA
from app.events.action_check import ActionCheckResult


class _FakeProvider:
    def __init__(self):
        self.name = "internal"
        self.created = None

    def create_event(self, **kwargs):
        self.created = kwargs
        return {
            "id": "evt1", "schedule_kind": kwargs.get("schedule_kind"),
            "dtstart": kwargs["dtstart"], "next_fire_at": 123.0,
            "status": "active", "rrule": kwargs.get("rrule"),
        }


def _drive(arguments, provider, monkeypatch, *, gate=None):
    monkeypatch.setattr(SA, "calendar_schedule_enabled", lambda profile=None: True)

    async def _fake_resolve(profile, context_id):
        return "conv1"

    monkeypatch.setattr(SA, "_resolve_conversation_id", _fake_resolve)
    monkeypatch.setattr(SA, "_publish_changed", lambda profile: None)
    monkeypatch.setattr(P, "get_calendar_provider", lambda profile: provider)
    if gate is not None:
        monkeypatch.setattr("app.events.action_check.gate_registration_action", gate)
    args = {"_profile": "p", "_context_id": "ctx", **arguments}
    return asyncio.run(SA.ScheduleCreateTool().run(args)).structured_content


def test_gate_reject_blocks_create(monkeypatch):
    prov = _FakeProvider()

    async def _reject(**kw):
        return ActionCheckResult(False, ["'the provided URL' — inline the full address"], "url missing")

    out = _drive(
        {"title": "check", "dtstart": "2026-07-10T09:00:00",
         "action": "Open the provided URL and email results."},
        prov, monkeypatch, gate=_reject,
    )
    assert out["ok"] is False
    assert out["error"] == "action_not_self_contained"
    assert out["missing"] and "inline" in out["missing"][0]
    assert "not self-contained" in out["message"]
    assert prov.created is None  # nothing persisted


def test_gate_pass_creates(monkeypatch):
    prov = _FakeProvider()

    async def _accept(**kw):
        return None

    out = _drive(
        {"title": "check", "dtstart": "2026-07-10T09:00:00",
         "action": "Open https://example.com/x and email li@olli-ai.com."},
        prov, monkeypatch, gate=_accept,
    )
    assert out["ok"] is True
    assert prov.created is not None
    assert prov.created["action"].startswith("Open https://example.com/x")


def test_gate_fail_open_when_no_agent(monkeypatch):
    # Real gate_registration_action, but no server agent wired → fail open → created.
    prov = _FakeProvider()
    monkeypatch.setattr("app.events.runner.get_cremind_agent", lambda: None)
    monkeypatch.setattr(ac, "record_action_check_usage", _noop)
    out = _drive(
        {"title": "check", "dtstart": "2026-07-10T09:00:00",
         "action": "Open the provided URL."},
        prov, monkeypatch,  # no gate override → uses the real one
    )
    assert out["ok"] is True
    assert prov.created is not None


async def _noop(**kw):
    return None
