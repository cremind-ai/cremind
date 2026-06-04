// Composable behind the Developer page's Restart Server card.
//
// Three responsibilities:
//   1. Resolve install_mode (docker / electron / native / null) from
//      ``/api/services/tray-capabilities`` so the dialog copy and the
//      restart channel adapt per environment.
//   2. Trigger the restart through the right channel:
//        - electron  → window.cremind.server.restart() (IPC)
//        - anything else → POST /api/system/restart (HTTP)
//   3. Poll ``/health`` until the backend answers again, with
//      exponential backoff and a hard timeout. Surface the lifecycle
//      as a small state machine the page can render.

import { computed, readonly, ref } from 'vue'

import { useSettingsStore } from '../stores/settings'

export type RestartPhase = 'idle' | 'restarting' | 'reconnected' | 'failed'

// Reported by GET /api/services/tray-capabilities. ``null`` covers both
// "endpoint unreachable" and "field missing" — we treat both the same.
export type InstallMode = 'docker' | 'electron' | 'native' | null

const HEALTH_POLL_INITIAL_MS = 500
const HEALTH_POLL_MAX_MS = 3000
const HEALTH_POLL_BUDGET_MS = 60_000
// Time the "Reconnected" badge stays visible before fading back to Idle.
const RECONNECTED_DISPLAY_MS = 3000

function isElectronRuntime(): boolean {
  return typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__
}

export function useServerRestart() {
  const phase = ref<RestartPhase>('idle')
  const error = ref<string | null>(null)
  const installMode = ref<InstallMode>(null)
  const installModeLoaded = ref(false)

  const isBusy = computed(() => phase.value === 'restarting')

  /** Fetch ``install_mode`` once and cache it on the composable. */
  async function loadInstallMode(): Promise<void> {
    if (installModeLoaded.value) return
    const settings = useSettingsStore()
    if (!settings.agentUrl) {
      installModeLoaded.value = true
      return
    }
    try {
      const r = await fetch(`${settings.agentUrl}/api/services/tray-capabilities`)
      if (r.ok) {
        const body = (await r.json()) as { install_mode?: InstallMode }
        installMode.value = body.install_mode ?? null
      }
    } catch {
      // Leave installMode at null — the dialog will show the
      // most-conservative "no supervisor" warning copy.
    } finally {
      installModeLoaded.value = true
    }
  }

  async function triggerHttpRestart(): Promise<void> {
    const settings = useSettingsStore()
    if (!settings.agentUrl) throw new Error('No backend URL configured.')
    const headers: Record<string, string> = {}
    if (settings.authToken) {
      headers['Authorization'] = `Bearer ${settings.authToken}`
    }
    const r = await fetch(`${settings.agentUrl}/api/system/restart`, {
      method: 'POST', headers,
    })
    // 202 is the happy path; 401/403 surface as errors below.
    if (!r.ok && r.status !== 202) {
      const body = await r.json().catch(() => ({}))
      const msg = (body as { error?: string }).error ?? `HTTP ${r.status}`
      throw new Error(msg)
    }
  }

  async function triggerElectronRestart(): Promise<void> {
    if (!window.cremind?.server?.restart) {
      // Bridge missing (e.g., dev-server-on-Electron mismatch) —
      // fall back to HTTP so we still do something useful.
      await triggerHttpRestart()
      return
    }
    const result = await window.cremind.server.restart()
    if (!result.ok) {
      throw new Error(result.error ?? 'Electron failed to restart the backend.')
    }
  }

  /** Poll ``/health`` with exponential backoff until a 200 lands or
   *  the budget runs out. Returns true on success. */
  async function pollHealth(): Promise<boolean> {
    const settings = useSettingsStore()
    if (!settings.agentUrl) return false
    const deadline = Date.now() + HEALTH_POLL_BUDGET_MS
    let delay = HEALTH_POLL_INITIAL_MS
    while (Date.now() < deadline) {
      await new Promise<void>((resolve) => setTimeout(resolve, delay))
      try {
        // No-cache + short timeout: the backend should answer fast
        // once it's back, and stale cached 200s would be misleading.
        const controller = new AbortController()
        const timer = setTimeout(() => controller.abort(), 2000)
        const r = await fetch(`${settings.agentUrl}/health`, {
          method: 'GET',
          cache: 'no-store',
          signal: controller.signal,
        })
        clearTimeout(timer)
        // 503 means "alive but degraded" — that still counts as
        // "the listener came back". Anything 2xx/5xx works; only
        // network failures should keep us polling.
        if (r.status > 0) return true
      } catch {
        // Network failure — backend is still down or restarting.
      }
      delay = Math.min(delay * 1.5, HEALTH_POLL_MAX_MS)
    }
    return false
  }

  /** Kick off the restart. Called by the Developer page after the
   *  user confirms the dialog. Safe to call multiple times — guarded
   *  by ``isBusy``. */
  async function restart(): Promise<void> {
    if (isBusy.value) return
    phase.value = 'restarting'
    error.value = null
    try {
      if (isElectronRuntime() && installMode.value === 'electron') {
        await triggerElectronRestart()
      } else {
        await triggerHttpRestart()
      }
    } catch (e) {
      phase.value = 'failed'
      error.value = e instanceof Error ? e.message : String(e)
      return
    }
    // The kill landed (or the IPC respawn returned). Now wait for the
    // listener to come back. Under Docker / Electron this finishes in
    // ~5–15s; under bare pip it never will, and we give up at the
    // budget.
    const ok = await pollHealth()
    if (!ok) {
      phase.value = 'failed'
      error.value =
        'Backend did not come back within the timeout. ' +
        'Check the supervisor (or relaunch manually if there isn\'t one).'
      return
    }
    phase.value = 'reconnected'
    setTimeout(() => {
      if (phase.value === 'reconnected') {
        phase.value = 'idle'
        error.value = null
      }
    }, RECONNECTED_DISPLAY_MS)
  }

  return {
    phase: readonly(phase),
    error: readonly(error),
    installMode: readonly(installMode),
    isBusy,
    loadInstallMode,
    restart,
  }
}
