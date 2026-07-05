/**
 * API client for event runs — the per-trigger execution history.
 *
 * Each fired event rule (skill / file-watcher / schedule) runs in its own hidden
 * conversation, tracked by an `event_runs` row. Mirrors skillEventsApi.ts: each
 * call resolves the base URL from the active agent URL and attaches a Bearer
 * token.
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

export type EventRunSourceKind = 'skill_event' | 'file_watcher' | 'schedule';
export type EventRunStatus =
  | 'running'
  | 'pending'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface EventRunUsage {
  input_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  total_usd: number;
  request_count: number;
}

export interface EventRun {
  id: string;
  profile: string;
  source_kind: EventRunSourceKind;
  subscription_id: string;
  conversation_id: string | null;
  run_id: string | null;
  status: EventRunStatus;
  label: string;
  action: string;
  trigger_payload: Record<string, any> | null;
  pending_question: string | null;
  error: string | null;
  turn_count: number;
  usage: EventRunUsage;
  created_at: number; // epoch ms
  updated_at: number; // epoch ms
  finished_at: number | null; // epoch ms
}

export interface ListEventRunsQuery {
  source_kind?: EventRunSourceKind;
  subscription_id?: string;
  status?: EventRunStatus;
  limit?: number;
  offset?: number;
}

export async function listEventRuns(
  agentUrl: string,
  token: string,
  query: ListEventRunsQuery = {},
): Promise<{ runs: EventRun[]; total: number }> {
  const base = resolveBaseUrl(agentUrl);
  const params = new URLSearchParams();
  if (query.source_kind) params.set('source_kind', query.source_kind);
  if (query.subscription_id) params.set('subscription_id', query.subscription_id);
  if (query.status) params.set('status', query.status);
  if (query.limit != null) params.set('limit', String(query.limit));
  if (query.offset != null) params.set('offset', String(query.offset));
  const qs = params.toString();
  const res = await fetch(`${base}/api/event-runs${qs ? `?${qs}` : ''}`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to list event runs: ${res.statusText}`);
  return res.json();
}

export async function getEventRun(
  agentUrl: string,
  token: string,
  id: string,
): Promise<EventRun> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/event-runs/${encodeURIComponent(id)}`, {
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to get event run: ${res.statusText}`);
  }
  const data = await res.json();
  return data.run;
}

export async function deleteEventRun(
  agentUrl: string,
  token: string,
  id: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/event-runs/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to delete event run: ${res.statusText}`);
  }
}

export async function cancelEventRun(
  agentUrl: string,
  token: string,
  id: string,
): Promise<{ cancelled: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/event-runs/${encodeURIComponent(id)}/cancel`,
    { method: 'POST', headers: authHeaders(token) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to cancel event run: ${res.statusText}`);
  }
  return res.json();
}
