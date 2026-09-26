"""`cremind docs status` and the documents folder that dies with its container.

An install whose compose file predates the documents bind mount keeps the
folder in the container's own layer: syncing works, but the folder and what
the agent saved in it vanish when the container is recreated. The status
command says so in one line on stderr — the same warning the web UI shows
under its state banner — and keeps stdout (and ``--json``) as they were.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

LEGACY = {
    "in_container": True, "kubernetes": False, "root_mounted": False, "persistent": False,
    "fstype": "overlay", "bind_expected": False, "snippet": '- "~/Documents:/root/Documents"',
}


def _snap(docker=None, **over):
    return {"v": 1, "state": "idle", "reason": None, "tool_mode": "normal", "docker": docker, **over}


@pytest.fixture
def status(monkeypatch):
    import app.cli.client.docs as client

    seen = {"snap": _snap(LEGACY)}

    async def get_status(c):
        return seen["snap"]

    monkeypatch.setattr(client, "get_status", get_status)
    return seen


def _run(*args):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args])


def test_status_warns_on_stderr_about_a_folder_in_the_container_layer(status):
    result = _run("docs", "status")
    assert result.exit_code == 0, result.output
    assert result.stdout == "idle\n"  # the status line itself is unchanged
    warning = result.stderr.strip()
    assert "\n" not in warning
    assert warning.startswith("warning: your documents folder is inside the container "
                              "and is lost when it is recreated")
    assert "re-run the installer (your data is kept)" in warning
    assert warning.endswith('docker-compose.yml: - "~/Documents:/root/Documents"')


def test_status_json_prints_the_snapshot_and_no_warning(status):
    result = _run("--json", "docs", "status")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["docker"] == LEGACY
    assert "warning" not in result.stderr


def test_status_in_a_pod_points_at_the_work_volume(status):
    status["snap"] = _snap({**LEGACY, "kubernetes": True})
    warning = _run("docs", "status").stderr
    assert "inside the pod and is lost when the pod restarts" in warning
    assert "--set persistence.work.enabled=true" in warning
    assert "docker-compose" not in warning and "installer" not in warning


@pytest.mark.parametrize("docker", [
    None,                                   # an older server, or the folder is off
    {**LEGACY, "in_container": False},      # a native install
    {**LEGACY, "root_mounted": True},       # mounted from the host
    {**LEGACY, "root_mounted": None},       # unknown: no mountinfo
    {**LEGACY, "bind_expected": True},      # the bind_missing hold covers it instead
])
def test_status_is_quiet_otherwise(status, docker):
    status["snap"] = _snap(docker)
    result = _run("docs", "status")
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_the_warning_without_a_snippet_still_names_the_installer():
    from app.cli.commands.docs import docker_warning

    line = docker_warning(_snap({**LEGACY, "snippet": None}))
    assert line.endswith("re-run the installer (your data is kept)")
    assert docker_warning({}) is None
    assert docker_warning(_snap("not a dict")) is None


def test_follow_warns_once(monkeypatch):
    from app.cli.client._base import Client

    frames = [_snap(LEGACY, seq=1), _snap(LEGACY, state="scanning", seq=2), _snap(LEGACY, seq=3)]

    async def fake_stream(self, path):
        for frame in frames:
            yield SimpleNamespace(event="documentation_search", data=frame)

    monkeypatch.setattr(Client, "stream", fake_stream)
    result = _run("docs", "status", "--follow")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["idle", "scanning", "idle"]
    assert result.stderr.count("warning: your documents folder") == 1
