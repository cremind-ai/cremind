"""The teardown order is the feature.

Every other property of unlink can be got wrong and recovered from. The order
cannot: stopping the Google push channel needs a live credential, so it has to
precede the revoke; revoking needs the refresh token, so it has to precede the
delete; and the listener re-creates the channel and rewrites the token file, so it
has to die before either. Get the order wrong and you leak a push channel that
can never be stopped — which is exactly the state a real install was found in.
"""

from __future__ import annotations

import asyncio

import pytest

import app.google.unlink as U
from app.google.registry import by_name


def test_the_teardown_runs_in_the_order_that_works(google):
    google.link("alice", "gcalendar")
    google.watch_state("alice", "gcalendar")

    result = asyncio.run(U.unlink_skill("alice", by_name("gcalendar")))

    assert google.events == [
        "teardown:gcalendar",
        "watch_stop:gcalendar",
        "revoke",
        "delete:gcalendar",
        "forget",
    ]
    assert result["unlinked"] is True
    assert result["ok"] is True


def test_the_watch_is_stopped_before_the_revoke_kills_the_credential(google):
    google.link("alice", "gdrive")
    google.watch_state("alice", "gdrive")

    asyncio.run(U.unlink_skill("alice", by_name("gdrive")))

    assert google.index("watch_stop") < google.index("revoke")
    assert google.stopped_channels == [("gdrive", "cm-abc")]


def test_the_listener_dies_before_anything_is_deleted(google):
    """A live listener rewrites the token file after any successful refresh."""
    google.link("alice", "gcalendar")

    asyncio.run(U.unlink_skill("alice", by_name("gcalendar")))

    assert google.index("teardown") < google.index("delete")


def test_no_revoke_never_contacts_google_but_still_wipes(google, monkeypatch):
    def boom(_data):
        raise AssertionError("revoke must not be attempted when revoke=False")

    monkeypatch.setattr(U, "revoke_grant", boom)
    google.link("alice", "gmail")

    result = asyncio.run(U.unlink_skill("alice", by_name("gmail"), revoke=False))

    assert result["unlinked"] is True
    assert result["revoke_attempted"] is False
    assert result["revoke_status"] == U.SKIPPED
    assert not google.token_file("alice", "gmail").exists()
    assert "the grant is still live at Google" in result["message"]


def test_unlink_all_stops_every_watch_before_any_revoke(google):
    """Phased, not looped.

    Revoking the first skill can end a grant the second one shares, leaving its
    ``channels.stop`` holding a dead token — so every watch must be closed while
    every credential is still valid.
    """
    for skill in ("gcalendar", "gdrive"):
        google.link("alice", skill)
        google.watch_state("alice", skill)
    for skill in ("gmail", "gsheets", "gdocs"):
        google.link("alice", skill)

    out = asyncio.run(U.unlink_all("alice"))

    watch_stops = [i for i, e in enumerate(google.events) if e.startswith("watch_stop")]
    revokes = [i for i, e in enumerate(google.events) if e == "revoke"]
    assert watch_stops and revokes
    assert max(watch_stops) < min(revokes)
    assert out["unlinked"] == 5
    assert out["failed"] == []
    assert sorted(google.stopped_channels) == [("gcalendar", "cm-abc"), ("gdrive", "cm-abc")]


def test_unlink_all_evicts_the_caches_exactly_once(google):
    for skill in ("gcalendar", "gdrive", "gmail"):
        google.link("alice", skill)

    asyncio.run(U.unlink_all("alice"))

    assert google.events.count("forget") == 1


def test_unlink_all_revokes_every_skill_despite_the_shared_grant(google):
    """The shared-grant guard protects siblings that are staying. None are."""
    for skill in ("gcalendar", "gdrive", "gmail"):
        google.link("alice", skill, refresh=f"rt-{skill}")

    asyncio.run(U.unlink_all("alice"))

    assert sorted(google.revoked_tokens) == ["rt-gcalendar", "rt-gdrive", "rt-gmail"]


def test_unlink_all_skips_skills_that_are_not_installed(google):
    google.link("alice", "gmail")

    out = asyncio.run(U.unlink_all("alice"))

    assert [row["skill"] for row in out["results"]] == ["gmail"]
    assert out["unlinked"] == 1
