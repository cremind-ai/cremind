/**
 * Reactive Pinia store for the global vector embedding lifecycle state.
 *
 * One subscription per app instance: App.vue calls ``connect()`` on
 * mount; per-page components (Settings → Vector Embedding, Setup
 * Wizard) read ``status`` / ``phase`` / ``error`` reactively without
 * opening their own connections.
 *
 * Source selection is auth-aware. When a profile token is present the
 * store rides the multiplexed ``/api/profile-events/stream`` connection
 * (one socket for everything), keeping us under Chrome's HTTP/1.1
 * 6-per-origin cap. Before login — the pre-token setup wizard — it falls
 * back to the standalone, intentionally-unauthenticated
 * ``/api/config/embedding/stream``. A watcher on the token transparently
 * swaps sources on login / logout / profile switch. Both sources are
 * shared across browser tabs by ``createSharedStream``.
 *
 * ``enabled`` also decides whether Settings → My Documents exists at all (it
 * needs Vector Embedding on). It reads false until the first snapshot lands,
 * so anything that hides or redirects on it waits for ``known`` — or asks
 * ``whenKnown()`` — instead of trusting the default.
 */

import { defineStore } from 'pinia';
import { computed, ref, watch } from 'vue';

import {
  openEmbeddingStateStream,
  type EmbeddingStateSnapshot,
} from '../services/embeddingStateStream';
import { subscribeEmbeddingState } from '../services/profileEventsStream';
import { getEmbeddingStatus, type EmbeddingStatus } from '../services/configApi';
import { useSettingsStore } from './settings';

interface StreamHandle { close: () => void; }

/** How long ``whenKnown`` waits by default before giving up (ms). */
export const EMBEDDING_STATE_WAIT_MS = 2500;

let streamHandle: StreamHandle | null = null;
let connectedAgentUrl: string | null = null;
let connectedToken: string | null = null;
// One status request at a time, shared by every ``whenKnown`` caller.
let statusRequest: Promise<void> | null = null;

export const useEmbeddingStatusStore = defineStore('embeddingStatus', () => {
  // Default to 'disabled' — the first SSE frame will overwrite within
  // milliseconds. We avoid a "loading…" intermediate state because the
  // stream connects fast enough that any flicker would be noise.
  const status = ref<EmbeddingStatus>('disabled');
  const phase = ref<string | null>(null);
  const error = ref<string | null>(null);
  const enabled = ref(false);
  const ready = ref(false);
  const busy = ref(false);
  /** A snapshot has landed: until then the fields above are defaults, and
   *  ``enabled = false`` means "not known yet", not "off". Server-wide state,
   *  so a later token swap keeps it. */
  const known = ref(false);

  const isBusy = computed(() => busy.value);
  const isReady = computed(() => ready.value);

  function applySnapshot(snap: EmbeddingStateSnapshot) {
    status.value = snap.status;
    phase.value = snap.phase ?? null;
    error.value = snap.error ?? null;
    enabled.value = !!snap.enabled;
    ready.value = !!snap.ready;
    busy.value = !!snap.busy;
    known.value = true;
  }

  const settingsStore = useSettingsStore();

  /** Ask ``GET /api/config/embedding/status`` once. Its answer is used only
   *  while no stream frame has landed: a frame that beat it is newer. */
  function requestStatus(agentUrl: string): Promise<void> {
    statusRequest ??= getEmbeddingStatus(agentUrl)
      .then((res) => {
        if (known.value) return;
        applySnapshot({
          status: res.status,
          phase: res.phase ?? null,
          error: res.error ?? null,
          ready: !!res.ready,
          busy: !!res.busy,
          enabled: !!res.enabled,
        });
      })
      .catch(() => {
        // The stream may still answer before the caller gives up.
      })
      .finally(() => { statusRequest = null; });
    return statusRequest;
  }

  /**
   * Whether Vector Embedding is on, as soon as the server has said so.
   *
   * At once when a snapshot has landed. Otherwise — a fresh load, where the
   * router's first navigation runs before App.vue mounts and connects the
   * stream — it also asks the (unauthenticated) status endpoint and takes
   * whichever answers first. Null when neither answered within ``timeoutMs``:
   * a caller must then not act as if embedding were off.
   */
  function whenKnown(timeoutMs = EMBEDDING_STATE_WAIT_MS): Promise<boolean | null> {
    if (known.value) return Promise.resolve(enabled.value);
    return new Promise<boolean | null>((resolve) => {
      let settled = false;
      let timer: ReturnType<typeof setTimeout> | null = null;
      const stop = watch(known, (now) => { if (now) finish(enabled.value); }, { flush: 'sync' });
      function finish(value: boolean | null) {
        if (settled) return;
        settled = true;
        stop();
        if (timer !== null) clearTimeout(timer);
        resolve(value);
      }
      timer = setTimeout(() => finish(null), timeoutMs);
      const agentUrl = connectedAgentUrl ?? settingsStore.agentUrl;
      if (agentUrl) void requestStatus(agentUrl);
    });
  }

  function connect(agentUrl: string) {
    const token = settingsStore.authToken;
    // Idempotent: same URL AND same token → nothing to do. The token is
    // part of the key because it selects the source (multiplexed vs
    // standalone) and, for the multiplexed source, which per-profile
    // connection we ride.
    if (streamHandle && connectedAgentUrl === agentUrl && connectedToken === token) return;
    if (streamHandle) {
      streamHandle.close();
      streamHandle = null;
    }
    connectedAgentUrl = agentUrl;
    connectedToken = token;
    streamHandle = token
      ? subscribeEmbeddingState(agentUrl, token, applySnapshot)
      : openEmbeddingStateStream(
          agentUrl,
          applySnapshot,
          (err) => {
            console.warn('[embeddingStatus] stream error:', err);
          },
        );
  }

  function disconnect() {
    if (streamHandle) {
      streamHandle.close();
      streamHandle = null;
    }
    connectedAgentUrl = null;
    connectedToken = null;
  }

  // Swap sources when the token changes (login → multiplexed, logout →
  // standalone, profile switch → the new token's connection). `connect`
  // is a no-op when nothing actually changed.
  watch(
    () => settingsStore.authToken,
    () => {
      if (connectedAgentUrl !== null) connect(connectedAgentUrl);
    },
  );

  return {
    status,
    phase,
    error,
    enabled,
    ready,
    busy,
    known,
    isBusy,
    isReady,
    connect,
    disconnect,
    whenKnown,
  };
});
