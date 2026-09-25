/**
 * The pure half of the userDocs store: how snapshots are folded in, what they
 * mean, and which transport should be carrying them.
 *
 * Kept free of Pinia and Vue so node:test can drive it directly
 * (tests/userdocs-reducer.test.mjs). The store wraps these in refs.
 *
 * Snapshots reach the page by three paths at once — an SSE frame, a 15 s poll,
 * the snapshot returned by a settings save or a control action — and nothing
 * orders them in flight. `reduceSnapshot` keeps whichever the server produced
 * last, by the (boot, seq) stamp every snapshot carries.
 */

import { isStaleSnapshot, type UserDocsSnapshot } from '../services/userdocsApi';

export interface UserDocsStreamState {
  snapshot: UserDocsSnapshot | null;
  /** Stamp of the newest ordered frame taken. Only frames that carry both
   *  `boot` and `seq` move it, so an unstamped fallback frame cannot reopen
   *  the door to older frames behind it. */
  cursor: { boot: string; seq: number } | null;
}

export const EMPTY_STREAM_STATE: UserDocsStreamState = Object.freeze({
  snapshot: null,
  cursor: null,
}) as UserDocsStreamState;

/** Fold `next` in. Returns the same object when `next` is stale, so a caller
 *  holding it in a ref does not trigger a re-render for nothing. */
export function reduceSnapshot(
  state: UserDocsStreamState,
  next: UserDocsSnapshot | null | undefined,
): UserDocsStreamState {
  if (!next || typeof next !== 'object' || typeof next.state !== 'string') return state;
  if (isStaleSnapshot(state.cursor, next)) return state;
  const stamped = typeof next.boot === 'string' && next.boot && typeof next.seq === 'number';
  return {
    snapshot: next,
    cursor: stamped ? { boot: next.boot as string, seq: next.seq as number } : state.cursor,
  };
}

// ── what a snapshot means ──────────────────────────────────────────────────

/** States in which work is visibly under way. */
const ACTIVE_STATES = new Set(['estimating', 'scanning', 'indexing', 'reembedding']);

/** Storage pauses the user has to act on (a user pause is their own choice). */
const STORAGE_PAUSES = new Set(['budget', 'disk_low', 'disk_critical']);

/** File statuses that are not part of "the index" for progress purposes:
 *  tombstones are deletes in their grace window, `missing` rows are held
 *  after a mass deletion and hidden from search. */
const OUTSIDE_PROGRESS = new Set(['tombstone', 'missing']);

export function isActive(snap: UserDocsSnapshot | null | undefined): boolean {
  return !!snap && ACTIVE_STATES.has(snap.state);
}

/** Files that failed to index. The per-status count is authoritative; the
 *  failed preview only holds this session's last few failures. */
export function failedCount(snap: UserDocsSnapshot | null | undefined): number {
  if (!snap) return 0;
  if (snap.stages && typeof snap.stages.error === 'number') return snap.stages.error;
  return snap.failed_preview?.length ?? 0;
}

export function needsAttention(snap: UserDocsSnapshot | null | undefined): boolean {
  if (!snap) return false;
  if (snap.state === 'awaiting_confirmation' || snap.state === 'hold') return true;
  if (snap.state === 'paused' && STORAGE_PAUSES.has(snap.reason ?? '')) return true;
  // Drive's holds and confirmations live in snap.drive, never in the
  // top-level state (which is the local folder's).
  if (snap.drive?.enabled && (snap.drive.state === 'hold' || snap.drive.confirmation)) return true;
  return failedCount(snap) > 0;
}

export interface SyncProgressModel {
  /** Units done (files, or chunks while re-embedding). */
  done: number;
  total: number;
  /** 0–100, or null when there is nothing to measure against. */
  pct: number | null;
  etaS: number | null;
  failed: number;
  /** Where the numbers came from: the engine's batch counter, the re-embed
   *  counter, or the per-status file counts. */
  source: 'batch' | 'reembed' | 'stages' | 'none';
  unit: 'files' | 'chunks';
}

/**
 * Overall progress. The engine's batch counter (with its ETA) wins when a
 * batch is open; a re-embed reports chunks; otherwise progress is read off the
 * per-status counts — everything in the index that is no longer waiting
 * (`dirty`) is done, which is also what a first sync looks like while its
 * scan is still adding rows.
 */
export function syncProgress(snap: UserDocsSnapshot | null | undefined): SyncProgressModel {
  const failed = failedCount(snap);
  const none: SyncProgressModel = {
    done: 0, total: 0, pct: null, etaS: null, failed, source: 'none', unit: 'files',
  };
  if (!snap) return none;

  const batch = snap.batch;
  if (batch && batch.label && batch.total > 0) {
    const done = Math.min(batch.done, batch.total);
    return {
      done,
      total: batch.total,
      pct: (100 * done) / batch.total,
      etaS: typeof batch.eta_s === 'number' && batch.eta_s > 0 ? batch.eta_s : null,
      failed,
      source: 'batch',
      unit: 'files',
    };
  }

  if (snap.state === 'reembedding' && snap.reembed && snap.reembed.total > 0) {
    const done = Math.min(snap.reembed.done, snap.reembed.total);
    return {
      done,
      total: snap.reembed.total,
      pct: (100 * done) / snap.reembed.total,
      etaS: null,
      failed,
      source: 'reembed',
      unit: 'chunks',
    };
  }

  const stages = snap.stages;
  if (stages) {
    let total = 0;
    for (const [status, n] of Object.entries(stages)) {
      if (!OUTSIDE_PROGRESS.has(status)) total += Number(n) || 0;
    }
    if (total > 0) {
      const done = total - Math.min(total, Number(stages.dirty) || 0);
      return { done, total, pct: (100 * done) / total, etaS: null, failed, source: 'stages', unit: 'files' };
    }
  }
  return none;
}

// ── transport ──────────────────────────────────────────────────────────────

export type UserDocsTransport = 'none' | 'profile-events' | 'page-stream' | 'poll';

export interface TransportInputs {
  hasToken: boolean;
  /** The current route is a `/:profile/...` page. */
  profileRoute: boolean;
  /** The current route renders chat (and so already holds profile-events). */
  chatRoute: boolean;
  /** Settings → My Documents is mounted. */
  pageMounted: boolean;
  /** The profile's own switch, from the last snapshot; null before any. */
  enabled: boolean | null;
}

/**
 * Which connection should carry snapshots right now (design §2.7):
 *
 * - My Documents mounted → its own stream, whatever the feature's state, since
 *   that page is where it gets switched on;
 * - a chat route → the `userdocs` topic of profile-events, which that route
 *   already holds open;
 * - any other profile page → a 15 s poll, but only while the feature is on (or
 *   not yet known — the first poll answers that);
 * - no token, or not a profile page → nothing.
 */
export function chooseTransport(o: TransportInputs): UserDocsTransport {
  if (!o.hasToken || !o.profileRoute) return 'none';
  if (o.pageMounted) return 'page-stream';
  if (o.chatRoute) return 'profile-events';
  return o.enabled === false ? 'none' : 'poll';
}
