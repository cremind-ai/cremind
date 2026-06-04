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

export interface SkillEventSubscription {
  id: string;
  conversation_id: string;
  conversation_title: string;
  profile: string;
  skill_name: string;
  event_type: string;
  action: string;
  created_at: number;
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
  kind: 'started' | 'completed' | 'error' | 'channel_otp' | 'skill_register_required';
  priority?: 'high' | 'normal';
  created_at: number;
  // Channel-specific extras when kind === 'channel_otp'.
  channel_id?: string;
  channel_type?: string;
  sender_id?: string;
  sender_name?: string;
  otp?: string;
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

export async function simulateEvent(
  agentUrl: string,
  token: string,
  id: string,
  content: string,
  filename?: string,
): Promise<{ ok: boolean; path: string }> {
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

