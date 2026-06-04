import { defineStore } from 'pinia';
import { ref } from 'vue';
import { listProcesses, type ProcessRow } from '../services/processApi';
import {
  openProcessesStream,
  type ProcessStreamHandle,
} from '../services/processesStream';
import { useSettingsStore } from './settings';

/**
 * Process Manager list state.
 *
 * Holds only the summary list.  Per-pid xterm.js instances and WebSocket
 * connections live in the terminal view component so DOM-bound objects
 * aren't trapped in the store.
 *
 * Updates arrive via SSE (``/api/processes/stream``): the backend pushes a
 * fresh snapshot whenever a process spawns, exits, is stopped, or an
 * autostart row mutates.  The poll-every-5-seconds design that this
 * replaces flooded the server log without ever having a data change to
 * justify itself.
 */
export const useProcessManagerStore = defineStore('processManager', () => {
  const processes = ref<ProcessRow[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);

  let streamHandle: ProcessStreamHandle | null = null;

  async function refresh() {
    const settings = useSettingsStore();
    if (!settings.agentUrl || !settings.authToken) return;
    loading.value = true;
    error.value = null;
    try {
      const res = await listProcesses(settings.agentUrl, settings.authToken);
      processes.value = res.processes || [];
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    } finally {
      loading.value = false;
    }
  }

  function streamStart() {
    streamStop();
    const settings = useSettingsStore();
    if (!settings.agentUrl || !settings.authToken) return;
    loading.value = true;
    streamHandle = openProcessesStream(
      settings.agentUrl,
      settings.authToken,
      (rows) => {
        processes.value = rows;
        error.value = null;
        loading.value = false;
      },
      (e) => {
        error.value = e instanceof Error ? e.message : String(e);
        loading.value = false;
      },
    );
  }

  function streamStop() {
    if (streamHandle) {
      streamHandle.close();
      streamHandle = null;
    }
  }

  return {
    processes,
    loading,
    error,
    refresh,
    streamStart,
    streamStop,
  };
});
