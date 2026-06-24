// Typed client for the token-usage + estimated-cost endpoints.
//
//   GET /api/usage/summary              → dashboard aggregate (UsageSummary)
//   GET /api/conversations/{id}/usage   → per-conversation detail (ConversationUsage)
//
// Follows the resolveBaseUrl + authHeaders + fetch convention used by the other
// services. agentUrl / authToken come from the settings Pinia store.

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(authToken: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
  return headers;
}

// ── shared shapes ─────────────────────────────────────────────────────────

/** The four-way token breakdown plus its estimated USD cost. */
export interface TokenBreakdown {
  input_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
}

/** One slice of a breakdown by source (reasoning agent / sub-agent / tool). */
export interface ToolUsage extends TokenBreakdown {
  source: string;
  display_name: string;
  source_type: string; // reasoning | tool | subagent | intrinsic | aggregate
  tool_id: string | null;
  request_count: number;
}

/** One assistant turn (request) with its per-source breakdown. */
export interface RequestUsage extends TokenBreakdown {
  message_id: string | null;
  created_at: number;
  model: string | null;
  provider: string | null;
  by_source: ToolUsage[];
}

/** Whole-conversation rollup — backs the conversation usage panel + chips. */
export interface ConversationUsage {
  conversation_id: string;
  totals: TokenBreakdown;
  cache_hit_rate: number;
  request_count: number;
  by_source: ToolUsage[];
  requests: RequestUsage[];
}

// ── dashboard summary ───────────────────────────────────────────────────────

export interface UsageTimePoint extends TokenBreakdown {
  bucket: string; // ISO date (local day)
  total_usd: number;
  request_count: number;
}

export interface UsageGroupSlice extends TokenBreakdown {
  key: string;
  display_name: string;
  request_count: number;
  source_type?: string;
  tool_id?: string | null;
}

export interface TopConversation extends TokenBreakdown {
  conversation_id: string;
  title: string;
  request_count: number;
  last_active_at: number;
  total_usd: number;
}

export interface UsageTotals extends TokenBreakdown {
  uncached_input_usd: number;
  cache_read_usd: number;
  cache_write_usd: number;
  output_usd: number;
  request_count: number;
  conversation_count: number;
}

export interface UsageSummary {
  totals: UsageTotals;
  cache_hit_rate: number;
  cache_read_usd: number;
  cache_write_usd: number;
  conversation_count: number;
  request_count: number;
  series: UsageTimePoint[];
  by_model: UsageGroupSlice[];
  by_provider: UsageGroupSlice[];
  by_source: UsageGroupSlice[];
  top_conversations: TopConversation[];
  has_unpriced: boolean;
}

export interface UsageQuery {
  start?: number | null; // epoch ms
  end?: number | null;   // epoch ms
  profile?: string | null; // admin-only cross-profile; omit = caller's profile
  tzOffsetMin?: number;    // local TZ offset for daily bucketing
}

export async function fetchUsageSummary(
  agentUrl: string, authToken: string, query: UsageQuery = {},
): Promise<UsageSummary> {
  const base = resolveBaseUrl(agentUrl);
  const params = new URLSearchParams();
  if (query.start != null) params.set('start', String(query.start));
  if (query.end != null) params.set('end', String(query.end));
  if (query.profile) params.set('profile', query.profile);
  params.set('tz_offset', String(query.tzOffsetMin ?? -new Date().getTimezoneOffset()));
  const res = await fetch(`${base}/api/usage/summary?${params}`, {
    headers: authHeaders(authToken),
  });
  if (!res.ok) throw new Error(`Failed to fetch usage summary: ${res.statusText}`);
  return res.json();
}

export async function fetchConversationUsage(
  agentUrl: string, authToken: string, conversationId: string,
): Promise<ConversationUsage> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/conversations/${encodeURIComponent(conversationId)}/usage`,
    { headers: authHeaders(authToken) },
  );
  if (!res.ok) throw new Error(`Failed to fetch conversation usage: ${res.statusText}`);
  return res.json();
}
