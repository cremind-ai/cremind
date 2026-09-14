"""`cremind config export` — the configuration file from the terminal.

The bundled documentation used to say, in as many words, that no CLI command
produced this file. That made "I lost my configuration file" unanswerable from a
terminal and unanswerable from chat, where the agent has a shell and no browser.

Two details carry more weight than the rest. The default output path is the
current directory, because that is the agent's working directory and therefore
somewhere the ``system_file`` tool may read it back from to produce a download.
And the file is written ``0600``, because it embeds a live JWT — the same
treatment the token file itself gets.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.cli.client.config import ConfigExport


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stub the client on its source module — the command imports it in-body."""
    import app.cli.client._base as base
    import app.cli.client.config as config_client

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    calls: list[dict] = []
    state = {"scope": "profile"}

    async def _export_config(_client, fmt="md", *, agent_url=None, pending_https=False):
        calls.append({"format": fmt, "agent_url": agent_url, "pending_https": pending_https})
        return ConfigExport(
            filename=f"cremind-li-config.{fmt}",
            content=b"# Cremind Configuration\ntoken\n",
            content_type="text/markdown",
            scope=state["scope"],
        )

    monkeypatch.setattr(base, "Client", lambda _cfg, **_kw: _FakeClient())
    monkeypatch.setattr(config_client, "export_config", _export_config)
    monkeypatch.setenv("CREMIND_TOKEN", "t")
    monkeypatch.setenv("CREMIND_SERVER", "http://localhost:1112")
    return {"calls": calls, "state": state}


def _run(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, args: list[str], cwd: Path | None = None):
    from app.cli.main import app

    monkeypatch.setattr(sys, "argv", ["cremind", *args])
    if cwd is not None:
        monkeypatch.chdir(cwd)
    return runner.invoke(app, args)


def test_the_file_lands_in_the_working_directory_under_the_servers_name(
    runner, monkeypatch, server, tmp_path,
) -> None:
    """The agent's shell starts in the user working directory, which is inside
    the file API's allowlist — so the path this prints is one it can hand over
    as a download."""
    result = _run(runner, monkeypatch, ["config", "export"], cwd=tmp_path)
    assert result.exit_code == 0, result.output
    written = tmp_path / "cremind-li-config.md"
    assert written.exists()
    assert str(written.resolve()).replace(os.sep, "/") in result.stdout


def test_out_may_name_a_file_or_a_directory(runner, monkeypatch, server, tmp_path) -> None:
    target = tmp_path / "somewhere" / "mine.md"
    target.parent.mkdir()
    assert _run(runner, monkeypatch, ["config", "export", "--out", str(target)]).exit_code == 0
    assert target.exists()

    into = tmp_path / "dir"
    into.mkdir()
    assert _run(runner, monkeypatch, ["config", "export", "--out", str(into)]).exit_code == 0
    assert (into / "cremind-li-config.md").exists()


def test_out_dash_streams_the_bytes_to_stdout(runner, monkeypatch, server, tmp_path) -> None:
    result = _run(runner, monkeypatch, ["config", "export", "--out", "-"], cwd=tmp_path)
    assert result.exit_code == 0, result.output
    assert "# Cremind Configuration" in result.stdout
    assert not list(tmp_path.iterdir()), "nothing should be written to disk"


def test_the_format_reaches_the_server(runner, monkeypatch, server, tmp_path) -> None:
    _run(runner, monkeypatch, ["config", "export", "--format", "env"], cwd=tmp_path)
    assert server["calls"][-1]["format"] == "env"


def test_an_unknown_format_is_refused_before_any_request(runner, monkeypatch, server, tmp_path) -> None:
    result = _run(runner, monkeypatch, ["config", "export", "--format", "pdf"], cwd=tmp_path)
    assert result.exit_code == 2
    assert "use md, json or env" in result.stderr
    assert server["calls"] == []


def test_the_agent_url_override_is_forwarded(runner, monkeypatch, server, tmp_path) -> None:
    _run(runner, monkeypatch, [
        "config", "export", "--agent-url", "http://localhost:1515", "--pending-https",
    ], cwd=tmp_path)
    assert server["calls"][-1]["agent_url"] == "http://localhost:1515"
    assert server["calls"][-1]["pending_https"] is True


def test_json_mode_reports_the_path_and_the_scope(runner, monkeypatch, server, tmp_path) -> None:
    result = _run(runner, monkeypatch, ["--json", "config", "export"], cwd=tmp_path)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["filename"] == "cremind-li-config.md"
    assert payload["format"] == "md"
    assert payload["bytes"] > 0
    assert payload["scope"] == "profile"


def test_a_reduced_file_says_what_is_missing_from_it(runner, monkeypatch, server, tmp_path) -> None:
    """Otherwise it reads as a broken export rather than a scoped one."""
    result = _run(runner, monkeypatch, ["config", "export"], cwd=tmp_path)
    assert "Reduced per-profile file" in result.stderr
    assert "admin profile's export" in result.stderr


def test_the_admin_file_gets_no_reduced_notice(runner, monkeypatch, server, tmp_path) -> None:
    server["state"]["scope"] = "full"
    result = _run(runner, monkeypatch, ["config", "export"], cwd=tmp_path)
    assert "Reduced per-profile file" not in result.stderr
    assert "keep it private" in result.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_file_is_written_private(runner, monkeypatch, server, tmp_path) -> None:
    _run(runner, monkeypatch, ["config", "export"], cwd=tmp_path)
    written = tmp_path / "cremind-li-config.md"
    assert oct(written.stat().st_mode & 0o777) == "0o600"
