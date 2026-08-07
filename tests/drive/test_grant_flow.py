"""Tests for the Google Picker grant flow: authorize URL, capture, and the
diff-based completion that makes the flow work without a reachable redirect."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest

import app.drive.grant_flow as gf
import app.drive.skill_token as st

DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"


def _wire(monkeypatch, *, linked=True, reachable=None, app_url="http://localhost:1515"):
    monkeypatch.setattr(
        gf.google_discovery, "google_client",
        lambda: {"client_id": "cid", "client_secret": "csecret", "scopes": []},
    )
    monkeypatch.setattr(gf.BaseConfig, "APP_URL", app_url, raising=False)
    monkeypatch.setattr(
        gf.skill_token, "status",
        lambda profile: {
            "linked": linked, "email": "user@example.com",
            "scopes": [DRIVE_FILE], "scopes_stale": False,
        },
    )
    ids = set(reachable or [])
    monkeypatch.setattr(gf.skill_token, "reachable_ids", lambda profile: set(ids))
    monkeypatch.setattr(
        gf.skill_token, "get_file",
        lambda profile, fid: {"id": fid, "name": f"file-{fid}", "mime_type": "text/plain"},
    )
    monkeypatch.setattr(gf.skill_token, "record_grants", lambda *a, **k: None)
    gf._pending.clear()
    return ids


def test_authorize_url_requests_only_drive_file(monkeypatch):
    """drive.file cannot be combined with any other scope in a Picker request."""
    _wire(monkeypatch)
    out = gf.start("alice")
    params = parse_qs(urlparse(out["authorize_url"]).query)
    assert params["scope"] == [DRIVE_FILE]
    assert " " not in params["scope"][0]
    assert params["trigger_onepick"] == ["true"]
    assert params["prompt"] == ["consent"]
    assert params["response_type"] == ["code"]
    assert params["login_hint"] == ["user@example.com"]


def test_authorize_url_options(monkeypatch):
    _wire(monkeypatch)
    out = gf.start(
        "alice", file_ids=["abc", "def"], allow_multiple=False,
        allow_folders=False, mime_types=["text/csv"],
    )
    params = parse_qs(urlparse(out["authorize_url"]).query)
    assert params["file_ids"] == ["abc,def"]
    assert params["mimetypes"] == ["text/csv"]
    assert "allow_multiple" not in params
    assert "allow_folder_selection" not in params


def test_start_requires_a_linked_account(monkeypatch):
    _wire(monkeypatch, linked=False)
    with pytest.raises(gf.DriveGrantError):
        gf.start("alice")


def test_picker_uses_the_client_the_token_was_minted_with(monkeypatch):
    """A grant attaches to the (app, user) pair.

    Requesting it under a different client than the token holds would land the
    grant somewhere the skill cannot use — the bring-your-own-credentials case,
    where the skill takes its client from its own scripts/.env.
    """
    _wire(monkeypatch)
    monkeypatch.setattr(gf.skill_token, "token_client_id", lambda profile: "byo-client")
    out = gf.start("alice")
    params = parse_qs(urlparse(out["authorize_url"]).query)
    assert params["client_id"] == ["byo-client"]


def test_picker_falls_back_to_the_broker_client_for_older_tokens(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(gf.skill_token, "token_client_id", lambda profile: "")
    out = gf.start("alice")
    params = parse_qs(urlparse(out["authorize_url"]).query)
    assert params["client_id"] == ["cid"]


def test_unavailable_broker_is_only_fatal_without_a_token_client(monkeypatch):
    _wire(monkeypatch)
    def boom():
        raise RuntimeError("connect unreachable")
    monkeypatch.setattr(gf.google_discovery, "google_client", boom)
    # The token names its own client, so the broker is not needed at all.
    monkeypatch.setattr(gf.skill_token, "token_client_id", lambda profile: "byo-client")
    assert gf.start("alice")["state"]
    # Without one, the broker failure is genuinely the cause and must surface.
    monkeypatch.setattr(gf.skill_token, "token_client_id", lambda profile: "")
    with pytest.raises(gf.DriveGrantError, match="OAuth client"):
        gf.start("alice")


def test_redirect_uri_is_always_loopback(monkeypatch):
    """Google's Desktop client type rejects non-loopback redirects outright."""
    _wire(monkeypatch, app_url="https://cremind.example.com")
    assert gf.redirect_uri().startswith("http://localhost")
    assert gf.capture_is_local() is False
    assert gf.capture_hint()  # the UI must warn that the final redirect will fail

    _wire(monkeypatch, app_url="http://localhost:1515")
    assert gf.redirect_uri() == "http://localhost:1515/api/oauth/google-drive/callback"
    assert gf.capture_is_local() is True
    assert gf.capture_hint() is None


def test_captured_redirect_reports_picked_files(monkeypatch):
    _wire(monkeypatch)
    state = gf.start("alice")["state"]
    gf.record_redirect(f"state={state}&code=xyz&picked_file_ids=f1,f2")
    out = gf.poll_status("alice", state)
    assert out["status"] == "completed"
    assert [f["id"] for f in out["files"]] == ["f1", "f2"]


def test_completion_without_a_redirect_uses_the_reachable_diff(monkeypatch):
    """The grant lands on approval, so a lost redirect must not lose the result."""
    ids = _wire(monkeypatch, reachable={"old"})
    state = gf.start("alice")["state"]
    assert gf.poll_status("alice", state)["files"] == []
    ids.add("new")  # the user approved; Google now lets the token see it
    out = gf.poll_status("alice", state)
    assert out["status"] == "completed"
    assert [f["id"] for f in out["files"]] == ["new"]


def test_a_second_poll_does_not_re_report_the_same_files(monkeypatch):
    ids = _wire(monkeypatch, reachable=set())
    state = gf.start("alice")["state"]
    ids.add("new")
    assert len(gf.poll_status("alice", state)["files"]) == 1
    assert gf.poll_status("alice", state)["files"] == []


def test_a_second_poll_does_not_re_report_captured_picks(monkeypatch):
    _wire(monkeypatch)
    state = gf.start("alice")["state"]
    gf.record_redirect(f"state={state}&code=c&picked_file_ids=f1")
    assert len(gf.poll_status("alice", state)["files"]) == 1
    assert gf.poll_status("alice", state)["files"] == []


def test_denied_consent_is_reported(monkeypatch):
    _wire(monkeypatch)
    state = gf.start("alice")["state"]
    gf.record_redirect(f"state={state}&error=access_denied")
    out = gf.poll_status("alice", state)
    assert out["status"] == "error"
    assert out["files"] == []


def test_unknown_state_is_rejected(monkeypatch):
    _wire(monkeypatch)
    with pytest.raises(gf.DriveGrantError):
        gf.record_redirect("state=nope&code=x")
    assert gf.poll_status("alice", "nope")["status"] == "unknown"


def test_state_from_another_profile_is_refused(monkeypatch):
    _wire(monkeypatch)
    state = gf.start("alice")["state"]
    assert gf.poll_status("bob", state)["status"] == "unknown"


def test_complete_from_pasted_url_accepts_url_or_bare_query(monkeypatch):
    _wire(monkeypatch)
    state = gf.start("alice")["state"]
    out = gf.complete_from_redirect_url(
        "alice",
        f"http://localhost:1515/api/oauth/google-drive/callback?state={state}&code=c&picked_file_ids=f9",
    )
    assert [f["id"] for f in out["files"]] == ["f9"]

    state2 = gf.start("alice")["state"]
    out2 = gf.complete_from_redirect_url("alice", f"state={state2}&code=c&picked_file_ids=f8")
    assert [f["id"] for f in out2["files"]] == ["f8"]


def test_complete_from_pasted_url_validates_input(monkeypatch):
    _wire(monkeypatch)
    with pytest.raises(gf.DriveGrantError):
        gf.complete_from_redirect_url("alice", "")
    with pytest.raises(gf.DriveGrantError):
        gf.complete_from_redirect_url("alice", "state=unknown&code=c")


def test_unverified_picks_are_flagged(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(gf.skill_token, "get_file", lambda profile, fid: None if fid == "bad" else
                        {"id": fid, "name": fid, "mime_type": "text/plain"})
    state = gf.start("alice")["state"]
    gf.record_redirect(f"state={state}&code=c&picked_file_ids=good,bad")
    out = gf.poll_status("alice", state)
    assert out["unverified"] == ["bad"]
    assert out["note"]


def test_pending_entries_are_pruned(monkeypatch):
    _wire(monkeypatch)
    state = gf.start("alice")["state"]
    gf._pending[state]["ts"] = time.time() - (gf._PENDING_TTL + 1)
    gf.start("alice")  # any start prunes
    assert state not in gf._pending


def test_cancel_drops_only_the_callers_round(monkeypatch):
    _wire(monkeypatch)
    state = gf.start("alice")["state"]
    gf.cancel("bob", state)
    assert state in gf._pending
    gf.cancel("alice", state)
    assert state not in gf._pending
