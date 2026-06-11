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
 * 3. Same-origin: the cremind app is a single same-origin app — the SPA, API,
 *    A2A, and OAuth are all served from the page's own origin (the one public
 *    port, 1515, in native/Docker; the single-port proxy / Ingress origin in
 *    Kubernetes). Returning ``window.location.origin`` lets one URL serve the
 *    whole workflow with no port juggling.
 * 4. Empty string — only when there is no window at all (SSR/tests); the
 *    UI treats this as "not configured" and routes to the setup wizard.
 *
 * Writes go through ``setAgentUrl`` so the persisted config stays in sync
 * across renderer restarts. Under the web build this is a no-op (the URL
 * is fixed at build time); the wizard's "save URL" button is hidden in
 * that mode.
 */

export function getAgentUrl(): string {
  const fromBridge = window.cremind?.config?.agentUrl
  if (fromBridge) return fromBridge

  const fromEnv = import.meta.env.VITE_AGENT_URL as string | undefined
  if (fromEnv) return fromEnv

  // Single same-origin app: the SPA, API, A2A, and OAuth are all served from
  // the page's own origin (the one public port in native/Docker; the proxy /
  // Ingress origin in Kubernetes). No port juggling.
  if (typeof window !== 'undefined' && window.location?.origin) {
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
