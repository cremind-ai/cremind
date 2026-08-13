"""The blast-radius guard.

All five Google skills share one OAuth client, and Google revokes per
(client, account) — not per skill. So revoking gmail's refresh token can end
gcalendar's and gdrive's grants for the same address. "Per-skill unlink" is a
promise about local state; keeping it honest means *not* revoking while a sibling
still holds the same grant, and saying so.
"""

from __future__ import annotations

import asyncio

import pytest

import app.google.unlink as U
from app.google.registry import by_name


def test_a_lone_link_is_revoked_normally(google):
    google.link("alice", "gmail")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["siblings_sharing_grant"] == []
    assert result["revoked"] is True
    assert result["revoke_status"] == U.REVOKED


def test_a_shared_grant_suppresses_the_revoke(google, monkeypatch):
    def boom(_data):
        raise AssertionError("revoking here would break the siblings")

    monkeypatch.setattr(U, "revoke_grant", boom)
    for skill in ("gmail", "gcalendar", "gdrive"):
        google.link("alice", skill, email="same@example.com")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["revoke_status"] == U.SKIPPED_SHARED_GRANT
    assert result["revoked"] is False
    assert sorted(result["siblings_sharing_grant"]) == ["gcalendar", "gdrive"]
    # Local state still goes, which is the half we always promise.
    assert result["unlinked"] is True
    assert not google.token_file("alice", "gmail").exists()
    # And the siblings are untouched: their grant is still live.
    assert google.token_file("alice", "gcalendar").exists()


def test_the_shared_grant_message_names_the_way_out(google):
    for skill in ("gmail", "gcalendar"):
        google.link("alice", skill, email="same@example.com")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert "gcalendar" in result["message"]
    assert "--all" in result["message"]


def test_force_revoke_overrides_the_guard(google):
    for skill in ("gmail", "gcalendar"):
        google.link("alice", skill, email="same@example.com")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail"), force_revoke=True))

    assert result["revoked"] is True
    assert google.revoked_tokens == ["rt-1"]


def test_a_different_account_is_not_a_sibling(google):
    google.link("alice", "gmail", email="work@example.com")
    google.link("alice", "gcalendar", email="home@example.com")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["siblings_sharing_grant"] == []
    assert result["revoked"] is True


def test_a_different_oauth_client_is_not_a_sibling(google):
    """Bring-your-own-credentials: same address, separate grant at Google."""
    google.link("alice", "gmail", email="same@example.com", client_id="shared-cid")
    google.link("alice", "gcalendar", email="same@example.com", client_id="my-own-cid")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["siblings_sharing_grant"] == []
    assert result["revoked"] is True


def test_an_unlinked_sibling_is_not_a_sibling(google):
    google.link("alice", "gmail", email="same@example.com")
    google.install("alice", "gcalendar")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["siblings_sharing_grant"] == []


def test_a_skill_never_matches_itself(google):
    google.link("alice", "gmail")

    assert U.siblings_sharing_grant("alice", by_name("gmail")) == []


def test_a_token_with_no_client_id_claims_no_siblings(google):
    """Without a client id we cannot know the blast radius, so claim nothing."""
    google.link("alice", "gmail", client_id="", email="same@example.com")
    google.link("alice", "gcalendar", client_id="", email="same@example.com")

    assert U.siblings_sharing_grant("alice", by_name("gmail")) == []


def test_another_profiles_link_is_never_a_sibling(google):
    google.link("alice", "gmail", email="same@example.com")
    google.link("bob", "gcalendar", email="same@example.com")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["siblings_sharing_grant"] == []
    assert google.token_file("bob", "gcalendar").exists()


def test_the_app_calendar_credential_is_flagged_at_risk(google, monkeypatch):
    """It shares the OAuth client, and the page's flow asks for no email scope,
    so we can only say "at risk" — never "revoked"."""
    monkeypatch.setattr(U, "_app_credential_present", lambda profile: True)
    google.link("alice", "gmail", client_id="shared-cid")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["app_credential_at_risk"] is True


def test_an_own_client_link_puts_the_app_credential_at_no_risk(google, monkeypatch):
    monkeypatch.setattr(U, "_app_credential_present", lambda profile: True)
    google.link("alice", "gmail", client_id="my-own-cid")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["app_credential_at_risk"] is False


def test_no_app_credential_means_nothing_at_risk(google, monkeypatch):
    monkeypatch.setattr(U, "_app_credential_present", lambda profile: False)
    google.link("alice", "gmail", client_id="shared-cid")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["app_credential_at_risk"] is False
