<script setup lang="ts">
/**
 * The Research activity panel: a Documentation Search research job, live.
 *
 * Shows the question, the mode, the phase, a progress bar, the latest steps
 * and the tokens spent against the job's budget, with a Cancel button while
 * the job works. A job outlives the turn that started it, so the panel keeps
 * updating after the agent has answered "still running"; when the job stops
 * to ask something, the panel says so (the agent asks the question in the
 * chat).
 *
 * Visuals follow AgentActivityPanel (a floating card in the chat column).
 * Colours come from the app's tokens through color-mix — no ElAlert, typed
 * ElTag or typed ElButton, whose light-tint variables the dark theme never
 * redeclares.
 */
import { computed, ref, watch, onBeforeUnmount, nextTick } from 'vue';
import { ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';
import {
  RESEARCH_WORKING_STATUSES,
  type ResearchActivityState,
  type ResearchActivityStep,
} from '../../stores/chat';
import { useSettingsStore } from '../../stores/settings';
import { cancelResearchJob } from '../../services/conversationApi';
import { formatTokensCompact } from '../../utils/usageFormat';

const props = defineProps<{ state: ResearchActivityState }>();
const emit = defineEmits<{ (e: 'dismiss'): void }>();

// Session-only: the flag is global but a job is per conversation.
const minimized = ref(false);

const isWorking = computed(() => RESEARCH_WORKING_STATUSES.includes(props.state.status));
const isWaiting = computed(() =>
  props.state.status === 'needs_clarification' || props.state.status === 'needs_confirmation',
);

const STATUS_LABELS: Record<string, string> = {
  queued: 'Queued',
  planning: 'Planning',
  running: 'Running',
  needs_clarification: 'Waiting for your answer',
  needs_confirmation: 'Waiting for your confirmation',
  complete: 'Complete',
  partial: 'Partial — stopped early',
  failed: 'Failed',
  cancelled: 'Cancelled',
  interrupted: 'Interrupted — ask the agent to continue it',
};
const statusLabel = computed(() => STATUS_LABELS[props.state.status] ?? props.state.status);

const tone = computed(() => {
  const s = props.state.status;
  if (isWorking.value) return 'working';
  if (s === 'complete') return 'ok';
  if (s === 'partial' || isWaiting.value) return 'warn';
  return 'bad';
});

const modeLabel = computed(() => (props.state.mode === 'compile' ? 'Compile' : 'Analyze'));

const progress = computed(() => {
  const p = props.state.progress;
  const total = Math.max(0, Number(p?.total ?? 0));
  const done = Math.min(total, Math.max(0, Number(p?.done ?? 0)));
  return { done, total, pct: total > 0 ? Math.round((done / total) * 100) : 0 };
});

const tokensLabel = computed(() => {
  const u = props.state.usage;
  if (!u) return null;
  const used = (u.tokens_in ?? 0) + (u.tokens_out ?? 0);
  if (!used && !u.budget) return null;
  return u.budget
    ? `${formatTokensCompact(used)} / ${formatTokensCompact(u.budget)} tokens`
    : `${formatTokensCompact(used)} tokens`;
});

function stepIcon(step: ResearchActivityStep): string {
  if (step.status === 'done') return 'mdi:check-circle-outline';
  if (step.status === 'failed') return 'mdi:close-circle-outline';
  if (step.status === 'stopped') return 'mdi:pause-circle-outline';
  return 'mdi:loading';
}

// Elapsed time, ticking while the job works.
const nowMs = ref(Date.now());
let timer: ReturnType<typeof setInterval> | null = null;
watch(
  isWorking,
  working => {
    if (working && !timer) {
      timer = setInterval(() => { nowMs.value = Date.now(); }, 1000);
    } else if (!working && timer) {
      clearInterval(timer);
      timer = null;
    }
  },
  { immediate: true },
);
const elapsedLabel = computed(() => {
  const startMs = (props.state.started_at || 0) * 1000;
  const endMs = isWorking.value ? nowMs.value : (props.state.updated_at || props.state.started_at || 0) * 1000;
  const secs = Math.max(0, Math.round((endMs - startMs) / 1000));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
});

// Cancel: the server settles the job; the next frame turns the panel.
const cancelling = ref(false);
watch(() => props.state.job_id, () => { cancelling.value = false; });
watch(isWorking, working => { if (!working) cancelling.value = false; });
async function cancel() {
  if (cancelling.value) return;
  cancelling.value = true;
  try {
    const settings = useSettingsStore();
    await cancelResearchJob(settings.agentUrl, settings.authToken, props.state.job_id);
  } catch (e: any) {
    cancelling.value = false;
    ElMessage.error(e?.message || 'Could not cancel the research job.');
  }
}

// Expand a step's detail.
const expanded = ref<Set<string>>(new Set());
function toggleDetail(step: ResearchActivityStep) {
  if (!step.detail) return;
  const next = new Set(expanded.value);
  if (next.has(step.id)) next.delete(step.id);
  else next.add(step.id);
  expanded.value = next;
}

// Highlight-then-fade on each update; follow the newest step unless the user
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
    const follow = nearBottom();
    nextTick(() => {
      if (follow && listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight;
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
    class="ra-pill"
    :class="[`tone-${tone}`, { bright }]"
    title="Show research activity"
    aria-label="Show research activity"
    @click="minimized = false"
  >
    <Icon icon="mdi:file-search-outline" class="ra-header-icon" />
    <span class="ra-name">Research</span>
    <Icon v-if="isWorking" icon="mdi:loading" class="spin" />
    <span v-else-if="progress.total" class="ra-count">{{ progress.done }}/{{ progress.total }}</span>
  </button>

  <section
    v-else
    class="ra-panel"
    :class="[`tone-${tone}`, { bright }]"
    role="status"
    aria-live="polite"
    aria-label="Research activity"
  >
    <div class="ra-header">
      <Icon icon="mdi:file-search-outline" class="ra-header-icon" />
      <span class="ra-name">Research</span>
      <span class="ra-badge">{{ modeLabel }}</span>
      <span class="ra-elapsed">{{ elapsedLabel }}</span>
      <button class="ra-action" title="Minimize" aria-label="Minimize" @click="minimized = true">
        <Icon icon="mdi:window-minimize" />
      </button>
      <button v-if="!isWorking" class="ra-action" title="Close" aria-label="Close" @click="emit('dismiss')">
        <Icon icon="mdi:close" />
      </button>
    </div>

    <p v-if="state.title" class="ra-question" :title="state.title">{{ state.title }}</p>

    <div class="ra-status">
      <Icon
        :icon="isWorking ? 'mdi:loading' : tone === 'ok' ? 'mdi:check-circle' : isWaiting ? 'mdi:help-circle' : 'mdi:alert-circle'"
        class="ra-status-icon"
        :class="{ spin: isWorking }"
      />
      <span class="ra-status-label">{{ statusLabel }}</span>
      <span v-if="state.phase && isWorking" class="ra-phase" :title="state.phase">· {{ state.phase }}</span>
    </div>

    <div v-if="progress.total" class="ra-progress" :aria-label="`${progress.done} of ${progress.total} done`">
      <div class="ra-bar"><div class="ra-bar-fill" :style="{ width: `${progress.pct}%` }" /></div>
      <span class="ra-progress-label">{{ progress.done }} / {{ progress.total }}</span>
    </div>

    <ul v-if="state.steps.length" ref="listRef" class="ra-list">
      <li
        v-for="step in state.steps"
        :key="step.id"
        class="ra-item"
        :class="[step.status ? `status-${step.status}` : 'status-running', { flash: flashIds.has(step.id), clickable: !!step.detail }]"
        @click="toggleDetail(step)"
      >
        <Icon
          :icon="stepIcon(step)"
          class="ra-item-icon"
          :class="{ spin: !step.status || step.status === 'running' }"
        />
        <div class="ra-item-body">
          <span class="ra-item-label">{{ step.label }}</span>
          <pre v-if="step.detail && expanded.has(step.id)" class="ra-item-detail">{{ step.detail }}</pre>
        </div>
      </li>
    </ul>

    <p v-if="state.summary && !isWorking" class="ra-summary">{{ state.summary }}</p>
    <p v-if="state.error" class="ra-error" :title="state.error">{{ state.error }}</p>

    <div class="ra-footer">
      <span v-if="tokensLabel" class="ra-tokens">{{ tokensLabel }}</span>
      <span v-if="state.total_steps > state.steps.length" class="ra-more">
        {{ state.total_steps }} steps
      </span>
      <button
        v-if="isWorking"
        type="button"
        class="ra-cancel"
        :disabled="cancelling"
        @click="cancel"
      >
        <Icon :icon="cancelling ? 'mdi:loading' : 'mdi:stop-circle-outline'" :class="{ spin: cancelling }" />
        {{ cancelling ? 'Cancelling…' : 'Cancel' }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.ra-panel,
.ra-pill {
  --tone: var(--primary-color);
}
.tone-ok { --tone: var(--success-color); }
.tone-warn { --tone: var(--warning-color); }
.tone-bad { --tone: var(--danger-color); }

.ra-panel {
  width: 320px;
  max-width: calc(100% - 32px);
  max-height: 55%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--surface-color);
  border: 1px solid color-mix(in srgb, var(--tone) 30%, var(--border-color));
  border-radius: 10px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  color: var(--text-primary);
  opacity: 0.7;
  transition: opacity 0.4s ease, box-shadow 0.4s ease;
}
.ra-panel.bright,
.ra-panel:hover,
.ra-panel.tone-warn {
  opacity: 1;
}
.ra-panel.bright {
  box-shadow: 0 6px 20px color-mix(in srgb, var(--tone) 22%, transparent);
}

.ra-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-color);
  user-select: none;
  flex-shrink: 0;
}
.ra-header-icon {
  font-size: 16px;
  color: var(--tone);
  flex-shrink: 0;
}
.ra-name {
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
}
.ra-badge {
  font-size: 11px;
  line-height: 1;
  padding: 3px 7px;
  border-radius: 999px;
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--primary-color) 10%, var(--surface-color));
  border: 1px solid color-mix(in srgb, var(--primary-color) 25%, var(--border-color));
}
.ra-elapsed {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.ra-action {
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
}
.ra-action:hover {
  background: var(--hover-bg);
  color: var(--text-primary);
}

.ra-question {
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

.ra-status {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px 0;
  font-size: 12px;
  min-width: 0;
  flex-shrink: 0;
}
.ra-status-icon {
  font-size: 15px;
  color: var(--tone);
  flex-shrink: 0;
}
.ra-status-label {
  font-weight: 600;
  white-space: nowrap;
}
.ra-phase {
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.ra-progress {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px 0;
  flex-shrink: 0;
}
.ra-bar {
  flex: 1;
  height: 6px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--tone) 14%, var(--surface-color));
  overflow: hidden;
}
.ra-bar-fill {
  height: 100%;
  border-radius: 999px;
  background: var(--tone);
  transition: width 0.4s ease;
}
.ra-progress-label {
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.ra-list {
  list-style: none;
  margin: 6px 0 0;
  padding: 0 6px;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}
.ra-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 5px 8px;
  border-radius: 6px;
  font-size: 12.5px;
  transition: background 1.2s ease;
}
.ra-item.clickable {
  cursor: pointer;
}
.ra-item.flash {
  background: color-mix(in srgb, var(--primary-color) 10%, transparent);
}
.ra-item-icon {
  flex-shrink: 0;
  font-size: 15px;
  margin-top: 1px;
  color: var(--primary-color);
}
.ra-item.status-done .ra-item-icon { color: var(--success-color); }
.ra-item.status-failed .ra-item-icon { color: var(--danger-color); }
.ra-item.status-stopped .ra-item-icon { color: var(--text-tertiary); }
.ra-item-body {
  min-width: 0;
  flex: 1 1 auto;
}
.ra-item-label {
  display: block;
  line-height: 1.4;
  word-break: break-word;
}
.ra-item-detail {
  margin: 4px 0 0;
  padding: 6px 8px;
  background: var(--code-bg, color-mix(in srgb, var(--text-primary) 8%, transparent));
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 160px;
  overflow: auto;
  color: var(--text-secondary);
}

.ra-summary,
.ra-error {
  margin: 6px 12px 0;
  font-size: 12px;
  line-height: 1.45;
  flex-shrink: 0;
}
.ra-summary {
  color: var(--text-secondary);
}
.ra-error {
  color: var(--danger-color);
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
}

.ra-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding: 8px 12px;
  border-top: 1px solid var(--border-color);
  font-size: 12px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}
.ra-tokens {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.ra-cancel {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  font-size: 12px;
  border-radius: 6px;
  cursor: pointer;
  color: var(--danger-color);
  background: color-mix(in srgb, var(--danger-color) 8%, var(--surface-color));
  border: 1px solid color-mix(in srgb, var(--danger-color) 35%, var(--border-color));
  transition: background 0.15s ease;
}
.ra-cancel:hover:not(:disabled) {
  background: color-mix(in srgb, var(--danger-color) 16%, var(--surface-color));
}
.ra-cancel:disabled {
  cursor: default;
  opacity: 0.7;
}

.ra-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: var(--surface-color);
  border: 1px solid color-mix(in srgb, var(--tone) 30%, var(--border-color));
  border-radius: 999px;
  color: var(--text-primary);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  cursor: pointer;
  opacity: 0.7;
  transition: opacity 0.4s ease;
}
.ra-pill:hover,
.ra-pill.bright {
  opacity: 1;
}
.ra-count {
  font-size: 12px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

.spin {
  animation: ra-spin 1s linear infinite;
}
@keyframes ra-spin {
  to { transform: rotate(360deg); }
}
@media (prefers-reduced-motion: reduce) {
  .spin { animation: none; }
}
</style>
