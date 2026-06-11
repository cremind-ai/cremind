import { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, session, shell } from 'electron'
import { fileURLToPath } from 'node:url'
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import fs from 'node:fs'
import http from 'node:http'
import https from 'node:https'
import path from 'node:path'
// Pulled in lazily to keep the dev / web build (which doesn't ship
// electron-updater) functional. The require is wrapped below.
type AutoUpdaterModule = typeof import('electron-updater')

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// On Windows, Electron's app.getPath('userData') resolves to %APPDATA%\Cremind
// by default. We point it at %LOCALAPPDATA%\Cremind — same dir the install
// script uses as the Install Dir — so Electron-owned scratch (cremind-config.json,
// downloaded installer scripts) sits next to install artifacts.
if (process.platform === 'win32' && process.env.LOCALAPPDATA) {
  app.setPath('userData', path.join(process.env.LOCALAPPDATA, 'Cremind'))
}

// Cremind System Directory — runtime state + user content root.
// Defaults to ~/.cremind on every platform, matching the install scripts.
// Holds: .env, bootstrap.toml, storage/, tokens/, venv/, bin/, per-profile
// dirs (PERSONA, skills, documents, browser-profile), server.log, upgrade
// artifacts, backups/. Override via $CREMIND_SYSTEM_DIR for staging.
function systemDirPath(): string {
  if (process.env.CREMIND_SYSTEM_DIR) return process.env.CREMIND_SYSTEM_DIR
  return path.join(app.getPath('home'), '.cremind')
}

// Cremind Install Directory — install-time scratch only.
// Holds: install.log, install.pid, docker/ compose bundle, pip-cache/,
// uv-cache/, python/. Platform-conventional default so install artifacts
// never pollute ~/.cremind. Override via $CREMIND_INSTALL_DIR.
//   Linux:   $XDG_DATA_HOME/cremind (or ~/.local/share/cremind)
//   macOS:   ~/Library/Application Support/Cremind  (== app.getPath('userData'))
//   Windows: %LOCALAPPDATA%\Cremind                 (== overridden userData above)
function installDirPath(): string {
  if (process.env.CREMIND_INSTALL_DIR) return process.env.CREMIND_INSTALL_DIR
  if (process.platform === 'linux') {
    const xdg = process.env.XDG_DATA_HOME || path.join(app.getPath('home'), '.local', 'share')
    return path.join(xdg, 'cremind')
  }
  return app.getPath('userData')
}

// Build-time install channel — baked in by Vite at compile time. Used as
// the value of ``runtimeConfig.channel`` (force-applied on each launch
// regardless of what's on disk) and also as the flag passed to the
// install scripts. Declared here so the config defaults below can
// reference it; the second definition site at line ~189 used to be the
// only one and led to the renderer reading a stale ``'stable'`` default
// for the Updates page "Release channel" row.
const INSTALL_CHANNEL: 'production' | 'test' | 'dev' = __CREMIND_INSTALL_CHANNEL__

// Env overlay for cremind subprocesses (``serve`` and ``upgrade apply``).
// On non-production builds we hand the channel to the child explicitly —
// the cremind CLI does not auto-load ``<CREMIND_SYSTEM_DIR>/.env``, and
// Electron's CWD on launch is not reliably the home dir, so dotenv
// discovery there is fragile. The installer spawn at ~line 708 already
// follows the same pattern via ``--channel``; this keeps backend serve
// and upgrade apply consistent with it.
function cremindSubprocessEnv(): NodeJS.ProcessEnv {
  const base = {
    ...process.env,
    CREMIND_SYSTEM_DIR: systemDirPath(),
    CREMIND_INSTALL_DIR: installDirPath(),
  }
  if (INSTALL_CHANNEL === 'production') return base
  return { ...base, CREMIND_UPGRADE_CHANNEL: INSTALL_CHANNEL }
}

// Test installers are prereleases for testers; DevTools is a feature there.
function devToolsEnabled(): boolean {
  return !app.isPackaged || INSTALL_CHANNEL !== 'production'
}

// ── Runtime config (cremind-config.json) ─────────────────────────────────────
//
// Lives in the per-user app-data directory. The installer writes to it at
// the end of the install flow; the setup wizard updates it via IPC; the
// renderer reads it synchronously through the preload bridge so the agent
// URL is available before any module-level code runs.
//
// Defaults are intentionally permissive: agentUrl="" makes the UI prompt
// the user instead of silently pointing at a missing server.

type CremindConfig = {
  agentUrl: string
  deploymentType: 'local' | 'server' | 'custom' | ''
  autoUpdate: boolean
  // Mirror of INSTALL_CHANNEL exposed to the renderer via the preload
  // bridge. The Updates page "Release channel" row reads this.
  // Not user-mutable — the channel is fixed at install/build time;
  // loadConfig() force-overwrites whatever is on disk with the current
  // build's INSTALL_CHANNEL so a re-installed test app never displays
  // a stale production channel (or vice versa).
  channel: 'production' | 'test' | 'dev'
}

const CONFIG_DEFAULTS: CremindConfig = {
  agentUrl: '',
  deploymentType: '',
  autoUpdate: true,
  channel: INSTALL_CHANNEL,
}

function configPath(): string {
  return path.join(app.getPath('userData'), 'cremind-config.json')
}

function loadConfig(): CremindConfig {
  let merged: CremindConfig
  try {
    const raw = fs.readFileSync(configPath(), 'utf8')
    const parsed = JSON.parse(raw)
    merged = { ...CONFIG_DEFAULTS, ...parsed }
  } catch {
    merged = { ...CONFIG_DEFAULTS }
  }
  // The channel is fixed at build time; force it back to INSTALL_CHANNEL
  // so a re-install / channel-switch never inherits the previous build's
  // value from disk. This is also how the Updates page "Release
  // channel" row gets the right value — before this, ``runtimeConfig``
  // was never connected to ``INSTALL_CHANNEL`` and the row always read
  // the static default of ``'stable'``.
  merged.channel = INSTALL_CHANNEL
  return merged
}

function saveConfig(cfg: CremindConfig): void {
  const file = configPath()
  fs.mkdirSync(path.dirname(file), { recursive: true })
  // Atomic write: temp file + rename so a crashed write never leaves a
  // half-formed config behind for the next launch.
  const tmp = `${file}.tmp`
  fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2), 'utf8')
  fs.renameSync(tmp, file)
}

// In-memory cache. Loaded once at app start; mutations go through
// updateConfig() so disk and memory stay in sync.
let runtimeConfig: CremindConfig = { ...CONFIG_DEFAULTS }

function updateConfig(patch: Partial<CremindConfig>): CremindConfig {
  runtimeConfig = { ...runtimeConfig, ...patch }
  saveConfig(runtimeConfig)
  return runtimeConfig
}

// The install script's ``.env`` is the canonical "Cremind is installed
// on this machine" marker. We reconcile runtimeConfig.agentUrl against
// it on each launch so the user can re-trigger the first-run installer
// by wiping the System Dir (or its contents).
//
// Docker installs also stamp a marker into <CREMIND_SYSTEM_DIR>/docker/.env
// (the compose env file). We treat that as a fallback so users whose
// Docker install predates the top-level marker fix don't get bounced back
// to the wizard on app upgrade.
function installMarkerExists(): boolean {
  // Native marker: $SYSTEM_DIR/.env is written at install time and read at
  // every boot. Docker marker: $INSTALL_DIR/docker/.env (the compose env
  // file). Either one is enough.
  return (
    fs.existsSync(path.join(systemDirPath(), '.env')) ||
    fs.existsSync(path.join(installDirPath(), 'docker', '.env'))
  )
}
function reconcileInstallStateWithDisk(): void {
  const installed = installMarkerExists()
  if (!installed && runtimeConfig.agentUrl) {
    // User wiped the System Dir to re-run the first-run installer.
    updateConfig({ agentUrl: '', deploymentType: '' })
  } else if (installed && !runtimeConfig.agentUrl) {
    // The script was run outside the Electron app (e.g., via the CLI)
    // and we lost track of it. Adopt the default local agent URL — the
    // user can correct it later via settings if they actually pointed
    // cremind at a remote host.
    updateConfig({ agentUrl: 'http://localhost:1515', deploymentType: 'local' })
  }
}

// The built directory structure
//
// ├─┬─┬ dist
// │ │ └── index.html
// │ │
// │ ├─┬ dist-electron
// │ │ ├── main.js
// │ │ └── preload.mjs
// │
process.env.APP_ROOT = path.join(__dirname, '..')

// 🚧 Use ['ENV_NAME'] avoid vite:define plugin - Vite@2.x
export const VITE_DEV_SERVER_URL = process.env['VITE_DEV_SERVER_URL']
export const MAIN_DIST = path.join(process.env.APP_ROOT, 'dist-electron')
export const RENDERER_DIST = path.join(process.env.APP_ROOT, 'dist')

process.env.VITE_PUBLIC = VITE_DEV_SERVER_URL ? path.join(process.env.APP_ROOT, 'public') : RENDERER_DIST

type WindowKind = 'main' | 'settings' | 'vnc' | 'processes' | 'events' | 'channels'
type PageKind = 'processes' | 'events' | 'channels'
const windows = new Set<BrowserWindow>()
const windowKinds = new WeakMap<BrowserWindow, WindowKind>()
// Most recently focused non-VNC window. Used by tray "Show", the
// second-instance fallback, and by `installer` / `backend-upgrade` IPC
// senders that need to know which window initiated them.
let mainWin: BrowserWindow | null = null
let tray: Tray | null = null
// Populated by fetchCapabilities() once the backend is reachable. Drives
// the conditional "Open VNC Desktop" entry in the tray / jumplist / dock.
let installMode: 'docker' | 'native' | null = null
// Set of UI feature names the backend's bundled SPA exposes. Drives the
// gating for Process Manager / Events / Channels (and future) tray /
// jumplist / dock entries. ``null`` means the backend hasn't reported a
// list — either pre-protocol (older pinned wheel; the SPA also lacks the
// route) or the capabilities call hasn't returned yet. In both cases we
// HIDE the gated entries; the alternative ("show all on null") is what
// shipped briefly in test10 and is exactly the silent-desync bug this
// gate exists to prevent.
let uiFeatures: Set<string> | null = null

function uiFeatureAvailable(feature: string): boolean {
  if (uiFeatures === null) return false
  return uiFeatures.has(feature)
}

// App version handler
ipcMain.handle('get-app-version', () => {
  return app.getVersion();
});

// Runtime config handlers. ``cremind:get-config-sync`` is the only sync IPC
// in the app — invoked once from the preload before the renderer boots so
// `window.cremind.config` is populated by the time module-level JS runs.
ipcMain.on('cremind:get-config-sync', (event) => {
  event.returnValue = runtimeConfig
})
ipcMain.handle('cremind:get-config', () => runtimeConfig)
ipcMain.handle('cremind:set-config', (_event, patch: Partial<CremindConfig>) => {
  const next = updateConfig(patch)
  if (patch.agentUrl !== undefined) {
    // Backend host changed — its install_mode may have flipped, so the
    // VNC tray/jumplist entry may need to appear or disappear.
    void fetchCapabilities()
  }
  return next
})

// ── External-link bridge ────────────────────────────────────────────────────
//
// Hands a URL to the OS default handler (browser / mail client / dialer) so
// user-clicked external links open outside Electron rather than spawning a new
// app window. The renderer's capture-phase anchor interceptor
// (ui/src/utils/externalLinks.ts) is what routes clicks here; programmatic
// ``window.open`` (OAuth popups, blob/about previews) deliberately does NOT use
// this — those stay in-app via setWindowOpenHandler. Any NEW external
// programmatic open should call ``window.cremind.openExternal`` instead.
//
// The scheme allowlist is authoritative: never trust the renderer with an
// arbitrary URL, or a crafted markdown link could ask us to launch
// ``file://`` targets or custom protocol handlers.
const EXTERNAL_SCHEMES = new Set(['http:', 'https:', 'mailto:', 'tel:'])
ipcMain.handle('cremind:open-external', (_event, rawUrl: unknown) => {
  if (typeof rawUrl !== 'string') return
  let u: URL
  try {
    u = new URL(rawUrl)
  } catch {
    return
  }
  if (!EXTERNAL_SCHEMES.has(u.protocol)) {
    console.warn('[cremind] refused open-external for scheme', u.protocol)
    return
  }
  void shell.openExternal(u.toString())
})

// ── First-run installer bridge ──────────────────────────────────────────────
//
// The Installer.vue view collects answers, asks main to detect the host
// environment, then asks main to download and run install.sh / install.ps1
// with those answers. Logs stream back via ``cremind:installer:log``;
// completion is signalled with ``cremind:installer:done``. We only allow
// one install at a time — concurrent installs fight over the same
// config files and PIDs.
//
// The install script source-of-truth lives in the cremind repo, not this
// one. Downloading it at runtime keeps the two repos decoupled and makes
// sure users get the latest fixes without an Electron app update.

const INSTALLER_SCRIPT_BASE =
  process.env.CREMIND_INSTALLER_BASE ??
  'https://raw.githubusercontent.com/cremind-ai/cremind/main/install'

// INSTALL_CHANNEL is declared at the top of this file (alongside the
// runtime-config defaults that reference it). The install script uses
// the same constant — see ``runInstaller`` below.

let installerProcess: ChildProcess | null = null
// Long-running ``cremind serve`` we spawn after the user clicks Continue
// to Setup Wizard (and on subsequent launches when an install is
// detected). Tracked so before-quit can tear it down cleanly.
let backendProcess: ChildProcess | null = null
// Short-lived ``cremind upgrade --yes`` child spawned by the in-app
// upgrade flow. At most one in flight; tracked so before-quit can kill
// it (the runner's lock-file recovery will roll back on next launch).
let upgradeProcess: ChildProcess | null = null
// Short-lived uninstall script. Like ``installerProcess`` but driven by
// the Settings → Uninstall flow; the renderer chooses keep vs purge.
let uninstallerProcess: ChildProcess | null = null

// ── Backend (``cremind serve``) lifecycle ──────────────────────────────────
//
// The install script *used* to start the backend itself. We moved that
// into Electron so the install step doesn't trigger a server startup —
// and therefore doesn't create the SQLite DB at install time. The
// backend (and the DB) only come into existence when the user clicks
// "Continue to Setup Wizard", or when the app is relaunched after an
// install has already completed.

function backendPidFilePath(): string {
  // install.pid is install-session scratch (the install script's spawned
  // backend), written to the Install Dir.
  return path.join(installDirPath(), 'install.pid')
}

// Resolve the actual cremind executable, bypassing the user-facing shim.
//
// Node ≥18.20 / ≥20.12 / ≥21.7 refuses to ``spawn`` ``.cmd`` / ``.bat``
// files directly without ``shell: true`` — the CVE-2024-27980 mitigation
// surfaces as ``Error: spawn EINVAL``. We dodge it by reading the
// install-time shim (``<SYSTEM_DIR>/bin/cremind.cmd``), pulling the
// underlying ``cremind.exe`` path out of it, and spawning that instead.
// On POSIX the shim is a symlink, which ``spawn`` follows transparently.
function cremindExePath(): string {
  const sys = systemDirPath()
  const bin = path.join(sys, 'bin')
  if (process.platform === 'win32') {
    const shim = path.join(bin, 'cremind.cmd')
    try {
      const content = fs.readFileSync(shim, 'utf8')
      // The shim install.ps1 writes is two lines:
      //   @echo off
      //   "C:\path\to\cremind.exe" %*
      const m = content.match(/^"([^"]+)"\s*%\*/m)
      if (m && m[1]) return m[1]
    } catch { /* fall through to default below */ }
    // Default install location when the shim is missing or unparseable.
    return path.join(sys, 'venv', 'Scripts', 'cremind.exe')
  }
  return path.join(bin, 'cremind')
}

// Resolve <venv>/Scripts/python.exe (POSIX: <venv>/bin/python). We spawn
// the interpreter directly instead of cremind.exe so pip can replace
// cremind.exe during ``pip install --upgrade cremind`` on Windows;
// otherwise pip's uninstall step hits WinError 32 trying to rename the
// live cremind.exe to cremind.exe.deleteme. python.exe is not touched by
// the cremind wheel install, so its handle being held is harmless.
function venvPythonPath(): string {
  const exe = cremindExePath()
  const scripts = path.dirname(exe)
  if (process.platform === 'win32') {
    return path.join(scripts, 'python.exe')
  }
  return path.join(path.dirname(scripts), 'bin', 'python')
}

function backendHealthUrl(): string {
  // The merged app serves /health on the single public origin (default 1515),
  // which is reachable on the same machine as the backend. We don't parse .env
  // here — keep this simple.
  return 'http://127.0.0.1:1515/health'
}

function isBackendHealthy(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(backendHealthUrl(), { timeout: 1500 }, (res) => {
      res.resume()
      resolve((res.statusCode ?? 0) < 400)
    })
    req.on('error', () => resolve(false))
    req.on('timeout', () => { req.destroy(); resolve(false) })
  })
}

async function waitForBackendHealthy(timeoutMs = 30000): Promise<boolean> {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    if (await isBackendHealthy()) return true
    await new Promise((r) => setTimeout(r, 500))
  }
  return false
}

// Origin of the backend's bundled SPA. The backend now serves the SPA, API,
// A2A, and OAuth as ONE same-origin app on the single public port (default
// 1515 — see ``app/server.py``); the internal API bind (1112) is loopback-only.
// The Electron shell pivots here once the backend is healthy. When
// CREMIND_UI_PORT is overridden on the backend, the shell would need a matching
// override here; today we hardcode the default, same as backendHealthUrl().
function backendSpaUrl(): string {
  return 'http://127.0.0.1:1515'
}

// Path the Electron shell loads from the backend's SPA listener. The
// backend serves two bundles side-by-side:
//   /                    → web bundle  (__IS_ELECTRON__: false)
//   /electron-renderer/  → Electron renderer bundle (__IS_ELECTRON__: true)
// Loading the web bundle inside the Electron shell hides the custom
// titlebar (gated on __IS_ELECTRON__) and breaks window dragging, so
// the shell always asks for /electron-renderer/.
const ELECTRON_RENDERER_PATH = '/electron-renderer/'

// HEAD-style probe for the electron-renderer mount. Older wheels don't
// ship the ui-electron directory; in that case the backend's StaticFiles
// fallback at ``/`` would silently serve the web bundle instead, so we
// detect the miss explicitly and fall back to the asar copy (which is
// also __IS_ELECTRON__: true and at least keeps the titlebar working
// until the user upgrades to a wheel that includes ui-electron).
function electronRendererAvailable(): Promise<boolean> {
  return new Promise((resolve) => {
    const url = `${backendSpaUrl()}${ELECTRON_RENDERER_PATH}index.html`
    const req = http.request(url, { method: 'HEAD', timeout: 1500 }, (res) => {
      res.resume()
      resolve((res.statusCode ?? 0) < 400)
    })
    req.on('error', () => resolve(false))
    req.on('timeout', () => { req.destroy(); resolve(false) })
    req.end()
  })
}

// Decide where to load a window's renderer from. The asar copy of the
// SPA is frozen at electron-builder time, so loading from it leaves
// wheel-only upgrades stuck on stale UI (no new Settings cards, no new
// routes, etc.). Loading from the backend's HTTP server instead means
// every wheel install immediately delivers fresh UI to the Electron
// renderer.
//
// Fallback chain when the backend is healthy:
//   1. /electron-renderer/  — preferred (__IS_ELECTRON__: true so the
//      custom titlebar / drag region renders).
//   2. /                    — same origin as #1, just the web bundle.
//      Loses the titlebar but keeps localStorage scoped to the wheel
//      origin (http://127.0.0.1:1515), so the auth token written by
//      SetupWizard.vue's pivot is still visible and the user stays
//      logged in. Used when /electron-renderer/ is missing — typically
//      a backend wheel that pre-dates test41 paired with a newer
//      Electron shell.
//   3. asar (file://)       — backend unreachable. Different origin
//      from the http token store, so the user sees a login screen, but
//      there's no backend to authenticate against anyway. Once the
//      backend comes back, maybePivotToBackend / pivotFileWindowsToBackend
//      migrate the window back to the http origin and the token reappears.
//
// The dev path (``vite serve``) keeps its own URL via VITE_DEV_SERVER_URL.
// The renderer's own ``maybePivotToBackend`` in ui/src/main.ts handles
// the post-Setup-Wizard transition: once the backend comes up the asar
// SPA navigates itself to the HTTP origin without main needing to chase.
async function loadMainContent(w: BrowserWindow, hash: string): Promise<void> {
  if (VITE_DEV_SERVER_URL) {
    void w.loadURL(VITE_DEV_SERVER_URL + hash)
    return
  }
  if (await isBackendHealthy()) {
    const targetPath = (await electronRendererAvailable()) ? ELECTRON_RENDERER_PATH : '/'
    void w.loadURL(`${backendSpaUrl()}${targetPath}${hash}`)
    return
  }
  void w.loadFile(path.join(RENDERER_DIST, 'index.html'), { hash: hash.slice(1) })
}

// Navigate any windows still on the asar (``file://``) to the backend's
// SPA listener. Used at boot once startBackend() has succeeded and the
// wheel-served SPA is reachable, and as part of the post-upgrade reload.
// Idempotent — windows already on http(s):// are left alone so a
// successful renderer-side ``maybePivotToBackend`` is not clobbered by a
// redundant navigation that would wipe SPA state.
async function pivotFileWindowsToBackend(): Promise<void> {
  if (!(await isBackendHealthy())) return
  // Same path-preference as loadMainContent: /electron-renderer/ when
  // it's there, otherwise the web bundle at /. Both share the http
  // origin, so the auth token created by the Setup Wizard's pivot
  // survives regardless of which path serves.
  const targetPath = (await electronRendererAvailable()) ? ELECTRON_RENDERER_PATH : '/'
  for (const win of BrowserWindow.getAllWindows()) {
    if (win.isDestroyed()) continue
    const url = win.webContents.getURL()
    if (!url.startsWith('file://')) continue
    const hashIdx = url.indexOf('#')
    const winHash = hashIdx >= 0 ? url.slice(hashIdx) : '#/'
    void win.loadURL(`${backendSpaUrl()}${targetPath}${winHash}`)
  }
}

async function startBackend(): Promise<{ ok: boolean; error?: string }> {
  // Already responding? Adopt the running instance and skip the spawn.
  if (await isBackendHealthy()) {
    return { ok: true }
  }
  // Already spawned but not healthy yet? Just wait.
  if (backendProcess && backendProcess.exitCode === null) {
    const ok = await waitForBackendHealthy()
    return ok ? { ok: true } : { ok: false, error: 'backend did not become healthy' }
  }

  // Spawn via the venv interpreter rather than cremind.exe so that
  // ``pip install --upgrade cremind`` can replace cremind.exe during an
  // in-app upgrade — see venvPythonPath() comment.
  const py = venvPythonPath()
  if (!fs.existsSync(py)) {
    return { ok: false, error: `venv python missing at ${py} — installer didn't finish?` }
  }

  const sys = systemDirPath()
  const serverLog = path.join(sys, 'server.log')
  const serverErr = path.join(sys, 'server.err.log')

  let stdoutFd: number | undefined
  let stderrFd: number | undefined
  try {
    fs.mkdirSync(sys, { recursive: true })
    stdoutFd = fs.openSync(serverLog, 'a')
    stderrFd = fs.openSync(serverErr, 'a')
  } catch (err) {
    return { ok: false, error: `could not open server log files: ${String(err)}` }
  }

  let child: ChildProcess
  try {
    child = spawn(py, ['-m', 'app.cli.main', 'serve'], {
      stdio: ['ignore', stdoutFd, stderrFd],
      windowsHide: true,
      env: cremindSubprocessEnv(),
    })
  } catch (err) {
    try { if (stdoutFd) fs.closeSync(stdoutFd) } catch { /* ignore */ }
    try { if (stderrFd) fs.closeSync(stderrFd) } catch { /* ignore */ }
    return { ok: false, error: String(err) }
  }
  backendProcess = child

  // Persist the PID so before-quit can fall back to the file if we lose
  // the in-memory reference somehow.
  try { fs.writeFileSync(backendPidFilePath(), String(child.pid ?? '')) } catch { /* best-effort */ }

  child.on('exit', () => {
    if (backendProcess === child) backendProcess = null
    try { if (stdoutFd) fs.closeSync(stdoutFd) } catch { /* ignore */ }
    try { if (stderrFd) fs.closeSync(stderrFd) } catch { /* ignore */ }
  })

  const ok = await waitForBackendHealthy()
  if (!ok) {
    return { ok: false, error: `backend at ${backendHealthUrl()} did not respond after 30s` }
  }
  return { ok: true }
}

ipcMain.handle('cremind:server:start', async () => startBackend())

// Restart the backend in-process. The Developer page's Restart Server
// button routes here when running under Electron — without this
// handler, hitting POST /api/system/restart would kill the backend
// (the ``child.on('exit')`` hook above just nulls out backendProcess
// without respawning), leaving the user with a dead app.
//
// We SIGTERM the child, wait briefly for the exit hook to fire (so
// backendProcess is already cleared and startBackend's "already
// spawned?" guard takes the cold path), then call startBackend()
// which spawns a fresh ``cremind serve`` and polls /health until it
// returns 200.
ipcMain.handle('cremind:server:restart', async (): Promise<{ ok: boolean; error?: string }> => {
  const existing = backendProcess
  if (existing && existing.exitCode === null) {
    // Wait for the exit hook before respawning so the new child isn't
    // racing the old one for the listening port.
    const exited = new Promise<void>((resolve) => {
      existing.once('exit', () => resolve())
      // Safety net — exit hook should fire within a second of SIGTERM.
      setTimeout(() => resolve(), 5000)
    })
    try { existing.kill('SIGTERM') } catch { /* already gone */ }
    await exited
  }
  return startBackend()
})

// ── In-app backend upgrade ──────────────────────────────────────────────
//
// Runs ``cremind upgrade --yes`` as a subprocess and streams its output
// to the renderer so the UpdateBanner can show a live progress modal
// instead of telling the user to open a terminal. Matches the manual
// CLI flow exactly:
//
//   - The backend stays running during the upgrade. The Python runner's
//     ``_wait_for_health`` step probes /health on the (still-running)
//     old backend; if /health doesn't answer the runner rolls back.
//     This is the same contract the CLI relies on.
//   - On successful exit we restart the backend so the new wheel's code
//     gets imported. The shell holds the subprocess handle for its
//     lifetime; without a restart, the user would keep running the old
//     code until they quit the app.
//   - On failure, the runner has already restored the backup and pip-
//     installed the previous version. We leave the (old) backend
//     running and surface the failure to the renderer.
ipcMain.handle('cremind:backend-upgrade:apply', async (event, payload?: { targetVersion?: string }) => {
  return runBackendUpgrade(event.sender, payload?.targetVersion)
})

ipcMain.handle('cremind:installer:detect', async () => detectInstallEnvironment())

ipcMain.handle('cremind:installer:list-versions', async () => listInstallVersions())

ipcMain.handle('cremind:installer:run', async (event, payload: InstallerRunPayload) => {
  if (installerProcess) {
    throw new Error('An install is already running.')
  }
  return runInstaller(event.sender, payload)
})

ipcMain.handle('cremind:installer:cancel', () => {
  if (!installerProcess) return false
  // SIGTERM gives the script a chance to clean up; if it ignores it,
  // Node will SIGKILL the orphan when the Electron app exits.
  try { installerProcess.kill('SIGTERM') } catch { /* already gone */ }
  return true
})

ipcMain.handle('cremind:installer:uninstall', async (event, mode: 'keep' | 'purge') => {
  if (uninstallerProcess) {
    throw new Error('An uninstall is already running.')
  }
  if (mode !== 'keep' && mode !== 'purge') {
    throw new Error(`Invalid uninstall mode: ${mode}`)
  }
  return runUninstaller(event.sender, mode)
})

type InstallerEnvironment = {
  os: 'linux' | 'macos' | 'windows' | 'unknown'
  arch: string
  hasDocker: boolean
  hasPython: boolean
  pythonVersion: string
  // Baked-in install channel. The renderer uses this for display
  // (badge in the welcome step) and as one of the inputs to
  // ``recommendInstallMode``; the recommended mode itself is no
  // longer computed here.
  channel: 'production' | 'test' | 'dev'
}

type InstallerRunPayload = {
  deployment: 'local' | 'server' | 'custom'
  appHost?: string
  mode: 'docker' | 'native'
  /** Advanced .env overrides for the `custom` deployment. Keys mirror
   *  install/catalog.toml's deployments.custom.advanced_fields[].key. */
  customFields?: {
    listen_host?: string
    public_url?: string
    allowed_origins?: string
    wizard_preset?: string
  }
  /** Optional explicit cremind package version, e.g. ``0.2.1`` (production)
   *  or ``0.2.1rc3`` (test). Omitted ⇒ the script resolves a default
   *  matching this Electron build's line. Validated against the channel
   *  + Electron line by both the wizard and the install script. */
  version?: string
}

type InstallVersionListing = {
  electronVersion: string
  channel: 'production' | 'test' | 'dev'
  /** Allowed versions, sorted oldest → newest under PEP 440. Empty for
   *  the ``dev`` channel (no version list applies). */
  versions: string[]
  /** Highest entry in ``versions`` — what "Latest" resolves to. */
  latest: string | null
  /** GitHub releases page for each version (for the "release notes"
   *  link in the wizard). Keyed by version. */
  htmlUrls: Record<string, string>
}

async function detectInstallEnvironment(): Promise<InstallerEnvironment> {
  // Detection runs commands that may be missing on PATH; ``runOnce`` swallows
  // non-zero exits so the UI gets a clean ``hasX = false`` rather than an
  // exception per check.
  const platform = process.platform
  const detected: InstallerEnvironment = {
    os: platform === 'linux' ? 'linux'
       : platform === 'darwin' ? 'macos'
       : platform === 'win32' ? 'windows'
       : 'unknown',
    arch: process.arch,
    hasDocker: false,
    hasPython: false,
    pythonVersion: '',
    channel: INSTALL_CHANNEL,
  }

  const dockerVersion = await runOnce('docker', ['--version']).catch(() => null)
  if (dockerVersion) {
    // ``docker info`` confirms the daemon is reachable, not just that the
    // CLI is installed. Docker Desktop on macOS/Windows commonly has the
    // CLI on PATH while the daemon is stopped.
    const info = await runOnce('docker', ['info']).catch(() => null)
    detected.hasDocker = info !== null
  }

  // Try the most-specific Python name first to avoid picking up a system
  // 3.10 named just ``python3``. Mirrors the install script's logic.
  const pythonCandidates = platform === 'win32'
    ? [['py', ['-3.13', '-c', 'import sys;print("%d.%d" % sys.version_info[:2])']]]
    : [['python3.13', ['-c', 'import sys;print("%d.%d" % sys.version_info[:2])']],
       ['python3',    ['-c', 'import sys;print("%d.%d" % sys.version_info[:2])']]]
  for (const [cmd, args] of pythonCandidates as [string, string[]][]) {
    const v = await runOnce(cmd, args).catch(() => null)
    if (v && /^3\.(1[3-9]|[2-9]\d)$/.test(v.trim())) {
      detected.hasPython = true
      detected.pythonVersion = v.trim()
      break
    }
  }

  // Recommendation is no longer computed here — the renderer derives
  // it from install/catalog.toml's [modes] table via
  // ``recommendInstallMode`` so install.sh, install.ps1, and the
  // Setup Wizard agree on the default. Detection only reports raw
  // capabilities (hasDocker, hasPython); the recommendation falls out
  // from the catalog's mode order + requires.
  return detected
}

// GitHub repo the in-app version list pulls from. Mirrors
// ``DEFAULT_REPO`` in app/upgrade/manifest.py so the install-time and
// upgrade-time listings stay aligned. Override via env var for staging
// / forks (same name as the Python side uses).
const INSTALL_VERSIONS_REPO = process.env.CREMIND_UPGRADE_REPO ?? 'cremind-ai/cremind'
const INSTALL_VERSIONS_API = `https://api.github.com/repos/${INSTALL_VERSIONS_REPO}/releases?per_page=100`

// ``vMAJOR.MINOR.PATCH[.HOTFIX]rcN.devM`` — the canonical test-channel
// tag format used by release-rc.yml (e.g. ``v0.2.9rc1.dev2``). The
// ``.devM`` counter is mandatory; bare ``vX.Y.ZrcN`` and the older
// hyphenated form ``v0.2.9-rc.1.dev.2`` are both rejected. Mirrors
// ``_RC_TAG`` in app/upgrade/channel.py.
const RC_TAG_RE = /^v(\d+\.\d+\.\d+(?:\.\d+)?)rc(\d+)\.dev(\d+)$/
// ``vMAJOR.MINOR.PATCH`` — the production tag format.
const PROD_TAG_RE = /^v(\d+)\.(\d+)\.(\d+)$/

function stripV(tag: string): string {
  return tag.startsWith('v') ? tag.slice(1) : tag
}

// PEP 440 ordering for the shapes we ship: ``X.Y.Z``, ``X.Y.ZrcN`` and the
// per-PR dev form ``X.Y.ZrcN.devM``. Mirrors ``parse_pep440`` in
// app/upgrade/channel.py (slots: major, minor, patch, isFinal, rc,
// isNotDev, dev). A dev release sorts strictly *before* its rc
// (``0.2.9rc1.dev2 < 0.2.9rc1``) but above the previous patch
// (``0.2.8 < 0.2.9rc1.dev1``); ``rcN`` sorts before the final.
//
// NOTE: this is the install-wizard's own ordering. electron-updater's
// auto-update path instead compares the SemVer ``latest-test.yml``, where
// semver ranks ``rc.1.dev.2`` *above* ``rc.1`` — opposite to PEP 440.
// Harmless for the monotonic dev.1→dev.2→dev.3 stream the release process
// actually ships; we don't fight electron-updater's semver here.
function parseVersion(v: string): [number, number, number, number, number, number, number] {
  const m = /^(\d+)\.(\d+)\.(\d+)(?:rc(\d+)(?:\.dev(\d+))?)?$/.exec(v)
  if (!m) return [0, 0, 0, 0, 0, 0, 0]
  const major = parseInt(m[1], 10)
  const minor = parseInt(m[2], 10)
  const patch = parseInt(m[3], 10)
  if (m[4] === undefined) return [major, minor, patch, 1, 0, 1, 0] // final
  const rc = parseInt(m[4], 10)
  if (m[5] === undefined) return [major, minor, patch, 0, rc, 1, 0] // rc, no dev
  return [major, minor, patch, 0, rc, 0, parseInt(m[5], 10)] // rcN.devM
}

function compareVersions(a: string, b: string): number {
  const pa = parseVersion(a)
  const pb = parseVersion(b)
  for (let i = 0; i < pa.length; i++) {
    if (pa[i] !== pb[i]) return pa[i] - pb[i]
  }
  return 0
}

function httpsGetJson(url: string, timeoutMs = 10000): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const req = https.get(
      url,
      {
        headers: {
          Accept: 'application/vnd.github+json',
          // GitHub requires a User-Agent on every request. 60 req/hr/IP
          // unauthenticated is plenty for a one-shot install lookup.
          'User-Agent': `cremind-installer/${app.getVersion()}`,
        },
      },
      (res) => {
        if (res.statusCode && res.statusCode >= 400) {
          res.resume()
          return reject(new Error(`${url} returned HTTP ${res.statusCode}`))
        }
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk) => { body += chunk })
        res.on('end', () => {
          try { resolve(JSON.parse(body)) }
          catch (e) { reject(e) }
        })
      },
    )
    req.on('error', reject)
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error(`${url} timed out after ${timeoutMs}ms`))
    })
  })
}

async function listInstallVersions(): Promise<InstallVersionListing> {
  // ``app.getVersion()`` returns the SemVer form from package.json
  // (e.g. ``0.1.9-dev.12`` on a test build). The GitHub-tag filter
  // below, the renderer's version-spec validator, and the install
  // scripts all compare against the *release line* (the bare
  // ``X.Y.Z``), so strip any pre-release / build suffix here.
  const electronVersion = app.getVersion().split(/[-+]/)[0]
  const channel = INSTALL_CHANNEL

  // Dev channel: editable install, no list applies. Returning an empty
  // listing lets the renderer skip the version sub-step entirely.
  if (channel === 'dev') {
    return { electronVersion, channel, versions: [], latest: null, htmlUrls: {} }
  }

  type GhRelease = { tag_name?: string; prerelease?: boolean; html_url?: string }
  const payload = await httpsGetJson(INSTALL_VERSIONS_API) as GhRelease[]
  if (!Array.isArray(payload)) {
    throw new Error('GitHub /releases did not return a list payload')
  }

  const htmlUrls: Record<string, string> = {}
  const allowed: string[] = []
  for (const entry of payload) {
    const tag = entry?.tag_name ?? ''
    if (channel === 'test') {
      // Test channel: any RC prerelease on any line — no line lock. The
      // test channel exists to validate arbitrary per-PR release
      // candidates, so we offer every published ``vX.Y.ZrcN.devM``
      // (a 0.2.8 shell may install a 0.2.9 RC; the shell auto-updates
      // independently). Mirrors matches_electron_line() in
      // app/upgrade/channel.py, which drops the line constraint on test.
      if (!entry.prerelease) continue
      const m = RC_TAG_RE.exec(tag)
      if (!m) continue
      // Tag is already in PEP 440 canonical shape; strip the ``v``.
      const version = tag.slice(1)
      allowed.push(version)
      if (entry.html_url) htmlUrls[version] = entry.html_url
    } else {
      // Production channel: only ``vELECTRON`` (the exact match) shows
      // up — the strict-match rule means everything else is invalid.
      if (entry.prerelease) continue
      const m = PROD_TAG_RE.exec(tag)
      if (!m) continue
      const version = stripV(tag)
      if (version !== electronVersion) continue
      allowed.push(version)
      if (entry.html_url) htmlUrls[version] = entry.html_url
    }
  }

  allowed.sort(compareVersions)
  const latest = allowed.length > 0 ? allowed[allowed.length - 1] : null
  return { electronVersion, channel, versions: allowed, latest, htmlUrls }
}

function runOnce(command: string, args: string[], timeoutMs = 5000): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'] })
    let out = ''
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      try { child.kill('SIGKILL') } catch { /* ignore */ }
    }, timeoutMs)
    child.stdout.on('data', (chunk) => { out += chunk.toString() })
    child.on('error', (err) => { clearTimeout(timer); reject(err) })
    child.on('close', (code) => {
      clearTimeout(timer)
      if (timedOut) return reject(new Error(`${command} timed out`))
      if (code !== 0) return reject(new Error(`${command} exited ${code}`))
      resolve(out.trim())
    })
  })
}

async function runInstaller(
  sender: Electron.WebContents,
  payload: InstallerRunPayload,
): Promise<{ exitCode: number }> {
  const send = (channel: string, message: unknown) => {
    if (!sender.isDestroyed()) sender.send(channel, message)
  }

  send('cremind:installer:log', { stream: 'info', line: `Detecting platform...` })
  const env = await detectInstallEnvironment()
  if (payload.mode === 'docker' && !env.hasDocker) {
    throw new Error('Docker mode selected but Docker is not available on this machine.')
  }
  if (payload.deployment === 'server' && !payload.appHost) {
    throw new Error('Server deployment requires a public host (IP or domain).')
  }

  // Resolve which install script to run.
  //
  // - Dev channel: use the local checkout's install/install.sh|ps1
  //   directly. ``--channel dev`` requires running from a checkout (the
  //   script refuses curl-pipe invocations), and ``npm run dev`` only
  //   ever runs out of the source tree so the path resolves to the
  //   working copy.
  // - Test / production: download from INSTALLER_SCRIPT_BASE. Re-running
  //   on every install picks up upstream fixes without an Electron app
  //   update.
  const isWindows = env.os === 'windows'
  const scriptName = isWindows ? 'install.ps1' : 'install.sh'
  let scriptPath: string
  if (INSTALL_CHANNEL === 'dev') {
    // __dirname after compile is <repo>/ui/dist-electron; the repo root
    // is two levels up. Resolve to <repo>/install/<scriptName>.
    scriptPath = path.resolve(__dirname, '..', '..', 'install', scriptName)
    if (!fs.existsSync(scriptPath)) {
      throw new Error(
        `Dev-channel install requires the local script at ${scriptPath}, ` +
        `but the file is missing. Run the Electron app from a checkout.`,
      )
    }
    send('cremind:installer:log', { stream: 'info', line: `Using local install script: ${scriptPath}` })
  } else {
    const scriptUrl = `${INSTALLER_SCRIPT_BASE}/${scriptName}`
    const scriptDir = path.join(app.getPath('userData'), 'installer')
    fs.mkdirSync(scriptDir, { recursive: true })
    scriptPath = path.join(scriptDir, scriptName)
    send('cremind:installer:log', { stream: 'info', line: `Downloading ${scriptUrl}...` })
    await downloadFile(scriptUrl, scriptPath)
    if (!isWindows) fs.chmodSync(scriptPath, 0o755)
  }

  // Build CLI args. We always pass --unattended + --no-launch so the
  // script doesn't prompt or open a browser — this UI handles both.
  const args: string[] = [
    '--deployment', payload.deployment,
    '--mode', payload.mode,
    '--unattended',
    '--no-launch',
  ]
  if (payload.appHost) args.push('--host', payload.appHost)
  // Forward custom-deployment overrides so the install scripts skip
  // their interactive prompts. Empty values are dropped so the scripts'
  // catalog defaults take effect for fields the user didn't fill in.
  if (payload.deployment === 'custom' && payload.customFields) {
    const cf = payload.customFields
    if (cf.listen_host)     args.push('--listen-host',     cf.listen_host)
    if (cf.public_url)      args.push('--public-url',      cf.public_url)
    if (cf.allowed_origins) args.push('--allowed-origins', cf.allowed_origins)
    if (cf.wizard_preset)   args.push('--wizard-preset',   cf.wizard_preset)
  }

  // Channel routing — both shells accept --channel / -Channel. Production
  // is the default in both installers, so only forward non-production
  // channels to keep the spawn args minimal.
  if (INSTALL_CHANNEL !== 'production') {
    args.push('--channel', INSTALL_CHANNEL)
  }

  // Version pinning. The Electron app always forwards its own build
  // version so the install script can enforce the channel-line rule
  // (production = exact, test = same-minor rcN). When the user picked
  // a specific version, forward that too — the script validates it
  // against ``--electron-version`` and exits with ``Invalid version``
  // if it falls outside the allowed set.
  // Pass the release line (``0.2.1``), not the SemVer build version
  // (``0.2.1-rc.12.dev.3``) — install.sh / install.ps1 enforce the
  // channel rule via ``${ELECTRON_VERSION}rcN``, which only works on
  // the bare ``X.Y.Z`` form.
  args.push('--electron-version', app.getVersion().split(/[-+]/)[0])
  if (payload.version) {
    args.push('--version', payload.version)
  }

  let cmd: string
  let cmdArgs: string[]
  if (isWindows) {
    cmd = 'powershell.exe'
    // -ExecutionPolicy Bypass sidesteps the user's machine policy without
    // mutating it; the bypass is scoped to this single invocation.
    const psArgs = args.flatMap((a) => {
      if (a === '--deployment')       return ['-Deployment']
      if (a === '--host')             return ['-AppHost']
      if (a === '--mode')             return ['-Mode']
      if (a === '--unattended')       return ['-Unattended']
      if (a === '--no-launch')        return ['-NoLaunch']
      if (a === '--channel')          return ['-Channel']
      if (a === '--listen-host')      return ['-ListenHost']
      if (a === '--public-url')       return ['-PublicUrl']
      if (a === '--allowed-origins')  return ['-AllowedOrigins']
      if (a === '--wizard-preset')    return ['-WizardPreset']
      if (a === '--version')          return ['-Version']
      if (a === '--electron-version') return ['-ElectronVersion']
      return [a]
    })
    cmdArgs = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', scriptPath, ...psArgs]
  } else {
    cmd = 'bash'
    cmdArgs = [scriptPath, ...args]
  }

  send('cremind:installer:log', {
    stream: 'info',
    line: `Running ${cmd} ${cmdArgs.join(' ')} (channel: ${INSTALL_CHANNEL})`,
  })

  return new Promise((resolve, reject) => {
    const child = spawn(cmd, cmdArgs, {
      env: {
        ...process.env,
        // Hand the spawned install script the same System Dir + Install
        // Dir the Electron app resolves, so both surfaces write to one
        // pair of locations.
        CREMIND_SYSTEM_DIR: systemDirPath(),
        CREMIND_INSTALL_DIR: installDirPath(),
        // Preserve any custom template/script base the user set, so
        // staging installs can be tested end-to-end from the GUI.
        CREMIND_TEMPLATE_BASE: process.env.CREMIND_TEMPLATE_BASE ?? `${INSTALLER_SCRIPT_BASE}/templates`,
        // Tell the script the install is being driven by the Electron
        // app so it suppresses the "Wizard URL: …" handoff text. The
        // Electron app navigates to the in-window wizard itself once
        // the script reports exitCode = 0.
        CREMIND_INSTALLER_FRONTEND: 'electron',
      },
    })
    installerProcess = child

    child.stdout.on('data', (chunk: Buffer) => {
      send('cremind:installer:log', { stream: 'stdout', line: chunk.toString() })
    })
    child.stderr.on('data', (chunk: Buffer) => {
      send('cremind:installer:log', { stream: 'stderr', line: chunk.toString() })
    })

    // ``'close'`` waits until every stdio handle on the child tree
    // closes. On Windows the install script launches the cremind
    // backend via ``Start-Process``, which inherits the script's
    // stdout/stderr handles even though we redirect both to log files
    // — so ``'close'`` never fires after the script itself exits, and
    // the renderer's ``onDone`` callback never runs.
    //
    // ``'exit'`` fires as soon as the script process terminates,
    // independent of any inherited handles in detached grandchildren.
    // Log data is delivered through ``stdout.on('data')`` as it streams
    // (the last line is already flushed before the script returns), so
    // we don't lose output by switching events.
    let settled = false
    const finish = (code: number | null, err?: unknown) => {
      if (settled) return
      settled = true
      installerProcess = null
      if (err !== undefined) {
        send('cremind:installer:done', { exitCode: -1, error: String(err) })
        reject(err)
        return
      }
      const exitCode = code ?? -1
      if (exitCode === 0) {
        // Persist the agent URL the wizard should connect to. The
        // script also writes its own .env files; this just keeps the
        // Electron-side runtime config in sync so the next launch
        // routes straight to /setup.
        // Resolve the agent URL the renderer should connect to next.
        //   - local : loopback
        //   - server: the public host the user typed
        //   - custom: the public URL the user gave (or localhost if blank)
        let resolvedAgentUrl: string
        if (payload.deployment === 'local') {
          resolvedAgentUrl = 'http://localhost:1515'
        } else if (payload.deployment === 'custom') {
          const pub = payload.customFields?.public_url ?? ''
          // Drop the path component, keep the scheme+host+port. Falls back
          // to localhost:1515 when the operator left public_url empty.
          if (pub) {
            try {
              const u = new URL(pub)
              resolvedAgentUrl = `${u.protocol}//${u.host}`
            } catch {
              resolvedAgentUrl = 'http://localhost:1515'
            }
          } else {
            resolvedAgentUrl = 'http://localhost:1515'
          }
        } else {
          resolvedAgentUrl = `http://${payload.appHost!}:1515`
        }
        updateConfig({
          agentUrl: resolvedAgentUrl,
          deploymentType: payload.deployment,
        })
      }
      send('cremind:installer:done', { exitCode })
      resolve({ exitCode })
    }
    child.on('error', (err) => finish(null, err))
    child.on('exit', (code) => finish(code))
  })
}

async function runUninstaller(
  sender: Electron.WebContents,
  mode: 'keep' | 'purge',
): Promise<{ exitCode: number }> {
  const send = (channel: string, message: unknown) => {
    if (!sender.isDestroyed()) sender.send(channel, message)
  }

  // Stop any backend we're tracking before yanking files. The install
  // script's --uninstall flow also kills the PID file's process, but doing
  // this here first closes any file handles the Electron process owns
  // (server.log fds in startBackend), which Windows is fussy about.
  if (backendProcess && backendProcess.pid) {
    try { backendProcess.kill('SIGTERM') } catch { /* ignore */ }
    backendProcess = null
  }

  const isWindows = process.platform === 'win32'
  // Uninstall now lives behind ``install.{sh,ps1} --uninstall`` — the
  // standalone uninstall scripts were merged into the installers.
  const scriptName = isWindows ? 'install.ps1' : 'install.sh'

  // Same script-resolution pattern as ``runInstaller`` — local checkout in
  // dev, download into userData/installer/ for test/production.
  let scriptPath: string
  if (INSTALL_CHANNEL === 'dev') {
    scriptPath = path.resolve(__dirname, '..', '..', 'install', scriptName)
    if (!fs.existsSync(scriptPath)) {
      throw new Error(
        `Dev-channel uninstall requires the local script at ${scriptPath}, ` +
        `but the file is missing.`,
      )
    }
    send('cremind:installer:uninstall:log', { stream: 'info', line: `Using local install script: ${scriptPath} (--uninstall)` })
  } else {
    const scriptUrl = `${INSTALLER_SCRIPT_BASE}/${scriptName}`
    const scriptDir = path.join(app.getPath('userData'), 'installer')
    fs.mkdirSync(scriptDir, { recursive: true })
    scriptPath = path.join(scriptDir, scriptName)
    send('cremind:installer:uninstall:log', { stream: 'info', line: `Downloading ${scriptUrl}...` })
    await downloadFile(scriptUrl, scriptPath)
    if (!isWindows) fs.chmodSync(scriptPath, 0o755)
  }

  const flag = isWindows
    ? (mode === 'purge' ? '-Purge' : '-Keep')
    : (mode === 'purge' ? '--purge' : '--keep')

  let cmd: string
  let cmdArgs: string[]
  if (isWindows) {
    cmd = 'powershell.exe'
    cmdArgs = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', scriptPath, '-Uninstall', flag]
  } else {
    cmd = 'bash'
    cmdArgs = [scriptPath, '--uninstall', flag]
  }

  send('cremind:installer:uninstall:log', {
    stream: 'info',
    line: `Running ${cmd} ${cmdArgs.join(' ')} (mode: ${mode})`,
  })

  return new Promise((resolve, reject) => {
    const child = spawn(cmd, cmdArgs, {
      env: {
        ...process.env,
        CREMIND_SYSTEM_DIR: systemDirPath(),
        CREMIND_INSTALL_DIR: installDirPath(),
      },
    })
    uninstallerProcess = child

    child.stdout.on('data', (chunk: Buffer) => {
      send('cremind:installer:uninstall:log', { stream: 'stdout', line: chunk.toString() })
    })
    child.stderr.on('data', (chunk: Buffer) => {
      send('cremind:installer:uninstall:log', { stream: 'stderr', line: chunk.toString() })
    })

    let settled = false
    const finish = (code: number | null, err?: Error) => {
      if (settled) return
      settled = true
      uninstallerProcess = null
      if (err) {
        send('cremind:installer:uninstall:done', { exitCode: -1, error: err.message })
        reject(err)
        return
      }
      const exitCode = code ?? -1
      if (exitCode === 0) {
        // After a clean uninstall, drop our cached agentUrl so the next
        // launch shows the first-run installer again. The renderer is
        // expected to also call updateConfig from its end, but doing it
        // here covers the case where the renderer crashes mid-flow.
        updateConfig({ agentUrl: '', deploymentType: '' })
      }
      send('cremind:installer:uninstall:done', { exitCode, mode })
      resolve({ exitCode })
    }
    child.on('error', (err) => finish(null, err))
    child.on('exit', (code) => finish(code))
  })
}

// ── Backend upgrade implementation ──────────────────────────────────────
//
// Spawns ``cremind upgrade --yes`` and forwards its stdout/stderr to the
// renderer line-by-line. The renderer mirrors the events into a modal
// log view. Three IPC channels are used:
//
//   cremind:backend-upgrade:status — phase transitions ('starting',
//                                   'upgrading', 'restarting')
//   cremind:backend-upgrade:log    — every raw line of upgrade output
//   cremind:backend-upgrade:done   — terminal result + exit code
//
// Reuses the existing log buffering pattern from runInstaller so the
// renderer can subscribe with the same shape as the installer bridge.

async function runBackendUpgrade(
  sender: Electron.WebContents,
  targetVersion?: string,
): Promise<{ exitCode: number; ok: boolean; error?: string }> {
  if (upgradeProcess) {
    throw new Error('An upgrade is already running.')
  }

  const send = (channel: string, message: unknown) => {
    if (!sender.isDestroyed()) sender.send(channel, message)
  }

  // Docker installs have no local venv on the host. The renderer is
  // expected to route these to POST /api/upgrade/apply (handled inside
  // the container) instead of invoking this IPC handler. If we land
  // here in Docker mode anyway — stale renderer, plugin, or future
  // regression — surface a clear error instead of the misleading
  // "venv python missing" path below.
  if (installMode === 'docker') {
    const error = 'Docker installs upgrade via the backend API, not the Electron IPC path.'
    send('cremind:backend-upgrade:done', { exitCode: -1, ok: false, error })
    return { exitCode: -1, ok: false, error }
  }

  // Spawn via the venv interpreter rather than cremind.exe so the pip
  // step inside this subprocess can replace cremind.exe on Windows —
  // see venvPythonPath() comment.
  const py = venvPythonPath()
  if (!fs.existsSync(py)) {
    const error = `venv python missing at ${py}`
    send('cremind:backend-upgrade:done', { exitCode: -1, ok: false, error })
    return { exitCode: -1, ok: false, error }
  }

  // ``--target`` (test channel only) pins a specific RC instead of the
  // latest — the runner enforces that the target is test-channel-valid.
  const applyArgs = ['-m', 'app.cli.main', 'upgrade', 'apply', '--yes']
  if (targetVersion) {
    applyArgs.push('--target', targetVersion)
  }

  send('cremind:backend-upgrade:status', { phase: 'starting' })
  send('cremind:backend-upgrade:log', {
    stream: 'info',
    line: `$ ${py} ${applyArgs.join(' ')}`,
  })

  return new Promise((resolve) => {
    let child: ChildProcess
    try {
      // ``upgrade apply --yes`` is the explicit subcommand form. The
      // older ``upgrade --yes`` form works too (the CLI's root callback
      // forwards flags down to apply), but spelling out the subcommand
      // avoids the "No such option" failure mode seen on shells / Typer
      // versions where the root callback didn't accept ``--yes``.
      child = spawn(py, applyArgs, {
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
        env: cremindSubprocessEnv(),
      })
    } catch (err) {
      const error = String(err)
      send('cremind:backend-upgrade:done', { exitCode: -1, ok: false, error })
      resolve({ exitCode: -1, ok: false, error })
      return
    }
    upgradeProcess = child
    send('cremind:backend-upgrade:status', { phase: 'upgrading' })

    const forward = (stream: 'stdout' | 'stderr') => (chunk: Buffer) => {
      // The Python runner prints one event per line; preserve that
      // boundary for the renderer instead of re-buffering by chunk.
      const text = chunk.toString()
      for (const line of text.split(/\r?\n/)) {
        if (line.length > 0) {
          send('cremind:backend-upgrade:log', { stream, line })
        }
      }
    }
    child.stdout?.on('data', forward('stdout'))
    child.stderr?.on('data', forward('stderr'))

    let settled = false
    const finish = async (code: number | null, err?: unknown) => {
      if (settled) return
      settled = true
      upgradeProcess = null

      if (err !== undefined) {
        const error = String(err)
        send('cremind:backend-upgrade:done', { exitCode: -1, ok: false, error })
        resolve({ exitCode: -1, ok: false, error })
        return
      }

      const exitCode = code ?? -1
      if (exitCode !== 0) {
        // The Python runner already restored the backup and pip-
        // installed the previous version. The old backend is still
        // running; nothing to restart.
        send('cremind:backend-upgrade:done', { exitCode, ok: false })
        resolve({ exitCode, ok: false })
        return
      }

      // Success path: restart the backend so the new wheel is loaded.
      send('cremind:backend-upgrade:status', { phase: 'restarting' })
      if (backendProcess && backendProcess.pid) {
        killProcessTreeSync(backendProcess.pid)
        backendProcess = null
      }
      // Give the OS a beat to release the listen port before we respawn;
      // SO_REUSEADDR isn't set on Windows by default and a too-fast
      // restart can hit EADDRINUSE.
      await new Promise((r) => setTimeout(r, 1000))
      const restart = await startBackend()
      if (!restart.ok) {
        send('cremind:backend-upgrade:done', {
          exitCode,
          ok: false,
          error: `backend failed to restart: ${restart.error ?? 'unknown'}`,
        })
        resolve({ exitCode, ok: false, error: restart.error })
        return
      }
      send('cremind:backend-upgrade:done', { exitCode, ok: true })
      void fetchCapabilities()
      // Reload renderers so they pick up the upgraded SPA. Windows
      // already on the backend HTTP origin reload to fetch the new
      // bundle (the wheel install just refreshed app/static/ui/, so
      // the served index.html has a new JS hash). Windows still on the
      // file:// asar — typical on the very first upgrade after a fresh
      // install, before maybePivotToBackend has had a chance to fire —
      // get navigated to the HTTP origin so they leave the stale asar.
      //
      // We wait for /health to come back before reloading. A fixed
      // delay caused renderers to reload against a partially-ready
      // backend; ProfileSelector then saw setup-status responses with
      // ``has_profiles=false`` and wiped every stored token, dumping
      // users on the login screen post-upgrade. Health-polling instead
      // means the renderer reloads against a fully-ready backend, and
      // ``getTokenForProfile`` finds the original token still in
      // localStorage. The 30 s ceiling matches startBackend's own
      // readiness budget — if we don't see /health by then, surface a
      // failure rather than reload onto a broken backend.
      void (async () => {
        const healthy = await waitForBackendHealthy(30000)
        if (!healthy) {
          send('cremind:backend-upgrade:done', {
            exitCode,
            ok: false,
            error: 'backend did not become healthy after upgrade',
          })
          return
        }
        // Stamp a one-shot grace flag on each renderer before reloading.
        // The Vue router consults sessionStorage('cremind:just_updated')
        // and, for the next ~30 s, suppresses redirect-to-login when a
        // localStorage token momentarily races with the new backend.
        const stamp = String(Date.now())
        for (const win of BrowserWindow.getAllWindows()) {
          if (win.isDestroyed()) continue
          const url = win.webContents.getURL()
          if (!url.startsWith('http://') && !url.startsWith('https://')) continue
          try {
            await win.webContents.executeJavaScript(
              `try { sessionStorage.setItem('cremind:just_updated', '${stamp}') } catch {}`,
              true,
            )
          } catch {
            // Renderer torn down between checks; reload() below will still try.
          }
          // Already on the backend HTTP origin: a plain reload refetches
          // the new bundle (the wheel install just refreshed index.html
          // and bumped the JS hash). Don't go through
          // pivotFileWindowsToBackend here — that would skip these.
          win.reload()
        }
        // Cover file:// windows in the same pass — typical on the very
        // first upgrade after a fresh install, before any pivot has run.
        // pivotFileWindowsToBackend navigates these to the SPA origin;
        // the grace flag is set by the renderer-side version poll on
        // first load instead.
        void pivotFileWindowsToBackend()
      })()
      resolve({ exitCode, ok: true })
    }
    child.on('error', (e) => { void finish(null, e) })
    child.on('exit', (code) => { void finish(code) })
  })
}

function downloadFile(url: string, dest: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(dest)
    const req = https.get(url, (res) => {
      // Follow one level of redirects so GitHub's raw URL resolving via
      // a 302 doesn't trip us up.
      if (res.statusCode === 301 || res.statusCode === 302) {
        res.resume()
        const next = res.headers.location
        if (!next) return reject(new Error(`Redirect from ${url} without Location`))
        file.close()
        return downloadFile(next, dest).then(resolve, reject)
      }
      if (!res.statusCode || res.statusCode >= 400) {
        res.resume()
        return reject(new Error(`Failed to fetch ${url}: HTTP ${res.statusCode}`))
      }
      res.pipe(file)
      file.on('finish', () => file.close((err) => err ? reject(err) : resolve()))
    })
    req.on('error', (err) => {
      try { fs.unlinkSync(dest) } catch { /* not yet written */ }
      reject(err)
    })
  })
}


// ── Multi-window: tray, jumplist, dock, and the window factory ──────────
//
// One process, one tray icon, many BrowserWindows. The tray menu, the
// Windows taskbar jumplist, and the macOS dock menu all surface the
// same three actions (VNC entry conditional on the backend being the
// `cremind/cremind-desktop` Docker image). Each click opens a fresh
// independent window — repeat clicks intentionally do NOT focus an
// existing window. The single-instance lock + the ``second-instance``
// handler ensure jumplist re-launches dispatch into the existing
// process rather than spawning a duplicate (which would yield a second
// tray icon).

function broadcastToAppWindows(channel: string, payload: unknown): void {
  for (const w of windows) {
    if (w.isDestroyed()) continue
    // VNC windows have no preload and no contextBridge, so IPC channels
    // would land in a renderer that can't decode them. Skip.
    if (windowKinds.get(w) === 'vnc') continue
    w.webContents.send(channel, payload)
  }
}

function focusMostRecentAppWindow(): void {
  const focusOne = (w: BrowserWindow): void => {
    if (w.isMinimized()) w.restore()
    if (!w.isVisible()) w.show()
    w.focus()
  }
  if (mainWin && !mainWin.isDestroyed()) { focusOne(mainWin); return }
  for (const w of windows) {
    if (w.isDestroyed()) continue
    if (windowKinds.get(w) === 'vnc') continue
    focusOne(w)
    return
  }
  createAppWindow('main')
}

// Tray / jumplist / dock entries for Process Manager, Events, and
// Channels use focus-or-open semantics — unlike "Open Main Page" /
// "Open Settings" which always spawn a fresh window. We identify an
// "already open" window by matching its current hash against
// #/<profile>/<segment>, so an in-window navigation away from the page
// correctly drops it out of the focus pool and a new click opens fresh.
function openOrFocusPageWindow(kind: PageKind): void {
  // The bare /:profile/<segment> route — not /:profile/<segment>/<deeper>.
  const re = new RegExp(`#/[^/?#]+/${kind}(?:[?#]|$)`)
  for (const w of windows) {
    if (w.isDestroyed()) continue
    if (windowKinds.get(w) === 'vnc') continue
    if (re.test(w.webContents.getURL())) {
      if (w.isMinimized()) w.restore()
      if (!w.isVisible()) w.show()
      w.focus()
      return
    }
  }
  createAppWindow(kind)
}

function vncUrlFromAgentUrl(agentUrl: string): string | null {
  if (!agentUrl) return null
  try {
    const u = new URL(agentUrl)
    // noVNC is served from the same host as the agent, but on the
    // docker-compose default port 6080. (See NOVNC_PORT:-6080 in
    // install/templates/docker-compose.yml.tmpl.)
    u.port = '6080'
    u.pathname = '/vnc.html'
    u.search = ''
    u.hash = ''
    // ``autoconnect`` skips noVNC's connect splash; ``resize=remote`` is
    // standard UX polish for a windowed viewer.
    return `${u.toString()}?autoconnect=1&resize=remote`
  } catch {
    return null
  }
}

function fetchCapabilities(): Promise<void> {
  return new Promise<void>((resolve) => {
    const finish = () => {
      rebuildTrayMenu()
      rebuildJumpList()
      rebuildDockMenu()
      resolve()
    }
    if (!runtimeConfig.agentUrl) {
      installMode = null
      uiFeatures = null
      finish()
      return
    }
    let url: URL
    try {
      // ``/tray-capabilities`` is the deliberately-public sibling of
      // ``/capabilities``. The Electron main process can't share the
      // renderer's session cookies, so the admin-gated capabilities
      // endpoint stops being reachable as soon as setup completes —
      // which silently disabled the tray gate on every post-setup
      // launch (test build looked fine because setup hadn't completed
      // yet; dev mode never worked).
      url = new URL('/api/services/tray-capabilities', runtimeConfig.agentUrl)
    } catch {
      finish()
      return
    }
    const client = url.protocol === 'https:' ? https : http
    const req = client.get(url.toString(), { timeout: 4000 }, (res) => {
      if ((res.statusCode ?? 0) >= 400) { res.resume(); finish(); return }
      let body = ''
      res.setEncoding('utf8')
      res.on('data', (chunk) => { body += chunk })
      res.on('end', () => {
        try {
          const parsed = JSON.parse(body) as {
            install_mode?: 'docker' | 'native' | null
            ui_features?: string[]
          }
          installMode = parsed.install_mode ?? null
          // Distinguish "field absent" (older backend, fall back to
          // hide-gated) from "field present but empty" (backend says
          // it ships none of these features — also hide). Either way
          // a missing entry hides the menu item; the only state that
          // shows it is an explicit listing in ``ui_features``.
          uiFeatures = Array.isArray(parsed.ui_features)
            ? new Set(parsed.ui_features)
            : null
        } catch { /* leave state unchanged on parse failure */ }
        finish()
      })
    })
    // Network errors or timeouts: keep last-known installMode to avoid
    // flickering the VNC entry off during transient outages.
    req.on('error', () => finish())
    req.on('timeout', () => { req.destroy(); finish() })
  })
}

// ── Windows taskbar jumplist ────────────────────────────────────────────
//
// Each task uses ``--open=<target>`` so the ``second-instance`` handler
// in the original process knows which window to spawn. Using
// ``setJumpList`` (rather than ``setUserTasks``) gives us an explicit
// category list, so Windows shows ONLY our entries and doesn't stitch
// in a default "launch via installed shortcut" task derived from the
// AppUserModelID.

function rebuildJumpList(): void {
  if (process.platform !== 'win32') return
  // In dev, ``process.execPath`` is node_modules/electron/dist/electron.exe;
  // running it with no args lands on default_app.asar's "Electron is
  // running" page. Pass the project root (the directory containing
  // package.json) so electron.exe boots our own main entry. In packaged
  // mode, ``process.execPath`` is "Cremind App.exe" and runs the app
  // on its own — no leading args needed.
  const baseArg = app.isPackaged ? '' : (process.env.APP_ROOT ?? '')
  const iconPath = path.join(process.env.VITE_PUBLIC, 'logo.ico')
  const mkTask = (target: WindowKind, title: string, description: string): Electron.JumpListItem => {
    const argParts: string[] = []
    if (baseArg) argParts.push(`"${baseArg}"`)
    argParts.push(`--open=${target}`)
    return {
      type: 'task',
      program: process.execPath,
      args: argParts.join(' '),
      iconPath,
      iconIndex: 0,
      title,
      description,
    }
  }

  const items: Electron.JumpListItem[] = []
  if (runtimeConfig.agentUrl && installMode === 'docker' && vncUrlFromAgentUrl(runtimeConfig.agentUrl)) {
    items.push(mkTask('vnc', 'Open VNC Desktop', 'Open the Cremind desktop VNC viewer'))
  }
  if (runtimeConfig.agentUrl) {
    items.push(mkTask('main', 'Open Main Page', 'Open a new Cremind chat window'))
    items.push(mkTask('settings', 'Open Settings', 'Open the Cremind settings window'))
    // Backend-capability gating: only surface these entries when the
    // installed cremind wheel's SPA actually has the matching route.
    // ``uiFeatureAvailable`` returns false on null (pre-protocol or
    // not-yet-fetched) so an older pinned backend never sees them.
    if (uiFeatureAvailable('processes')) {
      items.push(mkTask('processes', 'Process Manager', 'Open the Cremind process manager'))
    }
    if (uiFeatureAvailable('events')) {
      items.push(mkTask('events',    'Events',          'Open the Cremind skill events page'))
    }
    if (uiFeatureAvailable('channels')) {
      items.push(mkTask('channels',  'Channels',        'Open the Cremind channels page'))
    }
  } else {
    // Pre-install: at least give the user a way to relaunch the wizard.
    items.push(mkTask('main', 'Open Cremind', 'Open the Cremind application'))
  }

  app.setJumpList([{ type: 'tasks', items }])
}

function rebuildTrayMenu(): void {
  if (!tray) return
  const items: Electron.MenuItemConstructorOptions[] = []
  if (runtimeConfig.agentUrl && installMode === 'docker' && vncUrlFromAgentUrl(runtimeConfig.agentUrl)) {
    items.push({ label: 'Open VNC Desktop', click: () => { createAppWindow('vnc') } })
  }
  if (runtimeConfig.agentUrl) {
    items.push({ label: 'Open Main Page', click: () => { createAppWindow('main') } })
    items.push({ label: 'Open Settings',  click: () => { createAppWindow('settings') } })
    if (uiFeatureAvailable('processes')) {
      items.push({ label: 'Process Manager', click: () => openOrFocusPageWindow('processes') })
    }
    if (uiFeatureAvailable('events')) {
      items.push({ label: 'Events',          click: () => openOrFocusPageWindow('events') })
    }
    if (uiFeatureAvailable('channels')) {
      items.push({ label: 'Channels',        click: () => openOrFocusPageWindow('channels') })
    }
    items.push({ type: 'separator' })
  }
  items.push({ label: 'Show', click: () => focusMostRecentAppWindow() })
  items.push({ label: 'Exit', click: () => { app.quit() } })
  tray.setContextMenu(Menu.buildFromTemplate(items))
}

function rebuildDockMenu(): void {
  if (process.platform !== 'darwin') return
  const dock = app.dock
  if (!dock) return
  const items: Electron.MenuItemConstructorOptions[] = []
  if (runtimeConfig.agentUrl && installMode === 'docker' && vncUrlFromAgentUrl(runtimeConfig.agentUrl)) {
    items.push({ label: 'Open VNC Desktop', click: () => { createAppWindow('vnc') } })
  }
  if (runtimeConfig.agentUrl) {
    items.push({ label: 'Open Main Page', click: () => { createAppWindow('main') } })
    items.push({ label: 'Open Settings',  click: () => { createAppWindow('settings') } })
    if (uiFeatureAvailable('processes')) {
      items.push({ label: 'Process Manager', click: () => openOrFocusPageWindow('processes') })
    }
    if (uiFeatureAvailable('events')) {
      items.push({ label: 'Events',          click: () => openOrFocusPageWindow('events') })
    }
    if (uiFeatureAvailable('channels')) {
      items.push({ label: 'Channels',        click: () => openOrFocusPageWindow('channels') })
    }
  }
  dock.setMenu(Menu.buildFromTemplate(items))
}

function createTray(): void {
  const icon = nativeImage.createFromPath(path.join(process.env.VITE_PUBLIC, 'tray-logo-64x64.png'))
  tray = new Tray(icon)
  tray.setToolTip('Cremind')
  rebuildTrayMenu()
  tray.on('click', () => focusMostRecentAppWindow())
}

function createAppWindow(target: WindowKind): BrowserWindow {
  if (target === 'vnc') {
    const vncUrl = vncUrlFromAgentUrl(runtimeConfig.agentUrl)
    if (!vncUrl) {
      // Shouldn't reach here — the tray/jumplist gating hides the entry
      // when there's no agentUrl. Belt-and-suspenders: fall back to main.
      return createAppWindow('main')
    }
    const w = new BrowserWindow({
      width: 1280,
      height: 800,
      resizable: true,
      autoHideMenuBar: true,
      icon: path.join(process.env.VITE_PUBLIC, 'logo.png'),
      title: 'Cremind VNC Desktop',
      webPreferences: {
        // No preload — this window loads third-party content (noVNC)
        // and must not have access to the cremind IPC bridge.
        contextIsolation: true,
        sandbox: true,
        devTools: devToolsEnabled(),
      },
    })
    windows.add(w)
    windowKinds.set(w, 'vnc')
    w.on('closed', () => {
      windows.delete(w)
      if (mainWin === w) mainWin = null
    })
    void w.loadURL(vncUrl)
    return w
  }

  const w = new BrowserWindow({
    width: 1100,
    height: 750,
    resizable: true,
    autoHideMenuBar: true,
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: '#242424',
      symbolColor: '#ffffff',
      height: 32,
    },
    icon: path.join(process.env.VITE_PUBLIC, 'logo.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.mjs'),
      devTools: devToolsEnabled(),
    },
  })
  windows.add(w)
  windowKinds.set(w, target)
  mainWin = w

  w.on('focus', () => { mainWin = w })
  w.on('closed', () => {
    windows.delete(w)
    if (mainWin === w) mainWin = null
  })

  w.webContents.on('did-finish-load', () => {
    if (!w.isDestroyed()) {
      w.webContents.send('main-process-message', (new Date).toLocaleString())
    }
  })

  const hash = `#/?cremind_window=${target}`
  void loadMainContent(w, hash)
  return w
}

// ── Process tree cleanup on quit ────────────────────────────────────────
//
// The install script spawns the cremind backend in the background and
// writes the backend PID to <INSTALL_DIR>/install.pid. We track:
//   - the install script's own process (``installerProcess``)
//   - the backend PID from the pidfile
// and synchronously kill both trees on ``before-quit`` so closing the
// app reliably terminates everything it started — no orphaned PowerShell
// shells, no orphaned ``cremind serve`` processes.

function killProcessTreeSync(pid: number): void {
  if (!pid || pid <= 0) return
  try {
    if (process.platform === 'win32') {
      // /T = kill the entire process tree (children + grandchildren).
      // /F = force; spawn the tool synchronously so the kill completes
      // before Electron tears down.
      spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' })
    } else {
      // POSIX: try SIGTERM on the negative PID to hit the process group;
      // fall back to a regular SIGTERM on just the PID if the group call
      // isn't available (e.g., the child wasn't started with
      // ``detached: true``).
      try { process.kill(-pid, 'SIGTERM') } catch { try { process.kill(pid, 'SIGTERM') } catch { /* gone */ } }
    }
  } catch { /* ignore — process may already be gone */ }
}

function killTrackedProcessesSync(): void {
  // 1. The backend (``cremind serve``) tracked in-memory. This is the
  //    process Electron spawned via ``startBackend``; killing it
  //    directly takes the cremind.exe + uvicorn worker tree with it.
  if (backendProcess && backendProcess.pid) {
    killProcessTreeSync(backendProcess.pid)
    backendProcess = null
  }

  // 2. Fallback: the PID file (written by either the install script or
  //    ``startBackend``). Kills any backend we might have lost the
  //    in-memory reference to (e.g., the user wiped/restored the
  //    System Dir between launches).
  const serverPidFile = path.join(installDirPath(), 'install.pid')
  try {
    const pid = parseInt(fs.readFileSync(serverPidFile, 'utf8').trim(), 10)
    if (pid > 0) killProcessTreeSync(pid)
    try { fs.unlinkSync(serverPidFile) } catch { /* ignore */ }
  } catch { /* pid file missing — no install yet, or already cleaned */ }

  // 3. The install script itself, in case the user quit mid-install.
  if (installerProcess && installerProcess.pid) {
    killProcessTreeSync(installerProcess.pid)
    installerProcess = null
  }

  // 4. An in-flight ``cremind upgrade`` child, if the user quit mid-
  //    upgrade. Killing it leaves ``<SYSTEM_DIR>/.upgrade.lock`` behind;
  //    the runner's ``acquire_lock_or_recover`` rolls back from the
  //    captured backup on the next backend boot.
  if (upgradeProcess && upgradeProcess.pid) {
    killProcessTreeSync(upgradeProcess.pid)
    upgradeProcess = null
  }
}

// ── Auto-update (electron-updater + GitHub Releases) ────────────────────────
//
// The updater config (``publish:`` in electron-builder.json5) tells the
// runtime where to look for ``latest.yml`` and the binaries. Here we just
// drive the lifecycle:
//
//   - On app start (when packaged + autoUpdate=true), kick off an
//     ``checkForUpdates`` call. electron-updater handles HTTPS, signature
//     verification, and the disk cache.
//   - As the updater progresses, forward events to the renderer via
//     ``cremind:updater:status`` so the UpdateBanner can render. We never
//     auto-quit; the renderer asks the user before installing.
//   - ``cremind:updater:install`` quits and applies on user confirmation.
//
// Calling these from a dev launch (where the app isn't packaged) is a
// no-op — electron-updater refuses to update an unpackaged process,
// which is what we want.

let updater: AutoUpdaterModule['autoUpdater'] | null = null

function setupAutoUpdater(): void {
  if (!app.isPackaged) return  // dev launches don't auto-update
  try {
    // Require lazily so a missing optional dep doesn't break the
    // whole main process (e.g., during a CI test run with deps stripped).
    const mod: AutoUpdaterModule = require('electron-updater')
    updater = mod.autoUpdater
  } catch (err) {
    console.warn('[main] electron-updater unavailable, skipping auto-update:', err)
    return
  }

  // electron-updater discovers releases via a channel-specific yml file
  // (``latest.yml`` for production, ``latest-test.yml`` for test, etc.).
  // The matching name is taken from ``updater.channel`` — without this,
  // a test-channel build would still check ``latest.yml`` and so would
  // auto-update to the next production release, jumping its user off
  // the prerelease stream entirely. ``production`` keeps the default.
  if (INSTALL_CHANNEL !== 'production') {
    updater.channel = INSTALL_CHANNEL
  }

  updater.autoDownload = false           // we ask the user first
  updater.autoInstallOnAppQuit = true     // staged install on next quit

  const send = (status: string, payload: Record<string, unknown> = {}) => {
    // No windows yet? Drop — renderers re-query via
    // ``cremind:updater:check`` on mount to fetch the latest state.
    broadcastToAppWindows('cremind:updater:status', { status, ...payload })
  }

  updater.on('checking-for-update',    () => send('checking'))
  updater.on('update-available',       (info) => send('available', { info }))
  updater.on('update-not-available',   (info) => send('up_to_date', { info }))
  updater.on('error',                  (err)  => send('error', { error: String(err) }))
  updater.on('download-progress',      (prog) => send('downloading', { progress: prog }))
  updater.on('update-downloaded',      (info) => send('ready', { info }))

  if (runtimeConfig.autoUpdate !== false) {
    updater.checkForUpdates().catch((err) => {
      console.warn('[main] initial update check failed:', err)
    })
  }
}

ipcMain.handle('cremind:updater:check', async () => {
  if (!updater) return { status: 'unavailable' }
  try {
    const result = await updater.checkForUpdates()
    if (result?.updateInfo) {
      return { status: 'available', info: result.updateInfo }
    }
    return { status: 'up_to_date' }
  } catch (err) {
    return { status: 'error', error: String(err) }
  }
})

ipcMain.handle('cremind:updater:download', async () => {
  if (!updater) return { ok: false, error: 'updater unavailable' }
  try {
    await updater.downloadUpdate()
    return { ok: true }
  } catch (err) {
    return { ok: false, error: String(err) }
  }
})

ipcMain.handle('cremind:updater:install', () => {
  if (!updater) return { ok: false, error: 'updater unavailable' }
  // ``isSilent=false, isForceRunAfter=true`` → quit + install + relaunch.
  setImmediate(() => updater!.quitAndInstall(false, true))
  return { ok: true }
})

// Identify Cremind distinctly to Windows shell as early as possible
// (before app.whenReady, before any window is created). Must match the
// appId in electron-builder.json5 so the installer's shortcut and the
// running process group under the same taskbar entry.
//
// Dev runs use a ".dev" suffix so they don't share a taskbar identity
// with the installed packaged build — otherwise Windows surfaces the
// installed "Cremind App" Start Menu shortcut as an extra jumplist
// entry that launches a parallel packaged instance, bypassing our
// single-instance lock.
//
// Note: in dev mode the jumplist will still show an extra "Electron"
// entry because Windows derives a fallback launch label from the
// running .exe's VersionInfo (node_modules/electron/dist/electron.exe
// declares FileDescription="Electron") when the AppUserModelID has no
// registered Start Menu shortcut. The packaged build doesn't hit this
// — its .exe has the right metadata and the installer registers a
// proper shortcut.
if (process.platform === 'win32') {
  app.setAppUserModelId(app.isPackaged ? 'cremind-ui.client' : 'cremind-ui.client.dev')
}

// Single-instance lock — clicking the taskbar jumplist task re-runs the
// .exe, which would otherwise spawn a duplicate process. Acquiring the
// lock makes the second invocation exit immediately while the original
// process opens the requested window via the ``second-instance`` event.
// This is also what guarantees a single tray icon: only the first
// process ever runs createTray().
//
// Acquire the lock BEFORE registering any lifecycle handlers. A losing
// second instance calls ``app.quit()`` below, which fires ``before-quit``;
// if that handler were wired up at module top level it would run inside
// the secondary process and ``killTrackedProcessesSync()`` would read
// ``<INSTALL_DIR>/install.pid`` (written by the primary) and force-kill the
// primary's backend tree. Registering lifecycle handlers only inside the
// primary branch closes that hole.
const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  // Closing the last window does NOT quit. The tray icon stays alive so
  // the user can reopen Main / Settings / VNC from it. Exit is reached
  // only via Tray > Exit, which calls app.quit() and triggers the
  // before-quit cleanup below. Default Electron behavior on non-macOS
  // would quit here; this handler suppresses that.
  app.on('window-all-closed', () => { /* keep app alive in tray */ })

  app.on('before-quit', () => {
    killTrackedProcessesSync()
  })

  app.on('second-instance', (_event, argv) => {
    const openArg = argv.find((a) => a.startsWith('--open='))
    if (openArg) {
      const target = openArg.slice('--open='.length)
      if (target === 'main' || target === 'settings' || target === 'vnc') {
        createAppWindow(target)
        return
      }
      if (target === 'processes' || target === 'events' || target === 'channels') {
        openOrFocusPageWindow(target)
        return
      }
    }
    focusMostRecentAppWindow()
  })

  // ── window.open handler ─────────────────────────────────────────────────
  //
  // The renderer pops out same-origin SPA routes via ``window.open`` — most
  // notably the Process Manager terminal (ProcessList.vue's openTerminal).
  // Without a handler, Electron's default opens those as bare windows with NO
  // preload, so ``window.cremind`` is absent and the terminal's agentUrl/token
  // resolution falls back to fragile heuristics — which is why the terminal
  // streamed nothing. Give same-origin SPA pop-outs the same preload bridge,
  // titlebar, and (implicitly) session as createAppWindow, so they resolve the
  // backend exactly like the main window.
  //
  // Only same-origin http/https/file URLs are treated as SPA pop-outs. The
  // ``!internalSpa`` branch (action:'allow' → a new Electron window) now serves
  // ONLY programmatic ``window.open`` cases that must stay in-app:
  //   - OAuth/A2A sign-in popups and their provider sub-popups — the redirect
  //     chain ends back on our origin and posts ``a2a-auth-complete`` to the
  //     opener, which only works inside the same Electron context.
  //   - ``blob:`` file previews (openFile.ts) and ``about:blank`` print tabs
  //     (MessageBubble.vue) — shell.openExternal can't render those.
  // External user-CLICKED links (GitHub / PyPI / release notes / device-code)
  // never reach here: the renderer's capture-phase anchor interceptor
  // (ui/src/utils/externalLinks.ts) catches them first and routes them to the
  // system browser via the ``cremind:open-external`` IPC.
  app.on('web-contents-created', (_e, contents) => {
    contents.setWindowOpenHandler(({ url }) => {
      let internalSpa = false
      try {
        const target = new URL(url)
        const opener = new URL(contents.getURL())
        internalSpa =
          (target.protocol === 'http:' ||
            target.protocol === 'https:' ||
            target.protocol === 'file:') &&
          target.origin === opener.origin
      } catch {
        /* malformed / about:blank — treat as external, keep default */
      }
      if (!internalSpa) return { action: 'allow' }
      return {
        action: 'allow',
        overrideBrowserWindowOptions: {
          width: 1100,
          height: 750,
          autoHideMenuBar: true,
          titleBarStyle: 'hidden',
          titleBarOverlay: { color: '#242424', symbolColor: '#ffffff', height: 32 },
          icon: path.join(process.env.VITE_PUBLIC, 'logo.png'),
          webPreferences: {
            preload: path.join(__dirname, 'preload.mjs'),
            devTools: devToolsEnabled(),
          },
        },
      }
    })
  })

  app.whenReady().then(async () => {
    // Load the persisted config before the window opens so the preload's
    // sync IPC returns a populated value on the very first request.
    runtimeConfig = loadConfig();
    // Reconcile with <SYSTEM_DIR>/.env so a deleted install dir re-triggers
    // the first-run installer instead of falling through to a broken UI.
    reconcileInstallStateWithDisk();
    // First-run / re-install detection. ``userData`` (which holds
    // localStorage, IndexedDB, cookies) is NOT cleared by the Windows
    // uninstaller and is shared across all channels — without this,
    // the SetupWizard sees ``agent_token_*`` / ``logged_in_profiles``
    // from a prior install. The renderer-side ``_migrateOldToken``
    // runs at module load, so the clear must happen before any window
    // is created.
    if (!installMarkerExists()) {
      try {
        await session.defaultSession.clearStorageData({
          storages: ['localstorage', 'indexdb', 'cookies', 'serviceworkers'],
        });
        console.log('[cremind] first-run detected: cleared renderer storage');
      } catch (err) {
        console.warn('[cremind] failed to clear renderer storage', err);
      }
    }
    createTray();
    rebuildJumpList();
    rebuildDockMenu();
    createAppWindow('main');
    setupAutoUpdater();
    // Subsequent launches: if the install has already completed, fire the
    // backend up so the chat / profile-selector views have a server to
    // talk to. First-run launches skip this — the backend (and the
    // SQLite DB) only spawn after the user clicks Continue in the
    // installer flow.
    if (installMarkerExists()) {
      void (async () => {
        const r = await startBackend()
        if (r.ok) {
          await fetchCapabilities()
          // Cold-start race fix. createAppWindow runs before startBackend
          // (see the comment block above), so loadMainContent's
          // isBackendHealthy() probe got ECONNREFUSED and the renderer
          // fell back to the asar's frozen SPA. Now that the backend is
          // up, navigate any file:// windows onto the wheel-served SPA
          // so wheel-only UI updates take effect on a clean restart —
          // without this, every quit/relaunch leaves the renderer stuck
          // on whatever SPA was built into the installer's asar.
          void pivotFileWindowsToBackend()
        }
      })()
    }
  })
}
