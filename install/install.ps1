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

.EXAMPLE
    # Enable HTTPS after trusting the certificate during setup:
    .\install.ps1 -Ssl after-setup

.PARAMETER Deployment
    'local', 'server', or 'custom'. Skips the deployment-type prompt.
    'custom' exposes advanced fields (listen host, public URL, allowed
    origins, wizard preset) so you can configure unusual setups —
    running inside a container, behind a reverse proxy, etc. 'container'
    is accepted as a deprecated alias for 'custom' with container
    defaults.

.PARAMETER AppHost
    Public IP/domain for server deployments.

.PARAMETER Mode
    'docker', 'native' or 'kubernetes'. Skips the mode prompt. A mode is only
    offered (and only accepted) when this machine has what it needs: docker
    needs a reachable daemon, kubernetes needs kubectl with at least one
    kubeconfig context plus helm 3.8+, native needs nothing.

.PARAMETER KubeContext
    (kubernetes) The kubeconfig context to install into. Passed as
    --kube-context to every helm and kubectl call, so the ambient
    current-context never decides where the release lands. Interactive runs
    show a picker; -Unattended requires this flag whenever more than one
    context exists.

.PARAMETER KubeNamespace
    (kubernetes) Namespace for the release, created if missing.
    Default: cremind.

.PARAMETER K8sReleaseName
    (kubernetes) Helm release name. Default: cremind.

.PARAMETER K8sAppUrl
    (kubernetes) cremind.appUrl. Leave unset to let the chart derive
    http(s)://localhost:1515 from the port-forward.

.PARAMETER K8sLegacyPostgresImage
    (kubernetes) 'yes' or 'no'. Point the bundled PostgreSQL at
    docker.io/bitnamilegacy/postgresql. Default: yes — Bitnami froze its free
    images there, so the chart default no longer pulls.

.PARAMETER K8sDeletePostgresData
    (kubernetes) 'yes' or 'no'. Delete the Postgres volume when the release is
    uninstalled. Default: no.

.PARAMETER K8sExtraSet
    (kubernetes) The value of one extra helm --set, e.g.
    'ingress.enabled=true,ingress.host=cremind.example.com'. Applied last, so
    it overrides the installer's own values.

.PARAMETER K8sPostgresPassword
    (kubernetes) Adopt a retained Postgres volume whose password this
    installer never saw, instead of deleting it.

.PARAMETER HelmChart
    (kubernetes) Chart to install: an OCI reference, a .tgz, or a directory.
    Default: oci://registry-1.docker.io/cremind/cremind (the local
    helm\cremind on -Channel dev).

.PARAMETER NoPortForward
    (kubernetes) Don't start the background kubectl port-forward after
    install; just print the command.

.PARAMETER ListenHost
    (custom deployment) Override HOST in .env.

.PARAMETER PublicUrl
    (custom deployment) Override APP_URL in .env.

.PARAMETER AllowedOrigins
    (custom deployment) Override CORS_ALLOWED_ORIGINS in .env.

.PARAMETER WizardPreset
    (custom deployment) Override SETUP_WIZARD_ENV in .env.

.PARAMETER Ssl
    TLS on the public origin (port 1515). One of:

      after-setup  Plain HTTP while the Setup Wizard runs, then a
                   restart into HTTPS. The wizard hands you the local CA and
                   walks you through trusting it BEFORE any https page exists,
                   so the first https load never warns.
      auto         HTTPS from the first boot, with the same generated CA.
                   Browsers warn until it is trusted — the Setup Wizard is
                   already behind the certificate.
      none         (default) Plain HTTP. Enable HTTPS later in Settings > Security.

    A re-install keeps the previous choice unless you pass this flag. An
    already-set $env:CREMIND_SSL or certificate pair is an explicit override
    and is persisted when the flag is absent.

    Interactive installs offer Enable HTTPS (SSL), which selects after-setup.
    Unattended installs use HTTP unless TLS is explicitly configured.
    Electron-driven installs support the same HTTPS modes.

.PARAMETER Desktop
    (Docker mode) Include the VNC Desktop UI — pulls cremind/cremind-desktop
    (XFCE + VNC). Default when neither -Desktop nor -NoDesktop is given.

.PARAMETER NoDesktop
    (Docker mode) Skip the VNC Desktop UI — pulls the smaller headless
    cremind/cremind image (no noVNC/VNC ports). A re-install keeps the
    previous choice unless you pass a flag. Setting both flags is an error.

.PARAMETER VncPassword
    (Docker mode, desktop UI only) Password for the VNC Desktop at
    http://<host>:6080/vnc.html. 6-8 characters from [A-Za-z0-9@%_+=:,.-] —
    VNC ignores anything past the 8th character. Interactive installs ask for
    it (entered twice) when the desktop UI is included; with -Unattended or no
    console this flag wins, else the previous install's password is kept, else
    one is generated and printed at the end.

.PARAMETER BootService
    Register a Scheduled Task that starts Cremind at logon and restarts it if
    it stops. Default: on for native installs, which is also what makes the
    in-app restart and the after-setup HTTPS switch work. Manage it later with
    `cremind boot enable|disable|status`.

.PARAMETER NoBootService
    Skip the logon Scheduled Task; Cremind runs only until you log out. A
    re-install keeps a previous -NoBootService; an install made before this
    flag existed gains the service. Setting both flags is an error. Ignored
    for docker mode (the daemon supervises the container) and for
    Electron-driven installs (the app owns the backend).

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
    [ValidateSet('','docker','native','kubernetes')] [string] $Mode = '',
    # Kubernetes mode. Each of these is also a TUI output key, read back by
    # the whitelist in Invoke-InstallerTuiBootstrap; the ValidateSet above is
    # load-bearing for $Mode because that read-back assigns the variable.
    [string] $KubeContext = '',
    [string] $KubeNamespace = '',
    [string] $K8sReleaseName = '',
    [string] $K8sAppUrl = '',
    [ValidateSet('','yes','no')] [string] $K8sLegacyPostgresImage = '',
    [ValidateSet('','yes','no')] [string] $K8sDeletePostgresData = '',
    [string] $K8sExtraSet = '',
    # Flag-only kubernetes options (never asked, never in the TUI).
    [string] $K8sPostgresPassword = '',
    [string] $HelmChart = '',
    [switch] $NoPortForward,
    # TLS on the public origin. '' = not specified (fresh installs default to
    # plain HTTP; a re-install carries the previous choice forward). See
    # the ── ssl mode ── section below for the full precedence chain.
    [ValidateSet('','none','auto','after-setup')] [string] $Ssl = '',
    # Docker mode only: include the VNC Desktop UI? -Desktop pulls
    # cremind/cremind-desktop; -NoDesktop pulls the headless cremind/cremind.
    # Neither set = ask (default desktop). Setting both is an error.
    [switch] $Desktop,
    [switch] $NoDesktop,
    # Password for the VNC Desktop at http://<host>:6080/vnc.html. Docker +
    # desktop only. Empty = ask interactively (twice), or — with -Unattended
    # / no console — keep the previous install's password, else generate one.
    # 6-8 characters from [A-Za-z0-9@%_+=:,.-]; see $VncPasswordRe below.
    [string] $VncPassword = '',
    # Register a logon Scheduled Task that starts and supervises the server?
    # Neither set = on for native installs, unless a previous install opted
    # out. Setting both is an error. See the ── boot service ── section.
    [switch] $BootService,
    [switch] $NoBootService,
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

# The RFC 1123 label Kubernetes wants for a namespace and Helm for a release
# name. Mirrored verbatim in install.sh and app/installer/tui.py.
$KubeNameRe = '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'

# Run a native command and return its stdout + exit code without letting its
# stderr take the script down.
#
# Windows PowerShell 5.1 turns every redirected stderr line into a
# NativeCommandError record, and under $ErrorActionPreference = 'Stop' the
# first one terminates — on a command that SUCCEEDED. kubectl and helm write
# perfectly routine notices there (deprecated kubeconfig fields, "release not
# found"), so the redirect has to run under 'Continue' and the error records
# have to be unwrapped by hand.
#
# Defined here, above the uninstall flow, because that flow needs it too.
function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [string[]] $ArgumentList = @()
    )
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = [System.Collections.Generic.List[string]]::new()
        $errLines = [System.Collections.Generic.List[string]]::new()
        $raw = & $FilePath @ArgumentList 2>&1
        $code = $LASTEXITCODE
        foreach ($item in @($raw)) {
            if ($item -is [System.Management.Automation.ErrorRecord]) {
                $errLines.Add([string]$item.Exception.Message)
            } else {
                $out.Add([string]$item)
            }
        }
        $logVar = Get-Variable -Name LogFile -Scope Script -ErrorAction SilentlyContinue
        if ($logVar -and $logVar.Value -and $errLines.Count -gt 0) {
            $errLines | Out-File -FilePath $logVar.Value -Encoding utf8 -Append
        }
        return [pscustomobject]@{
            ExitCode = $code
            Stdout   = ($out -join "`n")
            Lines    = $out.ToArray()
            Stderr   = ($errLines -join "`n")
        }
    } finally {
        $ErrorActionPreference = $prevEap
    }
}

# Defined here, above the uninstall flow, because that flow needs it too
# (it rewrites the kubernetes release record it just preserved).
# Write a file as UTF-8 with no BOM, LF line endings and exactly one trailing
# newline — the bytes install.sh's heredocs produce.
#
# ``Set-Content -Encoding utf8`` under Windows PowerShell 5.1 prepends EF BB BF,
# and the ``toml`` parser the backend reads bootstrap.toml and credentials.toml
# with does not skip it: the BOM glues onto the leading ``#`` of the first
# comment and the file fails as "invalid character in key name". That takes down
# ``cremind db upgrade``, ``cremind db current`` and every later ``cremind
# serve`` — a boot loop out of a file the installer itself wrote. .NET's
# UTF8Encoding($false) emits the content and nothing else.
#
# Line endings are normalised because a here-string carries whatever this script
# was delivered with: LF from ``iwr | iex`` against raw GitHub, CRLF from a
# Windows checkout (.gitattributes marks it eol=crlf). Both installers should
# write the same file.
#
# Deliberately scoped to TOML. The .env files keep ``Set-Content -Encoding
# utf8``: this script's own .env round-trips are built around reading them back
# the same way (PS 5.1 decodes a BOM-less file as cp1252 and would mangle the
# em-dashes in the template comments), the ``cremind`` shim's findstr already
# skips a BOM, and python-dotenv tolerates one.
function Write-Utf8NoBomFile {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][AllowEmptyString()][string] $Content
    )
    $text = $Content -replace "`r`n", "`n"
    if (-not $text.EndsWith("`n")) { $text += "`n" }
    # .NET resolves a relative path against the process working directory, not
    # PowerShell's current location; ask the provider for the real one.
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    [System.IO.File]::WriteAllText($full, $text, [System.Text.UTF8Encoding]::new($false))
}

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

    # A Helm release is tracked independently of the host install kind: the
    # same machine can hold a docker or native install AND have installed a
    # release into a cluster. So this is a separate flag, not a $Kind value,
    # and its teardown runs whenever the marker is present.
    $UninstallK8sEnv = Join-Path $UninstallInstallDir 'k8s\release.env'
    $K8sPresent = Test-Path -LiteralPath $UninstallK8sEnv
    $UninstallK8s = @{}
    if ($K8sPresent) {
        foreach ($line in (Get-Content -LiteralPath $UninstallK8sEnv -Encoding UTF8 -ErrorAction SilentlyContinue)) {
            if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                if (-not $UninstallK8s.ContainsKey($Matches[1])) { $UninstallK8s[$Matches[1]] = $Matches[2] }
            }
        }
    }
    function Get-UninstallK8s {
        param([string] $Key)
        if ($UninstallK8s.ContainsKey($Key)) { return [string]$UninstallK8s[$Key] }
        return ''
    }

    # Detect kind from on-disk markers.
    $ComposeFile = Join-Path $UninstallInstallDir 'docker\docker-compose.yml'
    $UninstallVenvDir = Join-Path $UninstallSystemDir 'venv'
    if (Test-Path -LiteralPath $ComposeFile) {
        $Kind = 'docker'
    } elseif (Test-Path -LiteralPath $UninstallVenvDir) {
        $Kind = 'native'
    } elseif ($K8sPresent) {
        $Kind = 'kubernetes'
    } elseif ($installExists) {
        # Install Dir present but no markers - partial install. Default to
        # native so the residual scratch gets cleaned up.
        $Kind = 'native'
    } else {
        Write-Host "Unrecognized install layout." -ForegroundColor Red
        Write-Host "Expected $UninstallSystemDir\venv (native), $UninstallInstallDir\docker\docker-compose.yml (docker)," -ForegroundColor Red
        Write-Host "or $UninstallK8sEnv (kubernetes)." -ForegroundColor Red
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
        if ($K8sPresent) {
            Write-Host "  Helm release: $(Get-UninstallK8s 'HELM_RELEASE') in namespace $(Get-UninstallK8s 'KUBE_NAMESPACE') on context $(Get-UninstallK8s 'KUBE_CONTEXT')"
        }
        Write-Host ''
        Write-Host '  [k] Keep data    - remove the binaries + install scratch; preserve System Dir contents'
        Write-Host '                     (.env, bootstrap.toml, storage\, tokens\, profile dirs)'
        if ($K8sPresent) {
            Write-Host '                     The Helm release is removed; the PostgreSQL volume and the'
            Write-Host '                     record of its password are kept so a reinstall can reuse it.'
        }
        $purgeExtra = if ($Kind -eq 'docker') { ' (incl. Docker volumes)' }
                      elseif ($K8sPresent) { ' (incl. the cluster volumes)' }
                      else { '' }
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

    # Tear the boot service down FIRST. It is a supervisor: if the task and
    # its respawn loop are still alive when the kills below land, the loop
    # puts the server straight back, and the uninstall then deletes files out
    # from under a running process (which on Windows also locks the venv).
    #
    # Unconditional - both -Keep and -Purge, every install kind. The task
    # lives outside both directories (like the PATH entry removed further
    # down), so nothing else here would remove it. `cremind boot` owns the
    # details; the raw commands are the fallback for a venv too broken to
    # run, which is exactly when someone reaches for -Purge.
    $BootTornDown = $false
    $UninstallVenvCremind = Join-Path $UninstallSystemDir 'venv\Scripts\cremind.exe'
    if (Test-Path -LiteralPath $UninstallVenvCremind) {
        try {
            & $UninstallVenvCremind boot disable --yes *> $null
            $BootTornDown = ($LASTEXITCODE -eq 0)
        } catch { }
    }
    if (-not $BootTornDown) {
        & schtasks.exe /End /TN 'Cremind Server' 2>$null | Out-Null
        & schtasks.exe /Delete /TN 'Cremind Server' /F 2>$null | Out-Null
        $SupervisorPidFile = Join-Path $UninstallSystemDir 'supervisor.pid'
        if (Test-Path -LiteralPath $SupervisorPidFile) {
            try {
                $supervisorPid = [int](Get-Content -LiteralPath $SupervisorPidFile -ErrorAction Stop | Select-Object -First 1)
                # /T: the server hangs off the loop via cmd.exe, so only a
                # tree kill reaches it.
                if ($supervisorPid -gt 0) {
                    & taskkill.exe /PID $supervisorPid /T /F 2>$null | Out-Null
                    # taskkill returns once the kill is *requested*. The task's
                    # working directory is the System Dir, so deleting that
                    # directory below fails with a sharing violation until the
                    # tree has actually gone.
                    Wait-Process -Id $supervisorPid -Timeout 10 -ErrorAction SilentlyContinue
                }
            } catch { }
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

    # ── Helm release teardown ────────────────────────────────────────────
    #
    # Runs whenever a release is tracked, regardless of $Kind: a machine can
    # hold a docker or native install and still be the one that installed
    # into the cluster. helm uninstall removes the chart's own PVCs
    # (system/venv/work); the StatefulSet subcharts' data volumes are created
    # by their controllers, so nothing removes them unless -Purge does.
    if ($K8sPresent) {
        $K8sCtx       = Get-UninstallK8s 'KUBE_CONTEXT'
        $K8sNs        = Get-UninstallK8s 'KUBE_NAMESPACE'
        $K8sRel       = Get-UninstallK8s 'HELM_RELEASE'
        $K8sNsCreated = Get-UninstallK8s 'NAMESPACE_CREATED'

        # Stop the background port-forward first - it holds a connection to
        # the pod we are about to delete.
        $PfPidPath = Join-Path $UninstallInstallDir 'k8s\port-forward.pid'
        if (Test-Path -LiteralPath $PfPidPath) {
            $pfPid = (Get-Content -LiteralPath $PfPidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($pfPid) {
                $pfProc = Get-Process -Id ([int]$pfPid) -ErrorAction SilentlyContinue
                if ($pfProc -and $pfProc.ProcessName -eq 'kubectl') {
                    Stop-Process -Id ([int]$pfPid) -Force -ErrorAction SilentlyContinue
                }
            }
        }

        $kubectlCmdU = Get-Command kubectl -ErrorAction SilentlyContinue
        $helmCmdU    = Get-Command helm -ErrorAction SilentlyContinue
        $ctxKnown = $false
        if ($kubectlCmdU -and $K8sCtx) {
            $names = (Invoke-NativeCapture -FilePath $kubectlCmdU.Source -ArgumentList @('config', 'get-contexts', '-o', 'name')).Lines
            if ($names -contains $K8sCtx) { $ctxKnown = $true }
        }

        if (-not $ctxKnown -or -not $helmCmdU) {
            Write-Host "Cannot reach the cluster from here (kubectl/helm missing, or context '$K8sCtx' is gone)." -ForegroundColor Yellow
            Write-Host "Remove the release yourself with:" -ForegroundColor Yellow
            Write-Host "  helm uninstall $K8sRel --kube-context $K8sCtx -n $K8sNs" -ForegroundColor Yellow
        } else {
            Write-Host "Removing Helm release $K8sRel from namespace $K8sNs on context $K8sCtx..."
            $removed = Invoke-NativeCapture -FilePath $helmCmdU.Source -ArgumentList @(
                'uninstall', $K8sRel, '--kube-context', $K8sCtx, '--namespace', $K8sNs, '--wait')
            if ($removed.ExitCode -ne 0) {
                Write-Host 'helm uninstall reported a problem; continuing.' -ForegroundColor Yellow
            }

            if ($UninstallMode -eq 'purge') {
                # The bundled StatefulSets' data volumes. Found by label
                # because their names follow the subchart's pinned fullname
                # (cremind-postgresql), not the Helm release name.
                Write-Host 'Deleting cluster data volumes...'
                foreach ($sub in @('postgresql', 'qdrant', 'chromadb')) {
                    $found = (Invoke-NativeCapture -FilePath $kubectlCmdU.Source -ArgumentList @(
                        '--context', $K8sCtx, '-n', $K8sNs, 'get', 'pvc',
                        '-l', "app.kubernetes.io/name=$sub",
                        '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\n"}{end}')).Stdout
                    foreach ($pvc in ($found -split "`n")) {
                        $name = $pvc.Trim()
                        if (-not $name) { continue }
                        Invoke-NativeCapture -FilePath $kubectlCmdU.Source -ArgumentList @(
                            '--context', $K8sCtx, '-n', $K8sNs, 'delete', 'pvc', $name, '--ignore-not-found') | Out-Null
                        Write-Host "  removed pvc $name"
                    }
                }
                # Only a namespace this installer created is ours to delete.
                if ($K8sNsCreated -eq '1') {
                    Write-Host "Deleting namespace $K8sNs (created by the installer)..."
                    Invoke-NativeCapture -FilePath $kubectlCmdU.Source -ArgumentList @(
                        '--context', $K8sCtx, 'delete', 'namespace', $K8sNs, '--ignore-not-found') | Out-Null
                } else {
                    Write-Host "Namespace $K8sNs existed before the install; leaving it in place."
                }
            } else {
                Write-Host 'Kept the PostgreSQL volume. A reinstall reuses it with the recorded password.'
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
            'server.pid',
            'supervisor.pid'
        )
        foreach ($name in $toRemoveFromSystem) {
            $p = Join-Path $UninstallSystemDir $name
            if (Test-Path -LiteralPath $p) {
                Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        # The Install Dir is otherwise all scratch, but the kept cluster
        # volume is only usable with the password recorded here - wiping it
        # would leave a database nobody can open.
        $K8sKeepText = $null
        if ($K8sPresent -and (Test-Path -LiteralPath $UninstallK8sEnv)) {
            $K8sKeepText = Get-Content -LiteralPath $UninstallK8sEnv -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
        }
        # Wipe the Install Dir wholesale - it's all install scratch.
        if (Test-Path -LiteralPath $UninstallInstallDir) {
            Remove-Item -LiteralPath $UninstallInstallDir -Recurse -Force -ErrorAction SilentlyContinue
            Write-Host "Removed install scratch at $UninstallInstallDir."
        }
        if ($K8sKeepText) {
            $keepDir = Split-Path -Parent $UninstallK8sEnv
            New-Item -ItemType Directory -Path $keepDir -Force | Out-Null
            Write-Utf8NoBomFile -Path $UninstallK8sEnv -Content $K8sKeepText
            Write-Host "Kept $UninstallK8sEnv so the retained PostgreSQL volume stays usable."
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
# Keep the operator's raw -Version before the dev-channel guard blanks it: a
# dev *kubernetes* install still runs a published image in the pod, so the
# kubernetes branch pins image.tag from this copy. ($Mode is not known yet -
# the TUI may still choose it - so the blanking below stays unconditional.)
$VersionRaw = $Version
if ($Version -and $Channel -eq 'dev') {
    Write-Host "!!! -Version is ignored on dev channel (editable install; a kubernetes install still uses it as the pod's image tag)." -ForegroundColor Yellow
    $Version = ''
}
# Kubernetes flag shapes. Checked here, before anything reaches helm: a bad
# namespace or release name is a template error several minutes into an
# install otherwise.
if ($KubeNamespace -and $KubeNamespace -cnotmatch $KubeNameRe) {
    Write-Host "ERR Invalid -KubeNamespace: '$KubeNamespace' (lowercase letters, digits and hyphens, max 63 characters)" -ForegroundColor Red
    exit 2
}
if ($K8sReleaseName) {
    if ($K8sReleaseName -cnotmatch $KubeNameRe) {
        Write-Host "ERR Invalid -K8sReleaseName: '$K8sReleaseName' (lowercase letters, digits and hyphens)" -ForegroundColor Red
        exit 2
    }
    if ($K8sReleaseName.Length -gt 53) {
        Write-Host "ERR Invalid -K8sReleaseName: Helm allows at most 53 characters." -ForegroundColor Red
        exit 2
    }
}
if ($K8sAppUrl -and $K8sAppUrl -notmatch '^https?://[^/\s]+') {
    Write-Host "ERR Invalid -K8sAppUrl: '$K8sAppUrl' (expected http://host[:port] or https://host[:port])" -ForegroundColor Red
    exit 2
}
# -K8sExtraSet is the VALUE of one helm --set, not a fragment of argv.
if ($K8sExtraSet -and $K8sExtraSet.StartsWith('-')) {
    Write-Host "ERR Invalid -K8sExtraSet: pass key=value[,key=value], without the --set itself." -ForegroundColor Red
    exit 2
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

# Kubernetes has no host to bind - the chart sets HOST and APP_URL on the pod
# - so it never gets a deployment, not even an unattended default.
if ($Unattended -and -not $Deployment -and $Mode -ne 'kubernetes') { $Deployment = 'local' }
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

# The one rule for a VNC password, mirrored verbatim in install.sh and
# app/installer/tui.py (a drift test pins the three together). 6-8 is VNC's
# own range — vncpasswd rejects under 6 and the DES scheme keys off the first
# 8 only. The charset excludes | & \ $ # space and quotes because the value
# passes through a -replace into the .env template, an unquoted compose .env,
# and a TOML basic string in credentials.toml.
$VncPasswordRe = '^[A-Za-z0-9@%_+=:,.-]{6,8}$'
# A bad -VncPassword fails now, in every mode. Unattended installs never
# prompt, but they must not silently install a password the container will
# mangle either — failing fast beats debugging a desktop you cannot unlock.
if ($VncPassword -and $VncPassword -notmatch $VncPasswordRe) {
    Write-Err2 "Invalid -VncPassword: use 6-8 characters from letters, digits and @ % _ + = : , . -"
    Write-Err2 "(VNC itself ignores anything past the 8th character.)"
    exit 2
}
# The VNC password a previous install left in docker\.env, read before the TUI
# runs so an empty answer can mean "keep that one" instead of rotating it.
$PrevVncPassword = ''

if ($BootService -and $NoBootService) {
    Write-Err2 "-BootService and -NoBootService are mutually exclusive"
    exit 2
}
$BootExplicit = ($BootService -or $NoBootService)

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

# ── kubernetes capabilities ───────────────────────────────────────────────
#
# kubectl and helm are what the ``kubernetes`` install mode needs, and the
# catalog's ``requires`` turns their presence into whether the mode is even
# offered. Probing kubectl means more than "is it on PATH": a kubeconfig with
# no contexts gives us nothing to install into.
#
# `kubectl config view -o json`, not `-o jsonpath=...`: PowerShell 5.1's
# native-argument quoting mangles a jsonpath expression (it contains spaces,
# braces and embedded quotes). JSON is one argument and ConvertFrom-Json does
# the rest. `config view` reads the MERGED kubeconfig, so a KUBECONFIG naming
# several files is handled by kubectl rather than by us.
$HasKubectl        = $false
$HasHelm           = $false
$KubectlExe        = ''
$KubeContexts      = @()
$KubeCurrentContext = ''
$KubeContextsFile  = ''
$kubectlCmd = Get-Command kubectl -ErrorAction SilentlyContinue
if ($kubectlCmd) {
    $KubectlExe = $kubectlCmd.Source
    $viewed = Invoke-NativeCapture -FilePath $KubectlExe -ArgumentList @('config', 'view', '-o', 'json')
    if ($viewed.ExitCode -eq 0 -and $viewed.Stdout) {
        try {
            $cfg = $viewed.Stdout | ConvertFrom-Json
        } catch {
            $cfg = $null
        }
        if ($cfg -and $cfg.PSObject.Properties.Name -contains 'contexts' -and $cfg.contexts) {
            $servers = @{}
            if ($cfg.PSObject.Properties.Name -contains 'clusters' -and $cfg.clusters) {
                foreach ($cluster in @($cfg.clusters)) {
                    if ($cluster.name) { $servers[$cluster.name] = [string]$cluster.cluster.server }
                }
            }
            $rows = [System.Collections.Generic.List[object]]::new()
            foreach ($ctx in @($cfg.contexts)) {
                if (-not $ctx.name) { continue }
                $clusterName = ''
                if ($ctx.context.PSObject.Properties.Name -contains 'cluster') { $clusterName = [string]$ctx.context.cluster }
                $ns = ''
                if ($ctx.context.PSObject.Properties.Name -contains 'namespace') { $ns = [string]$ctx.context.namespace }
                $server = ''
                if ($clusterName -and $servers.ContainsKey($clusterName)) { $server = $servers[$clusterName] }
                $rows.Add([pscustomobject]@{ Name = [string]$ctx.name; Server = $server; Namespace = $ns })
            }
            $KubeContexts = @($rows)
            if ($cfg.PSObject.Properties.Name -contains 'current-context') {
                $KubeCurrentContext = [string]$cfg.'current-context'
            }
        }
    }
    if ($KubeContexts.Count -ge 1) {
        $HasKubectl = $true
        # One context per line: name<TAB>server<TAB>namespace — the format the
        # TUI's parse_kube_contexts reads.
        $KubeContextsFile = Join-Path $CremindInstallDir '.kube-contexts'
        $ctxText = ($KubeContexts | ForEach-Object { "$($_.Name)`t$($_.Server)`t$($_.Namespace)" }) -join "`n"
        Write-Utf8NoBomFile -Path $KubeContextsFile -Content ($ctxText + "`n")
        $currentNote = ''
        if ($KubeCurrentContext) { $currentNote = ", current: $KubeCurrentContext" }
        Write-Ok "kubectl: $($KubeContexts.Count) context(s)$currentNote"
    } else {
        Write-Info "kubectl: found, but the kubeconfig has no contexts"
    }
} else {
    Write-Info "kubectl: not detected"
}
if (Get-Command helm -ErrorAction SilentlyContinue) {
    # helm 3.8 is where OCI chart references stopped being experimental, and
    # the chart is published only as an OCI artifact.
    $helmVer = (Invoke-NativeCapture -FilePath 'helm' -ArgumentList @('version', '--short')).Stdout.Trim()
    if ($helmVer -match '^v(\d+)\.(\d+)\.') {
        $helmMajor = [int]$Matches[1]
        $helmMinor = [int]$Matches[2]
        if ($helmMajor -gt 3 -or ($helmMajor -eq 3 -and $helmMinor -ge 8)) {
            $HasHelm = $true
            Write-Ok "helm: $helmVer"
        } else {
            Write-Info "helm: $helmVer - 3.8 or newer is required for OCI charts"
        }
    } else {
        Write-Info "helm: found, but its version could not be read"
    }
} else {
    Write-Info "helm: not detected"
}

# ── mode availability ─────────────────────────────────────────────────────
#
# The catalog's per-mode ``requires`` decides what this machine may be
# offered. install.sh and the TUI apply the same rule over the same data. An
# unknown capability id counts as unmet, so a typo in catalog.toml hides a
# mode instead of advertising a broken one.
function Test-CremindCapability {
    param([string] $Name)
    switch ($Name) {
        'docker'  { return $HasDocker }
        'kubectl' { return $HasKubectl }
        'helm'    { return $HasHelm }
        default   { return $false }
    }
}

function Get-CapabilityLabel {
    param([string] $Name)
    switch ($Name) {
        'docker'  { return 'a running Docker daemon' }
        'kubectl' { return 'kubectl with a kubeconfig context' }
        'helm'    { return 'helm 3.8+' }
        default   { return $Name }
    }
}

function Test-ModeAvailable {
    param([string] $Id)
    if (-not $script:Modes.Contains($Id)) { return $false }
    foreach ($req in @($script:Modes[$Id].Requires)) {
        if (-not (Test-CremindCapability $req)) { return $false }
    }
    return $true
}

$AvailableModeIds = @($script:ModeIds | Where-Object { Test-ModeAvailable $_ })

# ── previous kubernetes release ───────────────────────────────────────────
#
# A kubernetes install records what it did in k8s\release.env so a re-run can
# offer the same answers and an uninstall knows what to tear down. Read -
# never dot-sourced: the file holds two secrets and arbitrary --set text.
$K8sDir        = Join-Path $CremindInstallDir 'k8s'
$K8sReleaseEnv = Join-Path $K8sDir 'release.env'
$PrevK8s = @{}
if (Test-Path -LiteralPath $K8sReleaseEnv) {
    foreach ($line in (Get-Content -LiteralPath $K8sReleaseEnv -Encoding UTF8)) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            if (-not $PrevK8s.ContainsKey($Matches[1])) { $PrevK8s[$Matches[1]] = $Matches[2] }
        }
    }
}
function Get-PrevK8s {
    param([string] $Key)
    if ($PrevK8s.ContainsKey($Key)) { return [string]$PrevK8s[$Key] }
    return ''
}

# Read the VNC password a previous install left behind, so both the TUI and
# the fallback prompt can offer "leave empty to keep the current one". Done
# once here because the TUI needs it before it renders, and the docker branch
# further down needs it again to resolve $VncPwd.
$prevVncEnv = Join-Path (Join-Path $CremindInstallDir 'docker') '.env'
if (Test-Path -LiteralPath $prevVncEnv) {
    $prevVncLine = Get-Content -LiteralPath $prevVncEnv |
        Where-Object { $_ -like 'VNC_PASSWORD=*' } | Select-Object -First 1
    if ($prevVncLine) {
        $PrevVncPassword = ($prevVncLine -replace '^VNC_PASSWORD=', '').Trim()
    }
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
    if ($Ssl)             { $tuiArgs.Add('--ssl');              $tuiArgs.Add($Ssl) }
    $tuiArgs.Add('--ssl-inherited')
    $tuiArgs.Add($(if ($env:CREMIND_SSL -or $env:CREMIND_SSL_CERTFILE -or $env:CREMIND_SSL_KEYFILE) { '1' } else { '0' }))
    $tuiArgs.Add('--native-env'); $tuiArgs.Add($EnvFile)
    $tuiArgs.Add('--docker-env'); $tuiArgs.Add((Join-Path (Join-Path $CremindInstallDir 'docker') '.env'))
    if ($DesktopUi)       { $tuiArgs.Add('--desktop');          $tuiArgs.Add($DesktopUi) }
    if ($VncPassword)     { $tuiArgs.Add('--vnc-password');     $tuiArgs.Add($VncPassword) }
    # Always sent (never empty): tells the TUI whether an empty password
    # answer may mean "keep the existing one".
    $tuiArgs.Add('--vnc-password-set')
    # Either previous install counts - the mode is not settled yet, and the
    # text prompt re-points $PrevVncPassword once it is.
    $tuiArgs.Add($(if ($PrevVncPassword -or (Get-PrevK8s 'VNC_PASSWORD')) { '1' } else { '0' }))
    # Kubernetes: capabilities and the probed context list are context-only
    # (the TUI never writes them back); the value flags round-trip, so a flag
    # the operator passed is echoed back instead of being cleared.
    $tuiArgs.Add('--has-kubectl'); $tuiArgs.Add(([int][bool]$HasKubectl).ToString())
    $tuiArgs.Add('--has-helm');    $tuiArgs.Add(([int][bool]$HasHelm).ToString())
    if ($KubeContextsFile)   { $tuiArgs.Add('--kube-contexts-file');   $tuiArgs.Add($KubeContextsFile) }
    if ($KubeCurrentContext) { $tuiArgs.Add('--kube-current-context'); $tuiArgs.Add($KubeCurrentContext) }
    if ($KubeContext)            { $tuiArgs.Add('--kube-context');               $tuiArgs.Add($KubeContext) }
    if ($KubeNamespace)          { $tuiArgs.Add('--kube-namespace');             $tuiArgs.Add($KubeNamespace) }
    if ($K8sReleaseName)         { $tuiArgs.Add('--k8s-release-name');           $tuiArgs.Add($K8sReleaseName) }
    if ($K8sAppUrl)              { $tuiArgs.Add('--k8s-app-url');                $tuiArgs.Add($K8sAppUrl) }
    if ($K8sLegacyPostgresImage) { $tuiArgs.Add('--k8s-legacy-postgres-image');  $tuiArgs.Add($K8sLegacyPostgresImage) }
    if ($K8sDeletePostgresData)  { $tuiArgs.Add('--k8s-delete-postgres-data');   $tuiArgs.Add($K8sDeletePostgresData) }
    if ($K8sExtraSet)            { $tuiArgs.Add('--k8s-extra-set');              $tuiArgs.Add($K8sExtraSet) }
    if ($Version)         { $tuiArgs.Add('--version');          $tuiArgs.Add($Version) }
    if ($AppHost)         { $tuiArgs.Add('--host');             $tuiArgs.Add($AppHost) }
    if ($ListenHost)      { $tuiArgs.Add('--listen-host');      $tuiArgs.Add($ListenHost) }
    if ($PublicUrl)       { $tuiArgs.Add('--public-url');       $tuiArgs.Add($PublicUrl) }
    if ($AllowedOrigins)  { $tuiArgs.Add('--allowed-origins');  $tuiArgs.Add($AllowedOrigins) }
    if ($WizardPreset)    { $tuiArgs.Add('--wizard-preset');    $tuiArgs.Add($WizardPreset) }
    if ($ElectronVersion) { $tuiArgs.Add('--electron-version'); $tuiArgs.Add($ElectronVersion) }

    # Clear any stale output / cancel-sentinel from a previous run so the
    # marker check below can't misfire on leftover content.
    Remove-Item -LiteralPath $tuiOut -Force -ErrorAction SilentlyContinue
    Write-Info "Launching installer TUI (Enter continues, Esc cancels, Ctrl+C force-quits)"
    if ($devPython) {
        & $devPython $InstallerTuiPyz @tuiArgs
    } else {
        & $UvExe run --quiet --python 3.13 `
            --with prompt_toolkit --with rich `
            python $InstallerTuiPyz @tuiArgs
    }
    $rc = $LASTEXITCODE

    # Trust the cancel sentinel the TUI writes on Esc/Ctrl+C over $rc:
    # `uv run` does not reliably propagate the child's exit code on Ctrl+C
    # (Windows returns 0), which would otherwise fall through to the legacy
    # text prompts instead of exiting the installer.
    $tuiCancelled = (Test-Path -LiteralPath $tuiOut) -and `
        ((Get-Content -LiteralPath $tuiOut -Raw -ErrorAction SilentlyContinue) -match '(?m)^CREMIND_TUI_CANCELLED=1\s*$')
    if ($tuiCancelled -or $rc -eq 1) {
        Remove-Item -LiteralPath $tuiOut -Force -ErrorAction SilentlyContinue
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
                    'SSL_CHOICE'           { if (-not $Ssl -and $v -in @('none', 'auto', 'after-setup')) { Set-Variable -Scope Script Ssl $v } }
                    'DESKTOP_UI'           { if (-not $DesktopUi)      { Set-Variable -Scope Script DesktopUi $v } }
                    'VNC_PASSWORD_INPUT'   { if (-not $VncPassword)    { Set-Variable -Scope Script VncPassword $v } }
                    'CUSTOM_listen_host'   { if (-not $ListenHost)     { Set-Variable -Scope Script ListenHost $v } }
                    'CUSTOM_public_url'    { if (-not $PublicUrl)      { Set-Variable -Scope Script PublicUrl $v } }
                    'CUSTOM_allowed_origins' { if (-not $AllowedOrigins) { Set-Variable -Scope Script AllowedOrigins $v } }
                    'CUSTOM_wizard_preset' { if (-not $WizardPreset)   { Set-Variable -Scope Script WizardPreset $v } }
                    'KUBE_CONTEXT'         { if (-not $KubeContext)    { Set-Variable -Scope Script KubeContext $v } }
                    'KUBE_NAMESPACE'       { if (-not $KubeNamespace)  { Set-Variable -Scope Script KubeNamespace $v } }
                    'K8S_release_name'     { if (-not $K8sReleaseName) { Set-Variable -Scope Script K8sReleaseName $v } }
                    'K8S_app_url'          { if (-not $K8sAppUrl)      { Set-Variable -Scope Script K8sAppUrl $v } }
                    'K8S_legacy_postgres_image' { if (-not $K8sLegacyPostgresImage -and $v -in @('yes', 'no')) { Set-Variable -Scope Script K8sLegacyPostgresImage $v } }
                    'K8S_delete_postgres_data'  { if (-not $K8sDeletePostgresData -and $v -in @('yes', 'no')) { Set-Variable -Scope Script K8sDeletePostgresData $v } }
                    'K8S_extra_set'        { if (-not $K8sExtraSet)    { Set-Variable -Scope Script K8sExtraSet $v } }
                }
            }
        }
        Remove-Item -LiteralPath $tuiOut -Force -ErrorAction SilentlyContinue
        $script:TuiApplied = $true
        Write-Ok "TUI selections applied."
    }
}

# 1 once the TUI's answers were applied. The text-mode kubernetes flow adds
# its own final confirmation; the TUI already has a confirm screen.
# Pre-declared for StrictMode, which faults on reading an unassigned variable.
$TuiApplied = $false
Invoke-InstallerTuiBootstrap

# ── mode (how Cremind is installed) ───────────────────────────────────────
#
# Asked before the deployment questions because it decides whether they are
# asked at all: a kubernetes install has no host to bind and no .env to
# render - the chart owns both on the pod.
#
# Only modes this machine can actually run are offered ($AvailableModeIds,
# computed from the catalog's ``requires`` above). When one survives there is
# nothing to ask, which is what happens on a plain laptop with no Docker.

# Guard against a mode whose requirements this machine does not meet. (An
# unknown -Mode is already impossible: the parameter's ValidateSet rejects it
# at binding time.)
if ($Mode) {
    if ($script:ModeIds -notcontains $Mode) {
        Write-Err2 "Unknown mode: $Mode (must be one of: $($script:ModeIds -join ', '))"
        exit 2
    }
    if (-not (Test-ModeAvailable $Mode)) {
        Write-Err2 "$Mode mode was requested, but this machine is missing what it needs:"
        foreach ($req in @($script:Modes[$Mode].Requires)) {
            if (-not (Test-CremindCapability $req)) {
                Write-Err2 "  - $(Get-CapabilityLabel $req)"
            }
        }
        if ($Mode -eq 'kubernetes') {
            Write-Err2 "Install kubectl and helm 3.8+, and make sure 'kubectl config get-contexts' lists at least one context."
        } elseif ($Mode -eq 'docker') {
            Write-Err2 "Start Docker Desktop and re-run."
        }
        exit 1
    }
}

if (-not $Mode) {
    if ($AvailableModeIds.Count -le 1 -or $Unattended) {
        # Catalog order is the recommendation, so the first available mode is
        # the pick. Unattended never lands on kubernetes: it sorts after
        # native, which is always available - an unattended kubernetes install
        # is opted into with -Mode kubernetes.
        if ($AvailableModeIds.Count -ge 1) {
            $Mode = $AvailableModeIds[0]
        } else {
            $Mode = 'native'
        }
    } else {
        Write-Host ''
        Write-Host 'How do you want to run Cremind?' -ForegroundColor White
        $idx = 0
        foreach ($mId in $AvailableModeIds) {
            $idx++
            $entry = $script:Modes[$mId]
            Write-Host ("  {0}) {1} - {2}" -f $idx, $entry.Label, $entry.Description)
            if ($entry.Hint) { Write-Host ("      {0}" -f $entry.Hint) -ForegroundColor DarkGray }
        }
        # Name what is missing rather than silently shortening the list.
        foreach ($mId in $script:ModeIds) {
            if (Test-ModeAvailable $mId) { continue }
            $missing = @()
            foreach ($req in @($script:Modes[$mId].Requires)) {
                if (-not (Test-CremindCapability $req)) { $missing += (Get-CapabilityLabel $req) }
            }
            Write-Host ("  (not offered: {0} - needs {1})" -f $script:Modes[$mId].Label, ($missing -join ', ')) -ForegroundColor DarkGray
        }
        while ($true) {
            $choice = Read-Host "Choice [1]"
            if (-not $choice) { $choice = '1' }
            $picked = ''
            $idx = 0
            foreach ($mId in $AvailableModeIds) {
                $idx++
                if ($choice -eq "$idx" -or $choice -eq $mId) { $picked = $mId; break }
            }
            if ($picked) { $Mode = $picked; break }
            Write-Warn2 "Pick a number 1-$idx or a mode id."
        }
    }
}
Write-Ok "Mode: $Mode"

# The desktop app drives this script for a local install; it has no helm
# branch, no context picker, and computes the post-install URL itself.
if ($Mode -eq 'kubernetes' -and $env:CREMIND_INSTALLER_FRONTEND -eq 'electron') {
    Write-Err2 "Kubernetes installs are not supported from the desktop app."
    Write-Err2 "Run install.ps1 -Mode kubernetes in a terminal instead."
    exit 2
}

# ── deployment type ───────────────────────────────────────────────────────

if ($Mode -eq 'kubernetes') {
    # The chart sets HOST, APP_URL and CORS on the pod; there is nothing here
    # to answer. Warn rather than silently dropping a flag the operator typed.
    if ($Deployment -or $AppHost) {
        Write-Warn2 "-Deployment / -AppHost do not apply to a kubernetes install; the chart sets them on the pod."
        $Deployment = ''
        $AppHost = ''
    }
}

if ($Mode -ne 'kubernetes') {

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

}  # end: deployment questions (skipped for kubernetes)

# ── desktop UI (docker mode only) ─────────────────────────────────────────
#
# Docker installs choose an image flavor: the desktop image
# (cremind/cremind-desktop, XFCE + VNC) or the basic headless image
# (cremind/cremind). Default is desktop everywhere, including -Unattended.
# A re-install reads the previous choice from the existing docker\.env
# (CREMIND_IMAGE) so an unattended re-run preserves the flavor.
#
# Kubernetes picks the same two images through the chart's desktop.enabled,
# with one extra rule: on the production channel the basic image is not
# installable at all (see the refusal below), so it is not offered.
if ($Mode -eq 'kubernetes' -and $Channel -eq 'production' -and -not $DesktopUi) {
    $DesktopUi = '1'
}
if (($Mode -eq 'docker' -or $Mode -eq 'kubernetes') -and -not $DesktopUi) {
    $desktopDefault = '1'
    if ($Mode -eq 'kubernetes') {
        if ((Get-PrevK8s 'DESKTOP_UI') -eq '0') { $desktopDefault = '0' }
    } else {
        $prevEnv = Join-Path (Join-Path $CremindInstallDir 'docker') '.env'
        if (Test-Path -LiteralPath $prevEnv) {
            $prevImageLine = Get-Content -LiteralPath $prevEnv | Where-Object { $_ -like 'CREMIND_IMAGE=*' } | Select-Object -First 1
            if ($prevImageLine -and ($prevImageLine -replace '^CREMIND_IMAGE=', '').Trim() -eq 'cremind/cremind') {
                $desktopDefault = '0'
            }
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
# The production chart and the production basic image are published to the
# SAME Docker Hub tag - `helm push` writes cremind/cremind:X.Y.Z after the
# image job has pushed an image there, so that tag holds a Helm chart. A
# headless production install would ask Kubernetes to run the chart artifact
# as a container image and fail on the pull. Release candidates do not collide
# (chart 0.0.17-rc.13.dev.1 vs image 0.0.17rc13.dev1), so the test channel is
# fine. Refused here rather than leaving a pod in ImagePullBackOff.
if ($Mode -eq 'kubernetes' -and $Channel -eq 'production' -and $DesktopUi -eq '0') {
    Write-Err2 "A production Kubernetes install cannot use the basic (headless) image."
    Write-Err2 "On the production channel, cremind/cremind:<version> on Docker Hub is the Helm"
    Write-Err2 "chart artifact, not a container image, so the pod could never pull it."
    Write-Err2 "Use the desktop image (drop -NoDesktop), or -Channel test for a headless install."
    exit 2
}

if ($Mode -eq 'docker' -or $Mode -eq 'kubernetes') {
    if ($DesktopUi -eq '0') {
        Write-Ok "Desktop UI: no (basic headless image)"
    } else {
        Write-Ok "Desktop UI: yes (VNC desktop image)"
    }
}

# ── VNC password (container modes + desktop only) ─────────────────────────
#
# Asked here when the TUI didn't (-NoTui, TUI failure) and no -VncPassword was
# passed. Unattended installs deliberately fall through without asking: the
# install branch below still resolves a password from the previous install or
# generates one, so a scripted install never blocks.
#
# Read-Host -AsSecureString, not -MaskInput: the latter is PowerShell 7.1+,
# and the Electron installer spawns Windows PowerShell 5.1.
if ($Mode -eq 'kubernetes') {
    # The relevant "previous install" is the Helm release, not docker\.env.
    $PrevVncPassword = Get-PrevK8s 'VNC_PASSWORD'
}
if (($Mode -eq 'docker' -or $Mode -eq 'kubernetes') -and $DesktopUi -ne '0' -and -not $VncPassword -and -not $Unattended) {
    Write-Host ""
    Write-Host $script:VncPasswordPrompt.Prompt -ForegroundColor White
    if ($script:VncPasswordPrompt.Hint) {
        Write-Host ("  $($script:VncPasswordPrompt.Hint)") -ForegroundColor DarkGray
    }
    # Bounded, because every ``continue`` below re-reads: a host whose
    # Read-Host returns empty without blocking would otherwise spin forever.
    # Giving up falls through to the previous/generated password, which is the
    # non-interactive behaviour.
    $vncTries = 0
    while (-not $VncPassword -and $vncTries -lt 5) {
        $vncTries++
        $sec = Read-Host "VNC password" -AsSecureString
        $pw = [System.Net.NetworkCredential]::new('', $sec).Password
        if (-not $pw) {
            if ($PrevVncPassword) {
                Write-Ok "Keeping the existing VNC password."
                break
            }
            Write-Warn2 "A password is required for the VNC Desktop."
            continue
        }
        if ($pw -notmatch $VncPasswordRe) {
            Write-Warn2 "Use 6-8 characters from letters, digits and @ % _ + = : , . -"
            continue
        }
        $sec2 = Read-Host "Confirm VNC password" -AsSecureString
        $pw2 = [System.Net.NetworkCredential]::new('', $sec2).Password
        if ($pw -ne $pw2) {
            Write-Warn2 "The two entries were different. Please try again."
            continue
        }
        $VncPassword = $pw
    }
    if (-not $VncPassword -and -not $PrevVncPassword) {
        Write-Warn2 "No VNC password entered; generating one and printing it at the end."
    }
}

# ── kubernetes questions ──────────────────────────────────────────────────
#
# The fallback for everything the TUI would have asked: which cluster, which
# namespace, and the Helm options. Reached with -NoTui, in a non-interactive
# host, when the TUI failed to launch, and in -Unattended (where nothing is
# asked and the flags/defaults stand).
#
# The context question is the one that cannot be defaulted away. Installing
# into the wrong cluster is not recoverable by re-running, so an unattended
# run with several contexts is an error, not a guess.
$KubeServer = ''
if ($Mode -eq 'kubernetes') {
    Write-Step "Kubernetes target"

    function Write-KubeContexts {
        $i = 0
        foreach ($ctx in $KubeContexts) {
            $i++
            $server = if ($ctx.Server) { $ctx.Server } else { 'server unknown' }
            $ns     = if ($ctx.Namespace) { $ctx.Namespace } else { 'default' }
            $marker = if ($ctx.Name -eq $KubeCurrentContext) { '  [current]' } else { '' }
            Write-Host ("  {0}) {1} - {2} ({3}){4}" -f $i, $ctx.Name, $server, $ns, $marker)
        }
    }

    $interactive = (-not $Unattended) -and [Environment]::UserInteractive
    if ($KubeContext) {
        if (-not ($KubeContexts | Where-Object { $_.Name -eq $KubeContext })) {
            Write-Err2 "Unknown kubeconfig context: $KubeContext"
            Write-Err2 "Available contexts:"
            Write-KubeContexts
            exit 2
        }
    } elseif ($KubeContexts.Count -eq 1) {
        $KubeContext = $KubeContexts[0].Name
    } elseif (-not $interactive) {
        Write-Err2 "-KubeContext is required: the kubeconfig has $($KubeContexts.Count) contexts and"
        Write-Err2 "an unattended install must not guess which cluster to install into."
        Write-KubeContexts
        exit 2
    } else {
        Write-Host ''
        Write-Host $script:Kubernetes.ContextPrompt -ForegroundColor White
        if ($script:Kubernetes.ContextHint) {
            Write-Host ("  $($script:Kubernetes.ContextHint)") -ForegroundColor DarkGray
        }
        Write-KubeContexts
        $ctxDefaultIdx = 1
        for ($i = 0; $i -lt $KubeContexts.Count; $i++) {
            if ($KubeContexts[$i].Name -eq $KubeCurrentContext) { $ctxDefaultIdx = $i + 1; break }
        }
        while (-not $KubeContext) {
            $choice = Read-Host "Choice [$ctxDefaultIdx]"
            if (-not $choice) { $choice = "$ctxDefaultIdx" }
            $picked = $KubeContexts | Where-Object { $_.Name -eq $choice } | Select-Object -First 1
            if (-not $picked) {
                $n = 0
                if ([int]::TryParse($choice, [ref]$n) -and $n -ge 1 -and $n -le $KubeContexts.Count) {
                    $picked = $KubeContexts[$n - 1]
                }
            }
            if ($picked) { $KubeContext = $picked.Name }
            else { Write-Warn2 "Pick a number from the list, or a context name." }
        }
    }
    $ctxEntry = $KubeContexts | Where-Object { $_.Name -eq $KubeContext } | Select-Object -First 1
    if ($ctxEntry) { $KubeServer = $ctxEntry.Server }
    $serverNote = if ($KubeServer) { " ($KubeServer)" } else { '' }
    Write-Ok "Context: $KubeContext$serverNote"

    if (-not $KubeNamespace) {
        $nsDefault = Get-PrevK8s 'KUBE_NAMESPACE'
        if (-not $nsDefault) { $nsDefault = $script:Kubernetes.NamespaceDefault }
        if (-not $nsDefault) { $nsDefault = 'cremind' }
        if (-not $interactive) {
            $KubeNamespace = $nsDefault
        } else {
            Write-Host ''
            Write-Host $script:Kubernetes.NamespacePrompt -ForegroundColor White
            if ($script:Kubernetes.NamespaceHint) {
                Write-Host ("  $($script:Kubernetes.NamespaceHint)") -ForegroundColor DarkGray
            }
            while (-not $KubeNamespace) {
                $answer = Read-Host "  [$nsDefault]"
                if (-not $answer) { $answer = $nsDefault }
                if ($answer -cmatch $KubeNameRe) { $KubeNamespace = $answer }
                else { Write-Warn2 "Use lowercase letters, digits and hyphens, max 63 characters." }
            }
        }
    }
    Write-Ok "Namespace: $KubeNamespace"

    # Helm options. The gate is skipped when a flag already answered part of
    # it (then only the unanswered fields are asked) and in -Unattended.
    $K8sValues = @{
        'release_name'          = $K8sReleaseName
        'app_url'               = $K8sAppUrl
        'legacy_postgres_image' = $K8sLegacyPostgresImage
        'delete_postgres_data'  = $K8sDeletePostgresData
        'extra_set'             = $K8sExtraSet
    }
    # A populated release name is the "already answered" signal - from the
    # TUI, a flag, or the previous release - here and in the TUI's
    # screen_k8s_advanced. It works because release_name is the one advanced
    # field with a non-empty default: app_url and extra_set are legitimately
    # blank, so emptiness alone cannot mean "not asked yet".
    $k8sAnyAnswered = $false
    foreach ($v in $K8sValues.Values) { if ($v) { $k8sAnyAnswered = $true } }
    $k8sCustomize = $false
    if ($K8sReleaseName) {
        $k8sCustomize = $false
    } elseif ($k8sAnyAnswered) {
        # A flag answered part of it, so the operator is customizing: ask the
        # rest, release name included.
        $k8sCustomize = $true
    } elseif ($interactive) {
        Write-Host ''
        Write-Host $script:Kubernetes.AdvancedPrompt -ForegroundColor White
        if ($script:Kubernetes.AdvancedHint) {
            Write-Host ("  $($script:Kubernetes.AdvancedHint)") -ForegroundColor DarkGray
        }
        while ($true) {
            $ans = Read-Host "Customize the Helm options? [y/N]"
            if (-not $ans -or $ans -match '^(n|no)$') { $k8sCustomize = $false; break }
            if ($ans -match '^(y|yes)$') { $k8sCustomize = $true; break }
            Write-Warn2 "Please answer yes or no."
        }
    }

    foreach ($field in $script:Kubernetes.AdvancedFields) {
        $key = $field.Key
        if ($K8sValues[$key]) {
            Write-Ok "$key`: $($K8sValues[$key])"
            continue
        }
        $default = Get-PrevK8s ("K8S_" + $key)
        if (-not $default) { $default = $field.Default }
        if (-not $k8sCustomize) {
            $K8sValues[$key] = $default
            continue
        }
        Write-Host ''
        Write-Host $field.Prompt -ForegroundColor White
        Write-Host $field.Hint   -ForegroundColor DarkGray
        if ($field.Choices.Count -gt 0) {
            Write-Host ("Choices: " + ($field.Choices -join ', ')) -ForegroundColor DarkGray
        }
        while ($true) {
            $answer = Read-Host "  [$default]"
            if (-not $answer) { $answer = $default }
            if ($field.Choices.Count -gt 0 -and -not ($field.Choices -contains $answer)) {
                Write-Warn2 "Pick one of: $($field.Choices -join ', ')"
                continue
            }
            if ($key -eq 'release_name' -and (($answer -cnotmatch $KubeNameRe) -or $answer.Length -gt 53)) {
                Write-Warn2 "Use lowercase letters, digits and hyphens, max 53 characters."
                continue
            }
            if ($key -eq 'app_url' -and $answer -and $answer -notmatch '^https?://[^/\s]+') {
                Write-Warn2 "Start the URL with http:// or https://, or leave it blank."
                continue
            }
            $K8sValues[$key] = $answer
            break
        }
    }
    $K8sReleaseName         = $K8sValues['release_name']
    $K8sAppUrl              = $K8sValues['app_url']
    $K8sLegacyPostgresImage = $K8sValues['legacy_postgres_image']
    $K8sDeletePostgresData  = $K8sValues['delete_postgres_data']
    $K8sExtraSet            = $K8sValues['extra_set']
    if (-not $K8sReleaseName) { $K8sReleaseName = 'cremind' }

    # The TUI has a confirm screen; the text path does not, and this is the
    # question worth confirming - the cluster.
    if (-not $TuiApplied -and $interactive) {
        Write-Host ''
        Write-Host 'About to install into:' -ForegroundColor White
        Write-Host ("  context    {0}" -f $KubeContext)
        Write-Host ("  server     {0}" -f $(if ($KubeServer) { $KubeServer } else { '(unknown)' }))
        Write-Host ("  namespace  {0}" -f $KubeNamespace)
        Write-Host ("  release    {0}" -f $K8sReleaseName)
        Write-Host ''
        $ans = Read-Host "Install into this cluster? [y/N]"
        if ($ans -notmatch '^(y|yes)$') {
            Write-Err2 "Cancelled."
            exit 1
        }
    }
}

# ── https scheme ──────────────────────────────────────────────────────────

# ``cremind serve`` puts TLS on the public origin (port 1515) when
# CREMIND_SSL is set (``auto`` generates a locally-signed pair) or when an
# explicit CREMIND_SSL_CERTFILE is configured. The env TEMPLATES ship those
# keys commented out and the installer appends the resolved value below, so
# only an uncommented assignment carrying a value counts — a leading '#' has
# to read as "off", or a plain-HTTP install would be handed an https:// URL
# that nothing is listening on.
#
# -EnvPath is the env file this install just wrote — optional, and it may
# not exist yet. Called with no argument (or a path that isn't there) the
# answer comes from the process environment alone, which is what the
# pre-write decision below needs. The environment wins in either case when
# it already carries the setting: ``docker compose`` interpolates
# CREMIND_SSL from the invoking shell, and the native path copies the .env
# into Env: before spawning the server.
#
# Note the internal API port (PORT, default 1112) stays plain HTTP even
# with TLS on — it binds loopback only, for the CLI and the skills. This
# helper is about the PUBLIC origin, so don't reach for it there.
#
# Only a mode the server recognises as enabled reads as HTTPS here.
# ``true``/``1``/``yes`` are compatibility aliases for ``auto``;
# ``false``/``0``/``no``/``none`` mean HTTP. Keep this aligned with
# app.config.tls_mode.effective_ssl_mode.
function Normalize-CremindSslMode {
    param([AllowEmptyString()][string] $Mode = '')
    $value = "$Mode".Trim().ToLowerInvariant()
    if ($value -in @('true', '1', 'yes')) { return 'auto' }
    if ($value -in @('false', '0', 'no', 'none')) { return '' }
    return $value
}

function Test-CremindSslModeEnabled {
    param([AllowEmptyString()][string] $Mode = '')
    return (Normalize-CremindSslMode $Mode) -in @('auto', 'after-setup')
}

function Get-CremindEnvValue {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Key
    )
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $pattern = '^\s*(?:export\s+)?' + [regex]::Escape($Key) + '\s*=(.*)$'
    foreach ($line in (Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)) {
        $text = "$line"
        if ($text.TrimStart().StartsWith('#')) { continue }
        if ($text -match $pattern) { return $Matches[1].Trim() }
    }
    return ''
}

function Get-CremindScheme {
    param([string] $EnvPath = '')
    $sslMode = $env:CREMIND_SSL
    $sslCert = $env:CREMIND_SSL_CERTFILE
    if ($EnvPath -and (Test-Path $EnvPath)) {
        foreach ($line in (Get-Content $EnvPath -ErrorAction SilentlyContinue)) {
            $t = "$line".Trim()
            if (-not $t -or $t.StartsWith('#')) { continue }
            if ((-not $sslMode) -and $t -match '^CREMIND_SSL\s*=(.*)$') {
                $sslMode = $Matches[1].Trim()
            }
            if ((-not $sslCert) -and $t -match '^CREMIND_SSL_CERTFILE\s*=(.*)$') {
                $sslCert = $Matches[1].Trim()
            }
        }
    }
    if ((Test-CremindSslModeEnabled $sslMode) -or $sslCert) { return 'https' }
    return 'http'
}

# The scheme this server answers on RIGHT NOW, as opposed to the steady state
# Get-CremindScheme reports.
#
# ``CREMIND_SSL=after-setup`` serves plain HTTP until the Setup Wizard writes
# bootstrap.toml, then restarts into https. Everything the installer WRITES —
# APP_URL, CORS_ALLOWED_ORIGINS, credentials.toml — must say https, because
# that is the origin for the whole life of the install bar the wizard, so
# those call sites keep using Get-CremindScheme. But the two things that touch
# the freshly-started server — the health probe and the wizard URL we hand the
# user — have to match the listener that exists at this moment, or we health-
# gate an https URL against an http listener and fail the install.
#
# An explicit certificate pair overrides the phase: with CREMIND_SSL_CERTFILE
# set there is nothing to defer and the server binds TLS immediately.
# Otherwise this is Get-CremindScheme, argument for argument.
#
# -SetupComplete says the wizard has already run (bootstrap.toml exists), which
# ends the deferral: the server binds TLS from boot one, exactly as
# ``_resolve_tls`` decides it (app/server.py). Re-running the installer over a
# finished install is the case that needs this — without it the health gate
# probes http:// against a TLS listener, waits out its full budget, and hands
# the user a wizard URL that cannot load. Caller-supplied rather than tested
# here because only the caller knows where to look: the native path can see
# the file on the host, while a Docker install keeps it inside the
# cremind-data volume and has to ask the running container instead.
function Get-CremindBootScheme {
    param(
        [string] $EnvPath = '',
        [switch] $SetupComplete
    )
    $sslMode = $env:CREMIND_SSL
    $sslCert = $env:CREMIND_SSL_CERTFILE
    if ($EnvPath -and (Test-Path $EnvPath)) {
        foreach ($line in (Get-Content $EnvPath -ErrorAction SilentlyContinue)) {
            $t = "$line".Trim()
            if (-not $t -or $t.StartsWith('#')) { continue }
            if ((-not $sslMode) -and $t -match '^CREMIND_SSL\s*=(.*)$') {
                $sslMode = $Matches[1].Trim()
            }
            if ((-not $sslCert) -and $t -match '^CREMIND_SSL_CERTFILE\s*=(.*)$') {
                $sslCert = $Matches[1].Trim()
            }
        }
    }
    $sslMode = Normalize-CremindSslMode $sslMode
    if ((-not $sslCert) -and $sslMode -eq 'after-setup' -and -not $SetupComplete) { return 'http' }
    if ((Test-CremindSslModeEnabled $sslMode) -or $sslCert) { return 'https' }
    return 'http'
}

# Health probe for a URL built by Get-CremindScheme. CREMIND_SSL=auto serves a
# certificate signed by a CA that is in no trust store yet, so the handshake —
# not the health endpoint — is what would fail. The probe only needs to know
# the listener answers, not to authenticate it. Plain HTTP takes the exact
# call it always had, untouched.
function Test-CremindHealth {
    param(
        [Parameter(Mandatory)][string] $Url,
        [int] $TimeoutSec = 2
    )
    if (-not $Url.StartsWith('https://')) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec $TimeoutSec | Out-Null
            return $true
        } catch {
            return $false
        }
    }
    # PowerShell 6+ has a per-request switch and needs no global state.
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec $TimeoutSec -SkipCertificateCheck | Out-Null
            return $true
        } catch {
            return $false
        }
    }
    # Windows PowerShell 5.1 has no such switch, and the obvious workaround is
    # a trap: ServerCertificateValidationCallback is invoked on the handshake
    # thread, which is not a PowerShell runspace, so a scriptblock delegate
    # never runs and EVERY https probe comes back false — a silent two-minute
    # stall waiting for a backend that is already up. The delegate has to be
    # compiled. Restored in ``finally`` so nothing else inherits it.
    if (-not ('CremindTlsBypass' -as [type])) {
        Add-Type -TypeDefinition @'
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public static class CremindTlsBypass {
    public static void Enable() {
        ServicePointManager.ServerCertificateValidationCallback =
            delegate (object s, X509Certificate c, X509Chain ch, SslPolicyErrors e) { return true; };
    }
}
'@
    }
    $savedCallback = [System.Net.ServicePointManager]::ServerCertificateValidationCallback
    [CremindTlsBypass]::Enable()
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec $TimeoutSec | Out-Null
        return $true
    } catch {
        return $false
    } finally {
        [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $savedCallback
    }
}

# Bring a rendered .env's public origin in line with a scheme. The
# local/server templates hard-code http:// on APP_URL and
# CORS_ALLOWED_ORIGINS — right for the overwhelmingly common plain-HTTP
# install, so this rewrites rather than adding a second pair of templates.
# Returns without touching the file unless TLS is on, and even then it only
# edits those two assignment lines: the surrounding comments keep their
# worked examples.
#
# -Raw in, -NoNewline out, so the file keeps the exact bytes it was written
# with: a line-by-line Get-Content/Set-Content roundtrip would re-terminate
# every line with CRLF, and Compose's env-file parser does not strip a
# trailing carriage return from a value. ``-Encoding utf8`` on both sides
# for the same reason the dev-channel CORS rewrite below needs it — Windows
# PowerShell 5.1 decodes BOM-less UTF-8 as cp1252 by default and would
# mangle the em-dashes in the template comments.
function Set-CremindEnvUrlScheme {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Scheme
    )
    if ($Scheme -ne 'https') { return }
    $text = Get-Content -Path $Path -Raw -Encoding utf8
    # ``.`` never matches \n, so ^...$ under (?m) is one line — and any \r
    # sits inside the match and is re-emitted with it.
    $text = [regex]::Replace($text, '(?m)^(APP_URL|CORS_ALLOWED_ORIGINS)=.*$', {
        param($m) $m.Value -replace 'http://', 'https://'
    })
    Set-Content -Path $Path -Value $text -NoNewline -Encoding utf8
}

# The inverse, for ``-Ssl none`` against a .env that a previous install left
# on https. Kept separate rather than folded in as an else-branch because
# every other caller of Set-CremindEnvUrlScheme relies on 'http' being a
# no-op: the templates ship http:// URLs, and a plain-HTTP install must not
# start rewriting a file it has no opinion about.
function Set-CremindEnvUrlSchemeDowngrade {
    param([Parameter(Mandatory)][string] $Path)
    $text = Get-Content -Path $Path -Raw -Encoding utf8
    $text = [regex]::Replace($text, '(?m)^(APP_URL|CORS_ALLOWED_ORIGINS)=.*$', {
        param($m) $m.Value -replace 'https://', 'http://'
    })
    Set-Content -Path $Path -Value $text -NoNewline -Encoding utf8
}

# Replace an existing uncommented ``Key=...`` line, or append one. Used only
# on a .env the installer is deliberately amending (the -Ssl keep branch);
# the ordinary path appends to a file it just wrote from a template.
#
# Same -Raw/-NoNewline/-Encoding utf8 roundtrip as the scheme rewrite above,
# for the same reason: a line-by-line Get-Content/Set-Content pass would
# re-terminate every line with CRLF and mangle the templates' em-dashes.
function Set-CremindEnvKey {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Key,
        [Parameter(Mandatory)][AllowEmptyString()][string] $Value
    )
    $text = Get-Content -Path $Path -Raw -Encoding utf8
    $pattern = '(?m)^' + [regex]::Escape($Key) + '\s*=.*$'
    if ([regex]::IsMatch($text, $pattern)) {
        # Scriptblock, not a replacement string: a literal '$' in the value
        # (rare in a hostname, but not impossible) would otherwise be read as
        # a group reference and silently rewrite the line.
        $line = "$Key=$Value"
        $text = [regex]::Replace($text, $pattern, { param($m) $line })
    } else {
        if ($text -and -not $text.EndsWith("`n")) { $text += "`r`n" }
        $text += "$Key=$Value`r`n"
    }
    Set-Content -Path $Path -Value $text -NoNewline -Encoding utf8
}

# ``CREMIND_SSL`` in a kept .env, set to $Mode. An empty $Mode writes an
# empty assignment rather than deleting the line: Get-CremindScheme treats a
# present-but-empty value as "off" (it Trims and tests for truthiness), and
# leaving an explicit ``CREMIND_SSL=`` behind is also what tells the NEXT
# re-install that plain HTTP was a decision, not an absence.
function Set-CremindEnvSslMode {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][AllowEmptyString()][string] $Mode
    )
    Set-CremindEnvKey -Path $Path -Key 'CREMIND_SSL' -Value $Mode
}

# ── ssl mode ──────────────────────────────────────────────────────────────
#
# Resolve the TLS mode BEFORE the scheme decision below, because that
# decision reads $env:CREMIND_SSL and every file written afterwards has to
# agree with it. Setting the process variable is deliberately the whole
# propagation mechanism: Get-CremindScheme reads it, ``docker compose``
# interpolates it into the container, and the native path re-asserts it from
# the .env it stamps — no call site below needs to know this block exists.
#
# Precedence, highest first:
#
#   1. -Ssl on the command line. ``none`` is a real answer, not "unset": it
#      clears an inherited CREMIND_SSL so the opt-out can't be undone by a
#      variable left in the shell.
#   2. An inherited $env:CREMIND_SSL / certificate pair — the pre-flag
#      way of asking, still honoured.
#   3. The previous install's choice. Docker regenerates its bundle every
#      run, so the old docker\.env is read before it's overwritten; a native
#      .env is kept as-is and carries itself. An install that predates this
#      flag has no CREMIND_SSL line and reads as "http", which is what it
#      was — upgrades never flip scheme behind the user's back.
#   4. Fresh installs use HTTP. Interactive installs offer Enable HTTPS,
#      which selects after-setup; unattended installs require -Ssl to opt in.
#      The TUI only asks for fresh installs, after the install mode is known.
#      Electron uses the same choices and precedence as browser installs.
$PreviousSslEnv = if ($Mode -eq 'docker') {
    Join-Path (Join-Path $CremindInstallDir 'docker') '.env'
} elseif ($Mode -eq 'kubernetes') {
    # The Helm release's own record. It stores the key as CREMIND_SSL exactly
    # so the machinery below works unchanged.
    $K8sReleaseEnv
} else {
    $EnvFile
}
$InheritedSslMode = "$env:CREMIND_SSL"
$InheritedSslCertFile = "$env:CREMIND_SSL_CERTFILE"
$InheritedSslKeyFile = "$env:CREMIND_SSL_KEYFILE"
$InheritedSslKeyFilePassword = "$env:CREMIND_SSL_KEYFILE_PASSWORD"
$InheritedSslAutoHosts = "$env:CREMIND_SSL_AUTO_HOSTS"
# Retain whether the process supplied a transport override after the
# environment is normalized below. Native installs must persist this choice
# just like the regenerated Docker bundle does.
$SslEnvironmentExplicit = [bool]($InheritedSslMode -or $InheritedSslCertFile -or $InheritedSslKeyFile)
$PreviousSslCertFile = Get-CremindEnvValue -Path $PreviousSslEnv -Key 'CREMIND_SSL_CERTFILE'
$PreviousSslKeyFile = Get-CremindEnvValue -Path $PreviousSslEnv -Key 'CREMIND_SSL_KEYFILE'
$PreviousSslKeyFilePassword = Get-CremindEnvValue -Path $PreviousSslEnv -Key 'CREMIND_SSL_KEYFILE_PASSWORD'
$PreviousSslAutoHosts = Get-CremindEnvValue -Path $PreviousSslEnv -Key 'CREMIND_SSL_AUTO_HOSTS'

$SslExplicit = ($Ssl -ne '')
$SslMode = ''
if ($SslExplicit) {
    $SslMode = Normalize-CremindSslMode $Ssl
} elseif ($env:CREMIND_SSL -or $env:CREMIND_SSL_CERTFILE -or $env:CREMIND_SSL_KEYFILE) {
    $SslMode = "$env:CREMIND_SSL"
} else {
    # $null distinguishes "no previous install to learn from" (fall through
    # to the default) from "previous install said plain HTTP" (keep it).
    $PrevSslMode = $null
    if (Test-Path -LiteralPath $PreviousSslEnv) {
        $PrevSslMode = ''
        foreach ($line in (Get-Content -LiteralPath $PreviousSslEnv -ErrorAction SilentlyContinue)) {
            $t = "$line".Trim()
            if (-not $t -or $t.StartsWith('#')) { continue }
            if ($t -match '^CREMIND_SSL\s*=(.*)$') {
                $PrevSslMode = $Matches[1].Trim()
                break
            }
        }
    }
    if ($null -ne $PrevSslMode) {
        $SslMode = $PrevSslMode
    } else {
        $SslMode = ''
        if (-not $Unattended) {
            Write-Host ""
            Write-Host 'HTTP is the default. HTTPS encrypts connections and enables HTTP/2.'
            Write-Host 'Setup will guide you through trusting the certificate before switching.'
            Write-Host 'You can also enable HTTPS later in Settings > Security.'
            $SslAnswer = Read-Host 'Enable HTTPS (SSL)? [y/N]'
            if ($SslAnswer -match '^(?i:y|yes)$') {
                $SslMode = 'after-setup'
                $SslExplicit = $true
            }
        }
    }
}
$SslMode = Normalize-CremindSslMode $SslMode

# Docker's template is regenerated on every install, so stage all independent
# TLS inputs before that file is replaced. A process-level transport selector
# overrides the previous bundle. An explicit -Ssl value also wins the pair.
if ($SslExplicit) {
    $ResolvedSslCertFile = ''
    $ResolvedSslKeyFile = ''
    $ResolvedSslKeyFilePassword = ''
} elseif ($InheritedSslMode -or $InheritedSslCertFile -or $InheritedSslKeyFile) {
    $ResolvedSslCertFile = $InheritedSslCertFile
    $ResolvedSslKeyFile = $InheritedSslKeyFile
    $ResolvedSslKeyFilePassword = $InheritedSslKeyFilePassword
} else {
    $ResolvedSslCertFile = $PreviousSslCertFile
    $ResolvedSslKeyFile = $PreviousSslKeyFile
    $ResolvedSslKeyFilePassword = $PreviousSslKeyFilePassword
}
$ResolvedSslAutoHosts = if ($InheritedSslAutoHosts) { $InheritedSslAutoHosts } else { $PreviousSslAutoHosts }
if ($SslMode) {
    $env:CREMIND_SSL = $SslMode
} elseif ($SslExplicit -or $InheritedSslMode) {
    # An explicit ``none`` has to beat the environment, or Get-CremindScheme
    # still answers https from a leftover variable and the opt-out is a lie.
    if ($SslExplicit -and $env:CREMIND_SSL_CERTFILE) {
        Write-Warn2 "-Ssl none: ignoring the inherited CREMIND_SSL_CERTFILE — this install serves plain HTTP."
    }
    Remove-Item Env:CREMIND_SSL -ErrorAction SilentlyContinue
}
Remove-Item Env:CREMIND_SSL_CERTFILE -ErrorAction SilentlyContinue
Remove-Item Env:CREMIND_SSL_KEYFILE -ErrorAction SilentlyContinue
Remove-Item Env:CREMIND_SSL_KEYFILE_PASSWORD -ErrorAction SilentlyContinue
Remove-Item Env:CREMIND_SSL_AUTO_HOSTS -ErrorAction SilentlyContinue
if ($ResolvedSslCertFile) { $env:CREMIND_SSL_CERTFILE = $ResolvedSslCertFile }
if ($ResolvedSslKeyFile) { $env:CREMIND_SSL_KEYFILE = $ResolvedSslKeyFile }
if ($ResolvedSslKeyFilePassword) { $env:CREMIND_SSL_KEYFILE_PASSWORD = $ResolvedSslKeyFilePassword }
if ($ResolvedSslAutoHosts) { $env:CREMIND_SSL_AUTO_HOSTS = $ResolvedSslAutoHosts }

# ── boot service ──────────────────────────────────────────────────────────
#
# Whether to register a logon Scheduled Task that starts ``cremind serve`` and
# restarts it when it exits. Resolved here, next to the ssl block, because
# both are settings the .env carries forward; Electron manages its own
# backend lifecycle.
#
# Default ON for native installs: without a supervisor the in-app restart and the after-setup
# HTTPS switch leave the server down, which reads as a bug rather than as a
# missing feature.
#
# The persistence rule differs from -Ssl on purpose. -Ssl treats a missing
# marker as "no previous choice"; here a missing CREMIND_BOOT_SERVICE means an
# install that predates this feature, and those should GAIN the service on
# upgrade. So only a literal 'disabled' sticks.
if ($env:CREMIND_INSTALLER_FRONTEND -eq 'electron') {
    if ($BootService) {
        Write-Warn2 "-BootService ignored: the desktop app starts and stops the backend itself."
    }
    $BootServiceOn = $false
} elseif ($Mode -eq 'docker') {
    if ($BootService) {
        Write-Warn2 "-BootService ignored: docker restarts the container for you."
    }
    $BootServiceOn = $false
} elseif ($Mode -eq 'kubernetes') {
    if ($BootService) {
        Write-Warn2 "-BootService ignored: kubelet restarts the pod for you."
    }
    $BootServiceOn = $false
} elseif ($BootExplicit) {
    $BootServiceOn = [bool]$BootService
} else {
    $BootServiceOn = $true
    if (Test-Path -LiteralPath $EnvFile) {
        foreach ($line in (Get-Content -LiteralPath $EnvFile -ErrorAction SilentlyContinue)) {
            $t = "$line".Trim()
            if ($t -match '^CREMIND_BOOT_SERVICE\s*=\s*disabled\s*$') {
                $BootServiceOn = $false
                break
            }
        }
    }
}
$BootMarker = if ($BootServiceOn) { 'enabled' } else { 'disabled' }

# The scheme of the public origin, decided ONCE here — before the first
# file is written. Everything the installer bakes a public URL into has to
# agree with it, not just the closing banner: APP_URL feeds the A2A agent
# card and the OAuth redirect derivation (the server logs a boot warning
# when TLS is on and APP_URL is http://), CORS_ALLOWED_ORIGINS has to match
# the origin the browser actually loads the SPA from, and credentials.toml
# copies both.
#
# No env file exists at this point, so the process environment is the only
# thing that can carry the setting — which is exactly what the block above
# just settled. Plain HTTP → 'http', and every file and line below is
# exactly what it was before TLS existed.
$UrlScheme = Get-CremindScheme

# 'custom' carries an operator-supplied public URL and CORS list rather
# than a preset the installer composes. They're still written through
# verbatim — except that TLS on the public origin makes an http:// origin
# simply wrong: the listener speaks TLS, and a page served from an https
# origin can't call an http:// API. Upgrade the scheme in place and keep
# the host, port and path the operator picked.
if ($Deployment -eq 'custom' -and $UrlScheme -eq 'https') {
    $CustomValues['public_url']      = $CustomValues['public_url']      -replace 'http://', 'https://'
    $CustomValues['allowed_origins'] = $CustomValues['allowed_origins'] -replace 'http://', 'https://'
}

# ── host-side CA trust ─────────────────────────────────────
#
# The CA lives inside the container (Docker) or the pod (Kubernetes), where
# nothing can reach the HOST's trust store - but this script runs on the host,
# so it can. This is the containerised counterpart of the wizard's one-click
# trust (which the server can only offer on native installs): download
# /ca.pem from the running server and offer to add it to the current user's
# Trusted Root store. Declining is fine - the wizard's "Secure this install"
# step shows the manual command, and every OTHER device needs that path
# anyway.
#
# Both listener phases serve /ca.pem: under after-setup it is plain http, and
# a finished install answers https - which Invoke-WebRequest accepts via
# Schannel exactly when this host already trusts the CA from a previous run
# (and then the thumbprint check below skips the prompt). A host that never
# trusted it gets a failed download and a pointer, not a failed install.
#
# Callers decide WHETHER to offer (TLS on, interactive, not Electron); this
# decides how.
function Invoke-HostCaTrust {
    param([Parameter(Mandatory)][string] $CaUrl)
    $CaTmp = Join-Path $env:TEMP 'cremind-local-ca.pem'
    $CaCert = $null
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $CaUrl -OutFile $CaTmp -TimeoutSec 10 | Out-Null
        $CaCert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($CaTmp)
    } catch {
        # No local CA (operator certificate pair → /ca.pem 404s, and there
        # is genuinely nothing of ours to trust) or an untrusted https
        # listener — either way the wizard step has it covered.
        Write-Info "Skipping host CA trust ($($_.Exception.Message.Trim()))."
    }
    if ($CaCert) {
        $CaStore = [System.Security.Cryptography.X509Certificates.X509Store]::new('Root', 'CurrentUser')
        $CaStore.Open('ReadWrite')
        try {
            $found = $CaStore.Certificates.Find('FindByThumbprint', $CaCert.Thumbprint, $false)
            if ($found.Count -gt 0) {
                Write-Ok "This machine already trusts the Cremind local CA."
            } else {
                $CaSha256 = [System.BitConverter]::ToString(
                    [System.Security.Cryptography.SHA256]::Create().ComputeHash($CaCert.RawData)
                ) -replace '-', ':'
                Write-Host ""
                Write-Host "Cremind will serve HTTPS with a certificate signed by its own local CA."
                Write-Host "Trusting that CA now removes the browser warning on this machine; every"
                Write-Host "other device gets the same walkthrough in the Setup Wizard."
                Write-Host "  Subject : $($CaCert.Subject)"
                Write-Host "  SHA-256 : $CaSha256"
                $TrustAnswer = Read-Host "Add it to the current user's Trusted Root store? Windows asks you to confirm. [Y/n]"
                if ($TrustAnswer -notmatch '^[nN]') {
                    try {
                        $CaStore.Add($CaCert)
                        Write-Ok "Trusted the Cremind local CA for the current user."
                    } catch {
                        # Includes the user clicking No on the Windows dialog.
                        Write-Warn2 "CA not trusted ($($_.Exception.Message.Trim())). The Setup Wizard's 'Secure this install' step shows the manual command."
                    }
                } else {
                    Write-Info "Skipped. The Setup Wizard's 'Secure this install' step covers it."
                }
            }
        } finally { $CaStore.Dispose() }
    }
    Remove-Item $CaTmp -ErrorAction SilentlyContinue
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

# ── kubernetes install ────────────────────────────────────────────────────
#
# Installs the Cremind Helm chart into the kubeconfig context the operator
# chose. Nothing about this install is ambient: every helm and kubectl call
# carries --kube-context, so a stale current-context can never redirect it.
#
# What this branch does NOT do, deliberately: write a host .env, register a
# boot service, or run migrations. The chart owns the pod's environment
# (INSTALL_MODE=kubernetes, SETUP_WIZARD_ENV=kubernetes, APP_URL), kubelet
# supervises the pod, and the Setup Wizard runs the first migration.
if ($Mode -eq 'kubernetes') {
    Write-Step "Kubernetes install"

    if (-not (Test-Path -LiteralPath $K8sDir)) {
        New-Item -ItemType Directory -Path $K8sDir -Force | Out-Null
    }

    # Every kubectl call goes through these. --request-timeout keeps an
    # unreachable API server (or an exec credential plugin waiting on a
    # browser) from looking like a frozen installer.
    function Invoke-Kubectl {
        param([string[]] $KubectlArgs)
        return Invoke-NativeCapture -FilePath $KubectlExe `
            -ArgumentList (@('--context', $KubeContext, '--request-timeout=20s') + $KubectlArgs)
    }
    function Invoke-KubectlNs {
        param([string[]] $KubectlArgs)
        return Invoke-Kubectl -KubectlArgs (@('--namespace', $KubeNamespace) + $KubectlArgs)
    }

    $HelmRelease = if ($K8sReleaseName) { $K8sReleaseName } else { 'cremind' }

    # A host tracks one release. Re-running against a different target would
    # leave the previous one behind with nothing recording it, so say so.
    $prevRelease = Get-PrevK8s 'HELM_RELEASE'
    if ($prevRelease) {
        if ((Get-PrevK8s 'KUBE_CONTEXT') -ne $KubeContext -or
            (Get-PrevK8s 'KUBE_NAMESPACE') -ne $KubeNamespace -or
            $prevRelease -ne $HelmRelease) {
            Write-Warn2 "This machine already tracks a Cremind release:"
            Write-Warn2 "  $prevRelease in namespace $(Get-PrevK8s 'KUBE_NAMESPACE') on context $(Get-PrevK8s 'KUBE_CONTEXT')"
            Write-Warn2 "You are about to install $HelmRelease in $KubeNamespace on $KubeContext."
            if ($Unattended -or -not [Environment]::UserInteractive) {
                Write-Err2 "Refusing to orphan the tracked release. Run -Uninstall first, or pass the same target."
                exit 2
            }
            Write-Host ''
            Write-Host 'The previous release will keep running, untracked by this installer.'
            $ans = Read-Host "Continue anyway? [y/N]"
            if ($ans -notmatch '^(y|yes)$') { Write-Err2 "Cancelled."; exit 1 }
        }
    }

    $serverNote = if ($KubeServer) { " ($KubeServer)" } else { '' }
    Write-Info "Target: context $KubeContext$serverNote, namespace $KubeNamespace, release $HelmRelease"

    # Pre-flight: can we actually reach this cluster? Better here than three
    # minutes into a helm install.
    if ((Invoke-Kubectl -KubectlArgs @('cluster-info')).ExitCode -ne 0) {
        Write-Err2 "Cannot reach the cluster for context '$KubeContext'$serverNote."
        Write-Err2 "Check your kubeconfig and VPN, then re-run. Details: $LogFile"
        exit 1
    }
    Write-Ok "Cluster reachable."

    # ── version + chart reference ─────────────────────────────────────────
    #
    # The image tag is a PEP 440 version (0.0.17rc13.dev1); the chart version
    # is its SemVer2 spelling (0.0.17-rc.13.dev.1). One release, two
    # spellings, and Helm rejects the PEP 440 form - so the translation runs
    # through app/upgrade/channel.py, the same file that already owns every
    # other version rule here.
    #
    # dev is the odd one: there is no published dev chart, so it installs the
    # checkout's chart against the newest test-channel IMAGE. (The pod then
    # reports the test channel in-app, because the chart derives the channel
    # from the image tag.)
    if ($Channel -eq 'dev') {
        if ($VersionRaw) {
            $CremindVer = $VersionRaw
        } else {
            Write-Info "Resolving the newest published image for the local chart"
            try {
                $k8sIndex = (Invoke-WebRequest -UseBasicParsing -Uri 'https://test.pypi.org/simple/cremind/').Content
            } catch {
                Write-Err2 "Failed to fetch https://test.pypi.org/simple/cremind/ : $_"
                exit 1
            }
            $CremindVer = Resolve-CremindWheel -IndexBody $k8sIndex -ResolveChannel 'test' -Emit 'version'
            if (-not $CremindVer) {
                Write-Err2 "No published cremind image found to run the local chart against."
                Write-Err2 "Pass -Version <X.Y.ZrcN.devM> to pick one."
                exit 1
            }
            Write-Ok "Image tag: $CremindVer"
        }
    } else {
        $CremindVer = Resolve-CremindVersion
    }

    $ChartVersion = ''
    $ChartIsLocal = $false
    if ($HelmChart) {
        $ChartRef = $HelmChart
        if (Test-Path -LiteralPath $ChartRef -PathType Container) { $ChartIsLocal = $true }
    } elseif ($Channel -eq 'dev') {
        $ChartRef = Join-Path $RepoRoot 'helm\cremind'
        $ChartIsLocal = $true
        if (-not (Test-Path -LiteralPath (Join-Path $ChartRef 'Chart.yaml'))) {
            Write-Err2 "-Channel dev needs the chart at $ChartRef, which is missing."
            exit 1
        }
    } else {
        $ChartRef = 'oci://registry-1.docker.io/cremind/cremind'
    }

    if ($ChartRef.StartsWith('oci://')) {
        # A production X.Y.Z is already SemVer2; only the RC form needs
        # translating, which is the only case that needs python here.
        if ($Channel -eq 'production') {
            $ChartVersion = $CremindVer
        } else {
            $resolverPy = Get-ResolverPython
            $resolver = Get-CremindResolver
            $chartOut = Invoke-NativeCapture -FilePath $resolverPy `
                -ArgumentList @($resolver, 'chart-version', '--version', $CremindVer)
            if ($chartOut.ExitCode -ne 0 -or -not $chartOut.Stdout.Trim()) {
                Write-Err2 "Could not derive a chart version from '$CremindVer'."
                exit 1
            }
            $ChartVersion = $chartOut.Stdout.Trim()
        }
        Write-Info "Chart: $ChartRef --version $ChartVersion"
        # Fail here, with a useful message, rather than inside helm.
        $shown = Invoke-NativeCapture -FilePath 'helm' `
            -ArgumentList @('show', 'chart', $ChartRef, '--version', $ChartVersion)
        if ($shown.ExitCode -ne 0) {
            Write-Err2 "Chart $ChartVersion is not published at $ChartRef."
            Write-Err2 "Release-candidate charts appear a few minutes after the tag; check 'helm show chart $ChartRef --devel'."
            exit 1
        }
    } else {
        Write-Info "Chart: $ChartRef (local)"
    }

    if ($ChartIsLocal) {
        # A checkout's chart has unresolved subchart dependencies and a
        # placeholder appVersion, so the image tag must be pinned explicitly.
        Write-Info "Building chart dependencies"
        Invoke-NativeCapture -FilePath 'helm' -ArgumentList @('repo', 'add', '--force-update', 'qdrant', 'https://qdrant.github.io/qdrant-helm') | Out-Null
        Invoke-NativeCapture -FilePath 'helm' -ArgumentList @('repo', 'add', '--force-update', 'chromadb', 'https://amikos-tech.github.io/chromadb-chart/') | Out-Null
        $dep = Invoke-NativeCapture -FilePath 'helm' -ArgumentList @('dependency', 'build', $ChartRef)
        if ($dep.ExitCode -ne 0) {
            Write-Err2 "helm dependency build failed for $ChartRef. Details: $LogFile"
            exit 1
        }
    }

    # ── Postgres password ─────────────────────────────────────────────────
    #
    # Pinned rather than left to the subchart's generator, because the
    # generated one lives only in a Secret: uninstall + reinstall regenerates
    # it while the retained data volume keeps the OLD password, and setup then
    # fails with "password authentication failed for user cremind".
    #
    # Pinning has its own trap in the other direction: the Bitnami helper
    # honours a provided password, so pinning a FRESH one onto an existing
    # release rewrites the Secret and locks the app out of its own database.
    # Hence: adopt what the release already uses before generating anything.
    $PgPassword = ''
    $K8sExternalPg = ",$K8sExtraSet," -like '*,postgresql.enabled=false,*'
    $ReleaseExists = (Invoke-NativeCapture -FilePath 'helm' -ArgumentList @(
        'status', $HelmRelease, '--kube-context', $KubeContext, '--namespace', $KubeNamespace
    )).ExitCode -eq 0
    if (-not $K8sExternalPg) {
        if ($K8sPostgresPassword) {
            $PgPassword = $K8sPostgresPassword
            Write-Info "Using the Postgres password from -K8sPostgresPassword."
        } elseif (Get-PrevK8s 'PG_PASSWORD') {
            $PgPassword = Get-PrevK8s 'PG_PASSWORD'
        } elseif ($ReleaseExists) {
            # Release we did not install (or release.env was lost): adopt the
            # live Secret so the running database keeps working.
            $secretName = (Invoke-KubectlNs -KubectlArgs @(
                'get', 'secret',
                '-l', "app.kubernetes.io/name=postgresql,app.kubernetes.io/instance=$HelmRelease",
                '-o', 'jsonpath={.items[0].metadata.name}'
            )).Stdout.Trim()
            if (-not $secretName) { $secretName = 'cremind-postgresql' }
            $pgB64 = (Invoke-KubectlNs -KubectlArgs @(
                'get', 'secret', $secretName, '-o', 'jsonpath={.data.password}'
            )).Stdout.Trim()
            if ($pgB64) {
                try {
                    $PgPassword = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($pgB64))
                    Write-Info "Adopted the existing PostgreSQL password from the release."
                } catch { $PgPassword = '' }
            }
            if (-not $PgPassword) {
                Write-Err2 "Release $HelmRelease exists but its PostgreSQL password could not be read."
                Write-Err2 "Pass -K8sPostgresPassword <password>, or uninstall and start clean."
                exit 1
            }
        } else {
            # No release. A leftover data volume from a previous one still has
            # its own password, and nothing here can guess it.
            $pgPvc = (Invoke-KubectlNs -KubectlArgs @(
                'get', 'pvc',
                '-l', "app.kubernetes.io/name=postgresql,app.kubernetes.io/instance=$HelmRelease",
                '-o', 'jsonpath={.items[0].metadata.name}'
            )).Stdout.Trim()
            if (-not $pgPvc) {
                $pgPvc = (Invoke-KubectlNs -KubectlArgs @(
                    'get', 'pvc', 'data-cremind-postgresql-0', '--ignore-not-found',
                    '-o', 'jsonpath={.metadata.name}'
                )).Stdout.Trim()
            }
            if ($pgPvc) {
                Write-Err2 "A PostgreSQL data volume ($pgPvc) survives from an earlier release,"
                Write-Err2 "and its password is not recorded on this machine. A fresh install would"
                Write-Err2 "write a new password that the retained database does not accept."
                Write-Err2 "Either delete it:"
                Write-Err2 "  kubectl --context $KubeContext -n $KubeNamespace delete pvc $pgPvc"
                Write-Err2 "or re-run with -K8sPostgresPassword <the old password>."
                exit 1
            }
            $PgPassword = New-Secret
        }
    }

    # ── VNC password ──────────────────────────────────────────────────────
    $VncPwd = ''
    $VncGenerated = $false
    if ($DesktopUi -ne '0') {
        $VncPwd = if ($VncPassword) { $VncPassword }
                  elseif (Get-PrevK8s 'VNC_PASSWORD') { Get-PrevK8s 'VNC_PASSWORD' }
                  else { '' }
        if (-not $VncPwd) {
            $VncPwd = (New-Secret).Substring(0, 8)
            $VncGenerated = $true
        }
    }

    # ── app URL ───────────────────────────────────────────────────────────
    #
    # Left unset unless the operator named one: the chart derives
    # http://localhost:1515 (or https:// under cremind.ssl) by itself, which
    # is exactly right for the port-forward - and an explicit http:// value is
    # what makes a later -Ssl re-run fail the chart's own validation.
    if ($K8sAppUrl -and $SslMode -and $K8sAppUrl.StartsWith('http://')) {
        Write-Err2 "-K8sAppUrl is http:// but -Ssl $SslMode serves HTTPS."
        Write-Err2 "Use https://, or leave it blank so the chart derives it."
        exit 2
    }

    # Combinations the chart refuses to render. Checking them here turns a
    # Go template error into a sentence.
    $K8sIngress = $false
    if ((",$K8sExtraSet," -like '*,ingress.enabled=true,*')) {
        if ($SslMode) {
            Write-Err2 "ingress.enabled and -Ssl are mutually exclusive: an ingress controller"
            Write-Err2 "speaks plain HTTP to the pod and cannot re-encrypt to Cremind's private CA."
            Write-Err2 "Terminate TLS at the ingress instead, and drop -Ssl."
            exit 2
        }
        $K8sIngress = $true
    }
    if ($K8sExtraSet -like '*CREMIND_DB_PROVIDER*') {
        Write-Err2 "CREMIND_DB_PROVIDER must not be set on Kubernetes: it would skip the Setup Wizard."
        exit 2
    }
    if ($K8sExtraSet -match 'replicaCount=(\d+)' -and $Matches[1] -ne '1') {
        Write-Err2 "Cremind runs as exactly one pod; the chart rejects any other replicaCount."
        exit 2
    }

    # ── values the installer owns ─────────────────────────────────────────
    #
    # Rendered into a values file rather than a wall of --set arguments. Same
    # manifests either way, but: helm's --set parser treats commas as
    # separators (and the VNC charset contains one), the two secrets stay off
    # the process list and out of install.log, and the operator gets a file
    # they can keep using with plain `helm upgrade -f`.
    $K8sValuesFile = Join-Path $K8sDir 'values.yaml'
    function ConvertTo-YamlScalar {
        param([string] $Value)
        return '"' + ($Value -replace '\\', '\\\\' -replace '"', '\"') + '"'
    }

    $valuesLines = [System.Collections.Generic.List[string]]::new()
    $valuesLines.Add('# Generated by the Cremind installer. Regenerated on every run.')
    $valuesLines.Add("# Safe to reuse by hand: helm upgrade --install $HelmRelease $ChartRef -f $K8sValuesFile")
    $valuesLines.Add('desktop:')
    $valuesLines.Add($(if ($DesktopUi -eq '0') { '  enabled: false' } else { '  enabled: true' }))
    $valuesLines.Add('cremind:')
    $valuesLines.Add("  ssl: $(ConvertTo-YamlScalar $(if ($SslMode) { $SslMode } else { 'none' }))")
    if ($K8sAppUrl) { $valuesLines.Add("  appUrl: $(ConvertTo-YamlScalar $K8sAppUrl)") }
    if ($VncPwd)    { $valuesLines.Add("  vncPassword: $(ConvertTo-YamlScalar $VncPwd)") }
    if ($ChartIsLocal) {
        # A checkout's Chart.yaml carries a placeholder appVersion, so the
        # image tag would render as :0.0.0 without this.
        $valuesLines.Add('image:')
        $valuesLines.Add("  tag: $(ConvertTo-YamlScalar $CremindVer)")
    }
    if (-not $K8sExternalPg) {
        $valuesLines.Add('postgresql:')
        $valuesLines.Add('  auth:')
        $valuesLines.Add("    password: $(ConvertTo-YamlScalar $PgPassword)")
        if ($K8sLegacyPostgresImage -ne 'no') {
            # Bitnami froze its free images into the bitnamilegacy namespace,
            # so the chart's default no longer pulls.
            $valuesLines.Add('  image:')
            $valuesLines.Add('    registry: docker.io')
            $valuesLines.Add('    repository: bitnamilegacy/postgresql')
        }
        if ($K8sDeletePostgresData -eq 'yes') {
            $valuesLines.Add('  primary:')
            $valuesLines.Add('    persistentVolumeClaimRetentionPolicy:')
            $valuesLines.Add('      enabled: true')
            $valuesLines.Add('      whenDeleted: Delete')
        }
    }
    Write-Info "Writing $K8sValuesFile"
    Write-Utf8NoBomFile -Path $K8sValuesFile -Content (($valuesLines -join "`n") + "`n")

    # ── install ───────────────────────────────────────────────────────────
    #
    # No --wait (it hides a Pending Postgres volume behind a generic timeout),
    # no --atomic (a rollback on a slow first image pull would delete the
    # chart-owned PVCs), no --reuse-values (every value is re-sent from the
    # values file above, so nothing silently carries over), and no --devel
    # (an exact --version already bypasses the prerelease filter).
    if ($ReleaseExists -and $Reinstall) {
        Write-Info "Removing release $HelmRelease (-Reinstall)"
        Invoke-NativeCapture -FilePath 'helm' -ArgumentList @(
            'uninstall', $HelmRelease, '--kube-context', $KubeContext,
            '--namespace', $KubeNamespace, '--wait') | Out-Null
        $ReleaseExists = $false
    } elseif ($ReleaseExists) {
        Write-Info "Release $HelmRelease already exists - upgrading it in place."
    }

    # Did the namespace exist before us? Only what we created may be deleted
    # again by -Uninstall -Purge.
    $NamespaceCreated = '0'
    $nsSeen = (Invoke-Kubectl -KubectlArgs @('get', 'namespace', $KubeNamespace, '--ignore-not-found', '-o', 'name')).Stdout.Trim()
    if (-not $nsSeen) {
        $NamespaceCreated = '1'
    } elseif ((Get-PrevK8s 'NAMESPACE_CREATED') -eq '1' -and (Get-PrevK8s 'KUBE_NAMESPACE') -eq $KubeNamespace) {
        $NamespaceCreated = '1'
    }

    Write-Info "Installing the chart (this pulls images; give it a few minutes)"
    $helmArgs = [System.Collections.Generic.List[string]]::new()
    $helmArgs.Add('upgrade'); $helmArgs.Add('--install')
    $helmArgs.Add($HelmRelease); $helmArgs.Add($ChartRef)
    $helmArgs.Add('--kube-context'); $helmArgs.Add($KubeContext)
    $helmArgs.Add('--namespace'); $helmArgs.Add($KubeNamespace)
    $helmArgs.Add('--create-namespace')
    $helmArgs.Add('--history-max'); $helmArgs.Add('5')
    $helmArgs.Add('-f'); $helmArgs.Add($K8sValuesFile)
    if ($ChartVersion) { $helmArgs.Add('--version'); $helmArgs.Add($ChartVersion) }
    # Last, so an operator's --set overrides the installer's own values.
    if ($K8sExtraSet) { $helmArgs.Add('--set'); $helmArgs.Add($K8sExtraSet) }
    $installed = Invoke-NativeCapture -FilePath 'helm' -ArgumentList $helmArgs.ToArray()
    if ($installed.ExitCode -ne 0) {
        Write-Err2 "helm upgrade --install failed:"
        if ($installed.Stderr) { Write-Err2 $installed.Stderr }
        if ($installed.Stdout) { Write-Err2 $installed.Stdout }
        Write-Err2 ""
        Write-Err2 "Common causes: a chart value the cluster rejects (see -K8sExtraSet),"
        Write-Err2 "an unreachable image registry, or insufficient quota."
        Write-Err2 "Inspect with: helm status $HelmRelease --kube-context $KubeContext -n $KubeNamespace"
        exit 1
    }
    Write-Ok "Chart installed."

    # ── names ─────────────────────────────────────────────────────────────
    #
    # Ask the cluster rather than recomputing the chart's fullname rule: an
    # operator's nameOverride/fullnameOverride in -K8sExtraSet would otherwise
    # silently break the rollout wait and the port-forward.
    $HelmFullname = (Invoke-KubectlNs -KubectlArgs @(
        'get', 'deploy', '-l', "app.kubernetes.io/instance=$HelmRelease",
        '-o', 'jsonpath={.items[0].metadata.name}'
    )).Stdout.Trim()
    if (-not $HelmFullname) {
        $HelmFullname = if ($HelmRelease -like '*cremind*') { $HelmRelease } else { "$HelmRelease-cremind" }
    }

    # ── wait for the rollout ──────────────────────────────────────────────
    if (-not $K8sExternalPg) {
        $pgSts = (Invoke-KubectlNs -KubectlArgs @(
            'get', 'statefulset',
            '-l', "app.kubernetes.io/name=postgresql,app.kubernetes.io/instance=$HelmRelease",
            '-o', 'jsonpath={.items[0].metadata.name}'
        )).Stdout.Trim()
        if ($pgSts) {
            Write-Info "Waiting for PostgreSQL"
            $pgRollout = Invoke-KubectlNs -KubectlArgs @('rollout', 'status', "statefulset/$pgSts", '--timeout=5m')
            if ($pgRollout.ExitCode -ne 0) {
                Write-Warn2 "PostgreSQL did not become ready within 5 minutes."
                Write-Warn2 "A Pending volume usually means the cluster has no default StorageClass:"
                Write-Warn2 "  kubectl --context $KubeContext -n $KubeNamespace get pvc"
            }
        }
    }

    Write-Info "Waiting for the Cremind pod"
    $rolloutTimeout = if ($env:CREMIND_K8S_ROLLOUT_TIMEOUT) { $env:CREMIND_K8S_ROLLOUT_TIMEOUT } else { '10m' }
    $appRollout = Invoke-KubectlNs -KubectlArgs @('rollout', 'status', "deployment/$HelmFullname", "--timeout=$rolloutTimeout")
    if ($appRollout.ExitCode -ne 0) {
        Write-Err2 "The Cremind pod did not become ready in time. Current state:"
        Write-Host (Invoke-KubectlNs -KubectlArgs @('get', 'pods', '-l', "app.kubernetes.io/instance=$HelmRelease")).Stdout
        Write-Err2 ""
        Write-Err2 "ImagePullBackOff  -> the image tag is not published, or the registry is unreachable."
        Write-Err2 "Pending           -> the node needs 2 CPU and 2Gi free for the desktop image."
        Write-Err2 "CrashLoopBackOff  -> kubectl --context $KubeContext -n $KubeNamespace logs deploy/$HelmFullname -c cremind"
        Write-Err2 ""
        Write-Err2 "The release is installed; re-running this installer upgrades it in place."
        exit 1
    }
    Write-Ok "Cremind is running."

    # ── how it is reached ─────────────────────────────────────────────────
    #
    # 1515 is the app, 1455 the transient Codex OAuth callback, 6080 noVNC -
    # the last only when the desktop image runs without the L7 proxy, which is
    # what happens under cremind.ssl. Read the Service rather than re-deriving
    # the rule.
    $svcPorts = (Invoke-KubectlNs -KubectlArgs @('get', 'svc', $HelmFullname, '-o', 'jsonpath={.spec.ports[*].port}')).Stdout.Trim()
    $PfPorts = @('1515:80', '1455:1455')
    if (" $svcPorts " -like '* 6080 *') { $PfPorts += '6080:6080' }
    $PortForwardCmd = "kubectl --context $KubeContext --namespace $KubeNamespace port-forward svc/$HelmFullname $($PfPorts -join ' ')"
    $K8sNovncUrl = (Invoke-KubectlNs -KubectlArgs @('get', 'configmap', "$HelmFullname-env", '-o', 'jsonpath={.data.CREMIND_NOVNC_URL}')).Stdout.Trim()
    $K8sPodAppUrl = (Invoke-KubectlNs -KubectlArgs @('get', 'configmap', "$HelmFullname-env", '-o', 'jsonpath={.data.APP_URL}')).Stdout.Trim()

    # ── port-forward ──────────────────────────────────────────────────────
    #
    # Started in the background so the wizard is reachable the moment this
    # script ends. An ingress install has a real address and needs none.
    $PfPidFile = Join-Path $K8sDir 'port-forward.pid'
    $PfLogFile = Join-Path $K8sDir 'port-forward.log'
    $PfErrFile = Join-Path $K8sDir 'port-forward.err.log'
    $PfRunning = $false
    if ($K8sIngress) {
        Write-Info "Ingress configured - skipping the port-forward."
    } elseif ($NoPortForward) {
        Write-Info "Skipping the port-forward (-NoPortForward)."
    } else {
        # A forward we started earlier is ours to replace; anything else on
        # 1515 is not, and a second Cremind on this host is a real possibility.
        if (Test-Path -LiteralPath $PfPidFile) {
            $oldPf = (Get-Content -LiteralPath $PfPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($oldPf) {
                $oldProc = Get-Process -Id ([int]$oldPf) -ErrorAction SilentlyContinue
                if ($oldProc -and $oldProc.ProcessName -eq 'kubectl') {
                    Stop-Process -Id ([int]$oldPf) -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 1
                }
            }
            Remove-Item -LiteralPath $PfPidFile -Force -ErrorAction SilentlyContinue
        }
        $portInUse = $false
        try {
            $probe = [System.Net.Sockets.TcpClient]::new()
            $probe.Connect('127.0.0.1', 1515)
            $portInUse = $true
            $probe.Close()
        } catch { $portInUse = $false }
        if ($portInUse) {
            Write-Warn2 "Port 1515 on this machine is already in use, so the port-forward was not started."
            Write-Warn2 "Stop whatever is listening and run:"
            Write-Warn2 "  $PortForwardCmd"
        } else {
            Write-Info "Starting the port-forward in the background"
            $pfArgs = @('--context', $KubeContext, '--namespace', $KubeNamespace,
                        'port-forward', "svc/$HelmFullname") + $PfPorts
            $pfProc = Start-Process -FilePath $KubectlExe -ArgumentList $pfArgs `
                -RedirectStandardOutput $PfLogFile -RedirectStandardError $PfErrFile `
                -WindowStyle Hidden -PassThru
            Set-Content -LiteralPath $PfPidFile -Value $pfProc.Id -Encoding ascii
            $PfRunning = $true
            Start-Sleep -Seconds 2
        }
    }

    # ── health ────────────────────────────────────────────────────────────
    $BootScheme = Get-CremindBootScheme
    $K8sHealthOk = $false
    if ($PfRunning) {
        Write-Info "Waiting for Cremind to answer on localhost:1515"
        for ($i = 0; $i -lt 60; $i++) {
            if (Test-CremindHealth -Url "${BootScheme}://localhost:1515/health") { $K8sHealthOk = $true; break }
            Start-Sleep -Seconds 2
        }
        if ($K8sHealthOk) {
            Write-Ok "Cremind is reachable at ${BootScheme}://localhost:1515"
        } else {
            Write-Warn2 "Cremind did not answer through the port-forward within 2 minutes."
            Write-Warn2 "Check it with: $PortForwardCmd"
        }
    }

    # ── CA trust ──────────────────────────────────────────────────────────
    if ($PfRunning -and $K8sHealthOk -and $UrlScheme -eq 'https' -and -not $Unattended `
        -and $env:CREMIND_INSTALLER_FRONTEND -ne 'electron') {
        Invoke-HostCaTrust -CaUrl "${BootScheme}://localhost:1515/ca.pem"
    }

    # ── state ─────────────────────────────────────────────────────────────
    #
    # What a re-run and an uninstall need. Read line by line, never
    # dot-sourced: it carries two secrets and free-form --set text.
    $releaseLines = [System.Collections.Generic.List[string]]::new()
    $releaseLines.Add('# Cremind Kubernetes release, written by the installer.')
    $releaseLines.Add('# Read by install.sh / install.ps1; never sourced.')
    $releaseLines.Add("KUBE_CONTEXT=$KubeContext")
    $releaseLines.Add("KUBE_SERVER=$KubeServer")
    $releaseLines.Add("KUBE_NAMESPACE=$KubeNamespace")
    $releaseLines.Add("NAMESPACE_CREATED=$NamespaceCreated")
    $releaseLines.Add("HELM_RELEASE=$HelmRelease")
    $releaseLines.Add("HELM_FULLNAME=$HelmFullname")
    $releaseLines.Add("CHART_REF=$ChartRef")
    $releaseLines.Add("CHART_VERSION=$ChartVersion")
    $releaseLines.Add("CREMIND_VERSION=$CremindVer")
    $releaseLines.Add("CREMIND_UPGRADE_CHANNEL=$Channel")
    $releaseLines.Add("DESKTOP_UI=$(if ($DesktopUi) { $DesktopUi } else { '1' })")
    $releaseLines.Add("VNC_PASSWORD=$VncPwd")
    $releaseLines.Add("PG_PASSWORD=$PgPassword")
    # Named CREMIND_SSL so the previous-choice machinery above reads this file
    # with no special case.
    $releaseLines.Add("CREMIND_SSL=$SslMode")
    $releaseLines.Add("APP_URL=$(if ($K8sPodAppUrl) { $K8sPodAppUrl } else { $K8sAppUrl })")
    $releaseLines.Add("PORT_FORWARD_PORTS=$($PfPorts -join ' ')")
    $releaseLines.Add("K8S_release_name=$K8sReleaseName")
    $releaseLines.Add("K8S_app_url=$K8sAppUrl")
    $releaseLines.Add("K8S_legacy_postgres_image=$K8sLegacyPostgresImage")
    $releaseLines.Add("K8S_delete_postgres_data=$K8sDeletePostgresData")
    $releaseLines.Add("K8S_extra_set=$K8sExtraSet")
    Write-Utf8NoBomFile -Path $K8sReleaseEnv -Content (($releaseLines -join "`n") + "`n")

    # credentials.toml - the single "how do I connect to X again?" file, same
    # schema app/config/credentials_file.py writes.
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $novnc = if ($K8sNovncUrl) { $K8sNovncUrl } else { 'http://localhost:1515/vnc/vnc.html' }
    $credLines = [System.Collections.Generic.List[string]]::new()
    $credLines.Add('# Cremind service credentials and connection info.')
    $credLines.Add('# Auto-generated by the installer.')
    $credLines.Add('')
    $credLines.Add("generated_at = `"$stamp`"")
    $credLines.Add('install_mode = "kubernetes"')
    $credLines.Add("cremind_version = `"$CremindVer`"")
    $credLines.Add("system_dir = `"$($CremindSystemDir -replace '\\', '\\')`"")
    $credLines.Add("install_dir = `"$($CremindInstallDir -replace '\\', '\\')`"")
    $credLines.Add('')
    $credLines.Add('[app]')
    $credLines.Add("api_url = `"${UrlScheme}://localhost:1515`"")
    $credLines.Add("spa_url = `"${UrlScheme}://localhost:1515`"")
    $credLines.Add('api_port = 1112')
    $credLines.Add('spa_port = 1515')
    $credLines.Add('cors_allowed_origins = ""')
    $credLines.Add('setup_wizard_env = "kubernetes"')
    if ($DesktopUi -ne '0') {
        $credLines.Add('')
        $credLines.Add('[desktop]')
        $credLines.Add("novnc_url = `"$novnc`"")
        $credLines.Add('novnc_port = 6080')
        $credLines.Add('vnc_port = 5900')
        $credLines.Add("vnc_password = `"$VncPwd`"")
        $credLines.Add('resolution = "1280x720"')
    }
    if (-not $K8sExternalPg) {
        $credLines.Add('')
        $credLines.Add('[postgres]')
        $credLines.Add('deployment_mode = "external"')
        $credLines.Add('host = "cremind-postgresql"')
        $credLines.Add('port = 5432')
        $credLines.Add('database = "cremind"')
        $credLines.Add('user = "cremind"')
        $credLines.Add("password = `"$PgPassword`"")
        $credLines.Add('sslmode = "prefer"')
    }
    $credLines.Add('')
    $credLines.Add('[kubernetes]')
    $credLines.Add("context = `"$KubeContext`"")
    $credLines.Add("server = `"$KubeServer`"")
    $credLines.Add("namespace = `"$KubeNamespace`"")
    $credLines.Add("release = `"$HelmRelease`"")
    $credLines.Add("workload = `"$HelmFullname`"")
    $credLines.Add("port_forward = `"$PortForwardCmd`"")
    $K8sCredsFile = Join-Path $CremindInstallDir 'credentials.toml'
    Write-Utf8NoBomFile -Path $K8sCredsFile -Content (($credLines -join "`n") + "`n")

    # ── handoff ───────────────────────────────────────────────────────────
    $K8sWizardUrl = "${BootScheme}://localhost:1515/#/setup"
    if ($K8sIngress -and $K8sPodAppUrl) {
        $K8sWizardUrl = ($K8sPodAppUrl.TrimEnd('/')) + '/#/setup'
    }

    if (-not $NoLaunch -and $PfRunning -and $K8sHealthOk) {
        try { Start-Process $K8sWizardUrl | Out-Null } catch {}
    }

    Write-Step "Setup wizard"
    Write-Host ''
    Write-Host "  Open: $K8sWizardUrl" -ForegroundColor White
    Write-Host ''
    Write-Host '  In the Database step, leave the password blank and click Next - the chart'
    Write-Host '  wires the PostgreSQL credentials into the pod for you.'
    Write-Host ''
    if ($DesktopUi -ne '0') {
        Write-Host "  Agent desktop: $novnc"
        Write-Host "  VNC password:  $VncPwd"
        if ($VncGenerated) {
            Write-Host "  (generated for this install; it is also in $K8sCredsFile)" -ForegroundColor DarkGray
        }
        Write-Host ''
    }
    Write-Host '  Port-forward:' -ForegroundColor White
    Write-Host "    $PortForwardCmd"
    if ($PfRunning) {
        Write-Host "  Running in the background (pid $(Get-Content -LiteralPath $PfPidFile))." -ForegroundColor DarkGray
        Write-Host "  Stop it with: Stop-Process -Id (Get-Content '$PfPidFile')" -ForegroundColor DarkGray
        Write-Host '  It ends when this session does; re-run the command above to reconnect.' -ForegroundColor DarkGray
    }
    Write-Host ''
    Write-Host '  Manage the release:' -ForegroundColor White
    Write-Host "    helm status $HelmRelease --kube-context $KubeContext -n $KubeNamespace"
    Write-Host "    kubectl --context $KubeContext -n $KubeNamespace logs deploy/$HelmFullname -c cremind -f"
    Write-Host '    .\install.ps1 -Uninstall'
    Write-Host ''
    Write-Host "  Credentials: $K8sCredsFile"
    Write-Host ''
    Write-Ok "Done."
    exit 0
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
    # templates on every run is the only way to keep state honest.
    # VNC_PASSWORD is the one true secret here, and it is deliberately NOT
    # re-rolled: it is carried over from the previous .env (see the
    # precedence chain below) so a re-run doesn't invalidate a password the
    # user chose or wrote down.
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
        # Precedence: what the operator chose (flag or prompt) → what the
        # previous install used → a fresh secret. The middle step is what
        # keeps a re-run from silently rotating a password the user picked
        # and wrote down.
        $VncPwd = if ($VncPassword) { $VncPassword }
                  elseif ($PrevVncPassword) { $PrevVncPassword }
                  else { New-Secret }
    }
    $CremindVer   = Resolve-CremindVersion

    # $UrlScheme (decided before anything was written) — not a literal
    # http:// — so the .env, the credentials.toml built from these two
    # variables, and the closing banner all name the same origin.
    switch ($Deployment) {
        'local' {
            $DockerAppUrl    = "${UrlScheme}://localhost:1515"
            $DockerCors      = "${UrlScheme}://localhost:1515,${UrlScheme}://127.0.0.1:1515"
            $DockerWizardEnv = 'local'
        }
        'server' {
            $DockerAppUrl    = "${UrlScheme}://${AppHost}:1515"
            $DockerCors      = "${UrlScheme}://${AppHost}:1515,${UrlScheme}://localhost:1515"
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

    # Stamp the resolved TLS mode. The template ships CREMIND_SSL commented
    # out, and compose interpolates ``${CREMIND_SSL:-}`` from THIS file (every
    # compose call runs from $DockerDir), so without the line here the setting
    # would live only in the installing shell: the next ``docker compose up -d``
    # from a clean terminal would recreate the container with TLS off while
    # APP_URL still said https://. Written even when empty, so a re-install can
    # tell "previous install chose plain HTTP" from "no previous install".
    Add-Content -Path $EnvDocker -Value "CREMIND_SSL=$SslMode" -Encoding utf8
    # Preserve custom-certificate and password inputs across the template
    # rewrite. A certificate pair enables TLS independently of CREMIND_SSL.
    if ($ResolvedSslCertFile) {
        Add-Content -Path $EnvDocker -Value "CREMIND_SSL_CERTFILE=$ResolvedSslCertFile" -Encoding utf8
    }
    if ($ResolvedSslKeyFile) {
        Add-Content -Path $EnvDocker -Value "CREMIND_SSL_KEYFILE=$ResolvedSslKeyFile" -Encoding utf8
    }
    if ($ResolvedSslKeyFilePassword) {
        Add-Content -Path $EnvDocker -Value "CREMIND_SSL_KEYFILE_PASSWORD=$ResolvedSslKeyFilePassword" -Encoding utf8
    }
    # Generated certificates cover localhost, the container's hostname and its
    # detected IPs — none of which is the name a server deployment is reached
    # by. Mirrors the commented AUTO_HOSTS pairs in server.env.tmpl.
    if ($ResolvedSslAutoHosts) {
        Add-Content -Path $EnvDocker -Value "CREMIND_SSL_AUTO_HOSTS=$ResolvedSslAutoHosts" -Encoding utf8
    } elseif ($SslMode -and $Deployment -eq 'server' -and $AppHost) {
        Add-Content -Path $EnvDocker -Value "CREMIND_SSL_AUTO_HOSTS=$AppHost" -Encoding utf8
    }

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

    # ``docker compose`` resolves ``${VAR}`` from the invoking shell BEFORE the
    # sibling .env, and this script runs inside the operator's own PowerShell
    # (``iwr | iex``, ``.\install.ps1``): whatever that session carries, compose
    # sees. INSTALL_MODE is the one interpolated name that is a constant here —
    # this branch IS the Docker install — and a session that ran the native
    # ``cremind`` shim, or a native install, earlier carries
    # ``INSTALL_MODE=native`` (both used to load ~\.cremind\.env into the
    # session and never remove it). The compose file rendered above pins the
    # literal, so this guards an OLD compose file — and it cleans the session,
    # so the ``docker compose up -d`` the closing banner suggests starts clean
    # too. Not restored, on purpose. The CREMIND_SSL* variables are the opposite
    # case and stay: they are how the resolved TLS mode reaches compose (see the
    # ssl block above), and the .env written here carries the same values.
    if ($env:INSTALL_MODE -and $env:INSTALL_MODE -ne 'docker') {
        Write-Warn2 "Ignoring INSTALL_MODE=$($env:INSTALL_MODE) from the environment: this is a Docker install."
    }
    Remove-Item Env:INSTALL_MODE -ErrorAction SilentlyContinue

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
    # Docker publishes only the single public port (1515); probe it on the
    # host. That port is the public origin, so its scheme follows the
    # container's CREMIND_SSL* settings — which compose reads from the .env
    # we just wrote. Re-checked against that file, but only when the
    # environment hasn't already settled it: the environment won when
    # $UrlScheme was decided, so this can never downgrade https to http and
    # contradict the URLs written above.
    if ($UrlScheme -ne 'https') {
        $UrlScheme = Get-CremindScheme -EnvPath $EnvDocker
    }
    # What the container is serving THIS MINUTE. Same inputs, but
    # CREMIND_SSL=after-setup answers http until the wizard finishes — probing
    # https there would hang the health gate against a plaintext listener and
    # fail the install. $UrlScheme above stays the steady state, because the
    # URLs already written into .env describe the whole life of the install.
    #
    # Under after-setup the answer flips once the wizard has run, and the
    # marker it flips on — bootstrap.toml — lives INSIDE the container's
    # cremind-data volume, where the host cannot see it. So a re-install of an
    # already-set-up install can't infer the phase; it has to ask. Probe both
    # candidates and believe whichever answers: that is the listener, whatever
    # a file says. Order matters only for speed, so try the declared guess
    # first.
    $BootScheme = Get-CremindBootScheme -EnvPath $EnvDocker
    $BootCandidates = @($BootScheme)
    if ($UrlScheme -eq 'https' -and $BootScheme -eq 'http') { $BootCandidates += 'https' }
    Write-Info "Waiting for backend at $($BootCandidates[0])://${HealthHost}:1515/health ..."
    for ($i = 0; $i -lt 60; $i++) {
        $answered = $false
        foreach ($candidate in $BootCandidates) {
            if (Test-CremindHealth -Url "${candidate}://${HealthHost}:1515/health") {
                if ($candidate -ne $BootScheme) {
                    Write-Info "Backend answered on ${candidate}:// — setup has already been completed on this install."
                    $BootScheme = $candidate
                }
                Write-Ok "Backend is up"
                $answered = $true
                break
            }
        }
        if ($answered) { break }
        Start-Sleep -Seconds 2
    }
    # https steady state reached over http right now == TLS deferred to the
    # wizard. The banner below says so instead of pointing at a CA download.
    $SslDeferred = ($UrlScheme -eq 'https' -and $BootScheme -eq 'http')

    # ── host-side CA trust ────────────────────────────────────────────────
    # The CA lives inside the container, where nothing can reach the HOST's
    # trust store — but this script runs on the host, so it can. This is the
    # Docker counterpart of the wizard's one-click trust (which the server
    # can only offer on native installs): download /ca.pem from the running
    # container and offer to add it to the current user's Trusted Root store.
    # Declining is fine — the wizard's "Secure this install" step shows the
    # manual command, and every OTHER device needs that path anyway.
    #
    # Both listener phases serve /ca.pem: under after-setup it is plain http,
    # and a finished install answers https — which Invoke-WebRequest accepts
    # via Schannel exactly when this host already trusts the CA from a
    # previous run (and then the thumbprint check below skips the prompt).
    # A host that never trusted it gets a failed download and a pointer, not
    # a failed install.
    if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron' -and -not $Unattended -and $UrlScheme -eq 'https') {
        Invoke-HostCaTrust -CaUrl "${BootScheme}://${HealthHost}:1515/ca.pem"
    }

    # Suppress the human-handoff block when the Electron app is driving —
    # it navigates to the in-window wizard via vue-router and a "Wizard
    # URL: ..." instruction would mislead the user.
    # The wizard runs against the listener that exists now, so this URL is
    # $BootScheme — under after-setup an https link here would simply not
    # open. It becomes https on its own once the wizard restarts the server.
    $WizardUrl = "${BootScheme}://${HealthHost}:1515/#/setup"
    if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron') {
        Write-Step "Setup wizard"

        Write-Host @"
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

Open this URL in your browser to continue setup:

    $WizardUrl

  Cremind     : ${BootScheme}://${HealthHost}:1515
"@
        if ($SslDeferred) {
            # after-setup: nothing to trust out of band, and pointing at
            # /ca.pem here would only duplicate what the wizard is about to
            # walk the user through — on a page that has no warning to explain.
            Write-Host "  Starts on http:// — the Setup Wizard walks you through trusting the certificate, then switches to https:// with no warning."
        }
        # With CREMIND_SSL=auto the certificate is signed by a CA generated
        # for this install, which no browser knows yet — say so here rather
        # than letting the interstitial be the user's first surprise.
        elseif ($UrlScheme -eq 'https') {
            # The CA lives inside the container, and a Docker install puts no
            # cremind CLI on the host — so point at the download, which every
            # device can reach, rather than a command that isn't there.
            Write-Host "  Browsers warn until you trust the local CA (one-time per device)."
            Write-Host "  Download it:  curl.exe -k -o cremind-ca.pem ${UrlScheme}://${HealthHost}:1515/ca.pem"
            Write-Host "  Then trust it with 'cremind tls trust --file cremind-ca.pem', or your OS certificate manager."
        }
        # Desktop image only: surface the noVNC viewer + VNC password.
        # noVNC is websockify on its own port and never speaks TLS, so this
        # URL stays http:// no matter what the app origin does.
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
  Restart: cd $DockerDir; docker compose up -d

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
    Write-Utf8NoBomFile -Path $CredsFile -Content $credsText

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

# Advisory only — Node is needed by the WhatsApp/Zalo channel sidecars, which
# are optional and off by default, so a missing (or too old) Node must never
# block the install. Docker installs skip this: their image bundles Node 22.
$NodeMajor = $null
if (Get-Command node -ErrorAction SilentlyContinue) {
    try {
        $NodeMajor = [int]((& node -e 'console.log(process.versions.node.split(".")[0])' 2>$null) | Select-Object -First 1)
    } catch { $NodeMajor = $null }
}
if ($null -eq $NodeMajor) {
    Write-Info "Node.js: not found - the WhatsApp and Zalo channels need Node 20+ (https://nodejs.org). Everything else works without it."
} elseif ($NodeMajor -lt 20) {
    Write-Info "Node.js: v$NodeMajor found, but the WhatsApp and Zalo channels need Node 20+."
} else {
    Write-Ok "Node.js: v$NodeMajor (WhatsApp/Zalo channel sidecars supported)"
}

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
#
# The wrapper also loads $CremindSystemDir\.env before handing over. Nothing
# in the app does: app/config/settings.py resolves its dotenv path relative
# to the working directory, so a ``cremind serve`` typed in an arbitrary
# folder sees none of the install's settings. That gap is invisible for a
# local install (the defaults match the template) right up until it isn't —
# under CREMIND_SSL=after-setup the wizard asks the operator to restart the
# server by hand, and a restart that dropped CREMIND_SSL would come back on
# plain HTTP and strand the wizard waiting for an https origin that never
# arrives. Loading it here fixes that for every entry point at once.
#
# The real process environment always wins, so ``$env:X = ...; cremind ...``
# still overrides, and a missing file is a silent no-op. The .ps1 flavour runs
# inside the caller's PowerShell, where Env: is process-wide, so it also removes
# what it added once the exe returns; the .cmd flavour is scoped by
# setlocal/endlocal already.
if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir | Out-Null
}
$CremindCmd = Join-Path $BinDir 'cremind.cmd'
# Single-quoted here-string: the batch body is full of % and " that PowerShell
# would otherwise have to escape. The exe path is substituted afterwards.
#
# ``findstr /r /b "^[A-Za-z_]"`` drops comments, blank lines and a UTF-8 BOM
# in one pass (a BOM-prefixed first key stops matching ^[A-Za-z_]).
# ``tokens=1,* delims==`` splits on the FIRST '=' only, so values containing
# '=' survive. No delayed expansion anywhere: with it enabled, a '!' in any
# value would be eaten.
$CremindCmdBody = @'
@echo off
setlocal
set "CREMIND_ENV_DIR=%CREMIND_SYSTEM_DIR%"
if not defined CREMIND_ENV_DIR set "CREMIND_ENV_DIR=%USERPROFILE%\.cremind"
if exist "%CREMIND_ENV_DIR%\.env" (
  for /f "usebackq tokens=1,* delims==" %%A in (`findstr /r /b "^[A-Za-z_]" "%CREMIND_ENV_DIR%\.env"`) do (
    if not defined %%A set "%%A=%%B"
  )
  rem The wizard's system-dir relocation writes this one key double-quoted;
  rem every other value is written bare by the installer.
  if defined CREMIND_SYSTEM_DIR set "CREMIND_SYSTEM_DIR=%CREMIND_SYSTEM_DIR:"=%"
)
"__VENV_CREMIND__" %*
endlocal & exit /b %ERRORLEVEL%
'@
$CremindCmdBody.Replace('__VENV_CREMIND__', $VenvCremind) |
    Set-Content -Path $CremindCmd -Encoding ascii

$CremindPs1 = Join-Path $BinDir 'cremind.ps1'
$CremindPs1Body = @'
# Generated by the Cremind installer. Loads KEY=VALUE lines from the Cremind
# .env for the duration of ONE command (the real environment wins: only keys
# that are absent are set), runs the venv binary, then removes exactly the keys
# it added.
#
# The removal is not optional. A .ps1 on PATH runs INSIDE the calling
# PowerShell and the Env: provider is process-wide, so without it every
# ``cremind`` left INSTALL_MODE, APP_URL, CREMIND_SSL, ... behind in the
# session — and a later ``docker compose up -d`` there (compose reads the shell
# before the project's .env) baked a native install's INSTALL_MODE=native into
# a Docker container, which then showed the wrong HTTPS runbook and could not
# switch itself.
#
# Both variables below are initialised before the .env guard: this script
# inherits the caller's Set-StrictMode, and with no .env the loop never runs,
# so an uninitialised list would throw in the finally block on every call.
$cremindLoadedKeys = @()
$cremindExitCode = 0
$cremindEnvDir = if ($env:CREMIND_SYSTEM_DIR) { $env:CREMIND_SYSTEM_DIR } else { Join-Path $HOME '.cremind' }
$cremindEnvFile = Join-Path $cremindEnvDir '.env'
if (Test-Path -LiteralPath $cremindEnvFile) {
    # -Encoding utf8 because Windows PowerShell 5.1 wrote this file with a BOM
    # and would otherwise decode it as cp1252.
    foreach ($line in (Get-Content -LiteralPath $cremindEnvFile -Encoding utf8 -ErrorAction SilentlyContinue)) {
        $t = "$line".Trim()
        if (-not $t -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        if ($k -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        if (Test-Path -LiteralPath "Env:$k") { continue }
        $v = $t.Substring($i + 1).Trim()
        if ($v.Length -ge 2) {
            $first = $v[0]; $last = $v[$v.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $v = $v.Substring(1, $v.Length - 2)
            }
        }
        Set-Item -Path "Env:$k" -Value $v
        $cremindLoadedKeys += $k
    }
}
try {
    & '__VENV_CREMIND__' $args
    $cremindExitCode = $LASTEXITCODE
} finally {
    # Runs after a normal exit, after an exception, and after Ctrl+C
    # (PowerShell runs finally blocks when the pipeline is stopped). Only a
    # second Ctrl+C during this loop, or closing the window, skips it.
    foreach ($k in $cremindLoadedKeys) {
        Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
    }
}
exit $cremindExitCode
'@
$CremindPs1Body.Replace('__VENV_CREMIND__', $VenvCremind) |
    Set-Content -Path $CremindPs1 -Encoding utf8

Write-Ok "Wrote shim $CremindCmd (loads .env, then -> venv\Scripts\cremind.exe)"

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
    # TLS moves the public origin to https://, so APP_URL and
    # CORS_ALLOWED_ORIGINS have to move with it — the local/server
    # templates hard-code http://. No-op on a plain-HTTP install. Inside
    # the "file didn't exist" branch on purpose: a .env we're reusing is
    # the user's, and the installer doesn't rewrite it.
    Set-CremindEnvUrlScheme -Path $EnvFile -Scheme $UrlScheme
    # Stamp the install mode so the backend's mode-rule filter knows which
    # service modes to expose in the wizard. Native installs land here too;
    # docker installs stamp INSTALL_MODE into docker.env above.
    if (-not (Select-String -Path $EnvFile -Pattern '^INSTALL_MODE=' -Quiet)) {
        Add-Content -Path $EnvFile -Value "INSTALL_MODE=$Mode" -Encoding utf8
    }
    # Stamp the resolved TLS mode. This is what makes the choice outlive the
    # installing shell: the server start below copies this file into Env:, and
    # the ``cremind`` shim does the same for every later run — including the
    # manual restart the wizard asks for under after-setup. Plain HTTP writes
    # nothing, leaving a fresh .env exactly as the template shipped it.
    if ($SslMode) {
        Add-Content -Path $EnvFile -Value "CREMIND_SSL=$SslMode" -Encoding utf8
    }
    # Environment-supplied custom certificates must survive the installing
    # process. The canonical .env is what boot services, Electron, and later
    # shell invocations load after the initial installer exits.
    if ($ResolvedSslCertFile) {
        Add-Content -Path $EnvFile -Value "CREMIND_SSL_CERTFILE=$ResolvedSslCertFile" -Encoding utf8
    }
    if ($ResolvedSslKeyFile) {
        Add-Content -Path $EnvFile -Value "CREMIND_SSL_KEYFILE=$ResolvedSslKeyFile" -Encoding utf8
    }
    if ($ResolvedSslKeyFilePassword) {
        Add-Content -Path $EnvFile -Value "CREMIND_SSL_KEYFILE_PASSWORD=$ResolvedSslKeyFilePassword" -Encoding utf8
    }
    if ($ResolvedSslAutoHosts) {
        Add-Content -Path $EnvFile -Value "CREMIND_SSL_AUTO_HOSTS=$ResolvedSslAutoHosts" -Encoding utf8
    } elseif ($SslMode -and $Deployment -eq 'server' -and $AppHost) {
        Add-Content -Path $EnvFile -Value "CREMIND_SSL_AUTO_HOSTS=$AppHost" -Encoding utf8
    }
    # Record the boot-service choice so a re-install doesn't silently undo an
    # opt-out. Nothing in the app reads this key — only the block above does.
    Add-Content -Path $EnvFile -Value "CREMIND_BOOT_SERVICE=$BootMarker" -Encoding utf8
    Write-Ok "Wrote $EnvFile"
} else {
    Write-Info ".env already exists — keeping it. Edit $EnvFile if you need to."
    # ...with one exception: an explicit -Ssl is an instruction about THIS
    # install, and silently losing it to a stale line in a kept file would
    # make the flag a no-op on every re-install. Only an explicit flag
    # reaches here — the resolution above already carried an unflagged
    # re-install's previous choice forward from this same file.
    if ($SslExplicit -or $SslEnvironmentExplicit) {
        Set-CremindEnvSslMode -Path $EnvFile -Mode $SslMode
        # A mode-only override clears a previous custom pair because those
        # paths enable TLS independently. A supplied pair is written back so
        # the first supervised restart keeps the requested certificate.
        Set-CremindEnvKey -Path $EnvFile -Key 'CREMIND_SSL_CERTFILE' -Value $ResolvedSslCertFile
        Set-CremindEnvKey -Path $EnvFile -Key 'CREMIND_SSL_KEYFILE' -Value $ResolvedSslKeyFile
        Set-CremindEnvKey -Path $EnvFile -Key 'CREMIND_SSL_KEYFILE_PASSWORD' -Value $ResolvedSslKeyFilePassword
        if ($SslExplicit -and $SslMode -and $Deployment -eq 'server' -and $AppHost) {
            Set-CremindEnvKey -Path $EnvFile -Key 'CREMIND_SSL_AUTO_HOSTS' -Value $AppHost
        } elseif ($ResolvedSslAutoHosts) {
            Set-CremindEnvKey -Path $EnvFile -Key 'CREMIND_SSL_AUTO_HOSTS' -Value $ResolvedSslAutoHosts
        }
        Set-CremindEnvUrlScheme -Path $EnvFile -Scheme $UrlScheme
        if ($UrlScheme -eq 'http') {
            Set-CremindEnvUrlSchemeDowngrade -Path $EnvFile
        }
        Write-Warn2 "Updated TLS settings (and the APP_URL/CORS scheme) in the existing $EnvFile to match the explicit installer environment or -Ssl choice."
    }
    # Unconditional, unlike -Ssl above: the resolution block already read the
    # old value out of this very file, so writing it back is either a no-op or
    # the operator's new instruction. It also upgrades a pre-feature .env,
    # which has no such key at all.
    Set-CremindEnvKey -Path $EnvFile -Key 'CREMIND_BOOT_SERVICE' -Value $BootMarker
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
#
# after-setup skips it for a different reason: bootstrap.toml existing is
# precisely what the server reads as "setup is done, serve TLS now"
# (app/config/tls_mode.py). Writing one here would collapse after-setup into
# ``auto`` — the wizard's very first page would sit behind a certificate no
# browser trusts yet, which is the one thing this mode exists to prevent. So
# the file is left to the wizard, exactly as under Docker and Kubernetes, and
# the server boots in deferred-storage mode until then.
if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron' -and
    -not ($SslMode -eq 'after-setup' -and -not (Test-Path $BootstrapFile))) {
    if (-not (Test-Path $BootstrapFile)) {
        Write-Info "Generating $BootstrapFile (SQLite, the recommended default)"
        $bootstrapText = @"
# Database selection. SQLite is the recommended default for native
# installs; switch to "postgres" via the setup wizard if you want a
# multi-process or networked DB.
db_provider = "sqlite"
"@
        Write-Utf8NoBomFile -Path $BootstrapFile -Content $bootstrapText
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
$credsText = @"
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
"@
Write-Utf8NoBomFile -Path $CredsFile -Content $credsText

# ── migrate ───────────────────────────────────────────────────────────────

# Skip Alembic's ``upgrade head`` (and therefore creating the SQLite DB
# file) when the Electron app is driving — the app starts the backend
# only after the user clicks "Continue to Setup Wizard", and the backend
# is what eventually creates the DB. Keeping this here means a stray
# $CremindSystemDir\storage\cremind.db never shows up between the installer
# finishing and the user choosing to continue.
#
# Skipped under after-setup for the bootstrap reason above, and it has to be
# BOTH: ``cremind db upgrade`` writes bootstrap.toml itself when the file is
# missing (app/cli/commands/db.py), so gating only the block above would let
# this line put it back and defeat the mode anyway.
if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron' -and
    -not ($SslMode -eq 'after-setup' -and -not (Test-Path $BootstrapFile))) {
    Write-Info "Migrating database to current schema"
    # Both results are checked, because this step used to report success for a
    # database it had never touched: ``Invoke-NativeLogged`` sends the migration's
    # own output to the log file, and an unreadable revision was printed as a
    # bare "?" — which is what a server that cannot start looks like, one line
    # before it fails to start. ``cremind serve`` runs the same migrations, so
    # there is nothing to gain by continuing past a failure here.
    Invoke-NativeLogged { & $VenvCremind db upgrade }
    if ($LASTEXITCODE -ne 0) {
        Write-Err2 "Database migration failed - see $LogFile"
        exit 1
    }
    # ``2>$null`` on a native command is what makes Windows PowerShell 5.1
    # materialise each stderr line as a NativeCommandError, and under
    # ``$ErrorActionPreference = 'Stop'`` that terminates — on success. Cremind
    # logs its startup lines to stderr, so the revision read threw on every
    # healthy install and was reported as the unreadable "?" this replaces.
    $Revision = ''
    $PrevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $Revision = "$(& $VenvCremind db current 2>$null | Select-Object -Last 1)".Trim() }
    catch { $Revision = '' }
    finally { $ErrorActionPreference = $PrevEap }
    if ($Revision) {
        Write-Ok "Database at revision $Revision"
    } else {
        Write-Warn2 "Could not read the database revision - see $LogFile"
    }
}

# ── start the server ──────────────────────────────────────────────────────

# Declared out here so the wizard handoff and the session restore can read them
# under Set-StrictMode even on the paths that never reach the block below — the
# Electron frontend skips it, and a restore reading an unset variable would end
# an otherwise successful install with a terminating error.
$BootRegistered = $false
$SessionEnvBefore = @{}

# Skip starting ``cremind serve`` when the Electron app is driving —
# the app spawns the backend itself once the user clicks Continue, and
# that's also when the SQLite DB is created.
if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron') {
    Write-Step "Starting Cremind"

    # Load .env into the process so HOST/PORT propagate to the child
    # without us needing a TOML parser. Keys that look like KEY=VALUE
    # are honored; anything else is ignored.
    #
    # This script runs inside the operator's own PowerShell and Env: is
    # process-wide, so each key's prior state is recorded and put back at the
    # end of this branch. Without that, a native install left INSTALL_MODE=native
    # (and APP_URL, CORS_ALLOWED_ORIGINS, ...) in the session, and a Docker
    # install run in the same window baked them into the container: compose
    # resolves ${VAR} from the shell before the project's .env.
    $ParsedEnv = @{}
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $idx = $line.IndexOf('=')
            $k = $line.Substring(0, $idx).Trim()
            $v = $line.Substring($idx + 1).Trim()
            $ParsedEnv[$k] = $v
            if (-not $SessionEnvBefore.ContainsKey($k)) {
                $SessionEnvBefore[$k] = if (Test-Path -Path "Env:$k") { (Get-Item -Path "Env:$k").Value } else { $null }
            }
            Set-Item -Path "Env:$k" -Value $v
        }
    }

    $ServerHost = if ($ParsedEnv.ContainsKey('HOST')) { $ParsedEnv['HOST'] } else { '127.0.0.1' }
    $ServerPort = if ($ParsedEnv.ContainsKey('PORT')) { $ParsedEnv['PORT'] } else { '1112' }

    # Stop a server a previous run of this installer started, so the boot
    # service can own the port instead. Returns $true only once it is gone.
    #
    # The PID is checked against the install before anything is killed:
    # install.pid outlives the process it names and Windows recycles PIDs, so
    # an unguarded Stop-Process could take out whatever unrelated program
    # holds the number now. Refusing is always safe — the caller falls back to
    # registering the service without starting it.
    function Stop-CremindInstallerServer {
        param([Parameter(Mandatory)][int] $ServerPid)
        $proc = Get-Process -Id $ServerPid -ErrorAction SilentlyContinue
        if (-not $proc) { return $true }
        $procPath = $null
        try { $procPath = $proc.Path } catch { }
        if (-not $procPath -or
            -not $procPath.StartsWith($CremindSystemDir, [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
        Write-Info "Stopping the unsupervised server (pid $ServerPid) so the boot service can take over."
        try {
            Stop-Process -InputObject $proc -Force -ErrorAction Stop
        } catch {
            # Exiting between the lookup and the kill is a success. Anything
            # else (no rights) leaves it running and must not read as one.
            if (-not $proc.HasExited) { return $false }
        }
        # Ask the HANDLE, not the PID. A process stays enumerable by PID while
        # any handle to it is open — and $proc is one — so ``Get-Process -Id``
        # keeps answering for a process that has already exited, reading a
        # successful stop as a failure. The caller gates the fallback spawn on
        # this answer, so that lie ends the install with nothing serving at
        # all. WaitForExit also pins the identity: no PID reuse to race.
        if (-not $proc.WaitForExit(15000)) { return $false }
        # The service writes server.pid; nothing should be left pointing a
        # later uninstall (or the desktop app's tree-kill) at a dead PID.
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $true
    }

    $running = $false
    # PID of an unsupervised server left behind by an EARLIER run of this
    # installer. ``install.pid`` is written by the fallback spawn below and by
    # nothing else, so a live PID in it means "ours, and unsupervised" — the
    # one server we may stop to hand its port to the boot service.
    $OurServerPid = 0
    if (Test-Path $PidFile) {
        $existingPid = Get-Content $PidFile | Select-Object -First 1
        if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
            Write-Info "Cremind is already running (pid $existingPid)."
            $running = $true
            $OurServerPid = [int] $existingPid
        }
    }

    # Detect a server that's already bound to the port without going
    # through this installer (typical dev case: ``uv run cremind serve``
    # in a separate terminal). Starting a second cremind would just
    # collide on the bind, so treat it as already-running and skip the
    # spawn.
    #
    # PORT is the INTERNAL api port, which stays plain HTTP even when
    # CREMIND_SSL puts TLS on the public origin (server.py keeps the
    # loopback bind on uvicorn without a cert, for the CLI and the skills).
    # So this probe — and the one below — must NOT follow Get-CremindScheme.
    if (-not $running) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "http://${ServerHost}:${ServerPort}/health" -TimeoutSec 2 | Out-Null
            Write-Info "Cremind is already responding at http://${ServerHost}:${ServerPort} — skipping server start."
            $running = $true
        } catch { }
    }

    # Preferred path: register a logon Scheduled Task and let IT start the
    # server, so the process serving the wizard is the same one that comes
    # back after a logout, a reboot, or the restart the wizard itself asks
    # for. ``cremind boot`` owns the task and the respawn loop — the installer
    # deliberately renders neither, so there is exactly one place that knows
    # what they look like.
    if ($BootServiceOn) {
        # Hand the port over first. An unsupervised server from an earlier
        # install is still running OUR process, and leaving it up means IT
        # serves the Setup Wizard — without CREMIND_SUPERVISED, so the
        # wizard's after-setup switch to HTTPS would tell the user to restart
        # by hand. That is the exact experience the boot service exists to
        # remove, and it would otherwise greet every upgrade from a
        # pre-service install. A foreign server on the port (a dev
        # ``cremind serve``) is never touched — it gets the register-only
        # path below.
        if ($running -and $OurServerPid -gt 0) {
            if (Stop-CremindInstallerServer $OurServerPid) {
                $running = $false
            } else {
                Write-Warn2 "Could not stop the server at pid $OurServerPid — registering the boot service without starting it."
            }
        }
        if ($running) {
            # Something else owns the port (typically a dev ``cremind serve``).
            # Register without starting: a second server would only collide on
            # the bind, and the task takes over at the next logon.
            Invoke-NativeLogged { & $VenvCremind boot enable --no-start --yes }
            if ($LASTEXITCODE -eq 0) {
                $BootRegistered = $true
                Write-Info "Boot service registered — it takes over when the running server stops."
            }
        } else {
            Invoke-NativeLogged { & $VenvCremind boot enable --yes }
            if ($LASTEXITCODE -eq 0) {
                $BootRegistered = $true
                $running = $true
                Write-Ok "Cremind started by the boot service (logs: $ServerLogFile)"
            }
        }
        if (-not $BootRegistered) {
            Write-Warn2 "Could not register a boot service (see $LogFile) — starting Cremind for this session only."
        }
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
#
# The public origin is the one bind TLS applies to, so its scheme comes
# from the .env we just wrote (or the environment the server inherits).
# Re-checked against the file here because a pre-existing .env the
# installer kept can carry an uncommented CREMIND_SSL the environment
# doesn't — the unflagged re-install case, where the ssl-mode block above
# already read the same value and set the environment to match, and the
# certfile case, which no flag covers. Only consulted when the environment
# hasn't already settled it, so this can only ever turn http into https —
# never contradict the APP_URL / CORS_ALLOWED_ORIGINS this run wrote.
if ($UrlScheme -ne 'https') {
    $UrlScheme = Get-CremindScheme -EnvPath $EnvFile
}
# The scheme the server is answering on right now. Identical to $UrlScheme
# except under CREMIND_SSL=after-setup, which serves plain HTTP until the
# wizard completes — the link below has to open, so it follows the listener,
# not the steady state the .env describes.
#
# ...and after-setup stops deferring once the wizard HAS completed, which is
# the case on every re-install of a finished install. bootstrap.toml is the
# marker the server itself reads for that, and on a native install it is
# right there on the host.
$BootScheme = Get-CremindBootScheme -EnvPath $EnvFile -SetupComplete:(Test-Path $BootstrapFile)
$SslDeferred = ($UrlScheme -eq 'https' -and $BootScheme -eq 'http')
if ($Deployment -eq 'server') {
    $WizardUrl = "${BootScheme}://${AppHost}:1515/#/setup"
} elseif ($Deployment -eq 'custom') {
    $CustomHostPart = $CustomValues['public_url']
    if ($CustomHostPart -match '^https?://([^:/]+)') { $CustomHostPart = $Matches[1] } else { $CustomHostPart = 'localhost' }
    $WizardUrl = "${BootScheme}://${CustomHostPart}:1515/#/setup"
} else {
    $WizardUrl = "${BootScheme}://localhost:1515/#/setup"
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
"@

    # Same nudge the Docker path prints: with CREMIND_SSL=auto the
    # certificate is signed by a CA generated for this install, so the
    # first visit is an interstitial until that CA is trusted once.
    # after-setup has no interstitial to warn about — the wizard hands the
    # user the CA while the origin is still plain HTTP.
    if ($SslDeferred) {
        Write-Host "  Starts on http:// — the Setup Wizard walks you through trusting the certificate, then switches to https:// with no warning."
    } elseif ($UrlScheme -eq 'https') {
        Write-Host "  Browsers warn until you trust the local CA (one-time): cremind tls trust"
    }

    # A service-run server ignores install.pid — telling the user to stop a
    # PID that isn't there (or one the loop would immediately respawn) would
    # be the wrong instruction.
    if ($BootRegistered) {
        Write-Host @"
  Starts automatically at logon. Manage it with: cremind boot status
  Stop once:     Stop-ScheduledTask -TaskName 'Cremind Server'
  Stop for good: cremind boot disable

  Tip: open a NEW terminal so ``cremind`` is on PATH (already-open
       shells won't see the User PATH update).

"@
    } else {
        Write-Host @"
  Stop:       Stop-Process -Id (Get-Content $PidFile)
  Re-open:    cremind serve   (or: & "$VenvCremind" serve)

  Tip: open a NEW terminal so ``cremind`` is on PATH (already-open
       shells won't see the User PATH update).

"@
    }

    if (-not $NoLaunch -and -not $Unattended) {
        Start-Process $WizardUrl
    }
}

# Put the session back the way it was. Deliberately here and not right after the
# server spawn: ``Get-CremindScheme`` and ``Get-CremindBootScheme`` above read
# $env:CREMIND_SSL / $env:CREMIND_SSL_CERTFILE first and fall back to the file
# only when those are empty, so restoring earlier would make them answer from a
# stale session value. Children already spawned keep the environment they
# inherited. An empty table (the Electron path, which never loads the .env) is a
# no-op.
foreach ($k in @($SessionEnvBefore.Keys)) {
    if ($null -eq $SessionEnvBefore[$k]) {
        Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
    } else {
        Set-Item -Path "Env:$k" -Value $SessionEnvBefore[$k]
    }
}

switch ($Channel) {
    'test' { Write-Ok "Done. Welcome to Cremind (test build)." }
    'dev'  { Write-Ok "Done. Welcome to Cremind (dev install from $RepoRoot)." }
    default { Write-Ok "Done. Welcome to Cremind." }
}
