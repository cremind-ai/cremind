<script setup lang="ts">
/**
 * Live sync progress — the "never silent" half of the feature.
 *
 * Everything above the activity feed comes from the snapshot and so updates
 * with every frame: the overall bar ("3,120/12,840 files · ~12 min left"),
 * files by status, the files being worked on right now and at what stage, the
 * queue, and the latest failures with their reason and a Retry. The feed below
 * is the persisted activity log (`GET /activity`), newest first with keyset
 * "Load more"; it re-reads its first page when the snapshot reports new work,
 * throttled, rather than on every frame.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElButton, ElProgress } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../../stores/settings';
import { isActive, syncProgress } from '../../stores/documentsReducer';
import {
  listDocumentsActivity,
  DocumentsApiError,
  type DocumentsActivityEvent,
  type DocumentsSnapshot,
} from '../../services/documentsApi';
import {
  STATUS_ORDER,
  formatCount,
  formatEta,
  reasonLabel,
  stageLabel,
  statusLabel,
} from '../../utils/documentsView';
import { formatRelativeTime } from '../../utils/relativeTime';

const props = withDefaults(defineProps<{
  snapshot: DocumentsSnapshot | null;
  /** Retry/Reindex in flight. */
  busy?: boolean;
}>(), { busy: false });

const emit = defineEmits<{
  retry: [targets: string[]];
  'retry-all': [];
  'show-failed': [];
  'engine-down': [];
}>();

const settingsStore = useSettingsStore();

const active = computed(() => isActive(props.snapshot));
const progress = computed(() => syncProgress(props.snapshot));

const progressLine = computed(() => {
  const p = progress.value;
  const snap = props.snapshot;
  if (!snap) return '';
  if (snap.state === 'estimating' || snap.state === 'scanning') {
    return snap.phase ? snap.phase[0].toUpperCase() + snap.phase.slice(1) : 'Looking through your folder…';
  }
  if (p.total <= 0) return active.value ? 'Starting…' : 'Nothing indexed yet.';
  const eta = formatEta(p.etaS);
  const unit = p.unit === 'chunks' ? 'passages' : 'files';
  return `${formatCount(p.done)}/${formatCount(p.total)} ${unit}${eta ? ` · ${eta}` : ''}`;
});

const stagePills = computed(() => {
  const stages = props.snapshot?.stages ?? {};
  const known = STATUS_ORDER.filter(s => (stages[s] ?? 0) > 0);
  const extra = Object.keys(stages).filter(s => !STATUS_ORDER.includes(s) && s !== 'tombstone' && stages[s] > 0);
  return [...known, ...extra].map(status => ({ status, label: statusLabel(status), n: stages[status] }));
});

const current = computed(() => props.snapshot?.current ?? []);
const queue = computed(() => props.snapshot?.queue_preview ?? []);
const failed = computed(() => props.snapshot?.failed_preview ?? []);
const failedTotal = computed(() => progress.value.failed);

// ── activity feed ─────────────────────────────────────────────────────────

const PAGE = 30;
const events = ref<DocumentsActivityEvent[]>([]);
const loadingEvents = ref(false);
const moreAvailable = ref(false);
const feedNote = ref('');
const now = ref(Date.now());
let nowTimer: ReturnType<typeof setInterval> | null = null;

function handleError(e: unknown) {
  if (e instanceof DocumentsApiError && e.code === 'EngineNotRunning') {
    emit('engine-down');
    feedNote.value = '';
    return;
  }
  if (e instanceof DocumentsApiError && e.code === 'NotEnabled') {
    feedNote.value = 'Nothing has been indexed yet.';
    return;
  }
  feedNote.value = e instanceof Error ? e.message : 'Could not load the activity.';
}

async function loadFirstPage() {
  loadingEvents.value = true;
  try {
    const res = await listDocumentsActivity(settingsStore.agentUrl, settingsStore.authToken, { limit: PAGE });
    events.value = res.events;
    moreAvailable.value = res.events.length === PAGE;
    feedNote.value = '';
  } catch (e) {
    handleError(e);
  } finally {
    loadingEvents.value = false;
  }
}

async function loadMore() {
  const last = events.value[events.value.length - 1];
  if (!last || loadingEvents.value) return;
  loadingEvents.value = true;
  try {
    const res = await listDocumentsActivity(settingsStore.agentUrl, settingsStore.authToken, {
      before: last.id, limit: PAGE,
    });
    const seen = new Set(events.value.map(e => e.id));
    events.value = [...events.value, ...res.events.filter(e => !seen.has(e.id))];
    moreAvailable.value = res.events.length === PAGE;
  } catch (e) {
    handleError(e);
  } finally {
    loadingEvents.value = false;
  }
}

// New work shows up as a new head of the snapshot's in-memory "recent" list.
// Re-read the log's first page then — at most every few seconds, since a first
// sync can finish dozens of files a second. Older pages already loaded stay.
const REFRESH_MS = 4000;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;
let lastRefresh = 0;
watch(() => props.snapshot?.recent?.[0]?.ts, (ts, prev) => {
  if (!ts || ts === prev || refreshTimer !== null) return;
  const wait = Math.max(0, lastRefresh + REFRESH_MS - Date.now());
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    lastRefresh = Date.now();
    try {
      const res = await listDocumentsActivity(settingsStore.agentUrl, settingsStore.authToken, { limit: PAGE });
      const fresh = res.events.filter(e => !events.value.some(x => x.id === e.id));
      if (fresh.length) events.value = [...fresh, ...events.value].sort((a, b) => b.id - a.id);
      feedNote.value = '';
    } catch (e) {
      handleError(e);
    }
  }, wait);
});

onMounted(() => {
  void loadFirstPage();
  // Keeps "2m ago" honest without a re-render per frame.
  nowTimer = setInterval(() => { now.value = Date.now(); }, 30_000);
});
onBeforeUnmount(() => {
  if (refreshTimer !== null) clearTimeout(refreshTimer);
  if (nowTimer !== null) clearInterval(nowTimer);
});

function eventIcon(e: DocumentsActivityEvent): string {
  if (e.level === 'error') return 'mdi:alert-circle-outline';
  if (e.level === 'warning') return 'mdi:alert-outline';
  switch (e.kind) {
    case 'added': return 'mdi:file-plus-outline';
    case 'updated': return 'mdi:file-document-edit-outline';
    case 'moved': return 'mdi:file-move-outline';
    case 'removed': return 'mdi:file-remove-outline';
    case 'metadata_only': return 'mdi:file-eye-outline';
    default: return 'mdi:information-outline';
  }
}

function retryTarget(item: { fid: string | null; rel_path: string }): string {
  return item.fid || item.rel_path;
}

defineExpose({ reload: loadFirstPage });
</script>

<template>
  <div class="activity">
    <!-- Overall progress -->
    <div class="overall">
      <ElProgress
        :percentage="progress.pct != null ? Math.floor(progress.pct) : 0"
        :indeterminate="active && progress.pct == null"
        :show-text="false"
        :stroke-width="8"
        :duration="2"
      />
      <div class="overall-line">
        <span class="overall-text">
          <Icon v-if="active" icon="mdi:sync" class="spin" />
          {{ progressLine }}
        </span>
        <span v-if="progress.pct != null" class="overall-pct">{{ Math.floor(progress.pct) }}%</span>
      </div>
    </div>

    <!-- Files by status -->
    <div v-if="stagePills.length" class="pills">
      <span
        v-for="pill in stagePills"
        :key="pill.status"
        class="pill"
        :class="`st-${pill.status}`"
      >
        {{ pill.label }} <strong>{{ formatCount(pill.n) }}</strong>
      </span>
    </div>

    <!-- Working on now -->
    <section v-if="current.length" class="block">
      <h4>Working on</h4>
      <ul class="rows">
        <li v-for="c in current" :key="c.rel_path" class="row">
          <Icon icon="mdi:file-document-outline" class="row-icon" />
          <span class="row-name" :title="c.rel_path">{{ c.name }}</span>
          <span class="row-meta">{{ stageLabel(c.stage, c.progress) }}</span>
        </li>
      </ul>
    </section>

    <!-- Up next -->
    <section v-if="queue.length" class="block">
      <h4>Up next</h4>
      <ul class="rows compact">
        <li v-for="q in queue" :key="q" class="row">
          <span class="row-path" :title="q">{{ q }}</span>
        </li>
      </ul>
    </section>

    <!-- Failures -->
    <section v-if="failedTotal > 0 || failed.length" class="block">
      <div class="block-head">
        <h4>Could not be indexed<span v-if="failedTotal"> ({{ formatCount(failedTotal) }})</span></h4>
        <div class="block-actions">
          <ElButton size="small" text :disabled="busy" @click="emit('show-failed')">Show all</ElButton>
          <ElButton size="small" :disabled="busy" @click="emit('retry-all')">Retry all</ElButton>
        </div>
      </div>
      <ul v-if="failed.length" class="rows">
        <li v-for="f in failed" :key="retryTarget(f)" class="row failed">
          <Icon icon="mdi:file-alert-outline" class="row-icon" />
          <div class="row-main">
            <span class="row-name" :title="f.rel_path">{{ f.name }}</span>
            <span class="row-reason">{{ reasonLabel(f.reason) || f.message }}</span>
          </div>
          <ElButton size="small" :disabled="busy" @click="emit('retry', [retryTarget(f)])">Retry</ElButton>
        </li>
      </ul>
    </section>

    <!-- Activity feed -->
    <section class="block">
      <h4>Recent activity</h4>
      <p v-if="feedNote" class="note">{{ feedNote }}</p>
      <p v-else-if="!events.length && !loadingEvents" class="note">No activity yet.</p>
      <ul v-if="events.length" class="rows feed">
        <li v-for="e in events" :key="e.id" class="row" :class="`lv-${e.level}`">
          <Icon :icon="eventIcon(e)" class="row-icon" />
          <span class="row-msg">{{ e.message }}</span>
          <span class="row-time">{{ e.ts ? formatRelativeTime(e.ts, now) : '' }}</span>
        </li>
      </ul>
      <div v-if="moreAvailable" class="more">
        <ElButton size="small" text :loading="loadingEvents" @click="loadMore">Load more</ElButton>
      </div>
    </section>
  </div>
</template>

<style scoped>
.activity { display: flex; flex-direction: column; gap: 14px; }
.overall-line {
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
  margin-top: 6px; font-size: 0.85rem; color: var(--text-primary);
}
.overall-text { display: inline-flex; align-items: center; gap: 6px; }
.overall-pct { color: var(--text-secondary); font-variant-numeric: tabular-nums; }

.pills { display: flex; flex-wrap: wrap; gap: 6px; }
.pill {
  --tone: var(--text-secondary);
  display: inline-flex; align-items: center; gap: 5px; padding: 2px 10px; border-radius: 999px;
  font-size: 0.75rem; color: var(--text-primary);
  background: color-mix(in srgb, var(--tone) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--tone) 30%, transparent);
}
.pill strong { font-variant-numeric: tabular-nums; }
.pill.st-indexed { --tone: var(--success-color); }
.pill.st-dirty { --tone: var(--primary-color); }
.pill.st-error { --tone: var(--danger-color); }
.pill.st-missing, .pill.st-deferred, .pill.st-awaiting_extractor { --tone: var(--warning-color); }

.block h4 {
  margin: 0 0 6px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--text-tertiary);
}
.block-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.block-head h4 { margin: 0; }
.block-actions { display: flex; gap: 6px; }
.block-actions :deep(.el-button + .el-button) { margin-left: 0; }
.rows { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.row {
  display: flex; align-items: center; gap: 8px; padding: 5px 8px; border-radius: 6px;
  font-size: 0.83rem; color: var(--text-primary);
}
.row:hover { background: var(--hover-bg); }
.rows.compact .row { padding: 2px 8px; }
.row-icon { flex: none; color: var(--text-secondary); }
.row.failed .row-icon { color: var(--danger-color); }
.row-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.row-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.row > .row-name { flex: 1; }
.row-meta, .row-reason { color: var(--text-secondary); font-size: 0.78rem; }
.row-meta { flex: none; }
.row-reason { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-path {
  font-family: var(--font-mono, monospace); font-size: 0.78rem; color: var(--text-secondary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.feed .row { align-items: flex-start; }
.feed .row-icon { margin-top: 3px; }
.row-msg { flex: 1; min-width: 0; line-height: 1.45; word-break: break-word; }
.row-time { flex: none; color: var(--text-tertiary); font-size: 0.75rem; }
.row.lv-error .row-icon { color: var(--danger-color); }
.row.lv-warning .row-icon { color: var(--warning-color); }
.note { margin: 4px 0 0; font-size: 0.82rem; color: var(--text-secondary); }
.more { display: flex; justify-content: center; margin-top: 4px; }

.spin { animation: spin 1.1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
