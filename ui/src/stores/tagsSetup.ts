import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { useSettingsStore } from './settings';
import { TagsApiError } from '../services/tagsApi';
import {
  IdempotencyKeys,
  cancelPairing as apiCancelPairing,
  cancelSetupSession,
  confirmSetupSession,
  createSetupSession,
  getConnectDownloads,
  getConnections,
  getDiscovery,
  getPairing,
  getRecovery,
  getSetupSession,
  renameSetupDevice,
  sendTestCard,
  setDevicePaused,
  setupServerUrl,
  startDiscovery as apiStartDiscovery,
  startPairing as apiStartPairing,
  startRecovery as apiStartRecovery,
  unpairDevice,
  type ConnectDownloads,
  type Discovery,
  type Pairing,
  type Recovery,
  type SetupComputer,
  type SetupDevice,
  type SetupOperation,
  type SetupSession,
  type SetupSessionOperation,
  type TagConnection,
} from '../services/tagsSetupApi';
import {
  isOperationTerminal, isSessionTerminal, pendingSetups, setupReadiness,
} from '../utils/tagsSetupFormat';

/** While a dialog follows a session or an operation. */
export const FAST_POLL_MS = 1500;
/** Otherwise (the connections list). */
export const SLOW_POLL_MS = 15_000;

/**
 * - `available`: the server offers simple setup (the page shows the hardware section);
 * - `disabled`: `simple_setup` is false, or GET /connections answered 403 `simple_setup_disabled`;
 * - `unsupported`: an older server without these endpoints (404/405).
 */
export type SetupAvailability = 'unknown' | 'available' | 'disabled' | 'unsupported';
export type FollowKind = 'session' | 'discovery' | 'pairing' | 'recovery';

const DISCOVERY_DONE = ['not_found', 'failed', 'cancelled'];

/**
 * Simple hardware setup for the signed-in profile (Settings → Tags): its
 * gateways (one Cremind Connect worker each) with their bridges and tags, the
 * computers they run on, and the setups still running on the server.
 *
 * Polling: the page runs `tick()` through useVisiblePoll at `pollIntervalMs` —
 * every 15 s for the list, every 1.5 s while a dialog `follow()`s a session,
 * discovery, pairing or recovery. A followed item that reaches its end is
 * dropped from the fast set and the list is re-read at once, so a new device
 * shows up without waiting for the slow poll.
 *
 * Profiles never mix: `reset()` clears everything when the page mounts for
 * another profile, and an answer that lands after the token changed is
 * dropped.
 */
export const useTagsSetupStore = defineStore('tagsSetup', () => {
  const settingsStore = useSettingsStore();
  const url = () => settingsStore.agentUrl;
  const token = () => settingsStore.authToken;

  const loadedFor = ref('');
  const availability = ref<SetupAvailability>('unknown');
  const loaded = ref(false);
  const loadError = ref('');
  const connections = ref<TagConnection[]>([]);
  const computers = ref<SetupComputer[]>([]);
  const activeSessions = ref<SetupSession[]>([]);
  const activeOperations = ref<SetupOperation[]>([]);
  const downloads = ref<ConnectDownloads | null>(null);

  // The latest answer for everything a dialog follows or created, by id.
  const sessions = ref<Record<string, SetupSession>>({});
  const discoveries = ref<Record<string, Discovery>>({});
  const pairings = ref<Record<string, Pairing>>({});
  const recoveries = ref<Record<string, Recovery>>({});
  /** Followed items the server no longer knows: id → `expired` | `not_found`. */
  const lost = ref<Record<string, 'expired' | 'not_found'>>({});
  const following = ref<string[]>([]);

  let keys = new IdempotencyKeys();
  let lastListAt = 0;
  let listSoon = false;
  let pollTrigger: (() => void) | null = null;

  const pollIntervalMs = computed(() => (following.value.length ? FAST_POLL_MS : SLOW_POLL_MS));
  const readiness = computed(() => setupReadiness(connections.value));
  const pending = computed(() => pendingSetups(activeSessions.value, activeOperations.value));
  const bridges = computed(() => connections.value.flatMap((c) => c.bridges.map((device) => ({ device, connection: c }))));
  const tags = computed(() => connections.value.flatMap((c) => c.tags.map((device) => ({ device, connection: c }))));

  function reset(profile: string) {
    if (loadedFor.value === profile) return;
    loadedFor.value = profile;
    availability.value = 'unknown';
    loaded.value = false;
    loadError.value = '';
    connections.value = [];
    computers.value = [];
    activeSessions.value = [];
    activeOperations.value = [];
    downloads.value = null;
    sessions.value = {};
    discoveries.value = {};
    pairings.value = {};
    recoveries.value = {};
    lost.value = {};
    following.value = [];
    keys = new IdempotencyKeys();
    lastListAt = 0;
    listSoon = false;
  }

  // ── the list ──

  /** Never throws: a failure is kept in `loadError` (and the last list stays). */
  async function loadConnections(): Promise<void> {
    const tok = token();
    if (!tok) return;
    try {
      const answer = await getConnections(url(), tok);
      if (token() !== tok) return;
      lastListAt = Date.now();
      loaded.value = true;
      loadError.value = '';
      availability.value = answer.simple_setup ? 'available' : 'disabled';
      connections.value = answer.simple_setup ? answer.connections ?? [] : [];
      computers.value = answer.computers ?? [];
      activeSessions.value = answer.active?.sessions ?? [];
      activeOperations.value = answer.active?.operations ?? [];
    } catch (e) {
      if (token() !== tok) return;
      if (e instanceof TagsApiError && e.status === 403 && e.code === 'simple_setup_disabled') {
        availability.value = 'disabled';
        loaded.value = true;
        return;
      }
      if (e instanceof TagsApiError && (e.status === 404 || e.status === 405) && !e.code
          && availability.value === 'unknown') {
        availability.value = 'unsupported';
        loaded.value = true;
        return;
      }
      loadError.value = e instanceof Error ? e.message : 'Could not load your devices';
    }
  }

  /** One read of a discovery (to learn its role before resuming it). */
  async function loadDiscovery(id: string): Promise<Discovery> {
    const tok = token();
    const d = await getDiscovery(url(), tok, id);
    if (token() === tok) discoveries.value[id] = d;
    return d;
  }

  async function loadDownloads(): Promise<ConnectDownloads | null> {
    if (downloads.value) return downloads.value;
    const tok = token();
    const answer = await getConnectDownloads(url(), tok);
    if (token() === tok) downloads.value = answer;
    return answer;
  }

  // ── following (fast polling while a dialog waits) ──

  const followKey = (kind: FollowKind, id: string) => `${kind}:${id}`;

  function follow(kind: FollowKind, id: string) {
    const key = followKey(kind, id);
    if (following.value.includes(key)) return;
    seed(kind, id);
    following.value = [...following.value, key];
    const { [id]: _gone, ...rest } = lost.value;
    lost.value = rest;
    pollTrigger?.();
  }

  /** A setup resumed from the list shows its last known state at once. */
  function seed(kind: FollowKind, id: string) {
    if (kind === 'session' && !sessions.value[id]) {
      const s = activeSessions.value.find((x) => x.id === id);
      if (s) sessions.value[id] = s;
      return;
    }
    const op = activeOperations.value.find((x) => x.id === id);
    if (!op) return;
    if (kind === 'pairing' && !pairings.value[id]) {
      pairings.value[id] = { ...op, role: op.kind === 'pair_bridge' ? 'bridge' : 'tag', first_tag: false };
    } else if (kind === 'recovery' && !recoveries.value[id]) {
      recoveries.value[id] = { ...op, companion_id: '', devices: [] };
    }
  }

  function unfollow(kind: FollowKind, id: string) {
    const key = followKey(kind, id);
    if (following.value.includes(key)) following.value = following.value.filter((k) => k !== key);
  }

  function isFollowing(kind: FollowKind, id: string): boolean {
    return following.value.includes(followKey(kind, id));
  }

  /** The page's useVisiblePoll trigger: following something polls at once. */
  function setPollTrigger(fn: (() => void) | null) {
    pollTrigger = fn;
  }

  function settled(kind: FollowKind, id: string) {
    unfollow(kind, id);
    listSoon = true;
  }

  async function pollOne(key: string, tok: string): Promise<void> {
    const at = key.indexOf(':');
    const kind = key.slice(0, at) as FollowKind;
    const id = key.slice(at + 1);
    try {
      if (kind === 'session') {
        const s = await getSetupSession(url(), tok, id);
        if (token() !== tok) return;
        sessions.value[id] = s;
        if (isSessionTerminal(s.state)) settled(kind, id);
      } else if (kind === 'discovery') {
        const d = await getDiscovery(url(), tok, id);
        if (token() !== tok) return;
        discoveries.value[id] = d;
        if (DISCOVERY_DONE.includes(d.state)) settled(kind, id);
      } else if (kind === 'pairing') {
        const p = await getPairing(url(), tok, id);
        if (token() !== tok) return;
        pairings.value[id] = p;
        if (isOperationTerminal(p.state)) settled(kind, id);
      } else {
        const r = await getRecovery(url(), tok, id);
        if (token() !== tok) return;
        recoveries.value[id] = r;
        if (isOperationTerminal(r.state)) settled(kind, id);
      }
    } catch (e) {
      if (token() !== tok) return;
      if (e instanceof TagsApiError && (e.status === 404 || e.status === 410)) {
        const why = e.status === 410 ? 'expired' : 'not_found';
        lost.value[id] = why;
        if (kind === 'session' && why === 'expired' && sessions.value[id]) {
          sessions.value[id] = { ...sessions.value[id], state: 'expired' };
        }
        settled(kind, id);
      }
      // Anything else is transient: keep following.
    }
  }

  /** One poll round: the followed items, and the list when it is due. */
  async function tick(): Promise<void> {
    const tok = token();
    if (!tok || availability.value === 'disabled' || availability.value === 'unsupported') return;
    const keysNow = [...following.value];
    const listDue = !keysNow.length || listSoon || Date.now() - lastListAt >= SLOW_POLL_MS - 250;
    listSoon = false;
    await Promise.allSettled([
      ...keysNow.map((key) => pollOne(key, tok)),
      ...(listDue ? [loadConnections()] : []),
    ]);
    if (listSoon && token() === tok) {
      listSoon = false;
      await loadConnections();
    }
  }

  // ── mutations (each with its Idempotency-Key) ──

  async function startSession(operation: SetupSessionOperation, companionId?: string) {
    const tok = token();
    const body = {
      operation,
      server_url: setupServerUrl(url()),
      ...(companionId ? { companion_id: companionId } : {}),
    };
    const res = await keys.run(`session:${operation}:${companionId ?? ''}`, body,
      (key) => createSetupSession(url(), tok, body, key));
    if (token() === tok) sessions.value[res.session.id] = res.session;
    return { session: res.session, launchUrl: res.launch_url };
  }

  async function confirmSession(id: string): Promise<SetupSession> {
    const tok = token();
    const s = await keys.run(`confirm:${id}`, {}, (key) => confirmSetupSession(url(), tok, id, key));
    if (token() === tok) sessions.value[id] = s;
    return s;
  }

  async function cancelSession(id: string): Promise<SetupSession> {
    const tok = token();
    const s = await keys.run(`cancel-session:${id}`, {}, (key) => cancelSetupSession(url(), tok, id, key));
    if (token() === tok) {
      sessions.value[id] = s;
      if (isSessionTerminal(s.state)) {
        settled('session', id);
        void loadConnections();
      }
    }
    return s;
  }

  async function startDiscovery(role: 'bridge' | 'tag', setupCode: string, gatewayId?: string): Promise<Discovery> {
    const tok = token();
    const body = { role, setup_code: setupCode, ...(gatewayId ? { gateway_id: gatewayId } : {}) };
    const d = await keys.run(`discovery:${role}`, body, (key) => apiStartDiscovery(url(), tok, body, key));
    if (token() === tok) discoveries.value[d.id] = d;
    return d;
  }

  async function startPairing(discoveryId: string, candidateId: string, name?: string): Promise<Pairing> {
    const tok = token();
    const clean = (name ?? '').trim();
    const body = { discovery_id: discoveryId, candidate_id: candidateId, ...(clean ? { name: clean } : {}) };
    const p = await keys.run(`pairing:${discoveryId}`, body, (key) => apiStartPairing(url(), tok, body, key));
    if (token() === tok) pairings.value[p.id] = p;
    return p;
  }

  /** Cancel and reconcile: the answer may still be running (a pairing that
   *  may have committed on the device is checked first), so it stays followed
   *  until it ends — kept, or cancelled. */
  async function cancelPairing(id: string): Promise<Pairing> {
    const tok = token();
    const p = await keys.run(`cancel-pairing:${id}`, {}, (key) => apiCancelPairing(url(), tok, id, key));
    if (token() === tok) {
      pairings.value[id] = p;
      if (isOperationTerminal(p.state)) {
        settled('pairing', id);
        void loadConnections();
      }
    }
    return p;
  }

  async function startRecovery(companionId: string) {
    const tok = token();
    const body = { companion_id: companionId, server_url: setupServerUrl(url()) };
    const res = await keys.run(`recovery:${companionId}`, body, (key) => apiStartRecovery(url(), tok, body, key));
    if (token() === tok) {
      recoveries.value[res.recovery.id] = res.recovery;
      sessions.value[res.session.id] = res.session;
    }
    return { recovery: res.recovery, session: res.session, launchUrl: res.launch_url };
  }

  async function unpair(deviceId: string) {
    const tok = token();
    const res = await keys.run(`unpair:${deviceId}`, {}, (key) => unpairDevice(url(), tok, deviceId, key));
    if (token() === tok) void loadConnections();
    return res;
  }

  async function setPaused(deviceId: string, paused: boolean): Promise<SetupDevice> {
    const tok = token();
    const device = await keys.run(`pause:${deviceId}`, { paused },
      (key) => setDevicePaused(url(), tok, deviceId, paused, key));
    if (token() === tok) void loadConnections();
    return device;
  }

  async function sendTest(deviceId: string) {
    const tok = token();
    return keys.run(`test:${deviceId}`, {}, (key) => sendTestCard(url(), tok, deviceId, key));
  }

  async function rename(deviceId: string, name: string) {
    const tok = token();
    const body = { name: name.trim() };
    const device = await keys.run(`rename:${deviceId}`, body,
      (key) => renameSetupDevice(url(), tok, deviceId, body.name, key));
    if (token() === tok) void loadConnections();
    return device;
  }

  return {
    availability,
    loaded,
    loadError,
    connections,
    computers,
    activeSessions,
    activeOperations,
    downloads,
    sessions,
    discoveries,
    pairings,
    recoveries,
    lost,
    following,
    pollIntervalMs,
    readiness,
    pending,
    bridges,
    tags,
    reset,
    loadConnections,
    loadDiscovery,
    loadDownloads,
    follow,
    unfollow,
    isFollowing,
    setPollTrigger,
    tick,
    startSession,
    confirmSession,
    cancelSession,
    startDiscovery,
    startPairing,
    cancelPairing,
    startRecovery,
    unpair,
    setPaused,
    sendTest,
    rename,
  };
});
