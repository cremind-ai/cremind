import { defineStore } from 'pinia';
import { useChatStore, type TerminalAttachment } from './chat';
import { useSettingsStore } from './settings';
import { listTerminals, spawnTerminal } from '../services/terminalApi';
import type { FileWatchEvent } from '../services/filesApi';

// Guards restoreTerminals() to a single run per page load (module scope: the
// SPA loads this module once).
let _terminalsRestored = false;

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
  // Main-chat terminal list (the focus-null path — behavior unchanged). When a
  // ``focusConversationId`` is set (the event-run drawer), the ``openTerminals``
  // / ``activePid`` getters resolve to that run's isolated bucket instead, so
  // the run's terminals never leak into the main chat.
  globalTerminals: TerminalAttachment[];
  globalActivePid: string | null;
  // Set by the event-run detail drawer to point the whole workspace (cwd, file
  // tree, terminals) at a specific run's conversation instead of the active one.
  focusConversationId: string | null;
  focusTerminalsByConversation: Record<string, TerminalAttachment[]>;
  focusActivePidByConversation: Record<string, string | null>;
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
  // The user working directory the panel was seeded with — an allowed read
  // base on the backend. Unlike ``userDefaultCwd`` (which follows no-
  // conversation navigation), this stays fixed so the breadcrumb knows the
  // floor below which it must not navigate while no conversation is active:
  // without a conversation there's no cwd override to widen the backend's
  // read allowlist, so leaving this subtree would 403 and strand the tree.
  userWorkingRoot: string;
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
    globalTerminals: [],
    globalActivePid: null,
    focusConversationId: null,
    focusTerminalsByConversation: {},
    focusActivePidByConversation: {},
    minimized: true,
    collapsed: loadInitialCollapsed(),
    panelWidth: loadInitialWidth(),
    cwdByConversation: {},
    userDefaultCwd: '',
    userWorkingRoot: '',
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
    // The conversation the workspace (cwd, file tree, terminals) is scoped to:
    // the drawer's focused run when set, otherwise the active chat. Single
    // source of truth so the file-tree components can retarget their backend
    // read-allowlist scope in one place.
    scopeConversationId(state): string | null {
      if (state.focusConversationId) return state.focusConversationId;
      return useChatStore().activeConversationId ?? null;
    },
    // Terminal tabs for the current scope: the run's isolated bucket when
    // focused, else the main-chat global list (unchanged behavior).
    openTerminals(state): TerminalAttachment[] {
      const fid = state.focusConversationId;
      if (fid) return state.focusTerminalsByConversation[fid] ?? [];
      return state.globalTerminals;
    },
    activePid(state): string | null {
      const fid = state.focusConversationId;
      if (fid) return state.focusActivePidByConversation[fid] ?? null;
      return state.globalActivePid;
    },
    hasTerminals(): boolean {
      return this.openTerminals.length > 0;
    },
    // The effective cwd shown in the file tree. Follows the scope conversation
    // (focused run or active chat); falls back to the user default for the
    // no-conversation slot.
    cwd(state): string {
      const id = this.scopeConversationId;
      if (id && state.cwdByConversation[id]) {
        return state.cwdByConversation[id];
      }
      return state.userDefaultCwd;
    },
  },

  actions: {
    // Point the workspace at a specific run's conversation (drawer maximized),
    // or clear back to following the active chat (null).
    setFocusConversation(id: string | null) {
      this.focusConversationId = id;
    },

    // The terminal list + active-pid setter for the current scope. Focused →
    // the run's bucket; else the main-chat global list.
    _scopeTerminals(): TerminalAttachment[] {
      const fid = this.focusConversationId;
      if (fid) {
        if (!this.focusTerminalsByConversation[fid]) {
          this.focusTerminalsByConversation[fid] = [];
        }
        return this.focusTerminalsByConversation[fid];
      }
      return this.globalTerminals;
    },
    _setScopeActive(pid: string | null) {
      const fid = this.focusConversationId;
      if (fid) this.focusActivePidByConversation[fid] = pid;
      else this.globalActivePid = pid;
    },

    openTerminal(attachment: TerminalAttachment) {
      const list = this._scopeTerminals();
      if (!list.find(t => t.processId === attachment.processId)) {
        list.push({ ...attachment });
      }
      this._setScopeActive(attachment.processId);
      this.minimized = false;
    },

    // Spawn a new user-created interactive terminal on the backend and open it
    // as a tab. Independent of the agent — see services/terminalApi.ts. Opens
    // in the workspace's current cwd. Throws on failure (e.g. the per-profile
    // cap) so the caller can surface a message.
    async newTerminal() {
      const settings = useSettingsStore();
      const row = await spawnTerminal(settings.agentUrl, settings.authToken, {
        cwd: this.cwd || undefined,
      });
      this.openTerminal({
        processId: row.terminal_id,
        command: row.shell,
        commandShort: row.title,
        workingDirectory: row.working_dir,
        pty: true,
        kind: 'terminal',
      });
    },

    // Re-attach user terminals that survived a page reload. Runs once per page
    // load. Pushes tabs directly into the global list (not via openTerminal, so
    // it never forces the panel open) and swallows all errors — a failed
    // restore should never block startup.
    async restoreTerminals() {
      if (_terminalsRestored) return;
      _terminalsRestored = true;
      try {
        const settings = useSettingsStore();
        if (!settings.authToken) return;
        const { terminals } = await listTerminals(
          settings.agentUrl, settings.authToken,
        );
        for (const row of terminals) {
          if (this.globalTerminals.find(t => t.processId === row.terminal_id)) {
            continue;
          }
          this.globalTerminals.push({
            processId: row.terminal_id,
            command: row.shell,
            commandShort: row.title,
            workingDirectory: row.working_dir,
            pty: true,
            kind: 'terminal',
          });
        }
        if (this.globalActivePid === null && this.globalTerminals.length > 0) {
          this.globalActivePid = this.globalTerminals[0].processId;
        }
      } catch {
        /* restore is best-effort */
      }
    },

    setActive(pid: string) {
      if (this._scopeTerminals().some(t => t.processId === pid)) {
        this._setScopeActive(pid);
        this.minimized = false;
      }
    },

    closeTab(pid: string) {
      const list = this._scopeTerminals();
      const idx = list.findIndex(t => t.processId === pid);
      if (idx === -1) return;
      list.splice(idx, 1);
      if (this.activePid === pid) {
        const next = list[idx] || list[idx - 1] || null;
        this._setScopeActive(next ? next.processId : null);
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

    // Record the immutable working-dir root (the no-conversation navigation
    // floor — see the state field). Seeded from the same
    // ``GET /api/files/cwd`` value as the default cwd, but never moved by
    // navigation.
    setUserWorkingRoot(path: string) {
      if (!path || this.userWorkingRoot === path) return;
      this.userWorkingRoot = path;
    },

    // Drop a conversation's cached cwd + focused terminal bucket (e.g. when a
    // run/conversation gets deleted).
    forgetConversation(conversationId: string) {
      if (this.cwdByConversation[conversationId] !== undefined) {
        delete this.cwdByConversation[conversationId];
      }
      if (this.focusTerminalsByConversation[conversationId] !== undefined) {
        delete this.focusTerminalsByConversation[conversationId];
      }
      if (this.focusActivePidByConversation[conversationId] !== undefined) {
        delete this.focusActivePidByConversation[conversationId];
      }
      if (this.focusConversationId === conversationId) {
        this.focusConversationId = null;
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
