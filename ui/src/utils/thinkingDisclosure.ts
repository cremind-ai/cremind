/**
 * When a reply's Thinking Process opens or closes by itself.
 *
 * It used to open as soon as the agent's first step arrived and stay open for
 * the whole turn — a wall of tool calls scrolling past people who only wanted
 * the answer. Now it stays closed until clicked (`collapsed`, the default);
 * `live` — the Auto-open switch on the row itself — brings back the
 * open-while-working timeline. Either way, once someone opens or closes a
 * reply's section themselves, it is theirs: nothing reopens or closes it under
 * them.
 */

import type { ThinkingProcessMode } from '../appearance/presets';

export interface ThinkingState {
  isStreaming: boolean;
  stepCount: number;
  /** The user has opened or closed this section during the turn. */
  userToggled: boolean;
}

/** Open it now, without a click? */
export function shouldAutoOpen(mode: ThinkingProcessMode, s: ThinkingState): boolean {
  return mode === 'live' && s.isStreaming && s.stepCount > 0 && !s.userToggled;
}

/** Close it as the reply finishes? */
export function shouldAutoClose(mode: ThinkingProcessMode, s: Pick<ThinkingState, 'userToggled'>): boolean {
  return mode === 'live' && !s.userToggled;
}

/**
 * Auto-open was just switched (on this row or any other, or from elsewhere):
 * what a section should do now — `true` open, `false` close, `null` stay. Only
 * a reply still being written follows the switch; a finished one keeps its
 * state until the next reply.
 */
export function openAfterModeChange(
  mode: ThinkingProcessMode,
  s: ThinkingState & { open: boolean },
): boolean | null {
  if (!s.isStreaming || s.userToggled || s.stepCount === 0) return null;
  if (mode === 'live' && !s.open) return true;
  if (mode === 'collapsed' && s.open) return false;
  return null;
}
