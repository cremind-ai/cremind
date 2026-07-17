<script setup lang="ts">
import { computed, reactive, ref } from 'vue';
import { ElEmpty, ElInput, ElOption, ElSelect } from 'element-plus';
import { Icon } from '@iconify/vue';
import TaskBoardColumn from './TaskBoardColumn.vue';
import TaskRunCard from './TaskRunCard.vue';
import TaskEventGroup from './TaskEventGroup.vue';
import TaskIdleCard from './TaskIdleCard.vue';
import {
  fromFileWatcher,
  fromSchedule,
  fromSkillEvent,
  sourceKindIcon,
  type BoardSubscription,
} from './boardTypes';
import { useEventRunsStore } from '../../../stores/eventRuns';
import { useAdminSubscriptions } from '../../../composables/useAdminSubscriptions';
import { useNow } from '../../../composables/useNow';
import { adminEventsStatus } from '../../../services/adminEventsStream';
import type { EventRun, EventRunSourceKind } from '../../../services/eventRunsApi';

const props = defineProps<{ profile: string }>();

const store = useEventRunsStore();
const { skillSubs, listeners, fileWatchers, schedules, loading } = useAdminSubscriptions();
const { now } = useNow();

const TERMINAL: EventRun['status'][] = ['completed', 'failed', 'cancelled'];
const KINDS: { value: EventRunSourceKind; label: string; icon: string }[] = [
  { value: 'skill_event', label: 'Skill', icon: sourceKindIcon('skill_event') },
  { value: 'file_watcher', label: 'Watcher', icon: sourceKindIcon('file_watcher') },
  { value: 'schedule', label: 'Schedule', icon: sourceKindIcon('schedule') },
];

// ── toolbar filter state (transient; not persisted) ──
const activeKinds = reactive<Record<EventRunSourceKind, boolean>>({
  skill_event: true,
  file_watcher: true,
  schedule: true,
});
const search = ref('');
const eventFilter = ref<string | null>(null);
const doneTimebox = ref<'recent' | 'today' | '24h'>('recent');
const failuresOnly = ref(false);

function toggleKind(k: EventRunSourceKind) {
  activeKinds[k] = !activeKinds[k];
}

// ── normalized subscriptions ──
const allSubs = computed<BoardSubscription[]>(() => [
  ...skillSubs.value.map(fromSkillEvent),
  ...fileWatchers.value.map(fromFileWatcher),
  ...schedules.value.map(fromSchedule),
]);
const subsByKey = computed(() => {
  const m = new Map<string, BoardSubscription>();
  for (const s of allSubs.value) m.set(s.key, s);
  return m;
});
const eventFilterTitle = computed(() =>
  eventFilter.value ? subsByKey.value.get(eventFilter.value)?.title ?? 'Selected event' : '',
);

// ── runs (snapshot only, profile-guarded) ──
const profileRuns = computed(() =>
  store.snapshotRuns.filter((r) => r.profile === props.profile),
);

function keyOf(r: EventRun) {
  return `${r.source_kind}:${r.subscription_id}`;
}
function matchesSearch(r: EventRun): boolean {
  const q = search.value.trim().toLowerCase();
  if (!q) return true;
  const sub = subsByKey.value.get(keyOf(r));
  const hay = [sub?.title, r.label, r.action, r.pending_question]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
  return hay.includes(q);
}

const filteredRuns = computed(() =>
  profileRuns.value.filter((r) => {
    if (!activeKinds[r.source_kind]) return false;
    if (eventFilter.value && keyOf(r) !== eventFilter.value) return false;
    return matchesSearch(r);
  }),
);

// filteredRuns preserves snapshot order (newest first).
const runningRuns = computed(() => filteredRuns.value.filter((r) => r.status === 'running'));
const pendingRuns = computed(() =>
  filteredRuns.value
    .filter((r) => r.status === 'pending')
    .slice()
    .sort((a, b) => a.created_at - b.created_at), // oldest first: stale blockers surface
);

function inTimebox(r: EventRun): boolean {
  if (doneTimebox.value === 'recent') return true;
  const end = r.finished_at ?? r.updated_at ?? r.created_at;
  if (doneTimebox.value === '24h') return end >= now.value - 24 * 3600 * 1000;
  const midnight = new Date(now.value);
  midnight.setHours(0, 0, 0, 0);
  return end >= midnight.getTime();
}

const doneRuns = computed(() =>
  filteredRuns.value.filter(
    (r) =>
      TERMINAL.includes(r.status) &&
      (!failuresOnly.value || r.status === 'failed') &&
      inTimebox(r),
  ),
);

interface DoneGroup {
  key: string;
  sub: BoardSubscription | null;
  runs: EventRun[];
}
const doneGroups = computed<DoneGroup[]>(() => {
  const m = new Map<string, DoneGroup>();
  for (const r of doneRuns.value) {
    const key = keyOf(r);
    let g = m.get(key);
    if (!g) {
      g = { key, sub: subsByKey.value.get(key) ?? null, runs: [] };
      m.set(key, g);
    }
    g.runs.push(r);
  }
  return [...m.values()];
});

// ── Upcoming / idle: subscriptions with no active run ──
const activeKeys = computed(() => {
  const s = new Set<string>();
  for (const r of profileRuns.value) {
    if (r.status === 'running' || r.status === 'pending') s.add(keyOf(r));
  }
  return s;
});

const idleEntries = computed(() => {
  const q = search.value.trim().toLowerCase();
  return allSubs.value
    .filter((e) => {
      if (!activeKinds[e.kind]) return false;
      if (eventFilter.value && e.key !== eventFilter.value) return false;
      if (activeKeys.value.has(e.key)) return false;
      // Only surface schedules that can still fire (or are paused); completed /
      // cancelled schedules live in Done via their terminal runs.
      if (e.kind === 'schedule' && !(e.scheduleStatus === 'active' || e.scheduleStatus === 'paused')) {
        return false;
      }
      if (q && !`${e.title} ${e.action}`.toLowerCase().includes(q)) return false;
      return true;
    })
    .sort((a, b) => {
      const an = a.kind === 'schedule' && a.scheduleStatus === 'active' ? a.nextFireAtMs : null;
      const bn = b.kind === 'schedule' && b.scheduleStatus === 'active' ? b.nextFireAtMs : null;
      if (an != null && bn != null) return an - bn;
      if (an != null) return -1;
      if (bn != null) return 1;
      return b.createdAtMs - a.createdAtMs;
    });
});

const boardEmpty = computed(
  () => !loading.value && allSubs.value.length === 0 && profileRuns.value.length === 0,
);
const showChip = computed(() => !eventFilter.value);
</script>

<template>
  <div class="tasks-board">
    <div class="board-toolbar">
      <div class="tb-kinds">
        <button
          v-for="k in KINDS"
          :key="k.value"
          type="button"
          class="tb-kind"
          :class="{ active: activeKinds[k.value] }"
          @click="toggleKind(k.value)"
        >
          <Icon :icon="k.icon" /> {{ k.label }}
        </button>
      </div>
      <div class="tb-right">
        <span v-if="eventFilter" class="tb-pill">
          <Icon icon="mdi:filter-variant" />
          {{ eventFilterTitle }}
          <button type="button" title="Clear filter" @click="eventFilter = null">
            <Icon icon="mdi:close" />
          </button>
        </span>
        <ElInput
          v-model="search"
          placeholder="Search events…"
          clearable
          size="small"
          class="tb-search"
        >
          <template #prefix><Icon icon="mdi:magnify" /></template>
        </ElInput>
      </div>
    </div>

    <p v-if="adminEventsStatus === 'reconnecting'" class="board-reconnect">
      <Icon icon="mdi:lan-disconnect" /> Reconnecting — data may be stale.
    </p>

    <ElEmpty
      v-if="boardEmpty"
      description="No events yet. Ask the assistant to set up an automation."
    />

    <div v-else class="board-cols">
      <TaskBoardColumn
        title="Upcoming"
        icon="mdi:calendar-arrow-right"
        :count="idleEntries.length"
        empty="No idle events."
      >
        <TransitionGroup name="board-card" tag="div" class="col-list">
          <TaskIdleCard
            v-for="e in idleEntries"
            :key="e.key"
            :entry="e"
            :listeners="listeners"
            :summary="store.summaryForSubscription(e.kind, e.id)"
            :now="now"
            @filter-event="(k) => (eventFilter = k)"
          />
        </TransitionGroup>
      </TaskBoardColumn>

      <TaskBoardColumn
        title="Running"
        icon="mdi:play-circle-outline"
        tone="primary"
        :count="runningRuns.length"
        empty="Nothing running."
      >
        <TransitionGroup name="board-card" tag="div" class="col-list">
          <TaskRunCard
            v-for="r in runningRuns"
            :key="r.id"
            :run="r"
            :sub="subsByKey.get(keyOf(r)) ?? null"
            :now="now"
            :show-event-chip="showChip"
            @filter-event="(k) => (eventFilter = k)"
          />
        </TransitionGroup>
      </TaskBoardColumn>

      <TaskBoardColumn
        title="Needs input"
        icon="mdi:account-clock-outline"
        tone="warning"
        :count="pendingRuns.length"
        empty="No runs waiting for input."
      >
        <TransitionGroup name="board-card" tag="div" class="col-list">
          <TaskRunCard
            v-for="r in pendingRuns"
            :key="r.id"
            :run="r"
            :sub="subsByKey.get(keyOf(r)) ?? null"
            :now="now"
            :show-event-chip="showChip"
            @filter-event="(k) => (eventFilter = k)"
          />
        </TransitionGroup>
      </TaskBoardColumn>

      <TaskBoardColumn
        title="Done"
        icon="mdi:check-circle-outline"
        tone="success"
        :count="doneRuns.length"
        :empty="failuresOnly ? 'No failures.' : 'No finished runs.'"
      >
        <template #header-actions>
          <button
            type="button"
            class="done-fail"
            :class="{ active: failuresOnly }"
            title="Show failures only"
            @click="failuresOnly = !failuresOnly"
          >
            <Icon icon="mdi:alert-circle-outline" />
          </button>
          <ElSelect v-model="doneTimebox" size="small" class="done-box">
            <ElOption label="Recent" value="recent" />
            <ElOption label="Today" value="today" />
            <ElOption label="24h" value="24h" />
          </ElSelect>
        </template>
        <TransitionGroup name="board-card" tag="div" class="col-list">
          <TaskEventGroup
            v-for="g in doneGroups"
            :key="g.key"
            :sub="g.sub"
            :runs="g.runs"
            :now="now"
            :force-expanded="!!eventFilter"
            @filter-event="(k) => (eventFilter = k)"
          />
        </TransitionGroup>
        <p v-if="doneGroups.length" class="done-note">
          Showing recent runs. Older history is in the table view.
        </p>
      </TaskBoardColumn>
    </div>
  </div>
</template>

<style scoped>
.tasks-board {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.board-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.tb-kinds {
  display: flex;
  gap: 6px;
}
.tb-kind {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--border-color);
  background: var(--surface-color);
  color: var(--text-secondary);
  border-radius: 999px;
  padding: 3px 10px;
  font-size: 0.8125rem;
  cursor: pointer;
  transition: all 0.15s ease;
}
.tb-kind.active {
  border-color: var(--primary-color);
  color: var(--primary-color);
  background: color-mix(in srgb, var(--primary-color) 10%, var(--surface-color));
}
.tb-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.tb-search {
  width: 200px;
}
.tb-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 0.8125rem;
  color: var(--primary-color);
  background: color-mix(in srgb, var(--primary-color) 12%, var(--surface-color));
  border: 1px solid var(--primary-color);
  border-radius: 999px;
  padding: 2px 6px 2px 10px;
}
.tb-pill button {
  border: none;
  background: transparent;
  color: inherit;
  cursor: pointer;
  display: inline-flex;
  padding: 0;
}
.board-reconnect {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.8125rem;
  color: var(--warning-color, #e6a23c);
  background: color-mix(in srgb, var(--warning-color, #e6a23c) 12%, transparent);
  border-radius: 6px;
  padding: 6px 10px;
}
.board-cols {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 12px;
  overflow-x: auto;
  align-items: stretch;
}
.col-list {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.done-fail {
  border: 1px solid var(--border-color);
  background: var(--surface-color);
  color: var(--text-tertiary);
  border-radius: 5px;
  padding: 2px 6px;
  cursor: pointer;
  display: inline-flex;
}
.done-fail.active {
  color: var(--danger-color, #f56c6c);
  border-color: var(--danger-color, #f56c6c);
}
.done-box {
  width: 92px;
}
.done-note {
  margin: 4px;
  font-size: 0.6875rem;
  color: var(--text-tertiary);
  text-align: center;
}

/* Card movement between/within columns on live snapshot updates. */
.board-card-enter-active,
.board-card-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.board-card-enter-from {
  opacity: 0;
  transform: translateY(-6px);
}
.board-card-leave-to {
  opacity: 0;
  transform: scale(0.97);
}
.board-card-leave-active {
  position: absolute;
  width: calc(100% - 16px);
}
.board-card-move {
  transition: transform 0.2s ease;
}
</style>
