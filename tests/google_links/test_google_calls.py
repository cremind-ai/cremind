"""The two Google calls an unlink makes, and how their failures are classified.

These drive the real :func:`revoke_grant` / :func:`_stop_watch` with ``httpx``
faked, so the HTTP-status semantics are pinned here rather than assumed by the
orchestration tests (which stub both out).

The load-bearing case is ``400 invalid_token``. Google returns it for a token it
has already forgotten, and every revoke after the first on a *shared* grant
returns exactly that — so classifying it as failure would make a completely
successful ``--all`` report four failures.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

import app.google.unlink as U
from app.google.registry import by_name


class FakeResponse:
    def __init__(self, status_code: int, *, text: str = "", payload: Any = None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_httpx(monkeypatch, responder, calls: Optional[List[Dict[str, Any]]] = None):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, data=None, json=None, headers=None):
            if calls is not None:
                calls.append({"url": url, "data": data, "json": json, "headers": headers})
            return responder(url, data, json, headers)

    monkeypatch.setattr(U.httpx, "Client", FakeClient)


# ── revoke ───────────────────────────────────────────────────────────────────

def test_a_200_is_revoked(monkeypatch):
    calls: List[Dict[str, Any]] = []
    _fake_httpx(monkeypatch, lambda *a: FakeResponse(200), calls)

    revoked, status = U.revoke_grant({"refresh_token": "rt-1", "access_token": "at-1"})

    assert (revoked, status) == (True, U.REVOKED)
    assert calls[0]["url"] == U.GOOGLE_REVOKE_ENDPOINT
    # The refresh token is what ends the grant; never the access token.
    assert calls[0]["data"] == {"token": "rt-1"}


def test_an_invalid_token_400_counts_as_already_revoked(monkeypatch):
    _fake_httpx(
        monkeypatch,
        lambda *a: FakeResponse(400, text='{"error":"invalid_token"}', payload={"error": "invalid_token"}),
    )

    revoked, status = U.revoke_grant({"refresh_token": "rt-1"})

    assert (revoked, status) == (True, U.ALREADY_REVOKED)


def test_an_invalid_token_400_is_recognised_from_a_non_json_body(monkeypatch):
    _fake_httpx(monkeypatch, lambda *a: FakeResponse(400, text="error=invalid_token"))

    assert U.revoke_grant({"refresh_token": "rt-1"}) == (True, U.ALREADY_REVOKED)


def test_any_other_400_is_a_failure(monkeypatch):
    _fake_httpx(
        monkeypatch,
        lambda *a: FakeResponse(
            400, text='{"error":"unsupported_token_type"}', payload={"error": "unsupported_token_type"}
        ),
    )

    revoked, status = U.revoke_grant({"refresh_token": "rt-1"})

    assert revoked is False
    assert status.startswith("http_400")


def test_a_server_error_is_a_failure(monkeypatch):
    _fake_httpx(monkeypatch, lambda *a: FakeResponse(500, text="boom"))

    revoked, status = U.revoke_grant({"refresh_token": "rt-1"})

    assert revoked is False
    assert status.startswith("http_500")


def test_a_network_error_is_reported_as_such(monkeypatch):
    def explode(*_a):
        raise OSError("connection refused")

    _fake_httpx(monkeypatch, explode)

    revoked, status = U.revoke_grant({"refresh_token": "rt-1"})

    assert revoked is False
    assert status.startswith("network:")


def test_the_access_token_is_used_when_there_is_no_refresh_token(monkeypatch):
    calls: List[Dict[str, Any]] = []
    _fake_httpx(monkeypatch, lambda *a: FakeResponse(200), calls)

    assert U.revoke_grant({"access_token": "at-only"})[0] is True
    assert calls[0]["data"] == {"token": "at-only"}


def test_nothing_to_revoke_never_touches_the_network(monkeypatch):
    def boom(*_a):
        raise AssertionError("no HTTP call should be made without a token")

    _fake_httpx(monkeypatch, boom)

    assert U.revoke_grant({}) == (False, U.NO_TOKEN)


# ── channels.stop ────────────────────────────────────────────────────────────

_LIVE = {"channel_id": "cm-abc", "resource_id": "res-abc"}


def _token(**over):
    data = {"access_token": "at-1", "refresh_token": "rt-1", "expiry": time.time() + 3600}
    data.update(over)
    return data


def test_a_live_channel_is_stopped_with_the_ids_from_the_state_file(monkeypatch):
    calls: List[Dict[str, Any]] = []
    _fake_httpx(monkeypatch, lambda *a: FakeResponse(204), calls)

    stopped, error = U._stop_watch(by_name("gcalendar"), _token(), _LIVE)

    assert (stopped, error) == (True, None)
    assert calls[0]["url"] == "https://www.googleapis.com/calendar/v3/channels/stop"
    assert calls[0]["json"] == {"id": "cm-abc", "resourceId": "res-abc"}
    assert calls[0]["headers"] == {"Authorization": "Bearer at-1"}


def test_drive_stops_its_channel_on_the_drive_endpoint(monkeypatch):
    calls: List[Dict[str, Any]] = []
    _fake_httpx(monkeypatch, lambda *a: FakeResponse(204), calls)

    U._stop_watch(by_name("gdrive"), _token(), _LIVE)

    assert calls[0]["url"] == "https://www.googleapis.com/drive/v3/channels/stop"


def test_no_channel_means_nothing_to_stop(monkeypatch):
    def boom(*_a):
        raise AssertionError("no HTTP call should be made without a channel")

    _fake_httpx(monkeypatch, boom)

    assert U._stop_watch(by_name("gcalendar"), _token(), {}) == (True, None)


def test_a_skill_with_no_watch_never_calls_google(monkeypatch):
    def boom(*_a):
        raise AssertionError("gmail registers no push channel")

    _fake_httpx(monkeypatch, boom)

    assert U._stop_watch(by_name("gmail"), _token(), _LIVE) == (True, None)


def test_a_channel_google_has_forgotten_counts_as_stopped(monkeypatch):
    _fake_httpx(monkeypatch, lambda *a: FakeResponse(404, text="not found"))

    assert U._stop_watch(by_name("gcalendar"), _token(), _LIVE) == (True, None)


def test_channel_id_not_found_counts_as_stopped(monkeypatch):
    _fake_httpx(monkeypatch, lambda *a: FakeResponse(400, text='{"error":"channelIdNotFound"}'))

    assert U._stop_watch(by_name("gcalendar"), _token(), _LIVE) == (True, None)


def test_a_refused_stop_is_reported_without_raising(monkeypatch):
    _fake_httpx(monkeypatch, lambda *a: FakeResponse(403, text="forbidden"))

    stopped, error = U._stop_watch(by_name("gcalendar"), _token(), _LIVE)

    assert stopped is False
    assert "HTTP 403" in error


def test_a_missing_credential_cannot_stop_the_channel(monkeypatch):
    """The state a real install was found in — and why order matters."""
    def boom(*_a):
        raise AssertionError("no credential means no call to attempt")

    _fake_httpx(monkeypatch, boom)

    stopped, error = U._stop_watch(by_name("gcalendar"), None, _LIVE)

    assert stopped is False
    assert "expires on its own" in error


def test_an_expired_access_token_is_refreshed_in_memory_only(monkeypatch, tmp_path):
    calls: List[Dict[str, Any]] = []

    def responder(url, data, json, headers):
        if url == U.GOOGLE_TOKEN_ENDPOINT:
            return FakeResponse(200, payload={"access_token": "fresh-at", "expires_in": 3600})
        return FakeResponse(204)

    _fake_httpx(monkeypatch, responder, calls)
    stale = _token(expiry=time.time() - 10)

    stopped, error = U._stop_watch(by_name("gcalendar"), stale, _LIVE)

    assert (stopped, error) == (True, None)
    assert calls[0]["url"] == U.GOOGLE_TOKEN_ENDPOINT
    assert calls[1]["headers"] == {"Authorization": "Bearer fresh-at"}
    # The token file is about to be deleted; refreshing must not rewrite it.
    assert stale["access_token"] == "at-1"


def test_a_failed_refresh_reports_rather_than_raising(monkeypatch):
    _fake_httpx(monkeypatch, lambda url, *a: FakeResponse(400, text="invalid_grant"))

    stopped, error = U._stop_watch(
        by_name("gcalendar"), _token(expiry=time.time() - 10), _LIVE
    )

    assert stopped is False
    assert "no usable access token" in error
