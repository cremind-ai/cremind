/**
 * API client for user-created interactive terminals.
 *
 * These are the terminals spawned by the Workspace panel's "New terminal"
 * button — independent of the agent's exec_shell Process Manager (see
 * processApi.ts). The URL/auth helpers mirror processApi.ts (duplicated per
 * the house style there); ``openTerminalSocket`` uses the same
 * ``Sec-WebSocket-Protocol`` bearer-token trick.
 */

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function resolveWsBaseUrl(agentUrl: string): string {
  const http = resolveBaseUrl(agentUrl);
  if (http.startsWith('https://')) return 'wss://' + http.slice('https://'.length);
  if (http.startsWith('http://')) return 'ws://' + http.slice('http://'.length);
  return http;
}

function authHeaders(token: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  return headers;
}

export interface TerminalRow {
  terminal_id: string;
  title: string;
  shell: string;
  working_dir: string;
  /** Unix seconds (wall clock) when the terminal was created. */
  created_at: number;
  status: 'running';
}

export class TerminalLimitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TerminalLimitError';
  }
}

export async function spawnTerminal(
  agentUrl: string,
  token: string,
  body?: { cwd?: string; cols?: number; rows?: number },
): Promise<TerminalRow> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/terminals`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(body || {}),
  });
  if (res.status === 409) {
    const data = await res.json().catch(() => ({}));
    throw new TerminalLimitError(data.error || 'Too many open terminals.');
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to create terminal: ${res.statusText}`);
  }
  return res.json();
}

export async function listTerminals(
  agentUrl: string,
  token: string,
): Promise<{ terminals: TerminalRow[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/terminals`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to list terminals: ${res.statusText}`);
  return res.json();
}

/** Terminate a terminal's shell. A 404 (already gone) is tolerated. */
export async function closeTerminal(
  agentUrl: string,
  token: string,
  tid: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/terminals/${encodeURIComponent(tid)}/close`,
    { method: 'POST', headers: authHeaders(token) },
  );
  if (!res.ok && res.status !== 404) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to close terminal: ${res.statusText}`);
  }
}

/**
 * Open a streaming WebSocket for a user terminal. The token is passed via the
 * ``Sec-WebSocket-Protocol`` subprotocol header because the browser WebSocket
 * constructor does not allow custom headers.
 */
export function openTerminalSocket(
  agentUrl: string,
  token: string,
  tid: string,
): WebSocket {
  if (!token) {
    throw new Error('Missing auth token — please log in before opening a terminal.');
  }
  const base = resolveWsBaseUrl(agentUrl);
  const url = `${base}/api/terminals/${encodeURIComponent(tid)}/ws`;
  return new WebSocket(url, ['bearer', token]);
}
