"""The ``/api/google/*`` handlers, driven directly — no ASGI app, no DB, no Google.

The status-code contract is the thing worth pinning: a failed *revoke* is a 200
(the local credential is gone, which is the half that matters), while a credential
file that survived the wipe is a 500 — the only hard failure. Getting those the
wrong way round would either hide a live token on disk or make a safe unlink look
broken.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import app.api.google as G
import app.google.unlink as U
from app.google.registry import GOOGLE_SKILLS


def _req(profile: str = "alice", *, body: Any = None, skill: Optional[str] = None):
    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=profile),
        path_params={"skill": skill} if skill else {},
        json=_json,
    )


def _anon(skill: Optional[str] = None):
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=False, username=""),
        path_params={"skill": skill} if skill else {},
        json=lambda: None,
    )


def _endpoint(path: str, method: str):
    for route in G.get_google_routes():
        if route.path == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"no route for {method} {path}")


def _body(resp) -> Dict[str, Any]:
    return json.loads(resp.body.decode())


ACCOUNTS = _endpoint("/api/google/accounts", "GET")
UNLINK = _endpoint("/api/google/accounts/{skill}/unlink", "POST")
UNLINK_ALL = _endpoint("/api/google/unlink-all", "POST")


@pytest.fixture
def wired(monkeypatch):
    """Stub the engine and record what the handlers published."""
    published: Dict[str, List[str]] = {"settings": [], "calendar": []}
    calls: List[Dict[str, Any]] = []
    state: Dict[str, Any] = {
        "installed": True,
        "result": {"skill": "gmail", "ok": True, "unlinked": True, "still_linked": False},
        "all": {"ok": True, "results": [{"skill": "gmail"}], "failed": []},
    }

    monkeypatch.setattr(
        "app.events.settings_state_bus.publish_settings_state_changed",
        lambda profile: published["settings"].append(profile),
    )
    monkeypatch.setattr(
        "app.api.calendar.publish_schedule_events_admin_changed",
        lambda profile: published["calendar"].append(profile),
    )
    monkeypatch.setattr(
        G.engine, "skill_dir", lambda profile, spec: object() if state["installed"] else None
    )
    monkeypatch.setattr(G.engine, "inventory", lambda profile: {"ok": True, "profile": profile})

    async def fake_unlink_skill(profile, spec, **kwargs):
        calls.append({"profile": profile, "skill": spec.dir_name, **kwargs})
        return dict(state["result"], skill=spec.dir_name)

    async def fake_unlink_all(profile, **kwargs):
        calls.append({"profile": profile, "all": True, **kwargs})
        return dict(state["all"])

    monkeypatch.setattr(G.engine, "unlink_skill", fake_unlink_skill)
    monkeypatch.setattr(G.engine, "unlink_all", fake_unlink_all)
    return SimpleNamespace(published=published, calls=calls, state=state)


# ── auth and routing ─────────────────────────────────────────────────────────

def test_the_handlers_require_authentication(wired):
    for endpoint, skill in ((ACCOUNTS, None), (UNLINK, "gmail"), (UNLINK_ALL, None)):
        resp = asyncio.run(endpoint(_anon(skill)))
        assert resp.status_code == 401
        assert _body(resp) == {"error": "Unauthenticated"}


def test_routes_are_registered():
    paths = {(r.path, tuple(sorted(r.methods or ()))) for r in G.get_google_routes()}
    assert ("/api/google/accounts", ("GET", "HEAD")) in paths
    assert ("/api/google/accounts/{skill}/unlink", ("POST",)) in paths
    assert ("/api/google/unlink-all", ("POST",)) in paths


def test_a_tokenless_request_is_refused(wired):
    resp = asyncio.run(UNLINK(_req("", skill="gmail")))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "Profile is required"


# ── the inventory ────────────────────────────────────────────────────────────

def test_accounts_returns_the_inventory_for_the_callers_profile(wired):
    resp = asyncio.run(ACCOUNTS(_req("alice")))
    assert resp.status_code == 200
    assert _body(resp) == {"ok": True, "profile": "alice"}


# ── unlink one ───────────────────────────────────────────────────────────────

def test_an_unknown_skill_is_refused_and_lists_the_real_ones(wired):
    resp = asyncio.run(UNLINK(_req(skill="foo")))

    assert resp.status_code == 400
    payload = _body(resp)
    assert payload["error"] == "unsupported_skill"
    # The message is the only prose the CLI can show, so it must name them all.
    for spec in GOOGLE_SKILLS:
        assert spec.dir_name in payload["message"]


def test_an_uninstalled_skill_is_a_404(wired):
    wired.state["installed"] = False

    resp = asyncio.run(UNLINK(_req(skill="gdrive")))

    assert resp.status_code == 404
    assert _body(resp)["error"] == "skill_not_installed"
    assert "not installed for profile 'alice'" in _body(resp)["message"]


def test_a_clean_unlink_is_a_200(wired):
    resp = asyncio.run(UNLINK(_req(skill="gmail")))

    assert resp.status_code == 200
    assert _body(resp)["unlinked"] is True


def test_a_failed_revoke_is_still_a_200(wired):
    """The local credential is gone; that is the half that matters for safety."""
    wired.state["result"] = {
        "ok": True, "unlinked": True, "still_linked": False,
        "revoked": False, "revoke_error": "http_500: boom",
        "message": "…remove Cremind at https://myaccount.google.com/connections.",
    }

    resp = asyncio.run(UNLINK(_req(skill="gmail")))

    assert resp.status_code == 200
    payload = _body(resp)
    assert "error" not in payload
    assert payload["revoked"] is False
    assert payload["revoke_error"] == "http_500: boom"


def test_a_surviving_credential_file_is_the_only_hard_failure(wired):
    wired.state["result"] = {
        "ok": False, "unlinked": False, "still_linked": True,
        "failed_paths": ["scripts/.google_token.json"],
        "message": "…still holds scripts/.google_token.json…",
    }

    resp = asyncio.run(UNLINK(_req(skill="gdrive")))

    assert resp.status_code == 500
    payload = _body(resp)
    assert payload["error"] == "wipe_failed"
    assert payload["failed_paths"] == ["scripts/.google_token.json"]
    assert "still holds" in payload["message"]


def test_nothing_linked_is_a_200_so_the_ui_can_double_fire(wired):
    wired.state["result"] = {
        "ok": True, "unlinked": False, "already": True, "still_linked": False,
        "reason": "not_linked", "message": "…nothing to unlink.",
    }

    resp = asyncio.run(UNLINK(_req(skill="gmail")))

    assert resp.status_code == 200
    assert _body(resp)["already"] is True


def test_the_body_defaults_to_revoking_and_stopping_the_watch(wired):
    asyncio.run(UNLINK(_req(skill="gmail")))

    assert wired.calls[0]["revoke"] is True
    assert wired.calls[0]["stop_watch"] is True
    assert wired.calls[0]["force_revoke"] is False


def test_a_missing_body_is_treated_as_defaults(wired):
    asyncio.run(UNLINK(_req(skill="gmail", body=None)))

    assert wired.calls[0]["revoke"] is True


def test_revoke_false_reaches_the_engine(wired):
    asyncio.run(UNLINK(_req(skill="gmail", body={"revoke": False})))

    assert wired.calls[0]["revoke"] is False


def test_force_revoke_reaches_the_engine(wired):
    asyncio.run(UNLINK(_req(skill="gmail", body={"force_revoke": True})))

    assert wired.calls[0]["force_revoke"] is True


# ── unlink all ───────────────────────────────────────────────────────────────

def test_unlink_all_returns_every_result(wired):
    wired.state["all"] = {
        "ok": True,
        "results": [{"skill": "gmail"}, {"skill": "gdrive"}],
        "failed": [],
        "unlinked": 2,
    }

    resp = asyncio.run(UNLINK_ALL(_req()))

    assert resp.status_code == 200
    assert [row["skill"] for row in _body(resp)["results"]] == ["gmail", "gdrive"]


def test_unlink_all_reports_a_partial_failure_as_a_500(wired):
    wired.state["all"] = {
        "ok": False, "results": [{"skill": "gsheets"}], "failed": ["gsheets"],
    }

    resp = asyncio.run(UNLINK_ALL(_req()))

    assert resp.status_code == 500
    assert _body(resp)["error"] == "wipe_failed"
    assert _body(resp)["failed"] == ["gsheets"]


# ── the buses ────────────────────────────────────────────────────────────────

def test_unlinking_wakes_the_settings_page(wired):
    asyncio.run(UNLINK(_req("alice", skill="gmail")))

    assert wired.published["settings"] == ["alice"]
    # gmail has nothing to do with the calendar page.
    assert wired.published["calendar"] == []


def test_unlinking_gcalendar_also_wakes_the_calendar_page(wired):
    asyncio.run(UNLINK(_req("alice", skill="gcalendar")))

    assert wired.published["settings"] == ["alice"]
    assert wired.published["calendar"] == ["alice"]


def test_unlink_all_wakes_the_calendar_page_when_gcalendar_was_touched(wired):
    wired.state["all"] = {
        "ok": True, "results": [{"skill": "gmail"}, {"skill": "gcalendar"}], "failed": [],
    }

    asyncio.run(UNLINK_ALL(_req("alice")))

    assert wired.published["calendar"] == ["alice"]


def test_a_publish_failure_never_fails_a_completed_unlink(wired, monkeypatch):
    def explode(_profile):
        raise RuntimeError("bus is down")

    monkeypatch.setattr(
        "app.events.settings_state_bus.publish_settings_state_changed", explode
    )

    resp = asyncio.run(UNLINK(_req(skill="gmail")))

    assert resp.status_code == 200


# ── the Drive index of User Document Search ──────────────────────────────────
#
# A gdrive unlink through Cremind deletes the Drive index at once — but only
# when the link is really gone (engine report AND filesystem), and never for
# another skill's unlink.


@pytest.fixture
def purges(monkeypatch):
    import app.drive.skill_token as drive_token
    from app.userdocs import state as uds_state

    recorded: List[tuple] = []
    token: Dict[str, Any] = {"path": None}  # the gdrive token file as the filesystem sees it
    monkeypatch.setattr(drive_token, "token_path", lambda profile: token["path"])
    uds_state.set_purge_handler(lambda profile, kind: recorded.append((profile, kind)))
    yield SimpleNamespace(recorded=recorded, token=token)
    uds_state.set_purge_handler(None)


def test_a_gdrive_unlink_purges_the_drive_index(wired, purges):
    resp = asyncio.run(UNLINK(_req("alice", skill="gdrive")))

    assert resp.status_code == 200
    assert purges.recorded == [("alice", "drive")]


def test_unlinking_a_gdrive_that_was_not_linked_still_purges(wired, purges):
    """The user asked Cremind to drop Drive; whatever index is left goes too."""
    wired.state["result"] = {"ok": True, "unlinked": False, "already": True, "still_linked": False}

    asyncio.run(UNLINK(_req("alice", skill="gdrive")))

    assert purges.recorded == [("alice", "drive")]


def test_a_failed_wipe_keeps_the_drive_index(wired, purges):
    wired.state["result"] = {"ok": False, "unlinked": False, "still_linked": True}

    resp = asyncio.run(UNLINK(_req("alice", skill="gdrive")))

    assert resp.status_code == 500
    assert purges.recorded == []


def test_a_token_back_on_disk_keeps_the_drive_index(wired, purges):
    """Re-linked from chat between the wipe and the check: a live link again."""
    purges.token["path"] = object()

    asyncio.run(UNLINK(_req("alice", skill="gdrive")))

    assert purges.recorded == []


@pytest.mark.parametrize("skill", ["gcalendar", "gmail", "gsheets", "gdocs"])
def test_other_skills_never_purge_the_drive_index(wired, purges, skill):
    asyncio.run(UNLINK(_req("alice", skill=skill)))

    assert purges.recorded == []


def test_unlink_all_purges_drive_when_gdrive_went(wired, purges):
    wired.state["all"] = {
        "ok": True, "failed": [],
        "results": [{"skill": "gcalendar", "still_linked": False}, {"skill": "gdrive", "still_linked": False}],
    }

    asyncio.run(UNLINK_ALL(_req("alice")))

    assert purges.recorded == [("alice", "drive")]


def test_unlink_all_without_gdrive_or_with_its_wipe_failed_keeps_it(wired, purges):
    wired.state["all"] = {"ok": True, "failed": [], "results": [{"skill": "gmail", "still_linked": False}]}
    asyncio.run(UNLINK_ALL(_req("alice")))

    wired.state["all"] = {
        "ok": False, "failed": ["gdrive"], "results": [{"skill": "gdrive", "still_linked": True}],
    }
    asyncio.run(UNLINK_ALL(_req("alice")))

    assert purges.recorded == []


def test_a_purge_failure_never_fails_a_completed_unlink(wired, purges):
    from app.userdocs import state as uds_state

    def explode(profile, kind):
        raise RuntimeError("engine queue closed")

    uds_state.set_purge_handler(explode)

    resp = asyncio.run(UNLINK(_req("alice", skill="gdrive")))

    assert resp.status_code == 200
    assert wired.published["settings"] == ["alice"]
