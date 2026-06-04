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

  async function navigate(newPath: string): Promise<NavigateResult> {
    if (!newPath) return { ok: false, error: 'empty path' };
    const conversationId = chat.activeConversationId;
    const previous = panel.cwd;
    if (previous === newPath) return { ok: true };

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

  return { navigate };
}
