import type { ChatMode } from '../constants/chatModes';

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(authToken: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authToken) {
    headers['Authorization'] = `Bearer ${authToken}`;
  }
  return headers;
}

export interface ConversationSummary {
  id: string;
  profile: string;
  channel_id: string | null;
  context_id: string | null;
  task_id: string | null;
  title: string;
  created_at: number;
  updated_at: number;
  message_count: number;
}

export interface MessageRecord {
  id: string;
  conversation_id: string;
  role: 'user' | 'agent';
  content: string | null;
  parts: any[] | null;
  thinking_steps: {
    step?: number | null;
    call_id?: string | null;
    tool?: string;
    tool_input?: string;
    result?: { kind: string; text?: string; data?: Record<string, any>; file?: any }[];
    // Legacy fields (older persisted messages) read for back-compat.
    observation?: { kind: string; text?: string; data?: Record<string, any>; file?: any }[];
    model_label?: string | null;
  }[] | null;
  token_usage: {
    input_tokens: number;
    output_tokens: number;
    cache_read_input_tokens?: number;
    cache_creation_input_tokens?: number;
  } | null;
  metadata: Record<string, any> | null;
  summary: string | null;
  created_at: number;
  ordering: number;
}

export async function fetchConversations(
  agentUrl: string, authToken: string,
  limit: number = 50, offset: number = 0,
  channelType: string | null = null,
  channelId: string | null = null,
): Promise<ConversationSummary[]> {
  const base = resolveBaseUrl(agentUrl);
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (channelId) params.set('channel_id', channelId);
  else if (channelType) params.set('channel_type', channelType);
  const res = await fetch(`${base}/api/conversations?${params}`, {
    headers: authHeaders(authToken),
  });
  if (!res.ok) throw new Error(`Failed to fetch conversations: ${res.statusText}`);
  const data = await res.json();
  return data.conversations;
}

export async function fetchConversationMessages(
  agentUrl: string, authToken: string, conversationId: string,
): Promise<{ conversation: ConversationSummary; messages: MessageRecord[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/conversations/${encodeURIComponent(conversationId)}`, {
    headers: authHeaders(authToken),
  });
  if (!res.ok) throw new Error(`Failed to fetch conversation: ${res.statusText}`);
  return res.json();
}

/**
 * Fetch the live agent-activity snapshot for a conversation (Claude Code and
 * future coding sub-agents). Returns the snapshot when a background task is
 * still tracked in memory, or null when it is gone (finished/cleaned up or the
 * server restarted). Used only to disambiguate a persisted "running" snapshot
 * on reload; never throws (returns null on any error).
 */
export async function fetchAgentActivity(
  agentUrl: string, authToken: string, conversationId: string,
): Promise<any | null> {
  try {
    const base = resolveBaseUrl(agentUrl);
    const res = await fetch(
      `${base}/api/conversations/${encodeURIComponent(conversationId)}/agent-activity`,
      { headers: authHeaders(authToken) },
    );
    if (!res.ok) return null;
    const body = await res.json();
    return body?.activity ?? null;
  } catch {
    return null;
  }
}

export async function deleteConversation(
  agentUrl: string, authToken: string, conversationId: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'DELETE',
    headers: authHeaders(authToken),
  });
  if (!res.ok) throw new Error(`Failed to delete conversation: ${res.statusText}`);
}

export async function deleteAllConversations(
  agentUrl: string, authToken: string,
): Promise<number> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/conversations`, {
    method: 'DELETE',
    headers: authHeaders(authToken),
  });
  if (!res.ok) throw new Error(`Failed to delete conversations: ${res.statusText}`);
  const data = await res.json();
  return data.deleted_count;
}

export async function updateConversationTitle(
  agentUrl: string, authToken: string, conversationId: string, title: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'PUT',
    headers: authHeaders(authToken),
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw new Error(`Failed to update conversation: ${res.statusText}`);
}

// Server-authoritative regex for conversation ids. Mirrored client-side for
// inline UX validation; the server still enforces it.
export const CONVERSATION_ID_REGEX = /^[a-z0-9][a-z0-9_-]{0,127}$/;

// Renames a conversation's id. When `newTitle` is omitted the server resets
// the title to match the new id; pass an explicit title to keep a custom one.
// Throws an Error with the server's error message on 400/409 so UI callers
// can surface it.
export async function updateConversationId(
  agentUrl: string, authToken: string, conversationId: string,
  newId: string, newTitle?: string,
): Promise<ConversationSummary> {
  const base = resolveBaseUrl(agentUrl);
  const body: Record<string, string> = { id: newId };
  if (newTitle !== undefined) body.title = newTitle;
  const res = await fetch(`${base}/api/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'PUT',
    headers: authHeaders(authToken),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data?.message || data?.error || detail;
    } catch { /* ignore body parse errors */ }
    throw new Error(detail);
  }
  const data = await res.json();
  return { ...data.conversation, message_count: 0 };
}

export async function createConversation(
  agentUrl: string, authToken: string, title?: string,
): Promise<ConversationSummary> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/conversations`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: JSON.stringify({ title: title ?? 'Untitled Chat' }),
  });
  if (!res.ok) throw new Error(`Failed to create conversation: ${res.statusText}`);
  const data = await res.json();
  return { ...data.conversation, message_count: 0 };
}

export interface SendMessageResponse {
  run_id: string;
  conversation_id: string;
}

export interface MessageAttachment {
  name: string;
  path: string;
}

export interface SendMessageOptions {
  mode?: ChatMode; // default 'reasoning'
  planAction?: 'accept'; // only on a Plan-mode Accept
  attachments?: MessageAttachment[];
}

export async function sendMessageRequest(
  agentUrl: string,
  authToken: string,
  conversationId: string,
  text: string,
  opts: SendMessageOptions = {},
): Promise<SendMessageResponse> {
  const base = resolveBaseUrl(agentUrl);
  const mode = opts.mode ?? 'reasoning';
  const body: Record<string, unknown> = {
    text,
    mode,
    // Keep the legacy boolean coherent so an older server still behaves.
    reasoning: mode !== 'instant',
  };
  if (opts.planAction) body.plan_action = opts.planAction;
  if (opts.attachments && opts.attachments.length) body.attachments = opts.attachments;
  const res = await fetch(
    `${base}/api/conversations/${encodeURIComponent(conversationId)}/messages`,
    {
      method: 'POST',
      headers: authHeaders(authToken),
      body: JSON.stringify(body),
    },
  );
  if (!res.ok) throw new Error(`Failed to send message: ${res.statusText}`);
  return res.json();
}

export interface PlanCancelResponse {
  message_id?: string;
  content?: string;
}

// Decline a pending Plan-mode approval WITHOUT starting an agent run.
export async function cancelPlan(
  agentUrl: string,
  authToken: string,
  conversationId: string,
): Promise<PlanCancelResponse> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/conversations/${encodeURIComponent(conversationId)}/plan/cancel`,
    {
      method: 'POST',
      headers: authHeaders(authToken),
    },
  );
  if (!res.ok) throw new Error(`Failed to cancel plan: ${res.statusText}`);
  return res.json();
}

export async function cancelTask(
  agentUrl: string, authToken: string, taskId: string,
): Promise<boolean> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: 'POST',
    headers: authHeaders(authToken),
  });
  if (!res.ok) throw new Error(`Failed to cancel task: ${res.statusText}`);
  const data = await res.json();
  return Boolean(data.cancelled);
}
