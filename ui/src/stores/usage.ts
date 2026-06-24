import { defineStore } from 'pinia';
import { ref } from 'vue';
import { useSettingsStore } from './settings';
import {
  fetchUsageSummary,
  fetchConversationUsage,
  type UsageSummary,
  type UsageQuery,
  type ConversationUsage,
} from '../services/usageApi';

/**
 * Caches the dashboard summary and per-conversation usage. The conversation
 * cache lets the in-chat chip's "expand" and the usage panel share one fetch,
 * so a second expand is instant. Cleared on profile switch by the caller.
 */
export const useUsageStore = defineStore('usage', () => {
  const settings = useSettingsStore();

  const summary = ref<UsageSummary | null>(null);
  const summaryLoading = ref(false);
  const summaryError = ref<string | null>(null);

  const conversationUsage = ref<Record<string, ConversationUsage>>({});
  const conversationLoading = ref<Record<string, boolean>>({});
  // Coalesce concurrent loads (many message chips mount at once) into one fetch.
  const inflight: Record<string, Promise<ConversationUsage | null> | undefined> = {};

  async function loadSummary(query: UsageQuery = {}): Promise<void> {
    summaryLoading.value = true;
    summaryError.value = null;
    try {
      summary.value = await fetchUsageSummary(
        settings.agentUrl, settings.authToken, query,
      );
    } catch (e: any) {
      summaryError.value = e?.message || 'Failed to load usage summary';
    } finally {
      summaryLoading.value = false;
    }
  }

  async function loadConversationUsage(
    conversationId: string, force = false,
  ): Promise<ConversationUsage | null> {
    if (!conversationId) return null;
    if (!force && conversationUsage.value[conversationId]) {
      return conversationUsage.value[conversationId];
    }
    const pending = inflight[conversationId];
    if (!force && pending) return pending;

    conversationLoading.value = { ...conversationLoading.value, [conversationId]: true };
    const p = (async () => {
      try {
        const data = await fetchConversationUsage(
          settings.agentUrl, settings.authToken, conversationId,
        );
        conversationUsage.value = { ...conversationUsage.value, [conversationId]: data };
        return data;
      } catch {
        return null;
      } finally {
        conversationLoading.value = { ...conversationLoading.value, [conversationId]: false };
        delete inflight[conversationId];
      }
    })();
    inflight[conversationId] = p;
    return p;
  }

  /** Drop a cached conversation rollup (e.g. after a new turn completes). */
  function invalidateConversation(conversationId: string): void {
    if (conversationUsage.value[conversationId]) {
      const next = { ...conversationUsage.value };
      delete next[conversationId];
      conversationUsage.value = next;
    }
  }

  return {
    summary,
    summaryLoading,
    summaryError,
    conversationUsage,
    conversationLoading,
    loadSummary,
    loadConversationUsage,
    invalidateConversation,
  };
});
