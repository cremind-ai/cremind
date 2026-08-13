"""What unlinking the *skill* must not touch.

Cremind has two Google credentials for the calendar: the gcalendar skill's token
file (which wins) and the Calendar & Schedule page's own ``auth_tokens`` rows
(the dormant fallback). Unlinking the skill has to hand the calendar *back* to
those rows — so deleting them, or the ``google_mirror_target`` record, would
destroy the whole point of the two-source design.

``google_mirror_target`` in particular survives even a real page-side disconnect,
because it describes events that are still sitting in someone's Google calendar.
And ``external_event_id``s are deliberately *not* cleared here: the provider's own
``_reconcile_mirror_target`` clears them on the next load if the account actually
changed, whereas clearing them now would re-mirror — and so duplicate — every
event if the user re-links the same account.
"""

from __future__ import annotations

import asyncio

import pytest

import app.google.unlink as U
from app.google.registry import by_name


@pytest.fixture
def tripwires(monkeypatch):
    """Explode if the unlink reaches for any of the page's own state."""
    calls = []

    def boom(name):
        def _explode(*_a, **_k):
            raise AssertionError(f"unlink must not call {name}")

        return _explode

    monkeypatch.setattr("app.calendar.google_auth.disconnect", boom("google_auth.disconnect"))
    monkeypatch.setattr(
        "app.utils.client_storage.DatabaseClientStorage.delete_token", boom("delete_token")
    )
    monkeypatch.setattr(
        "app.utils.client_storage.DatabaseClientStorage.delete_tokens_for_profile",
        boom("delete_tokens_for_profile"),
    )
    monkeypatch.setattr(
        "app.storage.schedule_event_storage.ScheduleEventSubscriptionStorage.clear_external_refs",
        boom("clear_external_refs"),
    )
    return calls


def test_unlinking_the_skill_leaves_the_pages_credential_alone(google, tripwires):
    google.link("alice", "gcalendar")

    result = asyncio.run(U.unlink_skill("alice", by_name("gcalendar")))

    assert result["unlinked"] is True


def test_unlink_all_leaves_the_pages_credential_alone(google, tripwires):
    for skill in ("gcalendar", "gdrive", "gmail"):
        google.link("alice", skill)

    out = asyncio.run(U.unlink_all("alice"))

    assert out["failed"] == []


def test_the_calendar_falls_back_to_the_page_credential(google, monkeypatch):
    monkeypatch.setattr(U, "_calendar_source", lambda profile: "app")
    google.link("alice", "gcalendar")

    result = asyncio.run(U.unlink_skill("alice", by_name("gcalendar")))

    assert result["calendar_source_after"] == "app"
    assert "account connected on that page" in result["message"]


def test_the_calendar_falls_back_to_the_internal_provider(google, monkeypatch):
    monkeypatch.setattr(U, "_calendar_source", lambda profile: None)
    google.link("alice", "gcalendar")

    result = asyncio.run(U.unlink_skill("alice", by_name("gcalendar")))

    assert result["calendar_source_after"] is None
    assert "built-in system calendar" in result["message"]


def test_only_gcalendar_reports_a_calendar_source(google):
    """The other four skills have nothing to do with the calendar page."""
    for skill in ("gmail", "gsheets", "gdocs", "gdrive"):
        google.link("alice", skill)

        result = asyncio.run(U.unlink_skill("alice", by_name(skill)))

        assert result["calendar_source_after"] is None, skill
        assert "Calendar & Schedule" not in result["message"], skill


def test_the_consequence_warns_that_mirrored_events_stay_in_google(google):
    """Deleting them is not ours to do, so the copy must not imply we did."""
    assert "stay in" in by_name("gcalendar").consequence
