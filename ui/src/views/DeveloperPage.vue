<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onBeforeUpdate, onMounted, ref, watch } from 'vue';
import type { ComponentPublicInstance } from 'vue';
import { useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import { copyTextToClipboard } from '../utils/clipboard';
import { ElButton, ElCard, ElCheckTag, ElEmpty, ElInput, ElMessage, ElMessageBox, ElSwitch, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../stores/settings';
import { openServerLogsStream, type LogEntry, type ServerLogsStreamHandle } from '../services/serverLogsStream';
import { useServerRestart } from '../composables/useServerRestart';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const;
type LogLevel = typeof LEVELS[number];

const CLIENT_BUFFER_CAP = 2000;
const AUTO_SCROLL_THRESHOLD_PX = 30;

const entries = ref<LogEntry[]>([]);
// Plain reactive record — one ref per chip. Avoids reactive-Set quirks
// where `:checked="set.has(lvl)"` could miss the re-render on replace.
const activeLevels = ref<Record<LogLevel, boolean>>({
  DEBUG: true,
  INFO: true,
  WARNING: true,
  ERROR: true,
  CRITICAL: true,
});
const searchText = ref('');
const paused = ref(false);
const autoScroll = ref(true);
const copied = ref(false);
let copiedTimer: ReturnType<typeof setTimeout> | null = null;
const streamReady = ref(false);
const streamError = ref<string | null>(null);

const currentMatchIndex = ref(0);
const matchEls = ref<HTMLElement[]>([]);

const viewportRef = ref<HTMLElement | null>(null);
let streamHandle: ServerLogsStreamHandle | null = null;

const {
  phase: restartPhase,
  error: restartError,
  installMode,
  isBusy: restartBusy,
  loadInstallMode,
  restart: triggerRestart,
} = useServerRestart();

const restartBadge = computed<{ label: string; type: 'info' | 'success' | 'warning' | 'danger' }>(() => {
  switch (restartPhase.value) {
    case 'restarting':
      return { label: 'Restarting…', type: 'warning' };
    case 'reconnected':
      return { label: 'Reconnected', type: 'success' };
    case 'failed':
      return { label: 'Failed', type: 'danger' };
    case 'idle':
    default:
      return { label: 'Idle', type: 'info' };
  }
});

// Confirmation copy is install-mode-specific so the user sees what
// will actually happen on their environment. The unsupervised case
// is loudest — that's the one where the backend will stay down.
const restartConfirmCopy = computed(() => {
  switch (installMode.value) {
    case 'docker':
      return {
        message:
          'Active connections will drop. The container will restart automatically ' +
          '— this usually takes 5–15 seconds.',
        type: 'warning' as const,
      };
    case 'electron':
      return {
        message:
          'Active connections will drop. Cremind will relaunch its backend automatically.',
        type: 'warning' as const,
      };
    default:
      // ``native`` and ``null`` (unknown) — assume no supervisor.
      return {
        message:
          'Warning: this install has no supervisor. The backend will exit and ' +
          'you will need to relaunch ``cremind serve`` manually. Active connections ' +
          'will drop.',
        type: 'error' as const,
      };
  }
});

async function handleRestartClick() {
  try {
    await ElMessageBox.confirm(
      restartConfirmCopy.value.message,
      'Restart Cremind Server',
      {
        type: restartConfirmCopy.value.type,
        confirmButtonText: 'Restart now',
        cancelButtonText: 'Cancel',
        confirmButtonClass: 'el-button--danger',
      },
    );
  } catch {
    return;
  }
  await triggerRestart();
}

function handleLevelChange(level: LogLevel, value: boolean) {
  activeLevels.value[level] = value;
}

const needle = computed(() => searchText.value.trim().toLowerCase());

const visibleEntries = computed(() => {
  const n = needle.value;
  return entries.value.filter((e) => {
    if (!activeLevels.value[e.level as LogLevel]) return false;
    if (n && !e.message.toLowerCase().includes(n) && !e.source.toLowerCase().includes(n)) {
      return false;
    }
    return true;
  });
});

interface Segment {
  text: string;
  isMatch: boolean;
  globalIndex: number;
}

interface RenderedEntry {
  entry: LogEntry;
  sourceSegments: Segment[];
  messageSegments: Segment[];
}

function splitForHighlight(
  text: string,
  needleStr: string,
  startIndex: number,
): { segments: Segment[]; nextIndex: number } {
  if (!needleStr) {
    return { segments: [{ text, isMatch: false, globalIndex: -1 }], nextIndex: startIndex };
  }
  const segments: Segment[] = [];
  const lower = text.toLowerCase();
  let pos = 0;
  let running = startIndex;
  while (true) {
    const found = lower.indexOf(needleStr, pos);
    if (found === -1) break;
    if (found > pos) segments.push({ text: text.slice(pos, found), isMatch: false, globalIndex: -1 });
    segments.push({
      text: text.slice(found, found + needleStr.length),
      isMatch: true,
      globalIndex: running,
    });
    running += 1;
    pos = found + needleStr.length;
  }
  if (pos < text.length) segments.push({ text: text.slice(pos), isMatch: false, globalIndex: -1 });
  return { segments, nextIndex: running };
}

const renderState = computed(() => {
  const n = needle.value;
  const rendered: RenderedEntry[] = [];
  let running = 0;
  for (const entry of visibleEntries.value) {
    const src = splitForHighlight(entry.source, n, running);
    running = src.nextIndex;
    const msg = splitForHighlight(entry.message, n, running);
    running = msg.nextIndex;
    rendered.push({ entry, sourceSegments: src.segments, messageSegments: msg.segments });
  }
  return { rendered, total: running };
});

const renderedEntries = computed(() => renderState.value.rendered);
const totalMatches = computed(() => renderState.value.total);

function registerMatchEl(el: Element | ComponentPublicInstance | null, index: number) {
  if (el instanceof HTMLElement) {
    matchEls.value[index] = el;
  }
}

function scrollCurrentMatchIntoView() {
  nextTick(() => {
    const el = matchEls.value[currentMatchIndex.value];
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
}

function gotoMatch(delta: 1 | -1) {
  const n = totalMatches.value;
  if (n === 0) return;
  currentMatchIndex.value = (currentMatchIndex.value + delta + n) % n;
  scrollCurrentMatchIntoView();
}

watch(searchText, () => {
  currentMatchIndex.value = 0;
});

watch(totalMatches, (n) => {
  if (n === 0) {
    currentMatchIndex.value = 0;
  } else if (currentMatchIndex.value >= n) {
    currentMatchIndex.value = n - 1;
  }
});

onBeforeUpdate(() => {
  matchEls.value = [];
});

function pushEntry(entry: LogEntry) {
  if (paused.value) return;
  const wasNearBottom = isViewportNearBottom();
  entries.value.push(entry);
  if (entries.value.length > CLIENT_BUFFER_CAP) {
    entries.value.splice(0, entries.value.length - CLIENT_BUFFER_CAP);
  }
  if (autoScroll.value && wasNearBottom) {
    nextTick(scrollViewportToBottom);
  }
}

function isViewportNearBottom(): boolean {
  const el = viewportRef.value;
  if (!el) return true;
  return el.scrollTop + el.clientHeight >= el.scrollHeight - AUTO_SCROLL_THRESHOLD_PX;
}

function scrollViewportToBottom() {
  const el = viewportRef.value;
  if (!el) return;
  el.scrollTop = el.scrollHeight;
}

function handleClear() {
  entries.value = [];
}

// Shared by Save and Copy so both emit identical text for the
// currently-displayed (filtered) entries.
function formatVisibleLogsText(): string {
  const lines = visibleEntries.value.map((e) => {
    const ts = e.ts || '';
    const level = (e.level || '').padEnd(8);
    return `[${ts}] ${level} ${e.source} - ${e.message}`;
  });
  return lines.join('\n') + '\n';
}

function handleSaveLog() {
  if (visibleEntries.value.length === 0) return;
  const content = formatVisibleLogsText();
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  const filename = `cremind-logs-${stamp}.log`;
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

async function handleCopyLog() {
  if (visibleEntries.value.length === 0) return;
  const ok = await copyTextToClipboard(formatVisibleLogsText());
  if (!ok) {
    ElMessage.error('Failed to copy logs');
    return;
  }
  copied.value = true;
  if (copiedTimer) clearTimeout(copiedTimer);
  copiedTimer = setTimeout(() => { copied.value = false; }, 2000);
}

function handleTogglePause() {
  paused.value = !paused.value;
}

function handleScrollToBottom() {
  scrollViewportToBottom();
}

function goBack() {
  goBackToChat(router, props.profile);
}

function levelTagType(level: string): 'success' | 'info' | 'warning' | 'danger' | 'primary' {
  switch (level) {
    case 'DEBUG': return 'info';
    case 'INFO': return 'success';
    case 'WARNING': return 'warning';
    case 'ERROR':
    case 'CRITICAL': return 'danger';
    default: return 'info';
  }
}

function formatTimestamp(iso: string): string {
  if (!iso) return '';
  // Parse ISO into HH:mm:ss.SSS — Loguru emits ISO 8601 with microseconds.
  const m = iso.match(/T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?/);
  if (!m) return iso;
  const millis = (m[4] ?? '').padEnd(3, '0').slice(0, 3);
  return `${m[1]}:${m[2]}:${m[3]}.${millis}`;
}

onMounted(() => {
  void loadInstallMode();
  const agentUrl = settingsStore.agentUrl;
  const token = settingsStore.authToken;
  if (!agentUrl || !token) {
    streamError.value = 'Not authenticated';
    return;
  }
  streamHandle = openServerLogsStream(
    agentUrl,
    token,
    (entry) => pushEntry(entry),
    () => {
      streamReady.value = true;
      nextTick(scrollViewportToBottom);
    },
    (err) => {
      streamError.value = err instanceof Error ? err.message : String(err);
    },
  );
});

onBeforeUnmount(() => {
  streamHandle?.close();
  streamHandle = null;
  if (copiedTimer) clearTimeout(copiedTimer);
});

// Snap to bottom whenever auto-scroll is re-enabled.
watch(autoScroll, (on) => {
  if (on) nextTick(scrollViewportToBottom);
});
</script>

<template>
  <div class="developer-page">
    <div class="developer-container">
    <button class="back-btn" @click="goBack">
      <Icon icon="mdi:arrow-left" />
      Back to Chat
    </button>

    <div class="developer-header">
      <h1>Developer</h1>
      <p>Debugging and development tools.</p>
    </div>

    <ElCard class="restart-card" shadow="never">
      <template #header>
        <div class="restart-card-header">
          <div class="restart-card-title">
            <Icon icon="mdi:restart" class="restart-card-icon" />
            <span>Restart Server</span>
            <ElTag
              :type="restartBadge.type"
              size="small"
              effect="plain"
            >{{ restartBadge.label }}</ElTag>
          </div>
          <ElButton
            type="danger"
            size="small"
            :loading="restartBusy"
            :disabled="restartBusy"
            @click="handleRestartClick"
          >
            <Icon icon="mdi:restart" />
            Restart Server
          </ElButton>
        </div>
      </template>

      <div class="restart-card-body">
        <p class="restart-description">
          Stops and respawns the Cremind backend process. Active HTTP, SSE, and chat
          connections will drop while the server is unavailable.
        </p>
        <div v-if="installMode === 'docker'" class="restart-hint">
          <Icon icon="mdi:docker" />
          <span>Docker install — the container will restart automatically.</span>
        </div>
        <div v-else-if="installMode === 'electron'" class="restart-hint">
          <Icon icon="mdi:application-brackets-outline" />
          <span>Electron install — Cremind will relaunch the backend automatically.</span>
        </div>
        <div v-else class="restart-hint restart-hint-warning">
          <Icon icon="mdi:alert-outline" />
          <span>
            No supervisor detected — restarting will leave the backend down. You will
            need to relaunch <code>cremind serve</code> manually.
          </span>
        </div>
        <div v-if="restartPhase === 'failed' && restartError" class="restart-error">
          <Icon icon="mdi:close-circle-outline" />
          <span>{{ restartError }}</span>
        </div>
      </div>
    </ElCard>

    <ElCard class="logs-card" shadow="never">
      <template #header>
        <div class="logs-card-header">
          <div class="logs-card-title">
            <Icon icon="mdi:file-document-outline" class="logs-card-icon" />
            <span>Server Logs</span>
            <ElTag
              v-if="streamReady"
              type="success"
              size="small"
              effect="plain"
            >live</ElTag>
            <ElTag
              v-else-if="!streamError"
              type="info"
              size="small"
              effect="plain"
            >connecting…</ElTag>
            <ElTag
              v-else
              type="danger"
              size="small"
              effect="plain"
            >{{ streamError }}</ElTag>
          </div>
          <div class="logs-card-actions">
            <ElButton
              size="small"
              :type="paused ? 'primary' : 'default'"
              @click="handleTogglePause"
            >
              <Icon :icon="paused ? 'mdi:play' : 'mdi:pause'" />
              {{ paused ? 'Resume' : 'Pause' }}
            </ElButton>
            <ElButton size="small" @click="handleClear">
              <Icon icon="mdi:broom" />
              Clear
            </ElButton>
            <ElButton
              size="small"
              :disabled="visibleEntries.length === 0"
              @click="handleSaveLog"
            >
              <Icon icon="mdi:download" />
              Save
            </ElButton>
            <ElButton
              size="small"
              :type="copied ? 'success' : 'default'"
              :disabled="visibleEntries.length === 0"
              @click="handleCopyLog"
            >
              <Icon :icon="copied ? 'mdi:check' : 'mdi:content-copy'" />
              {{ copied ? 'Copied!' : 'Copy' }}
            </ElButton>
            <ElButton size="small" @click="handleScrollToBottom">
              <Icon icon="mdi:chevron-down" />
              Tail
            </ElButton>
            <div class="autoscroll-toggle">
              <span>Auto-scroll</span>
              <ElSwitch v-model="autoScroll" size="small" />
            </div>
          </div>
        </div>
      </template>

      <div class="logs-toolbar">
        <div class="level-chips">
          <ElCheckTag
            v-for="lvl in LEVELS"
            :key="lvl"
            :checked="activeLevels[lvl]"
            :class="['level-chip', `level-chip-${lvl.toLowerCase()}`]"
            @change="handleLevelChange(lvl, $event)"
          >{{ lvl }}</ElCheckTag>
        </div>
        <ElInput
          v-model="searchText"
          placeholder="Filter by text…"
          size="small"
          class="logs-search"
          clearable
          @keydown.enter.exact.prevent="gotoMatch(1)"
          @keydown.enter.shift.prevent="gotoMatch(-1)"
        >
          <template #prefix><Icon icon="mdi:magnify" /></template>
        </ElInput>
        <div v-if="needle" class="logs-match-nav">
          <span class="logs-match-count">
            {{ totalMatches > 0 ? `${currentMatchIndex + 1} / ${totalMatches}` : '0 / 0' }}
          </span>
          <ElButton
            size="small"
            :disabled="totalMatches === 0"
            title="Previous match (Shift+Enter)"
            @click="gotoMatch(-1)"
          ><Icon icon="mdi:chevron-up" /></ElButton>
          <ElButton
            size="small"
            :disabled="totalMatches === 0"
            title="Next match (Enter)"
            @click="gotoMatch(1)"
          ><Icon icon="mdi:chevron-down" /></ElButton>
        </div>
      </div>

      <div
        ref="viewportRef"
        class="logs-viewport"
        :class="{ 'logs-viewport-paused': paused }"
      >
        <div
          v-for="(re, idx) in renderedEntries"
          :key="idx"
          :class="['log-row', `log-row-${re.entry.level.toLowerCase()}`]"
        >
          <span class="log-time">{{ formatTimestamp(re.entry.ts) }}</span>
          <ElTag
            :type="levelTagType(re.entry.level)"
            size="small"
            effect="plain"
            class="log-level"
          >{{ re.entry.level }}</ElTag>
          <span class="log-source">
            <template v-for="(seg, i) in re.sourceSegments" :key="`s${i}`">
              <mark
                v-if="seg.isMatch"
                :ref="(el) => registerMatchEl(el, seg.globalIndex)"
                :class="['logs-match', { 'is-current': seg.globalIndex === currentMatchIndex }]"
              >{{ seg.text }}</mark>
              <template v-else>{{ seg.text }}</template>
            </template>
          </span>
          <span class="log-message">
            <template v-for="(seg, i) in re.messageSegments" :key="`m${i}`">
              <mark
                v-if="seg.isMatch"
                :ref="(el) => registerMatchEl(el, seg.globalIndex)"
                :class="['logs-match', { 'is-current': seg.globalIndex === currentMatchIndex }]"
              >{{ seg.text }}</mark>
              <template v-else>{{ seg.text }}</template>
            </template>
          </span>
        </div>
        <ElEmpty
          v-if="renderedEntries.length === 0"
          :image-size="64"
          description="No log entries match the current filters."
        />
      </div>
      <div class="logs-footer">
        <span>{{ visibleEntries.length }} shown · {{ entries.length }} buffered (cap {{ CLIENT_BUFFER_CAP }})</span>
        <span v-if="paused" class="paused-hint">Paused — incoming entries are discarded.</span>
      </div>
    </ElCard>
    </div>
  </div>
</template>

<style scoped>
.developer-page {
  width: 100%;
  height: 100%;
  overflow-y: auto;
  background: var(--bg-color);
  padding: 24px;
  box-sizing: border-box;
}
.developer-container {
  max-width: 1200px;
  margin: 0 auto;
}

.back-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 0.875rem;
  padding: 4px 0;
  margin-bottom: 16px;
  transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }

.developer-header { margin-bottom: 20px; }
.developer-header h1 { font-size: 1.5rem; font-weight: 700; margin: 0 0 4px 0; color: var(--text-primary); }
.developer-header p { font-size: 0.875rem; color: var(--text-secondary); margin: 0; }

.logs-card { background: var(--card-bg, var(--bg-color)); }

.restart-card {
  background: var(--card-bg, var(--bg-color));
  margin-bottom: 16px;
}
.restart-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.restart-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--text-primary);
}
.restart-card-icon { font-size: 20px; color: var(--el-color-danger); }
.restart-card-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.restart-description {
  font-size: 0.875rem;
  color: var(--text-secondary);
  margin: 0;
}
.restart-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.85rem;
  color: var(--text-secondary);
}
.restart-hint code {
  font-family: var(--font-mono, ui-monospace, Consolas, monospace);
  background: var(--el-fill-color-light, rgba(127, 127, 127, 0.12));
  padding: 1px 4px;
  border-radius: 3px;
}
.restart-hint-warning { color: var(--el-color-warning); }
.restart-error {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.85rem;
  color: var(--el-color-danger);
}

.logs-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.logs-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--text-primary);
}
.logs-card-icon { font-size: 20px; color: var(--primary-color); }
.logs-card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.autoscroll-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  padding-left: 8px;
}

.logs-toolbar {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.level-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.level-chip {
  font-family: var(--font-mono, ui-monospace, Consolas, monospace);
  font-size: 0.78rem;
  letter-spacing: 0.04em;
  padding: 4px 12px;
  cursor: pointer;
  user-select: none;
  border: 1px solid transparent;
  opacity: 0.45;
  transition: opacity 0.15s, border-color 0.15s, background-color 0.15s;
}
.level-chip:hover { opacity: 0.85; }
.level-chip.is-checked {
  opacity: 1;
  border-color: currentColor;
}
.logs-search { width: 260px; }

.logs-match-nav {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 0.8rem;
  color: var(--text-secondary);
}
.logs-match-count {
  font-variant-numeric: tabular-nums;
  min-width: 48px;
  text-align: right;
}
:deep(mark.logs-match) {
  background: rgba(250, 204, 21, 0.35);
  color: inherit;
  padding: 0 1px;
  border-radius: 2px;
}
:deep(mark.logs-match.is-current) {
  background: #facc15;
  color: #1e1e1e;
}

.logs-viewport {
  background: #1e1e1e;
  color: #d4d4d4;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.55;
  padding: 12px 14px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  height: 60vh;
  min-height: 320px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  transition: opacity 0.15s;
}
.logs-viewport-paused { opacity: 0.7; }

.log-row {
  display: grid;
  grid-template-columns: 92px 78px minmax(160px, 240px) 1fr;
  gap: 10px;
  padding: 2px 0;
  align-items: baseline;
}
.log-row + .log-row { border-top: 1px dotted rgba(255, 255, 255, 0.05); }
.log-time { color: #9ca3af; }
.log-level { justify-self: start; min-width: 60px; text-align: center; }
.log-source { color: #94a3b8; word-break: break-all; }
.log-message { color: #e2e8f0; }

.log-row-debug .log-message { color: #94a3b8; }
.log-row-info .log-message { color: #d4d4d4; }
.log-row-warning .log-message { color: #facc15; }
.log-row-error .log-message { color: #f87171; }
.log-row-critical { background: rgba(248, 113, 113, 0.08); }
.log-row-critical .log-message { color: #fca5a5; font-weight: 600; }

.logs-footer {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  font-size: 0.8rem;
  color: var(--text-tertiary, var(--text-secondary));
}
.paused-hint { color: var(--el-color-warning); }
</style>
