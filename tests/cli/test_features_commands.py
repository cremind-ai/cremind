"""CLI: `cremind features list` / `install` — the update-required state.

``list`` does a function-body import of ``get_features`` from
``app.cli.client.features``, so it is patched there; ``install`` streams through
``Client.stream_post``, which is replaced on the class so nothing reaches the
network.

What these pin is the one thing "installed" never said: a feature can import
and still be too old (a runtime venv is never resynced on upgrade, and an old
Codex SDK silently offers built-in models instead of the account's). The CLI
cannot work that out itself — the venv that matters is the server's, which may
be on another machine — so it renders what the server reports, and a server
that predates version checks (no ``outdated`` / ``restart_pending`` keys at
all) has to read as "nothing known to be wrong".
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner


# ── fakes ──────────────────────────────────────────────────────────────────

_FEATURES = {
    "claude_code": {
        "installed": False,
        "requires_restart_after_install": False,
        "extras": ["claude-code"],
        "outdated": False,
        "required": [],
        "installed_versions": {},
        "restart_pending": False,
    },
    "codex": {
        "installed": True,
        "requires_restart_after_install": False,
        "extras": ["codex"],
        "outdated": True,
        "required": ["openai-codex>=0.154.0,<0.155"],
        "installed_versions": {"openai-codex": "0.1.0b3"},
        "restart_pending": False,
    },
    "embedding.me5": {
        "installed": True,
        "requires_restart_after_install": True,
        "extras": ["embeddings-me5"],
        "outdated": False,
        "required": [],
        "installed_versions": {},
        "restart_pending": False,
    },
}

_CODEX_NOTE = (
    "(codex: openai-codex 0.1.0b3 installed, needs openai-codex>=0.154.0,<0.155 "
    "- run `cremind features install codex`, then `cremind server restart`)"
)


def _patch_features(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    async def fake_get(client):
        return json.loads(json.dumps(payload))

    import app.cli.client.features as features_client

    monkeypatch.setattr(features_client, "get_features", fake_get)


def _patch_stream(monkeypatch: pytest.MonkeyPatch, frames: list[tuple[str, dict]]) -> dict:
    """Replace the install SSE stream with ``frames``; return what was posted."""
    captured: dict = {}

    def fake_stream_post(self, path, body):
        captured["path"] = path
        captured["body"] = body

        async def _gen():
            for name, data in frames:
                yield SimpleNamespace(event=name, data=data)

        return _gen()

    from app.cli.client._base import Client

    monkeypatch.setattr(Client, "stream_post", fake_stream_post)
    return captured


def _row(output: str, feature: str) -> list[str]:
    """The whitespace-split table row whose first cell is ``feature``."""
    for line in output.splitlines():
        cells = line.split()
        if cells and cells[0] == feature:
            return cells
    raise AssertionError(f"no row for {feature!r} in:\n{output}")


# ── features list ──────────────────────────────────────────────────────────


def test_list_has_an_update_column_after_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    _patch_features(monkeypatch, _FEATURES)
    result = CliRunner().invoke(app, ["--token", "t", "features", "list"])
    assert result.exit_code == 0, result.output

    header = result.stdout.splitlines()[0].split()
    assert header[:3] == ["FEATURE", "INSTALLED", "UPDATE"]
    assert _row(result.stdout, "codex")[:3] == ["codex", "true", "required"]
    assert _row(result.stdout, "claude_code")[:3] == ["claude_code", "false", "-"]
    assert _row(result.stdout, "embedding.me5")[:3] == ["embedding.me5", "true", "-"]


def test_list_writes_one_stderr_note_per_outdated_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The note names both versions and the two commands, and goes to stderr so
    # a piped table stays a table.
    from app.cli.main import app

    _patch_features(monkeypatch, _FEATURES)
    result = CliRunner().invoke(app, ["--token", "t", "features", "list"])
    assert result.exit_code == 0, result.output

    assert result.stderr.strip().splitlines() == [_CODEX_NOTE]
    assert "cremind features install" not in result.stdout


def test_restart_wins_and_drops_the_install_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the update has run, the server still has the old package loaded,
    so the feature is outdated AND restart-pending. The cell must say the step
    that is actually left, and the note must not send the user back to
    `install`, which would change nothing."""
    from app.cli.main import app

    payload = json.loads(json.dumps(_FEATURES))
    payload["codex"]["restart_pending"] = True
    _patch_features(monkeypatch, payload)
    result = CliRunner().invoke(app, ["--token", "t", "features", "list"])
    assert result.exit_code == 0, result.output

    assert _row(result.stdout, "codex")[2] == "restart"
    assert "features install codex" not in result.output


def test_list_from_an_older_server_reads_as_up_to_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli.main import app

    old = {
        fid: {
            key: info[key]
            for key in ("installed", "requires_restart_after_install", "extras")
        }
        for fid, info in _FEATURES.items()
    }
    _patch_features(monkeypatch, old)
    result = CliRunner().invoke(app, ["--token", "t", "features", "list"])
    assert result.exit_code == 0, result.output

    assert _row(result.stdout, "codex")[2] == "-"
    assert result.stderr.strip() == ""


def test_list_json_passes_the_new_fields_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    _patch_features(monkeypatch, _FEATURES)
    result = CliRunner().invoke(app, ["--json", "--token", "t", "features", "list"])
    assert result.exit_code == 0, result.output

    parsed = json.loads(result.stdout)
    assert parsed["codex"]["outdated"] is True
    assert parsed["codex"]["installed_versions"] == {"openai-codex": "0.1.0b3"}
    # JSON callers read the fields; the human note is not mixed in.
    assert result.stderr.strip() == ""


# ── features install ───────────────────────────────────────────────────────


def test_install_reports_an_update_under_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    captured = _patch_stream(monkeypatch, [
        ("log", {"message": "Collecting openai-codex>=0.154.0,<0.155", "ok": True}),
        ("done", {
            "ok": True,
            "restart_required": True,
            "installed": [],
            "upgraded": ["codex"],
            "failed": [],
            "already_present": [],
            "error": None,
        }),
    ])
    result = CliRunner().invoke(app, ["--token", "t", "features", "install", "codex"])
    assert result.exit_code == 0, result.output

    assert captured["body"] == {"features": ["codex"]}
    assert "updated: codex" in result.stdout
    assert "installed:" not in result.stdout
    assert "cremind server restart" in result.stdout


def test_install_reports_a_fresh_install_and_an_update_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli.main import app

    _patch_stream(monkeypatch, [
        ("done", {
            "ok": True,
            "restart_required": True,
            "installed": ["browser"],
            "upgraded": ["codex"],
            "failed": [],
            "already_present": [],
            "error": None,
        }),
    ])
    result = CliRunner().invoke(
        app, ["--token", "t", "features", "install", "codex", "browser"],
    )
    assert result.exit_code == 0, result.output

    assert "installed: browser\n" in result.stdout
    assert "updated: codex\n" in result.stdout


def test_install_done_frame_from_an_older_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    _patch_stream(monkeypatch, [
        ("done", {
            "ok": True,
            "restart_required": False,
            "installed": ["codex"],
            "failed": [],
            "already_present": [],
            "error": None,
        }),
    ])
    result = CliRunner().invoke(app, ["--token", "t", "features", "install", "codex"])
    assert result.exit_code == 0, result.output

    assert "installed: codex" in result.stdout
    assert "updated:" not in result.stdout
