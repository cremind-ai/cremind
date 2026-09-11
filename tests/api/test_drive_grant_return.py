"""``POST /api/drive/grants`` and the page the Picker tab is led back to.

Starting a Picker round may name a ``return_route``; the API records it against
the round's state (app/api/oauth_return.py) so the callback can bring the tab
back to the Drive section. Best-effort: the start response is unchanged, and a
record that cannot be made never fails the start. Drives the handler directly
(no ASGI app, no Google).
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import app.api.drive as D
import app.api.oauth_return as oauth_return
from app.config.settings import BaseConfig

_STATE = "UGlja2VyUm91bmRTdGF0ZUZvclRlc3Rz"
_STARTED = {
    "authorize_url": f"https://accounts.google.com/o/oauth2/v2/auth?state={_STATE}",
    "state": _STATE,
    "capture_hint": None,
    "local_capture": True,
}


def _req(body, profile: str = "alice") -> SimpleNamespace:
    async def read_json():
        if isinstance(body, Exception):
            raise body
        return body

    return SimpleNamespace(user=SimpleNamespace(is_authenticated=True, username=profile),
                           json=read_json)


def _start_grant():
    for route in D.get_drive_routes():
        if route.path == "/api/drive/grants" and "POST" in (route.methods or set()):
            return route.endpoint
    raise AssertionError("no POST /api/drive/grants route")


def _body(resp) -> dict:
    return json.loads(resp.body.decode())


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    return tmp_path / "oauth_returns"


@pytest.fixture
def started(monkeypatch):
    calls = []

    def start(profile, **kwargs):
        calls.append((profile, kwargs))
        return dict(_STARTED)

    monkeypatch.setattr(D.grant_flow, "start", start)
    return calls


def test_a_return_route_is_recorded_against_the_round(store, started):
    resp = asyncio.run(_start_grant()(_req({"return_route": "/alice/settings/gsuite"})))
    assert resp.status_code == 200
    assert _body(resp) == _STARTED, "the start response is unchanged"
    assert started[0][0] == "alice"
    result = oauth_return.consume(oauth_return.complete(_STATE, outcome="received", flow="drive"))
    assert (result["profile"], result["route"]) == ("alice", "/alice/settings/gsuite")


def test_the_route_is_kept_inside_the_calling_profile(store, started):
    asyncio.run(_start_grant()(_req({"return_route": "/alice/settings/gsuite"}, profile="bob")))
    result = oauth_return.consume(oauth_return.complete(_STATE, outcome="received", flow="drive"))
    assert (result["profile"], result["route"]) == ("bob", "/bob")


@pytest.mark.parametrize("body", [{}, {"return_route": None}, {"return_route": ["/alice"]},
                                  ValueError("empty body")])
def test_no_usable_return_route_records_nothing(store, started, body):
    resp = asyncio.run(_start_grant()(_req(body)))
    assert resp.status_code == 200
    assert _body(resp) == _STARTED
    assert not store.exists() or not any(store.iterdir())


def test_a_failed_record_never_fails_the_start(store, started, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(oauth_return, "record_context", refuse)
    resp = asyncio.run(_start_grant()(_req({"return_route": "/alice/settings/gsuite"})))
    assert resp.status_code == 200
    assert _body(resp) == _STARTED


def test_an_unavailable_start_records_nothing(store, monkeypatch):
    def unavailable(profile, **_kwargs):
        raise D.grant_flow.DriveGrantError("Google Drive is not linked yet.")

    monkeypatch.setattr(D.grant_flow, "start", unavailable)
    resp = asyncio.run(_start_grant()(_req({"return_route": "/alice/settings/gsuite"})))
    assert resp.status_code == 409
    assert _body(resp)["error"] == "unavailable"
    assert not store.exists() or not any(store.iterdir())


def test_starting_a_round_requires_authentication(store, started):
    anon = SimpleNamespace(user=SimpleNamespace(is_authenticated=False, username=""))
    assert asyncio.run(_start_grant()(anon)).status_code == 401
    assert started == []
