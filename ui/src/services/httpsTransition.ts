import { computed, readonly, ref } from 'vue';

import {
  acknowledgeTlsReady,
  createTlsHandoff,
  fetchTlsStatus,
  registerTlsClient,
  TlsApiError,
  unregisterTlsClient,
  type TlsHandoffState,
  type TlsRuntimeStatus,
  type TlsTransition,
} from './configApi';
import {
  httpsReadinessAdvice,
  probeHttpsReadiness,
  sameCertificateAuthority,
  tunnelCommand,
  type HttpsReadiness,
  type HttpsReadinessReason,
} from './httpsReadiness';
import { subscribeTransportChange, type ProfileEventsSubHandle } from './profileEventsStream';
import {
  beginMigrationGate,
  migrationTabId,
  releaseBrowserMigration,
  setMigrationTicketPreparer,
  waitForMigrationReady,
} from './migrationReadiness';

const TRANSITION_KEY = 'cremind:https-transition';
// Which transition the user has chosen to stop being covered by. Deliberately
// persistent and per-origin rather than per-tab: the overlay it hides comes
// back on every reload and in every new tab, so a per-tab record would just
// repeat the lockout it exists to end.
const DISMISS_KEY = 'cremind:https-overlay-dismissed';
const TICKET_PREFIX = 'cremind:https-ticket:';
const DRAFT_PREFIX = 'cremind:draft:';
const CHANNEL_NAME = 'cremind:https-transition';
const RECOVERY_HINT_AFTER_MS = 45_000;
const PREFERENCES = [
  'theme',
  'auto_connect',
  'conversations_panel_collapsed',
  'sidebar_collapsed',
  'usage_chip_hover',
  'events_view_mode',
  'terminalPanelWidth',
  'rightPanelSplitRatio',
  'rightPanelShowHidden',
  'rightPanelViewMode',
  'rightPanelCollapsed',
  'agent_activity_panel_maximized',
  'eventRunDrawerMaximized',
] as const;

function preferenceKeys(profile: string): string[] {
  return [
    ...PREFERENCES,
    ...(profile ? [`chat_mode_${profile}`, `reasoning_enabled_${profile}`] : []),
  ];
}

/** `pending` is the deliberately non-blocking one: the switch is recorded but
 *  waiting for a deployment change, so this tab keeps working normally and only
 *  shows a chip. Blocking here would be the lockout, not a safeguard. */
export type HttpsTransitionPhase =
  'idle' | 'pending' | 'preparing' | 'waiting' | 'moving' | 'attention';
/** How often a waiting tab looks for the secure address. Slow on purpose: the
 *  deployment change can be hours away and nothing is blocked meanwhile. */
const PENDING_PROBE_MS = 30_000;
/** Re-mint a handoff before it expires (server TTL is 600s), so a tab that
 *  waited out a long rollout still lands on its own page instead of a login. */
const TICKET_REFRESH_MS = 90_000;

/** How often an actively waiting tab re-checks the secure address. */
const PROBE_INTERVAL_MS = 1500;

const phase = ref<HttpsTransitionPhase>('idle');
const error = ref<string | null>(null);
const active = ref<TlsTransition | null>(null);
/** The last readiness verdict from the secure address (null while none ran). */
const lastReadiness = ref<HttpsReadiness | null>(null);
/** Why it was not ready, for recovery guidance. */
const reason = computed<HttpsReadinessReason | null>(() =>
  lastReadiness.value && !lastReadiness.value.ready ? lastReadiness.value.reason : null);
/** Deployment facts learned from this tab's own authenticated heartbeats, for
 *  recovery guidance only. In memory on purpose: the port-forward line names
 *  cluster objects that are admin-only on the server. */
const installMode = ref<string | null>(null);
const portForward = ref<string | null>(null);
/** Bumped whenever a cached ticket may have changed, so `recoveryUrl` (which
 *  reads sessionStorage) recomputes. */
const ticketTick = ref(0);

let subscription: ProfileEventsSubHandle | null = null;
let channel: BroadcastChannel | null = null;
let pollGeneration = 0;
/** Aborts the probes of every loop older than the current poll generation. */
let probeAbort = new AbortController();
/** Cuts the current loop's pause short (retry, or a newer generation). */
let wakeProbe: (() => void) | null = null;
/** What the last announcement was handled with, so a retry can re-run it. */
let lastContext: { agentUrl: string; token: string } | null = null;
/** When this tab started waiting for which transition's secure address. */
let waitSince: { id: string; at: number } | null = null;
let quiesceGateRelease: (() => void) | null = null;
let quiescePreparation: Promise<void> | null = null;
let quiesceTransitionId: string | null = null;
let quiesceGeneration = 0;

export interface StoredTicket {
  ticket: string;
  expires_at: number;
}

function safeParse<T>(raw: string | null): T | null {
  if (!raw) return null;
  try { return JSON.parse(raw) as T; } catch { return null; }
}

// ── the escape hatch ──────────────────────────────────────────────────────
//
// A switch that never completes used to cover the whole application with a
// modal that had no exit and survived every reload, so a user whose HTTPS
// address never came up could not reach their own data to get it out. The
// overlay is now something they can put away.
//
// Dismissal only hides the explanation; it never stops the migration. The
// poll in `waitForTarget` and every resume path keep running, so a dismissed
// tab still moves itself to HTTPS the moment the secure address answers.

interface DismissRecord { id: string; at: number }

/** The transition this tab is currently waiting on, if it knows of one.
 *
 * Reads through to the durable record on purpose: `resume()` can raise the
 * overlay from the saved transition alone, without ever populating `active`,
 * and that is exactly the stranded state dismissal has to work in. */
function currentTransitionId(): string {
  if (active.value?.id) return active.value.id;
  try {
    return safeParse<TlsTransition>(localStorage.getItem(TRANSITION_KEY))?.id ?? '';
  } catch { return ''; }
}

function readDismissed(): DismissRecord | null {
  try { return safeParse<DismissRecord>(localStorage.getItem(DISMISS_KEY)); } catch { return null; }
}

const dismissedRecord = ref<DismissRecord | null>(readDismissed());

/** True only while the dismissal still refers to what is on screen.
 *
 * Matching on the id means a *different* transition shows the overlay again
 * with no clearing step to forget: the conservative direction is re-showing,
 * which the user can dismiss again, never silently hiding something new. */
const overlayDismissed = computed(() =>
  dismissedRecord.value !== null && dismissedRecord.value.id === currentTransitionId());

/** Put the overlay away and keep using this page. */
export function dismissHttpsOverlay(): void {
  const record: DismissRecord = { id: currentTransitionId(), at: Date.now() };
  dismissedRecord.value = record;
  // Written after the ref: `storage` events do not fire in the writing tab,
  // so this tab must update itself directly and use the event only to follow
  // its siblings.
  try { localStorage.setItem(DISMISS_KEY, JSON.stringify(record)); } catch { /* storage may be unavailable */ }
}

/** Bring the full explanation back. */
export function restoreHttpsOverlay(): void {
  dismissedRecord.value = null;
  try { localStorage.removeItem(DISMISS_KEY); } catch { /* storage may be unavailable */ }
}

function forgetDismissal(): void {
  dismissedRecord.value = null;
  try { localStorage.removeItem(DISMISS_KEY); } catch { /* best effort */ }
}

function normalizeOrigin(value: string): string | null {
  try { return new URL(value).origin; } catch { return null; }
}

export function validTransition(value: unknown): value is TlsTransition {
  if (!value || typeof value !== 'object') return false;
  const t = value as Partial<TlsTransition>;
  const source = normalizeOrigin(t.source_origin ?? '');
  const target = normalizeOrigin(t.target_origin ?? '');
  if (!t.id || !t.instance_id || !source || !target) return false;
  const sourceUrl = new URL(source);
  const targetUrl = new URL(target);
  return sourceUrl.protocol === 'http:'
    && targetUrl.protocol === 'https:'
    && sourceUrl.hostname === targetUrl.hostname
    && ['prepared', 'quiescing', 'activating', 'active', 'cancelled'].includes(t.phase ?? '');
}

function sameTransition(a: TlsTransition | null, b: TlsTransition): boolean {
  return Boolean(a
    && a.id === b.id
    && a.phase === b.phase
    // Same phase, different meaning: this flip is what moves a tab between
    // "carry on, a person has to act" and "the secure address is coming".
    && a.awaiting_operator === b.awaiting_operator
    && a.activation_error === b.activation_error
    && a.source_origin === b.source_origin
    && a.target_origin === b.target_origin
    && a.instance_id === b.instance_id
    && a.certificate_kind === b.certificate_kind
    && a.certificate_sha256 === b.certificate_sha256
    && a.ca_sha256 === b.ca_sha256
    && a.same_public_port === b.same_public_port
    && a.public_port === b.public_port);
}

/** Same switch, same trust: used to decide whether an unauthenticated sibling
 * announcement may steer this tab's pinned transition. Readiness itself goes
 * through `evaluateHttpsReadiness`; both pin a generated certificate by its CA,
 * because a replaced pod renews the leaf under it (see httpsReadiness.ts). */
function sameTransitionIdentity(a: TlsTransition | null, b: TlsTransition): boolean {
  return Boolean(a
    && a.id === b.id
    && a.instance_id === b.instance_id
    && a.source_origin === b.source_origin
    && a.target_origin === b.target_origin
    && a.certificate_kind === b.certificate_kind
    && sameCertificateAuthority(a, b)
    && a.same_public_port === b.same_public_port
    && a.public_port === b.public_port);
}

const PHASE_ORDER: Record<TlsTransition['phase'], number> = {
  prepared: 0,
  quiescing: 1,
  activating: 2,
  active: 3,
  cancelled: 4,
};

function rememberTransition(t: TlsTransition): boolean {
  // Every tab on this origin receives both SSE and BroadcastChannel frames.
  // Ignore duplicates and delayed frames so they neither echo indefinitely
  // nor restart a tab migration with an older phase.
  const previous = active.value;
  if (sameTransition(previous, t)) return false;
  const retryRollback = previous?.id === t.id
    && previous.phase === 'quiescing'
    && t.phase === 'prepared'
    && Number.isFinite(previous.created_at)
    && Number.isFinite(t.created_at)
    && t.created_at > previous.created_at;
  if (previous?.id === t.id && PHASE_ORDER[t.phase] < PHASE_ORDER[previous.phase]
    && !retryRollback) return false;
  if (previous && previous.id !== t.id
    && Number.isFinite(previous.created_at)
    && Number.isFinite(t.created_at)
    && t.created_at <= previous.created_at) return false;
  active.value = t;
  try {
    if (t.phase === 'cancelled') {
      localStorage.removeItem(TRANSITION_KEY);
      // The ticket it minted is single-use and bound to this transition, so it
      // is dead material now rather than something to keep for later.
      sessionStorage.removeItem(ticketKey(t.id));
    } else localStorage.setItem(TRANSITION_KEY, JSON.stringify(t));
  } catch { /* storage may be unavailable */ }
  // A cancelled switch has nothing left to hide, and leaving the record would
  // pre-dismiss an unrelated transition that later reuses this id.
  if (t.phase === 'cancelled') forgetDismissal();
  try { channel?.postMessage(t); } catch { /* best effort */ }
  return true;
}

/** Use the hostname/port through which this particular browser reaches the
 * server. Transition announcements are system-wide, so their URL may contain
 * the initiating admin tab's localhost or LAN alias. Unchanged off plain HTTP.
 * Exported for the Settings/setup pivot, which must reach, link to and bind
 * tickets to the same address every background tab uses. */
export function localTransition(t: TlsTransition): TlsTransition {
  if (window.location.protocol !== 'http:') return t;
  try {
    const source = new URL(window.location.origin);
    const announcedSource = new URL(t.source_origin);
    const target = new URL(t.target_origin);
    target.hostname = source.hostname;
    if (t.same_public_port === true) {
      target.port = source.port || '80';
    } else if (t.same_public_port === undefined) {
      // Compatibility with a transition prepared by an older backend.
      if (source.port) target.port = source.port;
      else if (announcedSource.port) target.port = '';
    }
    return {
      ...t,
      source_origin: source.origin,
      target_origin: target.origin,
    };
  } catch {
    return t;
  }
}

function transitionMount(): '/' | '/electron-renderer/' {
  return window.location.pathname.startsWith('/electron-renderer')
    ? '/electron-renderer/'
    : '/';
}

function currentProfile(route: string): string {
  const first = route.split(/[/?]/).filter(Boolean)[0] ?? '';
  // Public, profile-less routes: their first segment is never a profile.
  if (first && !['setup', 'setup-handoff', 'tls-handoff', 'login', 'oauth-return'].includes(first)) {
    return first;
  }
  try { return localStorage.getItem('profile_id') ?? ''; } catch { return ''; }
}

function snapshotState(): TlsHandoffState {
  const drafts: Record<string, string> = {};
  const preferences: Record<string, string> = {};
  const route = window.location.hash.slice(1) || '/';
  const profile = currentProfile(route);
  const profileDraftPrefix = profile ? `${DRAFT_PREFIX}${profile}:` : '';
  try {
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if (profileDraftPrefix && key?.startsWith(profileDraftPrefix)) {
        drafts[key] = sessionStorage.getItem(key) ?? '';
      }
    }
  } catch { /* storage may be unavailable */ }
  try {
    for (const key of preferenceKeys(profile)) {
      const value = localStorage.getItem(key);
      if (value !== null) preferences[key] = value;
    }
  } catch { /* storage may be unavailable */ }
  return {
    route,
    mount: transitionMount(),
    drafts,
    preferences,
  } as TlsHandoffState;
}

export function restoreTransitionState(
  state: TlsHandoffState | null | undefined,
  authenticatedProfile = '',
) {
  if (!state || typeof state !== 'object') return;
  const raw = state as TlsHandoffState & {
    drafts?: Record<string, string>;
    preferences?: Record<string, string>;
  };
  try {
    for (const [key, value] of Object.entries(raw.drafts ?? {})) {
      if (key.startsWith(DRAFT_PREFIX) && typeof value === 'string') sessionStorage.setItem(key, value);
    }
  } catch { /* storage may be unavailable */ }
  try {
    for (const [key, value] of Object.entries(raw.preferences ?? {})) {
      const profile = authenticatedProfile || currentProfile(window.location.hash.slice(1) || '/');
      if (preferenceKeys(profile).includes(key) && typeof value === 'string') {
        localStorage.setItem(key, value);
      }
    }
  } catch { /* storage may be unavailable */ }
}

function ticketKey(transitionId: string) {
  return `${TICKET_PREFIX}${transitionId}`;
}

/** Read a pre-activation handoff without making a network request. Bearer
 * tokens must never be sent to a destination learned from transition storage. */
export function getCachedTlsHandoff(
  transitionId: string,
  minimumValidityMs = 5_000,
): StoredTicket | null {
  let cached: StoredTicket | null = null;
  try { cached = safeParse<StoredTicket>(sessionStorage.getItem(ticketKey(transitionId))); } catch { return null; }
  return cached
    && typeof cached.ticket === 'string'
    && /^[A-Za-z0-9_-]{43}$/.test(cached.ticket)
    && Number.isFinite(cached.expires_at)
    && cached.expires_at * 1000 > Date.now() + minimumValidityMs
    ? cached : null;
}

export async function primeTlsHandoff(
  transition: TlsTransition,
  agentUrl: string,
  token: string,
  fresh = false,
): Promise<StoredTicket> {
  const key = ticketKey(transition.id);
  const cached = getCachedTlsHandoff(transition.id);
  if (!fresh && cached) return cached;

  const payload = {
    transition_id: transition.id,
    source_origin: transition.source_origin,
    target_origin: transition.target_origin,
    route: window.location.hash.slice(1) || '/',
    state: snapshotState(),
  };
  const minted = await createTlsHandoff(agentUrl, token, payload);
  sessionStorage.setItem(key, JSON.stringify(minted));
  ticketTick.value += 1;
  return minted;
}

// ── polling generations ───────────────────────────────────────────────────
//
// Every loop that may navigate this tab (waitForTarget, probeTargetSlowly)
// belongs to one poll generation. Starting a newer one — a later
// announcement, a cancellation, a rollback to prepared — aborts the older
// loop's in-flight probe and wakes its pause, and each loop re-checks its
// generation after every await and immediately before navigating. A probe
// that resolves after the switch was cancelled therefore can never move the
// tab.

function nextPollGeneration(): number {
  pollGeneration += 1;
  probeAbort.abort();
  probeAbort = new AbortController();
  wakeProbe?.();
  return pollGeneration;
}

/** A loop's pause, cut short by `retryHttpsTransition()` or a newer generation. */
function pauseProbe(ms: number, generation: number): Promise<void> {
  if (generation !== pollGeneration) return Promise.resolve();
  const signal = probeAbort.signal;
  return new Promise((resolve) => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const done = () => {
      if (timer) clearTimeout(timer);
      signal.removeEventListener('abort', done);
      if (wakeProbe === done) wakeProbe = null;
      resolve();
    };
    timer = setTimeout(done, ms);
    signal.addEventListener('abort', done, { once: true });
    wakeProbe = done;
  });
}

function probeTarget(transition: TlsTransition): Promise<HttpsReadiness> {
  return probeHttpsReadiness(transition, { localize: localTransition, signal: probeAbort.signal });
}

function noteReadiness(readiness: HttpsReadiness): void {
  lastReadiness.value = readiness;
  // Ticket validity is time-based; re-evaluate the recovery link as we go.
  ticketTick.value += 1;
}

function noteDeployment(status: TlsRuntimeStatus): void {
  if (typeof status.install_mode === 'string') installMode.value = status.install_mode;
  const tunnel = tunnelCommand(status);
  if (tunnel) portForward.value = tunnel;
}

async function waitForTarget(transition: TlsTransition, generation: number): Promise<boolean> {
  // A focus/online resume restarts this loop for the same switch. Keep the
  // original start so a tab already showing recovery guidance does not drop
  // back to a spinner for another 45 seconds every time the window regains
  // focus — which is exactly when the user returns from fixing the tunnel.
  const carried = waitSince?.id === transition.id;
  const started = carried && waitSince ? waitSince.at : Date.now();
  waitSince = { id: transition.id, at: started };
  if (phase.value !== 'attention' || Date.now() - started < RECOVERY_HINT_AFTER_MS) {
    phase.value = 'waiting';
  }
  while (generation === pollGeneration) {
    // Bounded: a request the rollout swallows is abandoned after five seconds
    // and counts as unreachable, so the loop keeps going and keeps explaining.
    const readiness = await probeTarget(transition);
    if (generation !== pollGeneration) return false;
    noteReadiness(readiness);
    if (readiness.ready) return true;
    if (Date.now() - started >= RECOVERY_HINT_AFTER_MS) {
      phase.value = 'attention';
      error.value = httpsReadinessAdvice(readiness);
    }
    await pauseProbe(PROBE_INTERVAL_MS, generation);
  }
  return false;
}

function handoffUrl(transition: TlsTransition, ticket: string, mount = transitionMount()) {
  return `${transition.target_origin}${mount}#/tls-handoff?ticket=${encodeURIComponent(ticket)}`;
}

function safeRoute(): string {
  const route = window.location.hash.slice(1) || '/';
  return route.startsWith('/') && !route.startsWith('//') && !route.includes('\\')
    ? route : '/';
}

function expiredSessionUrl(transition: TlsTransition): string {
  const route = safeRoute();
  const profile = currentProfile(route);
  if (!/^[a-z0-9_-]{1,64}$/.test(profile)) {
    return `${transition.target_origin}${transitionMount()}#${route}`;
  }
  return `${transition.target_origin}${transitionMount()}#/login/${encodeURIComponent(profile)}?redirect=${encodeURIComponent(route)}`;
}

async function prepareThisClient(
  transition: TlsTransition,
  agentUrl: string,
  token: string,
): Promise<void> {
  if (!token) return;
  if (quiesceTransitionId === transition.id) {
    // Already saved and acknowledged, so this tab is where every acknowledged
    // tab is. A retry of an overlay left up by a failed status read ends here,
    // and returning without a phase kept that overlay up for good.
    if (phase.value === 'attention') {
      phase.value = 'waiting';
      error.value = null;
    }
    return;
  }
  if (quiescePreparation) return quiescePreparation;
  const generation = quiesceGeneration;
  quiescePreparation = (async () => {
    if (!quiesceGateRelease) quiesceGateRelease = beginMigrationGate();
    phase.value = 'preparing';
    await waitForMigrationReady(5 * 60_000);
    if (generation !== quiesceGeneration) return;
    const electronServer = window.cremind?.server;
    if (electronServer?.prepareHttpsMigration) {
      const prepared = await electronServer.prepareHttpsMigration({
        nextOrigin: transition.target_origin,
        transitionId: transition.id,
        instanceId: transition.instance_id,
      });
      if (!prepared.ok) {
        throw new Error(prepared.error || 'An Electron window could not prepare its HTTPS handoff.');
      }
    } else {
      await primeTlsHandoff(transition, agentUrl, token, true);
    }
    if (generation !== quiesceGeneration) return;
    await acknowledgeTlsReady(agentUrl, token, migrationTabId, transition.id);
    if (generation !== quiesceGeneration) return;
    quiesceTransitionId = transition.id;
    // Keep new work gated until cancellation or navigation. The server still
    // needs other enrolled tabs to acknowledge before it changes token epoch.
    phase.value = 'waiting';
  })();
  try {
    await quiescePreparation;
  } finally {
    quiescePreparation = null;
  }
}

/** Watch for the secure address without blocking anything.
 *
 * The counterpart to `waitForTarget` for a switch that is waiting on a person:
 * same verification, far slower, and the page stays completely usable while it
 * runs. It also keeps this tab's handoff fresh, because the server ticket
 * expires in ten minutes and a rollout can easily take longer — without that,
 * waiting patiently would cost the user their page and their drafts.
 */
async function probeTargetSlowly(
  transition: TlsTransition,
  agentUrl: string,
  token: string,
  generation: number,
) {
  while (generation === pollGeneration) {
    await pauseProbe(PENDING_PROBE_MS, generation);
    if (generation !== pollGeneration) return;
    if (token && document.visibilityState === 'visible'
      && !getCachedTlsHandoff(transition.id, TICKET_REFRESH_MS)) {
      // Best effort: a tab that cannot mint one just signs in again on HTTPS.
      try { await primeTlsHandoff(transition, agentUrl, token, true); } catch { /* not fatal */ }
      if (generation !== pollGeneration) return;
    }
    const readiness = await probeTarget(transition);
    if (generation !== pollGeneration) return;
    noteReadiness(readiness);
    if (readiness.ready && readiness.transition) {
      await handleHttpsTransition(readiness.transition, agentUrl, token);
      return;
    }
    // A secure server that answers but could not finish is worth saying on
    // the chip; anything else just means the deployment change has not landed.
    if (readiness.reason === 'activation-failed') error.value = readiness.message;
  }
}

async function moveThisTab(
  transition: TlsTransition,
  agentUrl: string,
  token: string,
) {
  if (window.location.protocol === 'https:') return;
  const generation = nextPollGeneration();
  try {
    if (transition.phase === 'prepared') {
      // The pre-activation barrier mints a fresh ticket after uploads settle.
      // Caching one while the user is still reading the trust instructions
      // would restore stale drafts or attachment references.
      phase.value = 'idle';
      return;
    }
    if (transition.phase === 'quiescing') {
      await prepareThisClient(transition, agentUrl, token);
      return;
    }
    if (window.cremind?.server?.migrateHttps) {
      phase.value = 'waiting';
      const moved = await window.cremind.server.migrateHttps({
        nextOrigin: transition.target_origin,
        transitionId: transition.id,
        instanceId: transition.instance_id,
      });
      if (generation !== pollGeneration) return;
      if (!moved.ok) {
        throw new Error(moved.error || 'Electron could not verify and open the HTTPS origin.');
      }
      return;
    }
    // A retry from the recovery overlay keeps it up instead of flashing the
    // preparing chip; uploads have long settled by then.
    if (phase.value !== 'attention') phase.value = 'preparing';
    await waitForMigrationReady(5 * 60_000);
    if (generation !== pollGeneration) return;
    if (!await waitForTarget(transition, generation)) return;
    // Tickets are minted while the authenticated HTTP source is still live.
    // Never send a bearer token to target_origin: transition metadata is
    // intentionally credential-free and may come from browser storage or BC.
    // A tab whose private ticket expired signs in over verified HTTPS with its
    // intended route retained, as opposed to extending the old session.
    // Read the ticket only now: a refresh during the wait may have replaced it.
    const ticket = getCachedTlsHandoff(transition.id);
    const destination = ticket
      ? handoffUrl(transition, ticket.ticket)
      : expiredSessionUrl(transition);
    if (generation !== pollGeneration) return;
    phase.value = 'moving';
    window.location.replace(destination);
  } catch (e) {
    // A superseded attempt has nothing to report: the newer one owns the tab.
    if (generation !== pollGeneration) return;
    phase.value = 'attention';
    error.value = e instanceof Error ? e.message : String(e);
  }
}

export async function handleHttpsTransition(
  transition: TlsTransition,
  agentUrl: string,
  token: string,
) {
  if (!validTransition(transition)) return;
  transition = localTransition(transition);
  if (!validTransition(transition)) return;
  lastContext = { agentUrl, token };
  if (!rememberTransition(transition)) {
    // Act on the switch this tab holds, never on the refused frame: that one
    // is a duplicate, or stale (rememberTransition takes anything newer), and a
    // stale phase must neither clear nor restart the wait of a switch that has
    // moved on. Resume paths also re-announce an active switch as activating.
    const held = active.value;
    if (phase.value === 'attention' && held?.id === transition.id) {
      if (held.phase === 'prepared') {
        // Nothing waits on a switch that is merely prepared, and no retry
        // path (this one, "Check now", a resume) ever ran for one — so an
        // overlay left up for it (a status read that timed out) stayed for
        // good. Put the tab back to work.
        phase.value = 'idle';
        error.value = null;
        lastReadiness.value = null;
      } else if (held.phase !== 'cancelled') {
        // A transient HTTPS fetch or handoff failure leaves the recovery
        // overlay on the old origin. Focus/online/pageshow must retry the same
        // durable transition instead of treating it as a duplicate.
        error.value = null;
        await moveThisTab(held, agentUrl, token);
      }
    }
    return;
  }
  if (transition.phase === 'prepared') {
    // The server rolls a failed pre-restart commit back to prepared with a
    // newer timestamp. Stop any in-flight acknowledgement and reopen uploads
    // so the same transition can start a clean readiness round on retry.
    quiesceGeneration += 1;
    quiesceTransitionId = null;
    quiesceGateRelease?.();
    quiesceGateRelease = null;
    releaseBrowserMigration(transition.id);
    nextPollGeneration();
    waitSince = null;
    phase.value = 'idle';
    error.value = null;
    lastReadiness.value = null;
    return;
  }
  if (transition.phase === 'cancelled') {
    quiesceGeneration += 1;
    quiesceTransitionId = null;
    quiesceGateRelease?.();
    quiesceGateRelease = null;
    releaseBrowserMigration(transition.id);
    nextPollGeneration();
    waitSince = null;
    phase.value = 'idle';
    error.value = null;
    lastReadiness.value = null;
    return;
  }
  if (transition.phase === 'activating' && transition.awaiting_operator) {
    // There is nothing to move to yet and nothing has been invalidated: a
    // person still has to change the deployment, which can take hours. This
    // tab therefore carries on working and only shows a chip — blocking it on
    // an HTTPS address that does not exist is the lockout, not a safeguard.
    if (window.location.protocol !== 'http:') return;
    quiesceGateRelease?.();
    quiesceGateRelease = null;
    releaseBrowserMigration(transition.id);
    const generation = nextPollGeneration();
    phase.value = 'pending';
    error.value = transition.activation_error ?? null;
    void probeTargetSlowly(transition, agentUrl, token, generation);
    return;
  }
  await moveThisTab(transition, agentUrl, token);
}

/** Check the secure address again right now.
 *
 * Wakes whichever loop is waiting (the blocking wait or the slow pending
 * probe); when nothing is running — the last attempt ended in `attention`
 * with an error — it re-runs the move for the transition this tab holds. */
export function retryHttpsTransition(): void {
  if (wakeProbe) {
    wakeProbe();
    return;
  }
  // Otherwise a probe is already in flight, unless the last attempt failed
  // outright. Only that case needs a new attempt — and never for a switch
  // still waiting on its operator, which must not start blocking the tab.
  if (phase.value !== 'attention') return;
  const transition = active.value ?? savedHttpsTransition();
  if (!transition || !lastContext || window.location.protocol !== 'http:') return;
  if (!['activating', 'active'].includes(transition.phase) || transition.awaiting_operator) return;
  error.value = null;
  const { agentUrl, token } = lastContext;
  void moveThisTab(transition, agentUrl, token);
}

/** The durable transition this browser recorded, localized to this tab's
 * address — for pages that must explain a switch while the plaintext server
 * no longer answers them (it went recovery-only, or the pod is gone). */
export function savedHttpsTransition(): TlsTransition | null {
  try {
    const saved = safeParse<TlsTransition>(localStorage.getItem(TRANSITION_KEY));
    if (!saved || !validTransition(saved) || saved.phase === 'cancelled') return null;
    const local = localTransition(saved);
    return validTransition(local) ? local : null;
  } catch {
    return null;
  }
}

/** Install one coordinator per renderer. No credentials are broadcast. */
export function installHttpsTransitionCoordinator(
  agentUrl: string,
  token: string,
): () => void {
  let stopped = false;
  let registrationTimer: ReturnType<typeof setInterval> | null = null;
  let channelRetryTimer: ReturnType<typeof setTimeout> | null = null;
  let resumeTimer: ReturnType<typeof setTimeout> | null = null;
  let resumeRunning = false;
  subscription?.close();
  subscription = token
    ? subscribeTransportChange(agentUrl, token, (t) => {
      void handleHttpsTransition(t, agentUrl, token);
    })
    : null;
  if (!channel && typeof BroadcastChannel !== 'undefined') {
    channel = new BroadcastChannel(CHANNEL_NAME);
  }
  setMigrationTicketPreparer(token ? async (transitionId) => {
    let transition = active.value;
    if (!transition || transition.id !== transitionId) {
      const status = await fetchTlsStatus(agentUrl);
      transition = status.transition ? localTransition(status.transition) : null;
    }
    if (!transition || transition.id !== transitionId
      || !['prepared', 'quiescing'].includes(transition.phase)) {
      throw new Error('This tab does not have the prepared HTTPS transition.');
    }
    await primeTlsHandoff(transition, agentUrl, token, true);
  } : null);
  const reconcileChannelTransition = async (announced: TlsTransition) => {
    if (stopped) return;
    const candidate = localTransition(announced);
    if (!validTransition(candidate)) return;
    try {
      // BroadcastChannel has no authentication. Re-read credential-free status
      // from the configured server and use its transition fields, rather than
      // trusting a sibling tab's destination or phase.
      const status = await fetchTlsStatus(agentUrl);
      if (stopped) return;
      const current = status.transition ? localTransition(status.transition) : null;
      if (current?.id === candidate.id && current.instance_id === candidate.instance_id) {
        await handleHttpsTransition(current, agentUrl, token);
      }
      return;
    } catch (e) {
      if (stopped) return;
      const pinned = active.value;
      if (!pinned || !sameTransitionIdentity(pinned, candidate)) return;
      if (e instanceof TlsApiError && e.status === 426) {
        // The source's explicit recovery-only response proves activation has
        // crossed the HTTP boundary. Keep polling the already-pinned target.
        await handleHttpsTransition(
          { ...pinned, phase: 'activating', awaiting_operator: false }, agentUrl, token,
        );
        return;
      }
      // Never throws; a restart or certificate prompt reads as not ready.
      const readiness = await probeHttpsReadiness(pinned, { localize: localTransition });
      if (stopped) return;
      if (readiness.ready && readiness.transition) {
        await handleHttpsTransition(readiness.transition, agentUrl, token);
        return;
      }
      if (!channelRetryTimer) {
        channelRetryTimer = setTimeout(() => {
          channelRetryTimer = null;
          void reconcileChannelTransition(announced);
        }, 1500);
      }
    }
  };
  const onChannelMessage = (event: MessageEvent) => {
    if (validTransition(event.data)) void reconcileChannelTransition(event.data);
  };
  channel?.addEventListener('message', onChannelMessage);
  const registerClient = async () => {
    if (stopped || !token || window.location.protocol !== 'http:') return;
    try {
      const status = await registerTlsClient(agentUrl, token, migrationTabId);
      if (stopped) return;
      // The heartbeat answers as this tab's profile, so an admin's carries
      // the Kubernetes identity: remember the reconnect line for later, when
      // the rollout has taken this server away and nobody can be asked.
      noteDeployment(status);
      if (status.transition) {
        await handleHttpsTransition(status.transition, agentUrl, token);
      }
    } catch {
      // A restarting server or an epoch changed by another activation can
      // reject this heartbeat. Durable transition recovery below handles it.
    }
  };
  const unregisterClient = (keepalive = false) => {
    if (!token || window.location.protocol !== 'http:') return;
    void unregisterTlsClient(agentUrl, token, migrationTabId, keepalive).catch(() => {});
  };
  const onPageHide = (event: PageTransitionEvent) => {
    if (!event.persisted) unregisterClient(true);
  };
  window.addEventListener('pagehide', onPageHide);
  void registerClient();
  registrationTimer = setInterval(() => { void registerClient(); }, 10_000);
  const scheduleResume = () => {
    if (stopped || resumeTimer) return;
    resumeTimer = setTimeout(() => {
      resumeTimer = null;
      void resume();
    }, 1500);
  };
  const resume = async () => {
    if (stopped || resumeRunning) return;
    resumeRunning = true;
    try {
    const saved = safeParse<TlsTransition>(localStorage.getItem(TRANSITION_KEY));
    if (!saved || !validTransition(saved)) return;

    // A suspended tab may retain prepared or quiescing while its SSE connection
    // is frozen. Always reconcile storage with live source/target status before
    // acting; in particular, do not try to ACK an obsolete quiesce round after
    // the source has crossed into recovery-only mode.
    let sourceRecoveryOnly = false;
    try {
      const sourceStatus = await fetchTlsStatus(window.location.origin);
      if (stopped) return;
      const sourceTransition = sourceStatus.transition;
      if (sourceTransition?.id === saved.id
        && sourceTransition.instance_id === saved.instance_id) {
        await handleHttpsTransition(sourceTransition, agentUrl, token);
        return;
      }
      // A reachable source with another installation or transition makes this
      // browser record stale. Do not use its destination.
      try { localStorage.removeItem(TRANSITION_KEY); } catch { /* best effort */ }
      forgetDismissal();
      return;
    } catch (e) {
      sourceRecoveryOnly = e instanceof TlsApiError && e.status === 426;
    }
    if (stopped) return;
    // Certificate trust or a restart still in progress reads as not ready
    // and keeps this page, with its guidance, where it is.
    const readiness = await probeHttpsReadiness(localTransition(saved), { localize: localTransition });
    if (stopped) return;
    if (readiness.ready && readiness.transition) {
      await handleHttpsTransition(readiness.transition, agentUrl, token);
      return;
    }
    if (sourceRecoveryOnly) {
      // A 426 is the source saying it has become recovery-only, which proves
      // the transport moved on. Stop waiting and go find the secure address.
      await handleHttpsTransition(
        { ...saved, phase: 'activating', awaiting_operator: false }, agentUrl, token,
      );
      return;
    }
    if (['activating', 'active'].includes(saved.phase)) {
      // Merely unreachable is not the same thing: a replaced pod, a dropped
      // port-forward, a laptop waking up. A switch that was waiting for its
      // operator keeps waiting, or every rollout blip raises the blocking
      // overlay and a transient outage reads as being locked out.
      if (saved.awaiting_operator) {
        await handleHttpsTransition(saved, agentUrl, token);
        scheduleResume();
        return;
      }
      await handleHttpsTransition({ ...saved, phase: 'activating' }, agentUrl, token);
      return;
    }
    // Prepared or quiescing, and the source did not answer this once (a read
    // that timed out mid-rollout, a blip). Nothing has activated, so there is
    // nothing to block this tab on and no secure address to recover to — the
    // blocking overlay raised here used to stay up for good, because every
    // path that clears it again skips a prepared switch. Keep what this tab
    // shows and read again shortly: the next answer is handled like any other.
    scheduleResume();
    } finally {
      resumeRunning = false;
    }
  };
  const resumeListener = () => { void resume(); };
  // Dismissing in one tab settles the others: `storage` fires only in the tabs
  // that did not write, which is exactly the sync wanted here.
  const dismissListener = (event: StorageEvent) => {
    if (event.key === DISMISS_KEY || event.key === null) {
      dismissedRecord.value = readDismissed();
    }
  };
  window.addEventListener('storage', dismissListener);
  window.addEventListener('online', resumeListener);
  window.addEventListener('pageshow', resumeListener);
  window.addEventListener('focus', resumeListener);
  document.addEventListener('visibilitychange', resumeListener);
  void resume();
  return () => {
    stopped = true;
    if (registrationTimer) clearInterval(registrationTimer);
    if (channelRetryTimer) clearTimeout(channelRetryTimer);
    if (resumeTimer) clearTimeout(resumeTimer);
    unregisterClient();
    window.removeEventListener('pagehide', onPageHide);
    subscription?.close();
    subscription = null;
    setMigrationTicketPreparer(null);
    window.removeEventListener('storage', dismissListener);
    window.removeEventListener('online', resumeListener);
    window.removeEventListener('pageshow', resumeListener);
    window.removeEventListener('focus', resumeListener);
    document.removeEventListener('visibilitychange', resumeListener);
    if (channel) {
      channel.removeEventListener('message', onChannelMessage);
      channel.close();
      channel = null;
    }
  };
}

/** The explicit "open the secure address" link for this tab, right now: the
 * one-use handoff when this tab still holds a live ticket (the page, its
 * drafts and its session come along), else a login on HTTPS that keeps this
 * route for afterwards. Empty while no switch is known. */
const recoveryUrl = computed(() => {
  void ticketTick.value;
  const known = active.value;
  const transition = known && known.phase !== 'cancelled' ? known : savedHttpsTransition();
  if (!transition) return '';
  const ticket = getCachedTlsHandoff(transition.id);
  return ticket ? handoffUrl(transition, ticket.ticket) : expiredSessionUrl(transition);
});

export const httpsTransitionState = {
  phase: readonly(phase),
  error: readonly(error),
  transition: readonly(active),
  /** Why the secure address was not ready at the last probe, and the whole
   *  verdict (its `message` is the server's own words when it answered). */
  reason,
  readiness: readonly(lastReadiness),
  /** See `recoveryUrl` above. */
  recoveryUrl,
  /** `install_mode` and the Kubernetes reconnect line (admin only), as last
   *  heard from this server while it still answered. */
  installMode: readonly(installMode),
  portForward: readonly(portForward),
  /** Whether the user has put the blocking explanation away for what is on
   *  screen. The switch itself carries on regardless. */
  overlayDismissed,
  retry: retryHttpsTransition,
};
