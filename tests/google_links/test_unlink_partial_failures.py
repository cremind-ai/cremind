"""Half-done unlinks, and which half is allowed to fail.

The asymmetry is the point. Failing to tell Google is survivable — Cremind can no
longer use the account either way — so it reports success with a loud message.
Failing to remove the local credential is not: a usable token is still on disk, so
that is the one hard failure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import app.google.unlink as U
from app.google.registry import GOOGLE_CONNECTIONS_URL, by_name


def test_an_already_revoked_grant_counts_as_revoked(google):
    """Google 400s ``invalid_token`` for a token it has already forgotten."""
    google.revoke_result = (True, U.ALREADY_REVOKED)
    google.link("alice", "gmail")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["revoked"] is True
    assert result["revoke_error"] is None
    assert "already dropped this grant" in result["message"]


def test_a_failed_revoke_still_wipes_and_says_so(google):
    google.revoke_result = (False, "http_500: upstream boom")
    google.link("alice", "gmail")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    # The security-critical half succeeded, so this is not a failure.
    assert result["ok"] is True
    assert result["unlinked"] is True
    assert result["revoked"] is False
    assert result["revoke_error"] == "http_500: upstream boom"
    assert not google.token_file("alice", "gmail").exists()
    # Never "try again": the token that could revoke it is gone for good.
    assert "re-running this will not help" in result["message"]
    assert GOOGLE_CONNECTIONS_URL in result["message"]


def test_a_network_failure_on_revoke_still_wipes(google):
    google.revoke_result = (False, "network: connection refused")
    google.link("alice", "gsheets")

    result = asyncio.run(U.unlink_skill("alice", by_name("gsheets")))

    assert result["unlinked"] is True
    assert not google.token_file("alice", "gsheets").exists()


def test_an_undeletable_token_file_is_the_one_hard_failure(google, monkeypatch):
    google.link("alice", "gdrive")

    real_unlink = Path.unlink

    def stubborn(self, *args, **kwargs):
        if self.name == ".google_token.json":
            raise PermissionError("[WinError 32] file is in use")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stubborn)
    # Retry backoff would otherwise make this test sleep.
    monkeypatch.setattr(U.time, "sleep", lambda _s: None)

    result = asyncio.run(U.unlink_skill("alice", by_name("gdrive")))

    assert result["ok"] is False
    assert result["still_linked"] is True
    assert result["unlinked"] is False
    assert "scripts/.google_token.json" in result["failed_paths"]
    assert "invalid_grant" in result["message"]


def test_a_watch_that_cannot_be_stopped_never_blocks_the_wipe(google):
    google.watch_result = (False, "Google refused to stop the push channel (HTTP 403): nope")
    google.link("alice", "gcalendar")
    google.watch_state("alice", "gcalendar")

    result = asyncio.run(U.unlink_skill("alice", by_name("gcalendar")))

    assert result["unlinked"] is True
    assert result["watch_stopped"] is False
    assert "push channel" in result["message"]
    assert not google.token_file("alice", "gcalendar").exists()


def test_unlinking_something_never_linked_succeeds(google):
    google.install("alice", "gmail")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert result["ok"] is True
    assert result["unlinked"] is False
    assert result["already"] is True
    assert result["reason"] == "not_linked"
    assert "nothing to unlink" in result["message"]


def test_a_second_unlink_is_idempotent(google):
    google.link("alice", "gmail")

    first = asyncio.run(U.unlink_skill("alice", by_name("gmail")))
    second = asyncio.run(U.unlink_skill("alice", by_name("gmail")))

    assert first["unlinked"] is True
    assert second["ok"] is True and second["already"] is True


def test_an_uninstalled_skill_reports_rather_than_raising(google):
    result = asyncio.run(U.unlink_skill("alice", by_name("gdocs")))

    assert result["ok"] is True
    assert result["already"] is True
    assert result["reason"] == "not_installed"


def test_orphaned_derived_files_are_swept_even_with_no_token(google):
    """The state a real install was found in: watch state, no credential."""
    google.install("alice", "gcalendar")
    google.watch_state("alice", "gcalendar")
    google.event_payload("alice", "gcalendar", "event_changed")

    result = asyncio.run(U.unlink_skill("alice", by_name("gcalendar")))

    assert result["already"] is True
    assert "scripts/.listener_state.json" in result["cleaned"]
    assert "events/event_changed/one.md" in result["cleaned"]
    assert not google.exists("alice", "gcalendar", "scripts/.listener_state.json")


def test_the_wipe_takes_the_temp_credential_and_the_event_payloads(google):
    scripts = google.link("alice", "gdrive")
    # A crash between write and os.replace leaves a full credential set here.
    (scripts / ".google_token.json.tmp").write_text('{"refresh_token": "rt-1"}', encoding="utf-8")
    (scripts / ".drive_grants.json").write_text("[]", encoding="utf-8")
    google.event_payload("alice", "gdrive", "file_changed", "changed.md")

    result = asyncio.run(U.unlink_skill("alice", by_name("gdrive")))

    assert set(result["cleaned"]) >= {
        "scripts/.google_token.json",
        "scripts/.google_token.json.tmp",
        "scripts/.drive_grants.json",
        "events/file_changed/changed.md",
    }
    assert result["failed_paths"] == []


def test_the_wipe_never_touches_user_config_or_the_lock(google):
    scripts = google.link("alice", "gcalendar")
    (scripts / ".env").write_text("GOOGLE_CLIENT_ID=mine\n", encoding="utf-8")
    (scripts / ".listener.lock").write_text("", encoding="utf-8")
    before = (scripts / ".env").read_bytes()

    result = asyncio.run(U.unlink_skill("alice", by_name("gcalendar")))

    assert (scripts / ".env").read_bytes() == before
    assert (scripts / ".listener.lock").exists()
    assert not any(name.endswith(".env") for name in result["cleaned"])
    assert not any(name.endswith(".listener.lock") for name in result["cleaned"])


def test_the_listener_deregistration_is_reported(google):
    """Unlink deregisters the listener, so re-linking is not enough on its own."""
    google.link("alice", "gcalendar")

    result = asyncio.run(U.unlink_skill("alice", by_name("gcalendar")))

    assert result["autostart_removed"] == 1
    assert "register it again after re-linking" in result["message"]


def test_unlink_all_reports_a_partial_failure(google, monkeypatch):
    google.link("alice", "gmail")
    google.link("alice", "gsheets")

    real_unlink = Path.unlink

    def stubborn(self, *args, **kwargs):
        if self.name == ".google_token.json" and "gsheets" in str(self):
            raise PermissionError("[WinError 32] file is in use")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stubborn)
    monkeypatch.setattr(U.time, "sleep", lambda _s: None)

    out = asyncio.run(U.unlink_all("alice"))

    assert out["ok"] is False
    assert out["failed"] == ["gsheets"]
    assert out["unlinked"] == 1
    assert "1 still holds a credential file" in out["message"]
