/**
 * The composer's search-tool selection, per conversation and per group room.
 *
 * One store behind every Search tools control — the two-party chat, the
 * event-run drawer's mini chat and a group room — so a change saved in one place
 * is what the others show, and the composer can wait for a save before it sends.
 *
 * What it holds, per profile and target:
 *
 * - `state` — the last `SearchToolsState` the server confirmed. Rows are only
 *   ever rendered from it: Documentation search appears exactly when a response
 *   says it is offered, never on an assumption.
 * - `pending` — a selection the person asked for that the server has not
 *   confirmed yet (the optimistic toggle). The control shows it at once; a
 *   failed save drops it, which reverts the checkbox; a 409 drops it and adopts
 *   the state the conflict carried.
 *
 * Saves are compare-and-swap on `version`, so they are strictly sequential per
 * target and COALESCED: toggling three boxes quickly sends at most one request
 * in flight plus one with the latest selection, each against the version the
 * previous answer returned — never two racing PUTs with the same stale version.
 *
 * A brand-new chat has no conversation to save to. Its toggles live in a local
 * draft (per profile) that the chat store hands to `POST /api/conversations`
 * when the first message or first attachment upload creates the conversation,
 * so the choice lands atomically with the row and the first response already
 * runs on it.
 *
 * Profiles are independent tenants: every entry and draft is keyed by the
 * profile it was read for, a response that returns after a profile switch is
 * dropped (`epoch`), and `resetForProfileSwitch` clears the lot.
 */
import { defineStore } from 'pinia';
import { useSettingsStore } from './settings';
import {
  SEARCH_TOOL_IDS,
  SearchToolsConflictError,
  fetchNewChatSearchTools,
  fetchSearchTools,
  orderSelection,
  saveSearchTools,
  type SearchToolId,
  type SearchToolsState,
} from '../services/searchToolsApi';

/** Mirrors `PENDING_NOTICE` in app/agent/search_tools.py. */
export const PENDING_NOTICE =
  'Saved for the next response. The current response keeps its existing search tools.';

const CONFLICT_FALLBACK = 'Search tools were changed elsewhere; showing the current choice.';

/** Where a composer's selection lives. `new` is the unsaved new-chat slot. */
export type SearchToolsTarget =
  | { kind: 'new' }
  | { kind: 'conversation'; id: string }
  | { kind: 'group'; id: string };

export const NEW_CHAT_TARGET: SearchToolsTarget = Object.freeze({ kind: 'new' }) as SearchToolsTarget;

export function searchToolsKey(target: SearchToolsTarget): string {
  return target.kind === 'new' ? 'new' : `${target.kind}:${target.id}`;
}

/**
 * Which selection a chat composer edits.
 *
 * `explicitId` is the composer's own `conversationId` prop: `undefined` for the
 * main chat (follow the active conversation, or the new-chat slot), a string
 * for an embedded composer such as the event-run drawer — which must edit the
 * RUN's conversation, never whatever chat happens to be open behind it — and
 * `null` for an embedded composer with no conversation yet (no control at all).
 */
export function composerSearchToolsTarget(
  explicitId: string | null | undefined,
  activeConversationId: string | null,
): SearchToolsTarget | null {
  if (explicitId === null) return null;
  if (explicitId) return { kind: 'conversation', id: explicitId };
  return activeConversationId ? { kind: 'conversation', id: activeConversationId } : NEW_CHAT_TARGET;
}

interface PendingSelection {
  /** `null` = the defaults (Reset). */
  enabled: SearchToolId[] | null;
  seq: number;
}

export interface SearchToolsEntry {
  state: SearchToolsState | null;
  loading: boolean;
  loadError: string | null;
  /** Bumped per GET so only the newest answer lands. */
  loadSeq: number;
  pending: PendingSelection | null;
  pendingSeq: number;
  saving: boolean;
  /** The last save failed and was reverted. */
  error: string | null;
  /** The last save hit a stale version; the state shown is the other writer's. */
  conflict: string | null;
  /** Whether a response was running when the latest change was asked for. */
  requestedWhileRunning: boolean;
  /** Show PENDING_NOTICE: saved while a response was running, not adopted yet. */
  pendingNotice: boolean;
  /** Highest version a stream frame announced. */
  remoteVersion: number | null;
}

function makeEntry(): SearchToolsEntry {
  return {
    state: null,
    loading: false,
    loadError: null,
    loadSeq: 0,
    pending: null,
    pendingSeq: 0,
    saving: false,
    error: null,
    conflict: null,
    requestedWhileRunning: false,
    pendingNotice: false,
    remoteVersion: null,
  };
}

export interface SearchToolRowView {
  id: SearchToolId;
  /** 1-based position among the rows shown (the fixed priority order). */
  rank: number;
  label: string;
  description: string;
  checked: boolean;
  available: boolean;
  /** Why the row is disabled. */
  reason: string | null;
  /** An informational note on an AVAILABLE row (group rooms). */
  note: string | null;
}

export interface SearchToolsView {
  /** Rows exist only once the server answered for this target. */
  loaded: boolean;
  loading: boolean;
  loadError: string | null;
  rows: SearchToolRowView[];
  /** The selection shown (optimistic while a save is in flight). */
  enabled: SearchToolId[];
  /** Checked sources that are available — the button's numerator. */
  enabledCount: number;
  availableCount: number;
  /** Showing the defaults already: Reset would change nothing. */
  isDefault: boolean;
  pendingNextResponse: boolean;
  showPendingNotice: boolean;
  cacheWarning: string | null;
  saving: boolean;
  error: string | null;
  conflict: string | null;
}

const EMPTY_VIEW: SearchToolsView = Object.freeze({
  loaded: false,
  loading: false,
  loadError: null,
  rows: [],
  enabled: [],
  enabledCount: 0,
  availableCount: 0,
  isDefault: true,
  pendingNextResponse: false,
  showPendingNotice: false,
  cacheWarning: null,
  saving: false,
  error: null,
  conflict: null,
}) as SearchToolsView;

function entryKey(profile: string, target: SearchToolsTarget): string {
  return `${profile}|${searchToolsKey(target)}`;
}

/** Both mean "every source": the server stores all four as the default. */
function sameSelection(a: readonly SearchToolId[] | null, b: readonly SearchToolId[] | null): boolean {
  const left = a === null ? SEARCH_TOOL_IDS : orderSelection(a);
  const right = b === null ? SEARCH_TOOL_IDS : orderSelection(b);
  return left.length === right.length && left.every((id, i) => id === right[i]);
}

function isAllSources(ids: readonly SearchToolId[]): boolean {
  return sameSelection(ids, null);
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

// In-flight save loops, one per target. Outside the Pinia state: a promise is
// plumbing, not something to render. Keyed by the store instance and its
// epoch too, so a loop from before a profile switch (or from another Pinia, in
// tests) is never mistaken for the current one.
const flushes = new Map<string, Promise<boolean>>();
let instances = 0;

export const useSearchToolsStore = defineStore('searchTools', {
  state: () => ({
    instance: ++instances,
    /** Bumped by every reset: late answers from before it are dropped. */
    epoch: 0,
    /** `${profile}|${target key}` → entry. */
    entries: {} as Record<string, SearchToolsEntry>,
    /** profile → the new-chat draft (`null`/absent = the defaults). */
    drafts: {} as Record<string, SearchToolId[] | null>,
  }),

  getters: {
    /** The render model for one control. Pure: never starts a request. */
    viewFor(state): (target: SearchToolsTarget | null) => SearchToolsView {
      return (target: SearchToolsTarget | null) => {
        const profile = useSettingsStore().profileId;
        if (!target || !profile) return EMPTY_VIEW;
        const entry = state.entries[entryKey(profile, target)];
        if (!entry) return EMPTY_VIEW;
        const current = entry.state;
        if (!current) {
          return { ...EMPTY_VIEW, loading: entry.loading, loadError: entry.loadError };
        }
        let enabled: SearchToolId[] = current.enabled;
        if (target.kind === 'new') {
          const draft = state.drafts[profile];
          if (draft !== undefined) enabled = draft === null ? [...SEARCH_TOOL_IDS] : draft;
        } else if (entry.pending) {
          enabled = entry.pending.enabled === null ? [...SEARCH_TOOL_IDS] : entry.pending.enabled;
        }
        const chosen = new Set(enabled);
        const rows = current.tools.map((row, index): SearchToolRowView => ({
          id: row.id,
          rank: index + 1,
          label: row.label,
          description: row.description,
          checked: chosen.has(row.id),
          available: row.available,
          reason: row.available ? null : row.unavailable_reason,
          note: row.available ? row.unavailable_reason : null,
        }));
        const pendingNext = target.kind !== 'new' && current.pending_next_response;
        return {
          loaded: true,
          loading: entry.loading,
          loadError: entry.loadError,
          rows,
          enabled,
          enabledCount: rows.filter((r) => r.available && r.checked).length,
          availableCount: rows.filter((r) => r.available).length,
          isDefault: isAllSources(enabled),
          pendingNextResponse: pendingNext,
          showPendingNotice: pendingNext && entry.pendingNotice,
          cacheWarning: target.kind === 'new' ? null : current.cache_warning,
          saving: entry.saving || entry.pending !== null,
          error: entry.error,
          conflict: entry.conflict,
        };
      };
    },
  },

  actions: {
    /** The entry for a target of the CURRENT profile, created on first use.
     *  Assign then re-read: the reactive proxy, not the raw object, is what
     *  later writes must go through. */
    entryFor(target: SearchToolsTarget): { key: string; profile: string; entry: SearchToolsEntry } | null {
      const profile = useSettingsStore().profileId;
      if (!profile) return null;
      const key = entryKey(profile, target);
      if (!this.entries[key]) this.entries[key] = makeEntry();
      return { key, profile, entry: this.entries[key] };
    },

    flushKey(key: string): string {
      return `${this.instance}|${this.epoch}|${key}`;
    },

    /**
     * Read the target's state from the server. With `force`, re-read even when
     * one is held (availability moves when the person changes tool settings or
     * a run adopts a saved change). An answer that lands while a save is in
     * flight is ignored — the save's own answer is newer.
     */
    async load(target: SearchToolsTarget, opts: { force?: boolean } = {}): Promise<void> {
      const settings = useSettingsStore();
      if (!settings.authToken) return;
      const found = this.entryFor(target);
      if (!found) return;
      const { key, entry } = found;
      if (!opts.force && (entry.state || entry.loading)) return;
      const epoch = this.epoch;
      entry.loadSeq += 1;
      const seq = entry.loadSeq;
      entry.loading = true;
      try {
        const next = target.kind === 'new'
          ? await fetchNewChatSearchTools(settings.agentUrl, settings.authToken)
          : await fetchSearchTools(settings.agentUrl, settings.authToken, target.kind, target.id);
        const current = this.entries[key];
        if (epoch !== this.epoch || !current || current.loadSeq !== seq) return;
        current.loadError = null;
        if (current.saving || current.pending || flushes.has(this.flushKey(key))) return;
        if (current.state && next.version < current.state.version) return;
        current.state = next;
        // The response the notice was about has now adopted the change.
        if (!next.pending_next_response) current.pendingNotice = false;
      } catch (error) {
        const current = this.entries[key];
        if (epoch !== this.epoch || !current || current.loadSeq !== seq) return;
        current.loadError = errorText(error, 'Could not load search tools.');
      } finally {
        const current = this.entries[key];
        if (epoch === this.epoch && current && current.loadSeq === seq) current.loading = false;
      }
    },

    /**
     * Flip one source. Only rows the server sent, and only available ones, can
     * be toggled; the change shows at once and is saved in the background.
     * `running`: a response is in progress, so a successful save is announced
     * as applying from the next one.
     */
    toggle(target: SearchToolsTarget, id: SearchToolId, opts: { running?: boolean } = {}): void {
      const found = this.entryFor(target);
      if (!found?.entry.state) return;
      const row = found.entry.state.tools.find((r) => r.id === id);
      if (!row || !row.available) return;
      const shown = this.viewFor(target).enabled;
      const next = shown.includes(id)
        ? shown.filter((x) => x !== id)
        : orderSelection([...shown, id]);
      if (target.kind === 'new') {
        this.drafts[found.profile] = isAllSources(next) ? null : next;
        return;
      }
      this.requestSave(target, found.key, next, !!opts.running);
    },

    /** Back to every source. */
    resetToDefaults(target: SearchToolsTarget, opts: { running?: boolean } = {}): void {
      const found = this.entryFor(target);
      if (!found?.entry.state) return;
      if (target.kind === 'new') {
        delete this.drafts[found.profile];
        return;
      }
      this.requestSave(target, found.key, null, !!opts.running);
    },

    requestSave(
      target: SearchToolsTarget, key: string, enabled: SearchToolId[] | null, running: boolean,
    ): void {
      const entry = this.entries[key];
      if (!entry) return;
      entry.error = null;
      entry.conflict = null;
      entry.pendingSeq += 1;
      entry.pending = { enabled: enabled === null ? null : [...enabled], seq: entry.pendingSeq };
      entry.requestedWhileRunning = running;
      void this.startFlush(target, key);
    },

    startFlush(target: SearchToolsTarget, key: string): Promise<boolean> {
      const fk = this.flushKey(key);
      const existing = flushes.get(fk);
      if (existing) return existing;
      const run: Promise<boolean> = this.runFlush(target, key).finally(() => {
        if (flushes.get(fk) === run) flushes.delete(fk);
      });
      flushes.set(fk, run);
      return run;
    },

    /**
     * Save until the latest asked-for selection is the confirmed one. Each PUT
     * carries the version the previous answer returned. Resolves `true` when
     * everything asked for was saved, `false` when a save failed or conflicted
     * (the optimistic selection is dropped either way, so the control shows
     * what the server holds).
     */
    async runFlush(target: SearchToolsTarget, key: string): Promise<boolean> {
      if (target.kind === 'new') return true;
      const settings = useSettingsStore();
      const epoch = this.epoch;
      let ok = true;
      for (;;) {
        const entry = this.entries[key];
        if (epoch !== this.epoch || !entry) return false;
        if (!entry.pending || !entry.state) break;
        const { enabled, seq } = entry.pending;
        if (sameSelection(enabled, entry.state.enabled)) {
          // Toggled back to what is saved: nothing to send.
          entry.pending = null;
          break;
        }
        const running = entry.requestedWhileRunning;
        entry.saving = true;
        try {
          const next = await saveSearchTools(
            settings.agentUrl, settings.authToken, target.kind, target.id,
            entry.state.version, enabled,
          );
          const current = this.entries[key];
          if (epoch !== this.epoch || !current) return false;
          current.state = next;
          current.error = null;
          current.conflict = null;
          current.pendingNotice = running && next.pending_next_response;
          if (current.pending?.seq === seq) current.pending = null;
        } catch (error) {
          const current = this.entries[key];
          if (epoch !== this.epoch || !current) return false;
          if (error instanceof SearchToolsConflictError) {
            current.state = error.state;
            current.conflict = error.message || CONFLICT_FALLBACK;
            current.pendingNotice = false;
          } else {
            current.error = errorText(error, 'Could not save search tools.');
          }
          current.pending = null;
          ok = false;
          break;
        } finally {
          const current = this.entries[key];
          if (epoch === this.epoch && current) current.saving = false;
        }
      }
      // A stream frame announced a change while this was saving: read it now.
      const after = this.entries[key];
      if (
        epoch === this.epoch && after?.state
        && after.remoteVersion !== null && after.remoteVersion > after.state.version
      ) {
        void this.load(target, { force: true });
      }
      return ok;
    },

    /** Whether a save for this target is still on its way. */
    hasPendingSave(target: SearchToolsTarget | null): boolean {
      if (!target || target.kind === 'new') return false;
      const profile = useSettingsStore().profileId;
      if (!profile) return false;
      return flushes.has(this.flushKey(entryKey(profile, target)));
    },

    /**
     * Wait for the target's pending save. The composer calls this before it
     * sends, so the message is answered with the selection the person just
     * chose (the message itself never carries it). `false` means the save
     * failed or conflicted — keep the draft and let the control explain.
     */
    async settle(target: SearchToolsTarget | null): Promise<boolean> {
      if (!target || target.kind === 'new') return true;
      const profile = useSettingsStore().profileId;
      if (!profile) return true;
      const inflight = flushes.get(this.flushKey(entryKey(profile, target)));
      return inflight ? inflight : true;
    },

    /**
     * A `search_tools` stream frame: somebody (another tab, the CLI, a room
     * member) saved version `version`. Re-read unless this tab already holds
     * it; while a save of ours is in flight, remember it for the loop's tail.
     * Targets nothing on screen asked about are ignored.
     */
    noteRemoteVersion(target: SearchToolsTarget, version: unknown): void {
      if (typeof version !== 'number') return;
      const profile = useSettingsStore().profileId;
      if (!profile) return;
      const key = entryKey(profile, target);
      const entry = this.entries[key];
      if (!entry) return;
      if (entry.remoteVersion === null || version > entry.remoteVersion) entry.remoteVersion = version;
      if (entry.saving || entry.pending || flushes.has(this.flushKey(key))) return;
      if (entry.state && entry.state.version >= version) return;
      void this.load(target, { force: true });
    },

    /**
     * A response started or finished. The pending flag clears only when a new
     * response adopts the saved selection, which no frame announces — so while
     * one is pending, re-read around the run's lifecycle.
     */
    refreshIfPending(target: SearchToolsTarget): void {
      const profile = useSettingsStore().profileId;
      if (!profile) return;
      const entry = this.entries[entryKey(profile, target)];
      if (!entry?.state || entry.saving || entry.pending) return;
      if (!entry.state.pending_next_response && !entry.pendingNotice) return;
      void this.load(target, { force: true });
    },

    /** Clear the inline error / conflict notice. */
    dismissMessages(target: SearchToolsTarget): void {
      const profile = useSettingsStore().profileId;
      if (!profile) return;
      const entry = this.entries[entryKey(profile, target)];
      if (!entry) return;
      entry.error = null;
      entry.conflict = null;
    },

    /**
     * The new-chat draft for the request that creates the conversation:
     * `null` for the defaults (untouched, or every source ticked).
     */
    newChatSelection(): SearchToolId[] | null {
      const profile = useSettingsStore().profileId;
      if (!profile) return null;
      const draft = this.drafts[profile];
      if (!draft) return null;
      return isAllSources(draft) ? null : [...draft];
    },

    /** The draft went into a created conversation; the next new chat starts
     *  from the defaults again. */
    clearNewChatDraft(): void {
      const profile = useSettingsStore().profileId;
      if (profile) delete this.drafts[profile];
    },

    /** Drop what is held for a conversation or room that no longer exists. */
    forget(target: SearchToolsTarget): void {
      const profile = useSettingsStore().profileId;
      if (!profile) return;
      delete this.entries[entryKey(profile, target)];
    },

    resetForProfileSwitch(): void {
      this.epoch += 1;
      this.entries = {};
      this.drafts = {};
      for (const key of Array.from(flushes.keys())) {
        if (key.startsWith(`${this.instance}|`)) flushes.delete(key);
      }
    },
  },
});
