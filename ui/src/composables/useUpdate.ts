/**
 * Unified update composable.
 *
 * One source of truth for both the global UpdateBanner and the
 * Updates page. Consumers get a single ``state`` ref plus
 * an ``applyUpdate()`` action; they don't have to know whether the
 * pending change is a backend wheel, an Electron shell, or both.
 *
 * Two inputs feed the state:
 *
 *   1. Backend ``GET /api/upgrade/check``  — polled at mount + every 6h,
 *      and re-fetched on demand via ``checkNow()``.
 *   2. Electron ``window.cremind.updater``  — subscribed via the existing
 *      onStatus bridge. Undefined under the web build; the rest still
 *      works.
 *
 * Two upgrade tracks are coordinated:
 *
 *   - Backend upgrade: Electron uses the existing ``backendUpgrade``
 *     IPC (in-process under main, streams events). Web UI uses the new
 *     ``POST /api/upgrade/apply`` + SSE/poll on ``/api/upgrade/status``.
 *   - Shell upgrade: ``window.cremind.updater.download()`` runs in
 *     parallel when a shell update is pending. The user confirms with
 *     "Restart now" once both tracks have settled.
 *
 * The unified phase intentionally hides "which component changed" —
 * the user sees one "Cremind vX → vY available" card. Internal `[shell]`
 * / `[backend]` tags on log lines preserve traceability for the curious
 * but don't surface in summary copy.
 */

import { computed, onUnmounted, ref, watch } from 'vue'

import { useSettingsStore } from '../stores/settings'
import type {
  BackendStatus,
  BackendUpgradeDone,
  BackendUpgradeLog,
  BackendUpgradeStatus,
  ReleaseChoice,
  UnifiedUpdatePhase,
  UnifiedUpdateState,
  UpdaterStatus,
  UpgradeApplyResponse,
  UpgradeStatusFile,
  VersionsResponse,
} from '../types/updates'

const LOG_TAIL_MAX = 500
const BACKEND_POLL_MS = 6 * 60 * 60 * 1000  // 6h — same cadence as the old banner
const STATUS_POLL_MS = 1500                  // web-UI poll cadence during applying
const VERSION_POLL_MS = 30000                // baseline /version poll cadence
const FAST_VERSION_POLL_MS = 2000            // /version cadence while applying
//
// Why two cadences: the 30s baseline is enough for picking up
// out-of-band restarts (sysadmin manually restarted the backend, etc.).
// During an active upgrade we want to detect the new backend within a
// couple of seconds of it coming up — the version change is the
// ground-truth completion signal that doesn't depend on the status-file
// handoff working perfectly across container restart.

// ── module-level singletons ──────────────────────────────────────────────
//
// The composable returns refs that survive component remounts because
// both UpdateBanner (mounted at App.vue) and the Updates page (mounted
// on route change) need to observe the same upgrade. Without sharing
// state, opening the Updates page during an upgrade would reset the
// modal log to empty.

const backend = ref<BackendStatus>({ status: 'unknown' })
const updater = ref<UpdaterStatus>({ status: 'unavailable' })
// Test-channel only: the list behind the Updates-page version picker.
const availableVersions = ref<ReleaseChoice[]>([])
const log = ref<string[]>([])
const phase = ref<UnifiedUpdatePhase>('idle')
const error = ref<string | null>(null)
const explicitRestartRequired = ref(false)

let initialised = false
let backendTimer: ReturnType<typeof setInterval> | null = null
let statusTimer: ReturnType<typeof setInterval> | null = null
let statusController: AbortController | null = null
let versionTimer: ReturnType<typeof setInterval> | null = null
let fastVersionTimer: ReturnType<typeof setInterval> | null = null
let bootBackendVersion: string | null = null

function isElectron(): boolean {
  return typeof window !== 'undefined' && !!window.cremind
}

function pushLog(line: string): void {
  log.value.push(line)
  if (log.value.length > LOG_TAIL_MAX) {
    log.value.splice(0, log.value.length - LOG_TAIL_MAX)
  }
}

function setPhase(next: UnifiedUpdatePhase, errMsg: string | null = null): void {
  phase.value = next
  error.value = errMsg
}

// ── Backend /api/upgrade/check polling ───────────────────────────────────

async function checkBackend(): Promise<void> {
  const settings = useSettingsStore()
  if (!settings.agentUrl) return
  try {
    const r = await fetch(`${settings.agentUrl}/api/upgrade/check`)
    if (!r.ok) {
      backend.value = { status: 'error', reason: `HTTP ${r.status}` }
      return
    }
    backend.value = await r.json()
  } catch {
    // Transient network failure — leave the previous state in place
    // so the banner doesn't flicker.
  }
  // Test channel only: refresh the picker list. The endpoint returns an
  // empty list on production/dev, so the picker never appears there.
  const ch = 'channel' in backend.value ? backend.value.channel : undefined
  if (ch === 'test') {
    void fetchVersions()
  } else {
    availableVersions.value = []
  }
}

// ── Test-channel version list (GET /api/upgrade/versions) ────────────────

async function fetchVersions(): Promise<void> {
  const settings = useSettingsStore()
  if (!settings.agentUrl) return
  try {
    const r = await fetch(`${settings.agentUrl}/api/upgrade/versions`)
    if (!r.ok) {
      availableVersions.value = []
      return
    }
    const data = (await r.json()) as VersionsResponse
    availableVersions.value = Array.isArray(data.versions) ? data.versions : []
  } catch {
    // Leave the previous list in place on a transient failure.
  }
}

// ── Electron updater bridge ──────────────────────────────────────────────

function onUpdaterStatus(s: UpdaterStatus): void {
  updater.value = s
  // Forward to the unified log when a shell upgrade is in flight, so
  // the user sees the download progress alongside the backend output.
  if (phase.value === 'applying') {
    if (s.status === 'downloading') {
      const pct = s.progress?.percent
      if (pct != null) pushLog(`[shell] Downloading… ${Math.round(pct)}%`)
    } else if (s.status === 'ready') {
      pushLog('[shell] Update downloaded — ready to install.')
      explicitRestartRequired.value = true
    } else if (s.status === 'error') {
      pushLog(`[shell] Update error: ${s.error ?? 'unknown'}`)
    }
  }
}

// ── Electron backend-upgrade IPC ─────────────────────────────────────────

function onElectronUpgradeStatus(p: BackendUpgradeStatus): void {
  pushLog(`[backend] ${labelForBackendPhase(p.phase)}`)
}

function onElectronUpgradeLog(entry: BackendUpgradeLog): void {
  pushLog(`[backend] ${entry.line}`)
}

function onElectronUpgradeDone(result: BackendUpgradeDone): void {
  if (result.ok) {
    // Don't flip to 'done' yet if a shell download is mid-flight —
    // reconcileTerminalPhase handles that.
    reconcileTerminalPhase({ backendOk: true })
  } else {
    setPhase(
      'failed',
      result.error ?? `Upgrade exited with code ${result.exitCode}`,
    )
  }
}

function labelForBackendPhase(p: string): string {
  switch (p) {
    case 'starting': return 'Starting upgrade…'
    case 'upgrading': return 'Installing new version and migrating database…'
    case 'restarting': return 'Restarting backend…'
    default: return p
  }
}

// ── Install mode (for transport routing) ─────────────────────────────────
//
// Docker installs have no local venv on the host, so the Electron IPC
// upgrade path (which spawns local Python) can't work. We must route
// them through the web HTTP POST instead. ``/api/services/tray-capabilities``
// is the deliberately-public endpoint that exposes ``install_mode`` —
// same source Electron's main process uses for its own gating. Cached
// because applyUpdate() consults this on every click.

let cachedInstallMode: string | null | undefined
async function getInstallMode(): Promise<string | null> {
  if (cachedInstallMode !== undefined) return cachedInstallMode
  const settings = useSettingsStore()
  if (!settings.agentUrl) {
    cachedInstallMode = null
    return null
  }
  try {
    const r = await fetch(`${settings.agentUrl}/api/services/tray-capabilities`)
    if (!r.ok) { cachedInstallMode = null; return null }
    const body = (await r.json()) as { install_mode?: string | null }
    cachedInstallMode = body.install_mode ?? null
  } catch {
    cachedInstallMode = null
  }
  return cachedInstallMode
}

// ── Web-UI POST /apply + status poll ─────────────────────────────────────

async function applyWebUpgrade(targetVersion?: string): Promise<void> {
  const settings = useSettingsStore()
  if (!settings.agentUrl) {
    setPhase('failed', 'No backend URL configured.')
    return
  }
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (settings.authToken) {
    headers['Authorization'] = `Bearer ${settings.authToken}`
  }
  // Only send a body when pinning a specific version (test channel). The
  // backend ignores ``target_version`` off the test channel anyway.
  const fetchInit: RequestInit = { method: 'POST', headers }
  if (targetVersion) {
    fetchInit.body = JSON.stringify({ target_version: targetVersion })
  }
  let kicked: UpgradeApplyResponse
  try {
    const r = await fetch(`${settings.agentUrl}/api/upgrade/apply`, fetchInit)
    if (r.status === 409) {
      // Another upgrade is already running — quietly attach to it.
      pushLog('[backend] An upgrade is already running; attaching to it.')
    } else if (!r.ok) {
      const body = await r.json().catch(() => ({}))
      setPhase('failed', body.error ?? `HTTP ${r.status}`)
      return
    } else {
      kicked = (await r.json()) as UpgradeApplyResponse
      pushLog(`[backend] Spawned upgrade runner (pid ${kicked.pid}).`)
    }
  } catch (e) {
    setPhase('failed', e instanceof Error ? e.message : String(e))
    return
  }
  // Start polling the status file. We use polling rather than SSE
  // because the SSE stream dies when the backend restarts itself
  // mid-upgrade; the poll just retries until the new backend is up.
  startStatusPolling()
}

function startStatusPolling(): void {
  if (statusTimer) return
  let lastLogLen = log.value.length
  const tick = async () => {
    const settings = useSettingsStore()
    if (!settings.agentUrl) return
    const headers: Record<string, string> = {}
    if (settings.authToken) {
      headers['Authorization'] = `Bearer ${settings.authToken}`
    }
    try {
      statusController?.abort()
      statusController = new AbortController()
      const r = await fetch(`${settings.agentUrl}/api/upgrade/status`, {
        headers, signal: statusController.signal,
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const body = (await r.json()) as UpgradeStatusFile
      mergeStatusFile(body, lastLogLen)
      lastLogLen = log.value.length
      if (body.phase === 'done' || body.phase === 'failed') {
        stopStatusPolling()
        reconcileTerminalPhase({
          backendOk: body.phase === 'done',
          backendError: body.error,
        })
      }
    } catch {
      // Backend likely restarting — the runner kills the parent at the
      // end of the upgrade and the supervisor (Docker / Electron) takes
      // a few seconds to bring it back. We deliberately keep polling at
      // the fixed brisk STATUS_POLL_MS cadence (no exponential backoff)
      // so we re-attach promptly when the new backend is up; slow
      // polling defeats the post-restart UX.
    }
  }
  statusTimer = setInterval(() => { void tick() }, STATUS_POLL_MS)
  void tick()
}

function stopStatusPolling(): void {
  if (statusTimer) clearInterval(statusTimer)
  statusTimer = null
  statusController?.abort()
  statusController = null
}

function mergeStatusFile(s: UpgradeStatusFile, knownLines: number): void {
  // The server already trims to LOG_TAIL_MAX; we want only the lines
  // we haven't shown yet. Comparing by index is fine because the tail
  // is append-only until trimmed, and the trim drops the *oldest*
  // entries — by the time that matters the renderer has them too.
  const incoming = s.log_tail ?? []
  // Tag every web-UI status line as [backend] so the combined log
  // visually distinguishes from [shell] download progress.
  for (let i = knownLines; i < incoming.length; i++) {
    pushLog(`[backend] ${incoming[i]}`)
  }
  // Refresh current/target version if the server learned them.
  if (s.current_version && (backend.value as any).current !== s.current_version) {
    backend.value = {
      ...(backend.value as any),
      current: s.current_version,
    }
  }
}

// ── Terminal-phase reconciliation ────────────────────────────────────────

type ReconcileArgs = { backendOk: boolean; backendError?: string | null }

/** Decide the final unified phase once a track finishes. */
function reconcileTerminalPhase(args: ReconcileArgs): void {
  // Either signal landing here means the upgrade is over; the fast
  // /version poll has done its job (or won't be needed). Stop it
  // before we touch phase so any in-flight tick can't race a
  // setPhase('done') against a setPhase('failed').
  stopFastVersionPoll()
  if (!args.backendOk) {
    setPhase('failed', args.backendError ?? 'Upgrade failed.')
    return
  }
  // Backend succeeded. If a shell update was downloaded, ask the user
  // to restart for it (which will also pick up the new backend on boot).
  if (explicitRestartRequired.value || updater.value.status === 'ready') {
    setPhase('restart_required')
    return
  }
  setPhase('done')
  // After 'done' we re-check the backend so the banner clears the
  // moment the new version reports itself. ``done`` itself is a
  // transient state the renderer can use to show a success toast for
  // a couple of seconds before settling back to ``idle``.
  void checkBackend()
  // Trigger the post-update reload deterministically right now,
  // rather than waiting up to 30 s for the next slow /version poll.
  // Without this the user can land on About (or Settings) while the
  // backend reports the new version but the in-memory SPA bundle is
  // still the old one — exactly the "UI didn't update" symptom that
  // appeared after test47 fixed the container restart. We re-fetch
  // /version explicitly here because a stale ``bootBackendVersion``
  // seed (or a fast-poll race) would otherwise let the success path
  // skip the reload entirely. If /version is briefly unreachable
  // because the new backend isn't quite ready yet, the slow 30 s
  // pollVersion still catches it later.
  void (async () => {
    if (bootBackendVersion === null) return
    const current = await fetchBackendVersion()
    if (current && current !== bootBackendVersion) {
      triggerPostUpdateReload()
    }
  })()
}

// ── Version polling for auto-reload ──────────────────────────────────────
//
// Every tab — Web UI or Electron renderer — polls ``/version`` and
// reloads itself if the ``backend`` field changes vs. the value seen
// at boot. This is what gives a browser tab the "auto-pick-up the new
// UI after an upgrade" behaviour the Electron main process already
// provides via ``win.reload()``. In Electron the two paths race
// harmlessly: whichever fires first reloads the window, the other
// becomes a no-op once the version matches again.

async function fetchBackendVersion(): Promise<string | null> {
  const settings = useSettingsStore()
  if (!settings.agentUrl) return null
  try {
    const r = await fetch(`${settings.agentUrl}/version`)
    if (!r.ok) return null
    const body = (await r.json()) as { backend?: string }
    return typeof body.backend === 'string' ? body.backend : null
  } catch {
    return null
  }
}

function triggerPostUpdateReload(): void {
  try {
    sessionStorage.setItem('cremind:just_updated', String(Date.now()))
  } catch {
    // sessionStorage unavailable (private mode etc.) — reload anyway.
  }
  // If we're on the asar fallback (file://) the cache-busting query
  // trick would navigate to a different origin entirely; just reload.
  if (window.location.protocol === 'file:') {
    window.location.reload()
    return
  }
  // Cache-bust the URL so Chromium can't serve a stale index.html from
  // its HTTP cache. ``StaticFiles`` ignores query strings for routing
  // (the path is still ``/electron-renderer/``), but the HTTP cache is
  // keyed on the full URL — a new ``_v`` makes it a guaranteed miss,
  // fetches fresh HTML, and the new asset-hash references resolve to
  // fresh bundles. This is the deterministic equivalent of a hard
  // reload — ``location.reload(true)`` is deprecated and ignored on
  // modern Chromium.
  const url = new URL(window.location.href)
  url.searchParams.delete('_v')
  url.searchParams.set('_v', Date.now().toString())
  window.location.href = url.toString()
}

async function pollVersion(): Promise<void> {
  const current = await fetchBackendVersion()
  if (!current) return
  if (bootBackendVersion === null) {
    bootBackendVersion = current
    return
  }
  if (current !== bootBackendVersion) {
    triggerPostUpdateReload()
  }
}

// While ``phase === 'applying'`` we run a second /version poll at
// FAST_VERSION_POLL_MS cadence so the page reload fires within a couple
// of seconds of the new backend coming up. Outside that phase the
// baseline 30s poller is sufficient — we don't want to burn battery
// hitting /version every 2s indefinitely.
function startFastVersionPoll(): void {
  if (fastVersionTimer) return
  // Seed bootBackendVersion if init's first pollVersion hasn't landed
  // yet (race when the user clicks Update very early in the session).
  // Overwrite-only-if-null so we don't lose the original boot value
  // mid-session.
  if (bootBackendVersion === null) {
    void fetchBackendVersion().then((v) => {
      if (bootBackendVersion === null && v) bootBackendVersion = v
    })
  }
  fastVersionTimer = setInterval(() => { void pollVersion() }, FAST_VERSION_POLL_MS)
}

function stopFastVersionPoll(): void {
  if (fastVersionTimer) clearInterval(fastVersionTimer)
  fastVersionTimer = null
}

// ── Initialisation (idempotent) ──────────────────────────────────────────

function init(): void {
  if (initialised) return
  initialised = true

  void checkBackend()
  backendTimer = setInterval(() => { void checkBackend() }, BACKEND_POLL_MS)

  // Seed the boot version, then poll on a fixed cadence.
  void pollVersion()
  versionTimer = setInterval(() => { void pollVersion() }, VERSION_POLL_MS)

  if (window.cremind?.updater) {
    window.cremind.updater.onStatus(onUpdaterStatus)
  }
  if (window.cremind?.backendUpgrade) {
    window.cremind.backendUpgrade.onStatus(onElectronUpgradeStatus)
    window.cremind.backendUpgrade.onLog(onElectronUpgradeLog)
    window.cremind.backendUpgrade.onDone(onElectronUpgradeDone)
  }
}

function teardown(): void {
  // Only tear down if no one is left; in practice we keep the
  // listeners alive for the lifetime of the SPA so the in-app state
  // doesn't reset when the user navigates between routes.
  if (backendTimer) {
    clearInterval(backendTimer)
    backendTimer = null
  }
  if (versionTimer) {
    clearInterval(versionTimer)
    versionTimer = null
  }
  stopFastVersionPoll()
  bootBackendVersion = null
  stopStatusPolling()
  if (window.cremind?.updater) {
    window.cremind.updater.offStatus(onUpdaterStatus)
  }
  if (window.cremind?.backendUpgrade) {
    window.cremind.backendUpgrade.offStatus(onElectronUpgradeStatus)
    window.cremind.backendUpgrade.offLog(onElectronUpgradeLog)
    window.cremind.backendUpgrade.offDone(onElectronUpgradeDone)
  }
  initialised = false
}

// ── Public API ───────────────────────────────────────────────────────────

export function useUpdate() {
  init()

  // Cleanup on the LAST consumer unmount is hard to track without ref
  // counting; in practice the global UpdateBanner is mounted for the
  // entire session, so this onUnmounted is a no-op in real use. Tests
  // can invoke teardown() explicitly.
  onUnmounted(() => {
    /* deliberately no-op — see comment */
  })

  const state = computed<UnifiedUpdateState>(() => {
    const b = backend.value
    const u = updater.value

    // Compute "is there an update available?" from both inputs.
    const backendAvailable = b.status === 'available'
    const shellAvailable =
      u.status === 'available' || u.status === 'downloading' || u.status === 'ready'

    const blocked = b.status === 'too_old'

    // Display version: prefer whichever is newer/known. The pure-shell
    // case carries its version in updater.info.version; the backend
    // case in backend.latest. If both are present and differ, pick the
    // string-larger one (semver lexicographic works for our 0.X.Y
    // versions; refine later if we go past 9).
    let latestVersion: string | null = null
    if (backendAvailable && 'latest' in b) latestVersion = b.latest
    const shellVersion = u.info?.version ?? null
    if (shellVersion && (!latestVersion || shellVersion > latestVersion)) {
      latestVersion = shellVersion
    }

    const currentVersion: string | null =
      'current' in b && b.current ? b.current : null

    let computedPhase: UnifiedUpdatePhase = 'idle'
    let errMsg = error.value
    let releaseUrl: string | null = null
    if ('release_url' in b && b.release_url) releaseUrl = b.release_url

    if (phase.value === 'applying') {
      computedPhase = 'applying'
    } else if (phase.value === 'failed') {
      computedPhase = 'failed'
    } else if (phase.value === 'done') {
      computedPhase = 'done'
    } else if (phase.value === 'restart_required') {
      computedPhase = 'restart_required'
    } else if (blocked) {
      computedPhase = 'blocked'
      if (b.status === 'too_old') {
        errMsg =
          `Latest is v${b.latest} but it requires at least v${b.min_supported_upgrade_from}; ` +
          `this install is v${b.current}.`
      }
    } else if (backendAvailable || shellAvailable) {
      computedPhase = 'available'
    }

    const canApply =
      computedPhase === 'available' || computedPhase === 'failed'

    const channel: string | null =
      'channel' in b && b.channel ? b.channel : null

    return {
      phase: computedPhase,
      latestVersion,
      currentVersion,
      canApply,
      log: log.value,
      phaseLabel: phaseLabelFor(computedPhase),
      error: errMsg,
      releaseUrl,
      channel,
      availableVersions: availableVersions.value,
    }
  })

  // ── Actions ────────────────────────────────────────────────────────────

  async function checkNow(): Promise<void> {
    void checkBackend()
    if (isElectron() && window.cremind?.updater) {
      try {
        const s = await window.cremind.updater.check()
        if (s) onUpdaterStatus(s)
      } catch (e) {
        onUpdaterStatus({
          status: 'error',
          error: e instanceof Error ? e.message : 'updater error',
        })
      }
    }
  }

  /** Start the unified upgrade. Backend + shell tracks run in parallel.
   *
   * ``targetVersion`` (test channel only) pins a specific RC via the
   * Updates-page picker. When set, the shell auto-update track is skipped:
   * electron-updater can only jump to the latest RC, so letting it run
   * would override the user's pinned choice. Only the backend wheel is
   * switched to the chosen version. */
  async function applyUpdate(targetVersion?: string): Promise<void> {
    if (state.value.phase === 'applying') return
    log.value = []
    explicitRestartRequired.value = false
    setPhase('applying')
    pushLog(targetVersion ? `Installing ${targetVersion}…` : 'Starting upgrade…')
    // Start fast /version polling so we detect the new backend within a
    // couple of seconds of it coming up — independent of the status-file
    // handoff. The version change triggers a page reload via the
    // existing pollVersion() path; reconcileTerminalPhase stops the
    // fast poll when the status file (or Electron IPC) also reports
    // done/failed.
    startFastVersionPoll()

    // Shell track (Electron only): kick off the download in parallel —
    // UNLESS the user pinned a specific version, in which case the shell
    // updater (latest-only) must not run and override the choice.
    const u = updater.value
    if (!targetVersion && isElectron() && (u.status === 'available' || u.status === 'downloading')) {
      pushLog('[shell] Requesting installer download…')
      try {
        void window.cremind!.updater.download()
      } catch (e) {
        pushLog(`[shell] Download failed to start: ${String(e)}`)
      }
    }

    // Backend track: pick whichever transport is correct for the install.
    // Native Electron installs prefer IPC because it spawns local Python
    // and carries the post-upgrade backend restart itself. Docker installs
    // MUST use the web HTTP POST — there's no venv on the host; the backend
    // (inside the container) runs the upgrade and Docker's restart policy
    // brings it back up. Pure-web (non-Electron) users always use HTTP.
    const installMode = await getInstallMode()
    const useElectronIpc =
      isElectron()
      && !!window.cremind?.backendUpgrade
      && installMode !== 'docker'

    if (useElectronIpc) {
      try {
        await window.cremind!.backendUpgrade!.apply(targetVersion)
        // onDone handler reconciles the terminal phase
      } catch (e) {
        setPhase('failed', e instanceof Error ? e.message : String(e))
      }
    } else {
      await applyWebUpgrade(targetVersion)
    }
  }

  /** Trigger ``electron-updater.quitAndInstall`` from the modal's footer. */
  async function applyShellRestart(): Promise<void> {
    if (!isElectron() || !window.cremind?.updater) return
    try {
      await window.cremind.updater.install()
    } catch (e) {
      setPhase('failed', e instanceof Error ? e.message : String(e))
    }
  }

  /** Clear a 'done' / 'failed' state so the modal can close. */
  function dismiss(): void {
    if (phase.value === 'done' || phase.value === 'failed') {
      setPhase('idle')
      log.value = []
      explicitRestartRequired.value = false
    }
  }

  return {
    state,
    isElectron,
    checkNow,
    applyUpdate,
    applyShellRestart,
    dismiss,
    // Exposed for tests / debugging only:
    _backend: backend,
    _updater: updater,
    _teardown: teardown,
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────

function phaseLabelFor(p: UnifiedUpdatePhase): string {
  switch (p) {
    case 'idle': return ''
    case 'available': return 'Update available'
    case 'applying': return 'Updating Cremind…'
    case 'restart_required': return 'Restart to finish update'
    case 'done': return 'Update complete'
    case 'failed': return 'Update failed'
    case 'blocked': return 'Cannot upgrade in place'
  }
}

// ── Re-exports for callers that don't want to dip into ../types ──────────

export type {
  UnifiedUpdatePhase,
  UnifiedUpdateState,
  UpgradeStatusFile,
  UpgradeStatusPhase,
} from '../types/updates'

// Silence linter "watch unused" if we add a watch later; keep the import.
void watch
