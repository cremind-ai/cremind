"""`cremind docs` and the folder it indexes: always the profile's working directory.

There is no folder to choose any more (`set-root` and `enable --root` are
gone); the admin changes a profile's working directory, and the profile
accepts a move with `confirm-root-change`, which says what would leave the
index and applies only with `--yes`. `status` and `settings` name the folder
as "your working directory". The client functions are patched in
``app.cli.client.docs``, so nothing reaches the network.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

HELD = {
    "v": 1, "state": "hold", "reason": "pending_root_change", "tool_mode": "normal",
    "detail": {"from": "/home/ann/Documents", "to": "/srv/ws/ann"},
    "confirmation": {"kind": "root_change", "from": "/home/ann/Documents", "to": "/srv/ws/ann",
                     "files": 12, "leaving": 5},
    "sources": {"local": {"enabled": True, "root": "/home/ann/Documents", "first_sync_confirmed": True}},
}
IDLE = {
    "v": 1, "state": "idle", "reason": None, "tool_mode": "normal",
    "sources": {"local": {"enabled": True, "root": "/srv/ws/ann", "first_sync_confirmed": True}},
}


@pytest.fixture
def api(monkeypatch):
    import app.cli.client.docs as client

    seen: dict = {"snap": HELD, "put": [], "control": [], "settings": {}}

    async def get_status(c):
        return seen["snap"]

    async def get_settings(c):
        return seen["settings"]

    async def put_settings(c, body):
        seen["put"].append(body)
        return {"settings": {"local": {"enabled": True}}}

    async def control(c, action, **params):
        seen["control"].append((action, params))
        return {"accepted": True, "snapshot": IDLE}

    monkeypatch.setattr(client, "get_status", get_status)
    monkeypatch.setattr(client, "get_settings", get_settings)
    monkeypatch.setattr(client, "put_settings", put_settings)
    monkeypatch.setattr(client, "control", control)
    return seen


def _run(*args):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args])


def test_confirm_root_change_says_what_leaves_and_needs_yes(api):
    result = _run("docs", "confirm-root-change")
    assert result.exit_code == 2, result.output
    assert "changed from /home/ann/Documents to /srv/ws/ann" in result.stderr
    assert "5 of the 12 indexed files" in result.stderr
    assert "Re-run with --yes" in result.stderr
    assert api["control"] == []


def test_confirm_root_change_with_yes_runs_the_control_action(api):
    result = _run("docs", "confirm-root-change", "--yes")
    assert result.exit_code == 0, result.output
    assert api["control"] == [("confirm_root_change", {})]
    assert result.stdout.strip() == "idle"


def test_confirm_root_change_with_nothing_waiting_fails(api):
    api["snap"] = IDLE
    result = _run("docs", "confirm-root-change", "--yes")
    assert result.exit_code == 1
    assert "no working directory change waiting" in result.stderr
    assert api["control"] == []


def test_there_is_no_folder_to_choose(api):
    assert _run("docs", "set-root", "/x").exit_code != 0
    assert _run("docs", "enable", "--root", "/x").exit_code != 0
    assert api["put"] == []
    result = _run("docs", "enable")
    assert result.exit_code == 0, result.output
    assert api["put"] == [{"kind": "local", "enabled": True}]


def test_status_names_the_folder_as_your_working_directory(api):
    api["snap"] = IDLE
    result = _run("docs", "status")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["idle", "folder: /srv/ws/ann (your working directory)"]


def test_status_on_hold_says_what_to_run(api):
    result = _run("docs", "status")
    lines = result.stdout.splitlines()
    assert lines[0] == "hold(pending_root_change)"
    assert "changed from /home/ann/Documents to /srv/ws/ann" in lines[1]
    assert "cremind docs confirm-root-change" in lines[1]


def test_settings_prints_the_folder_on_stderr_and_json_on_stdout(api):
    api["settings"] = {"policy_view": {"working_dir": "/srv/ws/ann", "working_dir_editable": False}}
    result = _run("docs", "settings")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["policy_view"]["working_dir"] == "/srv/ws/ann"
    assert "folder: /srv/ws/ann (your working directory — the admin changes it)" in result.stderr
    admin = _run("--json", "docs", "settings")
    assert admin.stderr == ""
