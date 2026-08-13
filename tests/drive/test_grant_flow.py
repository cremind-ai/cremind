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
    # Both feed redirect_uri(); a developer machine exporting either would skew
    # every redirect assertion below, so start from a known-empty environment.
    monkeypatch.delenv("CREMIND_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("CREMIND_UI_PORT", raising=False)
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
    # Exact, not startswith: a portless "http://localhost" is loopback too, and
    # asserting only the prefix is what let :80 ship (see the test below).
    assert gf.redirect_uri() == "http://localhost:1515" + gf.CALLBACK_PATH
    assert gf.capture_is_local() is False
    assert gf.capture_hint()  # the UI must warn that the final redirect will fail

    _wire(monkeypatch, app_url="http://localhost:1515")
    assert gf.redirect_uri() == "http://localhost:1515/api/oauth/google-drive/callback"
    assert gf.capture_is_local() is True
    assert gf.capture_hint() is None


def test_portless_public_app_url_never_emits_portless_localhost(monkeypatch):
    """A K8s Ingress APP_URL carries no port; :80 is a dead redirect everywhere.

    ``urlsplit("https://host").port`` is None (the scheme's implicit 443 is never
    returned), so scraping the port alone used to yield "http://localhost".
    """
    _wire(monkeypatch, app_url="https://test.cremind.io")
    assert gf.redirect_uri() == "http://localhost:1515" + gf.CALLBACK_PATH

    monkeypatch.setenv("CREMIND_UI_PORT", "8080")
    assert gf.redirect_uri() == "http://localhost:8080" + gf.CALLBACK_PATH


def test_ui_port_zero_or_garbage_falls_back_to_1515(monkeypatch):
    """0 = loopback-only behind an external proxy, so it names no reachable port."""
    _wire(monkeypatch, app_url="https://test.cremind.io")
    for value in ("0", "not-a-port", ""):
        monkeypatch.setenv("CREMIND_UI_PORT", value)
        assert gf.redirect_uri() == "http://localhost:1515" + gf.CALLBACK_PATH


def test_pinned_loopback_oauth_redirect_sets_the_drive_port(monkeypatch):
    """The operator's pin for the sibling skills flow names the forwarded port.

    One port-forward serves both callbacks, so honouring the pin is what makes
    capture work on a domain-fronted install.
    """
    _wire(monkeypatch, app_url="https://test.cremind.io")
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://localhost:1515/api/oauth/callback")
    assert gf.redirect_uri() == "http://localhost:1515" + gf.CALLBACK_PATH

    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://127.0.0.1:9999/api/oauth/callback")
    assert gf.redirect_uri() == "http://127.0.0.1:9999" + gf.CALLBACK_PATH

    # The pin outranks a port scraped from APP_URL: it is the address an operator
    # has actually arranged to be reachable.
    _wire(monkeypatch, app_url="http://192.168.1.50:8080")
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://localhost:1515/api/oauth/callback")
    assert gf.redirect_uri() == "http://localhost:1515" + gf.CALLBACK_PATH


def test_non_loopback_pin_is_ignored(monkeypatch):
    """Honouring it would turn an uncaptured redirect into a hard Google error."""
    _wire(monkeypatch, app_url="https://test.cremind.io")
    monkeypatch.setenv(
        "CREMIND_OAUTH_REDIRECT_URI", "https://test.cremind.io/api/oauth/callback"
    )
    assert gf.redirect_uri() == "http://localhost:1515" + gf.CALLBACK_PATH


def test_docker_server_mode_keeps_the_app_url_port(monkeypatch):
    """A remapped publish (-p 8080:1515) is reachable on APP_URL's port, not the
    container's own, so the scrape must still win over the bind-port fallback."""
    _wire(monkeypatch, app_url="http://192.168.1.50:8080")
    monkeypatch.setenv("CREMIND_UI_PORT", "1515")
    assert gf.redirect_uri() == "http://localhost:8080" + gf.CALLBACK_PATH


def test_malformed_app_url_port_falls_back_instead_of_raising(monkeypatch):
    """urlsplit(...).port raises on a bad port; start() must not die on it."""
    _wire(monkeypatch, app_url="http://bad-host:not-a-port")
    assert gf.redirect_uri() == "http://localhost:1515" + gf.CALLBACK_PATH
    assert gf.capture_is_local() is False


def test_authorize_url_carries_the_pinned_loopback_redirect(monkeypatch):
    """End to end on the K8s shape: Ingress APP_URL + the operator's pin."""
    _wire(monkeypatch, app_url="https://test.cremind.io")
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://localhost:1515/api/oauth/callback")
    out = gf.start("alice")
    params = parse_qs(urlparse(out["authorize_url"]).query)
    assert params["redirect_uri"] == ["http://localhost:1515" + gf.CALLBACK_PATH]
    assert out["local_capture"] is False
    # The hint has to name the origin the user must keep forwarded.
    assert "http://localhost:1515" in out["capture_hint"]


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
