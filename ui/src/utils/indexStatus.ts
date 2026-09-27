/**
 * The file tree's index status and the "Indexed content" preview — the pure
 * half, free of Vue, Pinia and Element Plus so node:test drives it directly
 * (tests/indexed-content.test.mjs).
 *
 * - What each status looks like: one icon, label and tone per content badge
 *   (`app/documents/content.py`) and per lookup state (outside, excluded, not
 *   indexed yet, removed), plus "index status unavailable" when the lookup
 *   itself could not answer.
 * - `IndexLookupController`: which visible files to look up and when. Paths
 *   are registered by the rows and tiles on screen (files only), looked up in
 *   batches of at most 500, debounced when new rows appear and throttled when
 *   the index changes under them. A session key (the auth token and the
 *   directory) guards every answer: one that lands after the profile or the
 *   directory changed is dropped, and a new session starts from an empty cache.
 * - The preview's paging reducer: pages append in source order, a passage
 *   split across two pages is joined back into one, and a stale cursor (the
 *   file was re-indexed) resets to the first page.
 * - Small formatters for the preview header: page coverage, metadata rows,
 *   where a reason's fix lives.
 *
 * Nothing here extracts, re-reads or calls a model: it only reads what the
 * index already holds, through the lookup and preview routes.
 */

import {
  LOOKUP_MAX_PATHS,
  type ContentBadge,
  type ContentPages,
  type ContentSegmentType,
  type ContentSummary,
  type DocumentsSnapshot,
  type FilePreviewPage,
  type IndexLookupItem,
  type IndexLookupResult,
  type PreviewSegment,
} from '../services/documentsApi';

// ── what a status looks like ───────────────────────────────────────────────

/** Every status a file row can show: a content badge for an indexed file,
 *  why a path is not in the index, or that the lookup could not answer. */
export type IndexStatusKind =
  | ContentBadge
  | 'outside'
  | 'excluded'
  | 'unmatched'
  | 'gone'
  | 'status_unavailable';

export type IndexStatusTone = 'ok' | 'info' | 'warn' | 'danger' | 'muted';

export interface IndexStatusPresentation {
  label: string;
  icon: string;
  tone: IndexStatusTone;
}

export const INDEX_STATUS_PRESENTATION: Readonly<Record<IndexStatusKind, IndexStatusPresentation>> = Object.freeze({
  waiting: { label: 'Waiting to be indexed', icon: 'mdi:clock-outline', tone: 'muted' },
  indexing: { label: 'Indexing…', icon: 'mdi:progress-clock', tone: 'info' },
  indexed: { label: 'Indexed content', icon: 'mdi:check-circle-outline', tone: 'ok' },
  partial: { label: 'Partly indexed', icon: 'mdi:circle-half-full', tone: 'warn' },
  metadata_only: { label: 'Metadata only', icon: 'mdi:card-text-outline', tone: 'muted' },
  blocked: { label: 'Blocked', icon: 'mdi:alert-circle-outline', tone: 'warn' },
  failed: { label: 'Failed', icon: 'mdi:close-circle-outline', tone: 'danger' },
  unknown: { label: 'Index status unknown', icon: 'mdi:help-circle-outline', tone: 'muted' },
  unavailable: { label: 'Not available', icon: 'mdi:minus-circle-outline', tone: 'muted' },
  outside: { label: 'Not in the indexed folder', icon: 'mdi:folder-off-outline', tone: 'muted' },
  excluded: { label: 'Excluded from indexing', icon: 'mdi:eye-off-outline', tone: 'muted' },
  unmatched: { label: 'Not indexed yet', icon: 'mdi:timer-sand', tone: 'muted' },
  gone: { label: 'Removed from the index', icon: 'mdi:file-remove-outline', tone: 'muted' },
  status_unavailable: { label: 'Index status unavailable', icon: 'mdi:database-alert-outline', tone: 'muted' },
});

const BADGES = new Set<string>([
  'waiting', 'indexing', 'indexed', 'partial', 'metadata_only', 'blocked', 'failed', 'unknown', 'unavailable',
]);

/** What the controller holds per path: the server's item, or a failed lookup. */
export type LookupEntry = IndexLookupItem | { state: 'error'; reason: string | null };

export interface IndexStatusView extends IndexStatusPresentation {
  kind: IndexStatusKind;
  /** The one sentence that says what it means: the summary's headline for
   *  an indexed file, an explanation otherwise. */
  detail: string;
  /** Label and detail together, for a tooltip. */
  tooltip: string;
  /** The indexed file's id; null when the path is not in the index. */
  fid: string | null;
  summary: ContentSummary | null;
}

const OUTSIDE_DETAIL: Record<string, string> = {
  foreign: "This file is in another profile's folder, which your index never reads.",
  system: "This file is in Cremind's own system folder, which is never indexed.",
  root: 'This is the indexed folder itself.',
};
const OUTSIDE_DEFAULT = 'Documentation search indexes your working directory; this file is outside it.';

const UNAVAILABLE_DETAIL: Record<string, string> = {
  root_unavailable: 'Your documents folder is not available right now, so its index cannot be checked.',
  no_index: 'The index has not been built yet.',
};

function kindOf(entry: LookupEntry): IndexStatusKind {
  switch (entry.state) {
    case 'indexed': {
      const badge = entry.summary?.badge;
      return badge && BADGES.has(badge) ? badge : 'unknown';
    }
    case 'outside':
    case 'excluded':
    case 'gone':
      return entry.state;
    case 'unmatched':
      return entry.reason && UNAVAILABLE_DETAIL[entry.reason] ? 'status_unavailable' : 'unmatched';
    default:
      return 'status_unavailable';
  }
}

function detailOf(entry: LookupEntry, kind: IndexStatusKind): string {
  if (entry.state === 'indexed') {
    return entry.summary?.headline || 'The index does not say what this file holds yet.';
  }
  const reason = entry.reason ?? '';
  switch (kind) {
    case 'outside':
      return OUTSIDE_DETAIL[reason] ?? OUTSIDE_DEFAULT;
    case 'excluded':
      return 'An exclusion rule (Settings → My Documents) keeps this file out of the index.';
    case 'unmatched':
      return 'This file is not in the index yet. New and changed files are picked up on the next sync.';
    case 'gone':
      return reason === 'missing'
        ? 'This file disappeared from the folder; its index entry is kept for now.'
        : 'This file was removed from the index.';
    default:
      return UNAVAILABLE_DETAIL[reason] ?? 'The index could not be checked right now.';
  }
}

/** How a lookup entry shows in the tree; null for nothing to show — also for
 *  a file in another profile's folder (a group seat working there): this
 *  profile's index has nothing to say about it, so no button is shown. */
export function describeIndexStatus(entry: LookupEntry | null | undefined): IndexStatusView | null {
  if (!entry || typeof entry !== 'object' || typeof entry.state !== 'string') return null;
  if (entry.state === 'outside' && entry.reason === 'foreign') return null;
  const kind = kindOf(entry);
  const look = INDEX_STATUS_PRESENTATION[kind];
  const detail = detailOf(entry, kind);
  return {
    kind,
    ...look,
    detail,
    tooltip: `${look.label} — ${detail}`,
    fid: entry.state === 'indexed' && entry.fid ? entry.fid : null,
    summary: entry.state === 'indexed' ? entry.summary ?? null : null,
  };
}

/** The status button's accessible name: which file, what state, what it does. */
export function indexStatusAriaLabel(view: IndexStatusView, fileName: string): string {
  return `Index status of ${fileName}: ${view.label}. Show what is indexed.`;
}

/** The status of a file straight from its content summary (the settings
 *  table's rows carry one). */
export function describeSummary(summary: ContentSummary | null | undefined, fid: string | null = null): IndexStatusView | null {
  if (!summary) return null;
  return describeIndexStatus({
    state: 'indexed', fid: fid ?? '', name: null, rel_path: null, kind: null, status: null, summary,
  });
}

/** The local folder source is on, per the documents snapshot: true or false,
 *  or null when no snapshot (or no per-source view) has arrived yet. */
export function localSourceEnabled(snap: DocumentsSnapshot | null | undefined): boolean | null {
  if (!snap || !snap.sources) return null;
  return !!snap.sources.local?.enabled;
}

/**
 * What a snapshot says about the index, without its stamp: two snapshots with
 * the same key describe the same index. Every snapshot carries a fresh
 * `seq`/`ts` — a status poll every 15 s included — so the stamp alone would
 * make an idle index look like it keeps changing.
 */
export function snapshotContentKey(snap: DocumentsSnapshot | null | undefined): string {
  if (!snap) return '';
  const { seq: _seq, ts: _ts, boot: _boot, ...rest } = snap;
  try {
    return JSON.stringify(rest);
  } catch {
    return `unkeyed:${snap.boot ?? ''}:${snap.seq ?? ''}`;
  }
}

// ── batching ───────────────────────────────────────────────────────────────

/** Split paths into lookup batches: de-duplicated, empty ones dropped, at
 *  most `size` (the server's cap, 500) per batch, order kept. */
export function chunkPaths(paths: Iterable<string>, size: number = LOOKUP_MAX_PATHS): string[][] {
  const step = Math.max(1, Math.min(LOOKUP_MAX_PATHS, Math.floor(size) || LOOKUP_MAX_PATHS));
  const unique: string[] = [];
  const seen = new Set<string>();
  for (const p of paths) {
    if (typeof p !== 'string' || !p || seen.has(p)) continue;
    seen.add(p);
    unique.push(p);
  }
  const out: string[][] = [];
  for (let i = 0; i < unique.length; i += step) out.push(unique.slice(i, i + step));
  return out;
}

/** The paths of a listing worth looking up: files only (a directory would
 *  come back `unmatched` anyway). */
export function lookupCandidates(entries: Iterable<{ path: string; is_dir: boolean }>): string[] {
  const out: string[] = [];
  for (const e of entries) if (e && !e.is_dir && e.path) out.push(e.path);
  return out;
}

// ── the lookup controller ──────────────────────────────────────────────────

export interface LookupSourceState {
  /** The local folder source is on (per the last lookup); null before one. */
  enabled: boolean | null;
  root: string | null;
  /** False: the folder or the index could not be read. */
  available: boolean;
}

export interface LookupControllerOptions {
  /** One `POST /files/lookup` for the current session (≤ 500 paths). */
  lookup: (paths: string[]) => Promise<IndexLookupResult>;
  /** Where entries go — a reactive Map in the app, a plain one in tests. */
  cache?: Map<string, LookupEntry>;
  state?: LookupSourceState;
  /** Wait after new rows appear, so a folder expanding registers as one batch. */
  debounceMs?: number;
  /** At most one full refresh this often while the index keeps changing. */
  refreshMs?: number;
  /** A failed lookup is tried again after this long. */
  retryMs?: number;
  batchSize?: number;
  /** Past this many entries, those no longer on screen are dropped. */
  maxCache?: number;
  now?: () => number;
}

export const LOOKUP_DEBOUNCE_MS = 80;
export const LOOKUP_REFRESH_MS = 2000;
export const LOOKUP_RETRY_MS = 15_000;

/**
 * Keeps the status of the files on screen current, with as few lookups as
 * that takes.
 *
 * - `register` / `unregister`: rows and tiles report themselves while shown
 *   (reference-counted, so two views of one path count once). A path not
 *   looked up in this session is fetched after `debounceMs`.
 * - `requestRefresh`: the index or the listing changed — every visible path
 *   is looked up again, at most once per `refreshMs` however often it is
 *   asked (a first sync sends many snapshots a second).
 * - One lookup runs at a time; a request made meanwhile runs after it.
 * - `setSession`: a new token or directory. Answers still in flight for the
 *   previous one are dropped when they land, and the cache starts empty.
 * - `sourceDisabled`: the local folder was turned off — nothing is shown or
 *   looked up until `sourceMaybeEnabled` (a lookup answering "off" has the
 *   same effect).
 */
export class IndexLookupController {
  readonly cache: Map<string, LookupEntry>;
  readonly state: LookupSourceState;

  private readonly lookup: (paths: string[]) => Promise<IndexLookupResult>;
  private readonly debounceMs: number;
  private readonly refreshMs: number;
  private readonly retryMs: number;
  private readonly batchSize: number;
  private readonly maxCache: number;
  private readonly now: () => number;

  private readonly visible = new Map<string, number>();
  /** Looked up in this session since the last invalidation. */
  private readonly fresh = new Set<string>();
  private session = '';
  private epoch = 0;
  private invalidations = 0;
  private invalidated = false;
  private suppressed = false;
  private inFlight = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private dueAt = Infinity;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private lastRunAt = -Infinity;

  constructor(opts: LookupControllerOptions) {
    this.lookup = opts.lookup;
    this.cache = opts.cache ?? new Map();
    this.state = opts.state ?? { enabled: null, root: null, available: true };
    this.debounceMs = opts.debounceMs ?? LOOKUP_DEBOUNCE_MS;
    this.refreshMs = opts.refreshMs ?? LOOKUP_REFRESH_MS;
    this.retryMs = opts.retryMs ?? LOOKUP_RETRY_MS;
    this.batchSize = opts.batchSize ?? LOOKUP_MAX_PATHS;
    this.maxCache = opts.maxCache ?? 3000;
    this.now = opts.now ?? (() => Date.now());
  }

  /** A row or tile for `path` is on screen. */
  register(path: string): void {
    if (!path) return;
    const n = this.visible.get(path) ?? 0;
    this.visible.set(path, n + 1);
    // This path itself is the pending work: no need to scan for any.
    if (n === 0 && !this.fresh.has(path)) this.kick(true);
  }

  /** …and is gone again. */
  unregister(path: string): void {
    const n = this.visible.get(path);
    if (!n) return;
    if (n <= 1) this.visible.delete(path);
    else this.visible.set(path, n - 1);
  }

  visiblePaths(): string[] {
    return [...this.visible.keys()];
  }

  get sessionKey(): string {
    return this.session;
  }

  /** The profile's token or the directory changed. Returns whether it did. */
  setSession(key: string): boolean {
    if (key === this.session) return false;
    this.session = key;
    this.stop();
    this.cache.clear();
    this.fresh.clear();
    this.invalidated = false;
    this.suppressed = false;
    this.state.enabled = null;
    this.state.root = null;
    this.state.available = true;
    this.kick();
    return true;
  }

  /** The index or the listing changed: look every visible path up again. */
  requestRefresh(): void {
    if (this.suppressed) return;
    this.fresh.clear();
    this.invalidations += 1;
    this.invalidated = true;
    this.kick();
  }

  /** The local folder source is off: show nothing, look nothing up. */
  sourceDisabled(): void {
    this.stop();
    this.cache.clear();
    this.fresh.clear();
    this.invalidated = false;
    this.suppressed = true;
    this.state.enabled = false;
    this.state.root = null;
  }

  /** It may be on (a new snapshot says so, or says nothing): check again. */
  sourceMaybeEnabled(): void {
    if (this.suppressed) {
      this.suppressed = false;
      this.fresh.clear();
      this.invalidated = false;
      this.kick();
      return;
    }
    this.requestRefresh();
  }

  /** Stop timers and drop whatever is in flight. */
  dispose(): void {
    this.stop();
  }

  // ── internals ────────────────────────────────────────────────────────────

  private stop() {
    this.epoch += 1;
    this.inFlight = false;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.dueAt = Infinity;
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private pending(): boolean {
    for (const p of this.visible.keys()) if (!this.fresh.has(p)) return true;
    return false;
  }

  private kick(knownPending = false) {
    // A run in flight kicks again when it finishes.
    if (this.suppressed || this.inFlight) return;
    if (!knownPending && !this.pending()) return;
    this.schedule(this.invalidated ? this.refreshDelay() : this.debounceMs);
  }

  private refreshDelay(): number {
    return Math.max(this.debounceMs, this.lastRunAt + this.refreshMs - this.now());
  }

  private schedule(delay: number) {
    const wait = Math.max(0, delay);
    const due = this.now() + wait;
    if (this.timer !== null) {
      if (this.dueAt <= due) return;
      clearTimeout(this.timer);
    }
    this.dueAt = due;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.dueAt = Infinity;
      void this.run();
    }, wait);
  }

  private scheduleRetry() {
    if (this.retryTimer !== null) return;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.requestRefresh();
    }, this.retryMs);
  }

  private disableFromLookup() {
    this.cache.clear();
    this.fresh.clear();
    this.invalidated = false;
    this.suppressed = true;
    this.state.enabled = false;
    this.state.root = null;
  }

  private prune() {
    if (this.cache.size <= this.maxCache) return;
    for (const p of [...this.cache.keys()]) {
      if (!this.visible.has(p)) {
        this.cache.delete(p);
        this.fresh.delete(p);
      }
    }
  }

  private async run(): Promise<void> {
    if (this.inFlight || this.suppressed) return;
    const paths = [...this.visible.keys()].filter(p => !this.fresh.has(p));
    if (!paths.length) return;
    const epoch = this.epoch;
    const invalidations = this.invalidations;
    this.invalidated = false;
    this.inFlight = true;
    this.lastRunAt = this.now();
    let failed = false;
    try {
      for (const chunk of chunkPaths(paths, this.batchSize)) {
        let res: IndexLookupResult;
        try {
          res = await this.lookup(chunk);
        } catch {
          if (epoch !== this.epoch) return;
          failed = true;
          for (const p of chunk) {
            const held = this.cache.get(p);
            if (!held || held.state === 'error') this.cache.set(p, { state: 'error', reason: null });
            // Not again at once: the retry timer (or the next change) asks.
            this.fresh.add(p);
          }
          continue;
        }
        // The profile or the directory changed while this was in flight.
        if (epoch !== this.epoch) return;
        if (!res || res.enabled !== true) {
          this.disableFromLookup();
          return;
        }
        this.state.enabled = true;
        this.state.root = res.root ?? null;
        this.state.available = res.available !== false;
        const items = res.items && typeof res.items === 'object' ? res.items : {};
        for (const p of chunk) {
          const item = items[p];
          this.cache.set(
            p,
            item && typeof item === 'object' && typeof item.state === 'string'
              ? item
              : { state: 'error', reason: null },
          );
          // Changed again while in flight: this answer may already be old.
          if (this.invalidations === invalidations) this.fresh.add(p);
        }
      }
    } finally {
      if (epoch === this.epoch) {
        this.inFlight = false;
        this.prune();
        if (failed) this.scheduleRetry();
        this.kick();
      }
    }
  }
}

// ── the preview ────────────────────────────────────────────────────────────

export type PreviewHeader = Omit<FilePreviewPage, 'segments' | 'next_cursor' | 'start_index'>;

export interface PreviewState {
  fid: string | null;
  loading: boolean;
  /** The file's summary and metadata, from the latest page. */
  header: PreviewHeader | null;
  /** Passages in source order; a passage split across pages is one entry. */
  segments: PreviewSegment[];
  nextCursor: string | null;
  revision: string | null;
  error: string | null;
  /** The file is not (or no longer) in the index. */
  notFound: boolean;
  /** The file was re-indexed while pages were loaded, so they started over. */
  restarted: boolean;
}

export const EMPTY_PREVIEW: PreviewState = Object.freeze({
  fid: null,
  loading: false,
  header: null,
  segments: [],
  nextCursor: null,
  revision: null,
  error: null,
  notFound: false,
  restarted: false,
}) as PreviewState;

/**
 * A request for `fid` is on its way. `fromStart` drops the loaded pages (a
 * reload, or another file); `restarted` says the reload happened because the
 * file was re-indexed underneath them.
 */
export function previewBegin(
  state: PreviewState,
  fid: string,
  fromStart: boolean,
  restarted = false,
): PreviewState {
  if (fromStart || state.fid !== fid) {
    return {
      ...EMPTY_PREVIEW,
      fid,
      loading: true,
      // Keep the header while the first page reloads, so it does not flicker.
      header: state.fid === fid ? state.header : null,
      restarted: state.fid === fid && restarted,
    };
  }
  return { ...state, loading: true, error: null };
}

function joinParts(prev: PreviewSegment, next: PreviewSegment): PreviewSegment {
  const text = prev.text + next.text;
  const start = prev.part?.start ?? 0;
  const length = next.part?.length ?? prev.part?.length ?? text.length;
  const end = next.part?.end ?? start + text.length;
  const whole = start === 0 && end >= length;
  const joined: PreviewSegment = { ...prev, text };
  if (whole) delete joined.part;
  else joined.part = { start, end, length };
  return joined;
}

/** Append `incoming` to `held`, joining a passage continued from the last one. */
export function mergeSegments(held: readonly PreviewSegment[], incoming: readonly PreviewSegment[]): PreviewSegment[] {
  const out = held.slice();
  for (const seg of incoming) {
    const last = out[out.length - 1];
    if (last && last.index === seg.index && (seg.part?.start ?? 0) > 0) {
      out[out.length - 1] = joinParts(last, seg);
    } else {
      out.push(seg);
    }
  }
  return out;
}

/**
 * A page arrived. `append` adds it after the loaded ones — only when it is the
 * same file and the same revision; otherwise it replaces them.
 */
export function previewReceive(state: PreviewState, page: FilePreviewPage, append: boolean): PreviewState {
  const { segments, next_cursor: nextCursor, start_index: _start, ...header } = page;
  const continues = append && state.fid === page.fid && state.revision === page.revision;
  return {
    ...state,
    fid: page.fid || state.fid,
    loading: false,
    header,
    segments: mergeSegments(continues ? state.segments : [], Array.isArray(segments) ? segments : []),
    nextCursor: nextCursor ?? null,
    revision: page.revision ?? null,
    error: null,
    notFound: false,
  };
}

/** The cursor belonged to an earlier revision: start again from the first page. */
export function previewStale(state: PreviewState): PreviewState {
  return {
    ...EMPTY_PREVIEW,
    fid: state.fid,
    loading: true,
    header: state.header,
    restarted: true,
  };
}

export function previewFailed(state: PreviewState, message: string, notFound = false): PreviewState {
  return { ...state, loading: false, error: message, notFound };
}

/** The summary's reasons worth listing under its headline: the headline
 *  already says the first one in so many words, so it is not repeated. */
export function reasonsBeyondHeadline(summary: ContentSummary | null | undefined): ContentSummary['reasons'] {
  const headline = summary?.headline ?? '';
  return (summary?.reasons ?? []).filter(r => !r.message || !headline.includes(r.message));
}

/** Passages loaded so far (a split one counts once). */
export function loadedPassages(state: PreviewState): number {
  return new Set(state.segments.map(s => s.index)).size;
}

// ── the preview header ─────────────────────────────────────────────────────

const COUNT = new Intl.NumberFormat('en-US');

function plural(n: number, word: string, many = `${word}s`): string {
  return `${COUNT.format(n)} ${n === 1 ? word : many}`;
}

/** "12 passages (34,567 characters)" / "No readable text". */
export function readableText(summary: ContentSummary | null | undefined): string {
  const passages = Number(summary?.readable?.passages) || 0;
  const chars = Number(summary?.readable?.chars) || 0;
  if (!passages) return 'No readable text';
  return `${plural(passages, 'passage')} (${plural(chars, 'character')})`;
}

/** A PDF's extraction coverage, e.g. ["12 pages", "4 with text", "6 OCR done",
 *  "1 blank", "1 pending OCR"]. Zero counts are left out (the total is not). */
export function coverageParts(pages: ContentPages | null | undefined): string[] {
  if (!pages) return [];
  const out: string[] = [];
  const total = typeof pages.total === 'number' ? pages.total : null;
  if (total !== null) out.push(plural(total, 'page'));
  if (total !== null && typeof pages.read === 'number' && pages.read < total) {
    out.push(`${COUNT.format(pages.read)} read`);
  }
  const add = (n: number | null | undefined, text: string) => {
    if (typeof n === 'number' && n > 0) out.push(`${COUNT.format(n)} ${text}`);
  };
  add(pages.text, 'with text');
  add(pages.ocr_done, 'OCR done');
  add(pages.blank, 'blank');
  add(pages.pending, 'pending OCR');
  add(pages.failed, 'OCR failed');
  add(pages.truncated, 'cut off');
  add(pages.unreadable, 'unreadable');
  return out;
}

/** Vectors behind the text, when not all are ready: search by keyword works
 *  meanwhile. Null when there is nothing to say. */
export function embeddingText(summary: ContentSummary | null | undefined): string | null {
  const emb = summary?.embedding;
  if (!emb || !emb.total) return null;
  if (emb.state === 'partial') {
    return `Semantic search: ${COUNT.format(emb.ready)} of ${COUNT.format(emb.total)} passages ready.`;
  }
  if (emb.state === 'pending') return 'Semantic search is not ready for this file yet; keyword search works.';
  return null;
}

/** The small tag that sets a passage apart from plain text. */
export function segmentTypeLabel(type: ContentSegmentType | string | null | undefined): string | null {
  if (type === 'ocr') return 'OCR';
  if (type === 'image_description') return 'Image description';
  return null;
}

export type ReasonLink =
  | { kind: 'route'; route: 'llm-settings' | 'documents-settings'; label: string }
  | { kind: 'retry'; label: string };

/** Where a reason's fix lives: a settings page, the retry button, or nowhere. */
export function reasonLink(action: string | null | undefined): ReasonLink | null {
  switch (action) {
    case 'vision_model':
      return { kind: 'route', route: 'llm-settings', label: 'Choose a vision model' };
    case 'vision_consent':
      return { kind: 'route', route: 'documents-settings', label: 'Review image descriptions' };
    case 'install':
      return { kind: 'route', route: 'documents-settings', label: 'Open My Documents' };
    case 'retry':
      return { kind: 'retry', label: 'Retry extraction' };
    default:
      return null;
  }
}

/** What a summary says about content, as one comparable string: a change
 *  means the stored text may have changed and the preview should reload. */
export function summarySignature(summary: ContentSummary | null | undefined): string {
  if (!summary) return '';
  return [summary.state, summary.badge, summary.indexed_at ?? '', summary.readable?.passages ?? 0].join('|');
}

const META_LABELS: Record<string, string> = {
  title: 'Title',
  author: 'Author',
  last_modified_by: 'Last modified by',
  subject: 'Subject',
  keywords: 'Keywords',
  created: 'Created',
  modified: 'Modified',
  pages: 'Pages',
  sheets: 'Sheets',
  slides: 'Slides',
  words: 'Words',
  app: 'Application',
  encoding: 'Encoding',
  legal: 'Legal',
};

function humanize(key: string): string {
  const text = key.replace(/[_-]+/g, ' ').trim();
  return text ? text[0].toUpperCase() + text.slice(1) : key;
}

function metaValue(v: unknown): string | null {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  if (Array.isArray(v)) {
    const parts = v.map(metaValue).filter((x): x is string => !!x);
    return parts.length ? parts.join(', ') : null;
  }
  try {
    return JSON.stringify(v);
  } catch {
    return null;
  }
}

/** Document metadata as label/value rows, in the server's order. A nested
 *  object (the legal-document fields) becomes one row per field. */
export function metadataRows(doc: Record<string, unknown> | null | undefined): { label: string; value: string }[] {
  const rows: { label: string; value: string }[] = [];
  if (!doc || typeof doc !== 'object') return rows;
  for (const [key, v] of Object.entries(doc)) {
    const label = META_LABELS[key] ?? humanize(key);
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      for (const [sub, sv] of Object.entries(v as Record<string, unknown>)) {
        const value = metaValue(sv);
        if (value) rows.push({ label: `${label} ${humanize(sub).toLowerCase()}`, value });
      }
      continue;
    }
    const value = metaValue(v);
    if (value) rows.push({ label, value });
  }
  return rows;
}
