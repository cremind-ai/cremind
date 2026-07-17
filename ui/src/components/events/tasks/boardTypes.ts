// Normalization seam for the Tasks board. The three subscription shapes
// (skill / file-watcher / schedule) are mapped to one `BoardSubscription`
// discriminated union so the board's cards don't special-case each source.
//
// IMPORTANT — units: subscription timestamps (`created_at`, `next_fire_at`) are
// epoch SECONDS, while EventRun timestamps are epoch MILLISECONDS. The seconds→ms
// conversion happens HERE, once, so every consumer downstream sees ms only.

import type { EventRunSourceKind } from '../../../services/eventRunsApi';
import type { SkillEventSubscription } from '../../../services/skillEventsApi';
import type { FileWatcherSubscription } from '../../../services/fileWatchersApi';
import type { ScheduleEventSubscription } from '../../../services/calendarApi';

export function sourceKindIcon(kind: EventRunSourceKind): string {
  switch (kind) {
    case 'skill_event':
      return 'mdi:lightning-bolt-outline';
    case 'file_watcher':
      return 'mdi:folder-eye-outline';
    case 'schedule':
      return 'mdi:calendar-clock';
    default:
      return 'mdi:bell-outline';
  }
}

export function sourceKindLabel(kind: EventRunSourceKind): string {
  switch (kind) {
    case 'skill_event':
      return 'Skill event';
    case 'file_watcher':
      return 'File watcher';
    case 'schedule':
      return 'Schedule';
    default:
      return kind;
  }
}

/**
 * Stable accent color for a rule, derived from its key so every card of the
 * same event shares one hue. Fixed S/L keeps it legible on both light and dark
 * surfaces (used as a thin left border, not a fill).
 */
export function accentColor(key: string): string {
  let h = 0;
  for (let i = 0; i < key.length; i++) {
    h = (h * 31 + key.charCodeAt(i)) >>> 0;
  }
  return `hsl(${h % 360}, 60%, 55%)`;
}

interface BoardSubscriptionBase {
  kind: EventRunSourceKind;
  id: string;
  /** `${kind}:${id}` — matches the run store's summary keys and grouping keys. */
  key: string;
  title: string;
  action: string;
  conversationId: string;
  createdAtMs: number;
  icon: string;
}

export interface SkillBoardSubscription extends BoardSubscriptionBase {
  kind: 'skill_event';
  skillName: string;
  eventType: string;
  /** The raw subscription, so rule actions can open the edit/simulate dialogs. */
  raw: SkillEventSubscription;
}

export interface FileWatcherBoardSubscription extends BoardSubscriptionBase {
  kind: 'file_watcher';
  armed: boolean;
  rootPath: string;
  raw: FileWatcherSubscription;
}

export interface ScheduleBoardSubscription extends BoardSubscriptionBase {
  kind: 'schedule';
  nextFireAtMs: number | null;
  rrule: string | null;
  scheduleStatus: ScheduleEventSubscription['status'];
  raw: ScheduleEventSubscription;
}

export type BoardSubscription =
  | SkillBoardSubscription
  | FileWatcherBoardSubscription
  | ScheduleBoardSubscription;

/** Rule-level actions a card's kebab menu can request; handled by TasksBoard. */
export type RuleAction =
  | 'edit'
  | 'simulate'
  | 'start-listener'
  | 'toggle-pause'
  | 'open-conversation'
  | 'delete';

export interface RuleActionPayload {
  action: RuleAction;
  sub: BoardSubscription;
}

export function fromSkillEvent(s: SkillEventSubscription): SkillBoardSubscription {
  return {
    kind: 'skill_event',
    id: s.id,
    key: `skill_event:${s.id}`,
    title: `${s.skill_name}: ${s.event_type}`,
    action: s.action,
    conversationId: s.conversation_id,
    createdAtMs: (s.created_at || 0) * 1000,
    icon: sourceKindIcon('skill_event'),
    skillName: s.skill_name,
    eventType: s.event_type,
    raw: s,
  };
}

export function fromFileWatcher(s: FileWatcherSubscription): FileWatcherBoardSubscription {
  return {
    kind: 'file_watcher',
    id: s.id,
    key: `file_watcher:${s.id}`,
    title: s.name || s.root_path,
    action: s.action,
    conversationId: s.conversation_id,
    createdAtMs: (s.created_at || 0) * 1000,
    icon: sourceKindIcon('file_watcher'),
    armed: s.armed,
    rootPath: s.root_path,
    raw: s,
  };
}

export function fromSchedule(s: ScheduleEventSubscription): ScheduleBoardSubscription {
  return {
    kind: 'schedule',
    id: s.id,
    key: `schedule:${s.id}`,
    title: s.title,
    action: s.action,
    conversationId: s.conversation_id,
    createdAtMs: (s.created_at || 0) * 1000,
    icon: sourceKindIcon('schedule'),
    nextFireAtMs: s.next_fire_at != null ? s.next_fire_at * 1000 : null,
    rrule: s.rrule,
    scheduleStatus: s.status,
    raw: s,
  };
}
