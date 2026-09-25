"""`cremind userdocs drive …` and the `--source` flags — Drive from the terminal.

The client functions are patched in ``app.cli.client.userdocs``, so nothing
reaches the network. What is pinned:

- **Turning Drive off is never silent.** `drive disable` deletes the Drive
  index, so without `--yes` it sends nothing and exits 2 — even when the
  server would not have asked.
- **The folder list is what the user typed.** Ids and pasted folder links
  both land as bare ids in ``include_folders``; `--clear` is the only way to
  send an empty list.
- **Drive actions are aimed at Drive.** `drive sync`, `deletions … --source
  drive` and `retry --source drive` carry ``source="drive"``; the default
  (no flag) still means the local folder, so older habits are unchanged.
- **`--source` filters** reach the file list and the query filters, and a
  typo fails before any request.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

LINK = {"linked": True, "email": "u@example.com", "whole_drive": True, "scopes_stale": False,
        "identity_email": "u@example.com",
        "index": {"enabled": True, "state": "live", "reason": None,
                  "counts": {"indexed": 40, "pending": 2, "error": 1, "metadata_only": 2},
                  "last_sync_at": 1790000000}}


def _settings(enabled=True, folders=("F1",), link=None):
    return {
        "local": {"enabled": False, "options": {}},
        "drive": {"kind": "drive", "enabled": enabled, "options": {"include_folders": list(folders)}},
        "drive_link": LINK if link is None else link,
    }


@pytest.fixture
def calls(monkeypatch):
    import app.cli.client.userdocs as client

    seen: dict = {"put": [], "control": [], "files": [], "query": [], "folders": [],
                  "settings": _settings(), "put_errors": [], "folder_error": None,
                  "listing": {"folders": [{"id": "F1", "name": "Reports"}, {"id": "F2", "name": "Taxes"}],
                              "parent": None, "whole_drive": True}}

    async def get_settings(c):
        return seen["settings"]

    async def put_settings(c, body):
        seen["put"].append(body)
        if seen["put_errors"]:
            raise seen["put_errors"].pop(0)
        return {"settings": {"drive": {"enabled": body.get("enabled", True),
                                       "options": body.get("options") or {}}}}

    async def control(c, action, **params):
        seen["control"].append((action, params))
        return {"accepted": True, "snapshot": {"state": "idle", "drive": {"enabled": True, "state": "live"}}}

    async def list_files(c, **params):
        seen["files"].append(params)
        return {"files": [], "next": None}

    async def query(c, leaf, body):
        seen["query"].append((leaf, body))
        return {"text": "ok", "items": []}

    async def drive_folders(c, *, parent=None):
        seen["folders"].append(parent)
        if seen["folder_error"] is not None:
            raise seen["folder_error"]
        return seen["listing"]

    for name, fn in (("get_settings", get_settings), ("put_settings", put_settings), ("control", control),
                     ("list_files", list_files), ("query", query), ("drive_folders", drive_folders)):
        monkeypatch.setattr(client, name, fn)
    return seen


def _api_error(status: int, payload: dict):
    from app.cli.client._base import APIError

    raw = json.dumps(payload).encode()
    return APIError(status, raw.decode(), raw)


def _run(*args):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args])


# ── status ─────────────────────────────────────────────────────────────────


def test_status_is_one_human_line(calls):
    result = _run("userdocs", "drive", "status")
    assert result.exit_code == 0, result.output
    line = result.stdout.strip()
    assert "\n" not in line
    assert line.startswith("on · live")
    assert "linked as u@example.com (whole-Drive)" in line
    assert "folders: F1" in line
    assert "42 files indexed" in line
    assert "2 pending" in line and "1 failed" in line
    assert "last sync " in line


def test_status_json_is_the_source_and_the_link(calls):
    result = _run("--json", "userdocs", "drive", "status")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["source"]["enabled"] is True
    assert out["link"]["index"]["state"] == "live"


def test_status_of_a_held_drive_says_when_the_index_goes(calls):
    held = {**LINK, "index": {"enabled": True, "state": "hold", "reason": "auth_revoked",
                              "detail": {"purge_at": 1790604800000}, "counts": {}}}
    calls["settings"] = _settings(link=held)
    line = _run("userdocs", "drive", "status").stdout
    assert "hold(auth_revoked), index removed on " in line


def test_status_when_not_linked(calls):
    calls["settings"] = _settings(enabled=False, folders=(), link={"linked": False})
    line = _run("userdocs", "drive", "status").stdout
    assert line.startswith("off · Google Drive is not linked")


# ── enable / disable ───────────────────────────────────────────────────────


def test_enable_sends_ids_and_pasted_links_as_ids(calls):
    result = _run("userdocs", "drive", "enable",
                  "--folder", "https://drive.google.com/drive/folders/1AbC-_x9?usp=sharing",
                  "--folder", "F2, https://drive.google.com/open?id=F3", "--folder", "F2")
    assert result.exit_code == 0, result.output
    assert calls["put"] == [{"kind": "drive", "enabled": True,
                             "options": {"include_folders": ["1AbC-_x9", "F2", "F3"]}}]


def test_enable_without_folders_leaves_them_alone(calls):
    assert _run("userdocs", "drive", "enable").exit_code == 0
    assert calls["put"] == [{"kind": "drive", "enabled": True}]


def test_enable_explains_a_whole_drive_account(calls):
    calls["put_errors"].append(_api_error(409, {
        "error": "DriveFoldersRequired", "message": "This Google account gives Cremind access to your whole Drive…",
        "whole_drive": True,
    }))
    result = _run("userdocs", "drive", "enable")
    assert result.exit_code == 1
    assert "whole Drive" in result.stderr
    assert "cremind userdocs drive folders list" in result.stderr


def test_enable_when_not_linked(calls):
    calls["put_errors"].append(_api_error(409, {"error": "DriveNotLinked", "message": "Link Google Drive first."}))
    result = _run("userdocs", "drive", "enable")
    assert result.exit_code == 1
    assert "Link Google Drive first." in result.stderr


def test_disable_without_yes_sends_nothing_and_exits_2(calls):
    result = _run("userdocs", "drive", "disable")
    assert result.exit_code == 2
    assert calls["put"] == []
    assert "delete every indexed Google Drive file (45 files)" in result.stderr
    assert "--yes" in result.stderr


def test_disable_with_yes_answers_the_servers_confirmation(calls):
    calls["put_errors"].append(_api_error(409, {
        "error": "ConfirmationRequired", "confirm": "tok123",
        "plan": {"effects": [{"kind": "purge_drive", "files": 45}], "destructive": True},
    }))
    result = _run("userdocs", "drive", "disable", "--yes")
    assert result.exit_code == 0, result.output
    assert calls["put"] == [
        {"kind": "drive", "enabled": False},
        {"kind": "drive", "enabled": False, "confirm": "tok123"},
    ]


def test_disable_when_already_off_is_a_no_op(calls):
    calls["settings"] = _settings(enabled=False)
    result = _run("userdocs", "drive", "disable")
    assert result.exit_code == 0
    assert calls["put"] == []
    assert "already off" in result.stderr


# ── sync ───────────────────────────────────────────────────────────────────


def test_sync_reads_changes_and_full_relists(calls):
    assert _run("userdocs", "drive", "sync").exit_code == 0
    assert _run("userdocs", "drive", "sync", "--full").exit_code == 0
    assert calls["control"] == [("sync_now", {"source": "drive"}), ("rescan", {"source": "drive"})]


# ── folders ────────────────────────────────────────────────────────────────


def test_folders_list_marks_the_indexed_ones(calls):
    result = _run("userdocs", "drive", "folders", "list")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "Top level (My Drive):"
    assert lines[1].startswith("* F1") and lines[1].endswith("Reports")
    assert lines[2].startswith("  F2") and lines[2].endswith("Taxes")
    assert calls["folders"] == [None]


def test_folders_list_takes_a_parent_link(calls):
    calls["listing"] = {"folders": [], "parent": {"id": "P9", "name": "Work"}, "whole_drive": False}
    result = _run("userdocs", "drive", "folders", "list", "--parent",
                  "https://drive.google.com/drive/u/0/folders/P9")
    assert result.exit_code == 0, result.output
    assert calls["folders"] == ["P9"]
    assert result.stdout.splitlines() == ["In Work (P9):", "  (no folders)"]


def test_folders_list_json_is_the_listing(calls):
    result = _run("--json", "userdocs", "drive", "folders", "list")
    assert json.loads(result.stdout) == calls["listing"]


def test_folders_list_prints_the_servers_reason(calls):
    calls["folder_error"] = _api_error(503, {"error": "DriveUnreachable", "message": "Google Drive could not be reached."})
    result = _run("userdocs", "drive", "folders", "list")
    assert result.exit_code == 1
    assert "could not be reached" in result.stderr


def test_folders_set_replaces_the_list(calls):
    result = _run("userdocs", "drive", "folders", "set", "F1", "https://drive.google.com/drive/folders/F7")
    assert result.exit_code == 0, result.output
    assert calls["put"] == [{"kind": "drive", "options": {"include_folders": ["F1", "F7"]}}]


def test_folders_set_clear_is_the_only_empty_list(calls):
    assert _run("userdocs", "drive", "folders", "set", "--clear").exit_code == 0
    assert calls["put"] == [{"kind": "drive", "options": {"include_folders": []}}]
    assert _run("userdocs", "drive", "folders", "set").exit_code == 1
    assert _run("userdocs", "drive", "folders", "set", "F1", "--clear").exit_code == 1
    assert len(calls["put"]) == 1


def test_narrowing_the_folders_asks_first(calls):
    calls["put_errors"].append(_api_error(409, {
        "error": "ConfirmationRequired", "confirm": "tok",
        "plan": {"effects": [{"kind": "purge_out_of_scope", "files": 12}], "destructive": True},
    }))
    result = _run("userdocs", "drive", "folders", "set", "F1")
    assert result.exit_code == 2
    assert "(12 files)" in result.stderr
    assert len(calls["put"]) == 1  # not re-sent with the token


# ── --source elsewhere ─────────────────────────────────────────────────────


@pytest.mark.parametrize("flag,expected", [(["--source", "drive"], "drive"), (["--source", "ALL"], None), ([], None)])
def test_files_source(calls, flag, expected):
    assert _run("userdocs", "files", *flag).exit_code == 0
    assert calls["files"][0]["source"] == expected


def test_a_bad_source_fails_before_any_request(calls):
    result = _run("userdocs", "files", "--source", "dropbox")
    assert result.exit_code == 1
    assert calls["files"] == []
    assert _run("userdocs", "search", "x", "--source", "dropbox").exit_code == 1
    assert calls["query"] == []


def test_search_and_find_filter_by_source(calls):
    assert _run("userdocs", "search", "budget", "--source", "drive").exit_code == 0
    assert _run("userdocs", "find", "--source", "local", "--type", "pdf").exit_code == 0
    assert _run("userdocs", "search", "budget", "--source", "all").exit_code == 0
    (leaf1, b1), (leaf2, b2), (_, b3) = calls["query"]
    assert (leaf1, b1["filters"]) == ("search", {"source": "drive"})
    assert (leaf2, b2["filters"]) == ("find", {"source": "local", "types": ["pdf"]})
    assert b3["filters"] is None  # "all" is the default: no filter sent


def test_deletions_and_retry_can_aim_at_drive(calls):
    assert _run("userdocs", "deletions", "confirm", "--source", "drive").exit_code == 0
    assert _run("userdocs", "deletions", "reject").exit_code == 0
    assert _run("userdocs", "retry", "--source", "drive", "--file-id", "k7m2xq9a").exit_code == 0
    assert calls["control"] == [
        ("confirm_deletions", {"source": "drive"}),
        ("reject_deletions", {}),
        ("retry_failed", {"source": "drive", "targets": ["k7m2xq9a"]}),
    ]
    assert _run("userdocs", "deletions", "confirm", "--source", "all").exit_code == 1
    assert len(calls["control"]) == 3


# ── the status line ────────────────────────────────────────────────────────


def test_summarize_snapshot_shows_drive_only_when_on():
    from app.cli.commands.userdocs import summarize_snapshot

    base = {"state": "idle", "tool_mode": "normal"}
    assert summarize_snapshot({**base, "drive": {"enabled": False, "state": "disabled"}}) == "idle"
    line = summarize_snapshot({**base, "drive": {
        "enabled": True, "state": "hold", "reason": "drive_unreachable",
        "confirmation": {"kind": "mass_delete"}, "counts": {"pending": 5},
    }})
    assert line == "idle · drive: hold(drive_unreachable), waiting for you: mass_delete, 5 pending"
