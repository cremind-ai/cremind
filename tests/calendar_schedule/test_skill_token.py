"""Tests for reading the gcalendar skill's token from the backend.

Two invariants carry the feature. This module must never write the token file —
the skill and its listener are its only writers, and a third would race them into
a corrupt file that breaks linking. And the link only counts while the skill is
actually enabled, because that is what makes the Calendar & Schedule page's own
Connect button meaningful again when the skill is switched off.
"""

from __future__ import annotations

import json
import time

import pytest

import app.calendar.skill_token as st

EVENTS = st.CALENDAR_EVENTS_SCOPE
BROAD = st.BROAD_CALENDAR_SCOPE
# Google records the granted email scope in its own long form, not as "email".
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"


def _token(**over):
    data = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "client_id": "cid",
        "client_secret": "csecret",
        "scopes": ["openid", USERINFO_EMAIL, EVENTS],
        "email": "user@example.com",
        "account_key": "keyabc",
        "expiry": time.time() + 3600,
    }
    data.update(over)
    return data


def _enable(monkeypatch, rows):
    class FakeToolStorage:
        def list_profile_tools(self, profile):
            return dict(rows)

    monkeypatch.setattr("app.storage.tool_storage.get_tool_storage", lambda: FakeToolStorage())


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _fake_httpx(monkeypatch, payload, calls=None):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, data=None):
            if calls is not None:
                calls.append(data or {})
            return FakeResp(payload)

    monkeypatch.setattr(st.httpx, "Client", FakeClient)


@pytest.fixture
def skill(tmp_path, monkeypatch):
    """An installed, enabled gcalendar skill with a linked token, for "alice"."""
    scripts = tmp_path / "alice" / "skills" / "gcalendar" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / ".google_token.json").write_text(json.dumps(_token()), encoding="utf-8")
    monkeypatch.setattr(
        "app.skills.sync.profile_skills_dir", lambda profile: tmp_path / profile / "skills"
    )
    _enable(monkeypatch, {"alice__gcalendar": True})
    st._access_cache.clear()
    return scripts


def _rewrite(scripts, **over):
    path = scripts / ".google_token.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(over)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ── status / precedence gate ────────────────────────────────────────────────

def test_status_reports_the_linked_account(skill):
    assert st.status("alice") == {
        "linked": True,
        "enabled": True,
        "effective": True,
        "email": "user@example.com",
        "scopes": ["openid", USERINFO_EMAIL, EVENTS],
        "account_key": "keyabc",
    }
    assert st.is_effective("alice") is True


def test_a_disabled_skill_does_not_drive_the_calendar(skill, monkeypatch):
    """Switching the skill off must hand the calendar back to the page."""
    _enable(monkeypatch, {"alice__gcalendar": False})
    out = st.status("alice")
    assert out["linked"] is True, "the token file is still there"
    assert (out["enabled"], out["effective"]) == (False, False)
    assert st.is_effective("alice") is False

    # Never enabled at all: skills have no profile_tools row until they are.
    _enable(monkeypatch, {})
    assert st.is_effective("alice") is False


def test_not_linked_without_a_token_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.skills.sync.profile_skills_dir", lambda profile: tmp_path / profile / "skills"
    )
    assert st.status("nobody")["linked"] is False
    assert st.is_effective("nobody") is False
    assert st.access_token("nobody") is None


def test_an_unreadable_token_reads_as_not_linked(skill):
    """The skill replaces this file atomically, so a read can land mid-swap.

    That must look like "not linked right now" and be retried, never raise into
    a request that was only asking which calendar to show.
    """
    (skill / ".google_token.json").write_text("{not json", encoding="utf-8")
    assert st.read_token("alice") is None
    assert st.is_effective("alice") is False
    assert st.access_token("alice") is None


def test_scope_gate_accepts_granted_forms_and_rejects_unrelated_grants(skill):
    """Membership, not list equality: Google returns scopes in its own forms."""
    assert st.has_calendar_scope(_token()) is True
    assert st.has_calendar_scope(_token(scopes=["openid", USERINFO_EMAIL, BROAD])) is True
    # A gmail-only link is a real possibility (the skills share one OAuth client).
    gmail_only = ["openid", USERINFO_EMAIL, "https://www.googleapis.com/auth/gmail.send"]
    assert st.has_calendar_scope(_token(scopes=gmail_only)) is False
    assert st.has_calendar_scope({}) is False

    _rewrite(skill, scopes=gmail_only)
    assert st.is_effective("alice") is False, "a link that cannot touch events must not win"


# ── access tokens ───────────────────────────────────────────────────────────

def test_the_files_unexpired_token_is_used_as_is(skill, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not refresh a token that is still valid")

    monkeypatch.setattr(st.httpx, "Client", boom)
    assert st.access_token("alice") == "at-1"


def test_expired_token_refreshes_in_memory_without_writing_the_file(skill, monkeypatch):
    path = _rewrite(skill, expiry=time.time() - 10)
    before = path.read_bytes()
    calls: list = []
    _fake_httpx(monkeypatch, {"access_token": "at-2", "expires_in": 3600}, calls)

    assert st.access_token("alice") == "at-2"
    assert path.read_bytes() == before, "the backend must never rewrite the skill's token"
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "rt-1"

    # Cached in memory, so a second call does not re-refresh.
    monkeypatch.setattr(st.httpx, "Client", None)
    assert st.access_token("alice") == "at-2"


def test_a_failed_refresh_returns_none_rather_than_raising(skill, monkeypatch):
    _rewrite(skill, expiry=time.time() - 10)

    class FailingClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            raise RuntimeError("Google said no")

    monkeypatch.setattr(st.httpx, "Client", FailingClient)
    assert st.access_token("alice") is None


def test_a_token_without_a_refresh_token_returns_none(skill, monkeypatch):
    _rewrite(skill, expiry=0, refresh_token="")
    monkeypatch.setattr(st.httpx, "Client", None)  # must not even try
    assert st.access_token("alice") is None


def test_relinking_another_account_never_serves_the_cached_token(skill, monkeypatch):
    """The whole point of precedence is that a re-link takes effect at once.

    The file is authoritative; a token cached for the previous account must not
    outlive it, or the calendar would keep writing into the old one.
    """
    _rewrite(skill, expiry=time.time() - 10)
    _fake_httpx(monkeypatch, {"access_token": "at-old", "expires_in": 3600})
    assert st.access_token("alice") == "at-old"

    # The user re-links as somebody else; the fresh file token wins immediately.
    _rewrite(skill, email="other@example.com", refresh_token="rt-2",
             access_token="at-new", expiry=time.time() + 3600)
    monkeypatch.setattr(st.httpx, "Client", None)
    assert st.access_token("alice") == "at-new"

    # And when that one expires, the stale cache entry is not reused either.
    _rewrite(skill, expiry=time.time() - 10)
    calls: list = []
    _fake_httpx(monkeypatch, {"access_token": "at-new-2", "expires_in": 3600}, calls)
    assert st.access_token("alice") == "at-new-2"
    assert calls[0]["refresh_token"] == "rt-2"


def test_a_token_file_deleted_mid_flight_reads_as_disconnected(skill):
    """Resetting the skill deletes the file; nothing may crash on the next call."""
    assert st.is_effective("alice") is True
    (skill / ".google_token.json").unlink()
    assert st.is_effective("alice") is False
    assert st.access_token("alice") is None
    assert st.status("alice")["linked"] is False


def test_a_nonsense_expiry_forces_a_refresh_instead_of_raising(skill, monkeypatch):
    _rewrite(skill, expiry="soon")
    _fake_httpx(monkeypatch, {"access_token": "at-3", "expires_in": 3600})
    assert st.access_token("alice") == "at-3"
