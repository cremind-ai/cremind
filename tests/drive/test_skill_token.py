"""Tests for reading the gdrive skill's token from the backend.

The invariant that matters most here: this module must never write the token
file. The skill and its listener are the only writers, and a third writer would
race them into a corrupt file that breaks linking.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import app.drive.skill_token as st

DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
LEGACY = "https://www.googleapis.com/auth/drive"


@pytest.fixture
def skill(tmp_path, monkeypatch):
    """A fake installed gdrive skill dir with a linked token."""
    scripts = tmp_path / "alice" / "skills" / "gdrive" / "scripts"
    scripts.mkdir(parents=True)
    token = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "client_id": "cid",
        "client_secret": "csecret",
        "scopes": [DRIVE_FILE, "openid", "email"],
        "email": "user@example.com",
        "expiry": time.time() + 3600,
    }
    (scripts / ".google_token.json").write_text(json.dumps(token), encoding="utf-8")
    monkeypatch.setattr(
        "app.skills.sync.profile_skills_dir", lambda profile: tmp_path / profile / "skills"
    )
    st._access_cache.clear()
    return scripts


def test_status_reports_the_linked_account(skill):
    out = st.status("alice")
    assert out == {
        "linked": True, "email": "user@example.com",
        "scopes": [DRIVE_FILE, "openid", "email"], "scopes_stale": False,
    }


def test_status_when_not_linked(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.skills.sync.profile_skills_dir", lambda profile: tmp_path / profile / "skills"
    )
    assert st.status("nobody")["linked"] is False


def test_stale_scope_detection():
    assert st.scopes_are_stale([LEGACY]) is True
    assert st.scopes_are_stale([LEGACY, DRIVE_FILE]) is False
    assert st.scopes_are_stale([DRIVE_FILE]) is False
    assert st.scopes_are_stale(None) is False


def test_unexpired_access_token_is_reused(skill):
    assert st.access_token("alice") == "at-1"


def test_expired_token_refreshes_without_writing_the_file(skill, monkeypatch):
    path = skill / ".google_token.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expiry"] = time.time() - 10
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()

    class FakeResp:
        def raise_for_status(self): return None
        def json(self): return {"access_token": "at-2", "expires_in": 3600}

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return FakeResp()

    monkeypatch.setattr(st.httpx, "Client", FakeClient)
    assert st.access_token("alice") == "at-2"
    assert path.read_bytes() == before, "the backend must never rewrite the skill's token"
    # Cached, so a second call does not re-refresh.
    monkeypatch.setattr(st.httpx, "Client", None)
    assert st.access_token("alice") == "at-2"


def test_missing_refresh_token_is_an_actionable_error(skill):
    path = skill / ".google_token.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update({"expiry": 0, "refresh_token": ""})
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(st.DriveTokenError, match="re-link"):
        st.access_token("alice")


def test_list_files_maps_the_drive_payload(skill, monkeypatch):
    class FakeResp:
        status_code = 200
        def raise_for_status(self): return None
        def json(self):
            return {
                "files": [
                    {"id": "f1", "name": "Report", "mimeType": "text/plain",
                     "modifiedTime": "2026-07-01T00:00:00Z", "webViewLink": "http://x"},
                ],
                "nextPageToken": "np",
            }

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return FakeResp()

    monkeypatch.setattr(st.httpx, "Client", FakeClient)
    out = st.list_files("alice")
    assert out["next_page_token"] == "np"
    assert out["files"][0]["id"] == "f1"
    assert out["files"][0]["mime_type"] == "text/plain"
    # No recorded provenance yet, so origin is blank rather than claiming "picked".
    assert out["files"][0]["origin"] == ""


def test_get_file_treats_403_and_404_as_unreachable(skill, monkeypatch):
    for status in (403, 404):
        class FakeResp:
            status_code = status
            def raise_for_status(self): return None
            def json(self): return {}

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, *a, **k): return FakeResp()

        monkeypatch.setattr(st.httpx, "Client", FakeClient)
        assert st.get_file("alice", "nope") is None


def test_grant_provenance_round_trip(skill):
    st.record_grants("alice", [{"id": "f1", "name": "One", "mime_type": "text/plain"}])
    st.record_grants("alice", [{"id": "f1", "name": "One", "mime_type": "text/plain"}])
    entries = st.read_grants("alice")
    assert len(entries) == 1, "recording the same file twice must not duplicate it"
    assert entries[0]["source"] == "picker"


def test_grant_provenance_survives_unreadable_cache(skill):
    (skill / ".drive_grants.json").write_text("{not json", encoding="utf-8")
    assert st.read_grants("alice") == []
    st.record_grants("alice", [{"id": "f2", "name": "Two", "mime_type": "text/plain"}])
    assert [e["id"] for e in st.read_grants("alice")] == ["f2"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://drive.google.com/file/d/ABC123/view", "ABC123"),
        ("https://docs.google.com/document/d/DOC1/edit", "DOC1"),
        ("https://docs.google.com/spreadsheets/d/SHEET1/edit#gid=0", "SHEET1"),
        ("https://drive.google.com/drive/folders/FOLDER1", "FOLDER1"),
        ("https://drive.google.com/open?id=OPEN1", "OPEN1"),
        ("  BAREID  ", "BAREID"),
        ("", ""),
    ],
)
def test_parse_file_reference(value, expected):
    assert st.parse_file_reference(value) == expected
