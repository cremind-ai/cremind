<#
.SYNOPSIS
    Windows/PowerShell port of scripts/build_ui.sh.

    Builds the SPA from the in-tree ``ui/`` directory and stages it into
    ``app/static/ui/`` (and the Electron-renderer bundle into
    ``app/static/ui-electron/``) so ``cremind serve`` — and the wheel produced
    by ``hatch build`` — carries the built UI alongside the server.

    Run this:
      - In CI before ``hatch build`` (so released wheels include the SPA).
      - Locally before ``pip install -e .`` / ``uv run cremind serve`` if you
        want the SPA served by the backend from a checkout (single-origin mode).

    Output layout:
      app/static/ui/
        index.html
        assets/...
        .built-at        <- timestamp marker, also the "is the SPA bundled?"
                            sentinel for cremind serve.

    Usage (any of):
      pwsh scripts/build_ui.ps1
      powershell -ExecutionPolicy Bypass -File scripts\build_ui.ps1
      .\scripts\build_ui.ps1
#>

# Stop on cmdlet errors. Native-command (npm) failures don't throw — we check
# $LASTEXITCODE explicitly, mirroring the bash script's manual retry logic.
$ErrorActionPreference = 'Stop'

$RepoRoot          = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Src               = Join-Path $RepoRoot 'ui'
$StaticDir         = Join-Path $RepoRoot 'app/static/ui'
$ElectronStaticDir = Join-Path $RepoRoot 'app/static/ui-electron'

if (-not (Test-Path -PathType Container $Src)) {
    Write-Error "[build_ui] expected $Src to exist (in-tree UI source)."
    exit 1
}
Write-Host "[build_ui] using local source at $Src"

# Run `npm <args>` in the ui/ source dir; return npm's exit code (no throw).
# `| Out-Host` streams npm's stdout to the console instead of letting it leak
# into the function's return value — otherwise the function would return
# [<npm stdout lines...>, <exit code>] and the caller's `-ne 0` check would
# misfire on a successful run that printed anything. npm's stderr (warnings)
# bypasses the pipe to the console on its own; we deliberately don't `2>&1`
# (that wraps stderr as errors on Windows PowerShell 5.1).
function Invoke-Npm([string[]] $NpmArgs) {
    Push-Location $Src
    try {
        & npm @NpmArgs | Out-Host
        return $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

# npm/cli#4828 workaround: a lockfile generated on one platform can omit this
# platform's optional native deps (e.g. rollup binaries), so `npm ci` "succeeds"
# but the build crashes. Wiping node_modules + the lockfile and doing a fresh
# `npm install` forces a host-correct resolve.
function Reset-NodeDeps {
    Write-Host "[build_ui] regenerating deps with a fresh npm install (npm/cli#4828 workaround)"
    Remove-Item -Recurse -Force (Join-Path $Src 'node_modules')      -ErrorAction SilentlyContinue
    Remove-Item -Force          (Join-Path $Src 'package-lock.json') -ErrorAction SilentlyContinue
    if ((Invoke-Npm @('install')) -ne 0) {
        Write-Error "[build_ui] npm install failed"
        exit 1
    }
}

# Copy the CONTENTS of $From into $To (incl. hidden files), replacing $To, and
# drop a .built-at timestamp sentinel. Mirrors `rm -rf; mkdir -p; cp -R "$From/."`.
function Publish-Dist([string] $From, [string] $To) {
    Write-Host "[build_ui] copying $From\ -> $To\"
    Remove-Item -Recurse -Force $To -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $To | Out-Null
    Get-ChildItem -Force -Path $From | Copy-Item -Destination $To -Recurse -Force
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    Set-Content -Path (Join-Path $To '.built-at') -Value $stamp -Encoding ascii
}

# ── 1. Install deps (lockfile-strict, with the #4828 fallback) ───────────────
if (Test-Path -PathType Leaf (Join-Path $Src 'package-lock.json')) {
    if ((Invoke-Npm @('ci')) -ne 0) {
        Write-Host "[build_ui] npm ci failed; regenerating with fresh npm install (npm/cli#4828 workaround)"
        Reset-NodeDeps
    }
} else {
    Write-Host "[build_ui] WARNING: no package-lock.json; falling back to npm install."
    if ((Invoke-Npm @('install')) -ne 0) {
        Write-Error "[build_ui] npm install failed"
        exit 1
    }
}

# ── 2. Web SPA build (Electron-free; leaves VITE_AGENT_URL unset so the bundle
#       resolves the agent URL from window.location at runtime) ───────────────
if ((Invoke-Npm @('run', 'web:build')) -ne 0) {
    Write-Host "[build_ui] web:build failed; regenerating deps and retrying (npm/cli#4828 workaround)"
    Reset-NodeDeps
    if ((Invoke-Npm @('run', 'web:build')) -ne 0) {
        Write-Error "[build_ui] web:build failed after retry"
        exit 1
    }
}

$Dist = Join-Path $Src 'dist-web'
if (-not (Test-Path -PathType Leaf (Join-Path $Dist 'index.html'))) {
    Write-Error "[build_ui] expected $Dist\index.html after npm run web:build, but it's missing"
    exit 1
}
Publish-Dist $Dist $StaticDir

# ── 3. Electron-renderer bundle (__IS_ELECTRON__: true) served under
#       /electron-renderer/ — see _build_spa_components in app/server.py ───────
if ((Invoke-Npm @('run', 'build:renderer')) -ne 0) {
    Write-Host "[build_ui] build:renderer failed; regenerating deps and retrying (npm/cli#4828 workaround)"
    Reset-NodeDeps
    if ((Invoke-Npm @('run', 'build:renderer')) -ne 0) {
        Write-Error "[build_ui] build:renderer failed after retry"
        exit 1
    }
}

$DistE = Join-Path $Src 'dist'
if (-not (Test-Path -PathType Leaf (Join-Path $DistE 'index.html'))) {
    Write-Error "[build_ui] expected $DistE\index.html after npm run build:renderer, but it's missing"
    exit 1
}
Publish-Dist $DistE $ElectronStaticDir

Write-Host "[build_ui] done."
