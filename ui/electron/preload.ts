import { ipcRenderer, contextBridge } from 'electron'
import { filterTransitionState, transitionLocalKeys, transitionProfile, type TransitionState } from './transitionState'

let beforeMigration: (() => Promise<string | void>) | null = null
let migrationReleased: (() => void) | null = null

// Restore the one-window handoff before Vue imports any store. Main verifies
// WebContents + destination origin and consumes this memory-only value once.
try {
  const transferred = ipcRenderer.sendSync('cremind:https:consume-sync') as TransitionState | null
  if (transferred) {
    const state = filterTransitionState(window.location.href, transferred)
    for (const [key, value] of Object.entries(state.local)) {
      if (key === 'logged_in_profiles') {
        let previous: string[] = []
        try {
          const parsed = JSON.parse(localStorage.getItem(key) || '[]')
          if (Array.isArray(parsed)) previous = parsed.filter((entry) => typeof entry === 'string')
        } catch { /* damaged or unavailable old storage */ }
        localStorage.setItem(key, JSON.stringify([...new Set([...previous, ...JSON.parse(value)])]))
      } else localStorage.setItem(key, value)
    }
    for (const [key, value] of Object.entries(state.session)) sessionStorage.setItem(key, value)
  }
} catch { /* blocked storage should still allow navigation and sign-in */ }

ipcRenderer.on('cremind:https:capture', async () => {
  ipcRenderer.send('cremind:https:capture-ack')
  let windowProfile: string | void
  try { windowProfile = await beforeMigration?.() } catch {
    ipcRenderer.send('cremind:https:capture-failed')
    return
  }
  const state: TransitionState = { local: {}, session: {} }
  try {
    // A just-finished Setup Wizard has no profile in its route yet.
    const activeProfile = typeof windowProfile === 'string'
      ? windowProfile : localStorage.getItem('profile_id') || undefined
    if (activeProfile) state.local.profile_id = activeProfile
    for (const key of transitionLocalKeys(window.location.href, activeProfile)) {
      const value = localStorage.getItem(key)
      if (value !== null) state.local[key] = value
    }
    const grace = sessionStorage.getItem('cremind:just_updated')
    if (grace) state.session['cremind:just_updated'] = grace
    const profile = transitionProfile(window.location.href, activeProfile)
    if (profile) {
      for (let i = 0; i < sessionStorage.length; i += 1) {
        const key = sessionStorage.key(i)
        if (key?.startsWith(`cremind:draft:${profile}:`)) state.session[key] = sessionStorage.getItem(key) || ''
      }
    }
  } catch { /* a storage-restricted window migrates without a session */ }
  try { ipcRenderer.send('cremind:https:captured', filterTransitionState(window.location.href, state)) }
  catch { ipcRenderer.send('cremind:https:capture-failed') }
})

ipcRenderer.on('cremind:https:released', () => migrationReleased?.())

// Snapshot the runtime config synchronously so the renderer can read the
// agent URL during module init (before any async IPC could resolve).
// `sendSync` is normally avoided, but here it's a one-shot call at preload
// time and it's the cleanest way to keep `window.cremind.config` populated
// from the very first line of renderer code.
let initialConfig: any = {}
try {
  initialConfig = ipcRenderer.sendSync('cremind:get-config-sync') ?? {}
} catch {
  initialConfig = {}
}

// Per-IPC log/done listener bookkeeping. We keep a parallel WeakMap so
// callers can pass plain functions to ``onLog`` / ``onDone`` and we wrap
// them with the (event, payload) → payload signature electron expects.
const installerListeners = new WeakMap<Function, (event: any, payload: any) => void>()

// Expose the Cremind-specific bridge: a synchronous snapshot for module
// init, plus async getters/setters for live updates from the wizard.
contextBridge.exposeInMainWorld('cremind', {
  config: initialConfig,
  getConfig: () => ipcRenderer.invoke('cremind:get-config'),
  setConfig: (patch: Record<string, unknown>) =>
    ipcRenderer.invoke('cremind:set-config', patch),

  // Open a URL in the OS default handler (browser / mail client) instead of a
  // new Electron window. The renderer's external-link interceptor calls this
  // for clicked external anchors; the main process enforces a scheme allowlist.
  openExternal: (url: string) => ipcRenderer.invoke('cremind:open-external', url),

  // First-run installer bridge — fronts the IPC handlers in
  // electron/main.ts. ``run`` returns a promise that resolves with the
  // installer's exit code; while it's pending, ``onLog`` callbacks
  // receive every line the script wrote to stdout/stderr.
  installer: {
    detect: () => ipcRenderer.invoke('cremind:installer:detect'),
    listVersions: () => ipcRenderer.invoke('cremind:installer:list-versions'),
    run: (payload: Record<string, unknown>) =>
      ipcRenderer.invoke('cremind:installer:run', payload),
    cancel: () => ipcRenderer.invoke('cremind:installer:cancel'),
    onLog(callback: (entry: { stream: string; line: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('cremind:installer:log', wrapper)
    },
    offLog(callback: (entry: { stream: string; line: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('cremind:installer:log', wrapper)
        installerListeners.delete(callback)
      }
    },
    onDone(callback: (result: { exitCode: number; error?: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('cremind:installer:done', wrapper)
    },
    offDone(callback: (result: { exitCode: number; error?: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('cremind:installer:done', wrapper)
        installerListeners.delete(callback)
      }
    },
    // Uninstall flow — keeps the same subscribe/unsubscribe shape as the
    // install side. ``mode`` is 'keep' (preserve .env/storage/tokens) or
    // 'purge' (wipe the System Dir; Docker volumes go too).
    uninstall: (mode: 'keep' | 'purge') =>
      ipcRenderer.invoke('cremind:installer:uninstall', mode),
    onUninstallLog(callback: (entry: { stream: string; line: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('cremind:installer:uninstall:log', wrapper)
    },
    offUninstallLog(callback: (entry: { stream: string; line: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('cremind:installer:uninstall:log', wrapper)
        installerListeners.delete(callback)
      }
    },
    onUninstallDone(callback: (result: { exitCode: number; mode?: 'keep' | 'purge'; error?: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('cremind:installer:uninstall:done', wrapper)
    },
    offUninstallDone(callback: (result: { exitCode: number; mode?: 'keep' | 'purge'; error?: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('cremind:installer:uninstall:done', wrapper)
        installerListeners.delete(callback)
      }
    },
  },

  // Backend lifecycle bridge. The renderer calls ``server.start()`` on
  // the Continue-to-Setup-Wizard click; the main process spawns
  // ``cremind serve`` and resolves once the health endpoint is reachable
  // (or rejects with an error string).
  server: {
    prepareHttpsMigration: (options: { nextOrigin: string; transitionId: string; instanceId: string }): Promise<{ ok: boolean; error?: string }> =>
      ipcRenderer.invoke('cremind:server:prepare-https', options),
    onBeforeMigration: (callback: () => Promise<string | void>) => {
      beforeMigration = callback
      return () => { if (beforeMigration === callback) beforeMigration = null }
    },
    onMigrationReleased: (callback: () => void) => {
      migrationReleased = callback
      return () => { if (migrationReleased === callback) migrationReleased = null }
    },
    releaseHttpsMigration: (): Promise<{ ok: boolean; error?: string }> =>
      ipcRenderer.invoke('cremind:server:release-https'),
    start: (): Promise<{ ok: boolean; error?: string; agentUrl?: string }> =>
      ipcRenderer.invoke('cremind:server:start'),
    // Kills + respawns the backend child. Used by the Developer
    // page's Restart Server button under Electron; web/Docker builds
    // hit POST /api/system/restart directly instead.
    restart: (options?: { nextOrigin?: string; transitionId?: string; instanceId?: string }): Promise<{ ok: boolean; error?: string; agentUrl?: string }> =>
      ipcRenderer.invoke('cremind:server:restart', options),
    // Main verifies HTTPS and migrates every Cremind window with a private,
    // one-use handoff. OAuth, VNC and document-preview windows are excluded.
    migrateHttps: (options: { nextOrigin: string; transitionId?: string; instanceId?: string }): Promise<{ ok: boolean; error?: string; agentUrl?: string }> =>
      ipcRenderer.invoke('cremind:server:migrate-https', options),
  },

  // Backend upgrade bridge — runs ``cremind upgrade --yes`` as a child
  // process under main, streaming progress back so the renderer can
  // show a live log modal without sending the user to a terminal.
  // ``apply`` resolves when the child has exited AND the backend has
  // been restarted (on success). ``onStatus`` / ``onLog`` / ``onDone``
  // mirror the installer bridge's shape so consumers can reuse the
  // same subscribe/unsubscribe pattern.
  backendUpgrade: {
    apply: (targetVersion?: string) =>
      ipcRenderer.invoke('cremind:backend-upgrade:apply', { targetVersion }),
    onStatus(callback: (payload: { phase: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('cremind:backend-upgrade:status', wrapper)
    },
    offStatus(callback: (payload: { phase: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('cremind:backend-upgrade:status', wrapper)
        installerListeners.delete(callback)
      }
    },
    onLog(callback: (entry: { stream: string; line: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('cremind:backend-upgrade:log', wrapper)
    },
    offLog(callback: (entry: { stream: string; line: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('cremind:backend-upgrade:log', wrapper)
        installerListeners.delete(callback)
      }
    },
    onDone(callback: (result: { exitCode: number; ok: boolean; error?: string }) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('cremind:backend-upgrade:done', wrapper)
    },
    offDone(callback: (result: { exitCode: number; ok: boolean; error?: string }) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('cremind:backend-upgrade:done', wrapper)
        installerListeners.delete(callback)
      }
    },
  },

  // Auto-updater bridge — fronts ``electron-updater`` running in the
  // main process. The renderer calls ``check``/``download``/``install``
  // explicitly; the user is always in control of when the new build
  // gets staged. ``onStatus`` receives the lifecycle events so the
  // banner can show "downloading 47%" without polling.
  updater: {
    check: () => ipcRenderer.invoke('cremind:updater:check'),
    download: () => ipcRenderer.invoke('cremind:updater:download'),
    install: () => ipcRenderer.invoke('cremind:updater:install'),
    onStatus(callback: (payload: any) => void) {
      const wrapper = (_event: any, payload: any) => callback(payload)
      installerListeners.set(callback, wrapper)
      ipcRenderer.on('cremind:updater:status', wrapper)
    },
    offStatus(callback: (payload: any) => void) {
      const wrapper = installerListeners.get(callback)
      if (wrapper) {
        ipcRenderer.off('cremind:updater:status', wrapper)
        installerListeners.delete(callback)
      }
    },
  },
})
