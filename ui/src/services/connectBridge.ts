// Cremind Connect on this computer, as the Settings → Tags page sees it.
//
// - The desktop app may expose `window.cremind.connect` (status / install /
//   launch) and `window.cremind.background` (keep Cremind running after the
//   window closes). Both are optional and feature-detected: the SPA can be
//   newer than the desktop shell that loads it, and a browser has neither.
// - A setup link (`cremind-connect://setup?…`) is opened over IPC in the
//   desktop app, else by clicking a hidden anchor inside the person's click:
//   a custom scheme is handed to the operating system and the page stays
//   where it is. (`openExternal` is no fallback: its allowlist refuses custom
//   schemes on purpose.)
// - What the last "Check this computer" found is remembered per profile in
//   localStorage — a convenience only, so every access is guarded.

export type ConnectBridge = NonNullable<NonNullable<Window['cremind']>['connect']>;
export type BackgroundBridge = NonNullable<NonNullable<Window['cremind']>['background']>;

function cremind(): Window['cremind'] | undefined {
  return typeof window === 'undefined' ? undefined : window.cremind;
}

/** The desktop app's Connect bridge, when this desktop shell has one. */
export function connectBridge(): ConnectBridge | null {
  const c = cremind()?.connect;
  return c && typeof c.status === 'function' && typeof c.install === 'function' && typeof c.launch === 'function'
    ? c : null;
}

export function backgroundBridge(): BackgroundBridge | null {
  const b = cremind()?.background;
  return b && typeof b.status === 'function' && typeof b.enable === 'function' ? b : null;
}

function reason(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * Open a Cremind Connect setup link. In a browser this must run inside the
 * click that asked for it (browsers only hand a custom scheme to the OS from
 * a user gesture), so callers do as little as possible before it.
 */
export async function launchConnect(url: string): Promise<{ ok: boolean; error?: string }> {
  const bridge = connectBridge();
  if (bridge) {
    try {
      return await bridge.launch(url);
    } catch (e) {
      return { ok: false, error: reason(e) };
    }
  }
  try {
    const a = document.createElement('a');
    a.href = url;
    a.rel = 'noopener noreferrer';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    a.remove();
    return { ok: true };
  } catch (e) {
    return { ok: false, error: reason(e) };
  }
}

/** Is the backend on this computer? (Only then can the desktop app keep it running.) */
export function isLocalBackend(agentUrl: string): boolean {
  if (cremind()?.config?.deploymentType === 'local') return true;
  try {
    const base = agentUrl.startsWith('http') ? agentUrl : window.location.origin;
    const host = new URL(base).hostname.replace(/^\[|\]$/g, '');
    return host === 'localhost' || host === '::1' || host.startsWith('127.');
  } catch {
    return false;
  }
}

// ── what the last check of this computer found ─────────────────────────────

export interface ConnectCheckMemory {
  /** Cremind Connect answered on this computer. */
  found: boolean;
  computer?: string;
  version?: string;
  installationId?: string;
  /** Epoch ms. */
  at: number;
}

const memoryKey = (profile: string) => `cremind:tags:connect-check:${profile}`;

export function recallConnectCheck(profile: string): ConnectCheckMemory | null {
  try {
    const raw = localStorage.getItem(memoryKey(profile));
    if (!raw) return null;
    const v = JSON.parse(raw);
    if (!v || typeof v !== 'object' || typeof v.found !== 'boolean' || typeof v.at !== 'number') return null;
    const text = (x: unknown) => (typeof x === 'string' && x ? x : undefined);
    return { found: v.found, at: v.at, computer: text(v.computer), version: text(v.version), installationId: text(v.installationId) };
  } catch {
    return null;
  }
}

export function rememberConnectCheck(profile: string, value: ConnectCheckMemory): void {
  try {
    localStorage.setItem(memoryKey(profile), JSON.stringify(value));
  } catch {
    /* private mode or full storage: the check simply is not remembered */
  }
}
