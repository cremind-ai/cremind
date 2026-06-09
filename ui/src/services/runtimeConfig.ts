/**
 * Resolves the Cremind runtime config — single source of truth for callers
 * who need to know where the backend is, regardless of how the app was
 * launched.
 *
 * Precedence (highest first):
 *
 * 1. ``window.cremind.config`` — set by electron/preload.ts from the
 *    JSON file the installer writes. Authoritative under Electron.
 * 2. ``import.meta.env.VITE_AGENT_URL`` — build-time default for the
 *    standalone web build and the Vite dev server, where there's no
 *    Electron bridge.
 * 3. Port-swap heuristic for the raw Docker bundle: when the SPA is served
 *    on port 1515 by the cremind container, the backend is at the same
 *    host on port 1112. This makes the bundle work both when the user
 *    accesses it locally and when they open it on a remote server, with
 *    no per-deploy build flag.
 * 4. Same-origin fallback: when the page is served from any other origin —
 *    e.g. behind the Kubernetes single-port reverse proxy or an Ingress,
 *    where a path-router sends ``/api`` and ``/health`` to the backend and
 *    everything else to the SPA — the backend shares the page's origin.
 *    Returning ``window.location.origin`` lets one URL serve the whole
 *    workflow with no port juggling.
 * 5. Empty string — only when there is no window at all (SSR/tests); the
 *    UI treats this as "not configured" and routes to the setup wizard.
 *
 * Writes go through ``setAgentUrl`` so the persisted config stays in sync
 * across renderer restarts. Under the web build this is a no-op (the URL
 * is fixed at build time); the wizard's "save URL" button is hidden in
 * that mode.
 */

const SPA_PORT = '1515'
const API_PORT = '1112'

export function getAgentUrl(): string {
  const fromBridge = window.cremind?.config?.agentUrl
  if (fromBridge) return fromBridge

  const fromEnv = import.meta.env.VITE_AGENT_URL as string | undefined
  if (fromEnv) return fromEnv

  if (typeof window !== 'undefined' && window.location?.hostname) {
    // Raw Docker bundle: SPA on <host>:1515, API on <host>:1112. Swap the
    // port to derive the API URL.
    if (window.location.port === SPA_PORT) {
      const protocol = window.location.protocol || 'http:'
      return `${protocol}//${window.location.hostname}:${API_PORT}`
    }
    // Any other origin (Kubernetes single-port proxy / Ingress, or a custom
    // reverse proxy): the backend is same-origin. A path-router forwards
    // /api and /health to the backend; everything else is the SPA.
    return window.location.origin
  }

  return ''
}

export async function setAgentUrl(url: string): Promise<void> {
  if (window.cremind) {
    await window.cremind.setConfig({ agentUrl: url })
    // ``window.cremind.config`` is the snapshot the preload script
    // captured synchronously at module init; the IPC above persists to
    // disk and updates the *main* process's cache, but never refreshes
    // the renderer's copy. Without this mirror, ``getAgentUrl()`` (and
    // anything routed through it — ``a2aClient.getBaseUrl``,
    // ``getApiOrigin``) keeps returning the stale value for the rest
    // of the session, producing ``Invalid base URL`` from the A2A SDK
    // the next time it tries to build a request.
    //
    // Wrapped in try/catch because Electron 30's ``contextBridge``
    // freezes the exposed config object — the assignment throws
    // ``TypeError: Cannot assign to read only property 'agentUrl'``,
    // which used to abort the wizard's pivot click handler before it
    // could call ``window.location.replace`` (the user had to click
    // ``Continue to Setup Wizard`` twice). Swallowing is safe: the
    // IPC above already persisted the value to disk, so the next
    // renderer process (post-pivot, when the SPA loads from
    // http://127.0.0.1:1515) reads it fresh via preload's
    // sendSync. The Pinia store ref (set by setAgentUrlAction one
    // frame earlier) is also already populated, so any in-session
    // caller routed through ``useSettingsStore()`` sees the new
    // value too. The only thing that stays stale is the direct
    // ``getAgentUrl()`` read — which ``a2aClient.getBaseUrl()``
    // already short-circuits past via its ``fromStore`` check.
    if (window.cremind.config) {
      try {
        window.cremind.config.agentUrl = url
      } catch {
        /* see comment above */
      }
    }
  }
  // Web build has no persistent runtime store. The renderer keeps the
  // value in its own state for the session; on next reload it falls back
  // to VITE_AGENT_URL. That's the documented behavior for the web build —
  // operators set the URL at deploy time.
}

export function getDeploymentType(): 'local' | 'server' | 'custom' | '' {
  return window.cremind?.config?.deploymentType ?? ''
}
