<script setup lang="ts">
/**
 * What a citation points at, opened from a chip or the Sources list.
 *
 * The point of a citation is that the reader can check it, so this shows the
 * cited passage in place — highlighted among its neighbours, with the file,
 * where it sits (page, lines, sheet range) and whether the server could verify
 * it. What "in place" means depends on the file:
 *
 * - text, code, Office documents, CSV and sheets: the extracted text around
 *   the passage (GET …/text), line-numbered when the locator has lines, with
 *   earlier/later to walk the file;
 * - PDF: the same, plus "Open at page N" — the original fetched with the
 *   Bearer token into a blob URL with `#page=N`, because a plain link would
 *   be refused (the server takes no cookie);
 * - images: the server's in-memory thumbnail, the caption and the EXIF;
 * - Google Drive files: "Open in Google Drive" (their bytes are not served);
 * - metadata-only kinds (executables, archives, media, folders): a card.
 *
 * A stale citation shows the passage as the agent read it next to the current
 * text; a removed one shows only that snapshot. The drawer never renders a
 * file's own bytes as a page: an HTML file opened from a blob URL would run in
 * this app's origin, so only PDFs (forced to application/pdf) open in a tab
 * and everything else is offered as a download.
 */
import { computed, nextTick, ref, watch } from 'vue';
import { Icon } from '@iconify/vue';
import { ElDrawer, ElMessage } from 'element-plus';
import { useSettingsStore } from '../../stores/settings';
import { useAuthedBlobUrl } from '../../composables/useAuthedBlobUrl';
import { citationStatusInfo, tokenParts, type CitationItem } from '../../utils/citations';
import { iconFor } from '../../utils/fileIcons';
import {
  UserDocsApiError,
  fetchCitedText,
  fetchUserDocFile,
  fetchUserDocRaw,
  userDocThumbnailUrl,
  type CitedSegment,
  type CitedTextQuery,
  type UserDocFileDetail,
} from '../../services/userdocsCitationsApi';

const props = defineProps<{
  modelValue: boolean;
  /** The canonical token that was clicked. */
  token: string;
  /** Its number in the message. */
  n?: number;
  /** The verified item, when known. Without one the token alone is enough:
   *  its cite id is the file's fid, its hash part the chunk. */
  item?: CitationItem | null;
}>();
const emit = defineEmits<{ (e: 'update:modelValue', open: boolean): void }>();

const settings = useSettingsStore();

// Content is never read for these; the index keeps name, path, size, dates
// (app/userdocs/types.py METADATA_ONLY_KINDS), plus folder citations.
const METADATA_KINDS = new Set([
  'audio', 'video', 'archive', 'executable', 'database', 'font', 'bundle', 'encrypted', 'other', 'folder',
]);
// Rendered monospaced: alignment carries meaning.
const MONO_KINDS = new Set(['code', 'csv', 'json', 'xml', 'xlsx', 'xls', 'ods']);

const parts = computed(() => tokenParts(props.token));
const fid = computed(() => props.item?.file?.fid || parts.value?.citeId || '');
const citedC8 = computed(() => parts.value?.c8 ?? null);

const detail = ref<UserDocFileDetail | null>(null);
const detailGone = ref(false);
const segments = ref<CitedSegment[]>([]);
const truncated = ref(false);
const textError = ref('');
const loadingText = ref(false);
// Whether the window has been moved off the one the citation opened on.
const shifted = ref(false);
const atStart = ref(false);
const atEnd = ref(false);
const bodyEl = ref<HTMLElement | null>(null);

const kind = computed(() => props.item?.file?.kind || detail.value?.kind || '');
const isDrive = computed(() => (props.item?.file?.source || detail.value?.source) === 'drive');
const name = computed(() => props.item?.file?.name || detail.value?.name || 'Cited source');
const relPath = computed(() => props.item?.file?.rel_path || detail.value?.rel_path || '');
const status = computed(() => citationStatusInfo(props.item));
const snapshot = computed(() => props.item?.snippet || '');
const icon = computed(() => iconFor({ name: name.value, is_dir: kind.value === 'folder' }));

/** Only an https link is ever put in an href — a server-supplied string must
 *  not become a `javascript:` URL. */
const webLink = computed(() => {
  const link = props.item?.file?.web_link || detail.value?.web_link || '';
  return /^https:\/\//i.test(link) ? link : '';
});

const view = computed<'removed' | 'image' | 'metadata' | 'text'>(() => {
  if (props.item?.status === 'removed' || detailGone.value) return 'removed';
  if (kind.value === 'image') return 'image';
  if (METADATA_KINDS.has(kind.value)) return 'metadata';
  return 'text';
});

const locatorLabel = computed(() => {
  if (props.item?.locator_label) return props.item.locator_label;
  return segments.value.find(isCited)?.locator_label || '';
});

const citedPage = computed<number | null>(() => {
  const fromItem = Number(props.item?.locator?.page);
  if (Number.isFinite(fromItem) && fromItem > 0) return fromItem;
  const fromSeg = Number(segments.value.find(isCited)?.locator?.page);
  return Number.isFinite(fromSeg) && fromSeg > 0 ? fromSeg : null;
});

function isCited(seg: CitedSegment): boolean {
  if (citedC8.value) return tokenParts(seg.token)?.c8 === citedC8.value;
  // A whole-file citation: the server's highlight is the passage it opened
  // on, meaningful only until the window moves.
  return seg.highlight && !shifted.value;
}

/** A segment as numbered lines, when its locator says where it starts. */
function numberedLines(seg: CitedSegment): { n: number; text: string }[] | null {
  const first = Number(seg.locator?.line_start);
  if (!Number.isFinite(first) || first < 1) return null;
  return seg.text.split('\n').map((text, i) => ({ n: first + i, text }));
}

const rendered = computed(() => segments.value.map((seg, i) => ({
  key: seg.token || `${i}:${seg.locator_label}`,
  seg,
  lines: numberedLines(seg),
  cited: isCited(seg),
})));

// ── loading ─────────────────────────────────────────────────────────────────

let generation = 0;

function range(a: unknown, b: unknown): string | null {
  const start = Number(a);
  if (!Number.isFinite(start) || start < 1) return null;
  const end = Number(b);
  return Number.isFinite(end) && end > start ? `${start}-${end}` : String(start);
}

function openingQuery(): CitedTextQuery {
  if (citedC8.value) return { chunk: citedC8.value, context: 2 };
  // A whole-file citation has no chunk; aim at where its locator points.
  const loc = props.item?.locator ?? {};
  return { pages: range(loc.page, loc.page_end), lines: range(loc.line_start, loc.line_end), context: 2 };
}

async function loadText(query: CitedTextQuery, direction: -1 | 0 | 1 = 0) {
  const gen = generation;
  loadingText.value = true;
  textError.value = '';
  try {
    const res = await fetchCitedText(settings.agentUrl, settings.authToken, fid.value, query);
    if (gen !== generation) return;
    const before = segments.value;
    // Re-centring on the edge segment and getting the same edge back means
    // the window is already at that end of the file.
    if (direction < 0 && before.length && res.segments[0]?.token === before[0].token) {
      atStart.value = true;
      return;
    }
    if (direction > 0 && before.length
      && res.segments[res.segments.length - 1]?.token === before[before.length - 1].token) {
      atEnd.value = true;
      return;
    }
    segments.value = res.segments;
    truncated.value = res.truncated;
    if (direction !== 0) {
      shifted.value = true;
      if (direction < 0) atEnd.value = false;
      else atStart.value = false;
    }
    await nextTick();
    const cited = bodyEl.value?.querySelector('.seg.cited') as HTMLElement | null;
    if (direction === 0) cited?.scrollIntoView({ block: 'center' });
  } catch (e: any) {
    if (gen !== generation) return;
    textError.value = e instanceof UserDocsApiError && e.status === 404
      ? (props.item?.status === 'stale'
        ? 'That passage is no longer in the file.'
        : 'The text of this file is not available.')
      : `Could not load the text: ${e?.message || e}`;
  } finally {
    if (gen === generation) loadingText.value = false;
  }
}

async function load() {
  generation += 1;
  const gen = generation;
  detail.value = null;
  detailGone.value = false;
  segments.value = [];
  truncated.value = false;
  textError.value = '';
  shifted.value = false;
  atStart.value = false;
  atEnd.value = false;
  if (!fid.value || props.item?.status === 'removed') return;
  // A folder citation has no file record to fetch; the item says it all.
  if (kind.value === 'folder') return;

  const detailLoad = fetchUserDocFile(settings.agentUrl, settings.authToken, fid.value)
    .then(d => { if (gen === generation) detail.value = d; })
    .catch(e => {
      if (gen !== generation) return;
      if (e instanceof UserDocsApiError && e.status === 404) detailGone.value = true;
    });
  // The kind decides what to show; without an item it comes from the record.
  if (!kind.value) await detailLoad;
  if (gen !== generation) return;
  if (view.value === 'text') await loadText(openingQuery());
  await detailLoad;
}

watch(
  () => [props.modelValue, props.token] as const,
  ([open]) => { if (open) void load(); },
  { immediate: true },
);

function shift(direction: -1 | 1) {
  const list = segments.value;
  const edge = direction < 0 ? list[0] : list[list.length - 1];
  const c8 = edge ? tokenParts(edge.token)?.c8 : null;
  if (c8) void loadText({ chunk: c8, context: 2 }, direction);
}

// ── image ───────────────────────────────────────────────────────────────────

const image = useAuthedBlobUrl(() =>
  props.modelValue && view.value === 'image' && fid.value
    ? userDocThumbnailUrl(settings.agentUrl, fid.value, 1024)
    : null,
);

const caption = computed(() => detail.value?.caption || snapshot.value);

const exifRows = computed(() => {
  const ex = detail.value?.exif;
  if (!ex || typeof ex !== 'object') return [];
  const rows: { label: string; value: string }[] = [];
  const taken = detail.value?.taken_at
    ? new Date(detail.value.taken_at).toLocaleString()
    : (typeof ex.taken_at === 'string' ? ex.taken_at : '');
  if (taken) rows.push({ label: 'Taken', value: taken });
  const camera = [ex.make, ex.model].filter(v => typeof v === 'string' && v).join(' ');
  if (camera) rows.push({ label: 'Camera', value: camera });
  if (ex.width && ex.height) rows.push({ label: 'Size', value: `${ex.width} × ${ex.height}` });
  const lat = Number(ex.gps?.lat);
  const lon = Number(ex.gps?.lon);
  if (Number.isFinite(lat) && Number.isFinite(lon) && ex.gps) {
    rows.push({ label: 'Location', value: `${lat.toFixed(5)}, ${lon.toFixed(5)}` });
  }
  if (typeof ex.software === 'string' && ex.software) rows.push({ label: 'Software', value: ex.software });
  return rows;
});

// ── metadata card ───────────────────────────────────────────────────────────

function formatBytes(n: number | null | undefined): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}

function formatWhen(ms: number | null | undefined): string {
  return typeof ms === 'number' && ms > 0 ? new Date(ms).toLocaleString() : '';
}

const metaRows = computed(() => {
  const d = detail.value;
  const rows: { label: string; value: string }[] = [];
  const push = (label: string, value: unknown) => {
    if (value !== null && value !== undefined && value !== '') rows.push({ label, value: String(value) });
  };
  push('Kind', kind.value);
  push('Path', relPath.value);
  if (d) {
    push('Size', formatBytes(d.size));
    push('Modified', formatWhen(d.modified));
    push('Indexed', formatWhen(d.indexed_at));
    if (d.status && d.status !== 'indexed') push('Status', d.status_reason ? `${d.status} — ${d.status_reason}` : d.status);
    const meta = d.doc_meta && typeof d.doc_meta === 'object' ? d.doc_meta : {};
    for (const key of ['title', 'author', 'subject', 'pages', 'app']) {
      const v = meta[key];
      if (typeof v === 'string' || typeof v === 'number') push(key[0].toUpperCase() + key.slice(1), v);
    }
  }
  return rows;
});

// ── opening the original ────────────────────────────────────────────────────

async function openPdfAtPage(page: number | null) {
  // Open the tab now, inside the click, so pop-up blockers allow it.
  const tab = window.open('', '_blank');
  if (!tab) {
    ElMessage.warning('Allow pop-ups for Cremind to open the PDF.');
    return;
  }
  try {
    const bytes = await fetchUserDocRaw(settings.agentUrl, settings.authToken, fid.value);
    // Forced to PDF whatever the response said, so the browser's PDF viewer
    // — not this origin — is what renders it.
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/pdf' }));
    tab.location.href = page ? `${url}#page=${page}` : url;
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (e: any) {
    tab.close();
    notifyOpenFailure(e);
  }
}

async function downloadOriginal() {
  try {
    const bytes = await fetchUserDocRaw(settings.agentUrl, settings.authToken, fid.value);
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = name.value;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  } catch (e: any) {
    notifyOpenFailure(e);
  }
}

function notifyOpenFailure(e: any) {
  if (e instanceof UserDocsApiError && e.code === 'DriveFile') {
    ElMessage.info('This file lives in Google Drive — open it there.');
  } else if (e instanceof UserDocsApiError && e.status === 404) {
    ElMessage.warning('This file is no longer available.');
  } else {
    ElMessage.warning(`Could not open the file: ${e?.message || e}`);
  }
}

const close = () => emit('update:modelValue', false);
</script>

<template>
  <ElDrawer
    :model-value="modelValue"
    direction="rtl"
    size="min(600px, 94vw)"
    append-to-body
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <template #header>
      <div class="ud-viewer-head">
        <span v-if="n" class="ud-viewer-n" :class="`tone-${status.tone}`">{{ n }}</span>
        <Icon :icon="icon" class="ud-viewer-icon" />
        <div class="ud-viewer-titles">
          <div class="ud-viewer-name" :title="name">{{ name }}</div>
          <div class="ud-viewer-sub">
            <span v-if="relPath" class="ud-viewer-path" :title="relPath">{{ relPath }}</span>
            <span class="ud-viewer-source">
              <Icon :icon="isDrive ? 'mdi:google-drive' : 'mdi:folder-outline'" />
              {{ isDrive ? 'Google Drive' : 'Local folder' }}
            </span>
            <span v-if="locatorLabel" class="ud-viewer-locator">{{ locatorLabel }}</span>
          </div>
        </div>
      </div>
    </template>

    <div ref="bodyEl" class="ud-viewer-body">
      <!-- Verification: what the reader should know before trusting the passage.
           (A removed source says so in its own view below.) -->
      <div v-if="status.note && view !== 'removed'" class="ud-viewer-note" :class="`tone-${status.tone}`">
        <Icon
          :icon="status.tone === 'ok' ? 'mdi:information-outline' : 'mdi:alert-outline'"
          class="ud-viewer-note-icon"
        />
        <div><strong>{{ status.label }}.</strong> {{ status.note }}</div>
      </div>
      <div v-if="status.quoteNote" class="ud-viewer-note tone-danger">
        <Icon icon="mdi:format-quote-close" class="ud-viewer-note-icon" />
        <div><strong>Quote mismatch.</strong> {{ status.quoteNote }}</div>
      </div>

      <div class="ud-viewer-actions">
        <a
          v-if="isDrive && webLink"
          :href="webLink"
          target="_blank"
          rel="noopener noreferrer"
          class="ud-viewer-btn"
        >
          <Icon icon="mdi:google-drive" /> Open in Google Drive
        </a>
        <button
          v-else-if="kind === 'pdf' && view !== 'removed'"
          type="button"
          class="ud-viewer-btn"
          @click="openPdfAtPage(citedPage)"
        >
          <Icon icon="mdi:file-pdf-box" />
          {{ citedPage ? `Open at page ${citedPage}` : 'Open PDF' }}
        </button>
        <button
          v-if="!isDrive && view !== 'removed' && kind !== 'folder' && kind !== 'bundle'"
          type="button"
          class="ud-viewer-btn subtle"
          @click="downloadOriginal"
        >
          <Icon icon="mdi:download" /> Download
        </button>
      </div>

      <!-- Removed: only what the agent saw is left. -->
      <template v-if="view === 'removed'">
        <div class="ud-viewer-note tone-gone">
          <Icon icon="mdi:file-remove-outline" class="ud-viewer-note-icon" />
          <div><strong>Source removed.</strong> The file is no longer in your indexed documents.</div>
        </div>
        <template v-if="snapshot">
          <div class="ud-viewer-section">As cited</div>
          <blockquote class="ud-viewer-snapshot">{{ snapshot }}</blockquote>
        </template>
      </template>

      <!-- Image: the server-made thumbnail, caption and EXIF. -->
      <template v-else-if="view === 'image'">
        <div class="ud-viewer-image">
          <img v-if="image.url.value" :src="image.url.value" :alt="name" />
          <div v-else-if="image.failed.value" class="ud-viewer-empty">The image could not be loaded.</div>
          <div v-else class="ud-viewer-empty">Loading…</div>
        </div>
        <template v-if="caption">
          <div class="ud-viewer-section">Caption</div>
          <p class="ud-viewer-caption">{{ caption }}</p>
        </template>
        <template v-if="exifRows.length">
          <div class="ud-viewer-section">Photo details</div>
          <dl class="ud-viewer-meta">
            <template v-for="row in exifRows" :key="row.label">
              <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
            </template>
          </dl>
        </template>
      </template>

      <!-- Metadata only: content is never read for these kinds. -->
      <template v-else-if="view === 'metadata'">
        <div class="ud-viewer-card">
          <Icon :icon="icon" class="ud-viewer-card-icon" />
          <dl class="ud-viewer-meta">
            <template v-for="row in metaRows" :key="row.label">
              <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
            </template>
          </dl>
        </div>
        <p class="ud-viewer-hint">
          Only this file's name and details are indexed — its content is never read.
        </p>
      </template>

      <!-- Text-like: the passage among its neighbours. -->
      <template v-else>
        <template v-if="item?.status === 'stale' && snapshot">
          <div class="ud-viewer-section">As cited</div>
          <blockquote class="ud-viewer-snapshot">{{ snapshot }}</blockquote>
          <div class="ud-viewer-section">Current text</div>
        </template>

        <div v-if="segments.length" class="ud-viewer-nav">
          <button type="button" class="ud-viewer-btn subtle" :disabled="loadingText || atStart" @click="shift(-1)">
            <Icon icon="mdi:chevron-up" /> Earlier
          </button>
          <span v-if="loadingText" class="ud-viewer-loading">Loading…</span>
        </div>

        <div
          v-for="row in rendered"
          :key="row.key"
          class="seg"
          :class="{ cited: row.cited, mono: MONO_KINDS.has(kind) }"
        >
          <div v-if="row.seg.locator_label" class="seg-label">{{ row.seg.locator_label }}</div>
          <table v-if="row.lines" class="seg-lines">
            <tbody>
              <tr v-for="line in row.lines" :key="line.n">
                <td class="seg-ln">{{ line.n }}</td>
                <td class="seg-lt">{{ line.text }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="seg-text">{{ row.seg.text }}</div>
        </div>

        <div v-if="segments.length" class="ud-viewer-nav">
          <button type="button" class="ud-viewer-btn subtle" :disabled="loadingText || atEnd" @click="shift(1)">
            <Icon icon="mdi:chevron-down" /> Later
          </button>
        </div>

        <p v-if="truncated" class="ud-viewer-hint">Long passages are shortened here.</p>
        <div v-if="!segments.length && loadingText" class="ud-viewer-empty">Loading…</div>
        <div v-else-if="textError" class="ud-viewer-empty">{{ textError }}</div>
        <template v-if="textError && snapshot && item?.status !== 'stale'">
          <div class="ud-viewer-section">As cited</div>
          <blockquote class="ud-viewer-snapshot">{{ snapshot }}</blockquote>
        </template>
      </template>
    </div>

    <template #footer>
      <button type="button" class="ud-viewer-btn subtle" @click="close">Close</button>
    </template>
  </ElDrawer>
</template>

<style scoped>
.ud-viewer-head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.ud-viewer-n {
  flex-shrink: 0;
  min-width: 22px;
  height: 22px;
  padding: 0 5px;
  border-radius: 11px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 0.75rem;
  font-weight: 700;
  color: var(--primary-color);
  background: color-mix(in srgb, var(--primary-color) 14%, transparent);
}
.ud-viewer-n.tone-warn {
  color: var(--warning-color);
  background: color-mix(in srgb, var(--warning-color) 16%, transparent);
}
.ud-viewer-n.tone-gone {
  color: var(--text-tertiary);
  background: color-mix(in srgb, var(--text-tertiary) 14%, transparent);
  text-decoration: line-through;
}
.ud-viewer-icon { flex-shrink: 0; font-size: 1.5rem; }
.ud-viewer-titles { min-width: 0; }
.ud-viewer-name {
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ud-viewer-sub {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  margin-top: 2px;
  font-size: 0.75rem;
  color: var(--text-secondary);
}
.ud-viewer-path {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ud-viewer-source { display: inline-flex; align-items: center; gap: 3px; }
.ud-viewer-locator {
  padding: 0 6px;
  border-radius: 8px;
  background: var(--surface-hover);
  color: var(--text-primary);
}

.ud-viewer-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 0.85rem;
  color: var(--text-primary);
}

.ud-viewer-note {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 8px 10px;
  border-radius: 8px;
  line-height: 1.35;
  border: 1px solid color-mix(in srgb, var(--primary-color) 35%, transparent);
  background: color-mix(in srgb, var(--primary-color) 8%, transparent);
}
.ud-viewer-note.tone-warn {
  border-color: color-mix(in srgb, var(--warning-color) 45%, transparent);
  background: color-mix(in srgb, var(--warning-color) 10%, transparent);
}
.ud-viewer-note.tone-gone {
  border-color: var(--border-color);
  background: var(--surface-hover);
  color: var(--text-secondary);
}
.ud-viewer-note.tone-danger {
  border-color: color-mix(in srgb, var(--danger-color) 45%, transparent);
  background: color-mix(in srgb, var(--danger-color) 9%, transparent);
}
.ud-viewer-note-icon { flex-shrink: 0; margin-top: 1px; font-size: 1.05rem; }
.tone-warn .ud-viewer-note-icon { color: var(--warning-color); }
.tone-danger .ud-viewer-note-icon { color: var(--danger-color); }

.ud-viewer-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.ud-viewer-actions:empty { display: none; }

.ud-viewer-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 12px;
  border-radius: 6px;
  border: 1px solid var(--primary-color);
  background: var(--primary-color);
  color: white;
  font: inherit;
  font-size: 0.8rem;
  text-decoration: none;
  cursor: pointer;
}
.ud-viewer-btn:hover { filter: brightness(1.08); }
.ud-viewer-btn.subtle {
  border-color: var(--border-color);
  background: var(--surface-color);
  color: var(--text-primary);
}
.ud-viewer-btn.subtle:hover {
  filter: none;
  border-color: var(--primary-color);
  color: var(--primary-color);
}
.ud-viewer-btn:disabled {
  opacity: 0.5;
  cursor: default;
  pointer-events: none;
}

.ud-viewer-section {
  font-size: 0.72rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-tertiary);
}

.ud-viewer-snapshot {
  margin: 0;
  padding: 8px 12px;
  border-left: 3px solid var(--border-color);
  background: var(--surface-hover);
  border-radius: 0 6px 6px 0;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.4;
}

.ud-viewer-nav {
  display: flex;
  align-items: center;
  gap: 10px;
}
.ud-viewer-loading { font-size: 0.75rem; color: var(--text-tertiary); }

.seg {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 8px 10px;
  background: var(--surface-color);
}
.seg.cited {
  border-color: color-mix(in srgb, var(--primary-color) 60%, transparent);
  background: color-mix(in srgb, var(--primary-color) 9%, var(--surface-color));
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary-color) 18%, transparent);
}
.seg-label {
  font-size: 0.72rem;
  color: var(--text-tertiary);
  margin-bottom: 4px;
}
.seg.cited .seg-label { color: var(--primary-color); font-weight: 600; }
.seg-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.45;
}
.seg.mono .seg-text,
.seg-lines {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 0.78rem;
}
.seg.mono .seg-text {
  white-space: pre;
  overflow-x: auto;
}
.seg-lines {
  border-collapse: collapse;
  width: 100%;
}
.seg-ln {
  width: 1%;
  padding-right: 10px;
  text-align: right;
  vertical-align: top;
  color: var(--text-tertiary);
  user-select: none;
  white-space: nowrap;
}
.seg-lt {
  white-space: pre-wrap;
  word-break: break-word;
}

.ud-viewer-image {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 160px;
  border-radius: 8px;
  background: var(--surface-hover);
  overflow: hidden;
}
.ud-viewer-image img {
  max-width: 100%;
  max-height: 60vh;
  object-fit: contain;
  display: block;
}
.ud-viewer-caption { margin: 0; line-height: 1.45; white-space: pre-wrap; }

.ud-viewer-card {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  padding: 12px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--surface-color);
}
.ud-viewer-card-icon { font-size: 2.2rem; flex-shrink: 0; }

.ud-viewer-meta {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  margin: 0;
  font-size: 0.8rem;
}
.ud-viewer-meta dt { color: var(--text-tertiary); }
.ud-viewer-meta dd { margin: 0; word-break: break-word; }

.ud-viewer-hint { margin: 0; font-size: 0.75rem; color: var(--text-tertiary); }
.ud-viewer-empty {
  padding: 16px;
  text-align: center;
  color: var(--text-tertiary);
}
</style>
