/**
 * Reactive Pinia store for the global vector embedding lifecycle state.
 *
 * One subscription per app instance: App.vue calls ``connect()`` on
 * mount; per-page components (Settings → Vector Embedding, Setup
 * Wizard) read ``status`` / ``phase`` / ``error`` reactively without
 * opening their own connections.
 *
 * The underlying SSE channel is shared across browser tabs by
 * ``createSharedStream`` (see ``services/embeddingStateStream.ts``)
 * so multiple tabs of the same origin only hold one connection.
 */

import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import {
  openEmbeddingStateStream,
  type EmbeddingStateStreamHandle,
  type EmbeddingStateSnapshot,
} from '../services/embeddingStateStream';
import type { EmbeddingStatus } from '../services/configApi';

let streamHandle: EmbeddingStateStreamHandle | null = null;
let connectedAgentUrl: string | null = null;

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

  function connect(agentUrl: string) {
    // Idempotent: if we already have a handle for this URL, do nothing.
    if (streamHandle && connectedAgentUrl === agentUrl) return;
    if (streamHandle) {
      streamHandle.close();
      streamHandle = null;
    }
    connectedAgentUrl = agentUrl;
    streamHandle = openEmbeddingStateStream(
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
  }

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
