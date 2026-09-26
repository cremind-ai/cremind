"""CLI: each profile's working directory — `profile working-dir`, the
`create`/`delete` options, `me`, and the retired `server-config` key.

The server is stubbed at the client-wrapper layer (the commands import the
wrappers inside their bodies, so they are patched on their SOURCE module).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from typer.testing import CliRunner

from app.cli.client._base import APIError
from app.cli.client.me import Me


def _run(monkeypatch: pytest.MonkeyPatch, args: list[str], **kw):
    from app.cli.main import app

    full = ["--token", "dummy", *args]
    monkeypatch.setattr(sys, "argv", ["cremind", *full])
    return CliRunner().invoke(app, full, **kw)


_VIEW = {
    "profile": "javis",
    "path": "/home/li/.cremind/workspaces/javis",
    "default_path": "/home/li/.cremind/workspaces/javis",
    "is_default": True,
    "exists": True,
}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import app.cli.client.me as me_client
    import app.cli.client.profiles as profiles_client

    calls: dict[str, Any] = {"get": [], "set": [], "me": 0, "delete": [], "create": []}
    state: dict[str, Any] = {"me": "javis", "set_error": None, "delete_response": {}}

    async def _get_me(_client):
        calls["me"] += 1
        return Me.from_dict({"profile": state["me"], "sub": state["me"]})

    async def _get_working_dir(_client, name):
        calls["get"].append(name)
        return {**_VIEW, "profile": name}

    async def _set_working_dir(_client, name, path):
        calls["set"].append((name, path))
        if state["set_error"] is not None:
            raise state["set_error"]
        if path is None:
            return {**_VIEW, "profile": name}
        return {**_VIEW, "profile": name, "path": path, "is_default": False}

    async def _delete_profile(_client, name, *, delete_working_dir=False):
        calls["delete"].append((name, delete_working_dir))
        return state["delete_response"]

    async def _create_profile(_client, name, working_dir=None):
        calls["create"].append((name, working_dir))
        return {"profile": name, "working_dir": {"path": working_dir or f"/ws/{name}", "is_default": not working_dir}}

    monkeypatch.setattr(me_client, "get_me", _get_me)
    monkeypatch.setattr(profiles_client, "get_working_dir", _get_working_dir)
    monkeypatch.setattr(profiles_client, "set_working_dir", _set_working_dir)
    monkeypatch.setattr(profiles_client, "delete_profile", _delete_profile)
    monkeypatch.setattr(profiles_client, "create_profile", _create_profile)
    return {"calls": calls, "state": state}


# ── working-dir ──────────────────────────────────────────────────────────────


def test_no_arguments_shows_your_own_folder(monkeypatch, server) -> None:
    result = _run(monkeypatch, ["profile", "working-dir"])
    assert result.exit_code == 0, result.output
    assert server["calls"]["get"] == ["javis"], "resolved from the token's profile"
    assert "/home/li/.cremind/workspaces/javis" in result.stdout
    assert "default" in result.stdout and "yes" in result.stdout
    assert server["calls"]["set"] == []


def test_a_name_shows_that_profiles_folder(monkeypatch, server) -> None:
    result = _run(monkeypatch, ["profile", "working-dir", "admin"])
    assert result.exit_code == 0, result.output
    assert server["calls"]["get"] == ["admin"]
    assert server["calls"]["me"] == 0


def test_a_name_and_a_path_set_it(monkeypatch, server) -> None:
    result = _run(monkeypatch, ["profile", "working-dir", "javis", "/srv/team"])
    assert result.exit_code == 0, result.output
    assert server["calls"]["set"] == [("javis", "/srv/team")]
    assert "/srv/team" in result.stdout


def test_a_lone_path_sets_your_own(monkeypatch, server) -> None:
    result = _run(monkeypatch, ["profile", "working-dir", "C:\\Work\\mine"])
    assert result.exit_code == 0, result.output
    assert server["calls"]["set"] == [("javis", "C:\\Work\\mine")]


def test_default_resets_to_null(monkeypatch, server) -> None:
    result = _run(monkeypatch, ["profile", "working-dir", "javis", "--default"])
    assert result.exit_code == 0, result.output
    assert server["calls"]["set"] == [("javis", None)]


def test_a_path_and_default_together_are_refused(monkeypatch, server) -> None:
    result = _run(monkeypatch, ["profile", "working-dir", "javis", "/x", "--default"])
    assert result.exit_code == 1
    assert "not both" in result.stderr
    assert server["calls"]["set"] == []


def test_json_prints_the_servers_view(monkeypatch, server) -> None:
    result = _run(monkeypatch, ["--json", "profile", "working-dir", "javis"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["is_default"] is True


def test_a_refusal_prints_the_servers_reason_cleanly(monkeypatch, server) -> None:
    raw = json.dumps({
        "error": "InvalidWorkingDir", "code": "inside_system_dir",
        "message": "That folder is inside Cremind's system folder.",
    }).encode()
    server["state"]["set_error"] = APIError(
        400, body="InvalidWorkingDir: That folder is inside Cremind's system folder.", raw=raw,
    )
    result = _run(monkeypatch, ["profile", "working-dir", "javis", "/home/li/.cremind/x"])
    assert result.exit_code == 1
    assert "InvalidWorkingDir: That folder is inside Cremind's system folder." in result.stderr
    assert "Traceback" not in result.output


# ── create / delete ──────────────────────────────────────────────────────────


def test_create_passes_the_chosen_folder_and_keeps_stdout_clean(monkeypatch, server) -> None:
    result = _run(monkeypatch, ["profile", "create", "bob", "--working-dir", "/srv/bob"])
    assert result.exit_code == 0, result.output
    assert server["calls"]["create"] == [("bob", "/srv/bob")]
    assert result.stdout == "bob\n"
    assert "Working directory: /srv/bob" in result.stderr


def test_create_without_one_gets_the_default(monkeypatch, server) -> None:
    result = _run(monkeypatch, ["profile", "create", "bob"])
    assert result.exit_code == 0, result.output
    assert server["calls"]["create"] == [("bob", None)]
    assert "Working directory: /ws/bob" in result.stderr


def test_delete_keeps_the_folder_by_default_and_says_where(monkeypatch, server) -> None:
    server["state"]["delete_response"] = {
        "success": True,
        "working_dir": {"action": "archived", "path": "/ws/javis", "archived_to": "/ws/.deleted/javis-1"},
    }
    result = _run(monkeypatch, ["profile", "delete", "javis"])
    assert result.exit_code == 0, result.output
    assert server["calls"]["delete"] == [("javis", False)]
    assert "moved to /ws/.deleted/javis-1" in result.stdout


def test_delete_working_dir_flag_asks_for_removal(monkeypatch, server) -> None:
    server["state"]["delete_response"] = {
        "success": True, "working_dir": {"action": "deleted", "path": "/ws/javis"},
    }
    result = _run(monkeypatch, ["profile", "delete", "javis", "--delete-working-dir"])
    assert result.exit_code == 0, result.output
    assert server["calls"]["delete"] == [("javis", True)]
    assert "Working directory deleted: /ws/javis" in result.stdout


def test_a_folder_chosen_elsewhere_is_reported_as_left_alone(monkeypatch, server) -> None:
    server["state"]["delete_response"] = {
        "success": True, "working_dir": {"action": "untouched", "path": "/srv/javis"},
    }
    result = _run(monkeypatch, ["profile", "delete", "javis", "--delete-working-dir"])
    assert result.exit_code == 0, result.output
    assert "left in place: /srv/javis" in result.stdout


def test_a_failed_move_is_a_warning_not_a_failure(monkeypatch, server) -> None:
    server["state"]["delete_response"] = {
        "success": True,
        "working_dir": {"action": "failed", "path": "/ws/javis", "error": "file in use"},
    }
    result = _run(monkeypatch, ["profile", "delete", "javis"])
    assert result.exit_code == 0, result.output
    assert "Warning:" in result.stderr and "file in use" in result.stderr


def test_the_delete_wrapper_sends_the_choice_as_a_query_parameter() -> None:
    from app.cli.client import profiles

    sent: list[tuple[str, Any]] = []

    class _Client:
        async def delete(self, path, body=None, *, params=None):
            sent.append((path, params))
            return {"success": True}

    asyncio.run(profiles.delete_profile(_Client(), "javis"))  # type: ignore[arg-type]
    asyncio.run(profiles.delete_profile(_Client(), "javis", delete_working_dir=True))  # type: ignore[arg-type]
    assert sent == [
        ("/api/profiles/javis", {"working_dir": "keep"}),
        ("/api/profiles/javis", {"working_dir": "delete"}),
    ]


# ── me ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reported", "row"),
    [(True, "yes"), (False, "no"), ("/ws/javis", "/ws/javis")],
)
def test_me_shows_the_working_directory_and_whether_it_is_the_default(monkeypatch, reported, row) -> None:
    import app.cli.client.me as me_client

    async def _get_me(_client):
        return Me.from_dict({
            "profile": "javis", "sub": "javis",
            "user_working_dir": "/ws/javis", "user_working_dir_default": reported,
        })

    monkeypatch.setattr(me_client, "get_me", _get_me)
    result = _run(monkeypatch, ["me"])
    assert result.exit_code == 0, result.output
    rows = {
        key.rstrip(":"): value.strip()
        for key, value in ((line.split(None, 1) + [""])[:2] for line in result.stdout.splitlines() if line.strip())
    }
    assert rows["user_working_dir"] == "/ws/javis"
    assert rows["user_working_dir_default"] == row


def test_me_from_an_older_server_omits_the_default_row(monkeypatch) -> None:
    import app.cli.client.me as me_client

    async def _get_me(_client):
        return Me.from_dict({"profile": "javis", "sub": "javis", "user_working_dir": "/ws/javis"})

    monkeypatch.setattr(me_client, "get_me", _get_me)
    result = _run(monkeypatch, ["--json", "me"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["user_working_dir"] == "/ws/javis"
    assert "user_working_dir_default" not in body


# ── setup server-config ──────────────────────────────────────────────────────


def test_setting_the_retired_server_key_prints_the_servers_pointer(monkeypatch) -> None:
    import app.cli.client.setup as setup_client

    async def _update(_client, _values):
        raise APIError(
            400,
            body=(
                "'user_working_dir' is no longer a server setting: Each profile has its own "
                "working directory now. The admin sets one with PUT "
                "/api/profiles/{name}/working-dir (CLI: cremind profile working-dir NAME PATH)."
            ),
        )

    monkeypatch.setattr(setup_client, "update_server_config", _update)
    result = _run(monkeypatch, ["setup", "server-config", "set", "user_working_dir=/srv/x"])
    assert result.exit_code == 1
    assert "no longer a server setting" in result.stderr
    assert "cremind profile working-dir" in result.stderr
    assert "Traceback" not in result.output


# ── reserved names in the profile wizard ─────────────────────────────────────


def test_the_wizard_mirrors_the_servers_reserved_names() -> None:
    """Spelled out in the CLI (no server imports); pinned equal here."""
    from app.cli import wizard_draft
    from app.cremind_documents.paths import RESERVED_PROFILE_NAMES

    assert wizard_draft._RESERVED_PROFILE_NAMES == RESERVED_PROFILE_NAMES


def test_the_wizard_refuses_a_reserved_name_for_a_new_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(tmp_path))
    result = _run(monkeypatch, ["profile", "wizard", "start", "workspaces"])
    assert result.exit_code == 1
    assert "reserved" in result.output and "working directory" in result.output


def test_a_reserved_name_is_not_refused_locally_when_adopting() -> None:
    from app.cli import wizard_draft

    assert wizard_draft.reserved_profile_name_error("workspaces")
    assert wizard_draft.reserved_profile_name_error("javis") is None
