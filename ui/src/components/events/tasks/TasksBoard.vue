<script setup lang="ts">
import { computed, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElEmpty, ElInput, ElMessage, ElMessageBox, ElOption, ElSelect } from 'element-plus';
import { Icon } from '@iconify/vue';
import TaskBoardColumn from './TaskBoardColumn.vue';
import TaskRunCard from './TaskRunCard.vue';
import TaskEventGroup from './TaskEventGroup.vue';
import TaskIdleCard from './TaskIdleCard.vue';
import SkillEventEditDialog from '../SkillEventEditDialog.vue';
import SkillEventSimulateDialog from '../SkillEventSimulateDialog.vue';
import FileWatcherEditDialog from '../FileWatcherEditDialog.vue';
import ScheduleEventDialog from '../../ScheduleEventDialog.vue';
import {
  fromFileWatcher,
  fromSchedule,
  fromSkillEvent,
  isRecurring,
  sourceKindIcon,
  type BoardSubscription,
  type RuleActionPayload,
} from './boardTypes';
import { useEventRunsStore } from '../../../stores/eventRuns';
import { useSettingsStore } from '../../../stores/settings';
import { useAdminSubscriptions } from '../../../composables/useAdminSubscriptions';
import { useNow } from '../../../composables/useNow';
import { adminEventsStatus } from '../../../services/adminEventsStream';
import { deleteSubscription, startListener, updateSubscription } from '../../../services/skillEventsApi';
import { deleteFileWatcher, updateFileWatcher } from '../../../services/fileWatchersApi';
import { deleteCalendarEvent, setScheduleEventStatus } from '../../../services/calendarApi';
import type { EventRun, EventRunSourceKind } from '../../../services/eventRunsApi';

const props = defineProps<{ profile: string }>();

const router = useRouter();
const store = useEventRunsStore();
const settings = useSettingsStore();
const { skillSubs, listeners, fileWatchers, schedules, loading } = useAdminSubscriptions();
const { now } = useNow();

// Rule-edit / simulate dialogs, hosted by the board (the table sections keep
// their own independent instances for Events mode).
const skillEditDialog = ref<InstanceType<typeof SkillEventEditDialog> | null>(null);
const skillSimulateDialog = ref<InstanceType<typeof SkillEventSimulateDialog> | null>(null);
const fwEditDialog = ref<InstanceType<typeof FileWatcherEditDialog> | null>(null);
const scheduleDialog = ref<InstanceType<typeof ScheduleEventDialog> | null>(null);

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

// A schedule that has run its course (completed / cancelled) — will never fire
// again, so it is dropped from the EVENTS column entirely (its runs still show
// under Done). Recurring rules that exhaust their recurrence-end also land here.
function isTerminalRule(e: BoardSubscription): boolean {
  return e.kind === 'schedule' && (e.scheduleStatus === 'completed' || e.scheduleStatus === 'cancelled');
}

// EVENTS column membership: show a rule iff it can still fire (NOT terminal) AND
// it is either recurring (multi-fire rules stay put even while a run is active,
// so you pause the event from here) or one-time with no active run (a one-time
// event lives in Running/Done while/after its single fire).
const idleEntries = computed(() => {
  const q = search.value.trim().toLowerCase();
  return allSubs.value
    .filter((e) => {
      if (!activeKinds[e.kind]) return false;
      if (eventFilter.value && e.key !== eventFilter.value) return false;
      if (isTerminalRule(e)) return false;
      if (!isRecurring(e) && activeKeys.value.has(e.key)) return false;
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

async function handleRuleAction({ action, sub }: RuleActionPayload) {
  switch (action) {
    case 'edit':
      if (sub.kind === 'skill_event') skillEditDialog.value?.open(sub.raw);
      else if (sub.kind === 'file_watcher') fwEditDialog.value?.open(sub.raw);
      else scheduleDialog.value?.openEditSubscription(sub.raw);
      break;
    case 'simulate':
      if (sub.kind === 'skill_event') skillSimulateDialog.value?.open(sub.raw);
      break;
    case 'start-listener':
      if (sub.kind === 'skill_event') {
        try {
          await startListener(settings.agentUrl, settings.authToken, sub.skillName);
          ElMessage.success(`${sub.skillName} listener started`);
        } catch (err) {
          ElMessage.error(err instanceof Error ? err.message : String(err));
        }
      }
      break;
    case 'toggle-pause':
      try {
        if (sub.kind === 'schedule') {
          const next = sub.scheduleStatus === 'paused' ? 'active' : 'paused';
          await setScheduleEventStatus(settings.agentUrl, settings.authToken, sub.id, next);
          ElMessage.success(next === 'paused' ? 'Event paused' : 'Event resumed');
        } else if (sub.kind === 'skill_event') {
          await updateSubscription(settings.agentUrl, settings.authToken, sub.id, { paused: !sub.paused });
          ElMessage.success(sub.paused ? 'Event resumed' : 'Event paused');
        } else if (sub.kind === 'file_watcher') {
          await updateFileWatcher(settings.agentUrl, settings.authToken, sub.id, { paused: !sub.paused });
          ElMessage.success(sub.paused ? 'Event resumed' : 'Event paused');
        }
      } catch (err) {
        ElMessage.error(err instanceof Error ? err.message : String(err));
      }
      break;
    case 'open-conversation':
      if (sub.conversationId) {
        router.push({
          name: 'conversation',
          params: { profile: props.profile, conversationId: sub.conversationId },
        });
      }
      break;
    case 'delete':
      await deleteRule(sub);
      break;
  }
}

async function deleteRule(sub: BoardSubscription) {
  const copy =
    sub.kind === 'skill_event'
      ? `Delete subscription for ${sub.skillName} → ${sub.eventType}?`
      : sub.kind === 'file_watcher'
        ? `Delete file watcher '${sub.title}' on ${sub.rootPath}?`
        : `Delete schedule event '${sub.title}'?`;
  try {
    await ElMessageBox.confirm(copy, 'Confirm delete', {
      confirmButtonText: 'Delete',
      cancelButtonText: 'Cancel',
      type: 'warning',
    });
  } catch {
    return; // cancelled
  }
  try {
    if (sub.kind === 'skill_event') {
      await deleteSubscription(settings.agentUrl, settings.authToken, sub.id);
      ElMessage.success('Subscription deleted');
    } else if (sub.kind === 'file_watcher') {
      await deleteFileWatcher(settings.agentUrl, settings.authToken, sub.id);
      ElMessage.success('File watcher deleted');
    } else {
      await deleteCalendarEvent(settings.agentUrl, settings.authToken, sub.id);
      ElMessage.success('Schedule event deleted');
    }
    // Don't leave the filter pill pointing at a rule that no longer exists.
    if (eventFilter.value === sub.key) eventFilter.value = null;
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}
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
        title="Events"
        icon="mdi:calendar-arrow-right"
        :count="idleEntries.length"
        empty="No events."
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
            @rule-action="handleRuleAction"
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

    <!-- Rule-edit / simulate dialogs, opened by handleRuleAction. -->
    <SkillEventEditDialog ref="skillEditDialog" />
    <SkillEventSimulateDialog ref="skillSimulateDialog" />
    <FileWatcherEditDialog ref="fwEditDialog" />
    <ScheduleEventDialog ref="scheduleDialog" />
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
