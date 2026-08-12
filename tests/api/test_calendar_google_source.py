"""Calendar & Schedule endpoints, where the two Google credentials meet.

The page has to know *which* account is in force, because a link owned by the
gcalendar skill is not the page's to connect or disconnect. These drive the three
handlers directly (no ASGI app, no DB, no Google).
"""

import asyncio
import json
from types import SimpleNamespace

import app.api.calendar as C
import app.calendar.google_auth as ga


def _req(profile: str = "alice") -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(is_authenticated=True, username=profile))


def _endpoint(path: str, method: str):
    for route in C.get_calendar_routes(None):
        if route.path == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"no route for {method} {path}")


def _body(resp) -> dict:
    return json.loads(resp.body.decode())


def _stub(monkeypatch, *, status, published=None):
    """Pin the credential status the handlers see."""
    sink = published if published is not None else []
    monkeypatch.setattr(ga, "status", lambda profile: dict(status))
    monkeypatch.setattr(C.calendar_feature, "is_enabled", lambda profile: True)
    monkeypatch.setattr(C, "get_calendar_provider", lambda profile: SimpleNamespace(name="google"))
    monkeypatch.setattr(C, "publish_schedule_events_admin_changed", sink.append)


# ── GET /api/calendar/settings ──────────────────────────────────────────────

def test_settings_reports_a_skill_link_with_its_address(monkeypatch):
    _stub(monkeypatch, status={"connected": True, "email": "linked@example.com", "source": "skill"})
    out = _body(asyncio.run(_endpoint("/api/calendar/settings", "GET")(_req())))
    assert out["google_connected"] is True
    assert out["google_source"] == "skill"
    assert out["google_email"] == "linked@example.com"


def test_settings_reports_a_page_connection_without_an_address(monkeypatch):
    """That flow requests no email scope, so "Connected" is all it can say."""
    _stub(monkeypatch, status={"connected": True, "email": None, "source": "app"})
    out = _body(asyncio.run(_endpoint("/api/calendar/settings", "GET")(_req())))
    assert (out["google_source"], out["google_email"]) == ("app", None)


def test_settings_reports_nothing_connected(monkeypatch):
    _stub(monkeypatch, status={"connected": False, "email": None, "source": None})
    out = _body(asyncio.run(_endpoint("/api/calendar/settings", "GET")(_req())))
    assert out["google_connected"] is False and out["google_source"] is None


# ── POST /api/calendar/google/connect ───────────────────────────────────────

def test_connect_is_refused_while_the_skill_owns_the_link(monkeypatch):
    """Consent here would mint a credential the skill's link instantly shadows."""
    _stub(monkeypatch, status={"connected": True, "email": "linked@example.com", "source": "skill"})

    def boom(_profile):
        raise AssertionError("must not start a consent flow that changes nothing")

    monkeypatch.setattr(ga, "build_authorize_url", boom)
    resp = asyncio.run(_endpoint("/api/calendar/google/connect", "POST")(_req()))
    out = _body(resp)
    assert resp.status_code == 409
    assert out["error"] == "skill_managed"
    # The message has to name the account and the way out, since the CLI prints it.
    assert "linked@example.com" in out["message"]
    assert "gcalendar" in out["message"]


def test_connect_still_works_when_the_skill_is_not_in_force(monkeypatch):
    _stub(monkeypatch, status={"connected": False, "email": None, "source": None})
    monkeypatch.setattr(ga, "build_authorize_url", lambda profile: "https://accounts.google.com/x")
    resp = asyncio.run(_endpoint("/api/calendar/google/connect", "POST")(_req()))
    assert resp.status_code == 200
    assert _body(resp)["authorize_url"] == "https://accounts.google.com/x"


def test_connect_reports_an_unresolvable_redirect_as_before(monkeypatch):
    _stub(monkeypatch, status={"connected": False, "email": None, "source": None})
    monkeypatch.setattr(ga, "build_authorize_url", lambda profile: None)
    resp = asyncio.run(_endpoint("/api/calendar/google/connect", "POST")(_req()))
    assert resp.status_code == 409
    assert _body(resp)["error"] == "unavailable"


# ── POST /api/calendar/google/disconnect ────────────────────────────────────

def test_disconnect_under_a_skill_link_says_what_actually_happened(monkeypatch):
    """Clearing the page's dormant rows is allowed — it is the only way to drop a
    stale second account — but the calendar is still connected via the skill."""
    dropped, published = [], []
    _stub(monkeypatch, status={"connected": True, "email": "linked@example.com", "source": "skill"},
          published=published)
    monkeypatch.setattr(ga, "disconnect", lambda profile: dropped.append(profile))

    out = _body(asyncio.run(_endpoint("/api/calendar/google/disconnect", "POST")(_req())))
    assert dropped == ["alice"], "the page's own rows are still cleared"
    assert out["google_connected"] is True
    assert out["google_source"] == "skill"
    assert "gcalendar skill" in out["message"]
    assert published == ["alice"]


def test_disconnect_without_a_skill_link_disconnects(monkeypatch):
    dropped = []
    _stub(monkeypatch, status={"connected": True, "email": None, "source": "app"})
    monkeypatch.setattr(ga, "disconnect", lambda profile: dropped.append(profile))

    out = _body(asyncio.run(_endpoint("/api/calendar/google/disconnect", "POST")(_req())))
    assert dropped == ["alice"]
    assert out == {"ok": True, "google_connected": False, "google_source": None}


def test_the_handlers_require_authentication():
    anon = SimpleNamespace(user=SimpleNamespace(is_authenticated=False, username=""))
    for path, method in (
        ("/api/calendar/settings", "GET"),
        ("/api/calendar/google/connect", "POST"),
        ("/api/calendar/google/disconnect", "POST"),
    ):
        assert asyncio.run(_endpoint(path, method)(anon)).status_code == 401
