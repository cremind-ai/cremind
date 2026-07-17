/// <reference types="vite/client" />

// Global flag for Electron environment detection (build-time define).
declare const __IS_ELECTRON__: boolean;

// Build-time app version (synced from app/__version__.py by
// scripts/sync_ui_version.py and exposed via Vite ``define``).
declare const __APP_VERSION__: string;

// Build-time ISO timestamp of when this bundle was compiled. Logged at boot
// (src/main.ts) so "which bundle is this tab running?" is answerable at a glance
// — a stale SPA tab never refetches index.html on its own, so this is the
// definitive staleness signal.
declare const __BUILT_AT__: string;

// Build-time install channel — which Cremind release stream the bundled
// installer points at. ``npm run dev`` → ``"dev"``; ``npm run build``
// → ``"production"``; ``npm run build:test`` → ``"test"``. Computed in
// vite.config.ts from ``mode`` + ``command``.
declare const __CREMIND_INSTALL_CHANNEL__: 'production' | 'test' | 'dev';

// Runtime config bridge — populated by electron/preload.ts. ``config`` is
// a synchronous snapshot loaded before the renderer initializes; the
// async getters/setters round-trip to the main process so the wizard can
// update the persisted JSON.
//
// Always defined under Electron. Undefined under the web build, so callers
// must guard with optional chaining and fall back to ``import.meta.env``.
type CremindInstallerEnvironment = {
  os: 'linux' | 'macos' | 'windows' | 'unknown'
  arch: string
  hasDocker: boolean
  hasPython: boolean
  pythonVersion: string
  channel: 'production' | 'test' | 'dev'
}

type CremindInstallerRunPayload = {
  deployment: 'local' | 'server' | 'custom'
  appHost?: string
  mode: 'docker' | 'native'
  /** Docker mode only: include the VNC Desktop UI (true → cremind-desktop)
   *  or the headless basic image (false → cremind/cremind). */
  desktopUi?: boolean
  /** Advanced .env overrides for the `custom` deployment. Keys mirror
   *  install/catalog.toml's deployments.custom.advanced_fields[].key. */
  customFields?: {
    listen_host?: string
    public_url?: string
    allowed_origins?: string
    wizard_preset?: string
  }
  /** Optional explicit cremind package version. Omitted ⇒ resolve the
   *  channel default matching this Electron build's line. */
  version?: string
}

type CremindInstallerLog = { stream: 'stdout' | 'stderr' | 'info'; line: string }

type CremindInstallerDone = { exitCode: number; error?: string }

type CremindInstallVersions = {
  electronVersion: string
  channel: 'production' | 'test' | 'dev'
  versions: string[]
  latest: string | null
  htmlUrls: Record<string, string>
}

type CremindUninstallMode = 'keep' | 'purge'
type CremindUninstallDone = { exitCode: number; mode?: CremindUninstallMode; error?: string }

type CremindInstallerBridge = {
  detect: () => Promise<CremindInstallerEnvironment>
  listVersions: () => Promise<CremindInstallVersions>
  run: (payload: CremindInstallerRunPayload) => Promise<{ exitCode: number }>
  cancel: () => Promise<boolean>
  onLog: (cb: (entry: CremindInstallerLog) => void) => void
  offLog: (cb: (entry: CremindInstallerLog) => void) => void
  onDone: (cb: (result: CremindInstallerDone) => void) => void
  offDone: (cb: (result: CremindInstallerDone) => void) => void
  // Uninstall flow — mirrors the install side. ``uninstall`` resolves
  // once the spawned uninstall.{sh,ps1} exits; ``onUninstallLog`` /
  // ``onUninstallDone`` carry progress + the terminal result.
  uninstall: (mode: CremindUninstallMode) => Promise<{ exitCode: number }>
  onUninstallLog: (cb: (entry: CremindInstallerLog) => void) => void
  offUninstallLog: (cb: (entry: CremindInstallerLog) => void) => void
  onUninstallDone: (cb: (result: CremindUninstallDone) => void) => void
  offUninstallDone: (cb: (result: CremindUninstallDone) => void) => void
}

// Status events emitted by electron-updater. We mirror the underlying
// event names verbatim so the renderer doesn't have to reinterpret
// what each phase means; ``status`` is what to render.
type CremindUpdaterStatus = {
  status: 'unavailable' | 'checking' | 'available' | 'up_to_date' |
          'downloading' | 'ready' | 'error'
  info?: { version?: string; releaseName?: string; releaseNotes?: string }
  progress?: { percent?: number; transferred?: number; total?: number; bytesPerSecond?: number }
  error?: string
}

type CremindUpdaterBridge = {
  check: () => Promise<CremindUpdaterStatus>
  download: () => Promise<{ ok: boolean; error?: string }>
  install: () => Promise<{ ok: boolean; error?: string }>
  onStatus: (cb: (payload: CremindUpdaterStatus) => void) => void
  offStatus: (cb: (payload: CremindUpdaterStatus) => void) => void
}

type CremindServerBridge = {
  // Spawn ``cremind serve`` (idempotent — no-op when something already
  // answers /health). Returns ``{ ok: true }`` once the backend's
  // health endpoint is reachable, or ``{ ok: false, error }`` if it
  // failed to start.
  start: () => Promise<{ ok: boolean; error?: string }>
  // SIGTERM the running backend child and respawn it. Used by the
  // Developer page's Restart Server button under Electron. Resolves
  // once the new backend's /health returns 200.
  restart: () => Promise<{ ok: boolean; error?: string }>
}

// In-app ``cremind upgrade`` flow. ``apply`` spawns the CLI under the
// main process; ``onLog`` streams every line it prints; ``onStatus``
// emits coarse phase transitions; ``onDone`` carries the terminal
// result. ``apply`` itself resolves with the same final shape, so
// callers can await it as a one-shot without subscribing to events.
type CremindBackendUpgradePhase = 'starting' | 'upgrading' | 'restarting'
type CremindBackendUpgradeStatus = { phase: CremindBackendUpgradePhase }
type CremindBackendUpgradeLog = { stream: 'stdout' | 'stderr' | 'info'; line: string }
type CremindBackendUpgradeDone = { exitCode: number; ok: boolean; error?: string }

type CremindBackendUpgradeBridge = {
  // ``targetVersion`` (test channel only) installs a specific RC instead
  // of the latest — used by the Updates-page version picker.
  apply: (targetVersion?: string) => Promise<CremindBackendUpgradeDone>
  onStatus: (cb: (payload: CremindBackendUpgradeStatus) => void) => void
  offStatus: (cb: (payload: CremindBackendUpgradeStatus) => void) => void
  onLog: (cb: (entry: CremindBackendUpgradeLog) => void) => void
  offLog: (cb: (entry: CremindBackendUpgradeLog) => void) => void
  onDone: (cb: (result: CremindBackendUpgradeDone) => void) => void
  offDone: (cb: (result: CremindBackendUpgradeDone) => void) => void
}

type CremindBridge = {
  config: {
    agentUrl: string
    deploymentType: 'local' | 'server' | 'custom' | ''
    autoUpdate: boolean
    channel: 'stable' | 'beta' | 'dev'
  }
  getConfig: () => Promise<CremindBridge['config']>
  setConfig: (
    patch: Partial<CremindBridge['config']>,
  ) => Promise<CremindBridge['config']>
  // Open a URL in the OS default handler (browser / mail client / dialer).
  openExternal: (url: string) => Promise<void>
  installer: CremindInstallerBridge
  server: CremindServerBridge
  updater: CremindUpdaterBridge
  backendUpgrade: CremindBackendUpgradeBridge
}

interface Window {
  cremind?: CremindBridge
}
