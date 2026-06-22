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
  short_term: MemoryEntry[];
  long_term: MemoryEntry[];
  token_progress: { current: number; threshold: number };
  enabled: boolean;
  extracting: boolean;
  last_extracted_at: number | null;
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

// Kicks off a background extraction. Returns immediately (202); the caller
// should poll `fetchConversationMemory` until `extracting` flips back to false.
export async function triggerConversationMemory(
  agentUrl: string, authToken: string, conversationId: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/conversations/${encodeURIComponent(conversationId)}/memory/trigger`,
    { method: 'POST', headers: authHeaders(authToken) },
  );
  if (!res.ok) throw new Error(`Failed to trigger memory extraction: ${res.statusText}`);
}
