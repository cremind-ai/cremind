"""Parity + contract guards for the installers' boot-service registration.

The two installers are hand-maintained mirrors of each other, and nothing
executes either one in CI (they install Python, pull images and start
servers). So the things that silently rot are the ones asserted here:

- the flag exists, and is documented, in both scripts;
- the choice is PERSISTED, in both, so a re-install doesn't undo an opt-out;
- unit generation lives in ONE place. The installers call ``cremind boot``;
  a unit file rendered in bash or PowerShell would be a third copy of the
  contract that ``app/system/boot_service.py`` owns, and the copies would
  drift the first time a setting changed;
- the teardown runs BEFORE the pid-file kills. The service is a supervisor:
  kill the server while the unit is still registered and it comes straight
  back, mid-uninstall, holding the files being deleted;
- a unit-run server never lands in ``install.pid`` — the desktop app
  tree-kills whatever PID sits there when it quits.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "install" / "install.ps1"
SH = REPO_ROOT / "install" / "install.sh"


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def _sh() -> str:
    return SH.read_text(encoding="utf-8")


# ── the flag itself ───────────────────────────────────────────────────────


def test_flag_declared_in_both_installers() -> None:
    sh = _sh()
    assert "--boot-service)" in sh and "--no-boot-service)" in sh
    ps1 = _ps1()
    assert "[switch] $BootService," in ps1
    assert "[switch] $NoBootService," in ps1


def test_flag_documented_in_both_help_texts() -> None:
    assert ".PARAMETER BootService" in _ps1()
    assert ".PARAMETER NoBootService" in _ps1()
    # install.sh's --help prints its own header block verbatim.
    header = _sh().split("set -e", 1)[0]
    assert "--boot-service" in header and "--no-boot-service" in header


def test_contradictory_powershell_switches_are_rejected() -> None:
    """The sh side can't be given both; the switch pair can."""
    assert "-BootService and -NoBootService are mutually exclusive" in _ps1()


def test_default_is_on_in_both() -> None:
    assert "$BootServiceOn = $true" in _ps1()
    assert "BOOT_SERVICE=1" in _sh()


# ── persistence ───────────────────────────────────────────────────────────


def test_the_choice_is_written_to_env_in_both() -> None:
    assert "CREMIND_BOOT_SERVICE=$BootMarker" in _ps1()
    assert "printf 'CREMIND_BOOT_SERVICE=%s\\n' \"$BOOT_MARKER\"" in _sh()


def test_a_kept_env_file_is_updated_too_in_both() -> None:
    """A pre-feature .env has no such key; it must gain one on re-install."""
    assert "Set-CremindEnvKey -Path $EnvFile -Key 'CREMIND_BOOT_SERVICE'" in _ps1()
    assert 'upsert_env_key "$ENV_FILE" CREMIND_BOOT_SERVICE' in _sh()


def test_only_an_explicit_opt_out_survives_a_reinstall() -> None:
    """Missing means "installed before this feature existed", not "no".

    Reading a missing marker as an opt-out would mean no existing install ever
    gains the service, which is the population that most needs it.
    """
    assert "CREMIND_BOOT_SERVICE\\s*=\\s*disabled" in _ps1()
    assert "CREMIND_BOOT_SERVICE[[:space:]]*=[[:space:]]*disabled" in _sh()


# ── gating ────────────────────────────────────────────────────────────────


def test_registration_is_skipped_under_the_desktop_app_in_both() -> None:
    """The app spawns and kills the backend itself; a unit would fight it."""
    sh = _sh()
    electron_block = sh.split("# ── boot service ──", 1)[1].split("BOOT_MARKER", 1)[0]
    assert 'CREMIND_INSTALLER_FRONTEND:-}" = "electron"' in electron_block
    assert "BOOT_SERVICE=0" in electron_block

    ps1 = _ps1()
    ps1_block = ps1.split("# ── boot service ──", 1)[1].split("$BootMarker =", 1)[0]
    assert "CREMIND_INSTALLER_FRONTEND -eq 'electron'" in ps1_block
    assert "$BootServiceOn = $false" in ps1_block


def test_docker_mode_forces_it_off_in_both() -> None:
    """The daemon already restarts the container."""
    sh_block = _sh().split("# ── boot service ──", 1)[1].split("BOOT_MARKER", 1)[0]
    assert '"$MODE" = "docker"' in sh_block

    ps1_block = _ps1().split("# ── boot service ──", 1)[1].split("$BootMarker =", 1)[0]
    assert "$Mode -eq 'docker'" in ps1_block


# ── single source of truth ────────────────────────────────────────────────


def test_the_installers_never_render_a_unit_file() -> None:
    """Registration goes through `cremind boot`, which owns the contract.

    A unit rendered here would be a second copy of settings like
    ``Restart=always`` and ``ExecutionTimeLimit``, and the copies would drift
    the moment one of them changed.
    """
    for script in (_sh(), _ps1()):
        assert "Restart=always" not in script
        assert "KeepAlive" not in script
        assert "WantedBy=" not in script
        assert "LogonTrigger" not in script
        assert "/XML" not in script


def test_both_installers_call_cremind_boot_enable() -> None:
    assert 'boot enable --no-start --yes' in _sh()
    assert 'boot enable --yes' in _sh()
    assert "boot enable --no-start --yes" in _ps1()
    assert "boot enable --yes" in _ps1()


def test_a_running_server_gets_registration_without_a_start() -> None:
    """A dev `cremind serve` owns the port; a second one would only collide."""
    sh = _sh()
    start_block = sh.split("# ── start the server", 1)[1]
    assert re.search(
        r'SERVER_RUNNING" -eq 1 \].*?boot enable --no-start', start_block, re.S
    )

    ps1_block = _ps1().split("# ── start the server", 1)[1]
    assert re.search(r"if \(\$running\).*?boot enable --no-start", ps1_block, re.S)


def test_a_failed_registration_falls_back_to_the_old_spawn() -> None:
    """WSL without systemd, SSH to a Mac, a locked-down Task Scheduler — the
    install must still leave the user with a reachable wizard."""
    sh_block = _sh().split("# ── start the server", 1)[1]
    assert "starting Cremind for this session only" in sh_block
    assert "nohup" in sh_block

    ps1_block = _ps1().split("# ── start the server", 1)[1]
    assert "starting Cremind for this session only" in ps1_block
    assert "Start-Process -FilePath $VenvCremind" in ps1_block


def test_a_service_run_server_is_not_written_to_install_pid() -> None:
    """Electron tree-kills that PID on quit; a service-run server must not be it.

    The legacy spawn is the only writer, and it only runs when the boot
    service did not take over.
    """
    sh_block = _sh().split("# ── start the server", 1)[1]
    pid_write = sh_block.index('echo $! > "$SERVER_PID_FILE"')
    fallback_guard = sh_block.index('if [ "$SERVER_RUNNING" -eq 0 ]; then\n        nohup')
    assert fallback_guard < pid_write

    ps1_block = _ps1().split("# ── start the server", 1)[1]
    assert ps1_block.index("if (-not $running) {") < ps1_block.index(
        "$proc.Id | Set-Content -Path $PidFile"
    )


def test_the_stop_hint_is_service_aware_in_both() -> None:
    """Telling a user to kill a PID a supervisor would respawn is wrong."""
    sh = _sh()
    assert "cremind boot disable" in sh
    assert "systemctl --user stop cremind" in sh
    assert "launchctl bootout gui/" in sh

    ps1 = _ps1()
    assert "cremind boot disable" in ps1
    assert "Stop-ScheduledTask -TaskName 'Cremind Server'" in ps1


# ── uninstall ─────────────────────────────────────────────────────────────


def test_teardown_precedes_the_pid_kills_in_both() -> None:
    """The ordering is the whole correctness argument.

    A registered unit respawns the server within seconds of the kill, so a
    teardown that ran afterwards would leave a live server while the
    uninstaller deletes its files.
    """
    sh = _sh()
    assert sh.index("BOOT_TORN_DOWN=0") < sh.index(
        'if [ -f "$UNINSTALL_INSTALL_DIR/install.pid" ]'
    )

    ps1 = _ps1()
    assert ps1.index("$BootTornDown = $false") < ps1.index(
        "Stop-CremindProcess (Join-Path $UninstallInstallDir 'install.pid')"
    )


def test_teardown_is_unconditional_and_precedes_removal_in_both() -> None:
    """Before the keep/purge branch, so neither mode can skip it — and so
    ``cremind boot disable`` still has the venv it is about to delete.

    The unit lives outside both directories, so nothing else in either script
    would ever remove it — exactly like the PATH entry beside it.
    """
    sh = _sh()
    # "# Apply the chosen mode." opens the keep/purge branch; everything that
    # must happen for both modes is above it.
    assert sh.index("BOOT_TORN_DOWN=0") < sh.index("# Apply the chosen mode.")
    assert sh.index("# Apply the chosen mode.") < sh.index(
        '"$UNINSTALL_SYSTEM_DIR/venv" \\'
    )

    ps1 = _ps1()
    assert ps1.index("$BootTornDown = $false") < ps1.index(
        "if ($UninstallMode -eq 'purge') {\n        if (Test-RootLikePath"
    )
    assert ps1.index("$BootTornDown = $false") < ps1.index("$toRemoveFromSystem = @(")


def test_the_raw_fallback_covers_every_platform() -> None:
    """A venv too broken to run `cremind boot` is exactly when --purge is used."""
    sh = _sh()
    assert "launchctl bootout" in sh
    assert "systemctl --user disable --now cremind.service" in sh
    assert "io.cremind.server.plist" in sh
    assert "systemd/user/cremind.service" in sh

    ps1 = _ps1()
    assert "schtasks.exe /Delete /TN 'Cremind Server' /F" in ps1
    assert "schtasks.exe /End /TN 'Cremind Server'" in ps1


def test_the_windows_supervisor_is_tree_killed() -> None:
    """The server hangs off the loop via cmd.exe; only /T reaches it."""
    ps1 = _ps1()
    assert "taskkill.exe /PID $supervisorPid /T /F" in ps1
    assert "'supervisor.pid'" in ps1
