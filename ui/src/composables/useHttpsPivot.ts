// HTTPS transition used by both first-run setup and Settings → Security.
// Sessions cross the origin through short-lived, one-use server tickets;
// readiness is accepted only from the expected Cremind instance/transition.

import { getCurrentScope, onScopeDispose, readonly, ref } from 'vue';

import {
  activateHttps,
  fetchTlsStatus,
  requestServerRestart,
  type TlsRuntimeStatus,
  type TlsTransition,
} from '../services/configApi';
import { getCachedTlsHandoff, primeTlsHandoff } from '../services/httpsTransition';
import {
  releaseBrowserMigration,
  waitForBrowserMigrationReady,
  waitForMigrationReady,
} from '../services/migrationReadiness';

export type PivotPhase =
  | 'idle'
  | 'restarting'
  | 'waiting'
  | 'redirecting'
  | 'manual'
  | 'failed';

const PROBE_INTERVAL_MS = 1500;
const MANUAL_INTERVAL_MS = 3000;
const K8S_INITIAL_DELAY_MS = 15_000;
const FORWARD_HINT_AFTER_MS = 45_000;

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
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function mountPath(): '/' | '/electron-renderer/' {
  return window.location.pathname.startsWith('/electron-renderer')
    ? '/electron-renderer/'
    : '/';
}

export function useHttpsPivot() {
  const phase = ref<PivotPhase>('idle');
  const error = ref<string | null>(null);
  const forwardHint = ref(false);
  let cancelled = false;
  let lastTarget: string | null = null;
  let lastTicket: string | null = null;
  let lastTicketExpiresAt = 0;
  let lastTransition: TlsTransition | null = null;
  let lastActivatedStatus: TlsRuntimeStatus | null = null;

  function handoffUrl(target: string, ticket: string): string {
    return `${target.replace(/\/$/, '')}${mountPath()}#/tls-handoff?ticket=${encodeURIComponent(ticket)}`;
  }

  async function preparedTransition(options: PivotRunOptions): Promise<TlsTransition> {
    const supplied = options.transition ?? lastTransition;
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
      const live = (await fetchTlsStatus(options.agentUrl)).transition;
      if (live) transition = live;
    }
    if (!transition || !['prepared', 'quiescing', 'activating'].includes(transition.phase)) {
      throw new Error('The HTTPS transition is not prepared. Return to the certificate step and prepare it again.');
    }
    if (options.nextOrigin) {
      const hinted = new URL(options.nextOrigin);
      const target = new URL(transition.target_origin);
      if (hinted.hostname !== target.hostname) {
        throw new Error('The secure address does not match the server that prepared this transition.');
      }
    }
    lastTransition = transition;
    return transition;
  }

  async function prime(
    options: PivotRunOptions,
    transition: TlsTransition,
    fresh = false,
  ) {
    await waitForMigrationReady(5 * 60_000);
    const ticket = await primeTlsHandoff(
      transition,
      options.agentUrl,
      options.profileToken,
      fresh,
    );
    lastTarget = transition.target_origin;
    lastTicket = ticket.ticket;
    lastTicketExpiresAt = ticket.expires_at;
  }

  function useCachedTicket(transition: TlsTransition): void {
    const ticket = getCachedTlsHandoff(transition.id);
    lastTarget = transition.target_origin;
    lastTicket = ticket?.ticket ?? null;
    lastTicketExpiresAt = ticket?.expires_at ?? 0;
  }

  function loginUrl(options: PivotRunOptions, transition: TlsTransition): string {
    const route = window.location.hash.slice(1) || `/${options.profile}`;
    return `${transition.target_origin}${mountPath()}#/login/${encodeURIComponent(options.profile)}?redirect=${encodeURIComponent(route)}`;
  }

  async function waitAndRedirect(options: PivotRunOptions, transition: TlsTransition) {
    const electronServer = window.cremind?.server;
    const manual = phase.value === 'manual';
    if (electronServer?.migrateHttps) {
      if (!manual) phase.value = 'waiting';
      const hintTimer = window.setTimeout(() => {
        forwardHint.value = true;
      }, FORWARD_HINT_AFTER_MS);
      while (!cancelled) {
        try {
          const moved = await electronServer.migrateHttps({
            nextOrigin: transition.target_origin,
            transitionId: transition.id,
            instanceId: transition.instance_id,
          });
          if (moved.ok) {
            window.clearTimeout(hintTimer);
            return;
          }
          if (!manual) phase.value = 'failed';
          error.value = moved.error || 'Electron could not verify and open the HTTPS origin.';
          await sleep(MANUAL_INTERVAL_MS);
          if (!manual) phase.value = 'waiting';
        } catch (e) {
          if (!manual) phase.value = 'failed';
          error.value = e instanceof Error ? e.message : String(e);
          await sleep(MANUAL_INTERVAL_MS);
          if (!manual) phase.value = 'waiting';
        }
      }
      window.clearTimeout(hintTimer);
      return;
    }
    const kubernetes = (options.installMode ?? '').toLowerCase() === 'kubernetes';
    if (!manual) phase.value = 'waiting';
    if (kubernetes) await sleep(K8S_INITIAL_DELAY_MS);
    const started = Date.now();
    while (!cancelled) {
      let ready = false;
      try {
        const status = await fetchTlsStatus(transition.target_origin);
        const target = status.transition;
        ready = Boolean(
          status.serving_https
          && status.ready !== false
          && status.instance_id === transition.instance_id
          && target?.id === transition.id
          && target.instance_id === transition.instance_id
          && target.phase === 'active'
          && target.source_origin === transition.source_origin
          && target.target_origin === transition.target_origin
          && target.certificate_kind === transition.certificate_kind
          && target.certificate_sha256 === transition.certificate_sha256
          && target.ca_sha256 === transition.ca_sha256
        );
      } catch {
        // Restarting, untrusted certificate, and a dead port-forward all reject.
        // Keep the explanatory page alive instead of navigating to an error.
      }
      if (ready) {
        const cached = lastTicket && lastTicketExpiresAt * 1000 > Date.now() + 5_000
          ? { ticket: lastTicket }
          : getCachedTlsHandoff(transition.id);
        if (cached) {
          lastTicket = cached.ticket;
          phase.value = 'redirecting';
          window.location.replace(handoffUrl(transition.target_origin, lastTicket));
          return;
        }
        // The old token is invalid after the transport epoch changes. Do not
        // send it to target_origin in an attempt to extend an expired handoff.
        phase.value = 'redirecting';
        window.location.replace(loginUrl(options, transition));
        return;
      }
      if (Date.now() - started >= FORWARD_HINT_AFTER_MS) {
        forwardHint.value = true;
      }
      await sleep(forwardHint.value ? MANUAL_INTERVAL_MS : PROBE_INTERVAL_MS);
    }
  }

  async function activateAndWait(options: PivotRunOptions, restart: boolean) {
    error.value = null;
    forwardHint.value = false;
    cancelled = false;
    let transitionId: string | null = null;
    let activationPersisted = false;
    try {
      let transition = await preparedTransition(options);
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
        if (!prepared.ok) {
          throw new Error(prepared.error || 'An Electron window is not ready to move to HTTPS.');
        }
      } else if (!activationAlreadyStarted) {
        await waitForBrowserMigrationReady(transition.id);
        await prime(options, transition, true);
      } else {
        useCachedTicket(transition);
      }
      if (!activationAlreadyStarted) {
        // Ticket preparation above may have registered another hostname and
        // regenerated the local leaf to cover it. Pin activation to the final
        // durable preparation, after every window/tab has joined the barrier.
        const refreshed = (await fetchTlsStatus(options.agentUrl)).transition;
        if (!refreshed || refreshed.id !== transition.id
          || !['prepared', 'quiescing'].includes(refreshed.phase)) {
          throw new Error('The HTTPS preparation changed while tabs were getting ready. Review it and retry activation.');
        }
        transition = refreshed;
        lastTransition = refreshed;
      }
      const activated = activationAlreadyStarted
        ? options.resumeStatus ?? lastActivatedStatus
        : await activateHttps(
          options.agentUrl,
          options.restartToken,
          transition.id,
          transition.certificate_sha256 ?? transition.ca_sha256,
          restart,
          options.onQuiescing,
          () => !cancelled,
        );
      if (!activated) {
        throw new Error('HTTPS activation is already persisted. Restart Cremind with: cremind serve');
      }
      activationPersisted = true;
      lastActivatedStatus = activated;
      options.onActivated?.(activated);
      const current = activated.transition ?? { ...transition, phase: 'activating' as const };
      lastTransition = current;

      // Give other connected tabs a short window to mint their own profile-
      // bound tickets from the transition announcement before the listener exits.
      await sleep(750);
      if (activated.restart_error) {
        phase.value = 'manual';
        error.value = activated.restart_error;
      } else if (restart) {
        phase.value = 'restarting';
        // prepareHttpsMigration arms Electron's main-process coordinator
        // before activation. It owns an Electron child restart even if this
        // renderer closes after the 202 response. Other native supervisors
        // use the authenticated backend restart endpoint.
        if (options.management !== 'electron' && activated.restart_scheduled !== true) {
          await requestServerRestart(options.agentUrl, options.restartToken);
        }
      } else {
        phase.value = 'manual';
      }
      await waitAndRedirect(options, current);
    } catch (e) {
      void window.cremind?.server?.releaseHttpsMigration?.();
      if (!activationPersisted && transitionId) releaseBrowserMigration(transitionId);
      if (cancelled) {
        phase.value = 'idle';
        error.value = null;
        return;
      }
      phase.value = 'failed';
      const message = e instanceof Error ? e.message : String(e);
      error.value = message;
      options.onFailure?.(message);
    }
  }

  async function run(options: PivotRunOptions): Promise<void> {
    await activateAndWait(options, true);
  }

  function enterManualMode(options: PivotRunOptions): void {
    void activateAndWait(options, false);
  }

  /** Explicit escape hatch after the user has dealt with browser trust. */
  function redirectNow(): void {
    if (!lastTarget) return;
    phase.value = 'redirecting';
    window.location.replace(lastTicket ? handoffUrl(lastTarget, lastTicket) : lastTarget);
  }

  function cancelManualProbe(): void {
    cancelled = true;
    void window.cremind?.server?.releaseHttpsMigration?.();
  }

  if (getCurrentScope()) onScopeDispose(cancelManualProbe);

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
