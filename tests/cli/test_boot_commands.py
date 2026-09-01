"""`cremind boot` — register, remove, and report the OS boot service.

The load-bearing properties:

* it runs entirely locally and never resolves a profile or token — it exists to
  arrange for a server that may not be running, or even installed, yet;
* what ``--print-only`` shows is what the command would run, so the manual
  fallback can never drift from the automated path;
* it launches the installer's shim, never the venv binary — only the shim
  loads ``~/.cremind/.env``, so a service that skipped it would come up with
  default ports and no ``CREMIND_SSL``;
* ``disable`` believes the OS, not the exit codes, because every teardown
  command is best-effort by design.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.system import boot_service


def _invoke(runner: CliRunner, monkeypatch, args: list[str]):
    """Invoke the CLI, mirroring ``args`` into ``sys.argv``.

    ``_should_resolve_profile`` reads ``sys.argv`` (Click doesn't expose the
    deep subcommand at root-callback stage) and ``CliRunner`` leaves it alone.
    """
    from app.cli.main import app

    monkeypatch.setattr(sys, "argv", ["cremind", *args])
    return runner.invoke(app, args)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sysdir(tmp_path, monkeypatch):
    """A system dir with a shim, which ``enable_plan`` accepts as the default.

    ``expanduser`` is redirected as well as ``CREMIND_SYSTEM_DIR`` because the
    side-by-side guard compares the two — satisfying it here rather than
    bypassing it keeps the guard itself under test elsewhere.
    """
    home = tmp_path / "home"
    d = home / ".cremind"
    (d / "bin").mkdir(parents=True)
    shim = d / "bin" / ("cremind.cmd" if sys.platform == "win32" else "cremind")
    shim.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(d))
    monkeypatch.delenv("CREMIND_TOKEN", raising=False)
    monkeypatch.delenv("CREMIND_PROFILE", raising=False)
    monkeypatch.delenv("INSTALL_MODE", raising=False)
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    monkeypatch.setattr(boot_service.os.path, "expanduser", lambda _p: str(home))
    return d


@pytest.fixture
def never_runs(monkeypatch):
    """Fail loudly if anything reaches the executor."""

    def _boom(_plan):  # pragma: no cover - only runs on regression
        raise AssertionError("run_boot_plan must not be reached here")

    monkeypatch.setattr(boot_service, "run_boot_plan", _boom)


# ── gating ───────────────────────────────────────────────────────────────


def test_never_resolves_a_profile_or_token(runner, monkeypatch, sysdir):
    """`boot` is token-free: the picker must not run, even with no token file."""
    import app.cli.tui.profile_picker as picker

    def _boom(*_a, **_kw):  # pragma: no cover - only runs on regression
        raise AssertionError("the profile picker must never run for `cremind boot`")

    monkeypatch.setattr(picker, "pick_profile", _boom, raising=False)

    result = _invoke(runner, monkeypatch, ["boot", "status"])

    assert result.exit_code == 0


# ── enable ───────────────────────────────────────────────────────────────


def test_print_only_prints_what_it_would_run(runner, monkeypatch, sysdir, never_runs):
    result = _invoke(runner, monkeypatch, ["boot", "enable", "--print-only"])

    assert result.exit_code == 0
    plan = boot_service.enable_plan(
        str(sysdir / "bin" / ("cremind.cmd" if sys.platform == "win32" else "cremind")),
        system_dir=sysdir,
        user_id="ignored" if sys.platform == "win32" else None,
    )
    for argv in plan.commands:
        # The tool and its distinctive argument must both be on screen; the
        # printed line is quoted, so compare against the rendered form.
        assert boot_service.render_command(argv) in result.stdout


def test_print_only_json_carries_the_unit_and_the_commands(
    runner, monkeypatch, sysdir, never_runs
):
    result = _invoke(
        runner, monkeypatch, ["--json", "boot", "enable", "--print-only"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] in ("systemd", "launchd", "schtasks")
    assert payload["artifacts"] and payload["artifacts"][0]["content"]
    assert payload["unit_path"]


def test_it_launches_the_shim_not_the_venv_binary(
    runner, monkeypatch, sysdir, never_runs
):
    """Only the shim loads ~/.cremind/.env — the venv binary sees none of it."""
    result = _invoke(runner, monkeypatch, ["--json", "boot", "enable", "--print-only"])

    payload = json.loads(result.stdout)
    rendered = " ".join(a["content"] for a in payload["artifacts"])
    shim = sysdir / "bin" / ("cremind.cmd" if sys.platform == "win32" else "cremind")
    assert str(shim) in rendered
    # Whatever else the artifacts mention, no venv path may be a launch target.
    for venv_target in ("venv/bin/cremind", "venv\\Scripts\\cremind"):
        assert venv_target not in rendered


def test_a_missing_shim_is_a_clear_error(runner, monkeypatch, sysdir, never_runs):
    for stale in (sysdir / "bin").iterdir():
        stale.unlink()

    result = _invoke(runner, monkeypatch, ["boot", "enable", "--yes"])

    assert result.exit_code == 1
    assert "No Cremind launcher at" in result.stderr
    assert "re-run the installer" in result.stderr


def test_json_without_yes_is_refused(runner, monkeypatch, sysdir, never_runs):
    """Confirmation has no meaning in JSON mode, so demand the explicit --yes."""
    result = _invoke(runner, monkeypatch, ["--json", "boot", "enable"])

    assert result.exit_code == 1
    assert "--json needs --yes" in result.stderr


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"INSTALL_MODE": "docker"}, "container"),
        ({"CREMIND_ELECTRON_PARENT": "1"}, "desktop app"),
    ],
)
def test_refusals_never_reach_the_executor(
    runner, monkeypatch, sysdir, never_runs, env, expected
):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    result = _invoke(runner, monkeypatch, ["boot", "enable", "--yes"])

    assert result.exit_code == 1
    assert expected in result.stderr


def test_enable_passes_the_plan_through_and_reports_success(
    runner, monkeypatch, sysdir
):
    seen: list = []

    def _fake_run(plan):
        seen.append(plan)
        return True, None, []

    monkeypatch.setattr(boot_service, "run_boot_plan", _fake_run)

    result = _invoke(runner, monkeypatch, ["boot", "enable", "--yes"])

    assert result.exit_code == 0
    assert len(seen) == 1 and seen[0].supported
    assert "Boot service registered" in result.stdout
    assert "cremind boot disable" in result.stdout


def test_enable_surfaces_the_failure_detail(runner, monkeypatch, sysdir):
    monkeypatch.setattr(
        boot_service, "run_boot_plan", lambda _p: (False, "Access is denied.", [])
    )

    result = _invoke(runner, monkeypatch, ["boot", "enable", "--yes"])

    assert result.exit_code == 1
    assert "Access is denied." in result.stderr


def test_enable_surfaces_warnings_but_still_succeeds(runner, monkeypatch, sysdir):
    """polkit refusing enable-linger is worth saying, not worth failing over."""
    monkeypatch.setattr(
        boot_service, "run_boot_plan", lambda _p: (True, None, ["linger denied"])
    )

    result = _invoke(runner, monkeypatch, ["boot", "enable", "--yes"])

    assert result.exit_code == 0
    assert "linger denied" in result.stderr


def test_no_start_says_so(runner, monkeypatch, sysdir):
    monkeypatch.setattr(boot_service, "run_boot_plan", lambda _p: (True, None, []))

    result = _invoke(runner, monkeypatch, ["boot", "enable", "--no-start", "--yes"])

    assert result.exit_code == 0
    assert "next login" in result.stdout


# ── disable ──────────────────────────────────────────────────────────────


def test_disable_confirms_against_the_os_not_the_exit_code(
    runner, monkeypatch, sysdir
):
    """Every teardown command is best-effort, so success has to be verified."""
    monkeypatch.setattr(boot_service, "run_boot_plan", lambda _p: (True, None, []))
    _staged_status(
        monkeypatch,
        boot_service.BootStatus(supported=True, registered=True),
        boot_service.BootStatus(supported=True, registered=True),
    )

    result = _invoke(runner, monkeypatch, ["boot", "disable", "--yes"])

    assert result.exit_code == 1
    assert "still registered" in result.stderr


def _staged_status(monkeypatch, *stages):
    """Answer ``boot_status`` differently before and after the teardown."""
    calls = iter(stages)

    monkeypatch.setattr(
        boot_service, "boot_status", lambda **_kw: next(calls, stages[-1])
    )


def test_disable_reports_removal(runner, monkeypatch, sysdir):
    monkeypatch.setattr(boot_service, "run_boot_plan", lambda _p: (True, None, []))
    _staged_status(
        monkeypatch,
        boot_service.BootStatus(supported=True, registered=True),
        boot_service.BootStatus(supported=True, registered=False),
    )

    result = _invoke(runner, monkeypatch, ["boot", "disable", "--yes"])

    assert result.exit_code == 0
    assert "Boot service removed." in result.stdout


def test_disable_is_quiet_when_nothing_was_registered(runner, monkeypatch, sysdir):
    """Every teardown command fails with "not found" here.

    Printing those as warnings makes a clean no-op look like a problem, and
    the uninstallers call this unconditionally.
    """
    monkeypatch.setattr(
        boot_service,
        "run_boot_plan",
        lambda _p: (True, None, ["schtasks exited with 1: cannot find the file"]),
    )
    _staged_status(
        monkeypatch,
        boot_service.BootStatus(supported=True, registered=False, active=False),
    )

    result = _invoke(runner, monkeypatch, ["boot", "disable", "--yes"])

    assert result.exit_code == 0
    assert "No boot service was registered." in result.stdout
    assert "cannot find the file" not in result.stderr


def test_disable_json_without_yes_is_refused(runner, monkeypatch, sysdir, never_runs):
    result = _invoke(runner, monkeypatch, ["--json", "boot", "disable"])

    assert result.exit_code == 1
    assert "--json needs --yes" in result.stderr


# ── status ───────────────────────────────────────────────────────────────


def test_status_json_reports_every_field(runner, monkeypatch, sysdir):
    monkeypatch.setattr(
        boot_service,
        "boot_status",
        lambda **_kw: boot_service.BootStatus(
            supported=True,
            kind="systemd",
            registered=True,
            active=True,
            unit_path="/x/cremind.service",
            linger=False,
            server_pid=4242,
        ),
    )

    result = _invoke(runner, monkeypatch, ["--json", "boot", "status"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["registered"] is True and payload["active"] is True
    assert payload["linger"] is False and payload["server_pid"] == 4242


def test_status_warns_when_the_service_dies_at_logout(runner, monkeypatch, sysdir):
    """Without lingering the server stops at logout, which looks like a bug."""
    monkeypatch.setattr(
        boot_service,
        "boot_status",
        lambda **_kw: boot_service.BootStatus(
            supported=True, kind="systemd", registered=True, active=True, linger=False
        ),
    )

    result = _invoke(runner, monkeypatch, ["boot", "status"])

    assert result.exit_code == 0
    assert "enable-linger" in result.stderr


def test_status_says_unknown_rather_than_no(runner, monkeypatch, sysdir):
    """An unknowable state is not a negative one."""
    monkeypatch.setattr(
        boot_service,
        "boot_status",
        lambda **_kw: boot_service.BootStatus(
            supported=True, kind="launchd", registered=True, active=None
        ),
    )

    result = _invoke(runner, monkeypatch, ["boot", "status"])

    assert result.exit_code == 0
    assert "unknown" in result.stdout


def test_status_explains_an_unsupported_platform(runner, monkeypatch, sysdir):
    monkeypatch.setattr(
        boot_service,
        "boot_status",
        lambda **_kw: boot_service.BootStatus(
            supported=False, reason="This looks like a container"
        ),
    )

    result = _invoke(runner, monkeypatch, ["boot", "status"])

    assert result.exit_code == 0
    assert "container" in result.stdout
