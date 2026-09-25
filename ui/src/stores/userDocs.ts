/**
 * User Document Search progress for the signed-in profile: the latest
 * snapshot, what it means, and the one connection that keeps it fresh.
 *
 * App.vue calls `connect()` once and reports every route change through
 * `setRoute()`; Settings → My Documents calls `attachPage()` while mounted.
 * From those the store picks its transport (see `chooseTransport`):
 *
 * - chat routes ride the `userdocs` topic of the multiplexed profile-events
 *   SSE, which those routes already hold open;
 * - My Documents opens the standalone `/api/userdocs/stream`;
 * - every other profile page polls `/api/userdocs/status` every 15 s — and
 *   only while the feature is on, so a profile that never uses it pays for
 *   one request per page load, not a connection.
 *
 * The profile-events SSE is deliberately not opened off the chat routes: each
 * origin gets ~6 HTTP/1.1 connections, and those pages' own streams already
 * compete for them (App.vue, `handleProfileNavigation`).
 *
 * Profile isolation: a token change (logout, profile switch) drops the held
 * snapshot and every connection at once, and answers that were in flight for
 * the previous token are discarded when they land.
 */

import { defineStore } from 'pinia';
import { computed, ref, shallowRef, watch } from 'vue';

import { getUserDocsStatus, type UserDocsSettings, type UserDocsSnapshot } from '../services/userdocsApi';
import { openUserDocsStream } from '../services/userdocsStream';
import { subscribeUserDocs } from '../services/profileEventsStream';
import { CHAT_ROUTES, PROFILE_ROUTES } from '../router/profileRoutes';
import { stateBanner } from '../utils/userdocsView';
import {
  EMPTY_STREAM_STATE,
  chooseTransport,
  isActive as snapshotIsActive,
  needsAttention as snapshotNeedsAttention,
  reduceSnapshot,
  syncProgress,
  type UserDocsStreamState,
  type UserDocsTransport,
} from './userDocsReducer';
import { useSettingsStore } from './settings';

export const POLL_INTERVAL_MS = 15_000;

/** Settings → My Documents (router/index.ts). */
const MY_DOCUMENTS_ROUTE = 'user-documents-settings';

export const useUserDocsStore = defineStore('userDocs', () => {
  const settingsStore = useSettingsStore();

  const stream = shallowRef<UserDocsStreamState>(EMPTY_STREAM_STATE);
  /** The profile's saved settings, once My Documents has loaded them. Only
   *  used to word the banner (folder names, admin-only links). */
  const settings = shallowRef<UserDocsSettings | null>(null);
  const transport = ref<UserDocsTransport>('none');

  const routeName = ref('');
  const pageMounts = ref(0);

  const snapshot = computed(() => stream.value.snapshot);
  const isActive = computed(() => snapshotIsActive(snapshot.value));
  const needsAttention = computed(() => snapshotNeedsAttention(snapshot.value));
  const progress = computed(() => syncProgress(snapshot.value));
  const progressPct = computed(() => progress.value.pct);
  const banner = computed(() => (snapshot.value ? stateBanner(snapshot.value, settings.value) : null));

  let agentUrl: string | null = null;
  // The token the current connection (and held snapshot) belongs to.
  let boundToken = '';
  // Bumped whenever the connection is torn down, so late callbacks from a
  // closed one (an in-flight poll, a frame already queued) are ignored.
  let generation = 0;
  let handle: { close: () => void } | null = null;
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  /** Fold a snapshot in — from any source; stale ones are ignored. */
  function applySnapshot(next: UserDocsSnapshot | null | undefined) {
    stream.value = reduceSnapshot(stream.value, next);
  }

  function setSettings(next: UserDocsSettings | null) {
    settings.value = next;
  }

  function stopTransport() {
    generation += 1;
    handle?.close();
    handle = null;
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    transport.value = 'none';
  }

  async function pollOnce(gen: number, url: string, token: string) {
    // A hidden tab has nobody to show the chip to; the next visible tick
    // catches up (snapshots are complete, nothing accumulates).
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    try {
      const snap = await getUserDocsStatus(url, token);
      if (gen === generation) applySnapshot(snap);
    } catch (e) {
      // Transient (restart, network): the next tick retries. A 401 is
      // handled globally by the fetch interceptor.
      console.debug('[userDocs] status poll failed:', e);
    }
  }

  function startTransport(kind: UserDocsTransport, url: string, token: string) {
    const gen = generation;
    const onSnapshot = (snap: UserDocsSnapshot) => {
      if (gen === generation) applySnapshot(snap);
    };
    transport.value = kind;
    if (kind === 'profile-events') {
      handle = subscribeUserDocs(url, token, onSnapshot);
    } else if (kind === 'page-stream') {
      handle = openUserDocsStream(url, token, onSnapshot, (err) => {
        console.warn('[userDocs] stream error:', err);
      });
    } else if (kind === 'poll') {
      void pollOnce(gen, url, token);
      pollTimer = setInterval(() => { void pollOnce(gen, url, token); }, POLL_INTERVAL_MS);
    }
  }

  /** Bring the connection in line with the token, the route and the page. */
  function reconcile() {
    const token = settingsStore.authToken || '';
    if (token !== boundToken) {
      // Another profile (or none): nothing of the previous one may linger.
      stopTransport();
      stream.value = EMPTY_STREAM_STATE;
      settings.value = null;
      boundToken = token;
    }
    const desired: UserDocsTransport = agentUrl === null ? 'none' : chooseTransport({
      hasToken: !!token,
      profileRoute: PROFILE_ROUTES.has(routeName.value),
      chatRoute: CHAT_ROUTES.has(routeName.value),
      // The route counts as well as the mount: the route change is reported
      // before the page mounts (and before the old one unmounts), and without
      // it every navigation to or from My Documents would briefly fall back to
      // polling and spend a request on nothing.
      pageMounted: pageMounts.value > 0 || routeName.value === MY_DOCUMENTS_ROUTE,
      enabled: snapshot.value ? !!snapshot.value.enabled : null,
    });
    if (desired === transport.value) return;
    stopTransport();
    if (desired !== 'none' && agentUrl !== null) startTransport(desired, agentUrl, token);
  }

  function connect(url: string) {
    agentUrl = url;
    reconcile();
  }

  function disconnect() {
    stopTransport();
    agentUrl = null;
  }

  function setRoute(name: string | null | undefined) {
    routeName.value = name ?? '';
    reconcile();
  }

  /** My Documents is mounted; returns the matching detach. */
  function attachPage(): () => void {
    pageMounts.value += 1;
    reconcile();
    let detached = false;
    return () => {
      if (detached) return;
      detached = true;
      pageMounts.value = Math.max(0, pageMounts.value - 1);
      reconcile();
    };
  }

  /** Fetch a snapshot now (after an action the page wants reflected at once). */
  async function refresh(): Promise<void> {
    const token = settingsStore.authToken || '';
    if (!agentUrl || !token) return;
    const gen = generation;
    try {
      const snap = await getUserDocsStatus(agentUrl, token);
      if (gen === generation) applySnapshot(snap);
    } catch (e) {
      console.debug('[userDocs] refresh failed:', e);
    }
  }

  watch(() => settingsStore.authToken, reconcile);
  // Turning the feature on or off moves "other pages" between polling and
  // nothing.
  watch(() => snapshot.value?.enabled, reconcile);

  return {
    snapshot,
    settings,
    transport,
    isActive,
    needsAttention,
    progress,
    progressPct,
    banner,
    applySnapshot,
    setSettings,
    connect,
    disconnect,
    setRoute,
    attachPage,
    refresh,
  };
});
