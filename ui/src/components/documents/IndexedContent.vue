<script setup lang="ts">
/**
 * What the index holds for one file: its content summary, then the exact
 * passages it stored, in source order — the answer to "was my file indexed,
 * and what did search actually get out of it?".
 *
 * Used by the file tree's "Indexed content" pane and by Settings → My
 * Documents' file table. It shows the index, nothing else: opening it reads
 * the stored passages (`GET …/files/{fid}/preview`) and never extracts,
 * transcribes or calls a model. Only "Reindex" / "Retry extraction" asks the
 * engine to read the file again.
 *
 * - The header: status, when it was indexed, how much readable text there is,
 *   a PDF's page coverage, the summary's one-sentence headline and each
 *   reason, with a link to where it is fixed.
 * - The passages: each labelled with where it sits (page, section) and its
 *   heading; OCR transcriptions and image descriptions are tagged apart from
 *   the file's own text. A passage split across two pages reads as one.
 * - "File details": the metadata card and the document's own metadata —
 *   kept apart from the text, as the index keeps them apart.
 * - Pages load with "Load more"; if the file is re-indexed in between, the
 *   next page is refused (409) and the view starts again from the top.
 *
 * Document text is rendered only through text interpolation — never as HTML.
 */
import { computed, ref, shallowRef, watch } from 'vue';
import { useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import { ElMessage } from 'element-plus';
import { useSettingsStore } from '../../stores/settings';
import { useDocumentsStore } from '../../stores/documents';
import {
  DocumentsApiError,
  PREVIEW_DEFAULT_LIMIT,
  StalePreviewError,
  controlDocuments,
  getFilePreview,
} from '../../services/documentsApi';
import {
  DocumentsApiError as CitationsApiError,
  fetchDocumentRaw,
} from '../../services/documentsCitationsApi';
import { openFileInNewTab } from '../../utils/openFile';
import { formatAgo, safeWebLink, sourceLabel } from '../../utils/documentsView';
import {
  EMPTY_PREVIEW,
  coverageParts,
  describeSummary,
  embeddingText,
  loadedPassages,
  metadataRows,
  previewBegin,
  previewFailed,
  previewReceive,
  previewStale,
  readableText,
  reasonLink,
  reasonsBeyondHeadline,
  segmentTypeLabel,
  summarySignature,
  type PreviewState,
  type ReasonLink,
} from '../../utils/indexStatus';

const props = withDefaults(defineProps<{
  /** The file's 8-character id in the index. */
  fid: string;
  /** The absolute path the file tree lists: "Open original" then opens it the
   *  way the tree does. Without one, the index's copy (`…/raw`) is used. */
  path?: string | null;
  /** The conversation the file tree is scoped to (widens file access). */
  conversationId?: string | null;
  /** The file's summary as the tree's lookup last saw it; when it moves
   *  (re-indexed, OCR finished) the view reloads. */
  liveSignature?: string | null;
}>(), { path: null, conversationId: null, liveSignature: null });

const emit = defineEmits<{
  /** A reason's fix lives on a settings page. */
  navigate: [route: 'llm-settings' | 'documents-settings'];
  /** A reindex was queued. */
  reindexed: [];
}>();

const settings = useSettingsStore();
const documents = useDocumentsStore();
const router = useRouter();

const preview = shallowRef<PreviewState>(EMPTY_PREVIEW);
const reindexing = ref(false);
const opening = ref(false);

const header = computed(() => preview.value.header);
const summary = computed(() => header.value?.summary ?? null);
const status = computed(() => describeSummary(summary.value, props.fid));
const coverage = computed(() => coverageParts(summary.value?.pages));
const embeddingNote = computed(() => embeddingText(summary.value));
const indexedWhen = computed(() => {
  const ms = summary.value?.indexed_at;
  if (!ms) return 'Not yet';
  return `${new Date(ms).toLocaleString()} (${formatAgo(ms)})`;
});
// A reason the headline already says keeps only its fix link here.
const reasons = computed(() => {
  const beyond = new Set(reasonsBeyondHeadline(summary.value));
  return (summary.value?.reasons ?? [])
    .map((r, i) => ({
      key: `${r.code}:${i}`,
      message: beyond.has(r) ? r.message : '',
      link: reasonLink(r.action) as ReasonLink | null,
    }))
    .filter(r => r.message || r.link);
});
const metaRows = computed(() => metadataRows(header.value?.metadata?.document));
const card = computed(() => header.value?.metadata?.card || '');
const fileRows = computed(() => {
  const h = header.value;
  if (!h) return [];
  const rows: { label: string; value: string }[] = [];
  if (h.rel_path) rows.push({ label: 'Path', value: h.rel_path });
  if (h.kind) rows.push({ label: 'Kind', value: h.kind });
  if (h.source) rows.push({ label: 'Source', value: sourceLabel(h.source) });
  if (h.status) rows.push({ label: 'Index status', value: h.status });
  return rows;
});
const loaded = computed(() => loadedPassages(preview.value));
const total = computed(() => header.value?.total_segments ?? 0);
const isDrive = computed(() => header.value?.source === 'drive');
/** "Retry extraction" when reading went wrong or stopped short; plain
 *  "Reindex" otherwise (including content that is never read, like a video). */
const RETRY_BADGES = new Set(['failed', 'blocked', 'partial', 'unknown']);
const reindexLabel = computed(() =>
  (summary.value && RETRY_BADGES.has(summary.value.badge) ? 'Retry extraction' : 'Reindex'));
const busyIndexing = computed(() => {
  const s = summary.value;
  return !!s && (s.phase === 'queued' || s.phase === 'indexing');
});

// ── loading ─────────────────────────────────────────────────────────────────

// Bumped on every request, so an answer for a superseded one (another file,
// a reload, a profile switch) is dropped when it lands.
let generation = 0;

function describeError(e: unknown): { message: string; notFound: boolean } {
  if (e instanceof DocumentsApiError && e.status === 404) {
    return { message: 'This file is no longer in the index.', notFound: true };
  }
  if (e instanceof DocumentsApiError && e.code === 'EngineNotRunning') {
    return { message: 'The document indexing engine is not running on this server.', notFound: false };
  }
  return {
    message: `Could not load the indexed content: ${e instanceof Error ? e.message : String(e)}`,
    notFound: false,
  };
}

async function fetchPage(cursor: string | null, append: boolean, gen: number, token: string, fid: string) {
  try {
    const page = await getFilePreview(settings.agentUrl, token, fid, cursor, PREVIEW_DEFAULT_LIMIT);
    if (gen !== generation || token !== settings.authToken) return;
    preview.value = previewReceive(preview.value, page, append);
  } catch (e) {
    if (gen !== generation || token !== settings.authToken) return;
    if (e instanceof StalePreviewError && cursor) {
      // Re-indexed since the last page: never stitch two versions together.
      preview.value = previewStale(preview.value);
      await fetchPage(null, false, gen, token, fid);
      return;
    }
    const { message, notFound } = describeError(e);
    preview.value = previewFailed(preview.value, message, notFound);
  }
}

/** Load from the first page (`restarted`: because the file changed). */
async function reload(restarted = false) {
  const fid = props.fid;
  if (!fid) return;
  generation += 1;
  preview.value = previewBegin(preview.value, fid, true, restarted);
  await fetchPage(null, false, generation, settings.authToken, fid);
}

async function loadMore() {
  const fid = props.fid;
  const cursor = preview.value.nextCursor;
  if (!fid || !cursor || preview.value.loading) return;
  generation += 1;
  preview.value = previewBegin(preview.value, fid, false);
  await fetchPage(cursor, true, generation, settings.authToken, fid);
}

watch(() => props.fid, () => {
  preview.value = EMPTY_PREVIEW;
  void reload();
}, { immediate: true });

// A profile switch: nothing of the previous profile's file may stay on screen,
// and its id means nothing in the next profile's index (the owner closes the
// view; until then it stays empty).
watch(() => settings.authToken, () => {
  generation += 1;
  preview.value = EMPTY_PREVIEW;
});

watch(() => props.liveSignature, (sig) => {
  if (!sig || preview.value.loading || !summary.value) return;
  if (sig === summarySignature(summary.value)) return;
  void reload(preview.value.segments.length > 0);
});

// ── actions ─────────────────────────────────────────────────────────────────

async function reindex() {
  const fid = props.fid;
  if (!fid || reindexing.value) return;
  reindexing.value = true;
  try {
    const res = await controlDocuments(settings.agentUrl, settings.authToken, {
      action: 'reindex',
      targets: [fid],
      ...(isDrive.value ? { source: 'drive' as const } : {}),
    });
    documents.applySnapshot(res.snapshot);
    ElMessage.success('This file is queued to be read again. Its content updates here when that is done.');
    emit('reindexed');
    await reload();
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e));
  } finally {
    reindexing.value = false;
  }
}

/** Types a browser would run as a page: never opened from a blob URL of this
 *  origin (the server already sends them as downloads — belt and braces). */
const ACTIVE_TYPE = /(html|xml|svg|javascript|ecmascript)/i;

function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(new Blob([blob], { type: 'application/octet-stream' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name || 'file';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

async function openOriginal() {
  if (opening.value) return;
  opening.value = true;
  try {
    if (props.path) {
      // The way the file tree opens it (double-click).
      await openFileInNewTab(settings.agentUrl, settings.authToken, props.path, props.conversationId || undefined);
      return;
    }
    const blob = await fetchDocumentRaw(settings.agentUrl, settings.authToken, props.fid);
    const type = blob.type || 'application/octet-stream';
    if (type === 'application/octet-stream' || ACTIVE_TYPE.test(type)) {
      download(blob, header.value?.name || 'file');
      return;
    }
    const url = URL.createObjectURL(blob);
    const win = window.open(url, '_blank');
    setTimeout(() => URL.revokeObjectURL(url), win ? 60_000 : 0);
    if (!win) ElMessage.warning('Allow pop-ups for Cremind to open the file.');
  } catch (e) {
    if (e instanceof CitationsApiError && e.code === 'DriveFile') {
      const link = safeWebLink(e.webLink);
      if (link) window.open(link, '_blank', 'noopener,noreferrer');
      else ElMessage.info('This file lives in Google Drive — open it there.');
    } else if (e instanceof CitationsApiError && e.status === 404) {
      ElMessage.warning('This file is no longer available.');
    } else {
      ElMessage.warning(`Could not open the file: ${e instanceof Error ? e.message : String(e)}`);
    }
  } finally {
    opening.value = false;
  }
}

function linkHref(link: ReasonLink): string {
  if (link.kind !== 'route' || !settings.profileId) return '#';
  try {
    return router.resolve({ name: link.route, params: { profile: settings.profileId } }).href;
  } catch {
    return '#';
  }
}

function onReasonLink(link: ReasonLink) {
  if (link.kind === 'route') emit('navigate', link.route);
  else void reindex();
}

defineExpose({ reload });
</script>

<template>
  <div class="ic" :aria-busy="preview.loading">
    <!-- Summary -->
    <section v-if="summary && status" class="ic-summary" aria-label="What is indexed">
      <div class="ic-status" :class="`tone-${status.tone}`">
        <Icon :icon="status.icon" class="ic-status-icon" aria-hidden="true" />
        <span>{{ status.label }}</span>
      </div>
      <p class="ic-headline">{{ summary.headline }}</p>
      <dl class="ic-facts">
        <dt>Indexed</dt>
        <dd>{{ indexedWhen }}</dd>
        <dt>Readable text</dt>
        <dd>{{ readableText(summary) }}</dd>
        <template v-if="coverage.length">
          <dt>Pages</dt>
          <dd>{{ coverage.join(' · ') }}</dd>
        </template>
        <template v-if="embeddingNote">
          <dt>Search</dt>
          <dd>{{ embeddingNote }}</dd>
        </template>
      </dl>
      <ul v-if="reasons.length" class="ic-reasons">
        <li v-for="r in reasons" :key="r.key">
          <Icon icon="mdi:information-outline" class="ic-reason-icon" aria-hidden="true" />
          <span class="ic-reason-text">
            {{ r.message }}
            <template v-if="r.link">
              <a
                v-if="r.link.kind === 'route'"
                :href="linkHref(r.link)"
                class="ic-link"
                @click.prevent="onReasonLink(r.link)"
              >{{ r.link.label }}</a>
              <button
                v-else
                type="button"
                class="ic-link ic-link-btn"
                :disabled="reindexing"
                @click="onReasonLink(r.link)"
              >{{ r.link.label }}</button>
            </template>
          </span>
        </li>
      </ul>
    </section>

    <!-- Actions -->
    <div v-if="header" class="ic-actions">
      <button type="button" class="ic-btn" :disabled="opening" @click="openOriginal">
        <Icon :icon="isDrive && !path ? 'mdi:google-drive' : 'mdi:open-in-new'" aria-hidden="true" />
        Open original
      </button>
      <button
        type="button"
        class="ic-btn"
        :disabled="reindexing || busyIndexing"
        :title="busyIndexing ? 'This file is already queued to be indexed.' : 'Read this file again and update its index'"
        @click="reindex"
      >
        <Icon icon="mdi:database-refresh-outline" aria-hidden="true" />
        {{ reindexing ? 'Queuing…' : reindexLabel }}
      </button>
      <button
        type="button"
        class="ic-btn ic-btn-icon"
        :disabled="preview.loading"
        title="Reload what the index holds"
        aria-label="Reload what the index holds"
        @click="reload()"
      >
        <Icon icon="mdi:refresh" aria-hidden="true" />
      </button>
    </div>

    <div v-if="preview.restarted" class="ic-note" role="status">
      <Icon icon="mdi:information-outline" aria-hidden="true" />
      <span>This file was re-indexed, so its content was reloaded from the start.</span>
    </div>

    <!-- File details: metadata, kept apart from the text -->
    <details v-if="header" class="ic-details">
      <summary>File details</summary>
      <dl v-if="fileRows.length || metaRows.length" class="ic-meta">
        <template v-for="row in fileRows" :key="`f:${row.label}`">
          <dt>{{ row.label }}</dt>
          <dd>{{ row.value }}</dd>
        </template>
        <template v-for="(row, i) in metaRows" :key="`m:${i}`">
          <dt>{{ row.label }}</dt>
          <dd>{{ row.value }}</dd>
        </template>
      </dl>
      <template v-if="card">
        <div class="ic-subhead">Metadata card (searchable by name and details)</div>
        <pre class="ic-card">{{ card }}</pre>
      </template>
    </details>

    <!-- The stored passages -->
    <section class="ic-text" aria-label="Indexed text">
      <div v-if="!preview.segments.length && preview.loading" class="ic-empty">Loading…</div>
      <div v-else-if="preview.error && !preview.segments.length" class="ic-empty ic-error">
        <p>{{ preview.error }}</p>
        <button v-if="!preview.notFound" type="button" class="ic-btn" @click="reload()">Try again</button>
      </div>
      <div v-else-if="header && !preview.segments.length" class="ic-empty">
        <p class="ic-empty-title">No readable text is stored for this file.</p>
        <p v-if="summary">{{ summary.headline }}</p>
      </div>

      <ol v-if="preview.segments.length" class="ic-segments">
        <li
          v-for="seg in preview.segments"
          :key="seg.index"
          class="ic-seg"
          :class="`type-${seg.type}`"
        >
          <div
            v-if="seg.locator_label || seg.heading || segmentTypeLabel(seg.type)"
            class="ic-seg-head"
          >
            <span v-if="seg.locator_label" class="ic-seg-loc">{{ seg.locator_label }}</span>
            <span v-if="seg.heading" class="ic-seg-heading">{{ seg.heading }}</span>
            <span v-if="segmentTypeLabel(seg.type)" class="ic-seg-type">{{ segmentTypeLabel(seg.type) }}</span>
          </div>
          <div class="ic-seg-text">{{ seg.text }}</div>
          <div v-if="seg.part && seg.part.end < seg.part.length" class="ic-seg-cont">
            This passage continues — load more to read the rest.
          </div>
        </li>
      </ol>

      <div v-if="preview.segments.length" class="ic-more">
        <span class="ic-count">Showing {{ loaded }} of {{ total }} passages</span>
        <button
          v-if="preview.nextCursor"
          type="button"
          class="ic-btn"
          :disabled="preview.loading"
          @click="loadMore"
        >
          {{ preview.loading ? 'Loading…' : 'Load more' }}
        </button>
      </div>
      <p v-if="preview.error && preview.segments.length" class="ic-inline-error" role="alert">{{ preview.error }}</p>
      <p
        v-if="preview.segments.length && !preview.nextCursor && summary && summary.state === 'partial'"
        class="ic-end-note"
      >
        End of the indexed text. {{ summary.headline }}
      </p>
    </section>
  </div>
</template>

<style scoped>
.ic {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  font-size: 0.82rem;
  color: var(--text-primary);
}

.ic-summary {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--surface-color);
}
.ic-status {
  --tone: var(--text-tertiary);
  display: inline-flex;
  align-items: center;
  gap: 5px;
  align-self: flex-start;
  padding: 1px 8px 1px 6px;
  border-radius: 999px;
  font-weight: 600;
  font-size: 0.78rem;
  color: var(--tone);
  background: color-mix(in srgb, var(--tone) 13%, transparent);
}
.ic-status-icon { font-size: 0.95rem; }
.tone-ok { --tone: var(--success-color); }
.tone-info { --tone: var(--primary-color); }
.tone-warn { --tone: var(--warning-color); }
.tone-danger { --tone: var(--danger-color); }
.tone-muted { --tone: var(--text-secondary); }
.ic-headline {
  margin: 0;
  line-height: 1.45;
  overflow-wrap: anywhere;
}
.ic-facts,
.ic-meta {
  display: grid;
  grid-template-columns: minmax(0, max-content) minmax(0, 1fr);
  gap: 3px 10px;
  margin: 0;
  font-size: 0.78rem;
}
.ic-facts dt,
.ic-meta dt { color: var(--text-tertiary); }
.ic-facts dd,
.ic-meta dd {
  margin: 0;
  overflow-wrap: anywhere;
  color: var(--text-secondary);
}
.ic-reasons {
  list-style: none;
  margin: 2px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ic-reasons li {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  line-height: 1.4;
  color: var(--text-secondary);
}
.ic-reason-icon { flex-shrink: 0; margin-top: 2px; color: var(--text-tertiary); }
.ic-reason-text { min-width: 0; overflow-wrap: anywhere; }
.ic-link {
  margin-left: 4px;
  color: var(--primary-color);
  text-decoration: underline;
  cursor: pointer;
}
.ic-link-btn {
  padding: 0;
  border: none;
  background: none;
  font: inherit;
}
.ic-link-btn:disabled { opacity: 0.5; cursor: default; }

.ic-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.ic-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 9px;
  border: 1px solid var(--border-color);
  border-radius: 5px;
  background: var(--surface-color);
  color: var(--text-primary);
  font: inherit;
  font-size: 0.78rem;
  cursor: pointer;
}
.ic-btn:hover:not(:disabled) {
  border-color: var(--primary-color);
  color: var(--primary-color);
}
.ic-btn:focus-visible,
.ic-link:focus-visible {
  outline: 2px solid var(--primary-color);
  outline-offset: 1px;
}
.ic-btn:disabled { opacity: 0.55; cursor: default; }
.ic-btn-icon { padding: 3px 6px; }

.ic-note {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  padding: 6px 8px;
  border-radius: 6px;
  line-height: 1.4;
  color: var(--text-primary);
  border: 1px solid color-mix(in srgb, var(--primary-color) 35%, transparent);
  background: color-mix(in srgb, var(--primary-color) 8%, transparent);
}

.ic-details {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--surface-color);
  padding: 6px 10px;
}
.ic-details > summary {
  cursor: pointer;
  font-weight: 600;
  font-size: 0.78rem;
  color: var(--text-secondary);
}
.ic-details[open] > summary { margin-bottom: 6px; }
.ic-subhead {
  margin: 8px 0 4px;
  font-size: 0.72rem;
  color: var(--text-tertiary);
}
.ic-card {
  margin: 0;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--surface-hover);
  font-family: inherit;
  font-size: 0.76rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--text-secondary);
}

.ic-text {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ic-segments {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ic-seg {
  --seg-accent: var(--border-color);
  border: 1px solid var(--border-color);
  border-left: 3px solid var(--seg-accent);
  border-radius: 6px;
  padding: 6px 9px;
  background: var(--surface-color);
}
.ic-seg.type-ocr { --seg-accent: var(--warning-color); }
.ic-seg.type-image_description { --seg-accent: var(--primary-color); }
.ic-seg-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 8px;
  margin-bottom: 4px;
  font-size: 0.72rem;
  color: var(--text-tertiary);
}
.ic-seg-loc { font-weight: 600; color: var(--text-secondary); }
.ic-seg-heading {
  min-width: 0;
  overflow-wrap: anywhere;
}
.ic-seg-type {
  padding: 0 6px;
  border-radius: 8px;
  font-size: 0.68rem;
  font-weight: 600;
  color: var(--seg-accent);
  background: color-mix(in srgb, var(--seg-accent) 14%, transparent);
}
.ic-seg-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.5;
}
.ic-seg-cont {
  margin-top: 4px;
  font-size: 0.72rem;
  font-style: italic;
  color: var(--text-tertiary);
}

.ic-more {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.ic-count { font-size: 0.74rem; color: var(--text-tertiary); }
.ic-empty {
  padding: 14px 10px;
  border: 1px dashed var(--border-color);
  border-radius: 8px;
  text-align: center;
  color: var(--text-secondary);
  line-height: 1.45;
}
.ic-empty p { margin: 0 0 6px; }
.ic-empty p:last-child { margin-bottom: 0; }
.ic-empty-title { font-weight: 600; color: var(--text-primary); }
.ic-error p { color: var(--danger-color); }
.ic-inline-error { margin: 0; color: var(--danger-color); font-size: 0.78rem; }
.ic-end-note {
  margin: 0;
  font-size: 0.76rem;
  line-height: 1.4;
  color: var(--text-secondary);
}
</style>
