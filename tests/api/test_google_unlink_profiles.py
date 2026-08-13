"""Profiles are independent, and unlink is the operation most able to break that.

Everything it touches is either path-scoped (``<profile>/skills/...``) or a
*process-global dict* keyed by profile. The dicts are the risk: a wholesale
``.clear()`` instead of a ``pop(profile)`` works perfectly on a single-profile dev
box and silently logs the other tenant out as soon as a second profile exists.

So this drives the real engine end to end — real files, real cache eviction — with
two linked profiles, and asserts that everything belonging to the other one
survives byte for byte.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import app.api.google as G
import app.calendar.skill_token as calendar_token
import app.drive.grant_flow as grant_flow
import app.drive.skill_token as drive_token
import app.google.unlink as U

SKILLS = ("gcalendar", "gdrive", "gmail")


def _req(profile: str, skill: str):
    async def _json():
        return {}

    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=profile),
        path_params={"skill": skill},
        json=_json,
    )


def _endpoint():
    for route in G.get_google_routes():
        if route.path == "/api/google/accounts/{skill}/unlink":
            return route.endpoint
    raise AssertionError("unlink route missing")


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """``alice`` and ``bob``, each with the same three Google skills linked."""
    monkeypatch.setattr(
        "app.skills.sync.profile_skills_dir", lambda profile: tmp_path / profile / "skills"
    )

    teardowns: List[Dict[str, Any]] = []

    async def fake_teardown(profile, base):
        teardowns.append({"profile": profile, "dir": str(base)})
        return {"stopped": [], "removed_autostart": 0}

    published: List[str] = []

    monkeypatch.setattr(U, "_teardown_listener", fake_teardown)
    monkeypatch.setattr(U, "_stop_watch", lambda spec, data, state: (True, None))
    monkeypatch.setattr(U, "revoke_grant", lambda data: (True, U.REVOKED))
    monkeypatch.setattr(U, "skill_enabled", lambda profile, spec: True)
    monkeypatch.setattr(U, "_idle_subscriptions", lambda profile, spec: 0)
    monkeypatch.setattr(U, "_autostart_row_count", lambda profile, base: 0)
    monkeypatch.setattr(U, "_app_credential_present", lambda profile: False)
    monkeypatch.setattr(U, "_calendar_source", lambda profile: None)
    monkeypatch.setattr(U, "_shared_client_id", lambda: "shared-cid")
    monkeypatch.setattr(
        "app.events.settings_state_bus.publish_settings_state_changed", published.append
    )
    monkeypatch.setattr(
        "app.api.calendar.publish_schedule_events_admin_changed", lambda profile: None
    )

    calendar_token._access_cache.clear()
    drive_token._access_cache.clear()
    grant_flow._pending.clear()

    later = time.time() + 3600
    for profile in ("alice", "bob"):
        for skill in SKILLS:
            scripts = tmp_path / profile / "skills" / skill / "scripts"
            scripts.mkdir(parents=True)
            (scripts / ".env").write_text(f"GOOGLE_SCOPES=\n# {profile}\n", encoding="utf-8")
            (scripts / ".google_token.json").write_text(
                json.dumps(
                    {
                        "access_token": f"at-{profile}",
                        "refresh_token": f"rt-{profile}",
                        "client_id": "shared-cid",
                        "client_secret": "csecret",
                        "scopes": ["openid", "email"],
                        "email": f"{profile}@example.com",
                        "account_key": f"ak-{profile}",
                        "expiry": later,
                    }
                ),
                encoding="utf-8",
            )
        # Per-profile in-memory state that must be evicted surgically.
        drive_token._access_cache[profile] = {"token": f"at-{profile}", "expiry": later}
        calendar_token._access_cache[profile] = {
            "token": f"at-{profile}", "expiry": later, "account": "acct",
        }
        grant_flow._pending[f"state-{profile}"] = {"profile": profile, "ts": time.time()}

    yield SimpleNamespace(root=tmp_path, teardowns=teardowns, published=published)

    calendar_token._access_cache.clear()
    drive_token._access_cache.clear()
    grant_flow._pending.clear()


def _token(root: Path, profile: str, skill: str) -> Path:
    return root / profile / "skills" / skill / "scripts" / ".google_token.json"


def _env(root: Path, profile: str, skill: str) -> Path:
    return root / profile / "skills" / skill / "scripts" / ".env"


def test_unlinking_one_profile_leaves_the_others_files_untouched(two_profiles):
    root = two_profiles.root
    before = _token(root, "bob", "gdrive").read_bytes()

    resp = asyncio.run(_endpoint()(_req("alice", "gdrive")))

    assert resp.status_code == 200
    assert not _token(root, "alice", "gdrive").exists()
    assert _token(root, "bob", "gdrive").read_bytes() == before


def test_unlinking_one_skill_leaves_the_profiles_other_skills_alone(two_profiles):
    root = two_profiles.root

    asyncio.run(_endpoint()(_req("alice", "gdrive")))

    assert not _token(root, "alice", "gdrive").exists()
    assert _token(root, "alice", "gcalendar").exists()
    assert _token(root, "alice", "gmail").exists()


def test_cache_eviction_is_scoped_to_the_acting_profile(two_profiles):
    asyncio.run(_endpoint()(_req("alice", "gdrive")))

    assert "alice" not in drive_token._access_cache
    assert "alice" not in calendar_token._access_cache
    assert drive_token._access_cache["bob"]["token"] == "at-bob"
    assert calendar_token._access_cache["bob"]["token"] == "at-bob"


def test_in_flight_grant_rounds_are_abandoned_only_for_that_profile(two_profiles):
    asyncio.run(_endpoint()(_req("alice", "gdrive")))

    assert list(grant_flow._pending) == ["state-bob"]


def test_the_listener_teardown_targets_only_that_profiles_directory(two_profiles):
    asyncio.run(_endpoint()(_req("alice", "gcalendar")))

    assert len(two_profiles.teardowns) == 1
    call = two_profiles.teardowns[0]
    assert call["profile"] == "alice"
    assert "alice" in call["dir"] and "bob" not in call["dir"]
    assert call["dir"].endswith("gcalendar")


def test_only_the_acting_profile_is_published(two_profiles):
    asyncio.run(_endpoint()(_req("alice", "gmail")))

    assert two_profiles.published == ["alice"]


def test_user_config_survives_the_unlink(two_profiles):
    """``.env`` carries bring-your-own-client settings, not link state."""
    root = two_profiles.root
    before = _env(root, "alice", "gdrive").read_bytes()

    asyncio.run(_endpoint()(_req("alice", "gdrive")))

    assert _env(root, "alice", "gdrive").read_bytes() == before


def test_the_other_profile_still_reports_linked(two_profiles):
    asyncio.run(_endpoint()(_req("alice", "gdrive")))

    bob = U.inventory("bob")
    assert all(row["linked"] for row in bob["skills"] if row["skill"] in SKILLS)
    assert bob["accounts"] == [
        {"email": "bob@example.com", "skills": ["gcalendar", "gdrive", "gmail"], "shared_grant": True}
    ]

    alice = U.inventory("alice")
    assert next(r for r in alice["skills"] if r["skill"] == "gdrive")["linked"] is False


def test_unlink_all_for_one_profile_leaves_the_other_fully_linked(two_profiles):
    root = two_profiles.root

    out = asyncio.run(U.unlink_all("alice"))

    assert out["unlinked"] == 3
    assert out["failed"] == []
    for skill in SKILLS:
        assert not _token(root, "alice", skill).exists()
        assert _token(root, "bob", skill).exists()
    assert "bob" in drive_token._access_cache
    assert list(grant_flow._pending) == ["state-bob"]


def test_one_profiles_shared_grant_never_implicates_anothers(two_profiles):
    """Both profiles use the same OAuth client; only same-profile links are siblings."""
    result = asyncio.run(U.unlink_skill("alice", U.GOOGLE_SKILLS[0]))

    assert sorted(result["siblings_sharing_grant"]) == ["gdrive", "gmail"]
