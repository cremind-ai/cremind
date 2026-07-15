import { defineStore } from 'pinia';

export type NotificationKind =
  | 'completed'
  | 'error'
  | 'channel_otp'
  | 'channel_subscribe_request'
  | 'skill_register_required'
  | 'event_run_pending'
  | 'event_run_completed'
  | 'event_run_failed';

export type NotificationPriority = 'high' | 'normal';

export interface NotificationEntry {
  id: string;
  conversationId: string;
  conversationTitle: string;
  messagePreview: string;
  kind: NotificationKind;
  priority: NotificationPriority;
  createdAt: number;
  seen: boolean;
  // Channel OTP context (only present when kind === 'channel_otp').
  channelType?: string;
  senderId?: string;
  senderName?: string;
  otp?: string;
  // Skill registration prompt (only present when kind === 'skill_register_required').
  skillId?: string;
  skillName?: string;
  // Event-run context (only present when kind starts with 'event_run_'). Used to
  // deep-link the notification to the Events-page run detail drawer.
  eventRunId?: string;
  sourceKind?: string;
}

interface NotificationsState {
  byProfile: Record<string, NotificationEntry[]>;
  hydrated: boolean;
}

const STORAGE_KEY = 'cremind.notifications.v1';
const MAX_PER_PROFILE = 50;

interface PersistedShape {
  v: 1;
  byProfile: Record<string, NotificationEntry[]>;
}

function loadFromStorage(): Record<string, NotificationEntry[]> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as PersistedShape;
    if (!parsed || parsed.v !== 1 || typeof parsed.byProfile !== 'object') return {};
    // Older entries (pre-priority) default to 'normal' so legacy data renders
    // under the "Other" section without crashing the type contract.
    for (const list of Object.values(parsed.byProfile)) {
      for (const entry of list) {
        if (entry.priority !== 'high' && entry.priority !== 'normal') {
          entry.priority = 'normal';
        }
      }
    }
    return parsed.byProfile;
  } catch {
    return {};
  }
}

function saveToStorage(byProfile: Record<string, NotificationEntry[]>) {
  try {
    const payload: PersistedShape = { v: 1, byProfile };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch (e) {
    console.warn('Failed to persist notifications:', e);
  }
}

export const useNotificationsStore = defineStore('notifications', {
  state: (): NotificationsState => ({
    byProfile: {},
    hydrated: false,
  }),

  getters: {
    forProfile(state) {
      return (profileId: string): NotificationEntry[] => {
        const list = state.byProfile[profileId] ?? [];
        return [...list].sort((a, b) => b.createdAt - a.createdAt);
      };
    },
    unreadCountForConversation(state) {
      return (profileId: string, conversationId: string): number => {
        const list = state.byProfile[profileId] ?? [];
        let n = 0;
        for (const e of list) {
          if (e.conversationId === conversationId && !e.seen) n++;
        }
        return n;
      };
    },
    hasErrorForConversation(state) {
      return (profileId: string, conversationId: string): boolean => {
        const list = state.byProfile[profileId] ?? [];
        return list.some(e => e.conversationId === conversationId && !e.seen && e.kind === 'error');
      };
    },
    totalUnread(state) {
      return (profileId: string): number => {
        const list = state.byProfile[profileId] ?? [];
        let n = 0;
        for (const e of list) if (!e.seen) n++;
        return n;
      };
    },
  },

  actions: {
    hydrate() {
      if (this.hydrated) return;
      this.byProfile = loadFromStorage();
      this.hydrated = true;
    },

    persist() {
      saveToStorage(this.byProfile);
    },

    push(profileId: string, entry: NotificationEntry) {
      this.hydrate();
      if (!this.byProfile[profileId]) this.byProfile[profileId] = [];
      this.byProfile[profileId].unshift(entry);
      if (this.byProfile[profileId].length > MAX_PER_PROFILE) {
        this.byProfile[profileId] = this.byProfile[profileId].slice(0, MAX_PER_PROFILE);
      }
      this.persist();
    },

    markSeenForConversation(profileId: string, conversationId: string) {
      this.hydrate();
      const list = this.byProfile[profileId];
      if (!list) return;
      let changed = false;
      for (const e of list) {
        if (e.conversationId === conversationId && !e.seen) {
          e.seen = true;
          changed = true;
        }
      }
      if (changed) this.persist();
    },

    markAllSeen(profileId: string) {
      this.hydrate();
      const list = this.byProfile[profileId];
      if (!list) return;
      let changed = false;
      for (const e of list) {
        if (!e.seen) {
          e.seen = true;
          changed = true;
        }
      }
      if (changed) this.persist();
    },

    dismiss(profileId: string, id: string) {
      this.hydrate();
      const list = this.byProfile[profileId];
      if (!list) return;
      const next = list.filter(e => e.id !== id);
      if (next.length !== list.length) {
        this.byProfile[profileId] = next;
        this.persist();
      }
    },

    clearForConversation(profileId: string, conversationId: string) {
      this.hydrate();
      const list = this.byProfile[profileId];
      if (!list) return;
      const next = list.filter(e => e.conversationId !== conversationId);
      if (next.length !== list.length) {
        this.byProfile[profileId] = next;
        this.persist();
      }
    },

    clearAll(profileId: string) {
      this.hydrate();
      if (!this.byProfile[profileId]) return;
      this.byProfile[profileId] = [];
      this.persist();
    },
  },
});
