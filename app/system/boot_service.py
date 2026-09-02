"""Register Cremind with the OS so it starts at login and stays up.

A native install has no supervisor. The installer starts ``cremind serve`` once,
for the install session, and that process dies at logout. Worse, the in-app
restart is a deliberate *stop* (see :mod:`app.system.restart`): the server
drains its connections, runs its cleanup and exits, but it cannot re-exec
itself, so on native installs ``POST /api/system/restart`` — and the
``CREMIND_SSL=after-setup`` flip to HTTPS that depends on it — leave the
backend down. This module closes both gaps by handing the job to the
operating system's own service manager:

- Linux:   a **systemd user unit** at ``~/.config/systemd/user/cremind.service``
- macOS:   a **LaunchAgent** at ``~/Library/LaunchAgents/io.cremind.server.plist``
- Windows: a **Scheduled Task** (``Cremind Server``) that fires at logon and
  launches a small respawn loop (Task Scheduler's own restart-on-failure has a
  one-minute floor, far too slow for the restart flows above).

Two invariants everything here is built around:

1. **Always launch the bin shim**, never the venv binary. ``app/config/settings.py``
   resolves its dotenv path relative to the working directory, so a bare
   ``cremind serve`` sees none of the install's ``~/.cremind/.env`` — no
   ``CREMIND_SSL``, no ``HOST``, no ``INSTALL_MODE``. The installer-generated
   shim (``<system dir>/bin/cremind``) is the only thing that loads it, and on
   POSIX it ``exec``s, so the unit's MainPID *is* the server.
2. **Never touch ``install.pid``.** The desktop app tree-kills whatever PID is
   in that file when it quits; a service-run server must not be collateral.
   The Windows loop uses ``supervisor.pid``, and the server itself writes
   ``server.pid`` when supervised (see ``app.server``).

The unit sets ``CREMIND_SUPERVISED=1``, which is how the running server knows
something will respawn it — that flag is what flips ``restart_supported`` for
native installs (``app.config.tls_mode``).

Scope: the registration is **per OS user**, not per Cremind profile. One
``cremind serve`` process serves every profile, so there is exactly one unit.

Stdlib-only and free of ``app.config`` imports, so ``app/cli/commands/boot.py``
can use it under the CLI's import discipline. Callers pass ``system_dir``.

Style follows ``app/config/tls_trust.py``: pure planners that decide and render
but run nothing, and one executor that runs a plan and returns error strings
rather than raising.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Service managers are quick; a call that takes longer than this is wedged, not
# working. (``schtasks`` against a busy Task Scheduler service is the slowest
# realistic case.)
_TOOL_TIMEOUT = 60

#: systemd user unit name.
UNIT_NAME = "cremind.service"
#: launchd job label (also the plist's basename).
LAUNCHD_LABEL = "io.cremind.server"
#: Windows Scheduled Task name. Registered in the root folder: creating a task
#: *folder* is not reliably permitted for a standard user, a root-level task is.
TASK_NAME = "Cremind Server"

#: Written by the Windows respawn loop so ``disable`` can find and stop it.
SUPERVISOR_PID_FILE = "supervisor.pid"
#: Written by the server itself when ``CREMIND_SUPERVISED`` is set. The
#: uninstallers have always stopped whatever PID is in here.
SERVER_PID_FILE = "server.pid"
#: Dropped by the server the moment it begins a *deliberate* shutdown (the
#: Restart Server button, an upgrade, a restore, the after-setup HTTPS flip),
#: and consumed by the Windows respawn loop below. Without it the loop cannot
#: tell "the user asked for a restart 4 seconds after boot" from "this server
#: crashes on startup", and damps the former as if it were the latter.
RESTART_DELIBERATE_FILE = ".restart.deliberate"


@dataclass(frozen=True)
class BootArtifact:
    """A file the plan writes before it runs anything."""

    path: str
    content: str
    #: ``schtasks /XML`` wants UTF-16; everything else is plain ASCII text.
    encoding: str = "utf-8"


@dataclass(frozen=True)
class BootPlan:
    """What enabling (or disabling) the boot service does on this platform."""

    supported: bool
    #: "systemd" | "launchd" | "schtasks"
    kind: str | None = None
    #: Human path of the unit/plist/task, for messages.
    unit_path: str | None = None
    artifacts: list[BootArtifact] = field(default_factory=list)
    #: Commands whose failure fails the plan.
    commands: list[list[str]] = field(default_factory=list)
    #: Commands whose failure is a warning ("already unloaded", linger denied
    #: by polkit, stopping a task that is not running).
    best_effort: list[list[str]] = field(default_factory=list)
    #: PID files to tree-kill after the commands (Windows teardown).
    kill_pid_files: list[str] = field(default_factory=list)
    #: Files to delete after the commands (teardown).
    remove_paths: list[str] = field(default_factory=list)
    #: Best-effort commands that must run *after* ``remove_paths`` — systemd
    #: only forgets a deleted unit once it has been told to reload.
    cleanup: list[list[str]] = field(default_factory=list)
    #: Why ``supported`` is False, in words a user can act on.
    reason: str | None = None


@dataclass(frozen=True)
class BootStatus:
    """What the OS says about the registration right now."""

    supported: bool
    kind: str | None = None
    #: True/False where knowable, None where the tooling cannot say.
    registered: bool | None = None
    #: Whether the service manager considers the unit running.
    active: bool | None = None
    unit_path: str | None = None
    #: Linux only: survives logout? None elsewhere / when unknown.
    linger: bool | None = None
    server_pid: int | None = None
    #: Free-text detail (unit state words, task query error).
    detail: str | None = None
    reason: str | None = None


def render_command(argv: list[str]) -> str:
    """argv as a copy-pasteable line."""
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


# ── refusals ──────────────────────────────────────────────────────────────


def boot_environment_error() -> str | None:
    """Why a boot service makes no sense here, or None if it does.

    Mirrors ``app/api/tls.py``'s ``_trust_environment_error``: a reason string
    the caller turns into a refusal, so the wording lives in one place.
    """
    mode = (os.environ.get("INSTALL_MODE") or "").strip().lower()
    if mode in ("docker", "kubernetes"):
        return (
            "This install runs in a container — Docker's restart policy (or "
            "the kubelet) already brings Cremind back. There is nothing for a "
            "boot service to do."
        )
    if os.environ.get("CREMIND_ELECTRON_PARENT") is not None:
        return (
            "The Cremind desktop app starts and stops the backend itself. Use "
            "the app's own launch-at-login setting instead."
        )
    if sys.platform.startswith("linux"):
        for marker in ("/.dockerenv", "/run/.containerenv"):
            if os.path.exists(marker):
                return (
                    "This looks like a container — its supervisor, not a "
                    "systemd user unit, decides when Cremind restarts."
                )
    return None


def default_system_dir() -> Path:
    """``~/.cremind`` — the one system dir a boot service is offered for."""
    return Path(os.path.expanduser("~")) / ".cremind"


def _is_default_system_dir(system_dir: Path) -> bool:
    """Whether ``system_dir`` is the canonical one, comparing resolved paths."""
    try:
        a = os.path.normcase(os.path.realpath(str(system_dir)))
        b = os.path.normcase(os.path.realpath(str(default_system_dir())))
    except OSError:  # pragma: no cover - defensive
        return False
    return a == b


# ── rendering ─────────────────────────────────────────────────────────────


_SYSTEMD_UNIT = """\
# Generated by "cremind boot enable" — do not edit; regenerated on enable.
[Unit]
Description=Cremind server
# Fast-crash damping. A healthy restart cycle (the in-app restart, the
# after-setup HTTPS flip, an upgrade) is one restart, nowhere near this.
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=exec
# The shim, never the venv binary: only the shim loads ~/.cremind/.env, and it
# exec()s, so MainPID below is the server itself. Quoted because systemd splits
# ExecStart on whitespace, and a home directory may contain a space.
ExecStart="__EXEC__" serve
WorkingDirectory=__SYSTEM_DIR__
Restart=always
RestartSec=2
Environment=CREMIND_SUPERVISED=1
# The server hard-exits 12s after SIGTERM when supervised; leave headroom
# before systemd escalates to SIGKILL.
TimeoutStopSec=20
StandardOutput=append:__SYSTEM_DIR__/server.log
StandardError=append:__SYSTEM_DIR__/server.log

[Install]
WantedBy=default.target
"""


_LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Generated by "cremind boot enable" - do not edit; regenerated on enable. -->
<plist version="1.0">
<dict>
  <key>Label</key><string>__LABEL__</string>
  <key>ProgramArguments</key>
  <array>
    <string>__EXEC__</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <!-- Plain true, not {SuccessfulExit: false}: the supervised shutdown path
       hard-exits with status 0, and that must still be respawned. -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CREMIND_SUPERVISED</key><string>1</string>
  </dict>
  <key>WorkingDirectory</key><string>__SYSTEM_DIR__</string>
  <key>StandardOutPath</key><string>__SYSTEM_DIR__/server.log</string>
  <key>StandardErrorPath</key><string>__SYSTEM_DIR__/server.log</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
"""


_TASK_XML = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts the Cremind server at logon and restarts it if it \
stops. Generated by "cremind boot enable" - manage it with that command, not \
by hand.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>__USER__</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>__USER__</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <!-- The default is 72 hours, after which Task Scheduler would kill a
         perfectly healthy server. PT0S means no limit. -->
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <!-- The task default (7) runs below-normal with low I/O priority, which
         is not what a server wants. 5 is normal. -->
    <Priority>5</Priority>
    <!-- Belt and braces: the loop below is the real supervisor, but if the
         loop itself dies this brings it back. One minute is the floor Task
         Scheduler allows, which is exactly why it cannot be the supervisor. -->
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>//B //NoLogo "__VBS__"</Arguments>
      <WorkingDirectory>__SYSTEM_DIR__</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# wscript is the only launcher that shows no console at all — powershell
# -WindowStyle Hidden still flashes one at every logon. Style 0 hides the
# loop's own window; Wait=True keeps the task instance "Running" for as long
# as the supervisor lives, so Task Scheduler's view matches reality.
#
# Single-quoted on purpose: VBScript escapes a quote by doubling it, so the
# body contains ``"""`` and would close a normal triple-quoted string.
_BOOT_VBS = '''\
' Generated by "cremind boot enable" - do not edit; regenerated on enable.
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""__LOOP__""", 0, True
'''


_BOOT_LOOP_PS1 = """\
# Generated by "cremind boot enable" - do not edit; regenerated on enable.
#
# The respawn supervisor. Task Scheduler can only retry a failed task once a
# minute, but the in-app restart and the after-setup HTTPS flip expect the
# server back within seconds, so the restarting happens here.
$ErrorActionPreference = 'Continue'
$systemDir = '__SYSTEM_DIR__'
$shim      = '__EXEC__'
$logFile   = Join-Path $systemDir 'server.log'
$supPid    = Join-Path $systemDir '__SUPERVISOR_PID__'
$srvPid    = Join-Path $systemDir '__SERVER_PID__'
$restartMk = Join-Path $systemDir '__RESTART_DELIBERATE__'

# Single-instance guard. A manual "schtasks /Run" or a second logon trigger
# (fast user switching) would otherwise start a rival supervisor.
try {
  $existing = [int]((Get-Content -LiteralPath $supPid -ErrorAction Stop | Select-Object -First 1).Trim())
  if ($existing -gt 0 -and $existing -ne $PID -and (Get-Process -Id $existing -ErrorAction SilentlyContinue)) {
    exit 0
  }
} catch { }
Set-Content -LiteralPath $supPid -Value $PID -Encoding ascii

# The contract with the server: something respawns me on exit. This is what
# flips restart_supported for native installs.
$env:CREMIND_SUPERVISED = '1'

# Internal API port for the health probe below. Same precedence as the shim:
# a real environment variable wins over the .env file.
$port = '1112'
if ($env:PORT) {
  $port = $env:PORT
} else {
  $envFile = Join-Path $systemDir '.env'
  if (Test-Path -LiteralPath $envFile) {
    foreach ($line in (Get-Content -LiteralPath $envFile -ErrorAction SilentlyContinue)) {
      if ($line -match '^\\s*PORT\\s*=\\s*"?(\\d+)"?\\s*$') { $port = $Matches[1]; break }
    }
  }
}

function Test-CremindUp {
  try {
    Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2 | Out-Null
    return $true
  } catch {
    return $false
  }
}

while ($true) {
  if (Test-CremindUp) {
    # Someone else owns the port - a manual "cremind serve", a dev run. The
    # server's own pre-flight would just SystemExit, so probing first saves a
    # full Python boot every cycle.
    Start-Sleep -Seconds 30
    continue
  }

  # Keep server.log from growing without bound on an always-on machine.
  try {
    if ((Test-Path -LiteralPath $logFile) -and ((Get-Item -LiteralPath $logFile).Length -gt 10MB)) {
      Move-Item -LiteralPath $logFile -Destination "$logFile.1" -Force
    }
  } catch { }

  $started = [DateTime]::UtcNow
  # cmd.exe owns the redirection so the shim's batch semantics stay untouched.
  & $env:ComSpec /d /c "`"$shim`" serve >> `"$logFile`" 2>&1"
  $elapsed = ([DateTime]::UtcNow - $started).TotalSeconds

  Remove-Item -LiteralPath $srvPid -Force -ErrorAction SilentlyContinue

  # Did it stop because someone asked it to? The server drops this marker as
  # it begins a deliberate shutdown - the Restart Server button, an upgrade, a
  # restore, the after-setup HTTPS flip. Consume it either way: a marker left
  # behind by an earlier restart must never excuse a later crash.
  $deliberate = Test-Path -LiteralPath $restartMk
  Remove-Item -LiteralPath $restartMk -Force -ErrorAction SilentlyContinue

  if ($elapsed -lt 15 -and -not $deliberate) {
    # Died on startup: a port clash the probe missed, a broken venv, bad
    # config. Back off hard rather than spinning on Python imports.
    Start-Sleep -Seconds 30
  } else {
    # A normal restart. The wizard's first probe lands about 2s after the
    # kill, so come back inside that window.
    Start-Sleep -Seconds 2
  }
}
"""


def _render(template: str, **subs: str) -> str:
    out = template
    for key, value in subs.items():
        out = out.replace(f"__{key}__", value)
    return out


def _xml(value: str) -> str:
    """Escape a value going into the task XML or the plist.

    Both are XML documents and both carry filesystem paths, which on Windows
    may legally contain ``&`` — enough to make the whole document unparseable
    and the registration fail with nothing useful said about why.

    Hand-rolled rather than ``xml.sax.saxutils.escape``: that module pulls in
    ``urllib`` transitively, which is a lot of import for four substitutions
    in a module that is meant to cost nothing to import.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _ps_single(value: str) -> str:
    """Escape a value going inside a PowerShell single-quoted string."""
    return value.replace("'", "''")


# ── platform paths ────────────────────────────────────────────────────────


def _systemd_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "systemd" / "user" / UNIT_NAME


def _launchd_plist_path() -> Path:
    return Path(os.path.expanduser("~")) / "Library" / "LaunchAgents" / (
        f"{LAUNCHD_LABEL}.plist"
    )


def _windows_paths(system_dir: Path) -> tuple[Path, Path, Path]:
    """``(task xml, launcher vbs, respawn loop)`` under ``<system dir>/bin``."""
    bin_dir = system_dir / "bin"
    return (
        bin_dir / "cremind-task.xml",
        bin_dir / "cremind-boot.vbs",
        bin_dir / "cremind-boot-loop.ps1",
    )


def _launchd_domain() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return f"gui/{uid}"


def _env_user_id() -> str:
    """``DOMAIN\\user`` from the environment — Task Scheduler resolves names."""
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    user = os.environ.get("USERNAME") or ""
    return f"{domain}\\{user}" if domain else user


def current_user_id() -> str:
    """The caller's SID, falling back to ``DOMAIN\\user``.

    A SID survives an account rename and sidesteps locale issues in the task
    XML; ``whoami`` is on every Windows install. Impure by nature — the pure
    planners take the result as an argument.
    """
    try:
        proc = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - defensive
        return _env_user_id()
    if proc.returncode != 0:  # pragma: no cover - defensive
        return _env_user_id()
    # ``"HOST\\user","S-1-5-21-..."`` — the SID is the last quoted field.
    fields = [f.strip().strip('"') for f in (proc.stdout or "").strip().split(",")]
    if fields and fields[-1].upper().startswith("S-1-"):
        return fields[-1]
    return _env_user_id()  # pragma: no cover - defensive


# ── enable ────────────────────────────────────────────────────────────────


def enable_plan(
    exec_path: str,
    *,
    system_dir: Path | str,
    start_now: bool = True,
    user_id: str | None = None,
) -> BootPlan:
    """How to register a boot service for ``exec_path`` (the bin shim).

    Pure: it probes the filesystem and renders text, but runs nothing.
    ``user_id`` is Windows-only; when omitted the environment's
    ``DOMAIN\\user`` is used (see :func:`current_user_id` for the SID form).
    """
    sysdir = Path(system_dir)
    refusal = boot_environment_error()
    if refusal:
        return BootPlan(supported=False, reason=refusal)

    if not _is_default_system_dir(sysdir):
        return BootPlan(
            supported=False,
            reason=(
                f"CREMIND_SYSTEM_DIR is {sysdir}, not the default "
                f"{default_system_dir()}. There is one boot service per user, "
                "so registering it for a side-by-side install would fight the "
                "main one. Run `cremind serve` yourself for this install."
            ),
        )

    if sys.platform == "win32":
        return _enable_windows(exec_path, sysdir, start_now, user_id)
    if sys.platform == "darwin":
        return _enable_launchd(exec_path, sysdir, start_now)
    if sys.platform.startswith("linux"):
        return _enable_systemd(exec_path, sysdir, start_now)
    return BootPlan(
        supported=False,
        reason=f"No boot-service integration for platform {sys.platform!r}.",
    )


def _enable_systemd(exec_path: str, sysdir: Path, start_now: bool) -> BootPlan:
    from shutil import which

    if which("systemctl") is None:
        return BootPlan(
            supported=False,
            reason="systemctl is not on PATH — this system does not run systemd.",
        )
    if not os.path.isdir("/run/systemd/system"):
        return BootPlan(
            supported=False,
            reason=(
                "systemd is not the init system here. On WSL, enable it by "
                "adding `[boot]` / `systemd=true` to /etc/wsl.conf and running "
                "`wsl --shutdown`; otherwise start Cremind however this system "
                "manages services."
            ),
        )
    runtime = os.environ.get("XDG_RUNTIME_DIR") or (
        f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else ""
    )
    if not runtime or not os.path.isdir(os.path.join(runtime, "systemd")):
        return BootPlan(
            supported=False,
            reason=(
                "The systemd *user* manager is not reachable from this session "
                "(no XDG_RUNTIME_DIR/systemd). Over SSH this usually means "
                "lingering is off: run `sudo loginctl enable-linger $USER`, "
                "reconnect, and try again."
            ),
        )

    unit_path = _systemd_unit_path()
    commands: list[list[str]] = [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", UNIT_NAME],
    ]
    if start_now:
        # restart, not start: idempotent, and it takes over cleanly from a
        # unit that is already running an older ExecStart.
        commands.append(["systemctl", "--user", "restart", UNIT_NAME])
    return BootPlan(
        supported=True,
        kind="systemd",
        unit_path=str(unit_path),
        artifacts=[
            BootArtifact(
                path=str(unit_path),
                content=_render(
                    _SYSTEMD_UNIT, EXEC=exec_path, SYSTEM_DIR=str(sysdir)
                ),
            )
        ],
        commands=commands,
        # Without lingering the user manager stops at logout, taking the
        # server with it. polkit may refuse; that is a warning, not a failure.
        best_effort=[["loginctl", "enable-linger", _current_user_name()]],
    )


def _enable_launchd(exec_path: str, sysdir: Path, start_now: bool) -> BootPlan:
    plist_path = _launchd_plist_path()
    domain = _launchd_domain()
    best_effort: list[list[str]] = []
    commands: list[list[str]] = []
    if start_now:
        # bootout first so a re-enable replaces a loaded job rather than
        # failing with "service already loaded"; absent is fine.
        best_effort.append(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
        commands.append(["launchctl", "bootstrap", domain, str(plist_path)])
    return BootPlan(
        supported=True,
        kind="launchd",
        unit_path=str(plist_path),
        artifacts=[
            BootArtifact(
                path=str(plist_path),
                content=_render(
                    _LAUNCHD_PLIST,
                    EXEC=_xml(exec_path),
                    SYSTEM_DIR=_xml(str(sysdir)),
                    LABEL=LAUNCHD_LABEL,
                ),
            )
        ],
        commands=commands,
        best_effort=best_effort,
    )


def _enable_windows(
    exec_path: str, sysdir: Path, start_now: bool, user_id: str | None
) -> BootPlan:
    xml_path, vbs_path, loop_path = _windows_paths(sysdir)
    user = user_id or _env_user_id()
    commands: list[list[str]] = [
        ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"]
    ]
    if start_now:
        commands.append(["schtasks", "/Run", "/TN", TASK_NAME])
    return BootPlan(
        supported=True,
        kind="schtasks",
        unit_path=f'Scheduled Task "{TASK_NAME}"',
        artifacts=[
            BootArtifact(
                path=str(loop_path),
                content=_render(
                    _BOOT_LOOP_PS1,
                    EXEC=_ps_single(exec_path),
                    SYSTEM_DIR=_ps_single(str(sysdir)),
                    SUPERVISOR_PID=SUPERVISOR_PID_FILE,
                    SERVER_PID=SERVER_PID_FILE,
                    RESTART_DELIBERATE=RESTART_DELIBERATE_FILE,
                ),
            ),
            BootArtifact(
                path=str(vbs_path),
                content=_render(_BOOT_VBS, LOOP=str(loop_path)),
            ),
            BootArtifact(
                path=str(xml_path),
                # schtasks /XML reads UTF-16; Python's "utf-16" writes the BOM
                # it expects.
                content=_render(
                    _TASK_XML,
                    USER=_xml(user),
                    VBS=_xml(str(vbs_path)),
                    SYSTEM_DIR=_xml(str(sysdir)),
                ),
                encoding="utf-16",
            ),
        ],
        commands=commands,
    )


def _current_user_name() -> str:
    """Login name for ``loginctl enable-linger``."""
    import getpass

    try:
        return getpass.getuser()
    except (KeyError, OSError):  # pragma: no cover - no passwd entry
        return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


# ── disable ───────────────────────────────────────────────────────────────


def disable_plan(*, system_dir: Path | str) -> BootPlan:
    """How to remove the boot service. Idempotent by construction.

    Every command is best-effort: stopping a task that is not running, or
    unloading a job that was never loaded, is an error to the tool but a
    success to us. Callers confirm the end state with :func:`boot_status`
    rather than trusting exit codes.
    """
    sysdir = Path(system_dir)
    if sys.platform == "win32":
        xml_path, vbs_path, loop_path = _windows_paths(sysdir)
        return BootPlan(
            supported=True,
            kind="schtasks",
            unit_path=f'Scheduled Task "{TASK_NAME}"',
            best_effort=[
                # /End first: kill the supervisor's task instance before the
                # registration goes, so nothing respawns mid-teardown.
                ["schtasks", "/End", "/TN", TASK_NAME],
                ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            ],
            # The loop, its cmd child and the server hang off the supervisor
            # PID; server.pid catches a server orphaned by a dead loop.
            kill_pid_files=[
                str(sysdir / SUPERVISOR_PID_FILE),
                str(sysdir / SERVER_PID_FILE),
            ],
            remove_paths=[
                str(xml_path),
                str(vbs_path),
                str(loop_path),
                str(sysdir / SUPERVISOR_PID_FILE),
            ],
        )
    if sys.platform == "darwin":
        plist_path = _launchd_plist_path()
        return BootPlan(
            supported=True,
            kind="launchd",
            unit_path=str(plist_path),
            best_effort=[
                ["launchctl", "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"]
            ],
            remove_paths=[str(plist_path)],
        )
    if sys.platform.startswith("linux"):
        unit_path = _systemd_unit_path()
        return BootPlan(
            supported=True,
            kind="systemd",
            unit_path=str(unit_path),
            best_effort=[
                ["systemctl", "--user", "disable", "--now", UNIT_NAME],
            ],
            remove_paths=[str(unit_path)],
            # Only after the file is gone does a reload make systemd forget it.
            cleanup=[["systemctl", "--user", "daemon-reload"]],
        )
    return BootPlan(
        supported=False,
        reason=f"No boot-service integration for platform {sys.platform!r}.",
    )


# ── execution ─────────────────────────────────────────────────────────────


def _no_window_kwargs() -> dict:
    """Keep console windows from flashing when the CLI runs under a GUI."""
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _run(argv: list[str]) -> tuple[int, str]:
    """``(returncode, detail)``. A tool that cannot run gets returncode 127."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT,
            **_no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, f"Failed to run {argv[0]}: {e}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return proc.returncode, (
            f"{argv[0]} exited with {proc.returncode}"
            + (f": {detail}" if detail else "")
        )
    return 0, (proc.stdout or "").strip()


def run_boot_plan(plan: BootPlan) -> tuple[bool, str | None, list[str]]:
    """Execute ``plan``. Returns ``(ok, error_detail, warnings)``.

    Order matters: artifacts land before the commands that register them,
    and files are removed only after the commands that referenced them have
    run. Nothing raises; failures come back as strings.
    """
    from shutil import which

    warnings: list[str] = []
    if not plan.supported:
        return False, plan.reason or "Not supported here.", warnings

    # Preflight the required tools up front, the way run_trust_plan does:
    # discovering a missing tool halfway through leaves a half-registered
    # service. Best-effort commands are merely skipped — a teardown must still
    # delete its files on a machine where the service manager has gone.
    for argv in plan.commands:
        if which(argv[0]) is None:
            return False, f"{argv[0]} is not on PATH.", warnings

    def _run_best_effort(argvs: list[list[str]]) -> None:
        for argv in argvs:
            if which(argv[0]) is None:
                warnings.append(f"Skipped {argv[0]}: not on PATH.")
                continue
            code, detail = _run(argv)
            if code != 0:
                warnings.append(detail)

    for artifact in plan.artifacts:
        try:
            path = Path(artifact.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(artifact.content, encoding=artifact.encoding)
        except OSError as e:
            return False, f"Could not write {artifact.path}: {e}", warnings

    _run_best_effort(plan.best_effort)

    for argv in plan.commands:
        code, detail = _run(argv)
        if code != 0:
            return False, detail, warnings

    for pid_file in plan.kill_pid_files:
        warnings.extend(_kill_pid_file(pid_file))

    for target in plan.remove_paths:
        try:
            Path(target).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            warnings.append(f"Could not remove {target}: {e}")

    _run_best_effort(plan.cleanup)

    return True, None, warnings


def _read_pid(path: str) -> int | None:
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    try:
        pid = int(raw.split()[0])
    except (ValueError, IndexError):
        return None
    return pid if pid > 0 else None


#: What ``supervisor.pid`` may name: the respawn loop, which the Scheduled
#: Task launches through wscript as ``powershell.exe``.
_SUPERVISOR_IMAGE_NAMES = ("powershell.exe",)
#: What ``server.pid`` may name. The loop runs the install's ``cremind.cmd``
#: shim, which runs ``venv\\Scripts\\cremind.exe`` — but that is a launcher
#: stub: it CreateProcess-es the interpreter and waits on it. The process that
#: writes ``server.pid`` (its own ``os.getpid()``) is therefore the child
#: ``python.exe``. ``cremind.exe`` stays accepted for any packaging whose
#: launcher runs the interpreter in-process instead.
_SERVER_IMAGE_NAMES = ("python.exe", "pythonw.exe", "cremind.exe")


def _kill_pid_file(path: str) -> list[str]:
    """Tree-kill the process named by ``path``. Returns warnings.

    Windows recycles PIDs aggressively, so the process is identified before
    anything is killed — a stale ``supervisor.pid`` must never take an
    innocent process with it.
    """
    pid = _read_pid(path)
    if pid is None:
        return []
    if sys.platform == "win32":
        is_supervisor = path.endswith(SUPERVISOR_PID_FILE)
        expected = (
            _SUPERVISOR_IMAGE_NAMES if is_supervisor else _SERVER_IMAGE_NAMES
        )
        name = _process_name(pid)
        if name is None:
            return []
        if name.lower() not in expected:
            return [
                f"Skipped killing pid {pid} from {os.path.basename(path)}: it is "
                f"{name}, not {' or '.join(expected)} (stale pid file)."
            ]
        # A name is weak evidence for the server: every Python on the machine
        # is a "python.exe". Where Windows will tell us the image path, demand
        # the very interpreter this install runs — ``boot disable`` and the
        # uninstaller both execute from the install's own venv, which is the
        # same venv the shim launches.
        if not is_supervisor:
            image = _process_image_path(pid)
            if image is not None and not _same_binary(image, sys.executable):
                return [
                    f"Skipped killing pid {pid} from {os.path.basename(path)}: "
                    f"it runs {image}, not this install's {sys.executable} "
                    "(stale pid file)."
                ]
        code, detail = _run(["taskkill", "/PID", str(pid), "/T", "/F"])
        if code != 0:
            return [detail]
        # taskkill returns as soon as the kill is *requested*. The task runs
        # with the system dir as its working directory, so until the tree has
        # actually gone Windows still holds a handle on it and an uninstall
        # deleting that directory fails with a sharing violation.
        if not _wait_for_exit(pid):
            return [f"pid {pid} did not exit after taskkill."]
        return []
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return []
    except OSError as e:  # pragma: no cover - defensive
        return [f"Could not stop pid {pid}: {e}"]
    return []


def _wait_for_exit(pid: int, timeout: float = 10.0) -> bool:
    """Block until ``pid`` is gone, or ``timeout`` elapses."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.25)
    return not _pid_alive(pid)


def _process_name(pid: int) -> str | None:
    """Image name of ``pid`` on Windows, or None if it is not running."""
    code, out = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    if code != 0:
        return None
    line = (out or "").strip()
    # tasklist prints an INFO line when nothing matches, and it is localized —
    # so only a properly quoted CSV row counts as a match.
    if not line.startswith('"'):
        return None
    return line.split(",")[0].strip().strip('"')


#: PROCESS_QUERY_LIMITED_INFORMATION — the least-privileged right that still
#: answers "which binary is this?", and the one a standard user is granted for
#: their own processes.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_image_path(pid: int) -> str | None:
    """Full path of ``pid``'s executable on Windows, or None if unknowable.

    Best-effort by nature — a process owned by another user, one that exits
    mid-call, or a Python without a usable ``ctypes`` all answer None, and the
    caller falls back to the image *name*. Same shape as the Windows ancestor
    lookup in ``app/cli/session.py``: any failure is None, never an exception.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                return None
            return buf.value or None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - identification is best-effort
        return None


def _same_binary(a: str, b: str) -> bool:
    """Whether two paths name the same executable file."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        # One of them is gone, or lives somewhere samefile cannot stat. Compare
        # the resolved text instead: on Windows, case-insensitively.
        return (
            os.path.normcase(os.path.realpath(a))
            == os.path.normcase(os.path.realpath(b))
        )


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _process_name(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - running as another user
        return True
    except OSError:  # pragma: no cover - defensive
        return False
    return True


# ── status ────────────────────────────────────────────────────────────────


def boot_status(*, system_dir: Path | str) -> BootStatus:
    """Ask the OS whether the boot service is registered and running."""
    sysdir = Path(system_dir)
    refusal = boot_environment_error()
    if refusal:
        return BootStatus(supported=False, reason=refusal)

    server_pid = _read_pid(str(sysdir / SERVER_PID_FILE))
    if server_pid is not None and not _pid_alive(server_pid):
        server_pid = None

    if sys.platform == "win32":
        # Exit code only. "Task does not exist" is a nonzero exit with a
        # localized message, so parsing the output would be both fragile and
        # pointless — and echoing it as a detail would read like a failure
        # when "not registered" is the whole answer.
        code, _ = _run(["schtasks", "/Query", "/TN", TASK_NAME])
        registered = code == 0
        sup = _read_pid(str(sysdir / SUPERVISOR_PID_FILE))
        active = bool(sup and _pid_alive(sup))
        note = f"supervisor pid {sup}" if active else None
        return BootStatus(
            supported=True,
            kind="schtasks",
            registered=registered,
            active=active,
            unit_path=f'Scheduled Task "{TASK_NAME}"',
            server_pid=server_pid,
            detail=note,
        )

    if sys.platform == "darwin":
        plist_path = _launchd_plist_path()
        code, _ = _run(
            ["launchctl", "print", f"{_launchd_domain()}/{LAUNCHD_LABEL}"]
        )
        return BootStatus(
            supported=True,
            kind="launchd",
            registered=plist_path.is_file(),
            active=code == 0,
            unit_path=str(plist_path),
            server_pid=server_pid,
        )

    if sys.platform.startswith("linux"):
        unit_path = _systemd_unit_path()
        enabled_code, enabled_out = _run(
            ["systemctl", "--user", "is-enabled", UNIT_NAME]
        )
        active_code, active_out = _run(
            ["systemctl", "--user", "is-active", UNIT_NAME]
        )
        # is-active prints one stable word ("active"/"inactive"/"failed") on
        # both the success and failure paths, so take it from either.
        state = (active_out or "").strip().splitlines()
        detail = state[-1] if state else None
        return BootStatus(
            supported=True,
            kind="systemd",
            registered=enabled_code == 0 or unit_path.is_file(),
            active=active_code == 0,
            unit_path=str(unit_path),
            linger=_linger_enabled(),
            server_pid=server_pid,
            detail=detail or (enabled_out or None),
        )

    return BootStatus(
        supported=False,
        reason=f"No boot-service integration for platform {sys.platform!r}.",
    )


def _linger_enabled() -> bool | None:
    """Whether this user's services survive logout. None if unknowable.

    ``enable-linger`` can be refused by polkit without failing loudly, so the
    answer is read back rather than inferred from the earlier exit code.
    """
    from shutil import which

    if which("loginctl") is None:
        return None
    code, out = _run(
        ["loginctl", "show-user", _current_user_name(), "--property=Linger"]
    )
    if code != 0:
        return None
    return "yes" in (out or "").strip().lower()
