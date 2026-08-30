/**
 * Frame → model mappers shared by every surface that renders an agent's work.
 *
 * A conversation stream and a group room stream carry the same step payloads —
 * a room's `seat_event` is one conversation frame re-published under the member
 * it came from — so both build the same ThinkingStep / TerminalAttachment /
 * TokenUsage objects out of them. These lived inline in `stores/chat.ts` until
 * the room needed them; a second copy over there would be the kind that drifts
 * unnoticed, because a mapping that falls out of step shows up as a chip that
 * quietly never renders, never as an error.
 *
 * Everything here is pure and store-free, so the chat store's types come in
 * with `import type` and the cycle is erased at compile time — the chat store
 * imports this module, never the reverse (same shape as utils/midTurnSplit.ts).
 */

import type { MessageRecord } from '../services/conversationApi';
import type {
  ObservationPart,
  StepTokenUsage,
  TerminalAttachment,
  ThinkingStep,
  TokenUsage,
} from '../stores/chat';

/**
 * Map the backend's snake_case per-step token payload (same shape on the live
 * ``thinking`` SSE event and the persisted ``thinking_steps`` blob) to camelCase.
 * Returns null when absent or all-zero so the UI can skip the badge cleanly.
 */
export function mapStepTokenUsage(raw: any): StepTokenUsage | null {
  if (!raw || typeof raw !== 'object') return null;
  const inputTokens = raw.input_tokens || 0;
  const cacheReadTokens = raw.cache_read_input_tokens || 0;
  const cacheCreationTokens = raw.cache_creation_input_tokens || 0;
  const outputTokens = raw.output_tokens || 0;
  if (!(inputTokens || cacheReadTokens || cacheCreationTokens || outputTokens)) return null;
  return { inputTokens, cacheReadTokens, cacheCreationTokens, outputTokens };
}

/**
 * One ``thinking`` frame as a step. ``Step`` groups the tool calls a single
 * model turn made; ``Call_Id`` is what pairs each of them with its result.
 *
 * ``receivedAt`` is stamped here and read back by the caller for the turn's
 * first-step latency, so the step and the latency mark the same instant.
 */
export function thinkingStepFromFrame(data: any): ThinkingStep {
  return {
    step: data.Step ?? null,
    callId: data.Call_Id ?? null,
    tool: data.Tool || '',
    toolInput: data.Tool_Input || '',
    receivedAt: Date.now(),
    modelLabel: data.Model_Label || null,
    tokenUsage: mapStepTokenUsage(data.Token_Usage),
  };
}

/**
 * Attach a ``result`` frame to the step whose call it answers, returning that
 * step (null when there is nothing to attach it to).
 *
 * Pairing goes by ``call_id`` first because one model turn can fire several
 * tools at once: "the newest step still missing a result" alone would hand the
 * first result to whichever parallel call happened to be pushed last. That
 * newest-first scan stays as the fallback, for frames from older runs that
 * carry no call id.
 *
 * Mutates the step in place — the array belongs to a reactive message and the
 * caller is relying on the assignment being seen.
 */
export function attachResultToSteps(
  steps: ThinkingStep[] | undefined,
  data: any,
): ThinkingStep | null {
  const resultRaw = data.Result ?? data.Observation;
  const resultParts: ObservationPart[] = Array.isArray(resultRaw)
    ? resultRaw
    : [{
        kind: 'text',
        text: typeof resultRaw === 'string' ? resultRaw : JSON.stringify(resultRaw),
      }];
  if (!steps || steps.length === 0) return null;
  const target = (data.call_id
    ? steps.find(s => s.callId === data.call_id && !s.result)
    : undefined)
    || [...steps].reverse().find(s => !s.result);
  if (!target) return null;
  target.result = resultParts;
  return target;
}

/** One ``terminal`` frame as a chip. Deduping against a message's existing
 *  chips is the caller's job — that list is per bubble, not per frame. */
export function terminalAttachmentFromFrame(data: any): TerminalAttachment {
  return {
    processId: data.process_id,
    command: data.command || '',
    commandShort: data.command_short || data.command || '',
    workingDirectory: data.working_directory || '',
    pty: Boolean(data.pty),
  };
}

/**
 * The persisted counterpart of the ``terminal`` frames: a stored row's ``parts``
 * as chips. A terminal is written into the message as a data part, so this is
 * how a shell the agent opened is still there after a reload.
 */
export function terminalAttachmentsFromParts(
  parts: any[] | null | undefined,
): TerminalAttachment[] {
  if (!Array.isArray(parts)) return [];
  return parts
    .filter((p: any) => p?.kind === 'data' && p.data && typeof p.data.process_id === 'string')
    .map((p: any) => ({
      processId: p.data.process_id,
      command: p.data.command || '',
      commandShort: p.data.command_short || p.data.command || '',
      workingDirectory: p.data.working_directory || '',
      pty: Boolean(p.data.pty),
    }));
}

/** One ``token_usage`` frame as the bubble's rollup. The counts arrive either
 *  nested under ``token_usage`` or flat on the frame, depending on the emitter. */
export function tokenUsageFromFrame(data: any): TokenUsage {
  const usage = data.token_usage ?? data;
  const cacheRead = usage.cache_read_input_tokens || 0;
  const cacheCreation = usage.cache_creation_input_tokens || 0;
  return {
    inputTokens: usage.input_tokens || 0,
    outputTokens: usage.output_tokens || 0,
    cacheReadTokens: cacheRead,
    cacheCreationTokens: cacheCreation,
    totalTokens:
      (usage.input_tokens || 0) + cacheRead + cacheCreation + (usage.output_tokens || 0),
  };
}

/**
 * A stored row's ``token_usage`` blob as the bubble's rollup, or `undefined`
 * when the row has none — a turn that made no LLM call (a silent group turn, a
 * message written by a tool) stores nothing, and no chip should appear for it.
 *
 * The persisted counterpart of {@link tokenUsageFromFrame}, sharing its
 * arithmetic so a bubble does not change its total when a reload swaps the
 * live rollup for the stored one.
 */
export function tokenUsageFromRecord(raw: any): TokenUsage | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  return tokenUsageFromFrame(raw);
}

/**
 * The persisted counterpart of the live frames: a stored row's
 * ``thinking_steps`` blob as steps. ``observation`` is the pre-rename field
 * name and is still read, because rows written before it moved to ``result``
 * are still in everyone's database.
 */
export function thinkingStepsFromRecord(
  raw: MessageRecord['thinking_steps'] | undefined,
): ThinkingStep[] | undefined {
  if (!raw) return undefined;
  return raw.map(s => ({
    step: (s as any).step ?? null,
    callId: (s as any).call_id ?? null,
    tool: (s as any).tool ?? '',
    toolInput: (s as any).tool_input ?? '',
    result: ((s as any).result ?? s.observation) as ObservationPart[] | undefined,
    modelLabel: s.model_label || null,
    tokenUsage: mapStepTokenUsage((s as any).token_usage),
  }));
}
