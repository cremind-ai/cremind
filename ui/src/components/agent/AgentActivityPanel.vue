<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount, nextTick } from 'vue';
import { Icon } from '@iconify/vue';
import type { AgentActivityState, AgentActivityStep } from '../../stores/chat';

const props = defineProps<{ state: AgentActivityState }>();
const emit = defineEmits<{ (e: 'dismiss'): void }>();

const MAX_KEY = 'agent_activity_panel_maximized';
// `maximized` is a deliberate size preference, so it persists across reloads.
const maximized = ref(localStorage.getItem(MAX_KEY) === '1');
// `minimized` is session-only: the flag is global but activity is
// per-conversation, so persisting it would pre-hide unrelated agent runs.
const minimized = ref(false);

function minimize() {
  minimized.value = true;
}
function restore() {
  minimized.value = false;
}
function toggleMaximized() {
  maximized.value = !maximized.value;
  try {
    localStorage.setItem(MAX_KEY, maximized.value ? '1' : '0');
  } catch {
    /* noop */
  }
}

const TERMINAL = new Set(['completed', 'done', 'failed', 'error', 'cancelled', 'interrupted']);
const isRunning = computed(() => !TERMINAL.has(props.state.status));
const isFailed = computed(() =>
  ['failed', 'error', 'interrupted', 'cancelled'].includes(props.state.status),
);

// Agent display name + icon (branch by discriminator so Codex etc. slot in later).
const agentLabel = computed(() => {
  if (props.state.agent === 'claude_code') return 'Claude Code';
  if (props.state.agent === 'codex') return 'Codex';
  return props.state.agent || 'Agent';
});
const agentIcon = computed(() => 'mdi:robot-outline');

function headerStatusIcon(): string {
  if (isRunning.value) return 'mdi:loading';
  if (props.state.status === 'completed' || props.state.status === 'done') return 'mdi:check-circle';
  return 'mdi:alert-circle';
}

function stepIcon(step: AgentActivityStep): string {
  if (step.kind === 'thinking') return 'mdi:thought-bubble-outline';
  if (step.kind === 'text') return 'mdi:message-text-outline';
  if (step.kind === 'result') return 'mdi:flag-checkered';
  // tool_use
  if (step.status === 'error') return 'mdi:close-circle-outline';
  if (step.status === 'ok') return 'mdi:check-circle-outline';
  return 'mdi:wrench-outline';
}

// Elapsed time ticker (only while running).
const nowMs = ref(Date.now());
let timer: ReturnType<typeof setInterval> | null = null;
watch(
  isRunning,
  running => {
    if (running && !timer) {
      timer = setInterval(() => {
        nowMs.value = Date.now();
      }, 1000);
    } else if (!running && timer) {
      clearInterval(timer);
      timer = null;
    }
  },
  { immediate: true },
);

const elapsedLabel = computed(() => {
  const startMs = (props.state.started_at || 0) * 1000;
  const endMs = isRunning.value
    ? nowMs.value
    : (props.state.updated_at || props.state.started_at || 0) * 1000;
  const secs = Math.max(0, Math.round((endMs - startMs) / 1000));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
});

const costLabel = computed(() => {
  const c = props.state.stats?.total_cost_usd;
  return typeof c === 'number' ? `$${c.toFixed(4)}` : null;
});
const turnsLabel = computed(() => {
  const n = props.state.stats?.num_turns;
  return typeof n === 'number' ? `${n} turn${n === 1 ? '' : 's'}` : null;
});

// Expand/collapse per-step detail.
const expanded = ref<Set<string>>(new Set());
function toggleDetail(step: AgentActivityStep) {
  if (!step.detail) return;
  const next = new Set(expanded.value);
  if (next.has(step.id)) next.delete(step.id);
  else next.add(step.id);
  expanded.value = next;
}

// Highlight-then-fade on each update; auto-scroll to newest unless the user
// scrolled up.
const bright = ref(false);
const flashIds = ref<Set<string>>(new Set());
let fadeTimer: ReturnType<typeof setTimeout> | null = null;
const listRef = ref<HTMLElement | null>(null);

function nearBottom(): boolean {
  const el = listRef.value;
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight < 48;
}

watch(
  () => props.state.updateSeq,
  () => {
    bright.value = true;
    flashIds.value = new Set(props.state.changedIds);
    if (fadeTimer) clearTimeout(fadeTimer);
    fadeTimer = setTimeout(() => {
      bright.value = false;
      flashIds.value = new Set();
    }, 2500);
    const wasNearBottom = nearBottom();
    nextTick(() => {
      if (wasNearBottom && listRef.value) {
        listRef.value.scrollTop = listRef.value.scrollHeight;
      }
    });
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  if (fadeTimer) clearTimeout(fadeTimer);
  if (timer) clearInterval(timer);
});
</script>

<template>
  <button
    v-if="minimized"
    class="aa-pill"
    :class="{ bright }"
    title="Show agent activity"
    aria-label="Show agent activity"
    @click="restore"
  >
    <Icon :icon="agentIcon" class="aa-header-icon" />
    <span class="aa-title">{{ agentLabel }}</span>
    <Icon v-if="isRunning" icon="mdi:loading" class="aa-spin" />
    <span v-else class="aa-count">{{ state.total_steps }}</span>
  </button>

  <div v-else class="aa-panel" :class="{ bright, maximized }">
    <div class="aa-header">
      <Icon :icon="agentIcon" class="aa-header-icon" />
      <span class="aa-title" :title="state.title">{{ agentLabel }}</span>
      <Icon
        :icon="headerStatusIcon()"
        class="aa-status-icon"
        :class="{ spin: isRunning, ok: state.status === 'completed' || state.status === 'done', bad: isFailed }"
      />
      <span class="aa-elapsed">{{ elapsedLabel }}</span>
      <span class="aa-count">{{ state.total_steps }}</span>
      <button
        class="aa-action"
        :title="maximized ? 'Restore size' : 'Maximize'"
        :aria-label="maximized ? 'Restore size' : 'Maximize'"
        @click="toggleMaximized"
      >
        <Icon :icon="maximized ? 'mdi:arrow-collapse' : 'mdi:arrow-expand'" />
      </button>
      <button class="aa-action" title="Minimize" aria-label="Minimize" @click="minimize">
        <Icon icon="mdi:window-minimize" />
      </button>
      <button
        v-if="!isRunning"
        class="aa-action"
        title="Close"
        aria-label="Close"
        @click="emit('dismiss')"
      >
        <Icon icon="mdi:close" />
      </button>
    </div>

    <p v-if="state.title" class="aa-task" :title="state.title">{{ state.title }}</p>

    <ul ref="listRef" class="aa-list">
      <li
        v-for="step in state.steps"
        :key="step.id"
        class="aa-item"
        :class="[step.kind, step.status ? `status-${step.status}` : '', { flash: flashIds.has(step.id), clickable: !!step.detail }]"
        @click="toggleDetail(step)"
      >
        <Icon :icon="stepIcon(step)" class="aa-item-icon" :class="{ spin: step.status === 'running' }" />
        <div class="aa-item-body">
          <span class="aa-item-label">{{ step.label }}</span>
          <pre v-if="step.detail && expanded.has(step.id)" class="aa-item-detail">{{ step.detail }}</pre>
        </div>
      </li>
    </ul>

    <div v-if="!isRunning" class="aa-footer">
      <span v-if="turnsLabel">{{ turnsLabel }}</span>
      <span v-if="turnsLabel && costLabel" class="aa-dot">·</span>
      <span v-if="costLabel">{{ costLabel }}</span>
      <span v-if="isFailed && state.error" class="aa-error" :title="state.error">{{ state.error }}</span>
    </div>
  </div>
</template>

<style scoped>
.aa-panel {
  width: 300px;
  max-width: calc(100% - 32px);
  max-height: 55%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 10px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  opacity: 0.6;
  transition: opacity 0.4s ease, box-shadow 0.4s ease, width 0.25s ease;
}

.aa-panel.bright {
  opacity: 1;
  box-shadow: 0 6px 20px rgba(37, 99, 235, 0.18);
}

.aa-panel:hover {
  opacity: 1;
}

.aa-panel.maximized {
  width: 440px;
  max-height: 70vh;
  opacity: 1;
}

.aa-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  user-select: none;
  flex-shrink: 0;
  border-bottom: 1px solid var(--border-color);
}

.aa-header-icon {
  font-size: 16px;
  color: var(--primary-color);
  flex-shrink: 0;
}

.aa-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
}

.aa-status-icon {
  font-size: 15px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}
.aa-status-icon.ok {
  color: var(--success-color);
}
.aa-status-icon.bad {
  color: var(--danger-color, #e5484d);
}

.aa-elapsed {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-left: auto;
  font-variant-numeric: tabular-nums;
}

.aa-count {
  font-size: 12px;
  color: var(--text-tertiary);
  min-width: 16px;
  text-align: right;
}

.aa-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 2px 4px;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  border-radius: 6px;
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  transition: background 0.15s ease, color 0.15s ease;
}

.aa-action:hover {
  background: var(--hover-bg);
  color: var(--text-primary);
}

.aa-task {
  margin: 0;
  padding: 8px 12px 0;
  font-size: 12px;
  color: var(--text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  flex-shrink: 0;
}

.aa-list {
  list-style: none;
  margin: 0;
  padding: 6px;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

.aa-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--text-primary);
  transition: background 1.2s ease;
}

.aa-item.clickable {
  cursor: pointer;
}

.aa-item.flash {
  background: rgba(37, 99, 235, 0.1);
}

.aa-item-icon {
  flex-shrink: 0;
  font-size: 16px;
  margin-top: 1px;
  color: var(--text-tertiary);
}

.aa-item.tool_use .aa-item-icon {
  color: var(--primary-color);
}
.aa-item.status-ok .aa-item-icon {
  color: var(--success-color);
}
.aa-item.status-error .aa-item-icon {
  color: var(--danger-color, #e5484d);
}
.aa-item.result .aa-item-icon {
  color: var(--success-color);
}

.aa-item-body {
  min-width: 0;
  flex: 1 1 auto;
}

.aa-item-label {
  line-height: 1.4;
  word-break: break-word;
  display: block;
}

.aa-item-detail {
  margin: 4px 0 0;
  padding: 6px 8px;
  background: var(--code-bg, rgba(127, 127, 127, 0.12));
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 180px;
  overflow: auto;
  color: var(--text-secondary);
}

.aa-footer {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  border-top: 1px solid var(--border-color);
  font-size: 12px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}

.aa-dot {
  opacity: 0.6;
}

.aa-error {
  color: var(--danger-color, #e5484d);
  margin-left: auto;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 60%;
}

/* Minimized pill. */
.aa-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 999px;
  color: var(--text-primary);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  cursor: pointer;
  opacity: 0.6;
  transition: opacity 0.4s ease;
}

.aa-pill:hover,
.aa-pill.bright {
  opacity: 1;
}

.spin {
  animation: aa-spin 1s linear infinite;
}
.aa-spin {
  animation: aa-spin 1s linear infinite;
}

@keyframes aa-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
