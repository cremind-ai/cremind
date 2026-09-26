/**
 * REST client for the per-conversation search-tool selection.
 *
 *   GET /api/search-tools                          the rows and defaults a NEW chat starts from
 *   GET /api/conversations/{id}/search-tools       one conversation's selection
 *   PUT /api/conversations/{id}/search-tools       {version, enabled} → the saved state
 *   GET /api/group-chats/{id}/search-tools         a room's selection (room-wide)
 *   PUT /api/group-chats/{id}/search-tools         any posting member or the admin
 *
 * The server is the authority for everything this module hands back — which
 * sources exist, which are available, what the next response will expose — and
 * `app/agent/search_tools.py` is where it decides. Two rules are worth knowing
 * on this side of the wire:
 *
 * - **Desire and availability are separate.** `enabled` echoes what was asked
 *   for, unavailable sources included; `effective` is what the next response
 *   would actually use. A temporary outage never rewrites a preference.
 * - **Rows come only from a response.** Documentation search is simply absent
 *   from `tools` when the profile, the administrator or the conversation's
 *   origin does not offer it, so nothing here invents a row the server did not
 *   send.
 *
 * Saves are compare-and-swap on `version`: a stale version answers 409 with the
 * current state, which this module surfaces as `SearchToolsConflictError` so the
 * caller can adopt it instead of silently overwriting a change made elsewhere.
 */

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) return agentUrl;
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(authToken: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
  return headers;
}

/** The four sources, in their fixed priority order (mirrors `SEARCH_TOOL_IDS`). */
export const SEARCH_TOOL_IDS = [
  'documentation_search',
  'cremind_documentation_search',
  'memory_search',
  'web_search',
] as const;

export type SearchToolId = (typeof SEARCH_TOOL_IDS)[number];

const PRIORITY: Record<string, number> = Object.fromEntries(
  SEARCH_TOOL_IDS.map((id, index) => [id, index]),
);

export function isSearchToolId(value: unknown): value is SearchToolId {
  return typeof value === 'string' && value in PRIORITY;
}

/** Distinct known ids, in priority order. Anything else is dropped. */
export function orderSelection(ids: readonly unknown[]): SearchToolId[] {
  const seen = new Set<SearchToolId>();
  for (const id of ids) if (isSearchToolId(id)) seen.add(id);
  return [...seen].sort((a, b) => PRIORITY[a] - PRIORITY[b]);
}

export interface SearchToolRow {
  id: SearchToolId;
  label: string;
  description: string;
  available: boolean;
  /**
   * Why the row cannot be used — or, on an AVAILABLE row in a group room, an
   * informational note ("Availability varies by agent in this room."). Read it
   * together with `available`, never on its own.
   */
  unavailable_reason: string | null;
}

export interface SearchToolsState {
  /** Compare-and-swap token. 0 for a new chat and a conversation never saved. */
  version: number;
  /** What was asked for, in priority order (unavailable sources included). */
  enabled: SearchToolId[];
  /** What the next response would expose: `enabled` ∩ available. */
  effective: SearchToolId[];
  tools: SearchToolRow[];
  /** A saved selection no response has adopted yet. */
  pending_next_response: boolean;
  /** Only ever set on a save's answer, when the change may cost cache reuse. */
  cache_warning: string | null;
}

/** Where a selection lives: an ordinary conversation, or a whole group room. */
export type SearchToolsScope = 'conversation' | 'group';

/**
 * Hand-narrow a server answer. Anything that is not a state (an HTML fallback,
 * a body from an older server) reads as `null`, and unknown ids are dropped
 * rather than rendered — a future fifth source must not appear as a row this
 * build cannot describe.
 */
export function normalizeSearchToolsState(raw: unknown): SearchToolsState | null {
  if (!raw || typeof raw !== 'object') return null;
  const body = raw as Record<string, unknown>;
  const version = typeof body.version === 'number' && Number.isFinite(body.version)
    ? body.version
    : null;
  if (version === null || !Array.isArray(body.enabled) || !Array.isArray(body.tools)) return null;
  const rows = new Map<SearchToolId, SearchToolRow>();
  for (const item of body.tools) {
    if (!item || typeof item !== 'object') continue;
    const row = item as Record<string, unknown>;
    if (!isSearchToolId(row.id) || rows.has(row.id)) continue;
    rows.set(row.id, {
      id: row.id,
      label: typeof row.label === 'string' && row.label ? row.label : row.id,
      description: typeof row.description === 'string' ? row.description : '',
      available: row.available === true,
      unavailable_reason: typeof row.unavailable_reason === 'string' && row.unavailable_reason
        ? row.unavailable_reason
        : null,
    });
  }
  return {
    version,
    enabled: orderSelection(body.enabled),
    effective: orderSelection(Array.isArray(body.effective) ? body.effective : []),
    tools: [...rows.values()].sort((a, b) => PRIORITY[a.id] - PRIORITY[b.id]),
    pending_next_response: body.pending_next_response === true,
    cache_warning: typeof body.cache_warning === 'string' && body.cache_warning
      ? body.cache_warning
      : null,
  };
}

/** A failed search-tools call. `status` is 0 when the server was never reached. */
export class SearchToolsError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = 'SearchToolsError';
    this.status = status;
    this.code = code;
  }
}

/** A save made against a stale version. `state` is the one to adopt. */
export class SearchToolsConflictError extends SearchToolsError {
  readonly state: SearchToolsState;

  constructor(message: string, state: SearchToolsState) {
    super(message, 409, 'VersionConflict');
    this.name = 'SearchToolsConflictError';
    this.state = state;
  }
}

function pathFor(scope: SearchToolsScope, id: string): string {
  const root = scope === 'group' ? '/api/group-chats' : '/api/conversations';
  return `${root}/${encodeURIComponent(id)}/search-tools`;
}

async function readBody(res: Response): Promise<unknown> {
  try {
    return await res.json();
  } catch {
    return null;
  }
}

async function call(url: string, init: RequestInit): Promise<SearchToolsState> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    throw new SearchToolsError('Could not reach Cremind. Check the connection and try again.', 0);
  }
  const body = await readBody(res);
  const detail = body && typeof body === 'object' ? body as Record<string, unknown> : {};
  const message = (typeof detail.message === 'string' && detail.message)
    || (typeof detail.error === 'string' && detail.error)
    || res.statusText
    || `Request failed (${res.status})`;
  if (res.status === 409) {
    const current = normalizeSearchToolsState(detail.state);
    if (current) {
      throw new SearchToolsConflictError(
        typeof detail.message === 'string' && detail.message
          ? detail.message
          : 'Search tools were changed elsewhere.',
        current,
      );
    }
  }
  if (!res.ok) {
    throw new SearchToolsError(
      message,
      res.status,
      typeof detail.error === 'string' ? detail.error : null,
    );
  }
  const state = normalizeSearchToolsState(body);
  if (!state) {
    throw new SearchToolsError('The server sent an unreadable search-tools answer.', res.status);
  }
  return state;
}

/** The rows and defaults a brand-new chat starts from (version 0). */
export function fetchNewChatSearchTools(
  agentUrl: string, authToken: string,
): Promise<SearchToolsState> {
  return call(`${resolveBaseUrl(agentUrl)}/api/search-tools`, {
    headers: authHeaders(authToken),
  });
}

export function fetchSearchTools(
  agentUrl: string, authToken: string, scope: SearchToolsScope, id: string,
): Promise<SearchToolsState> {
  return call(`${resolveBaseUrl(agentUrl)}${pathFor(scope, id)}`, {
    headers: authHeaders(authToken),
  });
}

/**
 * Save a selection against the version it was made from. `enabled: null`
 * means "the defaults" (Reset); a list is those sources, in any order — the
 * server stores them in priority order. Throws `SearchToolsConflictError` on a
 * stale version and `SearchToolsError` for anything else.
 */
export function saveSearchTools(
  agentUrl: string,
  authToken: string,
  scope: SearchToolsScope,
  id: string,
  version: number,
  enabled: readonly SearchToolId[] | null,
): Promise<SearchToolsState> {
  return call(`${resolveBaseUrl(agentUrl)}${pathFor(scope, id)}`, {
    method: 'PUT',
    headers: authHeaders(authToken),
    body: JSON.stringify({ version, enabled: enabled === null ? null : orderSelection(enabled) }),
  });
}
