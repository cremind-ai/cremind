import { defineStore } from 'pinia';
import { useChatStore, type TerminalAttachment } from './chat';
import type { FileWatchEvent } from '../services/filesApi';

const WIDTH_STORAGE_KEY = 'terminalPanelWidth';
const SPLIT_RATIO_STORAGE_KEY = 'rightPanelSplitRatio';
const HIDDEN_FILES_STORAGE_KEY = 'rightPanelShowHidden';
const VIEW_MODE_STORAGE_KEY = 'rightPanelViewMode';
const COLLAPSED_STORAGE_KEY = 'rightPanelCollapsed';
const MIN_PANEL_WIDTH = 280;
const MIN_CHAT_WIDTH = 320;
const DEFAULT_PANEL_WIDTH = 480;
const DEFAULT_SPLIT_RATIO = 0.5;
const MIN_SPLIT_RATIO = 0.15;
const MAX_SPLIT_RATIO = 0.85;

function loadInitialWidth(): number {
  const raw = Number(localStorage.getItem(WIDTH_STORAGE_KEY));
  return Number.isFinite(raw) && raw >= MIN_PANEL_WIDTH ? raw : DEFAULT_PANEL_WIDTH;
}

function loadInitialSplitRatio(): number {
  const raw = Number(localStorage.getItem(SPLIT_RATIO_STORAGE_KEY));
  if (Number.isFinite(raw) && raw >= MIN_SPLIT_RATIO && raw <= MAX_SPLIT_RATIO) {
    return raw;
  }
  return DEFAULT_SPLIT_RATIO;
}

function loadInitialShowHidden(): boolean {
  return localStorage.getItem(HIDDEN_FILES_STORAGE_KEY) === '1';
}

export type FileViewMode = 'list' | 'icon';

function loadInitialViewMode(): FileViewMode {
  return localStorage.getItem(VIEW_MODE_STORAGE_KEY) === 'icon' ? 'icon' : 'list';
}

function loadInitialCollapsed(): boolean {
  return localStorage.getItem(COLLAPSED_STORAGE_KEY) === '1';
}

interface State {
  openTerminals: TerminalAttachment[];
  activePid: string | null;
  minimized: boolean;
  collapsed: boolean;
  panelWidth: number;
  // Effective working directory per conversation. The map is populated from
  // the conversation SSE stream's ``ready`` and ``cwd`` events; the file
  // tree reads its current value via the ``cwd`` getter, which follows the
  // chat store's active conversation.
  cwdByConversation: Record<string, string>;
  // Fallback cwd used when no conversation is active (e.g. the brand-new
  // chat slot before the user sends their first message). Seeded once from
  // ``GET /api/files/cwd``.
  userDefaultCwd: string;
  splitRatio: number;
  showHiddenFiles: boolean;
  viewMode: FileViewMode;
  selectedFilePath: string | null;
  // Latest filesystem-watch event from the watchdog SSE stream. Tree
  // components subscribe to this and refetch their managed directory when
  // the event's parent path matches.
  lastFileEvent: FileWatchEvent | null;
}

export const useTerminalPanelStore = defineStore('terminalPanel', {
  state: (): State => ({
    openTerminals: [],
    activePid: null,
    minimized: true,
    collapsed: loadInitialCollapsed(),
    panelWidth: loadInitialWidth(),
    cwdByConversation: {},
    userDefaultCwd: '',
    splitRatio: loadInitialSplitRatio(),
    showHiddenFiles: loadInitialShowHidden(),
    viewMode: loadInitialViewMode(),
    selectedFilePath: null,
    lastFileEvent: null,
  }),

  getters: {
    visible(state): boolean {
      return !state.minimized;
    },
    hasTerminals(state): boolean {
      return state.openTerminals.length > 0;
    },
    // The effective cwd shown in the file tree. Follows the active
    // conversation; falls back to the user default for the no-conversation
    // slot. Re-evaluates whenever ``activeConversationId`` or the per-
    // conversation map entry changes.
    cwd(state): string {
      const chatStore = useChatStore();
      const id = chatStore.activeConversationId;
      if (id && state.cwdByConversation[id]) {
        return state.cwdByConversation[id];
      }
      return state.userDefaultCwd;
    },
  },

  actions: {
    openTerminal(attachment: TerminalAttachment) {
      const existing = this.openTerminals.find(t => t.processId === attachment.processId);
      if (!existing) {
        this.openTerminals.push({ ...attachment });
      }
      this.activePid = attachment.processId;
      this.minimized = false;
    },

    setActive(pid: string) {
      if (this.openTerminals.some(t => t.processId === pid)) {
        this.activePid = pid;
        this.minimized = false;
      }
    },

    closeTab(pid: string) {
      const idx = this.openTerminals.findIndex(t => t.processId === pid);
      if (idx === -1) return;
      this.openTerminals.splice(idx, 1);
      if (this.activePid === pid) {
        const next = this.openTerminals[idx] || this.openTerminals[idx - 1] || null;
        this.activePid = next ? next.processId : null;
      }
      // Tree stays visible after the last terminal closes — don't auto-minimize.
    },

    minimize() {
      this.minimized = true;
    },

    restore() {
      this.minimized = false;
    },

    setCollapsed(b: boolean) {
      this.collapsed = b;
      try {
        localStorage.setItem(COLLAPSED_STORAGE_KEY, b ? '1' : '0');
      } catch {
        /* noop */
      }
    },

    toggleCollapsed() {
      this.setCollapsed(!this.collapsed);
    },

    setWidth(px: number) {
      const maxWidth = Math.max(MIN_PANEL_WIDTH, window.innerWidth - MIN_CHAT_WIDTH);
      const clamped = Math.min(Math.max(px, MIN_PANEL_WIDTH), maxWidth);
      this.panelWidth = clamped;
      try {
        localStorage.setItem(WIDTH_STORAGE_KEY, String(clamped));
      } catch {
        // localStorage may be unavailable (private mode); ignore.
      }
    },

    // Record (or update) the effective cwd for a specific conversation.
    // Called from the chat store's SSE handler on ``ready`` and ``cwd``
    // events.
    setConversationCwd(conversationId: string, path: string) {
      if (!conversationId || !path) return;
      if (this.cwdByConversation[conversationId] === path) return;
      this.cwdByConversation[conversationId] = path;
    },

    // Set the fallback cwd used when no conversation is active. Seeded by
    // FileTreePanel from ``GET /api/files/cwd`` on first mount.
    setUserDefaultCwd(path: string) {
      if (!path || this.userDefaultCwd === path) return;
      this.userDefaultCwd = path;
    },

    // Drop a conversation's cached cwd (e.g. when it gets deleted).
    forgetConversation(conversationId: string) {
      if (this.cwdByConversation[conversationId] !== undefined) {
        delete this.cwdByConversation[conversationId];
      }
    },

    setSplitRatio(r: number) {
      const clamped = Math.min(Math.max(r, MIN_SPLIT_RATIO), MAX_SPLIT_RATIO);
      this.splitRatio = clamped;
      try {
        localStorage.setItem(SPLIT_RATIO_STORAGE_KEY, String(clamped));
      } catch {
        /* noop */
      }
    },

    setShowHidden(b: boolean) {
      this.showHiddenFiles = b;
      try {
        localStorage.setItem(HIDDEN_FILES_STORAGE_KEY, b ? '1' : '0');
      } catch {
        /* noop */
      }
    },

    toggleHidden() {
      this.setShowHidden(!this.showHiddenFiles);
    },

    setViewMode(mode: FileViewMode) {
      this.viewMode = mode;
      try {
        localStorage.setItem(VIEW_MODE_STORAGE_KEY, mode);
      } catch {
        /* noop */
      }
    },

    toggleViewMode() {
      this.setViewMode(this.viewMode === 'list' ? 'icon' : 'list');
    },

    setSelectedFile(path: string | null) {
      this.selectedFilePath = path;
    },

    pushFileEvent(ev: FileWatchEvent) {
      this.lastFileEvent = ev;
    },
  },
});
