<#
.SYNOPSIS
    Cremind installer for Windows (PowerShell).

.DESCRIPTION
    The Phase 2 installer. Native install only — Docker mode is detected
    and recommended, but the actual containerized bundle ships in Phase 3.

.EXAMPLE
    iwr -useb https://cremind.io/install.ps1 | iex

.EXAMPLE
    iwr -useb https://cremind.io/install.ps1 -OutFile install.ps1
    .\install.ps1 -Deployment server -Host 100.120.175.90

.PARAMETER Deployment
    'local', 'server', or 'custom'. Skips the deployment-type prompt.
    'custom' exposes advanced fields (listen host, public URL, allowed
    origins, wizard preset) so you can configure unusual setups —
    running inside a container, behind a reverse proxy, etc. 'container'
    is accepted as a deprecated alias for 'custom' with container
    defaults.

.PARAMETER AppHost
    Public IP/domain for server deployments.

.PARAMETER ListenHost
    (custom deployment) Override HOST in .env.

.PARAMETER PublicUrl
    (custom deployment) Override APP_URL in .env.

.PARAMETER AllowedOrigins
    (custom deployment) Override CORS_ALLOWED_ORIGINS in .env.

.PARAMETER WizardPreset
    (custom deployment) Override SETUP_WIZARD_ENV in .env.

.PARAMETER Desktop
    (Docker mode) Include the VNC Desktop UI — pulls cremind/cremind-desktop
    (XFCE + VNC). Default when neither -Desktop nor -NoDesktop is given.

.PARAMETER NoDesktop
    (Docker mode) Skip the VNC Desktop UI — pulls the smaller headless
    cremind/cremind image (no noVNC/VNC ports). A re-install keeps the
    previous choice unless you pass a flag. Setting both flags is an error.

.PARAMETER NoLaunch
    Skip opening the setup wizard at the end.

.PARAMETER Unattended
    Use defaults; never prompt. With Deployment='server', requires AppHost.

.PARAMETER Reinstall
    Wipe any existing %LOCALAPPDATA%\Cremind\venv before installing.

.PARAMETER AutoInstallPython
    Auto-install isolated Python 3.13 if missing (default: prompt;
    -Unattended installs silently).

.PARAMETER NoAutoInstallPython
    Never auto-install; print manual hints and exit when Python is missing.

.PARAMETER NoModifyPath
    Don't modify the User-scope PATH; print the manual setx instead.

.PARAMETER Channel
    Install source: 'production' (PyPI, default), 'test' (Test PyPI, for
    release-candidate validation), or 'dev' (pip install -e from the local
    checkout). 'dev' requires running this script from a clone of the
    repo; rejected when piped via iwr|iex. 'dev' works with both -Mode
    native (reuses <repo>\.venv) and -Mode docker (compose override
    bind-mounts the checkout at /src).

.PARAMETER Version
    Explicit cremind version to install (e.g. '0.2.1' for production,
    '0.2.1rc3' for test). Validated against the channel shape
    (production = X.Y.Z, test = X.Y.ZrcN). When -ElectronVersion is
    also given, must additionally match that line (production = exact,
    test = same X.Y.Z).

.PARAMETER ElectronVersion
    Build version of the Cremind desktop app driving this install (e.g.
    '0.2.1'). Forwarded by the Electron main process so the cremind
    package pins to the same line; CLI users typically don't set this.

.PARAMETER Uninstall
    Run the uninstaller instead of installing. Combine with -Keep or
    -Purge; omit both for an interactive k/p/c prompt. Honours
    $env:CREMIND_SYSTEM_DIR / $env:CREMIND_INSTALL_DIR.

.PARAMETER Keep
    (with -Uninstall) Remove binaries + install scratch; preserve
    .env, bootstrap.toml, storage\, tokens\, profile dirs.

.PARAMETER Purge
    (with -Uninstall) Wipe both System Dir and Install Dir. For docker
    installs, also runs ``docker compose down -v`` to drop volumes.

.NOTES
    Service selection (database backend, vector store backend, …) is no
    longer made at install time. The Setup Wizard now lets you pick each
    backing service's deployment mode (Docker / Native / External)
    per-service, independent of how Cremind itself is installed.
#>

[CmdletBinding()]
param(
    [ValidateSet('production','test','dev')] [string] $Channel = 'production',
    [ValidateSet('local','server','custom','container','')] [string] $Deployment = '',
    [string] $AppHost = '',
    [string] $ListenHost = '',
    [string] $PublicUrl = '',
    [string] $AllowedOrigins = '',
    [string] $WizardPreset = '',
    [ValidateSet('','docker','native')] [string] $Mode = '',
    # Docker mode only: include the VNC Desktop UI? -Desktop pulls
    # cremind/cremind-desktop; -NoDesktop pulls the headless cremind/cremind.
    # Neither set = ask (default desktop). Setting both is an error.
    [switch] $Desktop,
    [switch] $NoDesktop,
    [switch] $NoLaunch,
    [switch] $Unattended,
    [switch] $Reinstall,
    [switch] $AutoInstallPython,
    [switch] $NoAutoInstallPython,
    [switch] $ModifyPath,
    [switch] $NoModifyPath,
    [string] $Version = '',
    [string] $ElectronVersion = '',
    # Uninstall switch: when set, this script runs the uninstall flow
    # inline below (after param validation) and exits before any install-
    # side setup. -Keep / -Purge select the mode; omit both for interactive.
    [switch] $Uninstall,
    [switch] $Keep,
    [switch] $Purge,
    # Skip the prompt_toolkit TUI bootstrap and fall back to the legacy
    # numbered prompts. CI/debugging only — interactive users benefit
    # from the TUI's keyboard navigation and version picker.
    [switch] $NoTui
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── -Uninstall flow ───────────────────────────────────────────────────────
# Inline uninstaller. Runs before banner / catalog / TUI setup so the
# install path's interactive scaffolding never touches an uninstall run.
#
# Removes Cremind from this machine. Operates on two directories:
#
#   System Dir  ($env:CREMIND_SYSTEM_DIR / default %USERPROFILE%\.cremind)
#       Native installs: runtime + user content (.env, bootstrap.toml,
#       storage\, tokens\, venv\, bin\, per-profile dirs, server.log,
#       upgrade artifacts). Docker installs leave the host's ~\.cremind
#       untouched — runtime state lives in the ``cremind-data`` named
#       Docker volume.
#
#   Install Dir ($env:CREMIND_INSTALL_DIR / default %LOCALAPPDATA%\Cremind)
#       Install-time scratch: install.log, install.pid, docker\ (compose
#       bundle), pip-cache\, uv-cache\, python\.
#
# Detection: $InstallDir\docker\docker-compose.yml -> Docker; else
#            $SystemDir\venv -> Native; else partial-install -> Native.
#
# The User Working Directory (server_config.user_working_dir, picked in
# the Setup Wizard) is NEVER touched directly. When it resolves inside
# the System Dir (typical default), it goes with -Purge along with the
# rest of the System Dir.
if ($Uninstall) {
    if ($Keep -and $Purge) {
        Write-Host "Pass at most one of -Keep / -Purge." -ForegroundColor Red
        exit 2
    }
    # ErrorActionPreference is 'Stop' from the install path; the uninstall
    # path expects 'Continue' so a non-fatal failure (e.g. transient PID
    # write) doesn't take the whole run down. Scope it to this block only.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {

    # Resolve System Dir and Install Dir (must match install path defaults).
    if ($env:CREMIND_SYSTEM_DIR) {
        $UninstallSystemDir = $env:CREMIND_SYSTEM_DIR
    } else {
        $UninstallSystemDir = Join-Path $env:USERPROFILE '.cremind'
    }
    if ($env:CREMIND_INSTALL_DIR) {
        $UninstallInstallDir = $env:CREMIND_INSTALL_DIR
    } else {
        $UninstallInstallDir = Join-Path $env:LOCALAPPDATA 'Cremind'
    }

    $systemExists  = Test-Path -LiteralPath $UninstallSystemDir
    $installExists = Test-Path -LiteralPath $UninstallInstallDir
    if (-not $systemExists -and -not $installExists) {
        Write-Host "Nothing to uninstall - neither $UninstallSystemDir nor $UninstallInstallDir exists."
        exit 0
    }

    # Detect kind from on-disk markers.
    $ComposeFile = Join-Path $UninstallInstallDir 'docker\docker-compose.yml'
    $UninstallVenvDir = Join-Path $UninstallSystemDir 'venv'
    if (Test-Path -LiteralPath $ComposeFile) {
        $Kind = 'docker'
    } elseif (Test-Path -LiteralPath $UninstallVenvDir) {
        $Kind = 'native'
    } elseif ($installExists) {
        # Install Dir present but no markers - partial install. Default to
        # native so the residual scratch gets cleaned up.
        $Kind = 'native'
    } else {
        Write-Host "Unrecognized install layout." -ForegroundColor Red
        Write-Host "Expected $UninstallSystemDir\venv (native) or $UninstallInstallDir\docker\docker-compose.yml (docker)." -ForegroundColor Red
        exit 1
    }

    # Resolve mode (flag or interactive).
    $UninstallMode = ''
    if ($Purge) { $UninstallMode = 'purge' }
    elseif ($Keep) { $UninstallMode = 'keep' }

    if (-not $UninstallMode) {
        Write-Host ''
        Write-Host "Uninstall Cremind ($Kind):"
        Write-Host "  System Dir:  $UninstallSystemDir"
        Write-Host "  Install Dir: $UninstallInstallDir"
        Write-Host ''
        Write-Host '  [k] Keep data    - remove the binaries + install scratch; preserve System Dir contents'
        Write-Host '                     (.env, bootstrap.toml, storage\, tokens\, profile dirs)'
        $purgeExtra = if ($Kind -eq 'docker') { ' (incl. Docker volumes)' } else { '' }
        Write-Host "  [p] Purge all    - delete everything Cremind installed$purgeExtra"
        Write-Host '  [c] Cancel'
        Write-Host ''
        $ans = Read-Host -Prompt 'Choose'
        switch -Regex ($ans) {
            '^[kK]' { $UninstallMode = 'keep' }
            '^[pP]' { $UninstallMode = 'purge' }
            default { Write-Host 'Cancelled.'; exit 0 }
        }
    }

    # Probe the User Working Directory (best-effort; only when sqlite3.exe
    # is on PATH). Used only for the post-purge "preserved at..." message.
    $UserDir = $null
    if ($UninstallMode -eq 'purge') {
        $sqlite = Get-Command sqlite3.exe -ErrorAction SilentlyContinue
        if ($sqlite) {
            $dbPath = Join-Path $UninstallSystemDir 'storage\cremind.db'
            if (Test-Path -LiteralPath $dbPath) {
                try {
                    $UserDir = & $sqlite.Source $dbPath "select value from server_config where key='user_working_dir'" 2>$null
                    $UserDir = ($UserDir | Select-Object -First 1)
                } catch {
                    $UserDir = $null
                }
            }
        }
    }

    # Stop any backend tracked via install.pid (install session) or
    # server.pid (long-running server). PowerShell's $pid is automatic -
    # use a non-clashing name.
    function Stop-CremindProcess([string]$pidFilePath) {
        if (-not (Test-Path -LiteralPath $pidFilePath)) { return }
        try {
            $childPid = [int](Get-Content -LiteralPath $pidFilePath -ErrorAction Stop | Select-Object -First 1)
            if ($childPid -gt 0) {
                try {
                    Stop-Process -Id $childPid -Force -ErrorAction Stop
                    Wait-Process -Id $childPid -Timeout 5 -ErrorAction SilentlyContinue
                } catch {
                    # Process already gone, no permission, or transient failure - proceed.
                }
            }
        } catch {
            # PID file unreadable or non-numeric - ignore.
        }
    }

    # ── Docker pre-check (Docker-mode installs only) ─────────────────────
    #
    # Runs BEFORE process-kill / file removal so the user can Abort
    # without leaving the install half-torn-down. Three outcomes:
    #   - Daemon up  : proceed normally; `docker compose down [-v]` runs.
    #   - Daemon down: pop a GUI MessageBox (NSIS-driven OR in-app
    #                  uninstall both see it). Retry / Ignore / Abort.
    #   - Not installed: log + skip (nothing to clean up).
    #
    # Returns $true / $false / $null (the latter = docker.exe not on PATH).
    function Test-DockerDaemonUp {
        $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
        if (-not $docker) { return $null }
        try {
            & $docker.Source info 2>$null | Out-Null
            return ($LASTEXITCODE -eq 0)
        } catch {
            return $false
        }
    }

    function Resolve-DockerOffline {
        Add-Type -AssemblyName System.Windows.Forms | Out-Null
        $body = @(
            "Docker Desktop isn't running. Cremind can't remove its containers and named volumes without it.",
            "",
            "  Retry   Start Docker Desktop, then click this to try cleanup again.",
            "  Ignore  Force-remove the Cremind data dirs anyway. Containers + named",
            "          volumes will be orphaned -- clean up later with:",
            "             docker compose -p cremind down -v",
            "  Abort   Cancel the uninstall."
        ) -join "`r`n"
        return [System.Windows.Forms.MessageBox]::Show(
            $body,
            'Cremind Uninstall - Docker Not Running',
            [System.Windows.Forms.MessageBoxButtons]::AbortRetryIgnore,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        )
    }

    $forceRemove = $false
    $dockerInstalled = $true
    if ($Kind -eq 'docker') {
        Add-Type -AssemblyName System.Windows.Forms | Out-Null
        $dockerUp = Test-DockerDaemonUp
        if ($null -eq $dockerUp) {
            # docker.exe not on PATH at all. Nothing to clean; skip the prompt.
            Write-Host 'Docker not installed on PATH; skipping container cleanup.' -ForegroundColor DarkGray
            $dockerInstalled = $false
        } else {
            while (-not $dockerUp) {
                $choice = Resolve-DockerOffline
                if ($choice -eq [System.Windows.Forms.DialogResult]::Retry) {
                    $dockerUp = Test-DockerDaemonUp
                    if ($null -eq $dockerUp) {
                        # docker.exe vanished mid-flow (unlikely). Treat as not-installed.
                        Write-Host 'Docker not installed on PATH; skipping container cleanup.' -ForegroundColor DarkGray
                        $dockerInstalled = $false
                        break
                    }
                } elseif ($choice -eq [System.Windows.Forms.DialogResult]::Ignore) {
                    $forceRemove = $true
                    Write-Host 'Force-removing Cremind without Docker cleanup.' -ForegroundColor Yellow
                    Write-Host "Run 'docker compose -p cremind down -v' later to remove orphaned containers and volumes." -ForegroundColor Yellow
                    break
                } else {
                    # Abort, dialog closed, or any other return value.
                    Write-Host 'Uninstall cancelled - Docker is required for Docker-mode cleanup.' -ForegroundColor Red
                    exit 3
                }
            }
        }
    }

    Stop-CremindProcess (Join-Path $UninstallInstallDir 'install.pid')
    Stop-CremindProcess (Join-Path $UninstallSystemDir 'server.pid')

    # Docker container/volume cleanup. Only runs when the daemon is reachable
    # AND the user didn't pick force-remove at the pre-check above.
    if ($Kind -eq 'docker' -and $dockerInstalled -and -not $forceRemove) {
        $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
        if ($docker -and (Test-Path -LiteralPath (Join-Path $UninstallInstallDir 'docker'))) {
            $DockerDir = Join-Path $UninstallInstallDir 'docker'
            Push-Location -LiteralPath $DockerDir
            try {
                if ($UninstallMode -eq 'purge') {
                    Write-Host 'Stopping containers and removing volumes...'
                    & $docker.Source compose -p cremind down -v --remove-orphans
                } else {
                    Write-Host 'Stopping containers (volumes preserved)...'
                    & $docker.Source compose -p cremind down --remove-orphans
                }
            } finally {
                Pop-Location
            }
        }
    }

    # Remove the bin entry from User-scope PATH. We match $BinDir exactly
    # to avoid clobbering unrelated PATH entries.
    $UninstallBinDir = Join-Path $UninstallSystemDir 'bin'
    try {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if ($userPath) {
            $entries = $userPath -split ';' | Where-Object { $_ -and ($_.TrimEnd('\') -ine $UninstallBinDir.TrimEnd('\')) }
            $newPath = ($entries -join ';').TrimEnd(';')
            if ($newPath -ne $userPath) {
                [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
                Write-Host "Removed $UninstallBinDir from User PATH."
            }
        }
    } catch {
        Write-Host "Warning: failed to clean User PATH ($_)." -ForegroundColor Yellow
    }

    function Test-RootLikePath([string]$path) {
        $resolved = $path.TrimEnd('\','/')
        return (-not $resolved -or $resolved -match '^[A-Za-z]:[\\/]?$')
    }

    if ($UninstallMode -eq 'purge') {
        if (Test-RootLikePath $UninstallSystemDir) {
            Write-Host "Refusing to purge a root-like System Dir: '$UninstallSystemDir'" -ForegroundColor Red
            exit 1
        }
        if (Test-RootLikePath $UninstallInstallDir) {
            Write-Host "Refusing to purge a root-like Install Dir: '$UninstallInstallDir'" -ForegroundColor Red
            exit 1
        }
        if (Test-Path -LiteralPath $UninstallSystemDir) {
            Remove-Item -LiteralPath $UninstallSystemDir -Recurse -Force -ErrorAction Continue
            Write-Host "Removed $UninstallSystemDir."
        }
        if (Test-Path -LiteralPath $UninstallInstallDir) {
            Remove-Item -LiteralPath $UninstallInstallDir -Recurse -Force -ErrorAction Continue
            Write-Host "Removed $UninstallInstallDir."
        }
        if ($UserDir -and ($UserDir -notlike "$UninstallSystemDir*")) {
            Write-Host "User Working Directory preserved at: $UserDir"
        }
    } else {
        # Keep mode: remove venv + bin + install scratch; preserve runtime state.
        $toRemoveFromSystem = @(
            'venv',
            'bin',
            'server.pid'
        )
        foreach ($name in $toRemoveFromSystem) {
            $p = Join-Path $UninstallSystemDir $name
            if (Test-Path -LiteralPath $p) {
                Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        # Wipe the Install Dir wholesale - it's all install scratch.
        if (Test-Path -LiteralPath $UninstallInstallDir) {
            Remove-Item -LiteralPath $UninstallInstallDir -Recurse -Force -ErrorAction SilentlyContinue
            Write-Host "Removed install scratch at $UninstallInstallDir."
        }
        Write-Host "Kept data in $UninstallSystemDir (.env, bootstrap.toml, storage\, tokens\, profile dirs)."
    }

    Write-Host 'Uninstall complete.'

    } finally { $ErrorActionPreference = $prevEAP }
    exit 0
}

# Channel is validated by the param()'s ValidateSet attribute; reaching
# this point implies $Channel is one of production / test / dev.

# RepoRoot is the repo containing this script. Required for dev mode (we
# install from there and read templates from there). Empty when piped via
# iwr|iex — the dev-mode check below uses that to reject pipe invocation.
$RepoRoot = ''
if ($PSCommandPath) {
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}
if ($Channel -eq 'dev') {
    if (-not $RepoRoot -or -not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) {
        Write-Host "ERR -Channel dev requires running install.ps1 from a checkout (not via iwr|iex)." -ForegroundColor Red
        Write-Host "    Usage: .\install\install.ps1 -Channel dev" -ForegroundColor Red
        exit 2
    }
}

# ── -Version validation ───────────────────────────────────────────────────
#
# Two layers of check, mirroring install.sh:
#   1. Channel-shape — production = X.Y.Z, test = X.Y.ZrcN. Dev ignores
#      -Version (editable install).
#   2. Electron-line — only when -ElectronVersion is also provided.
#      Production: exact match. Test: same X.Y.Z, rcN suffix.
#
# The error messages are the strings the Cremind desktop app surfaces in
# its install log; they name the Electron build that's rejecting the spec.
if ($Version -and $Channel -eq 'dev') {
    Write-Host "!!! -Version is ignored on dev channel (editable install)." -ForegroundColor Yellow
    $Version = ''
}
if ($Version) {
    switch ($Channel) {
        'production' {
            if ($Version -notmatch '^\d+\.\d+\.\d+$') {
                Write-Host "ERR Invalid version: '$Version' does not look like a production release (expected X.Y.Z)." -ForegroundColor Red
                exit 2
            }
        }
        'test' {
            # Fast fail-on-bad-arg shape check. The per-PR dev form
            # ``X.Y.ZrcN.devM`` is valid on the test channel. The
            # authoritative rules + ordering live in app/upgrade/channel.py
            # (which the resolver below invokes); keep this pattern in sync
            # with its ``matches_channel`` (a parity test guards drift).
            if ($Version -notmatch '^\d+\.\d+\.\d+rc\d+(\.dev\d+)?$') {
                Write-Host "ERR Invalid version: '$Version' does not look like a test prerelease (expected X.Y.ZrcN[.devM])." -ForegroundColor Red
                exit 2
            }
        }
    }
}
if ($Version -and $ElectronVersion) {
    switch ($Channel) {
        'production' {
            if ($Version -ne $ElectronVersion) {
                Write-Host "ERR Invalid version: '$Version' is not a valid production release for this Electron build (v$ElectronVersion). Production requires an exact version match — use the in-app update flow to install a different version." -ForegroundColor Red
                exit 2
            }
        }
        'test' {
            # No line lock on test: any published RC of any line is
            # installable on a test build (the shell auto-updates
            # independently). The shape check above already validated the
            # X.Y.ZrcN[.devM] form; nothing line-specific to enforce here.
        }
    }
}
# Windows PowerShell 5.1 defaults [Console]::OutputEncoding to the OEM
# code page (cp437 on US-English Windows), which mojibakes multi-byte
# UTF-8 like the em-dashes and box-drawing chars in our section headers
# when stdout is consumed by a UTF-8 reader (the Electron log viewer).
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

# Pipeline-chain operators (`&&`, `||`) and ternaries are not available in
# Windows PowerShell 5.1. We stick to explicit if/else throughout so the
# script works on the default PS that ships with Windows 10/11.

# ── logging helpers ───────────────────────────────────────────────────────

function Write-Info  { param($Msg) Write-Host "==> $Msg"      -ForegroundColor Cyan }
function Write-Ok    { param($Msg) Write-Host " OK $Msg"      -ForegroundColor Green }
function Write-Warn2 { param($Msg) Write-Host "!!! $Msg"      -ForegroundColor Yellow }
function Write-Err2  { param($Msg) Write-Host "ERR $Msg"      -ForegroundColor Red }
function Write-Step  { param($Msg) Write-Host "`n── $Msg ──"  -ForegroundColor White }
# Native executables (docker, uv, pip) write progress to stderr; PS 5.1
# wraps each line as a NativeCommandError record, which trips
# $ErrorActionPreference='Stop' on benign output. We scope it to
# 'Continue' for the duration so the *>> redirect captures everything
# into $LogFile without aborting the script.
function Invoke-NativeLogged {
    param([Parameter(Mandatory)][scriptblock]$Action)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # Redirect through Out-File -Encoding utf8 so the log isn't written
        # as PS 5.1's default UTF-16 LE (which makes every byte appear
        # spaced out in any UTF-8 reader). The ForEach-Object unwraps
        # stderr lines that PS 5.1 wraps as NativeCommandError
        # ErrorRecords — otherwise each benign stderr line (docker/pip/uv
        # progress) gets the full "At C:\...:line char:N" error decoration
        # in the log even on successful runs.
        & $Action 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $_.Exception.Message
            } else {
                $_
            }
        } | Out-File -FilePath $script:LogFile -Encoding utf8 -Append
    } finally { $ErrorActionPreference = $prev }
}

# ── unattended sanity check ──────────────────────────────────────────────

if ($Unattended -and -not $Deployment) { $Deployment = 'local' }
if ($Unattended -and $Deployment -eq 'server' -and -not $AppHost) {
    Write-Err2 "-Unattended with -Deployment server requires -AppHost"
    exit 2
}
if ($AutoInstallPython -and $NoAutoInstallPython) {
    Write-Err2 "-AutoInstallPython and -NoAutoInstallPython are mutually exclusive"
    exit 2
}
# -Unattended implies "yes" for the auto-install prompt unless overridden.
if ($Unattended -and -not $AutoInstallPython -and -not $NoAutoInstallPython) {
    $AutoInstallPython = $true
}
# Docker desktop-UI tri-state derived from the -Desktop / -NoDesktop switches:
# '' = ask (default desktop), '1' = desktop image, '0' = basic image.
if ($Desktop -and $NoDesktop) {
    Write-Err2 "-Desktop and -NoDesktop are mutually exclusive"
    exit 2
}
$DesktopUi = if ($Desktop) { '1' } elseif ($NoDesktop) { '0' } else { '' }

# ── paths ─────────────────────────────────────────────────────────────────

# Cremind System Directory — runtime state + user content root. Defaults to
# %USERPROFILE%\.cremind (the conventional ~/.cremind on Windows) so the
# layout users see hasn't changed since v0.1.9-test55. Override via
# $env:CREMIND_SYSTEM_DIR so power users can install side-by-side.
$CremindSystemDir = if ($env:CREMIND_SYSTEM_DIR) {
    $env:CREMIND_SYSTEM_DIR
} else {
    Join-Path $env:USERPROFILE '.cremind'
}
$DefaultCremindSystemDir = Join-Path $env:USERPROFILE '.cremind'

# Cremind Install Directory — install-time scratch (install.log, install.pid,
# docker compose bundle, pip/uv caches, downloaded Python). Defaults to
# %LOCALAPPDATA%\Cremind so install artifacts never pollute ~/.cremind.
$CremindInstallDir = if ($env:CREMIND_INSTALL_DIR) {
    $env:CREMIND_INSTALL_DIR
} else {
    Join-Path $env:LOCALAPPDATA 'Cremind'
}

# Paths in the System Dir (preserved across uninstall -Keep)
$VenvDir       = Join-Path $CremindSystemDir 'venv'
$EnvFile       = Join-Path $CremindSystemDir '.env'
$BootstrapFile = Join-Path $CremindSystemDir 'bootstrap.toml'
$ServerLogFile = Join-Path $CremindSystemDir 'server.log'
$BinDir        = Join-Path $CremindSystemDir 'bin'
$UvExe         = Join-Path $BinDir 'uv.exe'

# Paths in the Install Dir (scratch — safely deletable on uninstall -Keep)
$LogFile       = Join-Path $CremindInstallDir 'install.log'
$PidFile       = Join-Path $CremindInstallDir 'install.pid'

# Scope pip's cache under the Install Dir so a wipe of the System Dir
# (or -Reinstall) doesn't lose it, and a wipe of the Install Dir clears
# stale index responses. Without this, pip uses %LOCALAPPDATA%\pip\Cache,
# which persists across reinstalls.
$env:PIP_CACHE_DIR = Join-Path $CremindInstallDir 'pip-cache'

# The Install Dir is always needed (install.log, docker compose bundle,
# uv/pip caches). The System Dir is only created for native installs —
# Docker installs keep runtime state inside the ``cremind-data`` named
# volume and never touch the host's ~\.cremind.
if (-not (Test-Path $CremindInstallDir)) { New-Item -ItemType Directory -Path $CremindInstallDir | Out-Null }

# Forward both to the Python backend so its settings layer agrees with this
# script on path resolution (subprocess inherits these).
$env:CREMIND_SYSTEM_DIR  = $CremindSystemDir
$env:CREMIND_INSTALL_DIR = $CremindInstallDir

# Default ModifyPath: only modify User PATH for the canonical System Dir,
# so a staging/test install at a custom CREMIND_SYSTEM_DIR doesn't clobber
# the prod PATH entry. -ModifyPath / -NoModifyPath override.
if (-not $ModifyPath -and -not $NoModifyPath) {
    if ($CremindSystemDir -ieq $DefaultCremindSystemDir) {
        $ModifyPath = $true
    } else {
        $NoModifyPath = $true
    }
}

# Templates fetched at install time. Production/test fetch from GitHub;
# dev reads from the checkout's install\templates\ directly. The
# $TemplateMode flag tells Get-TemplateContent which branch to use.
if ($Channel -eq 'dev') {
    $TemplateBase = Join-Path $RepoRoot 'install\templates'
    $TemplateMode = 'local'
} else {
    $TemplateBase = if ($env:CREMIND_TEMPLATE_BASE) {
        $env:CREMIND_TEMPLATE_BASE
    } else {
        'https://raw.githubusercontent.com/cremind-ai/cremind/main/install/templates'
    }
    $TemplateMode = 'remote'
}

# Centralized template loader. Returns the raw text of a template; the
# caller writes it to disk (after rendering) or to stdout. Local mode
# reads from the checkout; remote mode does an HTTP GET.
function Get-TemplateContent {
    param([Parameter(Mandatory)][string] $Name)
    if ($script:TemplateMode -eq 'local') {
        # Templates are authored as UTF-8 (em-dashes, box-drawing chars).
        # Windows PowerShell 5.1's ``Get-Content -Raw`` defaults to the
        # system ANSI codepage (Windows-1252 on US-English Windows), which
        # mojibakes multi-byte UTF-8. Force UTF8 so the rendered output
        # matches the template byte-for-byte.
        return Get-Content -Raw -Encoding UTF8 -Path (Join-Path $script:TemplateBase $Name)
    }
    return (Invoke-WebRequest -UseBasicParsing -Uri "$script:TemplateBase/$Name").Content
}

# ── catalog ───────────────────────────────────────────────────────────────
#
# Source the generated _catalog.ps1 so the prompts and rendered .env
# files share their labels / descriptions / rules with install.sh and
# the Setup Wizard. Dev installs read from the checkout; remote
# installs download it next to the install scripts on GitHub.
# Prefer the checkout's own catalog + TUI when running from a clone (dev,
# or test/prod from source): they're the exact assets being tested and
# avoid fetching a possibly-stale copy from the default ref. curl|iex runs
# have no $RepoRoot and fall through to the remote download.
$UseLocalInstallAssets = $RepoRoot -and (Test-Path (Join-Path $RepoRoot 'install\_catalog.ps1'))
if ($UseLocalInstallAssets) {
    $CatalogPath = Join-Path $RepoRoot 'install\_catalog.ps1'
    . $CatalogPath
    $CatalogJson = Join-Path $RepoRoot 'install\_catalog.json'
    $InstallerTuiPyz = Join-Path $RepoRoot 'install\installer_tui.pyz'
    if (-not (Test-Path $CatalogJson)) { $CatalogJson = '' }
    if (-not (Test-Path $InstallerTuiPyz)) { $InstallerTuiPyz = '' }
} else {
    $CatalogBase = if ($env:CREMIND_CATALOG_BASE) {
        $env:CREMIND_CATALOG_BASE
    } else {
        'https://raw.githubusercontent.com/cremind-ai/cremind/main/install'
    }
    $CatalogTmp = Join-Path $CremindInstallDir '_catalog.ps1'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$CatalogBase/_catalog.ps1" -OutFile $CatalogTmp
    } catch {
        Write-Err2 "Failed to fetch install catalog from $CatalogBase/_catalog.ps1"
        exit 1
    }
    . $CatalogTmp
    # JSON catalog + zipapp TUI bundle are optional — failing the fetch
    # just drops us back to the legacy numbered prompts.
    $CatalogJson = Join-Path $CremindInstallDir '_catalog.json'
    $InstallerTuiPyz = Join-Path $CremindInstallDir 'installer_tui.pyz'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$CatalogBase/_catalog.json" -OutFile $CatalogJson
    } catch {
        $CatalogJson = ''
    }
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$CatalogBase/installer_tui.pyz" -OutFile $InstallerTuiPyz
    } catch {
        $InstallerTuiPyz = ''
    }
}

# ── version resolver (single source of truth) ──────────────────────────────
#
# Version / RC-tag parsing + PEP 440 ordering live in ONE place:
# app/upgrade/channel.py. PowerShell can't import it, so we run that exact
# file standalone via ``python <channel.py> resolve|validate``. This
# replaces the old in-script PowerShell version-object sort, which couldn't
# order the per-PR dev form ``X.Y.ZrcN.devM`` (it fell through to 0.0.0.0
# and the newest *legacy* rc wheel wrongly won).
$script:ChannelPyPath = $null
function Get-CremindResolver {
    if ($script:ChannelPyPath) { return $script:ChannelPyPath }
    # Prefer the checkout's own channel.py when running from a clone (dev,
    # or ``-Channel test`` from source): it's the exact version being tested
    # and avoids fetching a possibly-older copy from the default ref.
    if ($RepoRoot) {
        $local = Join-Path $RepoRoot 'app\upgrade\channel.py'
        if (Test-Path $local) {
            $script:ChannelPyPath = $local
            return $local
        }
    }
    $src = if ($env:CREMIND_CHANNEL_PY) {
        $env:CREMIND_CHANNEL_PY
    } else {
        'https://raw.githubusercontent.com/cremind-ai/cremind/main/app/upgrade/channel.py'
    }
    $dest = Join-Path $CremindInstallDir 'channel.py'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $src -OutFile $dest
    } catch {
        Write-Err2 "Failed to fetch the version resolver from $src : $_"
        exit 1
    }
    $script:ChannelPyPath = $dest
    return $dest
}

# The resolver only needs a basic Python 3 (stdlib argparse/re), not the
# strict 3.13+ that $Python (Find-SystemPython) requires — and in docker
# mode $Python is often empty because the install runs inside the image.
# Accept any python on PATH; '' when none is found.
function Get-ResolverPython {
    if ($Python) { return $Python }
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return ''
}

# Pick the newest matching cremind wheel from a Test/PyPI simple-index page.
# Returns the wheel URL (-Emit url) or PEP 440 version (-Emit version); ''
# when nothing matches. Ordering + channel/line filtering are done by the
# canonical resolver, not here.
function Resolve-CremindWheel {
    param(
        [Parameter(Mandatory)][string] $IndexBody,
        [Parameter(Mandatory)][ValidateSet('production', 'test')][string] $ResolveChannel,
        [string] $Line,
        [ValidateSet('url', 'version')][string] $Emit = 'url'
    )
    $py = Get-ResolverPython
    if (-not $py) {
        Write-Err2 "python3 is required to resolve the latest $ResolveChannel version. Install Python 3 and re-run."
        exit 1
    }
    $resolver = Get-CremindResolver
    $resolveArgs = @($resolver, 'resolve', '--channel', $ResolveChannel, '--print', $Emit)
    if ($Line) { $resolveArgs += @('--line', $Line) }
    $out = $IndexBody | & $py @resolveArgs
    if ($LASTEXITCODE -ne 0) { return '' }
    if (-not $out) { return '' }
    return ($out | Select-Object -First 1).Trim()
}

# ── container alias ───────────────────────────────────────────────────────
#
# ``container`` was a separate deployment in earlier installs; it's now
# a narrow case of ``custom`` (listen on 0.0.0.0, URLs at localhost).
# Accept the old name for one release as an alias so existing scripts
# don't break, and warn so users migrate to --Deployment custom.
if ($Deployment -eq 'container') {
    Write-Warn2 "-Deployment container is deprecated; using -Deployment custom with container defaults."
    $Deployment = 'custom'
    if (-not $ListenHost)   { $ListenHost   = '0.0.0.0' }
    if (-not $PublicUrl)    { $PublicUrl    = 'http://localhost:1515' }
    if (-not $WizardPreset) { $WizardPreset = 'local' }
}

# ── banner ────────────────────────────────────────────────────────────────

# Single-quoted here-string: every char ($, backtick, apostrophe) is literal.
$Logo = @'
                            xrjjjjjjjjjrrxc
                      xrjfffjrx        jxjjfffjx
                   rjffj1                     jjfjr:
                rjfjj         *W8%%%%%%8&*       /ffjx
              rffj        M%%%BBBBBB@@@B%%%BB&      jjjj1
            jjjr       o%%%BB@@@@@@@@@@@BBB%%%B@8     rjjr
           jfx       C8%%B@@@@@@@@@@@BBBBBB%%%%%B@&     cjjr
         jfj      LJOB%B@@@@@@@@@BBBB%%%%%%88888%8BBQ     rjj
        jjx     xUYpBBB@@@@a|lllI;:,"^',tb&88888888%8C     ujj\
      :rjn     UYXq%%BB@v;;;:::,""^`'..      \M&&&&8%&YU    njjx
      jjj    cYXzZB%BB/,,""""^^``''.            CWW&8BmYU    xjr1
   $8%WU    YXzzcW%B8:^^`````'''..               ;*MW&*czY    :jj\
   &8&8%%M UzccvQ%%%>'''';UQ0Q].          ...     .*MWBXczXO   xjn
   &8&&8%B8cvvvu*%%k ...fX:```xu'      .-QZZ0U^    f#M%QvccX    rjn
   r8&W&&8%Wnuunp%8k                   11....iX;   ?*#8muvvvc   rjr
   fL&WWW&88%vxxn&%8>                              ]o*%Jnnunu    rj
  xfx&WMMWW&8%mrju888Y                             Ja*Wrxxxxn    ujn
  jjOM&MMMMMW&8*fff0WWWWv                        ;dka%zrrrrrrn   0jr
  rj  MW###MMWW&8ntt/j*M######Xl             ILbbbbo#ffjjjjjjr    jf
  fj   MM***##MMM8\\\(|00Um#*aaaaooooaahhhkkkbbbk#Q|//tttttfff    jj
  jj    M#*o****##ZoMbqmOLJYzuxjftjYwkaooabZYzYLmoQ|\\///////t    jj
  rfz    0#ooooooob@%%BB*bdpwmO0LCUYXzcccczzXUJLW%BQ||\\\\\||t   Ojj
  nfr    1)ooaaahhq@%%%%BBBBBBB@@BBBBBBBBBB%&WM*oaWhBM|((((((Y   nfn
   jjJ   )11roaahhx@%%%BBBBBB%%888&&&WWWMM##*oohdpZMW8Bn))))(    jj
   rjr    (1111111|@%%%%BBB%888&&&WWWMM##X0Uhahbw0hoMW8%f111|   rjr
   jjj     11111111B%%BBBB%%88&&&WWMM##*L0wZbhkwZQka#M&8B(1(    rfn
    zfr    |{{1{{{{uB%%%%%88&&&WWMMM##****M*akpmmQbho#W&%J1    jjn
     rfx    ){{{{{{1W%%%%88&&&WWWM###**oooahhdwwqLQha*M&88    nfr
      jjr    |{{{{{{Z%8%%8&&WWWMM###**oooaahkpqph){hho#W&%d  rfj
       rfr    :1{{{{}#%88&&WWWMM##**oooaaahkdqphY{[qha*M&%* jjj:
        rjr:    1{{{{f%88&WWMMM##***oaaaaahbddh#{{}XahoM&%orfr
         rfjx     {{{}f%8&WMM##***ooaaahhhkddao1{{{c*ha*W%rfr
           ffj\     ){}Z8&WM##***oaaaahhhkbbaM{{1(  aah*WMtx
            fffr       {0&WM#**ooahhhhhkkbkoW)1     a#hoW&
              fjfj\      o&W#*ooaahhhhhkkk*#b       fu*#&a
                :fffr     hWW#oaahhhhkkkoMa      nffffZh$
                   \jffjj   kMMoahhhhho#k    ujffjj
                       rjffffjjJoM#M*d rxjfjfjj1
                            Onjjjffffjjjr\
'@
Write-Host $Logo
Write-Host ""
Write-Host "Cremind installer" -ForegroundColor White
Write-Host "Logs: $LogFile" -ForegroundColor DarkGray
Write-Host ""

# Channel stamp: visible ONLY when non-production. End users never see it;
# CI / maintainers / devs do.
if ($Channel -ne 'production') {
    if ($Channel -eq 'dev') {
        Write-Host "==> channel: dev (source: $RepoRoot)" -ForegroundColor DarkGray
    } else {
        Write-Host "==> channel: $Channel" -ForegroundColor DarkGray
    }
    Write-Host ""
}

# ── detection ─────────────────────────────────────────────────────────────

Write-Step "Environment"

$Arch = $env:PROCESSOR_ARCHITECTURE
Write-Ok "OS:   Windows ($Arch)"

# Locate a Python 3.13+ interpreter. The Windows launcher ``py -3.13`` is
# the most reliable lookup; fall back to whatever ``python`` resolves to.
function Find-SystemPython {
    $candidates = @(
        @{ Cmd = 'py'; Args = @('-3.13','-c','import sys; print("%d.%d" % sys.version_info[:2])') },
        @{ Cmd = 'python'; Args = @('-c','import sys; print("%d.%d" % sys.version_info[:2])') }
    )
    foreach ($c in $candidates) {
        if (Get-Command $c.Cmd -ErrorAction SilentlyContinue) {
            try {
                $ver = & $c.Cmd @($c.Args) 2>$null
                if ($ver -match '^3\.(1[3-9]|[2-9]\d)$') {
                    if ($c.Cmd -eq 'py') {
                        return (& py -3.13 -c "import sys; print(sys.executable)").Trim()
                    } else {
                        return (Get-Command python).Source
                    }
                }
            } catch {
                # Fall through to the next candidate.
            }
        }
    }
    return $null
}
$Python = Find-SystemPython

if ($Python) {
    Write-Ok "Python: $(& $Python --version) at $Python"
} else {
    Write-Info "Python: 3.13+ not found (will auto-install in native mode)"
}

$HasDocker = $false
if (Get-Command docker -ErrorAction SilentlyContinue) {
    try {
        & docker info 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $HasDocker = $true }
    } catch {}
}
if ($HasDocker) {
    Write-Ok "Docker: detected (recommended in a future release)"
} else {
    Write-Info "Docker: not detected (or not running)"
}

# ── TUI bootstrap ─────────────────────────────────────────────────────────
#
# Mirror of the install.sh bootstrap: launch the prompt_toolkit TUI for a
# keyboard-driven walk through channel / version / deployment / mode in
# one screen flow. Any failure falls through to the legacy numbered
# prompts below.
function Invoke-InstallerTuiBootstrap {
    if ($Unattended) { return }
    if ($NoTui)      { return }
    if (-not $InstallerTuiPyz -or -not (Test-Path -LiteralPath $InstallerTuiPyz)) { return }
    if (-not $CatalogJson -or -not (Test-Path -LiteralPath $CatalogJson)) { return }
    if (-not [Environment]::UserInteractive) { return }

    # Decide how to launch the TUI:
    #   1. Dev channel from a checkout — reuse the project's .venv. It
    #      already has prompt_toolkit + rich installed (core deps in
    #      pyproject.toml), so we skip uv entirely.
    #   2. Otherwise — install a private uv into $BinDir and let it
    #      manage Python + the two TUI deps in an ephemeral env.
    $devPython = ''
    if ($Channel -eq 'dev' -and $RepoRoot) {
        $candidate = Join-Path $RepoRoot '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $candidate) { $devPython = $candidate }
    }

    if (-not $devPython) {
        if (-not (Test-Path -LiteralPath $UvExe)) {
            if (-not (Test-Path -LiteralPath $BinDir)) {
                New-Item -ItemType Directory -Path $BinDir | Out-Null
            }
            $env:UV_INSTALL_DIR       = $BinDir
            $env:UV_UNMANAGED_INSTALL = $BinDir
            $env:INSTALLER_NO_MODIFY_PATH = '1'
            try {
                & powershell.exe -NoProfile -ExecutionPolicy Bypass `
                    -Command "irm 'https://astral.sh/uv/install.ps1' | iex" `
                    *>> $LogFile
                if ($LASTEXITCODE -ne 0) { throw "uv installer exited $LASTEXITCODE" }
            } catch {
                Write-Warn2 "Could not download uv for TUI; falling back to text prompts."
                return
            }
            if (-not (Test-Path -LiteralPath $UvExe)) {
                Write-Warn2 "uv installer ran but $UvExe is missing; falling back to text prompts."
                return
            }
        }
    }

    $tuiOut = Join-Path $CremindInstallDir '.tui-out'
    # PowerShell drops empty strings from arrays when splatting to native
    # exes, which would turn ``--deployment ''`` into a bare ``--deployment``
    # and argparse would grab the next flag as its value. Only emit a flag
    # when its value is non-empty — argparse's ``default=""`` covers the
    # missing-flag case.
    $tuiArgs = [System.Collections.Generic.List[string]]::new()
    $tuiArgs.Add('--output'); $tuiArgs.Add($tuiOut)
    $tuiArgs.Add('--catalog'); $tuiArgs.Add($CatalogJson)
    $tuiArgs.Add('--in-container'); $tuiArgs.Add('0')
    $tuiArgs.Add('--has-docker'); $tuiArgs.Add(([int][bool]$HasDocker).ToString())
    if ($Channel)         { $tuiArgs.Add('--channel');          $tuiArgs.Add($Channel) }
    if ($Deployment)      { $tuiArgs.Add('--deployment');       $tuiArgs.Add($Deployment) }
    if ($Mode)            { $tuiArgs.Add('--mode');             $tuiArgs.Add($Mode) }
    if ($DesktopUi)       { $tuiArgs.Add('--desktop');          $tuiArgs.Add($DesktopUi) }
    if ($Version)         { $tuiArgs.Add('--version');          $tuiArgs.Add($Version) }
    if ($AppHost)         { $tuiArgs.Add('--host');             $tuiArgs.Add($AppHost) }
    if ($ListenHost)      { $tuiArgs.Add('--listen-host');      $tuiArgs.Add($ListenHost) }
    if ($PublicUrl)       { $tuiArgs.Add('--public-url');       $tuiArgs.Add($PublicUrl) }
    if ($AllowedOrigins)  { $tuiArgs.Add('--allowed-origins');  $tuiArgs.Add($AllowedOrigins) }
    if ($WizardPreset)    { $tuiArgs.Add('--wizard-preset');    $tuiArgs.Add($WizardPreset) }
    if ($ElectronVersion) { $tuiArgs.Add('--electron-version'); $tuiArgs.Add($ElectronVersion) }

    Write-Info "Launching installer TUI (Enter continues, Esc cancels, Ctrl+C force-quits)"
    if ($devPython) {
        & $devPython $InstallerTuiPyz @tuiArgs
    } else {
        & $UvExe run --quiet --python 3.13 `
            --with prompt_toolkit --with rich `
            python $InstallerTuiPyz @tuiArgs
    }
    $rc = $LASTEXITCODE
    if ($rc -eq 1) {
        Write-Err2 "Installer cancelled."
        exit 1
    }
    if ($rc -ne 0) {
        Write-Warn2 "TUI exited with code $rc; falling back to text prompts."
        return
    }

    if (Test-Path -LiteralPath $tuiOut) {
        Get-Content -LiteralPath $tuiOut | ForEach-Object {
            if ($_ -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                $k = $Matches[1]
                $v = $Matches[2]
                # Strip surrounding single quotes the bash quoter may have added.
                if ($v -match "^'(.*)'$") { $v = ($Matches[1] -replace "'\\\\''", "'") }
                switch ($k) {
                    'CHANNEL'              { if (-not $Channel)        { Set-Variable -Scope Script Channel $v } }
                    'VERSION_SPEC'         { if (-not $Version)        { Set-Variable -Scope Script Version $v } }
                    'DEPLOYMENT'           { if (-not $Deployment)     { Set-Variable -Scope Script Deployment $v } }
                    'APP_HOST'             { if (-not $AppHost)        { Set-Variable -Scope Script AppHost $v } }
                    'MODE'                 { if (-not $Mode)           { Set-Variable -Scope Script Mode $v } }
                    'DESKTOP_UI'           { if (-not $DesktopUi)      { Set-Variable -Scope Script DesktopUi $v } }
                    'CUSTOM_listen_host'   { if (-not $ListenHost)     { Set-Variable -Scope Script ListenHost $v } }
                    'CUSTOM_public_url'    { if (-not $PublicUrl)      { Set-Variable -Scope Script PublicUrl $v } }
                    'CUSTOM_allowed_origins' { if (-not $AllowedOrigins) { Set-Variable -Scope Script AllowedOrigins $v } }
                    'CUSTOM_wizard_preset' { if (-not $WizardPreset)   { Set-Variable -Scope Script WizardPreset $v } }
                }
            }
        }
        Remove-Item -LiteralPath $tuiOut -Force -ErrorAction SilentlyContinue
        Write-Ok "TUI selections applied."
    }
}

Invoke-InstallerTuiBootstrap

# ── deployment type ───────────────────────────────────────────────────────

Write-Step "Deployment"

if (-not $Deployment) {
    Write-Host "How will you run Cremind?"
    $idx = 0
    foreach ($id in $script:DeploymentIds) {
        $idx++
        $entry = $script:Deployments[$id]
        Write-Host ("  {0}) {1,-20} - {2}" -f $idx, $entry.Label, $entry.Description)
    }
    while (-not $Deployment) {
        $choice = Read-Host "Choice [1]"
        if (-not $choice) { $choice = '1' }
        # Resolve numeric or name choice against $script:DeploymentIds.
        $i = 0
        foreach ($id in $script:DeploymentIds) {
            $i++
            if ($choice -eq "$i" -or $choice -eq $id) {
                $Deployment = $id
                break
            }
        }
        if (-not $Deployment) {
            Write-Warn2 "Pick a number 1-$($script:DeploymentIds.Count) or a deployment id."
        }
    }
}
if (-not ($script:DeploymentIds -contains $Deployment)) {
    Write-Err2 "Unknown deployment: $Deployment (must be one of: $($script:DeploymentIds -join ', '))"
    exit 2
}
Write-Ok "Deployment: $Deployment"

if ($Deployment -eq 'server' -and -not $AppHost) {
    while (-not $AppHost) {
        $AppHost = Read-Host "Public IP or domain (e.g. 100.120.175.90 or cremind.example.com)"
        if (-not $AppHost) {
            Write-Warn2 "Required for server deployment."
        } elseif ($AppHost -notmatch '^[A-Za-z0-9\.\:\-]+$') {
            Write-Warn2 "Invalid characters; use letters, digits, dot, colon, hyphen."
            $AppHost = ''
        }
    }
}
if ($AppHost) { Write-Ok "Host: $AppHost" }

# ── custom-deployment fields ─────────────────────────────────────────────

# When the user picks ``custom`` walk the advanced-field array from the
# catalog and prompt for each one with its plain-English question +
# hint. Already-set values (from -ListenHost / -PublicUrl / etc., or
# from the deprecated container-deployment alias above) skip the
# prompt. In -Unattended the catalog default is used silently.
$CustomValues = @{}
if ($Deployment -eq 'custom') {
    Write-Host ""
    Write-Info "Custom deployment - answer a few questions about how Cremind should be reached."
    $paramFor = @{
        'listen_host'     = 'ListenHost'
        'public_url'      = 'PublicUrl'
        'allowed_origins' = 'AllowedOrigins'
        'wizard_preset'   = 'WizardPreset'
    }
    foreach ($key in $script:CustomFieldIds) {
        $field = $script:CustomFields[$key]
        $paramName = $paramFor[$key]
        $current = (Get-Variable -Name $paramName -Scope Script -ErrorAction SilentlyContinue).Value
        if (-not $current) {
            $current = (Get-Variable -Name $paramName -ErrorAction SilentlyContinue).Value
        }
        if ($current) {
            $CustomValues[$key] = $current
            Write-Ok "$key`: $current"
            continue
        }
        if ($Unattended) {
            $CustomValues[$key] = $field.Default
            Write-Ok "$key`: $($field.Default) (default)"
            continue
        }
        Write-Host ""
        Write-Host $field.Prompt -ForegroundColor White
        Write-Host $field.Hint   -ForegroundColor DarkGray
        if ($field.Choices.Count -gt 0) {
            Write-Host ("Choices: " + ($field.Choices -join ', ')) -ForegroundColor DarkGray
        }
        while ($true) {
            $answer = Read-Host "  [$($field.Default)]"
            if (-not $answer) { $answer = $field.Default }
            if ($field.Choices.Count -gt 0 -and -not ($field.Choices -contains $answer)) {
                Write-Warn2 "Pick one of: $($field.Choices -join ', ')"
                continue
            }
            $CustomValues[$key] = $answer
            break
        }
    }
    # Sensible fallback for allowed_origins: derive from the public URL
    # plus the localhost variants the SPA listener serves on. Saves
    # operators from constructing a CORS list by hand for the typical
    # browse-via-localhost flow.
    if (-not $CustomValues['allowed_origins']) {
        $CustomValues['allowed_origins'] = "$($CustomValues['public_url']),http://localhost:1515,http://127.0.0.1:1515"
    }
}

# ── mode (docker vs native) ──────────────────────────────────────────────

# Default: docker if available, native otherwise. Labels and descriptions
# come from the catalog ($script:ModeIds / $script:Modes) so the install
# scripts and the Setup Wizard show identical text.
if (-not $Mode) {
    if ($HasDocker) {
        if ($Unattended) {
            $Mode = 'docker'
        } else {
            Write-Host ""
            Write-Host "How do you want to run Cremind?" -ForegroundColor White
            $idx = 0
            foreach ($id in $script:ModeIds) {
                $idx++
                $entry = $script:Modes[$id]
                Write-Host ("  {0}) {1,-8} - {2}" -f $idx, $entry.Label, $entry.Description)
                if ($entry.Hint) {
                    Write-Host ("               $($entry.Hint)") -ForegroundColor DarkGray
                }
            }
            while (-not $Mode) {
                $choice = Read-Host "Choice [1]"
                if (-not $choice) { $choice = '1' }
                $i = 0
                foreach ($id in $script:ModeIds) {
                    $i++
                    if ($choice -eq "$i" -or $choice -eq $id) {
                        $Mode = $id
                        break
                    }
                }
                if (-not $Mode) {
                    Write-Warn2 "Pick a number 1-$($script:ModeIds.Count) or a mode id."
                }
            }
        }
    } else {
        $Mode = 'native'
    }
}
Write-Ok "Mode: $Mode"

if ($Mode -eq 'docker' -and -not $HasDocker) {
    Write-Err2 "Docker mode requested but Docker is not available."
    exit 1
}

# ── desktop UI (docker mode only) ─────────────────────────────────────────
#
# Docker installs choose an image flavor: the desktop image
# (cremind/cremind-desktop, XFCE + VNC) or the basic headless image
# (cremind/cremind). Default is desktop everywhere, including -Unattended.
# A re-install reads the previous choice from the existing docker\.env
# (CREMIND_IMAGE) so an unattended re-run preserves the flavor.
if ($Mode -eq 'docker' -and -not $DesktopUi) {
    $desktopDefault = '1'
    $prevEnv = Join-Path (Join-Path $CremindInstallDir 'docker') '.env'
    if (Test-Path -LiteralPath $prevEnv) {
        $prevImageLine = Get-Content -LiteralPath $prevEnv | Where-Object { $_ -like 'CREMIND_IMAGE=*' } | Select-Object -First 1
        if ($prevImageLine -and ($prevImageLine -replace '^CREMIND_IMAGE=', '').Trim() -eq 'cremind/cremind') {
            $desktopDefault = '0'
        }
    }

    if ($Unattended) {
        $DesktopUi = $desktopDefault
    } else {
        Write-Host ""
        Write-Host $script:DockerDesktop.Prompt -ForegroundColor White
        if ($script:DockerDesktop.Hint) {
            Write-Host ("  $($script:DockerDesktop.Hint)") -ForegroundColor DarkGray
        }
        $ynHint = if ($desktopDefault -eq '1') { '[Y/n]' } else { '[y/N]' }
        while (-not $DesktopUi) {
            $ans = Read-Host "Install the VNC Desktop UI? $ynHint"
            if (-not $ans) {
                $DesktopUi = $desktopDefault
            } elseif ($ans -match '^(y|yes)$') {
                $DesktopUi = '1'
            } elseif ($ans -match '^(n|no)$') {
                $DesktopUi = '0'
            } else {
                Write-Warn2 "Please answer yes or no."
            }
        }
    }
}
if ($Mode -eq 'docker') {
    if ($DesktopUi -eq '0') {
        Write-Ok "Desktop UI: no (basic headless image)"
    } else {
        Write-Ok "Desktop UI: yes (VNC desktop image)"
    }
}

# ── docker install ────────────────────────────────────────────────────────

# Random secret generator. ``RNGCryptoServiceProvider`` would be the
# pedantically-correct choice, but ``Get-Random`` with a wide enough
# alphabet is sufficient for non-crypto-grade install-time secrets that
# the user controls and can rotate.
function New-Secret {
    -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
}

function Remove-DesktopOnly {
    # Strip the ``# >>> desktop-only >>>`` … ``# <<< desktop-only <<<`` marker
    # blocks (noVNC/VNC ports + VNC env) from a rendered template so a basic
    # (headless) install carries no dead VNC config. Operates on the single
    # multi-line string Get-TemplateContent returns; a line filter (not a
    # multi-line -replace) sidesteps regex/CRLF escaping pitfalls.
    param([string] $Text)
    $inBlock = $false
    $out = [System.Collections.Generic.List[string]]::new()
    foreach ($line in ($Text -split '\r?\n')) {
        if ($line -match '^\s*#\s*>>> desktop-only >>>') { $inBlock = $true; continue }
        if ($line -match '^\s*#\s*<<< desktop-only <<<') { $inBlock = $false; continue }
        if (-not $inBlock) { $out.Add($line) }
    }
    return ($out -join "`n")
}

function Resolve-CremindVersion {
    # Pick the CREMIND_VERSION tag to bake into the rendered docker .env.
    # Channel-driven:
    #   production → -Version → -ElectronVersion → latest stable from PyPI JSON
    #   test       → -Version → highest rcN matching -ElectronVersion line
    #                from Test PyPI's simple index
    #   dev        → literal 'dev'; the docker-compose.override.yml
    #                rebuilds locally and re-tags so the value is only
    #                a cosmetic image label
    # Hard-fails on network errors for prod / test — the previous
    # 'main' fallback silently mis-tagged the image and masked the
    # failure behind ``docker compose up --build``.

    # Explicit -Version short-circuits all lookups. Already validated
    # against the channel shape (and Electron line, when applicable) by
    # the top-level guards.
    if ($Version -and $Channel -ne 'dev') {
        Write-Ok "Using cremind==$Version (-Version)"
        return $Version
    }

    switch ($Channel) {
        'production' {
            # When the Electron app drives the install, pin to its
            # build version. Electron + cremind wheel are released
            # together under the same tag; drifting the backend off
            # the Electron line silently desyncs tray-menu / taskbar
            # features (which live in the Electron main process).
            if ($ElectronVersion) {
                Write-Ok "Pinning cremind==$ElectronVersion (Electron build version)"
                return $ElectronVersion
            }
            Write-Info "Resolving latest cremind version from PyPI"
            try {
                $body = (Invoke-WebRequest -UseBasicParsing -Uri 'https://pypi.org/pypi/cremind/json').Content
                $v = ($body | ConvertFrom-Json).info.version
            } catch {
                Write-Err2 "Failed to resolve cremind version from https://pypi.org/pypi/cremind/json : $_"
                exit 1
            }
            if (-not $v) {
                Write-Err2 "PyPI JSON returned no version for cremind"
                exit 1
            }
            Write-Ok "Resolved cremind==$v from PyPI"
            return $v.Trim()
        }
        'test' {
            Write-Info "Resolving latest cremind pre-release from Test PyPI"
            try {
                $indexBody = (Invoke-WebRequest -UseBasicParsing -Uri 'https://test.pypi.org/simple/cremind/').Content
            } catch {
                Write-Err2 "Failed to fetch https://test.pypi.org/simple/cremind/ : $_"
                exit 1
            }
            # Pick the newest matching wheel via the canonical resolver
            # (PEP 440 ordering incl. the rcN.devM dev form). When
            # -ElectronVersion is set, only that dev line is considered;
            # cross-line picks would silently desync UI from backend.
            $v = Resolve-CremindWheel -IndexBody $indexBody -ResolveChannel 'test' -Line $ElectronVersion -Emit 'version'
            if (-not $v) {
                if ($ElectronVersion) {
                    Write-Err2 "No cremind wheel matching ${ElectronVersion}rcN[.devM] found at https://test.pypi.org/simple/cremind/ — has a test prerelease been published for this Electron build?"
                } else {
                    Write-Err2 "No cremind wheel found at https://test.pypi.org/simple/cremind/"
                }
                exit 1
            }
            Write-Ok "Resolved cremind==$v from Test PyPI"
            return $v
        }
        'dev' {
            # Dev mode rebuilds via docker-compose.override.yml; the tag is
            # only the local image label.
            return 'dev'
        }
    }
    Write-Err2 "Unknown channel: $Channel"
    exit 1
}

if ($Mode -eq 'docker') {
    Write-Step "Docker install"

    $DockerDir = Join-Path $CremindInstallDir 'docker'
    if (-not (Test-Path $DockerDir)) { New-Item -ItemType Directory -Path $DockerDir | Out-Null }

    $ComposeFile = Join-Path $DockerDir 'docker-compose.yml'
    $EnvDocker   = Join-Path $DockerDir '.env'

    # Sidecar services (postgres / qdrant / chroma) are no longer
    # provisioned here — the Setup Wizard activates each one on demand
    # via its own per-service deployment-mode picker.
    #
    # Bundle regeneration is unconditional. Channel-dependent fields
    # (CREMIND_VERSION, CREMIND_UPGRADE_CHANNEL, CREMIND_PIP_INDEX_URL) all
    # drift if the .env is reused across runs, and a previous-channel
    # docker-compose.override.yml silently keeps a stale ``build:``
    # context alive on dev→test/prod switches. Regenerating from
    # templates on every run is the only way to keep state honest;
    # VNC_PASSWORD is the only true secret here and the installer
    # surfaces it at the end of the run, so re-rolling it is cheap.
    if (Test-Path $ComposeFile) {
        Write-Info "Regenerating $DockerDir config (templates re-render every run)"
    }

    # Image flavor from the desktop-UI choice (default desktop). $BuildTarget
    # selects the Dockerfile stage for the dev-channel local build; $ImageRepo
    # is the Docker Hub repo written into .env as CREMIND_IMAGE.
    if ($DesktopUi -eq '0') {
        $ImageRepo   = 'cremind/cremind'
        $BuildTarget = 'basic'
        $VncPwd      = ''
    } else {
        $ImageRepo   = 'cremind/cremind-desktop'
        $BuildTarget = 'desktop'
        $VncPwd      = New-Secret
    }
    $CremindVer   = Resolve-CremindVersion

    switch ($Deployment) {
        'local' {
            $DockerAppUrl    = 'http://localhost:1515'
            $DockerCors      = 'http://localhost:1515,http://127.0.0.1:1515'
            $DockerWizardEnv = 'local'
        }
        'server' {
            $DockerAppUrl    = "http://${AppHost}:1515"
            $DockerCors      = "http://${AppHost}:1515,http://localhost:1515"
            $DockerWizardEnv = 'server'
        }
        'custom' {
            $DockerAppUrl    = $CustomValues['public_url']
            $DockerCors      = $CustomValues['allowed_origins']
            $DockerWizardEnv = $CustomValues['wizard_preset']
        }
    }

    # Dev channel: open CORS so ``npm run dev`` (Vite at localhost:5173)
    # and other ad-hoc dev origins can hit the API without preflight
    # failures. Production and test installs keep the locked-down list.
    if ($Channel -eq 'dev') {
        $DockerCors = '*'
    }

    Write-Info "Fetching docker-compose template"
    # The cremind container's /root/.cremind lives in the ``cremind-data``
    # named Docker volume — isolated from the host. No path substitution
    # needed at render time; the volume is declared in the template's
    # ``volumes:`` block.
    $composeContent = Get-TemplateContent 'docker-compose.yml.tmpl'
    if ($DesktopUi -eq '0') { $composeContent = Remove-DesktopOnly $composeContent }
    $composeContent | Set-Content -Path $ComposeFile -Encoding utf8

    Write-Info "Writing $EnvDocker (secrets, do not commit)"
    $rendered = (Get-TemplateContent 'docker.env.tmpl') `
        -replace '__CREMIND_IMAGE__',  $ImageRepo `
        -replace '__CREMIND_VERSION__', $CremindVer `
        -replace '__APP_URL__',        $DockerAppUrl `
        -replace '__CORS_ALLOWED_ORIGINS__', $DockerCors `
        -replace '__SETUP_WIZARD_ENV__', $DockerWizardEnv `
        -replace '__INSTALL_MODE__',   $Mode `
        -replace '__VNC_PASSWORD__',   $VncPwd
    if ($DesktopUi -eq '0') { $rendered = Remove-DesktopOnly $rendered }
    $rendered | Set-Content -Path $EnvDocker -Encoding utf8

    # Test channel: forward Test PyPI indices into the Dockerfile build
    # via the compose .env. Prod leaves both unset (Dockerfile treats
    # empty as default PyPI). Dev uses ``-e /src`` via the override
    # file below, so pip index overrides don't apply.
    if ($Channel -eq 'test') {
        Add-Content -Path $EnvDocker -Value 'CREMIND_PIP_INDEX_URL=https://test.pypi.org/simple/' -Encoding utf8
        Add-Content -Path $EnvDocker -Value 'CREMIND_PIP_EXTRA_INDEX_URL=https://pypi.org/simple/' -Encoding utf8
    }

    # Stamp the channel into docker.env so the running container's
    # upgrader and feature installer see it. Without this, dev images
    # fall back to ``production`` semantics at runtime — which makes
    # ``pip_spec()`` pin to ``cremind==<version>`` and look up PyPI
    # for a release that may not be published yet during release prep.
    Add-Content -Path $EnvDocker -Value "CREMIND_UPGRADE_CHANNEL=$Channel" -Encoding utf8

    # Dev channel: emit a docker-compose.override.yml that points the
    # build context at the local checkout, switches the pip install
    # to ``-e /src``, and bind-mounts the checkout for runtime
    # imports. Compose auto-merges this when running from $DockerDir.
    # Backslashes are converted to forward slashes — Docker Desktop
    # accepts either, and forward slashes keep the YAML readable.
    #
    # Non-dev channels must remove any previously-written override:
    # a stale dev override silently re-adds a local ``build:`` context
    # that wins over the pulled image.
    $OverrideFile = Join-Path $DockerDir 'docker-compose.override.yml'
    if ($Channel -eq 'dev') {
        $RepoRootCompose = $RepoRoot -replace '\\', '/'
        Write-Info "Writing $OverrideFile (bind-mounts $RepoRoot at /src)"
        $overrideRendered = (Get-TemplateContent 'docker-compose.override.yml.tmpl') `
            -replace '__REPO_ROOT__', $RepoRootCompose `
            -replace '__CREMIND_BUILD_TARGET__', $BuildTarget
        $overrideRendered | Set-Content -Path $OverrideFile -Encoding utf8
    } elseif (Test-Path $OverrideFile) {
        Write-Info "Removing stale docker-compose.override.yml (dev-only)"
        Remove-Item -Path $OverrideFile -Force
    }

    Write-Ok "Wrote $ComposeFile + .env"

    # Per-channel pull / build strategy:
    #   production / test → pull the pre-built image from Docker Hub
    #                       and refuse to fall back to a local build.
    #                       The main compose template has no ``build:``
    #                       section, so a missing tag fails hard at
    #                       ``compose pull`` with a clear error rather
    #                       than silently rebuilding off the user's
    #                       checkout.
    #   dev               → the docker-compose.override.yml re-adds a
    #                       ``build:`` section pointing at the local
    #                       checkout. ``compose pull`` is best-effort
    #                       (the dev image isn't published), and
    #                       ``compose up --build`` forces a rebuild
    #                       so host-side edits land in the image.
    Push-Location $DockerDir
    try {
        if ($Channel -eq 'dev') {
            Write-Info "Pulling sidecar images (cremind image is built locally for dev)"
            Invoke-NativeLogged { & docker compose pull --ignore-pull-failures }
            if ($LASTEXITCODE -ne 0) {
                Write-Warn2 "Some images couldn't be pulled; will build locally."
            }
            Write-Info "Building cremind image and starting bundle"
            Invoke-NativeLogged { & docker compose up -d --build }
            if ($LASTEXITCODE -ne 0) {
                throw "docker compose up failed (see $LogFile)"
            }
        } else {
            Write-Info "Pulling ${ImageRepo}:$CremindVer and sidecar images from Docker Hub"
            # Docker Hub's CDN drops large layer downloads mid-transfer with
            # an EOF more often than you'd like. Docker caches completed
            # layers, so a retry resumes where the last attempt died — a
            # bounded loop turns a transient CDN hiccup into a non-event. A
            # genuinely-missing tag (manifest unknown) can't be fixed by
            # retrying, so bail out of the loop early in that case.
            $maxPullAttempts = 3
            $pulled = $false
            for ($attempt = 1; $attempt -le $maxPullAttempts; $attempt++) {
                if ($attempt -gt 1) {
                    Write-Info "Retrying image pull (attempt $attempt/$maxPullAttempts)…"
                    Start-Sleep -Seconds 5
                }
                Invoke-NativeLogged { & docker compose pull }
                if ($LASTEXITCODE -eq 0) { $pulled = $true; break }
                $recent = (Get-Content -Path $LogFile -Tail 40 -ErrorAction SilentlyContinue) -join "`n"
                if ($recent -match 'manifest unknown|not found|repository does not exist|manifest for .* not found') {
                    throw "docker compose pull failed — ${ImageRepo}:$CremindVer is not on Docker Hub. Check the version/tag, or wait for the release to finish publishing (see $LogFile)."
                }
            }
            if (-not $pulled) {
                throw @"
docker compose pull failed after $maxPullAttempts attempts.
The image is published, but layer downloads from Docker Hub's CDN kept
dropping (look for 'EOF' / 'failed to copy' in $LogFile) — a network issue,
not a missing image. Things that help:
  - Re-run this installer; Docker caches completed layers and resumes.
  - Pull directly, repeating until it finishes:
      docker pull ${ImageRepo}:$CremindVer
  - Reduce parallelism: Docker Desktop -> Settings -> Docker Engine, add
      "max-concurrent-downloads": 1
    then Apply & Restart and re-run. (Most effective when one big layer
    keeps getting cut.)
  - If you're on a VPN or corporate proxy, try again off it — those often
    truncate long CDN transfers.
"@
            }
            Write-Info "Starting bundle"
            Invoke-NativeLogged { & docker compose up -d }
            if ($LASTEXITCODE -ne 0) {
                throw "docker compose up failed (see $LogFile)"
            }
        }
    } finally {
        Pop-Location
    }

    if ($Deployment -eq 'local') {
        $HealthHost = 'localhost'
    } elseif ($Deployment -eq 'custom') {
        # http://foo.bar:1515 → foo.bar
        $HealthHost = $CustomValues['public_url']
        if ($HealthHost -match '^https?://([^:/]+)') { $HealthHost = $Matches[1] }
        if (-not $HealthHost) { $HealthHost = 'localhost' }
    } else {
        $HealthHost = $AppHost
    }
    # Docker publishes only the single public port (1515); probe it on the host.
    $HealthUrl = "http://${HealthHost}:1515/health"
    Write-Info "Waiting for backend at $HealthUrl ..."
    for ($i = 0; $i -lt 60; $i++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2 | Out-Null
            Write-Ok "Backend is up"
            break
        } catch {
            Start-Sleep -Seconds 2
        }
    }

    # Suppress the human-handoff block when the Electron app is driving —
    # it navigates to the in-window wizard via vue-router and a "Wizard
    # URL: ..." instruction would mislead the user.
    $WizardUrl = "http://${HealthHost}:1515/#/setup"
    if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron') {
        Write-Step "Setup wizard"

        Write-Host @"
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

Open this URL in your browser to continue setup:

    $WizardUrl

  Cremind     : http://${HealthHost}:1515
"@
        # Desktop image only: surface the noVNC viewer + VNC password.
        if ($DesktopUi -ne '0') {
            $NoVncUrl  = "http://${HealthHost}:6080/vnc.html"
            $StoredVnc = (Get-Content $EnvDocker | Where-Object { $_ -like 'VNC_PASSWORD=*' } | Select-Object -First 1) -replace '^VNC_PASSWORD=', ''
            Write-Host @"
  Desktop     : $NoVncUrl
  VNC password (saved to $EnvDocker):
    $StoredVnc
"@
        }
        Write-Host @"

  Stop:    cd $DockerDir; docker compose down
  Logs:    cd $DockerDir; docker compose logs -f cremind
  Restart: cd $DockerDir; docker compose restart cremind

"@

        if (-not $NoLaunch -and -not $Unattended) {
            Start-Process $WizardUrl
        }
    }

    # The Electron app's reconcileInstallStateWithDisk() detects a Docker
    # install via $DockerDir\.env (the compose env file written above),
    # so no host-side marker in $CremindSystemDir is required. Docker
    # installs leave the host's ~\.cremind untouched — runtime state
    # lives in the cremind-data named volume.

    # Consolidated post-install credentials file — see
    # app/config/credentials_file.py for the canonical Python writer
    # the Setup Wizard calls to refresh this on Postgres provisioning.
    $CredsFile = Join-Path $CremindInstallDir 'credentials.toml'
    $GeneratedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    function _Get-DockerEnvVal($Key) {
        $line = Get-Content $EnvDocker -ErrorAction SilentlyContinue | Where-Object { $_ -like "$Key=*" } | Select-Object -First 1
        if ($line) { return ($line -replace "^$Key=", '') }
        return ''
    }
    $CredsApiPort = _Get-DockerEnvVal 'API_PORT';   if (-not $CredsApiPort)   { $CredsApiPort = '1112' }
    $CredsSpaPort = _Get-DockerEnvVal 'SPA_PORT';   if (-not $CredsSpaPort)   { $CredsSpaPort = '1515' }
    $CredsNoVncPort = _Get-DockerEnvVal 'NOVNC_PORT'; if (-not $CredsNoVncPort) { $CredsNoVncPort = '6080' }
    $CredsVncPort = _Get-DockerEnvVal 'VNC_PORT';   if (-not $CredsVncPort)   { $CredsVncPort = '5900' }
    $CredsResolution = _Get-DockerEnvVal 'RESOLUTION'; if (-not $CredsResolution) { $CredsResolution = '1280x720' }
    $SpaUrl = $DockerAppUrl -replace ':\d+$', ":$CredsSpaPort"
    Write-Info "Writing $CredsFile (consolidated credentials)"
    $credsText = @"
# Cremind service credentials and connection info.
# Auto-generated by the installer and the Setup Wizard.
# Postgres credentials reflect the last successful wizard
# setup; re-running the installer does not rotate them.
# To change a value, edit the source of truth (the docker
# bundle .env or bootstrap.toml) and re-run the installer
# or the wizard.

generated_at = "$GeneratedAt"
install_mode = "docker"
cremind_version = "$CremindVer"
system_dir = "$CremindSystemDir"
install_dir = "$CremindInstallDir"

[app]
api_url = "$DockerAppUrl"
spa_url = "$SpaUrl"
api_port = $CredsApiPort
spa_port = $CredsSpaPort
cors_allowed_origins = "$DockerCors"
setup_wizard_env = "$DockerWizardEnv"
"@
    # Desktop image only: the basic image has no noVNC/VNC. Keep this in
    # sync with app/config/credentials_file.py, which gates [desktop] the
    # same way (see tests/config/test_credentials_file.py).
    if ($DesktopUi -ne '0') {
        $credsText += @"

[desktop]
novnc_url = "http://${HealthHost}:$CredsNoVncPort/vnc.html"
novnc_port = $CredsNoVncPort
vnc_port = $CredsVncPort
vnc_password = "$VncPwd"
resolution = "$CredsResolution"
"@
    }
    $credsText | Set-Content -Path $CredsFile -Encoding utf8

    if ($Channel -eq 'test') {
        Write-Ok "Done. Welcome to Cremind (test build)."
    } else {
        Write-Ok "Done. Welcome to Cremind."
    }
    exit 0
}

# ── native install ────────────────────────────────────────────────────────

# (Reaching here implies Mode = native. The Docker path exited above.)

# Native installs write everything (venv\, .env, bootstrap.toml, storage\,
# tokens\, profile dirs) under the System Dir, so create it now.
if (-not (Test-Path $CremindSystemDir)) { New-Item -ItemType Directory -Path $CremindSystemDir | Out-Null }

function Write-PythonManualHint {
    Write-Host ""
    Write-Host "Install options:"
    Write-Host "  winget install Python.Python.3.13"
    Write-Host "  https://www.python.org/downloads/"
    Write-Host ""
    Write-Host "Re-run this script after Python is on your PATH (or pass -Mode docker)."
}

# Decide whether to auto-install Python. Honors -AutoInstallPython /
# -NoAutoInstallPython / -Unattended; otherwise prompts (default Yes).
function Confirm-AutoInstallPython {
    if ($NoAutoInstallPython) {
        Write-Err2 "Python 3.13 or newer is required for native mode but was not found."
        Write-PythonManualHint
        exit 1
    }
    if ($AutoInstallPython) { return }
    Write-Host ""
    Write-Host "Cremind can install an isolated Python 3.13 just for itself"
    Write-Host "(~70 MB downloaded into $CremindSystemDir\python, no admin needed; system"
    Write-Host "Python is left untouched)."
    Write-Host ""
    while ($true) {
        $choice = Read-Host "Install isolated Python 3.13 now? [Y/n]"
        if (-not $choice) { $choice = 'y' }
        switch ($choice.ToLower()) {
            'y'   { return }
            'yes' { return }
            'n'   {
                Write-Err2 "Aborted: Python 3.13 is required for native mode."
                Write-PythonManualHint
                exit 1
            }
            'no'  {
                Write-Err2 "Aborted: Python 3.13 is required for native mode."
                Write-PythonManualHint
                exit 1
            }
            default { Write-Warn2 "Please answer y or n." }
        }
    }
}

# Download a private copy of `uv` into $CremindSystemDir\bin so we can manage
# Python and venv installs without touching system tools.
function Install-UvLocally {
    if (Test-Path $UvExe) {
        Write-Info "uv already installed at $UvExe"
        return
    }
    Write-Info "Installing uv into $BinDir"
    if (-not (Test-Path $BinDir)) {
        New-Item -ItemType Directory -Path $BinDir | Out-Null
    }
    $env:UV_INSTALL_DIR        = $BinDir
    $env:UV_UNMANAGED_INSTALL  = $BinDir
    $env:INSTALLER_NO_MODIFY_PATH = '1'
    try {
        # Run uv's official installer in a fresh powershell.exe so it gets a
        # clean environment. Inline ``Invoke-Expression`` would force the
        # upstream script through our ``Set-StrictMode -Version Latest``
        # (which faults on its uninitialised ``$LASTEXITCODE`` read) and our
        # ``$ErrorActionPreference='Stop'``. The child inherits our env
        # vars (UV_INSTALL_DIR, UV_UNMANAGED_INSTALL, INSTALLER_NO_MODIFY_PATH)
        # so the binary still lands in $BinDir.
        Invoke-NativeLogged {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://astral.sh/uv/install.ps1' | iex"
        }
        if ($LASTEXITCODE -ne 0) { throw "uv installer exited with code $LASTEXITCODE" }
    } catch {
        Write-Err2 "Failed to download uv (the Python installer)."
        Write-Host ""
        Write-Host "Possible causes: no internet, corporate TLS interception, or astral.sh"
        Write-Host "is blocked. Set HTTPS_PROXY if you're behind a proxy, or install"
        Write-Host "Python manually."
        Write-PythonManualHint
        exit 1
    }
    if (-not (Test-Path $UvExe)) {
        Write-Err2 "uv installer ran but $UvExe is missing - see $LogFile."
        exit 1
    }
    Write-Ok "Installed uv at $UvExe"
}

# Use uv to download an isolated Python 3.13 into $CremindSystemDir\python and
# return its absolute path via $script:Python. The cache and install dir
# are scoped to CremindSystemDir so removing the dir cleans everything.
function Install-PythonViaUv {
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $CremindInstallDir 'python'
    $env:UV_CACHE_DIR          = Join-Path $CremindInstallDir 'uv-cache'
    Write-Info "Downloading isolated Python 3.13 (this may take a minute)"
    Invoke-NativeLogged { & $UvExe python install 3.13 }
    if ($LASTEXITCODE -ne 0) {
        Write-Err2 "uv failed to install Python 3.13 - see $LogFile."
        exit 1
    }
    $found = (& $UvExe python find 3.13 2>$null).Trim()
    if (-not $found -or -not (Test-Path $found)) {
        Write-Err2 "Python install completed but the interpreter could not be located."
        exit 1
    }
    $script:Python = $found
    Write-Ok "Python: $(& $Python --version) at $Python (isolated)"
}

# Dev channel reuses the developer's local .venv (managed by uv) for the
# install, so we don't need a separate Python here. Skip the prompt and
# the isolated-Python bootstrap entirely.
if (-not $Python -and $Channel -ne 'dev') {
    Confirm-AutoInstallPython
    Install-UvLocally
    Install-PythonViaUv
}

# ── existing install detection ────────────────────────────────────────────

Write-Step "Install"

if ($Channel -eq 'dev') {
    # Dev channel: reuse the developer's local .venv (managed by
    # ``uv sync`` from <repo>\pyproject.toml) instead of building a
    # parallel venv at $CremindSystemDir\venv. The dev already has cremind +
    # every transitive dep installed in editable mode; reinstalling
    # them into a separate venv takes minutes for no benefit.
    if ($Reinstall) {
        Write-Warn2 "-Reinstall has no effect in dev mode (the dev .venv is shared; refusing to wipe)."
    }
    $UiBundle = Join-Path $RepoRoot 'app\static\ui'
    if (-not (Test-Path $UiBundle)) {
        Write-Warn2 "Dev: $UiBundle is empty."
        Write-Warn2 "Run scripts\build_ui.sh once so the SPA listener can start."
    }
    $VenvDir    = Join-Path $RepoRoot '.venv'
    $VenvPip    = Join-Path $VenvDir 'Scripts\pip.exe'
    $VenvCremind = Join-Path $VenvDir 'Scripts\cremind.exe'
    if (-not (Test-Path $VenvCremind)) {
        Write-Err2 "Dev mode expects $VenvCremind to exist."
        Write-Err2 "Run 'uv sync' from $RepoRoot first, then re-run this installer."
        exit 1
    }
    Write-Info "Reusing dev .venv at $VenvDir (no pip install)"
    $InstalledVersion = ''
    try { $InstalledVersion = (& $VenvCremind version 2>$null).Split(' ')[-1] } catch {}
    if (-not $InstalledVersion) { $InstalledVersion = '?' }
    Write-Ok "Using cremind $InstalledVersion from dev .venv"
} else {
    if ($Reinstall -and (Test-Path $VenvDir)) {
        Write-Info "Removing existing venv (-Reinstall): $VenvDir"
        Remove-Item -Recurse -Force $VenvDir
    }

    $VenvPip = Join-Path $VenvDir 'Scripts\pip.exe'
    $VenvCremind = Join-Path $VenvDir 'Scripts\cremind.exe'

    # Resolve the spec passed to ``pip install``. Test pins a direct
    # wheel URL (Test PyPI's simple index is polluted, see the lengthy
    # rationale in the bash installer); production uses the bare
    # package name.
    #
    # The bare ``cremind`` install is intentionally thin — it ships only
    # the core deps the server + Setup Wizard + SQLite need. Optional
    # feature groups (vector embedding, vector stores, browser, channels,
    # LLM SDKs, postgres) are installed on demand by ``app/features/``
    # when the user enables them in the wizard. The Docker desktop image
    # pre-bakes ``cremind[all]`` instead (see ``Dockerfile``).
    $InstallSpec = $null
    $InstallSourceLabel = ''
    switch ($Channel) {
        'production' {
            # Pin order: -Version → -ElectronVersion → bare ``cremind``
            # (CLI users running install.ps1 directly).
            if ($Version) {
                $InstallSpec = "cremind==$Version"
            } elseif ($ElectronVersion) {
                $InstallSpec = "cremind==$ElectronVersion"
            } else {
                $InstallSpec = 'cremind'
            }
            $InstallSourceLabel = 'PyPI'
        }
        'test' {
            # PS 5.1 has no built-in HTML parser; regex the simple-index page.
            $indexBody = (Invoke-WebRequest -UseBasicParsing -Uri 'https://test.pypi.org/simple/cremind/').Content
            if ($Version) {
                Write-Info "Locating cremind==$Version wheel"
                # Exact pinned version: literal filename match (version-agnostic,
                # so the rcN.devM form works without special-casing). Anchor on
                # the exact version segment so 0.1.9 doesn't match a 0.1.91 wheel.
                $pattern = "https://[^`"]*cremind-" + [regex]::Escape($Version) + "-py3-none-any\.whl"
                $InstallSpec = ([regex]::Matches($indexBody, $pattern) |
                    ForEach-Object { $_.Value } |
                    Select-Object -Unique -First 1)
            } else {
                if ($ElectronVersion) {
                    Write-Info "Locating latest cremind test wheel for line ${ElectronVersion}rc*"
                } else {
                    Write-Info "Locating latest cremind test wheel"
                }
                # Newest matching wheel via the canonical resolver (PEP 440
                # ordering incl. the rcN.devM dev form).
                $InstallSpec = Resolve-CremindWheel -IndexBody $indexBody -ResolveChannel 'test' -Line $ElectronVersion -Emit 'url'
            }
            if (-not $InstallSpec) {
                if ($Version) {
                    Write-Err2 "No cremind-$Version-py3-none-any.whl found at https://test.pypi.org/simple/cremind/ — has this version been published?"
                } elseif ($ElectronVersion) {
                    Write-Err2 "No cremind-${ElectronVersion}rc*-py3-none-any.whl found at https://test.pypi.org/simple/cremind/ — has a test prerelease been published for this Electron build?"
                } else {
                    Write-Err2 "No cremind wheel found at https://test.pypi.org/simple/cremind/"
                }
                exit 1
            }
            Write-Ok ("Test wheel: " + (Split-Path $InstallSpec -Leaf))
            $InstallSourceLabel = 'Test PyPI'
        }
    }

    if (Test-Path $VenvDir) {
        Write-Info "Existing install detected at $VenvDir — upgrading in place."
        Invoke-NativeLogged { & $VenvPip install --upgrade pip }
        Invoke-NativeLogged { & $VenvPip install --upgrade $InstallSpec }
    } else {
        Write-Info "Creating venv at $VenvDir"
        Invoke-NativeLogged { & $Python -m venv $VenvDir }
        Write-Info "Installing cremind from $InstallSourceLabel (this may take a few minutes)"
        Invoke-NativeLogged { & $VenvPip install --upgrade pip }
        Invoke-NativeLogged { & $VenvPip install $InstallSpec }
    }

    $InstalledVersion = ''
    try { $InstalledVersion = (& $VenvCremind version 2>$null).Split(' ')[-1] } catch {}
    if (-not $InstalledVersion) { $InstalledVersion = '?' }
    Write-Ok "Installed cremind $InstalledVersion"
}

# ── shim & PATH ───────────────────────────────────────────────────────────

# Drop a small wrapper into $BinDir so a single, stable path on PATH
# points at the venv's cremind.exe even after re-installs. In dev mode
# the target lives in $RepoRoot\.venv, not $CremindSystemDir\venv, so we emit
# the absolute path of $VenvCremind rather than a relative ``..\venv``
# walk that would dangle.
if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir | Out-Null
}
$CremindCmd = Join-Path $BinDir 'cremind.cmd'
@"
@echo off
"$VenvCremind" %*
"@ | Set-Content -Path $CremindCmd -Encoding ascii

$CremindPs1 = Join-Path $BinDir 'cremind.ps1'
@"
& '$VenvCremind' `$args
exit `$LASTEXITCODE
"@ | Set-Content -Path $CremindPs1 -Encoding utf8

Write-Ok "Wrote shim $CremindCmd (-> venv\Scripts\cremind.exe)"

function Add-CremindPathEntry {
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $target  = $BinDir
    $exists  = $false
    if ($current) {
        foreach ($entry in ($current -split ';')) {
            if ($entry.TrimEnd('\') -ieq $target.TrimEnd('\')) {
                $exists = $true
                break
            }
        }
    }
    if (-not $exists) {
        $newPath = if ($current) { "$current;$target" } else { $target }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Write-Ok "Added $target to your User PATH"
    } else {
        Write-Info "$target is already on your User PATH"
    }
    # Make the current session see the change too.
    if (-not (";$($env:Path);" -like "*;$target;*")) {
        $env:Path = "$($env:Path);$target"
    }
}

if ($NoModifyPath) {
    Write-Info "Skipping PATH modification (-NoModifyPath). Add manually:"
    Write-Host '    $current = [Environment]::GetEnvironmentVariable(''Path'', ''User'')'
    Write-Host "    [Environment]::SetEnvironmentVariable('Path', `$current + ';$BinDir', 'User')"
} else {
    Add-CremindPathEntry
}

# ── env file ──────────────────────────────────────────────────────────────

if (-not (Test-Path $EnvFile)) {
    Write-Info "Generating $EnvFile"
    switch ($Deployment) {
        'local' {
            Get-TemplateContent 'local.env' | Set-Content -Path $EnvFile -Encoding utf8
        }
        'server' {
            # __APP_HOST__ is the only placeholder; the user-provided host
            # gets substituted as-is (validated above).
            ((Get-TemplateContent 'server.env.tmpl') -replace '__APP_HOST__', $AppHost) |
                Set-Content -Path $EnvFile -Encoding utf8
        }
        'custom' {
            # All four advanced fields come from $CustomValues (filled
            # either from -ListenHost/etc. params, the catalog defaults
            # in -Unattended, or interactive prompts). They're already
            # validated for the wizard_preset choice list and otherwise
            # copy-pasted by the operator.
            $rendered = (Get-TemplateContent 'custom.env.tmpl') `
                -replace '__LISTEN_HOST__',         $CustomValues['listen_host'] `
                -replace '__PUBLIC_URL__',          $CustomValues['public_url'] `
                -replace '__CORS_ALLOWED_ORIGINS__', $CustomValues['allowed_origins'] `
                -replace '__SETUP_WIZARD_ENV__',    $CustomValues['wizard_preset']
            $rendered | Set-Content -Path $EnvFile -Encoding utf8
        }
    }
    # Stamp the install mode so the backend's mode-rule filter knows which
    # service modes to expose in the wizard. Native installs land here too;
    # docker installs stamp INSTALL_MODE into docker.env above.
    if (-not (Select-String -Path $EnvFile -Pattern '^INSTALL_MODE=' -Quiet)) {
        Add-Content -Path $EnvFile -Value "INSTALL_MODE=$Mode" -Encoding utf8
    }
    Write-Ok "Wrote $EnvFile"
} else {
    Write-Info ".env already exists — keeping it. Edit $EnvFile if you need to."
}

# Stamp the channel-specific keys into .env so the running app's upgrader
# reads them via the .env loader. Each write is idempotent (skipped if
# the key is already present) so re-runs don't accumulate duplicates.
switch ($Channel) {
    'production' {
        if (-not (Select-String -Path $EnvFile -Pattern '^CREMIND_UPGRADE_CHANNEL=' -Quiet)) {
            Add-Content -Path $EnvFile -Value "`nCREMIND_UPGRADE_CHANNEL=production" -Encoding utf8
        }
    }
    'test' {
        if (-not (Select-String -Path $EnvFile -Pattern '^CREMIND_UPGRADE_CHANNEL=' -Quiet)) {
            Add-Content -Path $EnvFile -Value "`nCREMIND_UPGRADE_CHANNEL=test" -Encoding utf8
        }
        if (-not (Select-String -Path $EnvFile -Pattern '^CREMIND_PIP_INDEX_URL=' -Quiet)) {
            Add-Content -Path $EnvFile -Value 'CREMIND_PIP_INDEX_URL=https://test.pypi.org/simple/' -Encoding utf8
        }
        if (-not (Select-String -Path $EnvFile -Pattern '^CREMIND_PIP_EXTRA_INDEX_URL=' -Quiet)) {
            Add-Content -Path $EnvFile -Value 'CREMIND_PIP_EXTRA_INDEX_URL=https://pypi.org/simple/' -Encoding utf8
        }
    }
    'dev' {
        # Stamp the channel so the feature installer's ``pip_spec()`` knows
        # to skip the ``==<version>`` pin (the editable install in /src or
        # the developer's checkout already satisfies the requirement, and
        # pinning to a release that hasn't been published to PyPI yet
        # fails at install time). ``cremind upgrade`` itself is still a
        # footgun in dev — the dev path is ``git pull`` — but the
        # upgrader's no-op behavior on dev is preferable to it silently
        # treating dev as production.
        if (-not (Select-String -Path $EnvFile -Pattern '^CREMIND_UPGRADE_CHANNEL=' -Quiet)) {
            Add-Content -Path $EnvFile -Value "`nCREMIND_UPGRADE_CHANNEL=dev" -Encoding utf8
        }
        # Replace the static template's locked-down CORS list with ``*``
        # so ``npm run dev`` (Vite at localhost:5173) and other ad-hoc dev
        # origins work without preflight failures. Idempotent: skip if
        # already wildcarded so re-runs don't churn the file. ``-Encoding
        # utf8`` is critical on both read and write — Windows PowerShell
        # 5.1's default ``Get-Content`` decodes BOM-less UTF-8 as cp1252,
        # which would corrupt non-ASCII characters (em-dashes, etc.) on
        # roundtrip.
        if (-not (Select-String -Path $EnvFile -Pattern '^CORS_ALLOWED_ORIGINS=\*$' -Quiet)) {
            $filtered = Get-Content -Path $EnvFile -Encoding utf8 | Where-Object { $_ -notmatch '^CORS_ALLOWED_ORIGINS=' }
            Set-Content -Path $EnvFile -Value $filtered -Encoding utf8
            Add-Content -Path $EnvFile -Value 'CORS_ALLOWED_ORIGINS=*' -Encoding utf8
        }
    }
}

# ── bootstrap.toml (DB selection) ─────────────────────────────────────────

# Skip the default-SQLite bootstrap.toml when the Electron app is driving
# — the Setup Wizard will write the file once the user picks a backend,
# and the backend boots in deferred-storage mode until then so no DB is
# materialised under $CremindSystemDir\storage before the user has chosen.
# Native installs (curl | sh, no Electron) get the SQLite default here,
# matching the legacy behavior; the wizard can still flip them to
# Postgres on first setup.
if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron') {
    if (-not (Test-Path $BootstrapFile)) {
        Write-Info "Generating $BootstrapFile (SQLite, the recommended default)"
        @"
# Database selection. SQLite is the recommended default for native
# installs; switch to "postgres" via the setup wizard if you want a
# multi-process or networked DB.
db_provider = "sqlite"
"@ | Set-Content -Path $BootstrapFile -Encoding utf8
        Write-Ok "Wrote $BootstrapFile"
    }
}

# ── credentials.toml (consolidated connect info) ──────────────────────────
#
# Native installs have no VNC desktop — only [app] (and optionally
# [postgres] once the wizard provisions it). The Python writer at
# app/config/credentials_file.py re-emits this file on every wizard
# service provision.
$CredsFile = Join-Path $CremindInstallDir 'credentials.toml'
$GeneratedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
function _Get-EnvFileVal($Key) {
    $line = Get-Content $EnvFile -ErrorAction SilentlyContinue | Where-Object { $_ -like "$Key=*" } | Select-Object -First 1
    if ($line) { return ($line -replace "^$Key=", '') }
    return ''
}
$CredsAppUrl = _Get-EnvFileVal 'APP_URL'
$CredsApiPort = _Get-EnvFileVal 'PORT'; if (-not $CredsApiPort) { $CredsApiPort = '1112' }
$CredsCors = _Get-EnvFileVal 'CORS_ALLOWED_ORIGINS'
$CredsWizardEnv = _Get-EnvFileVal 'SETUP_WIZARD_ENV'
$CredsSpaPort = '1515'
$SpaUrl = $CredsAppUrl -replace ':\d+$', ":$CredsSpaPort"
Write-Info "Writing $CredsFile (consolidated credentials)"
@"
# Cremind service credentials and connection info.
# Auto-generated by the installer and the Setup Wizard.
# Postgres credentials reflect the last successful wizard
# setup; re-running the installer does not rotate them.
# To change a value, edit the source of truth (the docker
# bundle .env or bootstrap.toml) and re-run the installer
# or the wizard.

generated_at = "$GeneratedAt"
install_mode = "native"
cremind_version = "$InstalledVersion"
system_dir = "$CremindSystemDir"
install_dir = "$CremindInstallDir"

[app]
api_url = "$CredsAppUrl"
spa_url = "$SpaUrl"
api_port = $CredsApiPort
spa_port = $CredsSpaPort
cors_allowed_origins = "$CredsCors"
setup_wizard_env = "$CredsWizardEnv"
"@ | Set-Content -Path $CredsFile -Encoding utf8

# ── migrate ───────────────────────────────────────────────────────────────

# Skip Alembic's ``upgrade head`` (and therefore creating the SQLite DB
# file) when the Electron app is driving — the app starts the backend
# only after the user clicks "Continue to Setup Wizard", and the backend
# is what eventually creates the DB. Keeping this here means a stray
# $CremindSystemDir\storage\cremind.db never shows up between the installer
# finishing and the user choosing to continue.
if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron') {
    Write-Info "Migrating database to current schema"
    Invoke-NativeLogged { & $VenvCremind db upgrade }
    $Revision = '?'
    try { $Revision = (& $VenvCremind db current 2>$null) } catch {}
    Write-Ok "Database at revision $Revision"
}

# ── start the server ──────────────────────────────────────────────────────

# Skip starting ``cremind serve`` when the Electron app is driving —
# the app spawns the backend itself once the user clicks Continue, and
# that's also when the SQLite DB is created.
if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron') {
    Write-Step "Starting Cremind"

    # Load .env into the process so HOST/PORT propagate to the child
    # without us needing a TOML parser. Keys that look like KEY=VALUE
    # are honored; anything else is ignored.
    $ParsedEnv = @{}
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $idx = $line.IndexOf('=')
            $k = $line.Substring(0, $idx).Trim()
            $v = $line.Substring($idx + 1).Trim()
            $ParsedEnv[$k] = $v
            Set-Item -Path "Env:$k" -Value $v
        }
    }

    $ServerHost = if ($ParsedEnv.ContainsKey('HOST')) { $ParsedEnv['HOST'] } else { '127.0.0.1' }
    $ServerPort = if ($ParsedEnv.ContainsKey('PORT')) { $ParsedEnv['PORT'] } else { '1112' }

    $running = $false
    if (Test-Path $PidFile) {
        $existingPid = Get-Content $PidFile | Select-Object -First 1
        if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
            Write-Info "Cremind is already running (pid $existingPid)."
            $running = $true
        }
    }

    # Detect a server that's already bound to the port without going
    # through this installer (typical dev case: ``uv run cremind serve``
    # in a separate terminal). Starting a second cremind would just
    # collide on the bind, so treat it as already-running and skip the
    # spawn.
    if (-not $running) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "http://${ServerHost}:${ServerPort}/health" -TimeoutSec 2 | Out-Null
            Write-Info "Cremind is already responding at http://${ServerHost}:${ServerPort} — skipping server start."
            $running = $true
        } catch { }
    }

    if (-not $running) {
        # Start-Process refuses identical stdout / stderr paths on PS
        # 5.1, so write stderr to a sibling file. They're both rotated
        # together via ``Remove-Item $CremindSystemDir\server*.log`` if the user wants a
        # clean slate.
        $ServerErrFile = Join-Path $CremindSystemDir 'server.err.log'
        $proc = Start-Process -FilePath $VenvCremind -ArgumentList 'serve' `
            -RedirectStandardOutput $ServerLogFile -RedirectStandardError $ServerErrFile `
            -WindowStyle Hidden -PassThru
        $proc.Id | Set-Content -Path $PidFile -Encoding ascii
        Write-Ok "Cremind started (pid $($proc.Id), logs: $ServerLogFile)"
    }

    # Wait briefly for the HTTP listener so the wizard URL doesn't 404.
    $healthUrl = "http://${ServerHost}:${ServerPort}/health"
    for ($i = 0; $i -lt 10; $i++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2 | Out-Null
            break
        } catch {
            Start-Sleep -Seconds 1
        }
    }
}

# ── wizard handoff ────────────────────────────────────────────────────────

# 'custom' installs honour the public URL the user provided; 'server'
# uses the host from -AppHost; 'local' is loopback. Custom URLs may
# include a path/scheme; we normalise to the single public port (1515)
# while preserving the hostname.
if ($Deployment -eq 'server') {
    $WizardUrl = "http://${AppHost}:1515/#/setup"
} elseif ($Deployment -eq 'custom') {
    $CustomHostPart = $CustomValues['public_url']
    if ($CustomHostPart -match '^https?://([^:/]+)') { $CustomHostPart = $Matches[1] } else { $CustomHostPart = 'localhost' }
    $WizardUrl = "http://${CustomHostPart}:1515/#/setup"
} else {
    $WizardUrl = 'http://localhost:1515/#/setup'
}

# Suppress the human-handoff block when the Electron app is driving —
# it navigates to the in-window wizard via vue-router and a "Wizard
# URL: ..." instruction would mislead the user.
if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron') {
    Write-Step "Setup wizard"

    Write-Host @"
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

Open this URL in your browser to continue setup:

    $WizardUrl

  Cremind:    $($WizardUrl -replace '/#/setup$', '')
  Stop:       Stop-Process -Id (Get-Content $PidFile)
  Re-open:    cremind serve   (or: & "$VenvCremind" serve)

  Tip: open a NEW terminal so ``cremind`` is on PATH (already-open
       shells won't see the User PATH update).

"@

    if (-not $NoLaunch -and -not $Unattended) {
        Start-Process $WizardUrl
    }
}

switch ($Channel) {
    'test' { Write-Ok "Done. Welcome to Cremind (test build)." }
    'dev'  { Write-Ok "Done. Welcome to Cremind (dev install from $RepoRoot)." }
    default { Write-Ok "Done. Welcome to Cremind." }
}
