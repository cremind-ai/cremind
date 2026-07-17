/**
 * Store for event runs — the per-trigger execution history shown on the Events
 * page (expandable child tables) and in the run-detail drawer.
 *
 * Fed by the `event-runs` frame on the admin SSE (applySnapshot, upsert) plus
 * fetch-on-demand paging for older history (loadOlder). Coordinates teardown
 * with the chat/terminal/usage stores when a run is deleted.
 */

import { defineStore } from 'pinia';
import { useSettingsStore } from './settings';
import { useChatStore } from './chat';
import { useTerminalPanelStore } from './terminalPanel';
import { useUsageStore } from './usage';
import { useTodoPanelsStore } from './todoPanels';
import {
  listEventRuns,
  getEventRun,
  deleteEventRun,
  cancelEventRun,
  type EventRun,
  type EventRunSourceKind,
} from '../services/eventRunsApi';
import type { EventRunSubscriptionSummary } from '../services/adminEventsStream';

interface State {
  runsById: Record<string, EventRun>;
  // ids in the most-recent admin snapshot (server-capped)
  snapshotIds: string[];
  // per `${source_kind}:${subscription_id}` rollups from the snapshot
  summaries: Record<string, EventRunSubscriptionSummary>;
  // older ids loaded on demand, keyed by `${source_kind}:${subscription_id}`
  olderBySub: Record<string, string[]>;
  exhausted: Record<string, boolean>;
  loadingMore: Record<string, boolean>;
  activeRunId: string | null;
}

function subKey(sourceKind: EventRunSourceKind, subscriptionId: string): string {
  return `${sourceKind}:${subscriptionId}`;
}

export const useEventRunsStore = defineStore('eventRuns', {
  state: (): State => ({
    runsById: {},
    snapshotIds: [],
    summaries: {},
    olderBySub: {},
    exhausted: {},
    loadingMore: {},
    activeRunId: null,
  }),

  getters: {
    activeRun(state): EventRun | null {
      return state.activeRunId ? state.runsById[state.activeRunId] ?? null : null;
    },

    /**
     * Runs in the most-recent admin snapshot, server order (newest first).
     * The Tasks board reads ONLY this — never all of `runsById`, which retains
     * runs the server has since pruned (it is never told about deletions) and
     * would grow an unbounded ghost set.
     */
    snapshotRuns(state): EventRun[] {
      const out: EventRun[] = [];
      for (const id of state.snapshotIds) {
        const r = state.runsById[id];
        if (r) out.push(r);
      }
      return out;
    },

    /** All known runs for a subscription (snapshot + older pages), newest first. */
    runsForSubscription: (state) => (sourceKind: EventRunSourceKind, subscriptionId: string): EventRun[] => {
      const seen = new Set<string>();
      const out: EventRun[] = [];
      const push = (id: string) => {
        const r = state.runsById[id];
        if (r && r.source_kind === sourceKind && r.subscription_id === subscriptionId && !seen.has(id)) {
          seen.add(id);
          out.push(r);
        }
      };
      state.snapshotIds.forEach(push);
      (state.olderBySub[subKey(sourceKind, subscriptionId)] ?? []).forEach(push);
      out.sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));
      return out;
    },

    summaryForSubscription: (state) => (sourceKind: EventRunSourceKind, subscriptionId: string): EventRunSubscriptionSummary | null => {
      return state.summaries[subKey(sourceKind, subscriptionId)] ?? null;
    },

    pendingCountForSubscription() {
      return (sourceKind: EventRunSourceKind, subscriptionId: string): number => {
        const s = this.summaryForSubscription(sourceKind, subscriptionId);
        return s?.pending_count ?? 0;
      };
    },

    runCountForSubscription() {
      return (sourceKind: EventRunSourceKind, subscriptionId: string): number => {
        const s = this.summaryForSubscription(sourceKind, subscriptionId);
        return s?.run_count ?? 0;
      };
    },

    isExhausted: (state) => (sourceKind: EventRunSourceKind, subscriptionId: string): boolean =>
      !!state.exhausted[subKey(sourceKind, subscriptionId)],

    isLoadingMore: (state) => (sourceKind: EventRunSourceKind, subscriptionId: string): boolean =>
      !!state.loadingMore[subKey(sourceKind, subscriptionId)],
  },

  actions: {
    /** Apply a fresh admin snapshot: upsert runs, replace snapshot id list. */
    applySnapshot(runs: EventRun[], summaries: Record<string, EventRunSubscriptionSummary>) {
      for (const r of runs) this.runsById[r.id] = r;
      this.snapshotIds = runs.map((r) => r.id);
      this.summaries = summaries || {};
    },

    async loadOlder(sourceKind: EventRunSourceKind, subscriptionId: string) {
      const key = subKey(sourceKind, subscriptionId);
      if (this.exhausted[key] || this.loadingMore[key]) return;
      this.loadingMore[key] = true;
      try {
        const settings = useSettingsStore();
        const existing = this.runsForSubscription(sourceKind, subscriptionId).length;
        const { runs, total } = await listEventRuns(settings.agentUrl, settings.authToken, {
          source_kind: sourceKind,
          subscription_id: subscriptionId,
          limit: 50,
          offset: existing,
        });
        const ids: string[] = this.olderBySub[key] ? [...this.olderBySub[key]] : [];
        for (const r of runs) {
          this.runsById[r.id] = r;
          if (!ids.includes(r.id)) ids.push(r.id);
        }
        this.olderBySub[key] = ids;
        if (existing + runs.length >= total || runs.length === 0) {
          this.exhausted[key] = true;
        }
      } catch (e) {
        console.error('Failed to load older event runs:', e);
      } finally {
        this.loadingMore[key] = false;
      }
    },

    openRun(id: string) {
      this.activeRunId = id;
    },

    /**
     * Request cancellation of a running run. The status flip arrives via the
     * next admin snapshot, so we don't optimistically mutate here.
     */
    async cancelRun(id: string): Promise<void> {
      const settings = useSettingsStore();
      await cancelEventRun(settings.agentUrl, settings.authToken, id);
    },

    /** Open a run by id, fetching it if not already in the store (deep links). */
    async openRunById(id: string) {
      if (!this.runsById[id]) {
        try {
          const settings = useSettingsStore();
          this.runsById[id] = await getEventRun(settings.agentUrl, settings.authToken, id);
        } catch (e) {
          console.error('Failed to load event run:', e);
          return;
        }
      }
      this.activeRunId = id;
    },

    closeRun() {
      this.activeRunId = null;
    },

    async removeRun(id: string) {
      const run = this.runsById[id];
      const settings = useSettingsStore();
      await deleteEventRun(settings.agentUrl, settings.authToken, id);
      // Coordinate teardown with the other stores.
      try { useTodoPanelsStore().closeForEventRun(id); } catch { /* noop */ }
      if (run?.conversation_id) {
        const cid = run.conversation_id;
        try { useChatStore().forceCloseStream(cid); } catch { /* noop */ }
        try { useTerminalPanelStore().forgetConversation(cid); } catch { /* noop */ }
        try { useUsageStore().invalidateConversation(cid); } catch { /* noop */ }
        try { useTodoPanelsStore().closeForConversation(cid); } catch { /* noop */ }
      }
      if (this.activeRunId === id) this.activeRunId = null;
      delete this.runsById[id];
      this.snapshotIds = this.snapshotIds.filter((x) => x !== id);
      for (const key of Object.keys(this.olderBySub)) {
        this.olderBySub[key] = this.olderBySub[key].filter((x) => x !== id);
      }
    },
  },
});
