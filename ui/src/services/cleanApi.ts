// Typed client for the per-profile "clean data" endpoint.
//
//   POST /api/clean  → wipe the caller's own profile data
//     body: { scope: 'custom' | 'working' | 'factory', components?: string[] }
//     resp: { success, scope, profile, cleaned: {comp: count|detail}, errors, total, components }
//
// Follows the resolveBaseUrl + authHeaders + fetch convention of the other
// services. agentUrl / authToken come from the settings Pinia store. The
// CLEAN_GROUPS registry below is the UI mirror of app/reset/components.py — the
// keys MUST stay identical to the API/CLI vocabulary.

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(authToken: string, json = true): Record<string, string> {
  const headers: Record<string, string> = {};
  if (json) headers['Content-Type'] = 'application/json';
  if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
  return headers;
}

async function jsonOrThrow(res: Response, what: string): Promise<any> {
  if (!res.ok) {
    let msg = `${what} (HTTP ${res.status})`;
    try {
      const body = await res.json();
      if (body?.message || body?.error) msg = body.message || body.error;
    } catch { /* non-JSON body */ }
    throw new Error(msg);
  }
  return res.json();
}

export type CleanScope = 'custom' | 'working' | 'factory';

export interface CleanComponent {
  key: string;
  label: string;
}

export interface CleanGroup {
  title: string;
  components: CleanComponent[];
}

// Single source of truth for the checklist. Keys == the API/CLI component keys.
export const CLEAN_GROUPS: CleanGroup[] = [
  {
    title: 'Conversations, memory & uploads',
    components: [
      { key: 'conversations', label: 'Conversations (chat history)' },
      { key: 'memory', label: 'Long-term memory' },
      { key: 'uploads', label: 'Uploaded files' },
      { key: 'plans', label: 'Plan-mode files' },
    ],
  },
  {
    title: 'Usage & event-run history',
    components: [
      { key: 'usage', label: 'Usage & cost records' },
      { key: 'event_runs', label: 'Event-run history' },
    ],
  },
  {
    title: 'Automation & channels',
    components: [
      { key: 'processes', label: 'Running processes (background shells)' },
      { key: 'schedules', label: 'Schedules' },
      { key: 'file_watchers', label: 'File watchers' },
      { key: 'skill_events', label: 'Skill-event subscriptions' },
      { key: 'channels', label: 'External channels (keeps “main”)' },
    ],
  },
  {
    title: 'Config & credentials',
    components: [
      { key: 'llm_config', label: 'LLM configuration & API keys' },
      { key: 'oauth_tokens', label: 'OAuth tokens' },
      { key: 'tool_configs', label: 'Tools / MCP & their configs' },
      { key: 'skills', label: 'Persona & skills (reset to defaults)' },
      { key: 'documents', label: 'Documents + embeddings' },
      { key: 'browser_login', label: 'Browser login state' },
      { key: 'app_settings', label: 'App settings (reset to defaults)' },
    ],
  },
];

export interface CleanResult {
  success: boolean;
  scope: CleanScope;
  profile: string;
  cleaned: Record<string, unknown>;
  errors: Record<string, string>;
  total: number;
  components: string[];
}

export async function cleanProfileData(
  agentUrl: string,
  authToken: string,
  scope: CleanScope,
  components: string[] = [],
): Promise<CleanResult> {
  const body =
    scope === 'custom' ? { scope, components } : { scope };
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/clean`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: JSON.stringify(body),
  });
  return jsonOrThrow(res, 'Failed to clean data');
}
