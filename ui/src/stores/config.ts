/**
 * Pinia store for server configuration state.
 * Fetches and manages config from backend APIs.
 *
 * The settings-state SSE stream pings this store on every server-side
 * mutation (tool config, agent enable/disable, skill register, LLM
 * provider change, etc.). On each ping, the store refetches the
 * resources it owns so all consumers stay live without manual refresh.
 */

import { defineStore } from 'pinia';
import { ref } from 'vue';
import {
  checkSetupStatus,
  listLLMProviders,
  getModelGroups,
  listTools,
  type LLMProvider,
  type ToolStatus,
} from '../services/configApi';
import {
  openSettingsStateStream,
  type SettingsStateStreamHandle,
} from '../services/settingsStateStream';

let settingsStreamHandle: SettingsStateStreamHandle | null = null;

export const useConfigStore = defineStore('config', () => {
  const setupComplete = ref(false);
  const setupChecked = ref(false);
  const loading = ref(false);
  const error = ref<string | null>(null);

  const llmProviders = ref<LLMProvider[]>([]);
  const modelGroups = ref<Record<string, string>>({ high: '', low: '' });
  const defaultProvider = ref('groq');
  const tools = ref<ToolStatus[]>([]);

  async function checkSetup(agentUrl: string, profile?: string): Promise<boolean> {
    try {
      const result = await checkSetupStatus(agentUrl, profile);
      setupComplete.value = result.setup_complete;
      setupChecked.value = true;
      return result.setup_complete;
    } catch (e) {
      // Server unreachable - assume setup needed
      setupChecked.value = true;
      setupComplete.value = false;
      return false;
    }
  }

  async function fetchLLMProviders(agentUrl: string, token: string) {
    try {
      const result = await listLLMProviders(agentUrl, token);
      llmProviders.value = result.providers;
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to fetch providers';
    }
  }

  async function fetchModelGroups(agentUrl: string, token: string) {
    try {
      const result = await getModelGroups(agentUrl, token);
      modelGroups.value = result.model_groups;
      defaultProvider.value = result.default_provider;
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to fetch model groups';
    }
  }

  async function fetchTools(agentUrl: string, token: string) {
    try {
      const result = await listTools(agentUrl, token);
      tools.value = result.tools;
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to fetch tools';
    }
  }

  async function fetchAll(agentUrl: string, token: string) {
    loading.value = true;
    error.value = null;
    try {
      await Promise.all([
        fetchLLMProviders(agentUrl, token),
        fetchModelGroups(agentUrl, token),
        fetchTools(agentUrl, token),
      ]);
    } finally {
      loading.value = false;
    }
  }

  /**
   * Open the per-profile settings-state SSE stream. The stream is a
   * wakeup-only ping; on each ping we refetch every resource the store
   * owns so consumers (Settings page, Agents drawer) stay live.
   *
   * Idempotent — calling twice is a no-op (existing handle is reused).
   */
  function subscribeSettingsState(
    agentUrl: string,
    token: string,
    profileKey: string,
  ) {
    if (settingsStreamHandle !== null) return;
    settingsStreamHandle = openSettingsStateStream(
      agentUrl, token, profileKey,
      () => {
        fetchAll(agentUrl, token).catch(() => {});
        checkSetup(agentUrl, profileKey).catch(() => {});
      },
    );
  }

  function unsubscribeSettingsState() {
    if (settingsStreamHandle === null) return;
    try { settingsStreamHandle.close(); } catch { /* ignore */ }
    settingsStreamHandle = null;
  }

  return {
    setupComplete,
    setupChecked,
    loading,
    error,
    llmProviders,
    modelGroups,
    defaultProvider,
    tools,
    checkSetup,
    fetchLLMProviders,
    fetchModelGroups,
    fetchTools,
    fetchAll,
    subscribeSettingsState,
    unsubscribeSettingsState,
  };
});
