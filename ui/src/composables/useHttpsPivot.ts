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
//      off Kubernetes, when the budget expires we redirect anyway. If the user
//      skipped the trust step, the browser's own interstitial on the HTTPS URL
//      is the honest, actionable signal, and they were shown that URL
//      beforehand. On Kubernetes we never blind-redirect: see ``run``.
//
// A third thing shapes the timing. Under ``kubectl port-forward``, a dial that
// reaches the pod and finds nothing listening is REFUSED, and kubectl >= 1.23
// answers a refused dial by tearing down the whole tunnel. So every probe we
// fire into the restart gap risks killing the very tunnel we are waiting on —
// which is why the first Kubernetes probe waits for the server to plausibly be
// back rather than starting at ``INITIAL_DELAY_MS``. (The chart's relay sidecar
// makes the dial land on something that is always listening, so this is
// belt-and-braces there — but it still matters for ``proxy.enabled=false`` and
// for installs on an older chart.)

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
/** Kubernetes waits far longer before its FIRST probe. The restart is not one
 *  event but a chain: the server begins its own graceful shutdown ~1.5s after
 *  the 202, drains and cleans up (exiting within ~13.5s where a supervisor
 *  exists; the detached watchdog force-stops it only at +25s if that wedges),
 *  the container exits, the kubelet restarts it, the entrypoint runs
 *  ``cremind db upgrade``, and only then does the app boot — migrations,
 *  skills, documents, channels — before it binds. Nothing can answer inside
 *  ~15s, so an earlier probe cannot succeed; it can only cost us the tunnel
 *  (a refused in-pod dial ends a ``kubectl port-forward``). */
const K8S_INITIAL_DELAY_MS = 15_000;
const PROBE_INTERVAL_MS = 1500;
/** Enough for the whole chain above on a supervised native install: a
 *  graceful exit (up to ~13.5s), the respawn loop's 2s, and the new process's
 *  boot. The old 25s could brush that, and off Kubernetes a spent budget
 *  blind-redirects — into an origin nothing is listening on yet. */
const PROBE_BUDGET_MS = 40_000;
/** One probe's own timeout. Short: a live listener answers immediately. */
const PROBE_TIMEOUT_MS = 2000;
/** On Kubernetes, how long probes may fail (after the initial delay above)
 *  before we surface the "something needs your attention" hint. A healthy flip
 *  finishes well inside this, so the hint means one of the two things the
 *  probe genuinely cannot distinguish — a dead tunnel, or an untrusted CA —
 *  rather than "still booting". Firing it earlier made it appear during every
 *  normal restart, telling users to fix something that was not broken. */
const FORWARD_HINT_AFTER_MS = 45_000;

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
   *  server through ``kubectl port-forward``, a tunnel that a refused in-pod
   *  dial can kill outright. The chart's relay sidecar keeps it alive across
   *  the restart, but an install with ``proxy.enabled=false`` (or an older
   *  chart) still loses it and only the user can bring it back — so on
   *  Kubernetes we probe late, wait indefinitely, and never blind-redirect
   *  into an origin nothing may be tunnelling to. */
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
  // can stop it. ``cancelled`` stops the loop inside ``run`` for the same
  // reasons: on Kubernetes that loop no longer has a deadline, so leaving the
  // page has to end it explicitly or it would outlive the component.
  let manualTimer: ReturnType<typeof setTimeout> | null = null;
  let cancelled = false;
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
    // "Retry restart" reaches here after a cancel, so clear the flag or the
    // loop below would abort before its first probe.
    cancelled = false;
    phase.value = 'restarting';
    try {
      await requestServerRestart(options.agentUrl, options.restartToken);
    } catch (e) {
      phase.value = 'failed';
      error.value = e instanceof Error ? e.message : String(e);
      return;
    }

    // On Kubernetes the browser reaches the server through a tunnel that the
    // restart can take down with it, and from here every failed probe means
    // one of three indistinguishable things: "pod still booting", "tunnel
    // dead", or "up but CA untrusted". That shapes two decisions.
    //
    // WAITING: we hold off the first probe (K8S_INITIAL_DELAY_MS) because
    // nothing can answer that early anyway, and a dial into the gap is what
    // kills a tunnel in the first place.
    //
    // GIVING UP: we don't. Off Kubernetes a spent budget redirects anyway —
    // the browser's own interstitial is then the honest signal. Here the
    // origin may be unreachable rather than untrusted, and redirecting into a
    // tunnel nothing is listening on lands the user on a bare connection
    // error with no instructions and no way back. So we keep watching
    // indefinitely, surface the hint, and leave "Continue anyway" as the
    // deliberate escape hatch for the user who knows their tunnel is fine and
    // only the handshake is failing.
    const forwardDies = (options.installMode ?? '').toLowerCase() === 'kubernetes';
    phase.value = 'waiting';
    await sleep(forwardDies ? K8S_INITIAL_DELAY_MS : INITIAL_DELAY_MS);
    const started = Date.now();
    const deadline = started + PROBE_BUDGET_MS;
    while (!cancelled && (forwardDies || Date.now() < deadline)) {
      if (phase.value !== 'waiting') return; // redirectNow() won the race
      if (await probeOnce(target)) {
        if (cancelled) return;
        goTo(target, options.profile, options.profileToken);
        return;
      }
      if (forwardDies && Date.now() - started > FORWARD_HINT_AFTER_MS) {
        forwardHint.value = true;
      }
      // Once the hint is up we are no longer racing a boot — poll gently so an
      // unattended tab doesn't spin at 1.5s forever.
      await sleep(forwardHint.value ? MANUAL_INTERVAL_MS : PROBE_INTERVAL_MS);
    }
    // Only reachable off Kubernetes (spent budget) or after a cancel — and a
    // cancel means the user already left for the HTTP origin.
    if (cancelled) return;
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
    cancelled = false;
    const deadline = Date.now() + MANUAL_BUDGET_MS;

    const tick = async () => {
      if (cancelled) return;
      if (await probeOnce(target)) {
        if (cancelled) return;
        goTo(target, options.profile, options.profileToken);
        return;
      }
      if (cancelled || Date.now() >= deadline) return;
      manualTimer = setTimeout(() => { void tick(); }, MANUAL_INTERVAL_MS);
    };

    manualTimer = setTimeout(() => { void tick(); }, MANUAL_INTERVAL_MS);
  }

  /** Stop watching — the user chose to stay on HTTP for now. Ends the manual
   *  poll AND the ``run`` loop, which on Kubernetes has no deadline of its
   *  own. */
  function cancelManualProbe(): void {
    cancelled = true;
    if (manualTimer !== null) {
      clearTimeout(manualTimer);
      manualTimer = null;
    }
  }

  // Never leave a poll running against a torn-down component (the wizard
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
