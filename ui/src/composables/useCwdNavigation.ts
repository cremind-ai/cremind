// Shared CWD-navigation logic for the file tree.
//
// Used by both the breadcrumb (jump anywhere) and the icon grid view
// (double-click into a folder). Handles the optimistic update + rollback
// against the conversation cwd, and the no-conversation fallback that
// updates the user-default cwd locally without hitting the backend.

import { useChatStore } from '../stores/chat';
import { useSettingsStore } from '../stores/settings';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import { setConversationCwd, DirectoryAccessError } from '../services/filesApi';

export interface NavigateResult {
  ok: boolean;
  error?: string;
}

export function useCwdNavigation() {
  const chat = useChatStore();
  const settings = useSettingsStore();
  const panel = useTerminalPanelStore();

  // Is ``target`` within the pinned user working root? Used as the no-
  // conversation navigation floor. Fails open when the root is unknown
  // (pre-seed) — the backend still enforces the real allowlist.
  function isWithinRoot(target: string): boolean {
    const root = panel.userWorkingRoot;
    if (!root || target === root) return true;
    const sep = root.includes('\\') ? '\\' : '/';
    const base = root.endsWith(sep) ? root : root + sep;
    return target.startsWith(base);
  }

  // Whether the panel may navigate to ``target``. With an active conversation
  // the backend widens its read allowlist via the per-conversation cwd
  // override, so anywhere reachable on disk is fair game. Without one there's
  // no override to set, so navigation must stay inside the user working dir
  // (an allowed base) or the backend would 403 and strand the tree.
  function canNavigateTo(target: string): boolean {
    if (chat.activeConversationId) return true;
    return isWithinRoot(target);
  }

  async function navigate(newPath: string): Promise<NavigateResult> {
    if (!newPath) return { ok: false, error: 'empty path' };
    const conversationId = chat.activeConversationId;
    const previous = panel.cwd;
    if (previous === newPath) return { ok: true };

    // Refuse to leave the working-dir subtree when there's no conversation to
    // anchor a cwd override — otherwise the tree lands on an unlistable path.
    if (!conversationId && !isWithinRoot(newPath)) {
      return {
        ok: false,
        error: 'Start a conversation to browse outside the working folder',
      };
    }

    // Optimistic local update so the tree refresh kicks off immediately.
    if (conversationId) {
      panel.setConversationCwd(conversationId, newPath);
    } else {
      panel.setUserDefaultCwd(newPath);
    }

    // No conversation → no agent to sync; we're done.
    if (!conversationId) return { ok: true };

    try {
      await setConversationCwd(
        settings.agentUrl,
        settings.authToken,
        conversationId,
        newPath,
      );
      return { ok: true };
    } catch (e: unknown) {
      // Roll back the optimistic update.
      panel.setConversationCwd(conversationId, previous);
      const message =
        e instanceof DirectoryAccessError
          ? e.message
          : (e as Error)?.message || 'Failed to change directory';
      return { ok: false, error: message };
    }
  }

  return { navigate, canNavigateTo };
}
