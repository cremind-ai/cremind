"""`cremind google status` / `unlink`.

The commands do function-body imports, so every patch goes on the **client**
module. Args are mirrored into ``sys.argv`` because the root callback's
profile-resolution gate reads it directly.

Two behaviours carry the most weight. The confirmation caveat is built from the
server's inventory rather than CLI-side constants, so a consequence can never be
phrased differently here than on the settings page — which means an unlink without
``--yes`` must make a GET first. And the exit codes encode which half of the
operation failed: a failed *revoke* is exit 0 (local credentials are gone), while a
credential file that survived the wipe is exit 1.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

import pytest
from typer.testing import CliRunner

import app.cli.client.google as google_client
from app.cli.main import app

runner = CliRunner()


def _invoke(monkeypatch, args: List[str], **kwargs):
    monkeypatch.setattr(sys, "argv", ["cremind", *args])
    return runner.invoke(app, args, **kwargs)


_INVENTORY: Dict[str, Any] = {
    "ok": True,
    "profile": "alice",
    "revoke_url": "https://myaccount.google.com/connections",
    "skills": [
        {
            "skill": "gcalendar",
            "label": "Google Calendar",
            "installed": True,
            "enabled": True,
            "linked": True,
            "email": "u@example.com",
            "listener": {"declared": True, "autostart_rows": 1},
            "watch": {"active": True, "expires_at": 1787181142},
            "subscriptions": {"idle_after_unlink": 2},
            "siblings_sharing_grant": ["gdrive"],
            "consequence": "Cremind stops reading and writing this Google Calendar.",
        },
        {
            "skill": "gdrive",
            "label": "Google Drive",
            "installed": True,
            "enabled": True,
            "linked": True,
            "email": "u@example.com",
            "listener": {"declared": True, "autostart_rows": 0},
            "watch": {"active": False, "expires_at": None},
            "subscriptions": {"idle_after_unlink": 0},
            "siblings_sharing_grant": ["gcalendar"],
            "consequence": "Cremind loses access to every Drive file you granted it.",
        },
        {
            "skill": "gmail",
            "label": "Gmail",
            "installed": True,
            "enabled": False,
            "linked": False,
            "email": None,
            "listener": {"declared": False, "autostart_rows": 0},
            "watch": {"active": False, "expires_at": None},
            "subscriptions": {"idle_after_unlink": 0},
            "siblings_sharing_grant": [],
            "consequence": "Cremind can no longer send mail as this account.",
        },
    ],
    "accounts": [
        {"email": "u@example.com", "skills": ["gcalendar", "gdrive"], "shared_grant": True}
    ],
    "calendar": {"source": "skill", "connected": True, "app_credential_present": False},
}


def _clean_unlink(skill: str = "gcalendar", **over) -> Dict[str, Any]:
    result = {
        "skill": skill,
        "label": "Google Calendar",
        "ok": True,
        "unlinked": True,
        "already": False,
        "email": "u@example.com",
        "revoked": True,
        "revoke_status": "revoked",
        "revoke_error": None,
        "watch_stopped": True,
        "watch_error": None,
        "autostart_removed": 1,
        "cleaned": ["scripts/.google_token.json"],
        "failed_paths": [],
        "still_linked": False,
        "siblings_sharing_grant": [],
        "app_credential_at_risk": False,
        "calendar_source_after": "app",
        "message": "Unlinked Google Calendar (u@example.com).",
    }
    result.update(over)
    return result


@pytest.fixture
def wired(monkeypatch):
    """Patch the client module and record every call the command made."""
    calls: List[Dict[str, Any]] = []
    state: Dict[str, Any] = {"unlink": _clean_unlink(), "all": None}

    async def fake_accounts(client):
        calls.append({"op": "accounts"})
        return _INVENTORY

    async def fake_unlink(client, skill, *, revoke=True, force_revoke=False):
        calls.append(
            {"op": "unlink", "skill": skill, "revoke": revoke, "force_revoke": force_revoke}
        )
        return state["unlink"]

    async def fake_unlink_all(client, *, revoke=True):
        calls.append({"op": "unlink_all", "revoke": revoke})
        return state["all"]

    monkeypatch.setattr(google_client, "get_accounts", fake_accounts)
    monkeypatch.setattr(google_client, "unlink_skill", fake_unlink)
    monkeypatch.setattr(google_client, "unlink_all", fake_unlink_all)
    monkeypatch.setenv("CREMIND_TOKEN", "t")
    return {"calls": calls, "state": state}


def _ops(wired) -> List[str]:
    return [call["op"] for call in wired["calls"]]


# ── status ───────────────────────────────────────────────────────────────────

def test_status_lists_every_installed_skill(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "status"])

    assert result.exit_code == 0
    assert "gcalendar" in result.output
    assert "u@example.com" in result.output
    assert "gmail" in result.output


def test_status_warns_about_the_shared_grant(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "status"])

    assert "Google lists Cremind as one app" in result.output
    assert "gcalendar, gdrive" in result.output


def test_status_json_prints_the_raw_payload(monkeypatch, wired):
    result = _invoke(monkeypatch, ["--json", "google", "status"])

    assert result.exit_code == 0
    assert json.loads(result.output)["accounts"][0]["shared_grant"] is True


# ── argument validation ──────────────────────────────────────────────────────

def test_a_skill_and_all_together_is_refused(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "unlink", "gmail", "--all"])

    assert result.exit_code == 1
    assert "not both" in result.output
    assert _ops(wired) == []


def test_neither_a_skill_nor_all_is_refused(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "unlink"])

    assert result.exit_code == 1
    assert "--all" in result.output
    assert _ops(wired) == []


def test_an_unknown_skill_is_refused_before_any_unlink(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "unlink", "nope"])

    assert result.exit_code == 1
    assert "not a Google Suite skill" in result.output
    assert "unlink" not in _ops(wired)


# ── the confirmation ─────────────────────────────────────────────────────────

def test_yes_skips_the_confirm_and_the_inventory_fetch(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "unlink", "gcalendar", "--yes"])

    assert result.exit_code == 0
    assert _ops(wired) == ["unlink"]


def test_the_caveat_is_built_from_the_servers_inventory(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "unlink", "gcalendar"], input="y\n")

    assert result.exit_code == 0
    assert _ops(wired) == ["accounts", "unlink"]
    # consequence, listener deregistration, shared grant, idle automations
    assert "Cremind stops reading and writing this Google Calendar." in result.output
    assert "autostart registration is removed" in result.output
    assert "gdrive" in result.output
    assert "2 event automation(s)" in result.output


def test_declining_the_confirm_unlinks_nothing(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "unlink", "gcalendar"], input="n\n")

    assert result.exit_code == 1
    assert _ops(wired) == ["accounts"]


def test_an_unlinked_skill_short_circuits(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "unlink", "gmail"])

    assert result.exit_code == 0
    assert "nothing to unlink" in result.output
    assert _ops(wired) == ["accounts"]


def test_no_revoke_is_reflected_in_the_caveat(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "unlink", "gcalendar", "--no-revoke"], input="y\n")

    assert "leaves the grant live at Google" in result.output
    assert wired["calls"][-1]["revoke"] is False


def test_force_revoke_reaches_the_client(monkeypatch, wired):
    _invoke(monkeypatch, ["google", "unlink", "gcalendar", "--yes", "--force-revoke"])

    assert wired["calls"][-1]["force_revoke"] is True


# ── reporting one skill ──────────────────────────────────────────────────────

def test_a_clean_unlink_reports_the_facts(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "unlink", "gcalendar", "--yes"])

    assert result.exit_code == 0
    assert "unlinked gcalendar (u@example.com)" in result.output
    assert "revoked at Google: yes" in result.output
    assert "autostart registration removed" in result.output
    assert "account connected on that page" in result.output


def test_a_failed_revoke_is_exit_zero_but_loud(monkeypatch, wired):
    """Local credentials are gone — the half that matters — so this is not a failure."""
    wired["state"]["unlink"] = _clean_unlink(
        revoked=False, revoke_status="http_500: boom", revoke_error="http_500: boom"
    )

    result = _invoke(monkeypatch, ["google", "unlink", "gcalendar", "--yes"])

    assert result.exit_code == 0
    assert "revoked at Google: no" in result.output
    assert "Google was NOT told" in result.output
    assert "myaccount.google.com/connections" in result.output
    # Never suggest a retry: the token that could revoke it is gone for good.
    assert "re-running this will not help" in result.output


def test_a_surviving_credential_file_is_exit_one(monkeypatch, wired):
    from app.cli.client._base import APIError

    payload = {
        "error": "wipe_failed",
        "still_linked": True,
        "failed_paths": ["scripts/.google_token.json"],
        "message": (
            "Google access for u@example.com IS revoked, but this machine still holds "
            "scripts/.google_token.json for Google Drive."
        ),
    }

    async def boom(client, skill, *, revoke=True, force_revoke=False):
        raise APIError(
            status=500, body="wipe_failed", raw=json.dumps(payload).encode()
        )

    monkeypatch.setattr(google_client, "unlink_skill", boom)

    result = _invoke(monkeypatch, ["google", "unlink", "gdrive", "--yes"])

    assert result.exit_code == 1
    # The prose only exists in `message`, which _check_response drops from `error`.
    assert "still holds scripts/.google_token.json" in result.output


def test_the_at_risk_app_credential_is_surfaced(monkeypatch, wired):
    wired["state"]["unlink"] = _clean_unlink(app_credential_at_risk=True)

    result = _invoke(monkeypatch, ["google", "unlink", "gcalendar", "--yes"])

    assert "may have ended it too" in result.output


def test_a_failed_revoke_is_not_explained_twice(monkeypatch, wired):
    """The revoke-failure block already says it; the server sentence would repeat it."""
    wired["state"]["unlink"] = _clean_unlink(
        revoked=False,
        revoke_status="http_500: boom",
        revoke_error="http_500: boom",
        siblings_sharing_grant=["gdrive"],
        message="UNIQUE-SERVER-SENTENCE",
    )

    result = _invoke(monkeypatch, ["google", "unlink", "gcalendar", "--yes"])

    assert result.exit_code == 0
    assert "Google was NOT told" in result.output
    assert "UNIQUE-SERVER-SENTENCE" not in result.output


def test_gcalendar_always_reports_where_the_calendar_went(monkeypatch, wired):
    wired["state"]["unlink"] = _clean_unlink(calendar_source_after=None)

    result = _invoke(monkeypatch, ["google", "unlink", "gcalendar", "--yes"])

    assert "built-in system calendar" in result.output


def test_a_suppressed_revoke_explains_itself(monkeypatch, wired):
    wired["state"]["unlink"] = _clean_unlink(
        revoked=False,
        revoke_status="skipped_shared_grant",
        siblings_sharing_grant=["gdrive"],
        message="Unlinked Google Calendar on this machine only… use --all…",
    )

    result = _invoke(monkeypatch, ["google", "unlink", "gcalendar", "--yes"])

    assert result.exit_code == 0
    assert "--all" in result.output


# ── unlink --all ─────────────────────────────────────────────────────────────

def test_all_renders_a_per_skill_table(monkeypatch, wired):
    wired["state"]["all"] = {
        "ok": True,
        "unlinked": 2,
        "already": 1,
        "failed": [],
        "results": [
            _clean_unlink("gcalendar"),
            _clean_unlink("gdrive", revoked=True, revoke_status="already_revoked"),
            {"skill": "gmail", "unlinked": False, "already": True, "still_linked": False},
        ],
        "message": "2 unlinked, 1 not linked.",
    }

    result = _invoke(monkeypatch, ["google", "unlink", "--all", "--yes"])

    assert result.exit_code == 0
    assert "unlinked" in result.output
    assert "already" in result.output
    assert "not linked" in result.output
    assert "2 unlinked, 1 not linked." in result.output


def test_all_exits_one_when_a_credential_file_survived(monkeypatch, wired):
    from app.cli.client._base import APIError

    payload = {
        "error": "wipe_failed",
        "failed": ["gsheets"],
        "results": [
            _clean_unlink("gmail"),
            {
                "skill": "gsheets",
                "unlinked": False,
                "still_linked": True,
                "failed_paths": ["scripts/.google_token.json"],
            },
        ],
        "message": "1 unlinked, 1 still holds a credential file.",
    }

    async def boom(client, *, revoke=True):
        raise APIError(status=500, body="wipe_failed", raw=json.dumps(payload).encode())

    monkeypatch.setattr(google_client, "unlink_all", boom)

    result = _invoke(monkeypatch, ["google", "unlink", "--all", "--yes"])

    assert result.exit_code == 1
    assert "failed" in result.output
    assert "still holds a credential file" in result.output


def test_all_confirms_with_every_account_named(monkeypatch, wired):
    wired["state"]["all"] = {"ok": True, "unlinked": 2, "already": 0, "failed": [], "results": []}

    result = _invoke(monkeypatch, ["google", "unlink", "--all"], input="y\n")

    assert result.exit_code == 0
    assert "u@example.com (gcalendar, gdrive)" in result.output
    assert "Drive file grants are lost permanently" in result.output


def test_all_declined_unlinks_nothing(monkeypatch, wired):
    result = _invoke(monkeypatch, ["google", "unlink", "--all"], input="n\n")

    assert result.exit_code == 1
    assert "unlink_all" not in _ops(wired)


def test_all_with_nothing_linked_short_circuits(monkeypatch, wired):
    async def empty(client):
        return {"ok": True, "skills": [], "accounts": []}

    monkeypatch.setattr(google_client, "get_accounts", empty)

    result = _invoke(monkeypatch, ["google", "unlink", "--all"])

    assert result.exit_code == 0
    assert "No Google accounts are linked" in result.output


def test_all_json_prints_the_raw_payload(monkeypatch, wired):
    wired["state"]["all"] = {"ok": True, "unlinked": 1, "failed": [], "results": []}

    result = _invoke(monkeypatch, ["--json", "google", "unlink", "--all", "--yes"])

    assert json.loads(result.output)["unlinked"] == 1
