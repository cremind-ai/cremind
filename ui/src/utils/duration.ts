// Duration / relative-time helpers for event runs and board cards.
// All inputs are epoch milliseconds (matching EventRun.*_at). Kept tiny and
// dependency-free, alongside usageFormat.ts.

import type { EventRun } from '../services/eventRunsApi';

/** Elapsed-time label: <1s / 42s / 3m 20s / 1h 05m. Blank on bad input. */
export function formatDurationMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return '<1s';
  const totalSec = Math.round(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const totalMin = Math.floor(totalSec / 60);
  if (totalMin < 60) return `${totalMin}m ${totalSec % 60}s`;
  const hours = Math.floor(totalMin / 60);
  return `${hours}h ${String(totalMin % 60).padStart(2, '0')}m`;
}

/**
 * Duration of a run. Terminal runs use `finished_at`; a still-running run uses
 * `nowMs` (pass the shared ticker from useNow so the card updates live).
 */
export function runDuration(
  run: Pick<EventRun, 'created_at' | 'finished_at'>,
  nowMs?: number,
): string {
  if (!run.created_at) return '';
  const end = run.finished_at ?? nowMs;
  if (end == null) return '';
  return formatDurationMs(end - run.created_at);
}

/**
 * Compact relative time for both past and future instants:
 * "just now", "5m ago", "3h ago", "2d ago", "soon", "in 3h", "in 2d".
 * Pass `nowMs` (from useNow) to keep it live.
 */
export function formatRelative(targetMs: number | null | undefined, nowMs?: number): string {
  if (!targetMs) return '';
  const now = nowMs ?? Date.now();
  const diffMs = targetMs - now;
  const future = diffMs > 0;
  const abs = Math.abs(diffMs);
  if (abs < 45_000) return future ? 'soon' : 'just now';
  const mins = Math.round(abs / 60_000);
  let core: string;
  if (mins < 60) core = `${mins}m`;
  else if (mins < 60 * 24) core = `${Math.floor(mins / 60)}h`;
  else core = `${Math.floor(mins / (60 * 24))}d`;
  return future ? `in ${core}` : `${core} ago`;
}
