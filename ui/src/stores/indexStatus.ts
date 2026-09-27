/**
 * Index status of the files the file tree shows, for the signed-in profile.
 *
 * Rows and tiles register their path while on screen (files only); this store
 * looks them up in batches through `POST /api/documentation-search/files/lookup`
 * and keeps the answers per path. The batching, throttling and the session
 * guard live in `IndexLookupController` (utils/indexStatus.ts); this wraps it
 * in reactive state and feeds it:
 *
 * - the session — the auth token and the directory the tree shows. A new one
 *   drops every answer still in flight and starts from an empty cache, so one
 *   profile's statuses never show in another's tree;
 * - the documents snapshot the documents store already keeps fresh (the
 *   profile-events topic on chat routes, a poll elsewhere) — no stream of its
 *   own. Each new snapshot means the index moved: the visible paths are looked
 *   up again, throttled. A snapshot saying the local folder is off hides every
 *   status at once;
 * - listing changes, reported by the file tree.
 *
 * The status UI follows the "Search my documents" switch (the local folder
 * source), not the conversation's selected search tools.
 */

import { defineStore } from 'pinia';
import { computed, reactive, ref, shallowReactive, watch } from 'vue';

import { lookupIndexedPaths } from '../services/documentsApi';
import {
  IndexLookupController,
  describeIndexStatus,
  localSourceEnabled,
  snapshotContentKey,
  type IndexStatusView,
  type LookupEntry,
  type LookupSourceState,
} from '../utils/indexStatus';
import { useDocumentsStore } from './documents';
import { useSettingsStore } from './settings';

export const useIndexStatusStore = defineStore('indexStatus', () => {
  const settings = useSettingsStore();
  const documents = useDocumentsStore();

  // Per-key tracking: a row re-renders when its own path's entry changes.
  const cache = shallowReactive(new Map<string, LookupEntry>());
  const source = reactive<LookupSourceState>({ enabled: null, root: null, available: true });
  const directory = ref('');

  // The token the current session was opened with; lookups always carry it,
  // so a request never mixes one profile's session with another's token.
  let sessionToken = settings.authToken || '';

  const controller = new IndexLookupController({
    cache,
    state: source,
    lookup: (paths) => lookupIndexedPaths(settings.agentUrl, sessionToken, paths),
  });

  // The last snapshot acted on, without its stamp: a poll that finds the index
  // as it was (a new `seq`, nothing else) looks nothing up again.
  let lastSnapshotKey = '';

  function applySnapshot() {
    const snap = documents.snapshot;
    if (!snap) return;
    const key = snapshotContentKey(snap);
    if (key === lastSnapshotKey) return;
    lastSnapshotKey = key;
    if (localSourceEnabled(snap) === false) controller.sourceDisabled();
    else controller.sourceMaybeEnabled();
  }

  watch(
    () => [settings.authToken || '', directory.value] as const,
    ([token, dir]) => {
      if (token !== sessionToken) lastSnapshotKey = '';
      sessionToken = token;
      if (!token) {
        controller.setSession('');
        controller.sourceDisabled();
        return;
      }
      if (controller.setSession(`${token}\u0000${dir}`) && localSourceEnabled(documents.snapshot) === false) {
        controller.sourceDisabled();
      }
    },
    // Synchronously, so no lookup can start between a token change and the
    // session that goes with it.
    { immediate: true, flush: 'sync' },
  );

  watch(() => documents.snapshot, applySnapshot);

  /** Statuses are shown at all: the local folder source is on. */
  const enabled = computed(() => source.enabled === true);

  /** What the row for `path` shows; null for nothing (source off, not looked
   *  up yet). */
  function statusFor(path: string): IndexStatusView | null {
    if (source.enabled !== true || !path) return null;
    return describeIndexStatus(cache.get(path));
  }

  function register(path: string) {
    controller.register(path);
  }

  function unregister(path: string) {
    controller.unregister(path);
  }

  /** The directory the file tree shows (part of the session). */
  function setDirectory(dir: string) {
    directory.value = dir || '';
  }

  /** The listing changed (a file was added, changed, moved or removed). */
  function noteListingChanged() {
    controller.requestRefresh();
  }

  return {
    enabled,
    source,
    statusFor,
    register,
    unregister,
    setDirectory,
    noteListingChanged,
  };
});
