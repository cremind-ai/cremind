/**
 * API client for the Calendar & Schedule system.
 *
 * Mirrors skillEventsApi.ts: each call resolves the base URL from the active
 * agent URL and attaches a Bearer token. Live updates arrive on the shared
 * admin SSE (see adminEventsStream.ts -> subscribeScheduleEventsAdmin).
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

export interface ScheduleEventSubscription {
  id: string;
  conversation_id: string;
  conversation_title?: string;
  profile: string;
  title: string;
  action: string;
  all_day?: boolean;
  schedule_kind: string;
  dtstart: string;
  duration_minutes: number;
  rrule: string | null;
  recurrence_end_type: string | null;
  recurrence_end_value: string | null;
  next_fire_at: number | null;
  occurrences_fired: number;
  status: 'active' | 'completed' | 'cancelled' | 'paused';
  source: 'agent' | 'manual';
  created_at: number;
  updated_at: number;
}

export interface CalendarOccurrence {
  subscription_id: string | null;
  title: string;
  action: string;
  all_day: boolean;
  schedule_kind: string;
  is_recurring: boolean;
  rrule: string | null;
  status: string;
  source: string;
  conversation_id: string | null;
  start: string; // naive-local ISO
  end: string;
  read_only?: boolean;
  external?: string;
}

export interface CalendarSettings {
  enabled: boolean;
  google_connected: boolean;
  google_email?: string | null;
  provider?: string;
}

export interface CreateEventPayload {
  title: string;
  dtstart: string;
  action?: string;
  all_day?: boolean;
  duration_minutes?: number;
  schedule_kind?: string;
  rrule?: string | null;
  recurrence_end_type?: string | null;
  recurrence_end_value?: string | null;
}

export async function getCalendarSettings(agentUrl: string, token: string): Promise<CalendarSettings> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/calendar/settings`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to load settings: ${res.statusText}`);
  return res.json();
}

export async function setCalendarEnabled(
  agentUrl: string, token: string, enabled: boolean,
): Promise<CalendarSettings> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/calendar/settings`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw new Error(`Failed to update settings: ${res.statusText}`);
  return res.json();
}

export async function listCalendarEvents(
  agentUrl: string, token: string, from: string, to: string,
): Promise<{ events: CalendarOccurrence[]; from: string; to: string }> {
  const base = resolveBaseUrl(agentUrl);
  const url = `${base}/api/calendar/events?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
  const res = await fetch(url, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to list events: ${res.statusText}`);
  return res.json();
}

export async function listScheduleSubscriptions(
  agentUrl: string, token: string,
): Promise<{ subscriptions: ScheduleEventSubscription[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/schedule-events`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to list schedule events: ${res.statusText}`);
  return res.json();
}

export async function createCalendarEvent(
  agentUrl: string, token: string, payload: CreateEventPayload,
): Promise<{ ok: boolean; event: ScheduleEventSubscription }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/calendar/events`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || data.error || `Failed to create event: ${res.statusText}`);
  return data;
}

export async function updateCalendarEvent(
  agentUrl: string, token: string, id: string, fields: Partial<CreateEventPayload>,
): Promise<{ ok: boolean; event: ScheduleEventSubscription }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/calendar/events/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: authHeaders(token),
    body: JSON.stringify(fields),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || data.error || `Failed to update event: ${res.statusText}`);
  return data;
}

export async function deleteCalendarEvent(agentUrl: string, token: string, id: string): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/calendar/events/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to delete event: ${res.statusText}`);
  }
}

export async function connectGoogleCalendar(
  agentUrl: string, token: string,
): Promise<{ authorize_url?: string; error?: string; message?: string }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/calendar/google/connect`, {
    method: 'POST',
    headers: authHeaders(token),
    body: '{}',
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // 409 "unavailable" carries a helpful message — surface it, don't throw raw.
    return { error: data.error || 'error', message: data.message || res.statusText };
  }
  return data;
}

export async function disconnectGoogleCalendar(agentUrl: string, token: string): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/calendar/google/disconnect`, {
    method: 'POST',
    headers: authHeaders(token),
    body: '{}',
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || data.error || `Failed to disconnect: ${res.statusText}`);
  }
}

export async function setScheduleEventStatus(
  agentUrl: string, token: string, id: string, status: 'active' | 'paused' | 'cancelled',
): Promise<{ ok: boolean; event: ScheduleEventSubscription }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/schedule-events/${encodeURIComponent(id)}/status`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ status }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || data.error || `Failed to update status: ${res.statusText}`);
  return data;
}
