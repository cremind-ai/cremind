// REST client for the per-conversation memory feature. Mirrors the
// auth-header / base-url conventions of `conversationApi.ts`.

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

export interface MemoryEntry {
  id: string;
  content: string;
  token_count: number;
  created_at: number;
  source_conversation_id?: string | null;
}

export interface ConversationMemory {
  // Short-term memory is the conversation's running compaction summary.
  summary: string;
  long_term: MemoryEntry[];
  // current = the model's reported context size for the latest turn; threshold =
  // compact_threshold_percent / 100 * context_window (when to suggest compacting).
  token_progress: { current: number; threshold: number; context_window: number };
  enabled: boolean;
  last_compacted_at: number | null;
}

export async function fetchConversationMemory(
  agentUrl: string, authToken: string, conversationId: string,
): Promise<ConversationMemory> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/conversations/${encodeURIComponent(conversationId)}/memory`,
    { headers: authHeaders(authToken) },
  );
  if (!res.ok) throw new Error(`Failed to fetch memory: ${res.statusText}`);
  return res.json();
}

// Folds the conversation now (running summary + long-term memory), regardless of
// the compaction threshold. Runs synchronously server-side and returns whether a
// fold happened, so the caller can simply re-fetch afterwards.
export async function triggerConversationMemory(
  agentUrl: string, authToken: string, conversationId: string,
): Promise<{ folded: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/conversations/${encodeURIComponent(conversationId)}/memory/trigger`,
    { method: 'POST', headers: authHeaders(authToken) },
  );
  if (!res.ok) throw new Error(`Failed to update memory: ${res.statusText}`);
  return res.json();
}
