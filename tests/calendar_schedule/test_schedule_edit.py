"""ScheduleEditTool.run() edits an existing event in place via provider.update_event.

Covers the guards that keep the agent on the edit path (rather than the
cancel+recreate footgun): feature gating, unknown id, no-op calls, field
pass-through, the self-containment gate on a changed action, and the Google
sub-daily-recurrence block.
"""

from __future__ import annotations

import asyncio

import app.calendar.provider as P
import app.tools.builtin.scheduler_actions as SA
from app.events.action_check import ActionCheckResult


class _FakeProvider:
    def __init__(self, *, name="internal", rows=None):
        self.name = name
        self.rows = rows if rows is not None else [
            {"id": "evt1", "title": "Old title", "conversation_id": "conv1",
             "action": "Do the old thing", "status": "active"},
        ]
        self.updated = None

    def list_subscriptions(self, profile):
        return self.rows

    def update_event(self, event_id, **fields):
        self.updated = (event_id, fields)
        base = next((r for r in self.rows if r["id"] == event_id), {}).copy()
        base.update(fields)
        base.setdefault("next_fire_at", 456.0)
        base.setdefault("status", "active")
        base.setdefault("rrule", fields.get("rrule"))
        return base


def _drive(arguments, provider, monkeypatch, *, gate=None, feature_on=True):
    monkeypatch.setattr(SA, "calendar_schedule_enabled", lambda profile=None: feature_on)
    monkeypatch.setattr(SA, "_publish_changed", lambda profile: None)
    monkeypatch.setattr(P, "get_calendar_provider", lambda profile: provider)
    if gate is not None:
        monkeypatch.setattr("app.events.action_check.gate_registration_action", gate)
    args = {"_profile": "p", "_context_id": "ctx", **arguments}
    return asyncio.run(SA.ScheduleEditTool().run(args)).structured_content


def test_edit_disabled_when_feature_off(monkeypatch):
    prov = _FakeProvider()
    out = _drive({"event_id": "evt1", "title": "x"}, prov, monkeypatch, feature_on=False)
    assert out["ok"] is False
    assert out["error"] == "feature_disabled"
    assert prov.updated is None


def test_edit_unknown_id_not_found(monkeypatch):
    prov = _FakeProvider(rows=[])
    out = _drive({"event_id": "missing", "title": "x"}, prov, monkeypatch)
    assert out["ok"] is False
    assert out["error"] == "not_found"
    assert prov.updated is None


def test_edit_no_fields(monkeypatch):
    prov = _FakeProvider()
    out = _drive({"event_id": "evt1"}, prov, monkeypatch)
    assert out["ok"] is False
    assert out["error"] == "no_fields"
    assert prov.updated is None


def test_edit_passes_only_provided_fields(monkeypatch):
    prov = _FakeProvider()
    out = _drive(
        {"event_id": "evt1", "action": "Do the NEW thing", "title": "  "},
        prov, monkeypatch,
        gate=_accept,
    )
    assert out["ok"] is True
    assert out["id"] == "evt1"
    event_id, fields = prov.updated
    assert event_id == "evt1"
    # Only the non-blank action was forwarded; the blank title was dropped.
    assert fields == {"action": "Do the NEW thing"}
    assert out["changed"] == ["action"]
    assert "unchanged" in out["message"]


def test_edit_action_runs_self_containment_gate(monkeypatch):
    prov = _FakeProvider()

    async def _reject(**kw):
        return ActionCheckResult(False, ["'the provided URL' — inline the full address"], "url missing")

    out = _drive(
        {"event_id": "evt1", "action": "Open the provided URL and email results."},
        prov, monkeypatch, gate=_reject,
    )
    assert out["ok"] is False
    assert out["error"] == "action_not_self_contained"
    assert prov.updated is None  # nothing persisted


def test_edit_promotes_kind_when_rrule_added(monkeypatch):
    prov = _FakeProvider()
    out = _drive(
        {"event_id": "evt1", "rrule": "FREQ=DAILY"},
        prov, monkeypatch,
    )
    assert out["ok"] is True
    _event_id, fields = prov.updated
    assert fields["rrule"] == "FREQ=DAILY"
    assert fields["schedule_kind"] == "recurrence"
    # schedule_kind is internal plumbing, not reported as a user-facing change.
    assert out["changed"] == ["rrule"]


def test_edit_google_subdaily_rrule_blocked(monkeypatch):
    prov = _FakeProvider(name="google")
    monkeypatch.setattr(P, "google_supports_rrule", lambda rrule: False)
    out = _drive(
        {"event_id": "evt1", "rrule": "FREQ=MINUTELY"},
        prov, monkeypatch,
    )
    assert out["ok"] is False
    assert out["error"] == "google_unsupported_recurrence"
    assert prov.updated is None


def test_edit_google_subdaily_rrule_allowed_with_optin(monkeypatch):
    prov = _FakeProvider(name="google")
    monkeypatch.setattr(P, "google_supports_rrule", lambda rrule: False)
    out = _drive(
        {"event_id": "evt1", "rrule": "FREQ=MINUTELY", "allow_local_only": True},
        prov, monkeypatch,
    )
    assert out["ok"] is True
    assert prov.updated is not None


async def _accept(**kw):
    return None
