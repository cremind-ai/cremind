import type { MessageRecord } from '../services/conversationApi';
import type { ChatMessage } from '../stores/chat';

/**
 * Rebuild the bubbles a mid-turn message split, from the single row it left behind.
 *
 * A turn interrupted by a message the user sent while it was running is watched
 * as several bubbles — work so far, the message, the reply and the rest — but it
 * persists as ONE agent row, because it was one continuous chain to the model.
 * `metadata.mid_turn_breaks` records where the visible flow was cut, as offsets
 * into that row's text and thinking steps, so a reload can show what the user
 * actually saw instead of collapsing the turn back into a single block.
 *
 * Two rearrangements happen here, both of them restoring live order that DB
 * order cannot express on its own:
 *
 * - **Interrupting messages move down.** A mid-turn user row is written the
 *   moment it arrives, so it sits ABOVE the agent row of the turn it
 *   interrupted; live it belongs between two stretches of that turn's work. The
 *   break naming it says where.
 * - **Released messages move down too.** A message parked while the turn was
 *   ending never made it in and ran as its own follow-up turn, yet its row was
 *   still written first. It belongs after the turn it missed.
 */

interface MidTurnBreak {
  message_ids?: string[];
  step?: number | null;
  content_offset?: number;
  thinking_offset?: number;
}

function breaksOf(record: MessageRecord): MidTurnBreak[] {
  const raw = (record.metadata as any)?.mid_turn_breaks;
  if (!Array.isArray(raw) || raw.length === 0) return [];
  // Defensive sort: rendering depends on the offsets increasing, and a row
  // hand-edited or written by a future version should not scramble the turn.
  return [...raw].sort(
    (a, b) =>
      (a?.thinking_offset ?? 0) - (b?.thinking_offset ?? 0)
      || (a?.content_offset ?? 0) - (b?.content_offset ?? 0),
  );
}

function midTurnState(record: MessageRecord): string | undefined {
  return (record.metadata as any)?.mid_turn?.state;
}

function clamp(value: number | undefined, max: number): number {
  if (typeof value !== 'number' || Number.isNaN(value)) return max;
  return Math.max(0, Math.min(value, max));
}

export function splitMidTurnSegments(
  records: MessageRecord[],
  map: (record: MessageRecord) => ChatMessage,
): ChatMessage[] {
  const mapped = records.map(map);

  // Every user row some turn folded in. Those bubbles are emitted at their
  // break instead of where the row sits. `extracted` is the membership test and
  // never shrinks; `heldUser` is drained as bubbles are placed, so a break
  // naming the same row twice cannot duplicate it.
  const extracted = new Set<string>();
  const heldUser = new Map<string, ChatMessage>();
  for (const record of records) {
    for (const brk of breaksOf(record)) {
      for (const id of brk.message_ids ?? []) {
        const at = records.findIndex(r => r.id === id && r.role === 'user');
        if (at >= 0) {
          extracted.add(id);
          heldUser.set(id, mapped[at]);
        }
      }
    }
  }

  const out: ChatMessage[] = [];

  /** One agent row → its segments, with the folded-in bubbles between them. */
  const emitAgent = (index: number): void => {
    const record = records[index];
    const full = mapped[index];
    const brks = breaksOf(record);
    if (brks.length === 0) {
      out.push(full);
      return;
    }

    const steps = full.thinkingSteps ?? [];
    const content = full.content ?? '';
    let prevStep = 0;
    let prevChar = 0;

    brks.forEach((brk, k) => {
      const stepAt = clamp(brk.thinking_offset, steps.length);
      const charAt = clamp(brk.content_offset, content.length);
      const segSteps = steps.slice(prevStep, stepAt);
      const segText = content.slice(prevChar, charAt).trim();
      // A break can land before the turn produced anything — a message that
      // arrived before the first step. Nothing to show, so show nothing rather
      // than an empty bubble; the user message below still lands in order.
      if (segText || segSteps.length) {
        out.push({
          // Suffixed so every bubble keeps a unique v-for key. Only the LAST
          // segment carries the real row id, which is what the usage chip and
          // the todo chip match on.
          id: `${record.id}::seg${k}`,
          role: 'assistant',
          content: segText,
          parts: [],
          timestamp: full.timestamp,
          isStreaming: false,
          thinkingSteps: segSteps.length ? segSteps : undefined,
        });
      }
      for (const id of brk.message_ids ?? []) {
        const bubble = heldUser.get(id);
        if (bubble) {
          out.push(bubble);
          heldUser.delete(id);
        }
      }
      prevStep = stepAt;
      prevChar = charAt;
    });

    // The remainder keeps the row's identity and everything that belongs to the
    // turn as a whole: token usage, attachments, the todo snapshot, the summary.
    const tailSteps = steps.slice(prevStep);
    out.push({
      ...full,
      id: record.id,
      content: content.slice(prevChar).trim(),
      thinkingSteps: tailSteps.length ? tailSteps : undefined,
    });
  };

  let i = 0;
  while (i < records.length) {
    const record = records[i];

    // Emitted at its break instead of here.
    if (record.role === 'user' && extracted.has(record.id)) {
      i++;
      continue;
    }

    if (record.role === 'user' && midTurnState(record) === 'released') {
      // A whole burst can be released at once, so take the run — moving them
      // one at a time would strand every message but the last.
      let j = i;
      while (
        j < records.length
        && records[j].role === 'user'
        && midTurnState(records[j]) === 'released'
      ) j++;
      const followedByAgent = j < records.length && records[j].role === 'agent';
      if (followedByAgent) {
        emitAgent(j);
        for (let k = i; k < j; k++) out.push(mapped[k]);
        i = j + 1;
      } else {
        // No turn after them (the follow-up has not been written yet, or never
        // was): leave them where they are, which is also where they show live.
        for (let k = i; k < j; k++) out.push(mapped[k]);
        i = j;
      }
      continue;
    }

    if (record.role === 'agent') {
      emitAgent(i);
      i++;
      continue;
    }

    out.push(mapped[i]);
    i++;
  }

  return out;
}
