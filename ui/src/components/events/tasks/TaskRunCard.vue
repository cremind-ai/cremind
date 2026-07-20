<script setup lang="ts">
import { computed } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Icon } from '@iconify/vue';
import EventRunStatusTag from '../EventRunStatusTag.vue';
import EventIdChip from '../EventIdChip.vue';
import { useEventRunsStore } from '../../../stores/eventRuns';
import { formatTokensCompact, formatUsd } from '../../../utils/usageFormat';
import { formatDurationMs, formatRelative, runDuration } from '../../../utils/duration';
import { accentColor, sourceKindIcon } from './boardTypes';
import type { BoardSubscription } from './boardTypes';
import type { EventRun } from '../../../services/eventRunsApi';

const props = defineProps<{
  run: EventRun;
  // Only used for the card's icon/title fallbacks; rule/event actions (pause,
  // edit, delete the rule) live on the EVENTS-column card, not here.
  sub: BoardSubscription | null;
  now: number;
  /** Show the clickable event chip (hidden when already filtered to one event). */
  showEventChip?: boolean;
}>();

const emit = defineEmits<{
  (e: 'filter-event', key: string): void;
}>();

const store = useEventRunsStore();

const groupKey = computed(() => `${props.run.source_kind}:${props.run.subscription_id}`);
const accent = computed(() => accentColor(groupKey.value));
const icon = computed(() => props.sub?.icon ?? sourceKindIcon(props.run.source_kind));
// Runs carry a denormalized `label`, so an orphaned run (parent rule deleted)
// still names itself even with no subscription loaded.
const title = computed(() => props.sub?.title || props.run.label || 'Event');
const isTerminal = computed(() =>
  ['completed', 'failed', 'cancelled'].includes(props.run.status),
);
const firedLabel = computed(() => formatRelative(props.run.created_at, props.now));
const waitLabel = computed(() => formatDurationMs(props.now - props.run.created_at));
const durationLabel = computed(() =>
  isTerminal.value ? runDuration(props.run) : '',
);

function open() {
  store.openRun(props.run.id);
}

async function cancel() {
  try {
    await store.cancelRun(props.run.id);
    ElMessage.success('Cancellation requested');
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

async function confirmDelete() {
  try {
    await ElMessageBox.confirm(
      'Delete this run and its conversation? Usage totals are kept in Usage & Cost.',
      'Delete run',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await store.removeRun(props.run.id);
    ElMessage.success('Run deleted');
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}
</script>

<template>
  <article
    class="run-card"
    :class="`st-${run.status}`"
    :style="{ borderLeftColor: accent }"
    @click="open"
  >
    <div class="rc-top">
      <Icon :icon="icon" class="rc-src" />
      <button
        v-if="showEventChip !== false"
        type="button"
        class="rc-event"
        :title="`Filter to ${title}`"
        @click.stop="emit('filter-event', groupKey)"
      >
        {{ title }}
      </button>
      <span v-else class="rc-event static" :title="title">{{ title }}</span>
      <span class="rc-spacer" />
      <EventRunStatusTag :status="run.status" />
    </div>

    <p class="rc-action" :title="run.action || run.label">{{ run.action || run.label }}</p>

    <p v-if="run.status === 'running'" class="rc-line rc-running">
      <Icon icon="mdi:timer-outline" />
      <span>{{ runDuration(run, now) }}</span>
      <span v-if="run.turn_count"> · turn {{ run.turn_count }}</span>
    </p>
    <p
      v-else-if="run.status === 'pending' && run.pending_question"
      class="rc-line rc-pending"
      :title="run.pending_question"
    >
      <Icon icon="mdi:comment-question-outline" />
      <span>{{ run.pending_question }}</span>
    </p>
    <p
      v-else-if="run.status === 'failed' && run.error"
      class="rc-line rc-failed"
      :title="run.error"
    >
      <Icon icon="mdi:alert-circle-outline" />
      <span>{{ run.error }}</span>
    </p>

    <div class="rc-foot">
      <span class="rc-time">{{ firedLabel }}</span>
      <span v-if="run.status === 'pending'" class="rc-wait">waiting {{ waitLabel }}</span>
      <span v-else-if="durationLabel" class="rc-dur">{{ durationLabel }}</span>
      <span class="rc-spacer" />
      <span class="rc-metric">{{ formatTokensCompact(run.usage.total_tokens) }}</span>
      <span class="rc-metric">{{ formatUsd(run.usage.total_usd) }}</span>
      <EventIdChip :id="run.id" kind="run" size="xs" class="rc-id" />
    </div>

    <div class="rc-actions">
      <button
        v-if="run.status === 'running'"
        type="button"
        class="rc-act danger"
        title="Cancel run"
        @click.stop="cancel"
      >
        <Icon icon="mdi:stop-circle-outline" />
      </button>
      <button
        v-else-if="run.status === 'pending'"
        type="button"
        class="rc-act primary"
        title="Reply to this run"
        @click.stop="open"
      >
        <Icon icon="mdi:reply" /> Reply
      </button>
      <button
        v-if="isTerminal"
        type="button"
        class="rc-act danger"
        title="Delete run"
        @click.stop="confirmDelete"
      >
        <Icon icon="mdi:delete-outline" />
      </button>
      <button type="button" class="rc-act" title="Open run" @click.stop="open">
        <Icon icon="mdi:open-in-app" />
      </button>
    </div>
  </article>
</template>

<style scoped>
.run-card {
  position: relative;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-left: 3px solid var(--border-color);
  border-radius: 8px;
  padding: 8px 10px;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.run-card:hover {
  border-color: var(--primary-color);
  box-shadow: var(--el-box-shadow-light);
}
.st-failed { background: color-mix(in srgb, var(--danger-color, #f56c6c) 6%, var(--surface-color)); }

.rc-top {
  display: flex;
  align-items: center;
  gap: 6px;
}
.rc-src {
  font-size: 0.95rem;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.rc-event {
  border: none;
  background: transparent;
  padding: 0;
  font: inherit;
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--text-primary);
  cursor: pointer;
  max-width: 148px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rc-event:hover:not(.static) {
  color: var(--primary-color);
  text-decoration: underline;
}
.rc-event.static { cursor: default; }
.rc-spacer { flex: 1; }

.rc-action {
  margin: 6px 0 0;
  font-size: 0.8125rem;
  color: var(--text-secondary);
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.rc-line {
  margin: 6px 0 0;
  font-size: 0.75rem;
  display: flex;
  align-items: center;
  gap: 4px;
  overflow: hidden;
}
.rc-line span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rc-running { color: var(--primary-color); }
.rc-pending { color: var(--warning-color, #e6a23c); }
.rc-failed { color: var(--danger-color, #f56c6c); }

.rc-foot {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.6875rem;
  color: var(--text-tertiary);
  flex-wrap: wrap;
}
.rc-wait { color: var(--warning-color, #e6a23c); }
.rc-id { flex-shrink: 0; }
.rc-metric { font-variant-numeric: tabular-nums; }

.rc-actions {
  position: absolute;
  top: 6px;
  right: 6px;
  display: none;
  gap: 4px;
  background: var(--surface-color);
  padding: 2px;
  border-radius: 6px;
  box-shadow: var(--el-box-shadow-light);
}
.run-card:hover .rc-actions { display: flex; }
.rc-act {
  border: 1px solid var(--border-color);
  background: var(--bg-color);
  color: var(--text-secondary);
  border-radius: 5px;
  padding: 2px 6px;
  font-size: 0.75rem;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 3px;
}
.rc-act:hover { color: var(--primary-color); border-color: var(--primary-color); }
.rc-act.primary { color: var(--primary-color); border-color: var(--primary-color); }
.rc-act.danger:hover { color: var(--danger-color, #f56c6c); border-color: var(--danger-color, #f56c6c); }
</style>
