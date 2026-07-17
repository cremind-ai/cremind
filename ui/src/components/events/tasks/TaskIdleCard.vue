<script setup lang="ts">
import { computed } from 'vue';
import { Icon } from '@iconify/vue';
import { useEventRunsStore } from '../../../stores/eventRuns';
import { formatRelative } from '../../../utils/duration';
import { formatTimestamp } from '../../../utils/usageFormat';
import { accentColor } from './boardTypes';
import type { BoardSubscription } from './boardTypes';
import type { ListenerStatus } from '../../../services/skillEventsApi';
import type { EventRunSubscriptionSummary } from '../../../services/adminEventsStream';
import type { EventRunStatus } from '../../../services/eventRunsApi';

const props = defineProps<{
  entry: BoardSubscription;
  listeners: Record<string, ListenerStatus>;
  summary: EventRunSubscriptionSummary | null;
  now: number;
}>();

const emit = defineEmits<{ (e: 'filter-event', key: string): void }>();

const store = useEventRunsStore();

const accent = computed(() => accentColor(props.entry.key));

const openable = computed(() =>
  store.runsForSubscription(props.entry.kind, props.entry.id),
);

const listener = computed<ListenerStatus | null>(() =>
  props.entry.kind === 'skill_event'
    ? props.listeners[props.entry.skillName] ?? null
    : null,
);

interface StateLine {
  text: string;
  detail?: string;
  tone: 'ok' | 'warn' | 'muted';
  icon: string;
}

const state = computed<StateLine>(() => {
  const e = props.entry;
  if (e.kind === 'schedule') {
    if (e.scheduleStatus === 'paused') {
      return { text: 'Paused', tone: 'warn', icon: 'mdi:pause-circle-outline' };
    }
    if (e.scheduleStatus === 'active' && e.nextFireAtMs) {
      return {
        text: `Next ${formatRelative(e.nextFireAtMs, props.now)}`,
        detail: formatTimestamp(e.nextFireAtMs),
        tone: 'ok',
        icon: e.rrule ? 'mdi:autorenew' : 'mdi:clock-outline',
      };
    }
    if (e.scheduleStatus === 'completed') {
      return { text: 'Completed', tone: 'muted', icon: 'mdi:check-circle-outline' };
    }
    if (e.scheduleStatus === 'cancelled') {
      return { text: 'Cancelled', tone: 'muted', icon: 'mdi:cancel' };
    }
    return { text: 'Scheduled', tone: 'ok', icon: 'mdi:clock-outline' };
  }
  if (e.kind === 'file_watcher') {
    return e.armed
      ? { text: 'Watching', tone: 'ok', icon: 'mdi:eye-outline' }
      : { text: 'Disarmed', tone: 'muted', icon: 'mdi:eye-off-outline' };
  }
  // skill_event
  const l = listener.value;
  if (l?.running) return { text: 'Listening', tone: 'ok', icon: 'mdi:access-point' };
  if (l?.last_heartbeat) return { text: 'Listener down', tone: 'warn', icon: 'mdi:access-point-off' };
  return { text: 'Listener not started', tone: 'muted', icon: 'mdi:access-point-off' };
});

const STATUS_COLOR: Record<EventRunStatus, string> = {
  running: 'var(--primary-color)',
  pending: 'var(--warning-color, #e6a23c)',
  completed: 'var(--success-color, #67c23a)',
  failed: 'var(--danger-color, #f56c6c)',
  cancelled: 'var(--text-tertiary)',
};

const lastLine = computed(() => {
  const s = props.summary;
  if (!s || !s.run_count) return null;
  const rel = s.last_run_at ? formatRelative(s.last_run_at, props.now) : '';
  return {
    count: s.run_count,
    status: s.last_status ?? null,
    color: s.last_status ? STATUS_COLOR[s.last_status] : 'var(--text-tertiary)',
    rel,
  };
});

function open() {
  const runs = openable.value;
  if (runs.length) store.openRun(runs[0].id);
}
</script>

<template>
  <article
    class="idle-card"
    :class="{ clickable: openable.length > 0 }"
    :style="{ borderLeftColor: accent }"
    @click="open"
  >
    <div class="ic-top">
      <Icon :icon="entry.icon" class="ic-src" />
      <button
        type="button"
        class="ic-event"
        :title="`Filter to ${entry.title}`"
        @click.stop="emit('filter-event', entry.key)"
      >
        {{ entry.title }}
      </button>
    </div>

    <p class="ic-action" :title="entry.action">{{ entry.action }}</p>

    <p class="ic-state" :class="`tone-${state.tone}`">
      <Icon :icon="state.icon" />
      <span>{{ state.text }}</span>
      <span v-if="state.detail" class="ic-detail">· {{ state.detail }}</span>
    </p>

    <div class="ic-foot">
      <template v-if="lastLine">
        <span>{{ lastLine.count }} {{ lastLine.count === 1 ? 'run' : 'runs' }}</span>
        <span v-if="lastLine.status" class="ic-last">
          · last
          <span class="ic-dot" :style="{ background: lastLine.color }" />
          {{ lastLine.status }}
          <span v-if="lastLine.rel"> {{ lastLine.rel }}</span>
        </span>
      </template>
      <span v-else>No runs yet</span>
    </div>
  </article>
</template>

<style scoped>
.idle-card {
  background: transparent;
  border: 1px dashed var(--border-color);
  border-left: 3px solid var(--border-color);
  border-radius: 8px;
  padding: 8px 10px;
  cursor: default;
}
.idle-card.clickable { cursor: pointer; }
.idle-card.clickable:hover { border-color: var(--primary-color); }

.ic-top {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ic-src {
  font-size: 0.95rem;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.ic-event {
  border: none;
  background: transparent;
  padding: 0;
  font: inherit;
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--text-primary);
  cursor: pointer;
  max-width: 176px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ic-event:hover { color: var(--primary-color); text-decoration: underline; }

.ic-action {
  margin: 6px 0 0;
  font-size: 0.8125rem;
  color: var(--text-secondary);
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.ic-state {
  margin: 6px 0 0;
  font-size: 0.75rem;
  display: flex;
  align-items: center;
  gap: 4px;
}
.ic-state.tone-ok { color: var(--success-color, #67c23a); }
.ic-state.tone-warn { color: var(--warning-color, #e6a23c); }
.ic-state.tone-muted { color: var(--text-tertiary); }
.ic-detail { color: var(--text-tertiary); }

.ic-foot {
  margin-top: 6px;
  font-size: 0.6875rem;
  color: var(--text-tertiary);
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
.ic-last {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  text-transform: capitalize;
}
.ic-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
}
</style>
