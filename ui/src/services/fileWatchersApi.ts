/**
 * API client for the conversation-scoped file watcher subscription system.
 *
 * Mirrors skillEventsApi.ts: each call resolves the base URL from the active
 * agent URL and attaches a Bearer token from settings.
 */

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(token: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  return headers;
}

export interface FileWatcherSubscription {
  id: string;
  conversation_id: string;
  conversation_title: string;
  profile: string;
  name: string;
  root_path: string;
  recursive: boolean;
  target_kind: 'file' | 'folder' | 'any';
  event_types: string;       // comma-joined "created,modified,deleted,moved"
  extensions: string;        // comma-joined ".py,.md" (empty = all)
  action: string;
  armed: boolean;
  created_at: number;
}

export interface FileWatcherCreatePayload {
  path?: string;
  name?: string;
  triggers?: string[];
  target_kind?: 'file' | 'folder' | 'any';
  extensions?: string[];
  recursive?: boolean;
  action: string;
  conversation_id?: string;
}

export interface FileWatcherUpdatePayload {
  path?: string;
  name?: string;
  triggers?: string[];
  target_kind?: 'file' | 'folder' | 'any';
  extensions?: string[];
  recursive?: boolean;
  action?: string;
}

export async function listFileWatchers(
  agentUrl: string,
  token: string,
): Promise<{ subscriptions: FileWatcherSubscription[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/file-watchers`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to list file watchers: ${res.statusText}`);
  return res.json();
}

export async function deleteFileWatcher(
  agentUrl: string,
  token: string,
  id: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/file-watchers/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to delete file watcher: ${res.statusText}`);
  }
}

export async function createFileWatcher(
  agentUrl: string,
  token: string,
  payload: FileWatcherCreatePayload,
): Promise<FileWatcherSubscription> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/file-watchers`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.message || data.error || `Failed to create file watcher: ${res.statusText}`);
  }
  return data;
}

export async function updateFileWatcher(
  agentUrl: string,
  token: string,
  id: string,
  payload: FileWatcherUpdatePayload,
): Promise<FileWatcherSubscription> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/file-watchers/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: authHeaders(token),
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.message || data.error || `Failed to update file watcher: ${res.statusText}`);
  }
  return data;
}
