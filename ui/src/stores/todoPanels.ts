/**
 * Window-manager store for the floating todo panels.
 *
 * Replaces the old singleton todo panel (one per active conversation) with a
 * multi-window system: several panels can be open at once, overlapping like OS
 * windows. Each panel owns its own `TodoState` snapshot so the store has NO
 * runtime dependency on the chat store (the `import type` below is erased at
 * compile time) — chat/eventRuns/the event-run watcher push into it, it never
 * reads back.
 *
 * Three panel sources:
 *  - 'live'      — a plan-mode run in a normal conversation (key `live:<cid>`).
 *                  Auto-opens on the first `todos` frame, closes when the run
 *                  finishes with everything done (the message chip reopens it).
 *  - 'message'   — a frozen per-turn snapshot reopened from a completed bubble's
 *                  chip (key `msg:<serverMessageId>`). One per completed request
 *                  = per-conversation history.
 *  - 'event-run' — a live event-triggered run (schedule / file watcher / skill
 *                  event), key `run:<eventRunId>`. Each firing is its own hidden
 *                  conversation + run id, so each gets an independent panel.
 */

import { defineStore } from 'pinia';
import { nextTick } from 'vue';
import type { TodoItem, TodoState } from './chat';
import { changedTodoIds, allTodosCompleted } from '../utils/todos';

export type PanelSource = 'live' | 'message' | 'event-run';
export type PanelStatus = 'running' | 'completed' | 'idle';

export interface TodoPanelWindow {
  key: string;
  source: PanelSource;
  conversationId: string;
  messageId?: string;
  eventRunId?: string;
  title: string;
  subtitle?: string;
  state: TodoState;
  status: PanelStatus;
  x: number;
  y: number;
  zIndex: number;
  minimized: boolean;
  maximized: boolean;
  openedAt: number;
  // One-shot animation origin (chip center, viewport coords) consumed by the
  // enter transition, then nulled so a later focus doesn't re-grow from it.
  origin: { x: number; y: number } | null;
  // Set just before removal so the leave transition collapses the panel toward
  // this point (the chip on the final bubble). Null → a plain fade/scale exit.
  exitOrigin: { x: number; y: number } | null;
}

/** Payload for opening/updating a panel. */
export interface PanelDescriptor {
  key: string;
  source: PanelSource;
  conversationId: string;
  messageId?: string;
  eventRunId?: string;
  title: string;
  subtitle?: string;
  todos: TodoItem[];
}

interface UpsertOpts {
  origin?: { x: number; y: number } | null;
  // Chip click / explicit open: focus + un-minimize an existing panel. Live
  // updates leave `reveal` false so a background run never yanks focus.
  reveal?: boolean;
}

interface State {
  panels: TodoPanelWindow[];
  zCounter: number;
  cascadeIndex: number;
  // Top inset for placement/clamping (Electron titlebar pushes it down).
  viewportTop: number;
}

// Panel-key builders — kept as exports so callers never hand-format keys.
export const livePanelKey = (cid: string): string => `live:${cid}`;
export const messagePanelKey = (messageId: string): string => `msg:${messageId}`;
export const runPanelKey = (eventRunId: string): string => `run:${eventRunId}`;

const PANEL_W = 300;
const MARGIN = 16;
const CASCADE_STEP = 28;
const CASCADE_WRAP = 6;
const MAX_PANELS = 8;
const MIN_VISIBLE = 56; // px of the panel that must stay on-screen (grab handle)
const Z_BASE = 10;
const Z_RENORM_CEIL = 100000;

function makeState(items: TodoItem[]): TodoState {
  return { items, changedIds: items.map(t => t.id), updateSeq: 1 };
}

function statusForNew(source: PanelSource, items: TodoItem[]): PanelStatus {
  if (source === 'message') {
    return allTodosCompleted(items) ? 'completed' : 'idle';
  }
  return 'running';
}

export const useTodoPanelsStore = defineStore('todoPanels', {
  state: (): State => ({
    panels: [],
    zCounter: Z_BASE,
    cascadeIndex: 0,
    viewportTop: 12,
  }),

  getters: {
    /** Key of the top-most (highest z) panel, or null when none are open. */
    focusedKey(state): string | null {
      let top: TodoPanelWindow | null = null;
      for (const p of state.panels) {
        if (!top || p.zIndex > top.zIndex) top = p;
      }
      return top?.key ?? null;
    },
    hasPanels(state): boolean {
      return state.panels.length > 0;
    },
  },

  actions: {
    /** Layer sets this on mount so placement clears the Electron titlebar. */
    configureViewport(top: number) {
      this.viewportTop = top;
    },

    find(key: string): TodoPanelWindow | undefined {
      return this.panels.find(p => p.key === key);
    },

    /**
     * Open a panel, or update it in place if one with the same key exists.
     * Idempotent: safe to call on every `todos` frame. New panels cascade in
     * at the top of the z-stack; existing panels only re-focus when
     * `opts.reveal` is set (chip click / explicit open).
     */
    upsertPanel(desc: PanelDescriptor, opts: UpsertOpts = {}) {
      const existing = this.find(desc.key);
      if (existing) {
        const changed = changedTodoIds(existing.state.items, desc.todos);
        existing.state = {
          items: desc.todos,
          changedIds: changed,
          updateSeq: existing.state.updateSeq + 1,
        };
        if (desc.title) existing.title = desc.title;
        if (desc.subtitle !== undefined) existing.subtitle = desc.subtitle;
        if (desc.messageId) existing.messageId = desc.messageId;
        if (desc.eventRunId) existing.eventRunId = desc.eventRunId;
        // A fresh update on a message snapshot keeps its derived status; live /
        // event-run panels stay 'running' until markCompleted/markStopped.
        if (existing.source === 'message') {
          existing.status = allTodosCompleted(desc.todos) ? 'completed' : 'idle';
        }
        if (opts.reveal) {
          existing.minimized = false;
          this.focusPanel(existing.key);
        }
        return;
      }

      this.enforceCap();
      const pos = this.cascadePosition();
      this.zCounter += 1;
      this.panels.push({
        key: desc.key,
        source: desc.source,
        conversationId: desc.conversationId,
        messageId: desc.messageId,
        eventRunId: desc.eventRunId,
        title: desc.title || 'Tasks',
        subtitle: desc.subtitle,
        state: makeState(desc.todos),
        status: statusForNew(desc.source, desc.todos),
        x: pos.x,
        y: pos.y,
        zIndex: this.zCounter,
        minimized: false,
        maximized: false,
        openedAt: Date.now(),
        origin: opts.origin ?? null,
        exitOrigin: null,
      });
    },

    /** Mark a run's panel done (green, stops the spinner). Stays until closed. */
    markCompleted(key: string) {
      const p = this.find(key);
      if (p) p.status = 'completed';
    },

    /** Mark a run stopped/interrupted (partial todos): stop the spinner, keep it. */
    markStopped(key: string) {
      const p = this.find(key);
      if (p) p.status = 'idle';
    },

    setPosition(key: string, x: number, y: number) {
      const p = this.find(key);
      if (!p) return;
      const c = this.clampPosition(x, y);
      p.x = c.x;
      p.y = c.y;
    },

    setMinimized(key: string, minimized: boolean) {
      const p = this.find(key);
      if (!p) return;
      p.minimized = minimized;
      if (!minimized) this.focusPanel(key);
    },

    setMaximized(key: string, maximized: boolean) {
      const p = this.find(key);
      if (p) p.maximized = maximized;
    },

    /** Clear the one-shot grow-from-chip origin once its enter transition ends. */
    clearOrigin(key: string) {
      const p = this.find(key);
      if (p) p.origin = null;
    },

    /** Bring a panel to the front (monotonic z; renormalize before overflow). */
    focusPanel(key: string) {
      const p = this.find(key);
      if (!p) return;
      this.zCounter += 1;
      p.zIndex = this.zCounter;
      if (this.zCounter > Z_RENORM_CEIL) this.renormalizeZ();
    },

    renormalizeZ() {
      const sorted = [...this.panels].sort((a, b) => a.zIndex - b.zIndex);
      sorted.forEach((p, i) => {
        p.zIndex = Z_BASE + i;
      });
      this.zCounter = Z_BASE + sorted.length;
    },

    /**
     * Close a panel. With `opts.toward` (chip center, viewport coords) the panel
     * first records an exit origin and removes on the next tick, so the leave
     * transition collapses it toward the chip; otherwise it's removed at once.
     */
    closePanel(key: string, opts: { toward?: { x: number; y: number } | null } = {}) {
      const p = this.find(key);
      if (!p) return;
      if (opts.toward) {
        p.exitOrigin = opts.toward;
        // Let the exit-origin CSS vars flush onto the element before removal so
        // the TransitionGroup leave animation picks them up.
        nextTick(() => this._removePanel(key));
        return;
      }
      this._removePanel(key);
    },

    _removePanel(key: string) {
      const idx = this.panels.findIndex(p => p.key === key);
      if (idx === -1) return;
      this.panels.splice(idx, 1);
      if (this.panels.length === 0) this.cascadeIndex = 0;
    },

    // ── lifecycle hooks (called from chat / eventRuns stores) ─────────────
    closeForConversation(conversationId: string) {
      this.panels
        .filter(p => p.conversationId === conversationId)
        .forEach(p => this.closePanel(p.key));
    },

    closeForEventRun(eventRunId: string) {
      this.panels
        .filter(p => p.eventRunId === eventRunId)
        .forEach(p => this.closePanel(p.key));
    },

    /** A conversation was assigned a new id — rewrite panels keyed by the old one. */
    remapConversation(oldId: string, newId: string) {
      for (const p of this.panels) {
        if (p.conversationId !== oldId) continue;
        p.conversationId = newId;
        if (p.key === livePanelKey(oldId)) p.key = livePanelKey(newId);
      }
    },

    closeAll() {
      this.panels = [];
      this.cascadeIndex = 0;
      this.zCounter = Z_BASE;
    },

    /** Re-clamp every panel after a viewport resize / titlebar change. */
    clampAllToViewport() {
      for (const p of this.panels) {
        const c = this.clampPosition(p.x, p.y);
        p.x = c.x;
        p.y = c.y;
      }
    },

    // ── internals ────────────────────────────────────────────────────────
    enforceCap() {
      while (this.panels.length >= MAX_PANELS) {
        const evictable = this.panels
          .filter(p => p.status !== 'running')
          .sort((a, b) => a.zIndex - b.zIndex);
        // All open panels are live/running — allow the overflow rather than
        // killing an in-flight run's panel.
        if (!evictable.length) break;
        this.closePanel(evictable[0].key);
      }
    },

    cascadePosition(): { x: number; y: number } {
      const vw = typeof window !== 'undefined' ? window.innerWidth : 1280;
      const i = this.cascadeIndex % CASCADE_WRAP;
      this.cascadeIndex += 1;
      const top = this.viewportTop + 56;
      if (vw <= 640) {
        return { x: 12, y: top + i * CASCADE_STEP };
      }
      return {
        x: Math.max(MARGIN, vw - PANEL_W - MARGIN - i * CASCADE_STEP),
        y: top + i * CASCADE_STEP,
      };
    },

    clampPosition(x: number, y: number): { x: number; y: number } {
      const vw = typeof window !== 'undefined' ? window.innerWidth : 1280;
      const vh = typeof window !== 'undefined' ? window.innerHeight : 800;
      const clampedX = Math.min(
        Math.max(x, -(PANEL_W - MIN_VISIBLE)),
        vw - MIN_VISIBLE,
      );
      const clampedY = Math.min(Math.max(y, this.viewportTop), vh - 40);
      return { x: clampedX, y: clampedY };
    },
  },
});
