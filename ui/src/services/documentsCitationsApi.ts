/**
 * API client for what a citation points at in the user's own documents.
 *
 *   POST /api/documentation-search/citations/resolve   tokens → verified items (answers
 *                                          saved without citation metadata)
 *   GET  /api/documentation-search/files/{fid}         the file record (kind, EXIF, …)
 *   GET  /api/documentation-search/files/{fid}/text    the cited passage and its neighbours
 *   GET  /api/documentation-search/files/{fid}/raw     the original bytes (local files only)
 *   GET  /api/documentation-search/files/{fid}/thumbnail?size=N   an in-memory JPEG
 *
 * Follows the resolveBaseUrl + authHeaders + fetch convention of the other
 * services. The server scopes every route to the caller's profile (another
 * profile's fid is a 404), and it accepts only Bearer auth — which is why
 * raw bytes and thumbnails come back as Blobs for an object URL rather than
 * as URLs an <img> or a new tab could load on their own.
 */

import { normalizeCitationItem, type CitationItem } from '../utils/citations';

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl.replace(/\/$/, '');
  }
  return `${window.location.origin}${agentUrl}`.replace(/\/$/, '');
}

function authHeaders(authToken: string, json = true): Record<string, string> {
  const headers: Record<string, string> = json ? { 'Content-Type': 'application/json' } : {};
  if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
  return headers;
}

/** A refused request, with the server's error code when it sent one
 *  (`NotFound`, `DriveFile`, …) and, for a Drive file, where to open it. */
export class DocumentsApiError extends Error {
  status: number;
  code: string | null;
  webLink: string | null;

  constructor(status: number, body: any) {
    const message = (body && (body.message || body.error)) || `Request failed (${status})`;
    super(String(message));
    this.name = 'DocumentsApiError';
    this.status = status;
    this.code = body && typeof body.error === 'string' ? body.error : null;
    this.webLink = body && typeof body.web_link === 'string' ? body.web_link : null;
  }
}

async function refusal(res: Response): Promise<DocumentsApiError> {
  const body = await res.json().catch(() => ({}));
  return new DocumentsApiError(res.status, body);
}

function fileUrl(agentUrl: string, fid: string, tail = ''): string {
  return `${resolveBaseUrl(agentUrl)}/api/documentation-search/files/${encodeURIComponent(fid)}${tail}`;
}

// ── resolve ──────────────────────────────────────────────────────────────────

/** The server takes at most this many tokens per request. */
export const RESOLVE_BATCH = 100;

/**
 * Verify tokens the way an answer's metadata would have: `{token: item}` for
 * every token the server answered for. `conversationId` lets it tell
 * "issued in this conversation" (verified) from "issued in another one"
 * (verified_elsewhere); without it nothing can be more than the latter.
 */
export async function resolveCitations(
  agentUrl: string,
  authToken: string,
  tokens: string[],
  conversationId?: string | null,
): Promise<Record<string, CitationItem>> {
  const out: Record<string, CitationItem> = {};
  for (let i = 0; i < tokens.length; i += RESOLVE_BATCH) {
    const body: Record<string, unknown> = { tokens: tokens.slice(i, i + RESOLVE_BATCH) };
    if (conversationId) body.conversation_id = conversationId;
    const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/documentation-search/citations/resolve`, {
      method: 'POST',
      headers: authHeaders(authToken),
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await refusal(res);
    const data = await res.json().catch(() => ({}));
    const items = data && typeof data.items === 'object' && data.items ? data.items : {};
    for (const [token, raw] of Object.entries(items)) {
      const item = normalizeCitationItem(raw, token);
      if (item) out[item.token] = item;
    }
  }
  return out;
}

// ── the file behind a citation ───────────────────────────────────────────────

export interface DocumentFileDetail {
  fid: string;
  name: string;
  rel_path: string;
  kind: string;
  status: string;
  status_reason?: string | null;
  source?: string | null;
  size?: number | null;
  /** Epoch milliseconds. */
  modified?: number | null;
  indexed_at?: number | null;
  taken_at?: number | null;
  doc_created_at?: number | null;
  chunks?: number | null;
  caption_state?: string | null;
  caption?: string | null;
  doc_meta?: Record<string, any> | null;
  exif?: Record<string, any> | null;
  web_link?: string | null;
  error?: string | null;
}

export async function fetchDocumentFile(
  agentUrl: string,
  authToken: string,
  fid: string,
): Promise<DocumentFileDetail> {
  const res = await fetch(fileUrl(agentUrl, fid), { headers: authHeaders(authToken, false) });
  if (!res.ok) throw await refusal(res);
  return res.json();
}

export interface CitedSegment {
  /** The segment's own token, so the viewer can re-centre on it. */
  token: string;
  locator: Record<string, any>;
  locator_label: string;
  text: string;
  /** True for the segment the request asked for (`chunk`). */
  highlight: boolean;
}

export interface CitedText {
  fid: string;
  name: string;
  rel_path: string;
  kind: string;
  source: string;
  segments: CitedSegment[];
  truncated: boolean;
}

export interface CitedTextQuery {
  /** First 8 hex of the chunk's text hash — the part after `#` in a token. */
  chunk?: string | null;
  /** "12" or "12-13" (PDF pages); used when there is no chunk to centre on. */
  pages?: string | null;
  /** "40-58" (text lines). */
  lines?: string | null;
  /** Neighbouring segments on each side. */
  context?: number;
}

export async function fetchCitedText(
  agentUrl: string,
  authToken: string,
  fid: string,
  query: CitedTextQuery = {},
): Promise<CitedText> {
  const params = new URLSearchParams();
  if (query.chunk) params.set('chunk', query.chunk);
  if (query.pages) params.set('pages', query.pages);
  if (query.lines) params.set('lines', query.lines);
  params.set('context', String(query.context ?? 2));
  const res = await fetch(fileUrl(agentUrl, fid, `/text?${params.toString()}`), {
    headers: authHeaders(authToken, false),
  });
  if (!res.ok) throw await refusal(res);
  const data = await res.json();
  return {
    fid: String(data.fid ?? fid),
    name: String(data.name ?? ''),
    rel_path: String(data.rel_path ?? ''),
    kind: String(data.kind ?? ''),
    source: String(data.source ?? 'local'),
    segments: Array.isArray(data.segments)
      ? data.segments.map((s: any) => ({
          token: String(s?.token ?? ''),
          locator: s?.locator && typeof s.locator === 'object' ? s.locator : {},
          locator_label: String(s?.locator_label ?? ''),
          text: String(s?.text ?? ''),
          highlight: s?.highlight === true,
        }))
      : [],
    truncated: data.truncated === true,
  };
}

export function documentRawUrl(agentUrl: string, fid: string): string {
  return fileUrl(agentUrl, fid, '/raw');
}

export function documentThumbnailUrl(agentUrl: string, fid: string, size = 256): string {
  return fileUrl(agentUrl, fid, `/thumbnail?size=${encodeURIComponent(String(size))}`);
}

/**
 * The original file. A Google Drive file is refused with 409 `DriveFile`
 * (thrown as a DocumentsApiError whose `webLink` says where to open it).
 */
export async function fetchDocumentRaw(
  agentUrl: string,
  authToken: string,
  fid: string,
): Promise<Blob> {
  const res = await fetch(documentRawUrl(agentUrl, fid), { headers: authHeaders(authToken, false) });
  if (!res.ok) throw await refusal(res);
  return res.blob();
}
