/**
 * API client for per-file Google Drive access.
 *
 * Mirrors calendarApi.ts: each call resolves the base URL from the active agent
 * URL and attaches a Bearer token. Cremind holds the `drive.file` scope, so the
 * "granted files" list is whatever Google says the token can reach — there is no
 * local grant registry to keep in sync.
 */

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(token: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

export interface DriveStatus {
  linked: boolean;
  email: string | null;
  scopes: string[];
  expected_scopes: string[];
  scopes_stale: boolean;
  /** True when the token still carries the broad scope, so every file is reachable. */
  whole_drive?: boolean;
  /** Server-computed label: which access model this account has, and why. */
  access_model?: string;
  local_capture: boolean;
  capture_hint: string | null;
  revoke_url: string;
  hint?: string;
  access_note?: string;
}

export interface DriveFile {
  id: string;
  name: string;
  mime_type: string;
  modified_time?: string;
  web_view_link?: string;
  size?: string;
  origin?: string;
}

export interface DriveFilePage {
  files: DriveFile[];
  next_page_token?: string | null;
  error?: string;
  message?: string;
}

export interface DriveGrantStart {
  authorize_url?: string;
  state?: string;
  capture_hint?: string | null;
  local_capture?: boolean;
  error?: string;
  message?: string;
}

export interface DriveGrantResult {
  status: 'pending' | 'captured' | 'completed' | 'error' | 'unknown' | 'timeout';
  files: DriveFile[];
  unverified?: string[];
  note?: string;
  error?: string;
}

export async function getDriveStatus(agentUrl: string, token: string): Promise<DriveStatus> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/drive/status`, { headers: authHeaders(token) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || data.error || `Failed to load status: ${res.statusText}`);
  return data;
}

export async function listDriveFiles(
  agentUrl: string, token: string, pageToken?: string,
): Promise<DriveFilePage> {
  const base = resolveBaseUrl(agentUrl);
  const params = pageToken ? `?page_token=${encodeURIComponent(pageToken)}` : '';
  const res = await fetch(`${base}/api/drive/files${params}`, { headers: authHeaders(token) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // 409 means "not linked yet" or a dead token — a state to display, not throw on.
    return { files: [], error: data.error || 'error', message: data.message || res.statusText };
  }
  return data;
}

export async function startDriveGrant(
  agentUrl: string,
  token: string,
  options: { fileIds?: string[]; allowFolders?: boolean } = {},
): Promise<DriveGrantStart> {
  const base = resolveBaseUrl(agentUrl);
  const body: Record<string, unknown> = {
    allow_multiple: true,
    allow_folders: options.allowFolders !== false,
  };
  if (options.fileIds?.length) body.file_ids = options.fileIds;
  const res = await fetch(`${base}/api/drive/grants`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    return { error: data.error || 'error', message: data.message || res.statusText };
  }
  return data;
}

export async function getDriveGrant(
  agentUrl: string, token: string, state: string,
): Promise<DriveGrantResult> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/drive/grants/${encodeURIComponent(state)}`, {
    headers: authHeaders(token),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || data.error || `Failed to poll grant: ${res.statusText}`);
  return data;
}

export async function completeDriveGrant(
  agentUrl: string, token: string, redirectUrl: string,
): Promise<DriveGrantResult> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/drive/grants/complete`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ redirect_url: redirectUrl }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || data.error || `Failed to complete grant: ${res.statusText}`);
  return data;
}

export async function cancelDriveGrant(
  agentUrl: string, token: string, state: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  await fetch(`${base}/api/drive/grants/${encodeURIComponent(state)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  }).catch(() => undefined);
}
