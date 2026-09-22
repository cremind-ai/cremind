/**
 * How long a turn took, in words — shared by the bubble's summary line and the
 * Thinking Process timeline so the two never disagree about what a number means.
 *
 * Every label here answers "how long did I wait", which makes the baseline the
 * whole story. Latency used to be timed entirely in the browser against a
 * baseline set when the FIRST frame created the assistant bubble; for a turn
 * whose first frame was a token, the baseline and the milestone were the same
 * instant and the bubble reported "First token: 0ms". The server now times its
 * own turn and the numbers ride the frames and the stored row, so the fields
 * below come from it whenever it has spoken — and only fall back to this tab's
 * stamps while a turn is still streaming.
 *
 * Structural parameter types (rather than the store's) keep this module free of
 * store imports: it is pure, and both callers are components.
 */

/** Milestones a turn reports. Mirrors ``LatencyInfo`` in stores/chat.ts. */
export interface LatencyFields {
  firstStepMs?: number;
  firstTokenMs?: number;
  totalMs?: number;
  totalMsApprox?: number;
  requestSentAt?: number;
  firstEventAt?: number;
  firstStepAt?: number;
  firstTokenAt?: number;
  completedAt?: number;
}

/** A step's two timings: the server's elapsed, and this tab's arrival stamp. */
export interface StepTiming {
  elapsedMs?: number;
  receivedAt?: number;
}

export interface LatencySummary {
  /** e.g. ``"First step: 5.6s | First token: 12.7s | Total: 13.2s"``. */
  text: string;
  /** True when the total was inferred rather than measured (rendered ``~``). */
  approximate: boolean;
}

/** ``4823`` -> ``"4.8s"``, ``820`` -> ``"820ms"``. */
export function formatLatencyMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * The bubble's one-line summary, or null when the turn has nothing to say yet.
 *
 * A milestone at or below zero is dropped rather than printed: it means the
 * baseline and the milestone are the same instant, which says nothing and reads
 * as broken.
 */
export function latencySummary(latency: LatencyFields | undefined): LatencySummary | null {
  if (!latency) return null;

  // While the turn streams, this tab measures from the send that started it. A
  // turn nobody here started (an automation, a channel message) has no send, so
  // it measures from its first frame until the server's numbers arrive.
  const base = latency.requestSentAt ?? latency.firstEventAt;
  const since = (at?: number) =>
    base !== undefined && at !== undefined ? at - base : undefined;

  const measuredTotal = latency.totalMs ?? since(latency.completedAt);
  const total = measuredTotal ?? latency.totalMsApprox;
  const approximate = measuredTotal === undefined && total !== undefined;

  const milestones: [string, string, number | undefined][] = [
    ['First step', '', latency.firstStepMs ?? since(latency.firstStepAt)],
    ['First token', '', latency.firstTokenMs ?? since(latency.firstTokenAt)],
    ['Total', approximate ? '~' : '', total],
  ];

  const parts = milestones
    .filter(([, , ms]) => ms !== undefined && ms > 0)
    .map(([label, prefix, ms]) => `${label}: ${prefix}${formatLatencyMs(ms as number)}`);

  return parts.length > 0 ? { text: parts.join(' | '), approximate } : null;
}

/**
 * The ``· 4.3s`` suffix on a timeline step: how long the turn spent getting
 * from the previous step to this one, or from the start of the turn to the
 * first one (pass ``previous`` as undefined for that).
 *
 * ``elapsedMs`` is the server's stamp and is preferred, because it is the one
 * that is persisted — a reloaded timeline shows the labels the live one did.
 * ``receivedAt`` is this tab's arrival time and exists only while the turn
 * streams; it stays as the fallback for steps from runs that predate the stamp.
 *
 * Returns ``''`` when neither source can answer, so a caller can concatenate it
 * unconditionally.
 */
export function stepElapsedLabel(
  current: StepTiming | undefined,
  previous: StepTiming | undefined,
  requestSentAt?: number,
): string {
  if (!current) return '';

  let ms: number | undefined;
  if (typeof current.elapsedMs === 'number') {
    ms = previous === undefined
      ? current.elapsedMs
      : typeof previous.elapsedMs === 'number'
        ? current.elapsedMs - previous.elapsedMs
        : undefined;
  } else if (current.receivedAt !== undefined) {
    ms = previous === undefined
      ? requestSentAt !== undefined
        ? current.receivedAt - requestSentAt
        : undefined
      : previous.receivedAt !== undefined
        ? current.receivedAt - previous.receivedAt
        : undefined;
  }

  return ms !== undefined && ms > 0 ? ` · ${formatLatencyMs(ms)}` : '';
}

/** The little of a loaded message ``backfillLegacyTotals`` needs to see. */
export interface TimedMessage {
  role: string;
  timestamp: Date;
  latency?: LatencyFields;
  isEventResult?: boolean;
  isRejectedTrigger?: boolean;
}

// A turn longer than this is almost certainly not one: a pair of rows that only
// look adjacent, an import, a clock that moved. Better no number than that one.
const LEGACY_TOTAL_CEILING_MS = 60 * 60 * 1000;

/**
 * Give turns that ran before the server timed itself an approximate total.
 *
 * Their rows carry no ``metadata.latency``, so an older conversation reopened
 * with no timings at all. The two row timestamps still bracket the turn: the
 * user row is written as the run starts and the agent row when it ends, so the
 * gap is how long the turn took, give or take the work either side of it.
 *
 * Approximate, and shown as such (``~14.3s``) — it is never merged into a
 * measured number, and a row that has one is left alone. Only a user bubble
 * directly followed by an agent one qualifies; anything else (an automation
 * reporting in, a turn a mid-turn message split) has no single moment to
 * subtract from.
 *
 * Mutates in place and returns the same array: the caller is assigning it
 * straight into the store.
 */
export function backfillLegacyTotals<T extends TimedMessage>(messages: T[]): T[] {
  for (let i = 1; i < messages.length; i++) {
    const agent = messages[i];
    const user = messages[i - 1];
    if (agent.role !== 'assistant' || user.role !== 'user') continue;
    if (agent.latency?.totalMs !== undefined) continue;
    if (agent.isEventResult || agent.isRejectedTrigger) continue;
    const ms = agent.timestamp.getTime() - user.timestamp.getTime();
    if (ms <= 0 || ms > LEGACY_TOTAL_CEILING_MS) continue;
    agent.latency = { ...(agent.latency || {}), totalMsApprox: ms };
  }
  return messages;
}
