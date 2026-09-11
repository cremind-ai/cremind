// HTTPS transition used by both first-run setup and Settings → Security.
// Sessions cross the origin through short-lived, one-use server tickets;
// readiness is accepted only from the expected Cremind instance/transition,
// judged by the same rule every background tab uses (services/httpsReadiness).
//
// Every run owns a generation and an AbortController. Cancelling, disposing,
// or starting another run bumps the generation and aborts in-flight probes, and
// every await is followed by an `alive()` check before the phase changes or
// the page navigates — so a stale loop can never move the tab after the user
// cancelled, and two loops can never race each other to a redirect.

import { computed, getCurrentScope, onScopeDispose, readonly, ref } from 'vue';

import {
  activateHttps,
  fetchTlsStatus,
  isTransientTlsFailure,
  requestServerRestart,
  TlsApiError,
  type TlsRuntimeStatus,
  type TlsTransition,
} from '../services/configApi';
import { probeHttpsReadiness, type HttpsReadiness } from '../services/httpsReadiness';
import { getCachedTlsHandoff, localTransition, primeTlsHandoff } from '../services/httpsTransition';
import {
  releaseBrowserMigration,
  waitForBrowserMigrationReady,
  waitForMigrationReady,
} from '../services/migrationReadiness';

export type PivotPhase =
  | 'idle'
  /** Holding the other tabs of this browser to a stop before activating. */
  | 'preparing'
  | 'restarting'
  | 'waiting'
  | 'redirecting'
  | 'manual'
  | 'failed';

const PROBE_INTERVAL_MS = 1500;
const MANUAL_INTERVAL_MS = 3000;
const K8S_INITIAL_DELAY_MS = 15_000;
const FORWARD_HINT_AFTER_MS = 45_000;
/** Re-mint the handoff once it has less than this left (server TTL is 600s),
 *  so a long manual rollout still lands on this page with its drafts. */
const TICKET_REFRESH_BELOW_MS = 90_000;
const TICKET_REFRESH_EVERY_MS = 30_000;
const TICKET_REFRESH_TIMEOUT_MS = 15_000;
/** Source status reads retried across a rollout blip before giving up. */
const SOURCE_READ_ATTEMPTS = 4;

export interface PivotRunOptions {
  agentUrl: string;
  restartToken: string;
  nextOrigin: string | null;
  profile: string;
  profileToken: string;
  installMode?: string | null;
  /** The backend owner reported by /api/tls/status. Only an Electron-owned
   * backend may be stopped and respawned through Electron IPC. */
  management?: 'native' | 'electron' | 'external';
  /** Durable state retained by a settings page resuming an activating switch. */
  transition?: TlsTransition | null;
  resumeStatus?: TlsRuntimeStatus | null;
  /** Route restored after a flow (such as first setup) finishes. */
  destinationRoute?: string;
  /** Called only after activation metadata has been durably persisted. */
  onActivated?: (status: TlsRuntimeStatus) => void;
  /** Called while the server waits for enrolled tabs to save handoffs. */
  onQuiescing?: (status: TlsRuntimeStatus) => void;
  onFailure?: (message: string) => void;
  /** Called once when this run stops owning the flow, whatever the reason —
   *  activated, failed, or quietly superseded by a newer run or a cancel.
   *  ``onActivated``/``onFailure`` report an OUTCOME and a superseded run has
   *  none, so anything a caller disabled for the duration (its activate button)
   *  has to be released here instead. */
  onSettled?: () => void;
}

function mountPath(): '/' | '/electron-renderer/' {
  return window.location.pathname.startsWith('/electron-renderer')
    ? '/electron-renderer/'
    : '/';
}

function safeRoute(fallback: string): string {
  const route = window.location.hash.slice(1) || fallback;
  return route.startsWith('/') && !route.startsWith('//') && !route.includes('\\')
    ? route : fallback;
}

export function useHttpsPivot() {
  const phase = ref<PivotPhase>('idle');
  const error = ref<string | null>(null);
  const forwardHint = ref(false);
  /** The last readiness verdict from the secure address (null on Electron,
   *  whose main process verifies instead, and before the first probe). */
  const readiness = ref<HttpsReadiness | null>(null);
  /** The transition this pivot is carrying the tab across. */
  const pinned = ref<TlsTransition | null>(null);
  /** Bumped whenever the cached ticket may have changed, so `recoveryUrl`
   *  (which reads sessionStorage) recomputes. */
  const ticketTick = ref(0);

  let generation = 0;
  let controller = new AbortController();
  let wake: (() => void) | null = null;
  let profileForLogin = '';
  let lastTicket: string | null = null;
  let lastTicketExpiresAt = 0;
  let lastActivatedStatus: TlsRuntimeStatus | null = null;
  let refreshing = false;
  let lastRefreshAttempt = 0;
  /** The run holding this tab's pre-activation upload barrier (and for which
   *  transition). Held from the barrier until activation is durable — after
   *  that it stays up on purpose until the page navigates. */
  let barrierRun: number | null = null;
  let barrierId: string | null = null;
  /** The barrier round this run registered, so releasing it can never cancel a
   *  newer run's barrier for the same transition. */
  let barrierNonce: string | null = null;

  function releaseBarrier(): void {
    if (barrierId) releaseBrowserMigration(barrierId, barrierNonce ?? undefined);
    barrierRun = null;
    barrierId = null;
    barrierNonce = null;
  }

  /** Start a new run: everything the previous one had in flight is abandoned,
   *  including an upload barrier it raised but never turned into activation. */
  function begin(): number {
    generation += 1;
    controller.abort();
    controller = new AbortController();
    wake?.();
    if (barrierRun !== null) releaseBarrier();
    return generation;
  }

  function alive(run: number): boolean {
    return run === generation;
  }

  /** A pause that `retryNow()` and cancellation both cut short. */
  function pause(ms: number, run: number): Promise<void> {
    if (!alive(run)) return Promise.resolve();
    const signal = controller.signal;
    return new Promise((resolve) => {
      let timer: ReturnType<typeof setTimeout> | null = null;
      const done = () => {
        if (timer) clearTimeout(timer);
        signal.removeEventListener('abort', done);
        if (wake === done) wake = null;
        resolve();
      };
      timer = setTimeout(done, ms);
      signal.addEventListener('abort', done, { once: true });
      wake = done;
    });
  }

  function handoffUrl(target: string, ticket: string): string {
    return `${target.replace(/\/$/, '')}${mountPath()}#/tls-handoff?ticket=${encodeURIComponent(ticket)}`;
  }

  /** The secure address as *this* browser reaches it. A transition names the
   *  origins of whichever tab prepared it (the server's own localhost, another
   *  LAN alias); probing or linking to those from here reaches the wrong
   *  machine, and a ticket bound to them is consumed, not redeemed, on arrival.
   *  Identity (id, instance, fingerprints) stays the raw transition's. */
  function secureOrigin(transition: TlsTransition): string {
    return localTransition(transition).target_origin;
  }

  function loginUrl(profile: string, transition: TlsTransition): string {
    const route = safeRoute(`/${profile}`);
    return `${secureOrigin(transition)}${mountPath()}#/login/${encodeURIComponent(profile)}?redirect=${encodeURIComponent(route)}`;
  }

  /** The newest usable ticket: the tab-wide cache first (the coordinator and a
   *  background refresh both write it), then this run's own copy. */
  function freshestTicket(transition: TlsTransition): string | null {
    const cached = getCachedTlsHandoff(transition.id);
    if (cached) return cached.ticket;
    return lastTicket && lastTicketExpiresAt * 1000 > Date.now() + 5_000 ? lastTicket : null;
  }

  function destination(transition: TlsTransition): string {
    const ticket = freshestTicket(transition);
    // The old token is invalid after the transport epoch changes. Never send
    // it to target_origin to extend an expired handoff: sign in over verified
    // HTTPS instead, with this page's route kept for after the login.
    return ticket
      ? handoffUrl(secureOrigin(transition), ticket)
      : loginUrl(profileForLogin || 'admin', transition);
  }

  /** Where the explicit "Open the HTTPS address" link goes right now. */
  const recoveryUrl = computed(() => {
    void ticketTick.value;
    void readiness.value;
    return pinned.value ? destination(pinned.value) : '';
  });

  function pin(transition: TlsTransition): void {
    pinned.value = transition;
    ticketTick.value += 1;
  }

  /** Read the source's status with this tab's token, riding out a blip. */
  async function readSource(options: PivotRunOptions, run: number): Promise<TlsRuntimeStatus> {
    let failure: unknown = null;
    for (let attempt = 0; attempt < SOURCE_READ_ATTEMPTS; attempt += 1) {
      try {
        return await fetchTlsStatus(options.agentUrl, options.restartToken);
      } catch (e) {
        failure = e;
        if (!isTransientTlsFailure(e) || !alive(run)) throw e;
        await pause(500 * 2 ** attempt, run);
        if (!alive(run)) throw e;
      }
    }
    throw failure;
  }

  async function preparedTransition(options: PivotRunOptions, run: number): Promise<TlsTransition> {
    const supplied = options.transition ?? pinned.value;
    let transition = supplied && ['prepared', 'quiescing', 'activating'].includes(supplied.phase)
      ? supplied
      : null;
    // A second browser hostname can join while tabs quiesce. For the generated
    // certificate that expands the SAN set and rotates the leaf under the same
    // already-trusted CA. Refresh prepared/quiescing metadata before sending
    // its fingerprint so a setup-page retry cannot loop forever on the old
    // leaf. Once activation is durable, the HTTP status endpoint is purposely
    // recovery-only, so retain the pinned activating transition instead.
    if (!transition || transition.phase !== 'activating') {
      try {
        const live = (await readSource(options, run)).transition;
        if (live) transition = live;
      } catch (e) {
        // 426 is plaintext saying it became recovery-only: a previous attempt
        // committed activation and the server came back on HTTPS before this
        // page heard the 202. Carry on with what this page pinned.
        if (!(e instanceof TlsApiError && e.status === 426 && supplied)) throw e;
        transition = { ...supplied, phase: 'activating' };
      }
    }
    if (!transition || !['prepared', 'quiescing', 'activating'].includes(transition.phase)) {
      throw new Error('The HTTPS transition is not prepared. Return to the certificate step and prepare it again.');
    }
    if (options.nextOrigin) {
      // Callers hint either with the target the server announced (Settings
      // passes the status it read) or with this browser's own host (setup's
      // next_origin comes from the request's Host header). Both name this
      // switch; anything else does not. Where the tab goes is always the
      // localized address either way.
      const hinted = new URL(options.nextOrigin).hostname;
      const announced = new URL(transition.target_origin).hostname;
      if (hinted !== announced && hinted !== new URL(secureOrigin(transition)).hostname) {
        throw new Error('The secure address does not match the server that prepared this transition.');
      }
    }
    pin(transition);
    return transition;
  }

  async function prime(
    options: PivotRunOptions,
    transition: TlsTransition,
    fresh = false,
  ) {
    await waitForMigrationReady(5 * 60_000);
    // Bound to the address this tab will land on: the ticket is redeemed there.
    const ticket = await primeTlsHandoff(
      localTransition(transition),
      options.agentUrl,
      options.profileToken,
      fresh,
    );
    lastTicket = ticket.ticket;
    lastTicketExpiresAt = ticket.expires_at;
    ticketTick.value += 1;
  }

  function useCachedTicket(transition: TlsTransition): void {
    const ticket = getCachedTlsHandoff(transition.id);
    lastTicket = ticket?.ticket ?? null;
    lastTicketExpiresAt = ticket?.expires_at ?? 0;
    ticketTick.value += 1;
  }

  /** Keep the handoff alive through a long manual wait. Best effort and never
   *  awaited by the probe loop: the source may be gone for good (that is the
   *  point of the switch), and then the tab signs in again on HTTPS instead. */
  function refreshTicketIfStale(options: PivotRunOptions, transition: TlsTransition): void {
    if (refreshing || !options.profileToken) return;
    if (getCachedTlsHandoff(transition.id, TICKET_REFRESH_BELOW_MS)) return;
    if (Date.now() - lastRefreshAttempt < TICKET_REFRESH_EVERY_MS) return;
    refreshing = true;
    lastRefreshAttempt = Date.now();
    let timer: ReturnType<typeof setTimeout> | null = null;
    const limit = new Promise<void>((resolve) => {
      timer = setTimeout(resolve, TICKET_REFRESH_TIMEOUT_MS);
    });
    void Promise.race([
      // Same sessionStorage slot the coordinator redeems from, so it must be
      // bound to the same (localized) destination as the coordinator's own.
      primeTlsHandoff(localTransition(transition), options.agentUrl, options.profileToken, true).then((ticket) => {
        lastTicket = ticket.ticket;
        lastTicketExpiresAt = ticket.expires_at;
        ticketTick.value += 1;
      }),
      limit,
    ]).catch(() => { /* not fatal: the login fallback keeps the route */ })
      .finally(() => {
        if (timer) clearTimeout(timer);
        refreshing = false;
      });
  }

  function navigate(run: number, url: string): boolean {
    if (!alive(run)) return false;
    phase.value = 'redirecting';
    window.location.replace(url);
    return true;
  }

  async function waitAndRedirect(options: PivotRunOptions, transition: TlsTransition, run: number) {
    const electronServer = window.cremind?.server;
    const manual = phase.value === 'manual';
    const started = Date.now();
    if (electronServer?.migrateHttps) {
      if (!manual) phase.value = 'waiting';
      // A timer, not a check per attempt: one migrateHttps call can itself
      // wait a long time in the main process.
      const hintTimer = setTimeout(() => {
        if (alive(run)) forwardHint.value = true;
      }, FORWARD_HINT_AFTER_MS);
      try {
        while (alive(run)) {
          try {
            const moved = await electronServer.migrateHttps({
              nextOrigin: transition.target_origin,
              transitionId: transition.id,
              instanceId: transition.instance_id,
            });
            if (!alive(run)) return;
            if (moved.ok) return;
            // Not a failure of the switch: the main process could not verify
            // the secure origin *yet*. Keep waiting with the reason on screen
            // rather than flashing the failed pane every few seconds.
            error.value = moved.error || 'Electron could not verify and open the HTTPS origin.';
          } catch (e) {
            if (!alive(run)) return;
            error.value = e instanceof Error ? e.message : String(e);
          }
          await pause(MANUAL_INTERVAL_MS, run);
        }
      } finally {
        clearTimeout(hintTimer);
      }
      return;
    }
    const kubernetes = (options.installMode ?? '').toLowerCase() === 'kubernetes';
    if (!manual) phase.value = 'waiting';
    if (kubernetes) {
      await pause(K8S_INITIAL_DELAY_MS, run);
      if (!alive(run)) return;
    }
    while (alive(run)) {
      const verdict = await probeHttpsReadiness(transition, {
        localize: localTransition, signal: controller.signal,
      });
      if (!alive(run)) return;
      readiness.value = verdict;
      if (verdict.ready) {
        // Read the ticket at the last moment: a background refresh or this
        // tab's coordinator may have minted a newer one while we waited.
        navigate(run, destination(transition));
        return;
      }
      if (Date.now() - started >= FORWARD_HINT_AFTER_MS) forwardHint.value = true;
      refreshTicketIfStale(options, transition);
      await pause(forwardHint.value ? MANUAL_INTERVAL_MS : PROBE_INTERVAL_MS, run);
    }
  }

  /** After a thrown (or unacknowledged) activation, find out whether the
   *  server committed it anyway — typically a 202 lost to the very restart it
   *  triggered. `moved` means plaintext no longer serves the application, so
   *  there is nothing left to ask the HTTP server (a restart included). */
  async function confirmPersisted(
    options: PivotRunOptions,
    transition: TlsTransition,
    run: number,
  ): Promise<{ status: TlsRuntimeStatus; moved: boolean } | null> {
    // Carries only what is true once the boundary moved. The status it builds
    // on may be the caller's *prepared* one (Settings hands over what it
    // rendered), whose can_cancel and runbook would otherwise reach onActivated
    // as a "Cancel HTTPS switch" for a switch that already happened — and that
    // click stops this wait before the cancel itself is refused.
    const assumed = (): TlsRuntimeStatus => ({
      ...(lastActivatedStatus ?? options.resumeStatus ?? {} as TlsRuntimeStatus),
      can_cancel: false,
      steps: [],
      instructions: [],
      quiesce_pending: 0,
      restart_required: false,
      restart_error: null,
      transition: { ...transition, phase: 'activating', awaiting_operator: false },
    });
    try {
      const status = await readSource(options, run);
      const live = status.transition;
      if (live?.id === transition.id && ['activating', 'active'].includes(live.phase)) {
        return { status, moved: false };
      }
      return null;
    } catch (e) {
      if (!alive(run)) return null;
      // Recovery-only plaintext proves the boundary moved with this switch.
      if (e instanceof TlsApiError && e.status === 426) return { status: assumed(), moved: true };
      // The source is unreachable, so ask the target: a secure server already
      // reporting this transition settles it the same way.
      const verdict = await probeHttpsReadiness(transition, {
        localize: localTransition, signal: controller.signal,
      });
      if (!alive(run) || !verdict.responded || verdict.transition?.id !== transition.id) return null;
      return { status: assumed(), moved: true };
    }
  }

  async function activateAndWait(options: PivotRunOptions, restart: boolean) {
    const run = begin();
    error.value = null;
    forwardHint.value = false;
    readiness.value = null;
    profileForLogin = options.profile;
    let transitionId: string | null = null;
    let activationPersisted = false;
    try {
      let transition = await preparedTransition(options, run);
      if (!alive(run)) return;
      transitionId = transition.id;
      const activationAlreadyStarted = transition.phase === 'activating';
      if (options.destinationRoute) {
        const next = new URL(window.location.href);
        next.hash = `#${options.destinationRoute}`;
        window.history.replaceState(window.history.state, '', next);
      }
      const electronServer = window.cremind?.server;
      if (electronServer?.prepareHttpsMigration) {
        const prepared = await electronServer.prepareHttpsMigration({
          nextOrigin: transition.target_origin,
          transitionId: transition.id,
          instanceId: transition.instance_id,
        });
        if (!alive(run)) return;
        if (!prepared.ok) {
          throw new Error(prepared.error || 'An Electron window is not ready to move to HTTPS.');
        }
      } else if (!activationAlreadyStarted) {
        barrierRun = run;
        barrierId = transition.id;
        // Visible from the first moment: this wait makes no request, so without
        // a phase the page would sit on an unchanged card for its duration.
        phase.value = 'preparing';
        barrierNonce = await waitForBrowserMigrationReady(transition.id);
        if (!alive(run)) {
          // Superseded while the barrier went up: drop it unless a newer run
          // has already raised its own for this transition.
          if (barrierRun === run) releaseBarrier();
          return;
        }
        await prime(options, transition, true);
        if (!alive(run)) return;
      } else {
        useCachedTicket(transition);
      }
      if (!activationAlreadyStarted) {
        // Ticket preparation above may have registered another hostname and
        // regenerated the local leaf to cover it. Pin activation to the final
        // durable preparation, after every window/tab has joined the barrier.
        const refreshed = (await readSource(options, run)).transition;
        if (!alive(run)) return;
        if (!refreshed || refreshed.id !== transition.id
          || !['prepared', 'quiescing'].includes(refreshed.phase)) {
          throw new Error('The HTTPS preparation changed while tabs were getting ready. Review it and retry activation.');
        }
        transition = refreshed;
        pin(refreshed);
      }
      let activated: TlsRuntimeStatus;
      // Plaintext is already recovery-only: no restart to request over it.
      let boundaryMoved = false;
      if (activationAlreadyStarted) {
        // Resuming: a page reload, a wizard retry, or a lost 202. Use what the
        // caller or a previous run saw; otherwise re-read the source, and if
        // that is recovery-only already, carry on with the pinned transition.
        const known = options.resumeStatus ?? lastActivatedStatus;
        if (known) {
          activated = known;
        } else {
          const confirmed = await confirmPersisted(options, transition, run);
          boundaryMoved = confirmed?.moved ?? false;
          activated = confirmed?.status ?? ({ transition } as TlsRuntimeStatus);
        }
      } else {
        try {
          activated = await activateHttps(
            options.agentUrl,
            options.restartToken,
            transition.id,
            transition.certificate_sha256 ?? transition.ca_sha256,
            restart,
            options.onQuiescing,
            () => alive(run),
          );
        } catch (e) {
          if (!alive(run)) return;
          const confirmed = await confirmPersisted(options, transition, run);
          if (!confirmed) throw e;
          boundaryMoved = confirmed.moved;
          activated = confirmed.status;
        }
      }
      if (!alive(run)) return;
      activationPersisted = true;
      if (barrierRun === run) {
        // Durable now: keep uploads gated until this tab navigates.
        barrierRun = null;
        barrierId = null;
        barrierNonce = null;
      }
      lastActivatedStatus = activated;
      options.onActivated?.(activated);
      const current = activated.transition ?? { ...transition, phase: 'activating' as const };
      pin(current);

      // Give other connected tabs a short window to mint their own profile-
      // bound tickets from the transition announcement before the listener exits.
      await pause(750, run);
      if (!alive(run)) return;
      if (activated.restart_error) {
        phase.value = 'manual';
        error.value = activated.restart_error;
      } else if (restart) {
        phase.value = 'restarting';
        // prepareHttpsMigration arms Electron's main-process coordinator
        // before activation. It owns an Electron child restart even if this
        // renderer closes after the 202 response. Other native supervisors
        // use the authenticated backend restart endpoint.
        if (options.management !== 'electron' && activated.restart_scheduled !== true
          && !boundaryMoved) {
          try {
            await requestServerRestart(options.agentUrl, options.restartToken);
          } catch (e) {
            // A server already going down cannot acknowledge the request; the
            // readiness wait below is what tells the truth about the restart.
            if (!isTransientTlsFailure(e)) throw e;
          }
          if (!alive(run)) return;
        }
      } else {
        phase.value = 'manual';
      }
      await waitAndRedirect(options, current, run);
    } catch (e) {
      // A superseded run owns nothing any more: begin() already released
      // what it held, and the newer run reports its own outcome.
      if (!alive(run)) return;
      void window.cremind?.server?.releaseHttpsMigration?.();
      if (!activationPersisted && transitionId) {
        releaseBrowserMigration(transitionId, barrierNonce ?? undefined);
      }
      if (barrierRun === run) {
        barrierRun = null;
        barrierId = null;
        barrierNonce = null;
      }
      phase.value = 'failed';
      const message = e instanceof Error ? e.message : String(e);
      error.value = message;
      options.onFailure?.(message);
    } finally {
      // Every exit this run still owns, including the `!alive(run)` returns
      // above, which report no outcome at all — without it a run that ended
      // without an outcome would leave whatever the caller disabled for its
      // duration disabled for good.
      //
      // Not when it has been superseded: `begin()` runs inside the newer
      // caller, which has already taken the flag for its own work, and this
      // run's continuation only resumes a microtask later. Releasing it there
      // would un-dim the buttons in the middle of a cancel or a retry — the
      // very "my click did nothing" shape this is meant to remove.
      if (alive(run)) options.onSettled?.();
    }
  }

  async function run(options: PivotRunOptions): Promise<void> {
    await activateAndWait(options, true);
  }

  function enterManualMode(options: PivotRunOptions): void {
    void activateAndWait(options, false);
  }

  /** Explicit escape hatch after the user has dealt with browser trust. Works
   *  without a completed run: the pinned transition names the destination,
   *  and the freshest cached ticket (or a login keeping this route) goes with it. */
  function redirectNow(): void {
    const transition = pinned.value;
    if (!transition) return;
    const url = destination(transition);
    begin();
    phase.value = 'redirecting';
    window.location.replace(url);
  }

  /** Probe again right now instead of at the next interval (a no-op when no
   *  wait is running — there is nothing to wake). */
  function retryNow(): void {
    wake?.();
  }

  function cancelManualProbe(): void {
    const wasRunning = phase.value !== 'idle';
    begin();
    if (wasRunning && phase.value !== 'redirecting') {
      phase.value = 'idle';
      error.value = null;
    }
    forwardHint.value = false;
    void window.cremind?.server?.releaseHttpsMigration?.();
  }

  if (getCurrentScope()) onScopeDispose(cancelManualProbe);

  return {
    phase: readonly(phase),
    error: readonly(error),
    forwardHint: readonly(forwardHint),
    readiness: readonly(readiness),
    transition: readonly(pinned),
    recoveryUrl,
    run,
    redirectNow,
    retryNow,
    enterManualMode,
    cancelManualProbe,
  };
}
