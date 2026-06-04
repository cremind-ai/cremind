// Opens a file from the agent's filesystem in a new browser tab. Used by
// both the list-view (FileTreeNode) and the icon grid (FileGridView) on
// double-click.
//
// The /api/files/open endpoint requires a JWT bearer header which a plain
// ``window.open`` can't carry, so we fetch the blob, materialise it as an
// object URL, and open that in a new tab.

import { fileOpenUrl } from '../services/filesApi';

const REVOKE_AFTER_MS = 60_000;

export async function openFileInNewTab(
  agentUrl: string,
  authToken: string,
  filePath: string,
  conversationId?: string,
): Promise<void> {
  const url = fileOpenUrl(agentUrl, filePath, conversationId);
  const res = await fetch(url, { headers: { Authorization: `Bearer ${authToken}` } });
  if (!res.ok) throw new Error(`Open failed: ${res.statusText}`);
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const win = window.open(objectUrl, '_blank');
  if (win) {
    setTimeout(() => URL.revokeObjectURL(objectUrl), REVOKE_AFTER_MS);
  } else {
    URL.revokeObjectURL(objectUrl);
  }
}
