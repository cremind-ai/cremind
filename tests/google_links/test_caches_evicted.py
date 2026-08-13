"""The caches that outlive the file.

``app.drive.skill_token.access_token`` consults its in-memory cache *before*
reading the token file, and the entry is not pinned to an account — so without an
eviction a deleted link keeps serving a working Drive token for up to an hour, with
full access if the revoke also failed. That regression is what this file exists to
prevent.

Everything here is keyed by profile, so the other assertion that matters is that
eviction is surgical: another profile's cached token must survive.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import app.calendar.skill_token as calendar_token
import app.drive.grant_flow as grant_flow
import app.drive.skill_token as drive_token
import app.google.unlink as U
from app.google.registry import by_name


@pytest.fixture(autouse=True)
def clean_caches():
    calendar_token._access_cache.clear()
    drive_token._access_cache.clear()
    grant_flow._pending.clear()
    yield
    calendar_token._access_cache.clear()
    drive_token._access_cache.clear()
    grant_flow._pending.clear()


def _seed(profile: str) -> None:
    later = time.time() + 3600
    drive_token._access_cache[profile] = {"token": f"at-{profile}", "expiry": later}
    calendar_token._access_cache[profile] = {
        "token": f"at-{profile}", "expiry": later, "account": "acct",
    }
    grant_flow._pending[f"state-{profile}"] = {"profile": profile, "ts": time.time()}


def test_unlinking_evicts_every_cache_for_that_profile(google):
    _seed("alice")
    google.link("alice", "gdrive")

    asyncio.run(U.unlink_skill("alice", by_name("gdrive")))

    assert "alice" not in drive_token._access_cache
    assert "alice" not in calendar_token._access_cache
    assert grant_flow._pending == {}


def test_the_drive_cache_can_no_longer_hand_out_a_token(google):
    """The actual regression: a cached token surviving the file's deletion."""
    _seed("alice")
    google.link("alice", "gdrive")
    assert drive_token.access_token("alice") == "at-alice"  # served from cache

    asyncio.run(U.unlink_skill("alice", by_name("gdrive")))

    with pytest.raises(drive_token.DriveTokenError):
        drive_token.access_token("alice")


def test_eviction_never_touches_another_profile(google):
    _seed("alice")
    _seed("bob")
    google.link("alice", "gdrive")
    google.link("bob", "gdrive")

    asyncio.run(U.unlink_skill("alice", by_name("gdrive")))

    assert "bob" in drive_token._access_cache
    assert "bob" in calendar_token._access_cache
    assert list(grant_flow._pending) == ["state-bob"]
    assert google.token_file("bob", "gdrive").exists()


def test_every_google_skill_evicts_the_caches_not_just_drive(google):
    """Conditioning eviction on skill identity is one more thing to get wrong."""
    for skill in ("gmail", "gsheets", "gdocs", "gcalendar"):
        _seed("alice")
        google.link("alice", skill)

        asyncio.run(U.unlink_skill("alice", by_name(skill)))

        assert "alice" not in drive_token._access_cache, skill
        assert "alice" not in calendar_token._access_cache, skill
        assert grant_flow._pending == {}, skill


def test_abandon_rounds_reports_what_it_dropped():
    grant_flow._pending.update(
        {
            "s1": {"profile": "alice"},
            "s2": {"profile": "alice"},
            "s3": {"profile": "bob"},
        }
    )

    assert grant_flow.abandon_rounds("alice") == 2
    assert list(grant_flow._pending) == ["s3"]
    assert grant_flow.abandon_rounds("nobody") == 0


def test_forget_access_token_is_idempotent():
    calendar_token.forget_access_token("never-cached")
    drive_token.forget_access_token("never-cached")
