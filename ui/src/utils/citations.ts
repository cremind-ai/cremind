/**
 * Citation tokens in the web UI: parse, number, render as chips, flatten for copy.
 *
 * The agent cites the user's own documents by copying the tokens the User
 * Documents tools print next to every result — `[ud:k7m2xq9a]` for a file or
 * folder, `[ud:k7m2xq9a#3f9c2e1b]` for one chunk of it. The grammar is
 * app/userdocs/cite.py's, reimplemented here rather than asked of the server
 * because the numbering is visible: the web bubble, a channel's "Sources:"
 * footer and the CLI must all call the same source "[2]", so the tolerant parse
 * and the first-appearance numbering have to agree with the Python exactly.
 * ui/tests/fixtures/citation-grammar.json holds the cases both sides are
 * checked against (ui/tests/citation-grammar.test.mjs and
 * tests/userdocs/test_cite_ui_parity.py).
 *
 * Deliberately dependency-free (types only), so the grammar can be tested
 * without a browser and imported anywhere without dragging a store along.
 */

import type { MarkedExtension, Tokens } from 'marked';

// ── the wire format (message metadata, the `citations` stream event, resolve) ──

export type CitationStatus =
  | 'verified'
  | 'verified_elsewhere'
  | 'unissued'
  | 'stale'
  | 'removed'
  | 'invalid';

export type QuoteStatus = 'exact' | 'normalized' | 'fuzzy' | 'mismatch' | null;

export interface CitationFile {
  /** The file's cite id: the same 8 characters as the token, and the key of
   *  every /api/userdocs/files/{fid} route. */
  fid: string;
  name: string;
  rel_path: string;
  /** 'local' | 'drive' */
  source: string;
  /** File kind as the engine detected it (pdf, docx, image, …; 'folder' for a
   *  folder citation). */
  kind: string;
  web_link?: string | null;
}

export interface CitationItem {
  /** First-appearance number in the message the server verified. A bubble
   *  renumbers from its own text (see numberTokens), so this is advisory. */
  n: number;
  token: string;
  status: CitationStatus;
  quote_status: QuoteStatus;
  file: CitationFile | null;
  locator: Record<string, any>;
  locator_label: string;
  snippet: string;
}

/** One distinct citation as a bubble shows it: its number in that bubble's
 *  text, and the verified item once known. */
export interface CitationSource {
  token: string;
  n: number;
  item?: CitationItem;
}

export interface CitationsMeta {
  v: number;
  items: CitationItem[];
  /** How many items are not verified — the server's count for the whole answer. */
  unverified: number;
}

const STATUSES: ReadonlySet<string> = new Set<CitationStatus>([
  'verified', 'verified_elsewhere', 'unissued', 'stale', 'removed', 'invalid',
]);
const QUOTE_STATUSES: ReadonlySet<string> = new Set(['exact', 'normalized', 'fuzzy', 'mismatch']);

/** True for the statuses a reader can take at face value. */
export function isVerifiedStatus(status: CitationStatus | undefined | null): boolean {
  return status === 'verified' || status === 'verified_elsewhere';
}

/** How a chip, the hover preview, the footer and the viewer describe a status.
 *  `tone` picks the colour: ok (normal), warn (amber), gone (grey, struck
 *  through), pending (not resolved yet). */
export interface CitationStatusInfo {
  tone: 'ok' | 'warn' | 'gone' | 'pending';
  label: string;
  note: string;
  /** Set when the quote next to the token does not match the source. */
  quoteNote: string;
}

export function citationStatusInfo(
  item: Pick<CitationItem, 'status' | 'quote_status'> | undefined | null,
): CitationStatusInfo {
  const quoteNote = item?.quote_status === 'mismatch'
    ? 'The quoted words next to this citation are not in the source.'
    : '';
  switch (item?.status) {
    case 'verified':
      return { tone: 'ok', label: 'Verified', note: '', quoteNote };
    case 'verified_elsewhere':
      return {
        tone: 'ok', label: 'Verified',
        note: 'Found by a search in another conversation, not this one.', quoteNote,
      };
    case 'unissued':
      return {
        tone: 'warn', label: 'Unverified',
        note: 'No search in this conversation returned this citation — the agent may have copied or invented it.',
        quoteNote,
      };
    case 'stale':
      return {
        tone: 'warn', label: 'Changed since cited',
        note: 'The file changed after it was cited. The passage below is the one the agent read.',
        quoteNote,
      };
    case 'removed':
      return {
        tone: 'gone', label: 'Source removed',
        note: 'This file is no longer in your indexed documents.', quoteNote,
      };
    case 'invalid':
      return {
        tone: 'gone', label: 'Invalid',
        note: 'This citation does not point at anything in your documents.', quoteNote,
      };
    default:
      return { tone: 'pending', label: 'Not checked', note: '', quoteNote };
  }
}

// ── the grammar (mirror of app/userdocs/cite.py) ──────────────────────────────

// The shape of every token the parser can produce: 8 lowercase ASCII letters
// or digits, then optionally 8 hex. Wider than the Crockford alphabet on
// purpose — cite.py lowercases whatever it was given ("[ud:ILOUilou]" →
// "[ud:ilouilou]") and leaves rejecting it to verification, which marks it
// invalid; it must still count, and render, as a numbered citation or the
// numbers after it would drift from the other renderers'.
const TOKEN_SHAPE_RE = /^\[ud:([0-9a-z]{8})(?:#([0-9a-f]{8}))?\]$/;

// What models actually write when they copy a token imperfectly: full-width
// brackets, spaces, upper case, several tokens in one bracket. cite.py spells
// this with re.IGNORECASE; here the case-insensitivity is written out
// ([Uu][Dd]) instead of using the `i` flag, so the characters that can reach a
// canonical token stay ASCII letters and digits whatever the engine's case
// folding does.
const TOLERANT_SRC =
  String.raw`[\[【]\s*((?:[Uu][Dd]:\s*[0-9A-Za-z]{8}(?:#[0-9A-Fa-f]{8})?\s*[;,]?\s*)+)[\]】]`;
const ONE_SRC = String.raw`[Uu][Dd]:\s*([0-9A-Za-z]{8})(?:#([0-9A-Fa-f]{8}))?`;

export interface ParsedCitation {
  /** Canonical form — what the registry stores and what items are keyed by. */
  token: string;
  citeId: string;
  c8: string | null;
  /** The bracket this citation was written in (shared by every citation in a
   *  multi-token bracket), as UTF-16 offsets into the text. */
  start: number;
  end: number;
}

export function makeToken(citeId: string, c8?: string | null): string {
  return c8 ? `[ud:${citeId}#${c8.slice(0, 8)}]` : `[ud:${citeId}]`;
}

/** The parts of a token in canonical (lower-case) form, or null for anything
 *  else — including anything that could not be put in an HTML attribute as is. */
export function tokenParts(token: string): { citeId: string; c8: string | null } | null {
  const m = TOKEN_SHAPE_RE.exec(token);
  return m ? { citeId: m[1], c8: m[2] ?? null } : null;
}

/** Every citation in `text`, in order of appearance — cite.py's parse_tokens. */
export function parseCitationTokens(text: string): ParsedCitation[] {
  const out: ParsedCitation[] = [];
  if (!text || !text.toLowerCase().includes('ud:')) return out;
  for (const bracket of text.matchAll(new RegExp(TOLERANT_SRC, 'g'))) {
    const start = bracket.index ?? 0;
    const end = start + bracket[0].length;
    for (const one of bracket[1].matchAll(new RegExp(ONE_SRC, 'g'))) {
      const citeId = one[1].toLowerCase();
      const c8 = one[2] ? one[2].toLowerCase() : null;
      out.push({ token: makeToken(citeId, c8), citeId, c8, start, end });
    }
  }
  return out;
}

/** Distinct canonical tokens numbered 1, 2, 3… by first appearance — the
 *  numbering every renderer (web, channels, CLI) shares. */
export function numberTokens(text: string): Map<string, number> {
  const numbers = new Map<string, number>();
  for (const { token } of parseCitationTokens(text)) {
    if (!numbers.has(token)) numbers.set(token, numbers.size + 1);
  }
  return numbers;
}

/** Whether a message is worth rendering through the citation-aware parser. */
export function mentionsCitations(text: string | null | undefined): boolean {
  return !!text && /ud\s*[:：]/i.test(text);
}

// ── normalising what the server sends ─────────────────────────────────────────

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

/** One item from metadata / the stream / resolve, or null when it is unusable
 *  (no canonical token). Unknown statuses read as 'invalid' — a chip must
 *  never look verified because the server said something this build does not
 *  know. */
export function normalizeCitationItem(raw: any, fallbackToken?: string): CitationItem | null {
  if (!raw || typeof raw !== 'object') return null;
  const token = str(raw.token) || fallbackToken || '';
  if (!tokenParts(token)) return null;
  const f = raw.file && typeof raw.file === 'object' ? raw.file : null;
  return {
    n: typeof raw.n === 'number' ? raw.n : 0,
    token,
    status: STATUSES.has(raw.status) ? raw.status : 'invalid',
    quote_status: QUOTE_STATUSES.has(raw.quote_status) ? raw.quote_status : null,
    file: f && str(f.fid)
      ? {
          fid: str(f.fid),
          name: str(f.name),
          rel_path: str(f.rel_path),
          source: str(f.source) || 'local',
          kind: str(f.kind),
          web_link: str(f.web_link) || str(f.web_view_link) || null,
        }
      : null,
    locator: raw.locator && typeof raw.locator === 'object' ? raw.locator : {},
    locator_label: str(raw.locator_label),
    snippet: str(raw.snippet),
  };
}

/** `metadata.citations` / the `citations` event payload, or undefined. */
export function normalizeCitationsMeta(raw: any): CitationsMeta | undefined {
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.items)) return undefined;
  const items = raw.items
    .map((it: any) => normalizeCitationItem(it))
    .filter((it: CitationItem | null): it is CitationItem => it !== null);
  return {
    v: typeof raw.v === 'number' ? raw.v : 1,
    items,
    unverified: typeof raw.unverified === 'number'
      ? raw.unverified
      : items.filter((it: CitationItem) => !isVerifiedStatus(it.status)).length,
  };
}

// ── chips (a marked inline extension) ─────────────────────────────────────────

export interface CitationRenderContext {
  /** The verification result for a token, when known. Read at render time so
   *  a chip picks up its status as soon as the item arrives. */
  lookup?: (token: string) => Pick<CitationItem, 'status' | 'quote_status'> | undefined;
}

interface CiteToken extends Tokens.Generic {
  type: 'udCite';
  raw: string;
  /** Canonical tokens in this bracket, deduplicated, in order. Empty for a
   *  near miss, which renders as escaped text. */
  cites: string[];
}

const ESCAPES: Record<string, string> = {
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
};
function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, ch => ESCAPES[ch]);
}

// A bracket that starts like a citation but is not one ("[ud:<script>]", a
// neutralised "[ud：…]" from document text, a truncated hash). marked would
// pass any HTML inside it straight through; rendering it as escaped text
// means a string a document planted, and the agent echoed, stays inert. A
// following "(" is left alone: that is a Markdown link, not a citation.
const NEAR_MISS_RE = /^[\[【]\s*[Uu][Dd]\s*[:：][^\]】\n]{0,200}[\]】](?!\()/;
const START_RE = /[\[【]\s*[Uu][Dd]\s*[:：]/;
const TOLERANT_AT_START_RE = new RegExp(`^${TOLERANT_SRC}`);

function chipHtml(
  token: string,
  n: number,
  info: Pick<CitationItem, 'status' | 'quote_status'> | undefined,
): string {
  // Defence in depth: the tokenizer only ever builds tokens from [0-9a-z#],
  // but nothing that does not have a token's exact shape reaches markup.
  if (!tokenParts(token)) return escapeHtml(token);
  const status = info && STATUSES.has(info.status) ? info.status : 'pending';
  const quote = info?.quote_status === 'mismatch' ? ' data-quote="mismatch"' : '';
  return `<button type="button" class="ud-cite" data-token="${token}" data-n="${n}"`
    + ` data-status="${status}"${quote} aria-label="Source ${n}">${n}</button>`;
}

/**
 * A marked extension that renders citation tokens as numbered chips:
 * `<button type="button" class="ud-cite" data-token="[ud:…]" data-n="2"
 * data-status="verified">2</button>`. The number is the token's first
 * appearance in the WHOLE message (computed in the preprocess hook), so a
 * token first cited inside a code span keeps the number the other renderers
 * give it even though no chip is drawn there.
 */
export function citationMarkedExtension(ctx: CitationRenderContext = {}): MarkedExtension {
  let numbers = new Map<string, number>();
  return {
    hooks: {
      preprocess(markdown: string) {
        numbers = numberTokens(markdown);
        return markdown;
      },
    },
    extensions: [{
      name: 'udCite',
      level: 'inline',
      start(src: string) {
        const m = START_RE.exec(src);
        return m ? m.index : undefined;
      },
      tokenizer(src: string): CiteToken | undefined {
        const m = TOLERANT_AT_START_RE.exec(src);
        if (m) {
          const cites: string[] = [];
          for (const one of m[1].matchAll(new RegExp(ONE_SRC, 'g'))) {
            const token = makeToken(one[1].toLowerCase(), one[2] ? one[2].toLowerCase() : null);
            if (!cites.includes(token)) cites.push(token);
          }
          return { type: 'udCite', raw: m[0], cites };
        }
        const near = NEAR_MISS_RE.exec(src);
        if (near) return { type: 'udCite', raw: near[0], cites: [] };
        return undefined;
      },
      renderer(token: Tokens.Generic) {
        const t = token as CiteToken;
        if (!t.cites.length) return escapeHtml(t.raw);
        return t.cites
          .map(cite => {
            if (!numbers.has(cite)) numbers.set(cite, numbers.size + 1);
            return chipHtml(cite, numbers.get(cite)!, ctx.lookup?.(cite));
          })
          .join('');
      },
    }],
  };
}

// ── plain text (copy to clipboard) ────────────────────────────────────────────

function statusSuffix(item: CitationItem): string {
  const notes: string[] = [];
  if (item.status === 'stale') notes.push('changed since cited');
  else if (item.status === 'removed') notes.push('source removed');
  else if (item.status === 'unissued' || item.status === 'invalid') notes.push('unverified');
  if (item.quote_status === 'mismatch') notes.push('quote does not match the source');
  return notes.length ? ` (${notes.join('; ')})` : '';
}

function describe(item: CitationItem | undefined): string {
  if (!item || !item.file) return item ? `(source unavailable)${statusSuffix(item)}` : '(source unavailable)';
  const { name, rel_path: relPath, web_link: webLink } = item.file;
  let line = name || relPath || item.file.fid;
  if (item.locator_label) line += `, ${item.locator_label}`;
  if (relPath && relPath !== name) line += ` — ${relPath}`;
  if (webLink) line += ` ${webLink}`;
  return line + statusSuffix(item);
}

/**
 * The message as plain text for the clipboard: every citation bracket becomes
 * its number(s) — "[ud:a#b; ud:c#d]" → "[1][2]" — and a "Sources:" list
 * follows, numbered the way the chips are. A token pasted anywhere else means
 * nothing; a numbered footnote with a file name does.
 */
export function toPlainFootnotes(
  text: string,
  items: Iterable<CitationItem> = [],
): string {
  const parsed = parseCitationTokens(text);
  if (!parsed.length) return text;
  const numbers = numberTokens(text);
  const byToken = new Map<string, CitationItem>();
  for (const item of items) byToken.set(item.token, item);

  let out = '';
  let cursor = 0;
  for (let i = 0; i < parsed.length;) {
    const { start, end } = parsed[i];
    const inBracket: string[] = [];
    for (; i < parsed.length && parsed[i].start === start; i++) {
      if (!inBracket.includes(parsed[i].token)) inBracket.push(parsed[i].token);
    }
    out += text.slice(cursor, start) + inBracket.map(t => `[${numbers.get(t)}]`).join('');
    cursor = end;
  }
  out += text.slice(cursor);

  const lines = Array.from(numbers, ([token, n]) => `[${n}] ${describe(byToken.get(token))}`);
  return `${out.trimEnd()}\n\nSources:\n${lines.join('\n')}`;
}
