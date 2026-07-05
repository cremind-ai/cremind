/**
 * Injection seam so a MessageBubble's terminal chips open into the surrounding
 * surface's terminal pane instead of the global Workspace panel.
 *
 * The main chat provides nothing → MessageBubble falls back to the global
 * `terminalPanel` store (unchanged behavior). The event-run detail drawer
 * provides a handler that maximizes the drawer and opens the terminal in the
 * run-focused workspace (the terminal store buckets it under the focused run,
 * so it never leaks into the main chat).
 */
import type { InjectionKey } from 'vue';
import type { TerminalAttachment } from '../stores/chat';

export const OpenTerminalKey: InjectionKey<(term: TerminalAttachment) => void> =
  Symbol('openTerminal');
