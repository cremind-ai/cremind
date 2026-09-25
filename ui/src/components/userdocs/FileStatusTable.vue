<script setup lang="ts">
/**
 * Every file in the index with its status — the place to answer "why isn't my
 * file found?". Tabs filter by status (their counts come with each page),
 * the search box matches names and paths, and pages load by keyset
 * ("Load more" continues after the last (path, id) seen), so a 100k-file index
 * never has to be counted or offset through.
 *
 * Reindex is emitted, not sent: the page runs every control action, so their
 * errors and the snapshot they return land in one place.
 *
 * With Google Drive on, files come from two sources: a source filter and
 * column appear, and a Drive file links to its page in Drive (https links
 * only — the link is server-supplied).
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElButton, ElInput, ElOption, ElSelect, ElTable, ElTableColumn } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../../stores/settings';
import {
  listUserDocsFiles,
  UserDocsApiError,
  type UserDocsFileRow,
  type UserDocsSourceKind,
} from '../../services/userdocsApi';
import {
  STATUS_ORDER,
  captionStateLabel,
  formatBytes,
  formatCount,
  reasonLabel,
  safeWebLink,
  sourceLabel,
  statusLabel,
} from '../../utils/userdocsView';

const props = withDefaults(defineProps<{
  /** Bump to reload from the first page (after a reindex, a rescan…). */
  refreshKey?: number;
  busy?: boolean;
  /** Files come from more than one source (Google Drive is on). */
  showSource?: boolean;
}>(), { refreshKey: 0, busy: false, showSource: false });

const emit = defineEmits<{
  /** `source` is set for Drive files only (the server defaults to local). */
  reindex: [targets: string[], source?: UserDocsSourceKind];
  'engine-down': [];
}>();

const settingsStore = useSettingsStore();

const PAGE = 100;
const status = ref<string>('');
const source = ref<'' | UserDocsSourceKind>('');
const query = ref('');
const files = ref<UserDocsFileRow[]>([]);
const counts = ref<Record<string, number>>({});
const next = ref<{ rel_path: string; id: number } | null>(null);
const loading = ref(false);
const note = ref('');
let loadSeq = 0;

const total = computed(() =>
  Object.entries(counts.value)
    .filter(([s]) => s !== 'tombstone')
    .reduce((sum, [, n]) => sum + (Number(n) || 0), 0));

const tabs = computed(() => {
  const withFiles = STATUS_ORDER.filter(s => (counts.value[s] ?? 0) > 0);
  // Keep the selected tab visible even once its count drops to zero.
  if (status.value && !withFiles.includes(status.value)) withFiles.push(status.value);
  return [
    { status: '', label: 'All', n: total.value },
    ...withFiles.map(s => ({ status: s, label: statusLabel(s), n: counts.value[s] ?? 0 })),
  ];
});

async function load(reset: boolean) {
  const seq = ++loadSeq;
  loading.value = true;
  try {
    const page = await listUserDocsFiles(settingsStore.agentUrl, settingsStore.authToken, {
      status: status.value || null,
      source: (props.showSource && source.value) || null,
      q: query.value.trim() || null,
      after: reset ? null : next.value,
      limit: PAGE,
    });
    if (seq !== loadSeq) return;
    files.value = reset ? page.files : [...files.value, ...page.files];
    counts.value = page.counts ?? {};
    next.value = page.next;
    note.value = '';
  } catch (e) {
    if (seq !== loadSeq) return;
    if (reset) {
      files.value = [];
      next.value = null;
    }
    if (e instanceof UserDocsApiError && e.code === 'EngineNotRunning') {
      emit('engine-down');
      note.value = '';
    } else if (e instanceof UserDocsApiError && e.code === 'NotEnabled') {
      note.value = 'Nothing has been indexed yet.';
    } else {
      note.value = e instanceof Error ? e.message : 'Could not load the files.';
    }
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

let searchTimer: ReturnType<typeof setTimeout> | null = null;
watch(query, () => {
  if (searchTimer !== null) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { searchTimer = null; void load(true); }, 300);
});
watch(status, () => { void load(true); });
watch(source, () => { void load(true); });
watch(() => props.refreshKey, () => { void load(true); });
// Drive turned off: back to one source (the filter would hide the rest).
watch(() => props.showSource, (shown) => { if (!shown) source.value = ''; });

const SOURCE_OPTIONS: { value: '' | UserDocsSourceKind; label: string }[] = [
  { value: '', label: 'All sources' },
  { value: 'local', label: sourceLabel('local') },
  { value: 'drive', label: sourceLabel('drive') },
];

// Table slot rows are typed loosely by ElTable, hence Partial.
function isDrive(row: Partial<UserDocsFileRow>): boolean {
  return row.source === 'drive';
}

function reindexRow(row: Partial<UserDocsFileRow>) {
  const target = row.fid || row.rel_path || '';
  if (!target) return;
  if (isDrive(row)) emit('reindex', [target], 'drive');
  else emit('reindex', [target]);
}

onMounted(() => { void load(true); });
onBeforeUnmount(() => { if (searchTimer !== null) clearTimeout(searchTimer); });

function folderOf(rel: string): string {
  const i = rel.lastIndexOf('/');
  return i > 0 ? rel.slice(0, i) : '';
}

function formatDate(ms: number | null): string {
  if (!ms) return '';
  return new Date(ms).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

/** Jump to one status (the activity panel's "Show all" for failures). */
function showStatus(target: string) {
  status.value = target;
}

defineExpose({ showStatus, reload: () => load(true) });
</script>

<template>
  <div class="files">
    <div class="files-bar">
      <div class="tabs" role="tablist">
        <button
          v-for="tab in tabs"
          :key="tab.status || 'all'"
          type="button"
          role="tab"
          class="tab"
          :class="[{ active: status === tab.status }, tab.status ? `st-${tab.status}` : '']"
          :aria-selected="status === tab.status"
          @click="status = tab.status"
        >
          {{ tab.label }} <span class="tab-n">{{ formatCount(tab.n) }}</span>
        </button>
      </div>
      <div class="files-filters">
        <ElSelect
          v-if="showSource"
          v-model="source"
          size="small"
          class="files-source"
          aria-label="Source"
        >
          <ElOption v-for="o in SOURCE_OPTIONS" :key="o.value || 'all'" :value="o.value" :label="o.label" />
        </ElSelect>
        <ElInput
          v-model="query"
          size="small"
          clearable
          placeholder="Search names and paths"
          class="files-search"
        >
          <template #prefix><Icon icon="mdi:magnify" /></template>
        </ElInput>
      </div>
    </div>

    <p v-if="note" class="note">{{ note }}</p>

    <ElTable
      v-else
      v-loading="loading && !files.length"
      :data="files"
      row-key="id"
      size="small"
      class="files-table"
      empty-text="No files"
    >
      <ElTableColumn v-if="showSource" label="Source" width="64" align="center">
        <template #default="{ row }">
          <Icon
            :icon="isDrive(row) ? 'mdi:google-drive' : 'mdi:laptop'"
            class="cell-source"
            :class="{ drive: isDrive(row) }"
            :title="sourceLabel(row.source)"
            :aria-label="sourceLabel(row.source)"
          />
        </template>
      </ElTableColumn>
      <ElTableColumn label="File" min-width="260">
        <template #default="{ row }">
          <div class="cell-file">
            <span class="cell-name" :title="row.rel_path">{{ row.name }}</span>
            <span v-if="folderOf(row.rel_path)" class="cell-folder" :title="row.rel_path">
              {{ folderOf(row.rel_path) }}
            </span>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Status" min-width="190">
        <template #default="{ row }">
          <div class="cell-status">
            <span class="status-dot" :class="`st-${row.status}`" />
            <span>{{ statusLabel(row.status) }}</span>
          </div>
          <div v-if="row.status_reason" class="cell-reason">{{ reasonLabel(row.status_reason) }}</div>
          <div v-else-if="captionStateLabel(row.caption_state)" class="cell-reason">
            {{ captionStateLabel(row.caption_state) }}
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Size" width="90" align="right">
        <template #default="{ row }">{{ row.size != null ? formatBytes(row.size) : '' }}</template>
      </ElTableColumn>
      <ElTableColumn label="Modified" width="120">
        <template #default="{ row }">{{ formatDate(row.modified) }}</template>
      </ElTableColumn>
      <ElTableColumn label="Passages" width="84" align="right">
        <template #default="{ row }">{{ row.chunks ?? '' }}</template>
      </ElTableColumn>
      <ElTableColumn :width="showSource ? 124 : 96" align="right">
        <template #default="{ row }">
          <div class="cell-actions">
            <a
              v-if="isDrive(row) && safeWebLink(row.web_link)"
              :href="safeWebLink(row.web_link) ?? undefined"
              target="_blank"
              rel="noopener noreferrer"
              class="open-drive"
              title="Open in Google Drive"
              aria-label="Open in Google Drive"
            >
              <Icon icon="mdi:open-in-new" />
            </a>
            <ElButton
              size="small"
              text
              :disabled="busy || row.status === 'missing'"
              title="Read this file again and update its index"
              @click="reindexRow(row)"
            >
              Reindex
            </ElButton>
          </div>
        </template>
      </ElTableColumn>
    </ElTable>

    <div v-if="next && !note" class="more">
      <ElButton size="small" text :loading="loading" @click="load(false)">Load more</ElButton>
    </div>
  </div>
</template>

<style scoped>
.files { display: flex; flex-direction: column; gap: 10px; }
.files-bar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
.files-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.files-search { width: 220px; }
.files-source { width: 150px; }
.tabs { display: flex; flex-wrap: wrap; gap: 4px; }
.tab {
  display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 999px;
  border: 1px solid var(--border-color); background: transparent; cursor: pointer;
  font-size: 0.78rem; color: var(--text-secondary);
}
.tab:hover { color: var(--primary-color); border-color: var(--primary-color); }
.tab.active {
  color: var(--primary-color); border-color: var(--primary-color);
  background: color-mix(in srgb, var(--primary-color) 10%, transparent);
}
.tab-n { font-variant-numeric: tabular-nums; color: var(--text-tertiary); }
.tab.active .tab-n { color: inherit; }

.files-table { width: 100%; }
.cell-file { display: flex; flex-direction: column; min-width: 0; }
.cell-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); }
.cell-folder {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 0.75rem; color: var(--text-tertiary);
}
.cell-status { display: flex; align-items: center; gap: 6px; }
.cell-reason { font-size: 0.75rem; color: var(--text-secondary); line-height: 1.35; }
.status-dot {
  --tone: var(--text-tertiary);
  width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--tone);
}
.status-dot.st-indexed { --tone: var(--success-color); }
.status-dot.st-dirty { --tone: var(--primary-color); }
.status-dot.st-error { --tone: var(--danger-color); }
.status-dot.st-missing, .status-dot.st-deferred, .status-dot.st-awaiting_extractor { --tone: var(--warning-color); }
.cell-source { font-size: 16px; color: var(--text-secondary); vertical-align: middle; }
.cell-source.drive { color: var(--primary-color); }
.cell-actions { display: inline-flex; align-items: center; justify-content: flex-end; gap: 2px; }
.open-drive {
  display: inline-flex; padding: 4px; border-radius: 4px; line-height: 1;
  color: var(--text-secondary); font-size: 15px;
}
.open-drive:hover { color: var(--primary-color); background: var(--hover-bg); }
.note { margin: 0; font-size: 0.85rem; color: var(--text-secondary); }
.more { display: flex; justify-content: center; }
</style>
