// The per-turn agent mode chosen in the composer's mode selector. Sent to the
// backend as ``mode`` on POST /messages; persisted per-profile in localStorage.
export type ChatMode = 'plan' | 'reasoning' | 'instant';

export interface ChatModeMeta {
  id: ChatMode;
  label: string; // shown in the menu row + button title
  description: string; // one-liner under the label in the menu
  icon: string; // iconify name (mdi:*)
  buttonClass: string; // class applied to the composer button per active mode
}

export const CHAT_MODES: ChatModeMeta[] = [
  {
    id: 'plan',
    label: 'Plan mode',
    description: 'Research, ask questions, and write a plan for approval before executing.',
    icon: 'mdi:clipboard-list-outline',
    buttonClass: 'mode-plan',
  },
  {
    id: 'reasoning',
    label: 'Reasoning',
    description: 'Default. Step-by-step thinking with tools.',
    icon: 'mdi:head-cog-outline',
    buttonClass: 'mode-reasoning',
  },
  {
    id: 'instant',
    label: 'Instant Mode',
    description: 'Fastest. No extended thinking; at most one round of tool use.',
    icon: 'mdi:flash-outline',
    buttonClass: 'mode-instant',
  },
];

export const DEFAULT_CHAT_MODE: ChatMode = 'reasoning';

export function isChatMode(v: unknown): v is ChatMode {
  return v === 'plan' || v === 'reasoning' || v === 'instant';
}

export function chatModeMeta(mode: ChatMode): ChatModeMeta {
  return CHAT_MODES.find((m) => m.id === mode) ?? CHAT_MODES[1];
}
