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
 */

import { defineStore } from 'pinia';
import { computed, ref, watch } from 'vue';

import {
  openEmbeddingStateStream,
  type EmbeddingStateSnapshot,
} from '../services/embeddingStateStream';
import { subscribeEmbeddingState } from '../services/profileEventsStream';
import type { EmbeddingStatus } from '../services/configApi';
import { useSettingsStore } from './settings';

interface StreamHandle { close: () => void; }

let streamHandle: StreamHandle | null = null;
let connectedAgentUrl: string | null = null;
let connectedToken: string | null = null;

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

  const isBusy = computed(() => busy.value);
  const isReady = computed(() => ready.value);

  function applySnapshot(snap: EmbeddingStateSnapshot) {
    status.value = snap.status;
    phase.value = snap.phase ?? null;
    error.value = snap.error ?? null;
    enabled.value = !!snap.enabled;
    ready.value = !!snap.ready;
    busy.value = !!snap.busy;
  }

  const settingsStore = useSettingsStore();

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
    isBusy,
    isReady,
    connect,
    disconnect,
  };
});
