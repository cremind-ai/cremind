import { readonly, ref } from 'vue';

import {
  acknowledgeTlsReady,
  createTlsHandoff,
  fetchTlsStatus,
  registerTlsClient,
  TlsApiError,
  unregisterTlsClient,
  type TlsHandoffState,
  type TlsTransition,
} from './configApi';
import { subscribeTransportChange, type ProfileEventsSubHandle } from './profileEventsStream';
import {
  beginMigrationGate,
  migrationTabId,
  releaseBrowserMigration,
  setMigrationTicketPreparer,
  waitForMigrationReady,
} from './migrationReadiness';

const TRANSITION_KEY = 'cremind:https-transition';
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

export type HttpsTransitionPhase = 'idle' | 'preparing' | 'waiting' | 'moving' | 'attention';

const phase = ref<HttpsTransitionPhase>('idle');
const error = ref<string | null>(null);
const active = ref<TlsTransition | null>(null);

let subscription: ProfileEventsSubHandle | null = null;
let channel: BroadcastChannel | null = null;
let pollGeneration = 0;
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
    && a.source_origin === b.source_origin
    && a.target_origin === b.target_origin
    && a.instance_id === b.instance_id
    && a.certificate_kind === b.certificate_kind
    && a.certificate_sha256 === b.certificate_sha256
    && a.ca_sha256 === b.ca_sha256
    && a.same_public_port === b.same_public_port
    && a.public_port === b.public_port);
}

function sameTransitionIdentity(a: TlsTransition | null, b: TlsTransition): boolean {
  return Boolean(a
    && a.id === b.id
    && a.instance_id === b.instance_id
    && a.source_origin === b.source_origin
    && a.target_origin === b.target_origin
    && a.certificate_kind === b.certificate_kind
    && a.certificate_sha256 === b.certificate_sha256
    && a.ca_sha256 === b.ca_sha256
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
    if (t.phase === 'cancelled') localStorage.removeItem(TRANSITION_KEY);
    else localStorage.setItem(TRANSITION_KEY, JSON.stringify(t));
  } catch { /* storage may be unavailable */ }
  try { channel?.postMessage(t); } catch { /* best effort */ }
  return true;
}

/** Use the hostname/port through which this particular browser reaches the
 * server. Transition announcements are system-wide, so their URL may contain
 * the initiating admin tab's localhost or LAN alias. */
function localTransition(t: TlsTransition): TlsTransition {
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
  if (first && !['setup', 'setup-handoff', 'tls-handoff', 'login'].includes(first)) return first;
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
  return minted;
}

async function waitForTarget(transition: TlsTransition, generation: number): Promise<boolean> {
  phase.value = 'waiting';
  const started = Date.now();
  while (generation === pollGeneration) {
    try {
      const status = await fetchTlsStatus(transition.target_origin);
      const targetTransition = status.transition
        ? localTransition(status.transition)
        : null;
      if (
        status.serving_https
        && status.ready !== false
        && status.instance_id === transition.instance_id
        && targetTransition?.phase === 'active'
        && sameTransitionIdentity(transition, targetTransition)
      ) return true;
    } catch {
      // An untrusted certificate and a server that is still restarting are
      // indistinguishable to fetch. Keep the current page alive with guidance.
    }
    if (Date.now() - started >= RECOVERY_HINT_AFTER_MS) {
      phase.value = 'attention';
      error.value = 'The secure server is still unreachable. Trust the certificate, check the restart, or reopen the Kubernetes port-forward.';
    }
    await new Promise(resolve => setTimeout(resolve, 1500));
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
  if (!token || quiesceTransitionId === transition.id) return;
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

async function moveThisTab(
  transition: TlsTransition,
  agentUrl: string,
  token: string,
) {
  if (window.location.protocol === 'https:') return;
  const generation = ++pollGeneration;
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
      if (!moved.ok) {
        throw new Error(moved.error || 'Electron could not verify and open the HTTPS origin.');
      }
      return;
    }
    phase.value = 'preparing';
    await waitForMigrationReady(5 * 60_000);
    let ticket = getCachedTlsHandoff(transition.id);
    if (!await waitForTarget(transition, generation)) return;
    // Tickets are minted while the authenticated HTTP source is still live.
    // Never send a bearer token to target_origin: transition metadata is
    // intentionally credential-free and may come from browser storage or BC.
    // A tab whose private ticket expired signs in over verified HTTPS with its
    // intended route retained, as opposed to extending the old session.
    ticket = getCachedTlsHandoff(transition.id);
    phase.value = 'moving';
    const destination = ticket
      ? handoffUrl(transition, ticket.ticket)
      : expiredSessionUrl(transition);
    window.location.replace(destination);
  } catch (e) {
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
  if (!rememberTransition(transition)) {
    // A transient HTTPS fetch or handoff failure leaves the recovery overlay
    // on the old origin. Focus/online/pageshow must retry the same durable
    // transition instead of treating it as a duplicate announcement.
    if (phase.value === 'attention' && active.value?.id === transition.id
      && transition.phase !== 'prepared' && transition.phase !== 'cancelled') {
      error.value = null;
      await moveThisTab(transition, agentUrl, token);
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
    pollGeneration += 1;
    phase.value = 'idle';
    error.value = null;
    return;
  }
  if (transition.phase === 'cancelled') {
    quiesceGeneration += 1;
    quiesceTransitionId = null;
    quiesceGateRelease?.();
    quiesceGateRelease = null;
    releaseBrowserMigration(transition.id);
    pollGeneration += 1;
    phase.value = 'idle';
    error.value = null;
    return;
  }
  await moveThisTab(transition, agentUrl, token);
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
      const current = status.transition ? localTransition(status.transition) : null;
      if (current?.id === candidate.id && current.instance_id === candidate.instance_id) {
        await handleHttpsTransition(current, agentUrl, token);
      }
      return;
    } catch (e) {
      const pinned = active.value;
      if (!pinned || !sameTransitionIdentity(pinned, candidate)) return;
      if (e instanceof TlsApiError && e.status === 426) {
        // The source's explicit recovery-only response proves activation has
        // crossed the HTTP boundary. Keep polling the already-pinned target.
        await handleHttpsTransition({ ...pinned, phase: 'activating' }, agentUrl, token);
        return;
      }
      try {
        const targetStatus = await fetchTlsStatus(pinned.target_origin);
        const target = targetStatus.transition ? localTransition(targetStatus.transition) : null;
        if (targetStatus.serving_https && targetStatus.ready !== false
          && target?.phase === 'active'
          && sameTransitionIdentity(pinned, target)) {
          await handleHttpsTransition(target, agentUrl, token);
          return;
        }
      } catch { /* a restart or certificate prompt needs another attempt */ }
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
      const sourceTransition = sourceStatus.transition;
      if (sourceTransition?.id === saved.id
        && sourceTransition.instance_id === saved.instance_id) {
        await handleHttpsTransition(sourceTransition, agentUrl, token);
        return;
      }
      // A reachable source with another installation or transition makes this
      // browser record stale. Do not use its destination.
      try { localStorage.removeItem(TRANSITION_KEY); } catch { /* best effort */ }
      return;
    } catch (e) {
      sourceRecoveryOnly = e instanceof TlsApiError && e.status === 426;
    }
    try {
      const targetStatus = await fetchTlsStatus(saved.target_origin);
      const targetTransition = targetStatus.transition
        ? localTransition(targetStatus.transition)
        : null;
      const localSaved = localTransition(saved);
      if (targetStatus.serving_https && targetStatus.ready !== false
        && targetTransition?.phase === 'active'
        && sameTransitionIdentity(localSaved, targetTransition)) {
        await handleHttpsTransition(targetTransition, agentUrl, token);
        return;
      }
    } catch { /* certificate trust or restart recovery stays on this page */ }
    if (sourceRecoveryOnly || ['activating', 'active'].includes(saved.phase)) {
      await handleHttpsTransition({ ...saved, phase: 'activating' }, agentUrl, token);
      return;
    }
    phase.value = 'attention';
    error.value = 'The HTTP server is temporarily unreachable. This tab will retry before acknowledging the HTTPS switch.';
    scheduleResume();
    } finally {
      resumeRunning = false;
    }
  };
  const resumeListener = () => { void resume(); };
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

export const httpsTransitionState = {
  phase: readonly(phase),
  error: readonly(error),
  transition: readonly(active),
};
