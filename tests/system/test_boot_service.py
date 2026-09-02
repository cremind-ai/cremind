"""Tests for ``app.system.boot_service`` — the boot-service plans.

Three OS integrations that almost nobody can run all three of, so what these
tests pin is the *rendered* contract: the lines in each unit file that make
the difference between "Cremind comes back" and "Cremind stays down", and the
argv lists that register them. Whatever ships in the unit file is what the
service manager obeys — there is no second chance to notice a missing
``Restart=always`` at 3am on a user's machine.

Platform is faked the way ``tests/config/test_tls_trust.py`` does it, so the
whole matrix runs on any dev box.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.system import boot_service


SHIM = "C:\\x\\bin\\cremind.cmd" if sys.platform == "win32" else "/x/bin/cremind"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No install-mode or Electron markers leaking in from the environment."""
    monkeypatch.delenv("INSTALL_MODE", raising=False)
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)


@pytest.fixture
def sysdir(tmp_path, monkeypatch):
    """A system dir that ``enable_plan`` accepts as the default one.

    ``enable_plan`` refuses a non-default ``CREMIND_SYSTEM_DIR`` (one service
    per user, so a side-by-side install must not register a rival), which is
    exactly the guard being satisfied here rather than bypassed.
    """
    home = tmp_path / "home"
    (home / ".cremind").mkdir(parents=True)
    monkeypatch.setattr(boot_service.os.path, "expanduser", lambda _p: str(home))
    return home / ".cremind"


def _enable(monkeypatch, platform, sysdir, **kwargs):
    monkeypatch.setattr(sys, "platform", platform)
    return boot_service.enable_plan(SHIM, system_dir=sysdir, **kwargs)


def _artifact(plan, suffix):
    """The one artifact whose path ends with ``suffix``."""
    matches = [a for a in plan.artifacts if a.path.endswith(suffix)]
    assert matches, f"no artifact ending in {suffix}: {[a.path for a in plan.artifacts]}"
    return matches[0]


# ── linux / systemd ───────────────────────────────────────────────────────


@pytest.fixture
def linux_ok(monkeypatch, tmp_path):
    """A Linux box where systemd and its user manager are both present."""
    runtime = tmp_path / "run-user"
    (runtime / "systemd").mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        boot_service.os.path,
        "isdir",
        lambda p: p == "/run/systemd/system" or Path(p).is_dir(),
    )
    monkeypatch.setattr(boot_service.os.path, "exists", lambda _p: False)
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")


def test_systemd_unit_carries_every_load_bearing_line(monkeypatch, linux_ok, sysdir):
    plan = _enable(monkeypatch, "linux", sysdir)

    assert plan.supported is True
    assert plan.kind == "systemd"
    unit = _artifact(plan, "cremind.service").content

    # Without Restart, the in-app restart and the after-setup HTTPS flip both
    # leave the server down — the whole reason this feature exists.
    assert "Restart=always" in unit
    assert "RestartSec=2" in unit
    # The contract that flips restart_supported for native installs.
    assert "Environment=CREMIND_SUPERVISED=1" in unit
    # The shim, never the venv binary: only the shim loads ~/.cremind/.env.
    assert f'ExecStart="{SHIM}" serve' in unit
    assert "WantedBy=default.target" in unit
    # The supervised shutdown path hard-exits at 12s; SIGKILL must come later.
    assert "TimeoutStopSec=20" in unit
    assert "__" not in unit, "an unrendered placeholder survived"


def test_systemd_enables_and_starts_in_order(monkeypatch, linux_ok, sysdir):
    plan = _enable(monkeypatch, "linux", sysdir)

    assert plan.commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", boot_service.UNIT_NAME],
        ["systemctl", "--user", "restart", boot_service.UNIT_NAME],
    ]
    # Lingering is what keeps a headless box serving after logout, but polkit
    # may refuse it — a warning, never a failed enable.
    assert plan.best_effort[0][:2] == ["loginctl", "enable-linger"]


def test_systemd_no_start_registers_without_starting(monkeypatch, linux_ok, sysdir):
    plan = _enable(monkeypatch, "linux", sysdir, start_now=False)

    assert ["systemctl", "--user", "enable", boot_service.UNIT_NAME] in plan.commands
    assert not any("restart" in argv for argv in plan.commands)


def test_wsl_without_systemd_is_unsupported_and_says_so(monkeypatch, linux_ok, sysdir):
    monkeypatch.setattr(boot_service.os.path, "isdir", lambda _p: False)

    plan = _enable(monkeypatch, "linux", sysdir)

    assert plan.supported is False
    assert "wsl.conf" in (plan.reason or "").lower()
    assert plan.commands == [] and plan.artifacts == []


def test_missing_user_manager_points_at_linger(monkeypatch, linux_ok, sysdir):
    """Over SSH without lingering there is no user manager to talk to."""
    monkeypatch.setattr(
        boot_service.os.path, "isdir", lambda p: p == "/run/systemd/system"
    )

    plan = _enable(monkeypatch, "linux", sysdir)

    assert plan.supported is False
    assert "enable-linger" in (plan.reason or "")


def test_systemd_disable_reloads_only_after_removing_the_unit(monkeypatch, tmp_path):
    """A reload before the delete leaves systemd still holding the unit."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    plan = boot_service.disable_plan(system_dir=tmp_path)

    assert plan.best_effort == [
        ["systemctl", "--user", "disable", "--now", boot_service.UNIT_NAME]
    ]
    assert plan.remove_paths == [str(boot_service._systemd_unit_path())]
    assert plan.cleanup == [["systemctl", "--user", "daemon-reload"]]


# ── macos / launchd ───────────────────────────────────────────────────────


def test_launchd_plist_keeps_alive_and_runs_at_load(monkeypatch, sysdir):
    monkeypatch.setattr(boot_service.os, "getuid", lambda: 501, raising=False)

    plan = _enable(monkeypatch, "darwin", sysdir)

    assert plan.supported is True and plan.kind == "launchd"
    plist = _artifact(plan, ".plist").content
    assert "<key>RunAtLoad</key><true/>" in plist
    # Plain true, not {SuccessfulExit: false}: the supervised shutdown path
    # exits 0 on purpose and must still be respawned.
    assert "<key>KeepAlive</key><true/>" in plist
    assert f"<string>{SHIM}</string>" in plist
    assert "CREMIND_SUPERVISED" in plist
    assert str(sysdir / "server.log").replace("\\", "/") in plist.replace("\\", "/")
    assert "__" not in plist


def test_launchd_bootout_precedes_bootstrap(monkeypatch, sysdir):
    """Re-enabling over a loaded job fails unless it is booted out first."""
    monkeypatch.setattr(boot_service.os, "getuid", lambda: 501, raising=False)

    plan = _enable(monkeypatch, "darwin", sysdir)

    assert plan.best_effort == [
        ["launchctl", "bootout", f"gui/501/{boot_service.LAUNCHD_LABEL}"]
    ]
    assert plan.commands[0][:2] == ["launchctl", "bootstrap"]


def test_launchd_no_start_only_writes_the_plist(monkeypatch, sysdir):
    """A LaunchAgent is loaded at login anyway — writing it is 'registered'."""
    monkeypatch.setattr(boot_service.os, "getuid", lambda: 501, raising=False)

    plan = _enable(monkeypatch, "darwin", sysdir, start_now=False)

    assert plan.commands == [] and plan.best_effort == []
    assert plan.artifacts


# ── windows / schtasks ────────────────────────────────────────────────────


def test_windows_task_xml_is_scoped_to_this_user(monkeypatch, sysdir):
    plan = _enable(monkeypatch, "win32", sysdir, user_id="S-1-5-21-99")

    assert plan.supported is True and plan.kind == "schtasks"
    xml = _artifact(plan, ".xml")
    # Unelevated registration only works when nothing privileged is asked
    # for: the caller's own principal, an interactive token, least privilege.
    assert xml.content.count("<UserId>S-1-5-21-99</UserId>") == 2
    assert "<LogonType>InteractiveToken</LogonType>" in xml.content
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml.content
    # Task Scheduler kills a task after 72h by default, and runs it at
    # below-normal priority. Both are wrong for a server.
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml.content
    assert "<Priority>5</Priority>" in xml.content
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml.content
    # schtasks /XML reads UTF-16 and rejects anything else.
    assert xml.encoding == "utf-16"
    assert "__" not in xml.content


def test_windows_task_launches_the_loop_not_the_shim(monkeypatch, sysdir):
    """The task action must be the respawn loop.

    Pointing it straight at the shim would mean Task Scheduler is the only
    supervisor, and its restart-on-failure floor is one minute — far too slow
    for the in-app restart and the after-setup HTTPS flip.
    """
    plan = _enable(monkeypatch, "win32", sysdir, user_id="S-1-5-21-99")

    xml = _artifact(plan, ".xml").content
    assert "<Command>wscript.exe</Command>" in xml
    assert "cremind-boot.vbs" in xml
    assert "cremind.cmd</Command>" not in xml

    vbs = _artifact(plan, ".vbs").content
    assert "cremind-boot-loop.ps1" in vbs
    # Style 0 is the only launcher that shows no console window at logon.
    assert ", 0, True" in vbs

    loop = _artifact(plan, ".ps1").content
    assert f'$shim      = \'{SHIM}\'' in loop
    assert "CREMIND_SUPERVISED = '1'" in loop
    # Never install.pid: the desktop app tree-kills that PID when it quits.
    assert boot_service.SUPERVISOR_PID_FILE in loop
    assert "install.pid" not in loop
    assert "__" not in loop


def test_the_loop_damps_crashes_but_not_deliberate_restarts(monkeypatch, sysdir):
    """A restart clicked seconds after a boot is not a crash loop.

    The 15s/30s damping is right for a port clash or a broken venv and wrong
    for the Restart Server button, the after-setup HTTPS flip, or an upgrade —
    all of which the server marks before it goes.
    """
    plan = _enable(monkeypatch, "win32", sysdir, user_id="S-1-5-21-99")
    loop = _artifact(plan, ".ps1").content

    assert boot_service.RESTART_DELIBERATE_FILE in loop
    # The marker is read, then consumed unconditionally: one left behind by an
    # earlier restart must never excuse a later crash.
    assert "$deliberate = Test-Path -LiteralPath $restartMk" in loop
    assert "Remove-Item -LiteralPath $restartMk" in loop
    assert "if ($elapsed -lt 15 -and -not $deliberate) {" in loop


def test_windows_enable_registers_then_runs(monkeypatch, sysdir):
    plan = _enable(monkeypatch, "win32", sysdir, user_id="S-1-5-21-99")
    xml_path = _artifact(plan, ".xml").path

    assert plan.commands == [
        ["schtasks", "/Create", "/TN", boot_service.TASK_NAME, "/XML", xml_path, "/F"],
        ["schtasks", "/Run", "/TN", boot_service.TASK_NAME],
    ]


def test_windows_disable_ends_the_task_before_deleting_it(monkeypatch, tmp_path):
    """Deleting first would leave the supervisor running with no owner."""
    monkeypatch.setattr(sys, "platform", "win32")

    plan = boot_service.disable_plan(system_dir=tmp_path)

    assert plan.best_effort == [
        ["schtasks", "/End", "/TN", boot_service.TASK_NAME],
        ["schtasks", "/Delete", "/TN", boot_service.TASK_NAME, "/F"],
    ]
    assert plan.kill_pid_files == [
        str(tmp_path / boot_service.SUPERVISOR_PID_FILE),
        str(tmp_path / boot_service.SERVER_PID_FILE),
    ]
    assert str(tmp_path / "bin" / "cremind-boot-loop.ps1") in plan.remove_paths


# ── awkward paths ─────────────────────────────────────────────────────────


def test_systemd_quotes_an_exec_path_with_a_space(monkeypatch, linux_ok, sysdir):
    """systemd splits ExecStart on whitespace; a home with a space would break."""
    monkeypatch.setattr(sys, "platform", "linux")
    plan = boot_service.enable_plan(
        "/home/first last/.cremind/bin/cremind", system_dir=sysdir
    )

    unit = _artifact(plan, "cremind.service").content
    assert 'ExecStart="/home/first last/.cremind/bin/cremind" serve' in unit


def test_the_task_xml_escapes_a_path_windows_actually_allows(monkeypatch, sysdir):
    """``&`` is legal in a Windows path and would make the XML unparseable.

    schtasks would then fail with nothing useful said about why.
    """
    import xml.etree.ElementTree as ET

    monkeypatch.setattr(sys, "platform", "win32")
    plan = boot_service.enable_plan(
        "C:\\Users\\a&b\\.cremind\\bin\\cremind.cmd",
        system_dir=sysdir,
        user_id="DOM\\a&b",
    )

    xml = _artifact(plan, ".xml").content
    assert "&amp;" in xml
    ET.fromstring(xml)  # raises if the document is malformed


def test_the_plist_escapes_the_same_way(monkeypatch, sysdir):
    import xml.etree.ElementTree as ET

    monkeypatch.setattr(boot_service.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    plan = boot_service.enable_plan("/Users/a&b/.cremind/bin/cremind", system_dir=sysdir)

    ET.fromstring(_artifact(plan, ".plist").content)


def test_the_windows_loop_escapes_a_quote_in_a_path(monkeypatch, sysdir):
    """A PowerShell single-quoted string ends at the first bare quote."""
    monkeypatch.setattr(sys, "platform", "win32")
    plan = boot_service.enable_plan(
        "C:\\Users\\o'brien\\.cremind\\bin\\cremind.cmd",
        system_dir=sysdir,
        user_id="S-1-5-21-1",
    )

    loop = _artifact(plan, ".ps1").content
    assert "o''brien" in loop


# ── refusals ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["docker", "kubernetes"])
def test_containers_are_refused(monkeypatch, sysdir, mode):
    monkeypatch.setenv("INSTALL_MODE", mode)

    plan = _enable(monkeypatch, "linux", sysdir)

    assert plan.supported is False
    assert "container" in (plan.reason or "").lower()


def test_electron_is_refused(monkeypatch, sysdir):
    """The desktop app spawns and kills the backend itself."""
    monkeypatch.setenv("CREMIND_ELECTRON_PARENT", "1")

    plan = _enable(monkeypatch, "win32", sysdir)

    assert plan.supported is False
    assert "desktop app" in (plan.reason or "")


def test_side_by_side_system_dir_is_refused(monkeypatch, tmp_path):
    """One service per user, so a non-default system dir must not register."""
    monkeypatch.setattr(
        boot_service.os.path, "expanduser", lambda _p: str(tmp_path / "home")
    )
    (tmp_path / "home" / ".cremind").mkdir(parents=True)

    plan = _enable(monkeypatch, "linux", tmp_path / "elsewhere")

    assert plan.supported is False
    assert "side-by-side" in (plan.reason or "")


# ── executor ──────────────────────────────────────────────────────────────


def _record_runs(monkeypatch, ran, returncode=0, stderr=""):
    def _fake_run(argv, **_kwargs):
        ran.append(argv)
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(boot_service.subprocess, "run", _fake_run)


def test_a_missing_tool_stops_everything_before_it_starts(monkeypatch, tmp_path):
    """Half-registering a service is worse than not registering it."""
    ran: list = []
    _record_runs(monkeypatch, ran)
    monkeypatch.setattr("shutil.which", lambda _tool: None)
    target = tmp_path / "unit"

    plan = boot_service.BootPlan(
        supported=True,
        artifacts=[boot_service.BootArtifact(path=str(target), content="x")],
        commands=[["systemctl", "--user", "enable", "cremind.service"]],
    )
    ok, error, _warnings = boot_service.run_boot_plan(plan)

    assert ok is False and "not on PATH" in error
    assert ran == [], "nothing may run once a required tool is known to be missing"
    assert not target.exists(), "no artifact may be written either"


def test_artifacts_land_before_the_commands_that_register_them(monkeypatch, tmp_path):
    seen: list = []
    unit = tmp_path / "nested" / "cremind.service"

    def _fake_run(argv, **_kwargs):
        seen.append((argv[0], unit.exists()))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(boot_service.subprocess, "run", _fake_run)
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")

    plan = boot_service.BootPlan(
        supported=True,
        artifacts=[boot_service.BootArtifact(path=str(unit), content="[Unit]\n")],
        commands=[["systemctl", "--user", "enable", "cremind.service"]],
    )
    ok, error, _warnings = boot_service.run_boot_plan(plan)

    assert (ok, error) == (True, None)
    assert seen == [("systemctl", True)]
    assert unit.read_text() == "[Unit]\n"


def test_best_effort_failures_are_warnings_not_errors(monkeypatch, tmp_path):
    """polkit refusing enable-linger must not fail an otherwise good enable."""
    calls: list = []

    def _fake_run(argv, **_kwargs):
        calls.append(argv[0])
        if argv[0] == "loginctl":
            return SimpleNamespace(returncode=1, stdout="", stderr="denied")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(boot_service.subprocess, "run", _fake_run)
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")

    plan = boot_service.BootPlan(
        supported=True,
        best_effort=[["loginctl", "enable-linger", "me"]],
        commands=[["systemctl", "--user", "enable", "cremind.service"]],
    )
    ok, error, warnings = boot_service.run_boot_plan(plan)

    assert (ok, error) == (True, None)
    assert warnings and "denied" in warnings[0]
    assert calls == ["loginctl", "systemctl"]


def test_a_missing_best_effort_tool_still_lets_teardown_finish(monkeypatch, tmp_path):
    """A teardown must delete its files even where the service manager is gone."""
    stale = tmp_path / "cremind.service"
    stale.write_text("[Unit]\n")
    monkeypatch.setattr("shutil.which", lambda _tool: None)

    plan = boot_service.BootPlan(
        supported=True,
        best_effort=[["systemctl", "--user", "disable", "--now", "cremind.service"]],
        remove_paths=[str(stale)],
    )
    ok, error, warnings = boot_service.run_boot_plan(plan)

    assert (ok, error) == (True, None)
    assert not stale.exists()
    assert any("Skipped systemctl" in w for w in warnings)


def test_removing_an_absent_file_is_not_an_error(monkeypatch, tmp_path):
    """Disable is called unconditionally by the uninstallers."""
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")

    plan = boot_service.BootPlan(
        supported=True, remove_paths=[str(tmp_path / "never-existed")]
    )
    ok, error, warnings = boot_service.run_boot_plan(plan)

    assert (ok, error, warnings) == (True, None, [])


def test_a_failing_command_surfaces_the_tools_own_words(monkeypatch, tmp_path):
    _record_runs(monkeypatch, [], returncode=1, stderr="Access is denied.")
    monkeypatch.setattr("shutil.which", lambda tool: tool)

    plan = boot_service.BootPlan(
        supported=True, commands=[["schtasks", "/Create", "/TN", "Cremind Server"]]
    )
    ok, error, _warnings = boot_service.run_boot_plan(plan)

    assert ok is False
    assert "Access is denied." in error


def test_an_unsupported_plan_never_runs_anything(monkeypatch):
    ran: list = []
    _record_runs(monkeypatch, ran)

    ok, error, _warnings = boot_service.run_boot_plan(
        boot_service.BootPlan(supported=False, reason="no systemd here")
    )

    assert (ok, error) == (False, "no systemd here")
    assert ran == []


def test_a_stale_windows_pid_file_never_kills_a_stranger(monkeypatch, tmp_path):
    """Windows recycles PIDs fast; killing by a stale pid file is a real hazard."""
    monkeypatch.setattr(sys, "platform", "win32")
    pid_file = tmp_path / boot_service.SUPERVISOR_PID_FILE
    pid_file.write_text("4242")
    killed: list = []

    def _fake_run(argv, **_kwargs):
        if argv[0] == "tasklist":
            return SimpleNamespace(
                returncode=0, stdout='"notepad.exe","4242","Console"', stderr=""
            )
        killed.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(boot_service.subprocess, "run", _fake_run)
    monkeypatch.setattr("shutil.which", lambda tool: tool)

    warnings = boot_service._kill_pid_file(str(pid_file))

    assert killed == [], "a pid that is not our supervisor must be left alone"
    assert warnings and "stale pid file" in warnings[0]


def _fake_tasklist(name: str, killed: list):
    """subprocess.run stand-in: ``tasklist`` reports ``name`` until a kill
    lands, then reports nothing — so ``_wait_for_exit`` sees it go."""
    alive = [True]

    def _run(argv, **_kwargs):
        if argv[0] == "tasklist":
            if not alive[0]:
                return SimpleNamespace(returncode=0, stdout="INFO: no tasks", stderr="")
            return SimpleNamespace(
                returncode=0, stdout=f'"{name}","4242","Console"', stderr=""
            )
        killed.append(argv)
        alive[0] = False
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return _run


def test_the_server_pid_names_the_interpreter_not_the_launcher(monkeypatch, tmp_path):
    """``venv\\Scripts\\cremind.exe`` is a launcher stub — it CreateProcess-es
    the interpreter and waits — so the process that writes ``server.pid`` (its
    own getpid) is a ``python.exe``. Expecting ``cremind.exe`` meant the
    orphan-server kill was skipped on every single Windows install.
    """
    pid_file = tmp_path / boot_service.SERVER_PID_FILE
    pid_file.write_text("4242")
    killed: list = []

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(boot_service.subprocess, "run", _fake_tasklist("python.exe", killed))
    # The interpreter this install runs is the one holding the pid file.
    monkeypatch.setattr(boot_service, "_process_image_path", lambda pid: sys.executable)

    warnings = boot_service._kill_pid_file(str(pid_file))

    assert killed == [["taskkill", "/PID", "4242", "/T", "/F"]]
    assert warnings == []


def test_a_foreign_python_holding_the_server_pid_is_left_alone(monkeypatch, tmp_path):
    """Every Python on the machine is a "python.exe", so the name alone is
    weak evidence. Where Windows will name the binary, it has to be ours."""
    pid_file = tmp_path / boot_service.SERVER_PID_FILE
    pid_file.write_text("4242")
    killed: list = []

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(boot_service.subprocess, "run", _fake_tasklist("python.exe", killed))
    monkeypatch.setattr(
        boot_service, "_process_image_path", lambda pid: r"C:\Other\Python\python.exe"
    )

    warnings = boot_service._kill_pid_file(str(pid_file))

    assert killed == []
    assert warnings and "stale pid file" in warnings[0]


def test_an_unidentifiable_image_falls_back_to_the_name(monkeypatch, tmp_path):
    """A process whose path Windows won't hand over must not become
    unkillable — the name check is the floor, not a bonus."""
    pid_file = tmp_path / boot_service.SERVER_PID_FILE
    pid_file.write_text("4242")
    killed: list = []

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(boot_service.subprocess, "run", _fake_tasklist("python.exe", killed))
    monkeypatch.setattr(boot_service, "_process_image_path", lambda pid: None)

    warnings = boot_service._kill_pid_file(str(pid_file))

    assert killed == [["taskkill", "/PID", "4242", "/T", "/F"]]
    assert warnings == []


def test_a_stranger_holding_the_server_pid_is_left_alone(monkeypatch, tmp_path):
    """PID reuse again, from the other direction: the recycled number now
    belongs to something that is not a Python at all."""
    pid_file = tmp_path / boot_service.SERVER_PID_FILE
    pid_file.write_text("4242")
    killed: list = []

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(boot_service.subprocess, "run", _fake_tasklist("notepad.exe", killed))

    warnings = boot_service._kill_pid_file(str(pid_file))

    assert killed == []
    assert warnings and "stale pid file" in warnings[0]


def test_the_windows_supervisor_pid_is_tree_killed(monkeypatch, tmp_path):
    """The server hangs off the loop via cmd.exe, so only /T reaches it."""
    monkeypatch.setattr(sys, "platform", "win32")
    pid_file = tmp_path / boot_service.SUPERVISOR_PID_FILE
    pid_file.write_text("4242\n")
    killed: list = []
    alive = [True]

    def _fake_run(argv, **_kwargs):
        if argv[0] == "tasklist":
            if not alive[0]:
                return SimpleNamespace(returncode=0, stdout="INFO: no tasks", stderr="")
            return SimpleNamespace(
                returncode=0, stdout='"powershell.exe","4242","Console"', stderr=""
            )
        killed.append(argv)
        alive[0] = False
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(boot_service.subprocess, "run", _fake_run)

    assert boot_service._kill_pid_file(str(pid_file)) == []
    assert killed == [["taskkill", "/PID", "4242", "/T", "/F"]]


def test_the_kill_waits_for_the_tree_to_actually_exit(monkeypatch):
    """``taskkill`` returns when the kill is *requested*, not when it lands.

    The Scheduled Task's working directory is the system dir, so a still-dying
    tree holds a handle on it and the uninstaller's ``Remove-Item`` fails with
    a sharing violation — observed as a leftover empty ~/.cremind.
    """
    # Alive for the first two polls, gone on the third.
    polls = [True, True, False]
    monkeypatch.setattr(boot_service, "_pid_alive", lambda _pid: polls.pop(0))

    assert boot_service._wait_for_exit(4242, timeout=5.0) is True
    assert polls == [], "it must keep polling until the process is really gone"


def test_a_tree_that_never_dies_is_reported(monkeypatch, tmp_path):
    """Silence would let the uninstaller delete files out from under it."""
    monkeypatch.setattr(sys, "platform", "win32")
    pid_file = tmp_path / boot_service.SUPERVISOR_PID_FILE
    pid_file.write_text("4242")

    monkeypatch.setattr(
        boot_service.subprocess,
        "run",
        lambda argv, **_k: SimpleNamespace(
            returncode=0, stdout='"powershell.exe","4242","Console"', stderr=""
        ),
    )
    monkeypatch.setattr(boot_service, "_wait_for_exit", lambda _pid, **_kw: False)

    warnings = boot_service._kill_pid_file(str(pid_file))

    assert warnings and "did not exit" in warnings[0]


# ── status ────────────────────────────────────────────────────────────────


def test_status_reads_systemd_and_linger(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")

    def _fake_run(argv, **_kwargs):
        if "is-enabled" in argv:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if "is-active" in argv:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if argv[0] == "loginctl":
            return SimpleNamespace(returncode=0, stdout="Linger=yes\n", stderr="")
        raise AssertionError(f"unexpected command {argv}")

    monkeypatch.setattr(boot_service.subprocess, "run", _fake_run)

    status = boot_service.boot_status(system_dir=tmp_path)

    assert (status.registered, status.active, status.linger) == (True, True, True)
    assert status.kind == "systemd" and status.detail == "active"


def test_status_reports_linger_off_so_the_logout_surprise_is_visible(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")

    def _fake_run(argv, **_kwargs):
        if argv[0] == "loginctl":
            return SimpleNamespace(returncode=0, stdout="Linger=no\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="active\n", stderr="")

    monkeypatch.setattr(boot_service.subprocess, "run", _fake_run)

    assert boot_service.boot_status(system_dir=tmp_path).linger is False


def test_status_ignores_a_dead_server_pid(monkeypatch, tmp_path):
    """A pid file outliving its process must not be reported as running."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(boot_service, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        boot_service.subprocess,
        "run",
        lambda argv, **_k: SimpleNamespace(returncode=0, stdout="active", stderr=""),
    )
    (tmp_path / boot_service.SERVER_PID_FILE).write_text("31337")

    assert boot_service.boot_status(system_dir=tmp_path).server_pid is None
