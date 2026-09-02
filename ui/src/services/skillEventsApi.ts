/**
 * API client for the conversation-scoped skill event subscription system.
 *
 * Mirrors processApi.ts: each call resolves the base URL from the active
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

/** Lifecycle of a one-shot event task. `null` for a standing subscription. */
export type EventTaskStatus =
  | 'active'      // armed, waiting for its single occurrence
  | 'triggered'   // fired; its run is in flight
  | 'completed'   // its one run reported back and the task ended
  | 'cancelled'
  | 'timed_out';  // the awaited event never happened before the deadline

export interface SkillEventSubscription {
  id: string;
  conversation_id: string;
  conversation_title: string;
  profile: string;
  skill_name: string;
  event_type: string;
  action: string;
  created_at: number;
  paused: boolean;
  /**
   * ONE-SHOT task: waits for the next matching event only, runs once, reports,
   * then ends (optionally giving up at `timeout_at`). Reporting is not what
   * makes it a task — a standing subscription fires forever and reports EVERY
   * run's result back into `conversation_id` too.
   */
  task: boolean;
  task_status: EventTaskStatus | null;
  timeout_at: number | null;   // epoch SECONDS (event runs use ms)
  completed_at: number | null; // epoch seconds
}

export interface SkillEventDeclaration {
  name: string;
  description?: string;
}

export interface SkillEventsInfo {
  skill_name: string;
  source_dir: string;
  events: SkillEventDeclaration[];
}

export interface ListenerStatus {
  skill_name: string;
  running: boolean;
  last_heartbeat: number | null;
  autostart_id: string | null;
  command: string;
}

export interface EventNotificationEntry {
  id: string;
  profile: string;
  conversation_id: string;
  conversation_title: string;
  message_preview: string;
  kind:
    | 'started'
    | 'completed'
    | 'error'
    | 'channel_otp'
    | 'channel_subscribe_request'
    | 'channel_group_request'
    | 'channel_group_brake'
    | 'skill_register_required'
    | 'event_run_pending'
    | 'event_run_completed'
    | 'event_run_failed';
  priority?: 'high' | 'normal';
  created_at: number;
  // Event-run extras (when kind starts with 'event_run_').
  event_run_id?: string;
  source_kind?: string;
  subscription_id?: string;
  // Channel-specific extras when kind === 'channel_otp'.
  channel_id?: string;
  channel_type?: string;
  sender_id?: string;
  sender_name?: string;
  // Set on a channel_group_request that names no single group: the account
  // joined somewhere with many rooms (a Discord server), so the operator picks
  // rather than approves.
  pick?: boolean;
  otp?: string;
  // Channel group-chat extras (kind === 'channel_group_request' | 'channel_group_brake').
  group_id?: string;
  group_title?: string;
  platform_chat_id?: string;
  status?: string;
  discovered_via?: string;
  brake?: string;
  // Skill-registration extras when kind === 'skill_register_required'.
  skill_id?: string;
  skill_name?: string;
}

export async function listSubscriptions(
  agentUrl: string,
  token: string,
): Promise<{ subscriptions: SkillEventSubscription[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/skill-events`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to list subscriptions: ${res.statusText}`);
  return res.json();
}

export async function deleteSubscription(
  agentUrl: string,
  token: string,
  id: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/skill-events/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to delete subscription: ${res.statusText}`);
  }
}

export async function updateSubscription(
  agentUrl: string,
  token: string,
  id: string,
  fields: {
    event_type?: string;
    action?: string;
    paused?: boolean;
    /** Tasks only: minutes from now, or null to wait indefinitely. */
    timeout_minutes?: number | null;
  },
): Promise<SkillEventSubscription> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/skill-events/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: authHeaders(token),
    body: JSON.stringify(fields),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.message || data.error || `Failed to update subscription: ${res.statusText}`);
  }
  return data;
}

export async function simulateEvent(
  agentUrl: string,
  token: string,
  id: string,
  content: string,
  filename?: string,
): Promise<{ ok: boolean; path: string; warnings?: string[] }> {
  const base = resolveBaseUrl(agentUrl);
  const body: Record<string, string> = { content };
  if (filename && filename.trim()) {
    body.filename = filename.trim();
  }
  const res = await fetch(`${base}/api/skill-events/${encodeURIComponent(id)}/simulate`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.message || data.error || `Failed to simulate: ${res.statusText}`);
  }
  return data;
}

export async function getSkillEvents(
  agentUrl: string,
  token: string,
  skillName: string,
): Promise<SkillEventsInfo> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/skills/${encodeURIComponent(skillName)}/events`,
    { headers: authHeaders(token) },
  );
  if (!res.ok) throw new Error(`Failed to get skill events: ${res.statusText}`);
  return res.json();
}

export async function getListenerStatus(
  agentUrl: string,
  token: string,
  skillName: string,
): Promise<ListenerStatus> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/skills/${encodeURIComponent(skillName)}/listener-status`,
    { headers: authHeaders(token) },
  );
  if (!res.ok) throw new Error(`Failed to get listener status: ${res.statusText}`);
  return res.json();
}

export async function startListener(
  agentUrl: string,
  token: string,
  skillName: string,
): Promise<{ ok: boolean; process_id: string; autostart_id: string }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/skills/${encodeURIComponent(skillName)}/listener-start`,
    { method: 'POST', headers: authHeaders(token) },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.message || data.error || `Failed to start listener: ${res.statusText}`);
  }
  return data;
}

