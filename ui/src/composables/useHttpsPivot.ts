// Drives the end of the Setup Wizard under ``CREMIND_SSL=after-setup``:
// restart the server so it comes back on TLS, wait for the HTTPS origin to
// answer, then move the browser there carrying the freshly-minted session.
//
// Two things make this different from the Developer page's restart poll
// (``useServerRestart``):
//
//   1. We are polling a *different origin* than the page is on (http → https),
//      so the probe uses ``mode: 'no-cors'``. Any fulfilled fetch means the
//      listener is up — an opaque response carries no status to inspect, which
//      is exactly what lets us sidestep CORS entirely.
//   2. A rejected probe is ambiguous: the server may be down, or it may be up
//      with a certificate this browser does not trust (the TLS handshake fails
//      the fetch identically). So we never treat "still failing" as fatal —
//      when the budget expires we redirect anyway. If the user skipped the
//      trust step, the browser's own interstitial on the HTTPS URL is the
//      honest, actionable signal, and they were shown that URL beforehand.

import { getCurrentScope, onScopeDispose, readonly, ref } from 'vue';

import { requestServerRestart } from '../services/configApi';

export type PivotPhase =
  | 'idle'
  | 'restarting'
  | 'waiting'
  | 'redirecting'
  | 'manual'
  | 'failed';

/** Let the old process actually die before the first probe, or we'd get a
 *  fulfilled fetch off the listener that is on its way out. */
const INITIAL_DELAY_MS = 2000;
const PROBE_INTERVAL_MS = 1500;
const PROBE_BUDGET_MS = 25_000;
/** One probe's own timeout. Short: a live listener answers immediately. */
const PROBE_TIMEOUT_MS = 2000;
/** On Kubernetes, how long probes may fail before we conclude the
 *  port-forward died with the restart and say so. The pod itself is back
 *  well inside this on any healthy cluster. */
const FORWARD_HINT_AFTER_MS = 8000;

// Manual mode — the operator restarts the server by hand, so the wait is
// open-ended and the poll is gentler.
const MANUAL_INTERVAL_MS = 3000;
const MANUAL_BUDGET_MS = 10 * 60_000;

export interface PivotRunOptions {
  /** Origin of the server as this page currently reaches it (plain HTTP). */
  agentUrl: string;
  /** Admin token authorizing ``POST /api/system/restart``. */
  restartToken: string;
  /** Where the server says it will answer next. May disagree with what the
   *  browser can reach — see ``resolveTarget``. */
  nextOrigin: string | null;
  /** Profile + token handed to the HTTPS origin through the URL fragment. */
  profile: string;
  profileToken: string;
  /** INSTALL_MODE, when known. On ``kubernetes`` the browser reaches the
   *  server through ``kubectl port-forward`` — a tunnel to a specific pod
   *  that DIES when the restart replaces the container. The wait then only
   *  ends when the user re-runs the forward, so we tell them, keep watching,
   *  and never blind-redirect into an origin nothing is tunnelling to. */
  installMode?: string | null;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function useHttpsPivot() {
  const phase = ref<PivotPhase>('idle');
  const error = ref<string | null>(null);
  /** True once we believe the user's port-forward died with the restart and
   *  they need to re-run it (Kubernetes only). Drives the interstitial hint. */
  const forwardHint = ref(false);

  // Manual-mode poll handle, so ``cancelManualProbe`` (and scope teardown)
  // can stop it. The budgeted probe inside ``run`` needs no handle: it is
  // awaited inline and always terminates.
  let manualTimer: ReturnType<typeof setTimeout> | null = null;
  let manualCancelled = false;
  // The last run's destination, so the interstitial's "Continue anyway"
  // button can redirect on demand (e.g. a user who skipped the trust step
  // and whose probes therefore fail on the handshake, not the tunnel).
  let lastTarget: { target: string; profile: string; token: string } | null = null;

  /** The HTTPS origin to send the browser to.
   *
   *  ``nextOrigin`` comes from the server's own Host header, which is usually
   *  right — but under ``kubectl port-forward`` or an SSH tunnel the name the
   *  server knows and the name the browser used can differ. Only trust it when
   *  its hostname matches the one in the address bar; otherwise keep the host
   *  the browser is demonstrably able to reach and swap the scheme.
   */
  function resolveTarget(nextOrigin: string | null): string {
    const fallback = `https://${window.location.host}`;
    if (!nextOrigin) return fallback;
    try {
      const parsed = new URL(nextOrigin);
      if (parsed.hostname === window.location.hostname) {
        return nextOrigin.replace(/\/+$/, '');
      }
    } catch {
      // Unparseable — fall through.
    }
    return fallback;
  }

  function handoffUrl(target: string, profile: string, token: string): string {
    // The SPA uses hash history, so the fragment is the route AND never
    // reaches the server — the token rides in the part of the URL that stays
    // in the browser. ``ts`` bounds replay if the URL is later bookmarked or
    // leaked; the receiving guard rejects a stale one.
    const q = `token=${encodeURIComponent(token)}`
      + `&profile=${encodeURIComponent(profile)}`
      + `&ts=${Date.now()}`;
    return `${target}/#/setup-handoff?${q}`;
  }

  /** One probe. Resolves true if anything at all answered. */
  async function probeOnce(target: string): Promise<boolean> {
    try {
      await fetch(`${target}/health`, {
        mode: 'no-cors',
        cache: 'no-store',
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      });
      // Opaque responses expose no status; a fulfilled fetch is the signal.
      return true;
    } catch {
      // Down, or up-but-untrusted. Indistinguishable here — keep polling.
      return false;
    }
  }

  function goTo(target: string, profile: string, token: string): void {
    phase.value = 'redirecting';
    window.location.replace(handoffUrl(target, profile, token));
  }

  async function run(options: PivotRunOptions): Promise<void> {
    const target = resolveTarget(options.nextOrigin);
    lastTarget = { target, profile: options.profile, token: options.profileToken };
    error.value = null;
    forwardHint.value = false;
    phase.value = 'restarting';
    try {
      await requestServerRestart(options.agentUrl, options.restartToken);
    } catch (e) {
      phase.value = 'failed';
      error.value = e instanceof Error ? e.message : String(e);
      return;
    }

    // On Kubernetes the restart takes the user's ``kubectl port-forward``
    // down with it — the tunnel targets one pod instance and does not
    // reconnect. From here every probe fails with no distinction between
    // "pod still booting", "tunnel dead" and "up but CA untrusted", so the
    // policy differs by deployment: elsewhere, a spent budget redirects
    // anyway (the browser's own interstitial is the honest signal); on
    // Kubernetes the origin is unreachable until the user re-runs the
    // forward, so redirecting lands on a bare connection error with no
    // instructions. Instead we say what to do, keep watching so the moment
    // the forward is back we pivot, and leave "Continue anyway" for the
    // user who skipped the trust step (their tunnel is fine; only the
    // handshake fails).
    const forwardDies = (options.installMode ?? '').toLowerCase() === 'kubernetes';
    phase.value = 'waiting';
    await sleep(INITIAL_DELAY_MS);
    const started = Date.now();
    const deadline = started + (forwardDies ? MANUAL_BUDGET_MS : PROBE_BUDGET_MS);
    while (Date.now() < deadline) {
      if (phase.value !== 'waiting') return; // redirectNow() won the race
      if (await probeOnce(target)) {
        goTo(target, options.profile, options.profileToken);
        return;
      }
      if (forwardDies && Date.now() - started > FORWARD_HINT_AFTER_MS) {
        forwardHint.value = true;
      }
      await sleep(PROBE_INTERVAL_MS);
    }
    goTo(target, options.profile, options.profileToken);
  }

  /** Redirect immediately, budget or not — the interstitial's escape hatch
   *  for the user who knows why the probe is failing (untrusted CA). */
  function redirectNow(): void {
    if (lastTarget) {
      goTo(lastTarget.target, lastTarget.profile, lastTarget.token);
    }
  }

  /** No supervisor: the operator restarts the server themselves. We just
   *  watch the HTTPS origin and pivot the moment it answers. */
  function enterManualMode(options: PivotRunOptions): void {
    const target = resolveTarget(options.nextOrigin);
    lastTarget = { target, profile: options.profile, token: options.profileToken };
    error.value = null;
    phase.value = 'manual';
    manualCancelled = false;
    const deadline = Date.now() + MANUAL_BUDGET_MS;

    const tick = async () => {
      if (manualCancelled) return;
      if (await probeOnce(target)) {
        if (manualCancelled) return;
        goTo(target, options.profile, options.profileToken);
        return;
      }
      if (manualCancelled || Date.now() >= deadline) return;
      manualTimer = setTimeout(() => { void tick(); }, MANUAL_INTERVAL_MS);
    };

    manualTimer = setTimeout(() => { void tick(); }, MANUAL_INTERVAL_MS);
  }

  /** Stop the manual poll — the user chose to stay on HTTP for now. */
  function cancelManualProbe(): void {
    manualCancelled = true;
    if (manualTimer !== null) {
      clearTimeout(manualTimer);
      manualTimer = null;
    }
  }

  // Never leave a timer running against a torn-down component (the wizard
  // unmounts the moment "Continue on HTTP for now" routes away).
  if (getCurrentScope()) {
    onScopeDispose(cancelManualProbe);
  }

  return {
    phase: readonly(phase),
    error: readonly(error),
    forwardHint: readonly(forwardHint),
    run,
    redirectNow,
    enterManualMode,
    cancelManualProbe,
  };
}
