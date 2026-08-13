"""What the inventory reports — the payload every surface renders.

The consequence sentences and the shared-grant groupings live in the backend on
purpose: the CLI caveat, the UI confirm dialog and the API message are all built
from this, so they cannot end up describing the same account three different ways.
"""

from __future__ import annotations

import pytest

import app.google.unlink as U
from app.google.registry import GOOGLE_SKILLS, by_name


def test_every_google_skill_is_reported_even_when_absent(google):
    out = U.inventory("alice")

    assert [row["skill"] for row in out["skills"]] == [s.dir_name for s in GOOGLE_SKILLS]
    assert all(row["installed"] is False for row in out["skills"])
    assert all(row["linked"] is False for row in out["skills"])
    assert out["accounts"] == []
    assert out["revoke_url"] == "https://myaccount.google.com/connections"


def _row(out, skill):
    return next(row for row in out["skills"] if row["skill"] == skill)


def test_a_linked_skill_reports_its_account(google):
    google.link("alice", "gmail", email="u@example.com")

    row = _row(U.inventory("alice"), "gmail")

    assert row["installed"] is True
    assert row["linked"] is True
    assert row["email"] == "u@example.com"
    assert row["label"] == "Gmail"
    assert row["tool_id"] == "alice__gmail"
    assert row["consequence"] == by_name("gmail").consequence


def test_a_linked_but_disabled_skill_is_reported_as_both(google, monkeypatch):
    """A disabled skill still holds a live credential — which is why the link is
    keyed on the directory, not on registry presence."""
    monkeypatch.setattr(U, "skill_enabled", lambda profile, spec: False)
    google.link("alice", "gcalendar")

    row = _row(U.inventory("alice"), "gcalendar")

    assert row["linked"] is True
    assert row["enabled"] is False


def test_accounts_are_grouped_and_shared_grants_flagged(google):
    google.link("alice", "gmail", email="same@example.com")
    google.link("alice", "gcalendar", email="same@example.com")
    google.link("alice", "gdocs", email="other@example.com")

    out = U.inventory("alice")

    assert out["accounts"] == [
        {"email": "other@example.com", "skills": ["gdocs"], "shared_grant": False},
        {
            "email": "same@example.com",
            "skills": ["gcalendar", "gmail"],
            "shared_grant": True,
        },
    ]
    assert _row(out, "gmail")["siblings_sharing_grant"] == ["gcalendar"]
    assert _row(out, "gdocs")["siblings_sharing_grant"] == []


def test_own_client_is_detected_from_the_skill_env(google):
    scripts = google.link("alice", "gdrive")
    (scripts / ".env").write_text("GOOGLE_CLIENT_ID=my-own\n", encoding="utf-8")

    assert _row(U.inventory("alice"), "gdrive")["own_client"] is True


def test_own_client_is_detected_from_a_client_id_mismatch(google):
    google.link("alice", "gdrive", client_id="my-own-cid")

    assert _row(U.inventory("alice"), "gdrive")["own_client"] is True


def test_the_shared_client_is_not_reported_as_your_own(google):
    google.link("alice", "gdrive", client_id="shared-cid")

    assert _row(U.inventory("alice"), "gdrive")["own_client"] is False


def test_an_unreachable_broker_never_claims_your_own_client(google, monkeypatch):
    """A broker outage proves nothing about which client minted the token."""
    monkeypatch.setattr(U, "_shared_client_id", lambda: "")
    google.link("alice", "gdrive", client_id="shared-cid")

    assert _row(U.inventory("alice"), "gdrive")["own_client"] is False


def test_a_live_watch_is_reported_with_its_expiry(google):
    google.link("alice", "gcalendar")
    google.watch_state("alice", "gcalendar")

    row = _row(U.inventory("alice"), "gcalendar")

    assert row["watch"]["active"] is True
    # Google reports channel expiry in milliseconds.
    assert row["watch"]["expires_at"] == 1787181142


def test_skills_without_a_watch_report_none(google):
    google.link("alice", "gmail")

    row = _row(U.inventory("alice"), "gmail")

    assert row["watch"] == {"active": False, "expires_at": None}
    assert row["listener"] == {"declared": False, "autostart_rows": 0}


def test_the_listener_declaration_is_reported(google):
    google.link("alice", "gdrive")

    assert _row(U.inventory("alice"), "gdrive")["listener"]["declared"] is True


def test_idle_subscriptions_are_surfaced(google, monkeypatch):
    monkeypatch.setattr(U, "_idle_subscriptions", lambda profile, spec: 2)
    google.link("alice", "gcalendar")

    assert _row(U.inventory("alice"), "gcalendar")["subscriptions"] == {"idle_after_unlink": 2}


def test_the_calendar_source_is_reported(google, monkeypatch):
    monkeypatch.setattr(
        "app.calendar.google_auth.status",
        lambda profile: {"connected": True, "email": "u@example.com", "source": "skill"},
    )
    monkeypatch.setattr(U, "_app_credential_present", lambda profile: True)
    google.link("alice", "gcalendar")

    out = U.inventory("alice")

    assert out["calendar"] == {
        "source": "skill",
        "connected": True,
        "app_credential_present": True,
    }


def test_one_profiles_inventory_never_shows_anothers(google):
    google.link("bob", "gmail", email="bob@example.com")

    out = U.inventory("alice")

    assert all(row["linked"] is False for row in out["skills"])
    assert out["accounts"] == []


def test_an_unreadable_token_reads_as_not_linked(google):
    scripts = google.link("alice", "gmail")
    # The skill replaces this atomically, so a read can land mid-swap.
    (scripts / ".google_token.json").write_text("{not json", encoding="utf-8")

    assert _row(U.inventory("alice"), "gmail")["linked"] is False
