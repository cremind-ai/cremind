<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton,
  ElEmpty,
  ElMessage,
  ElMessageBox,
  ElTable,
  ElTableColumn,
  ElTag,
  ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import {
  deleteCalendarEvent,
  setScheduleEventStatus,
  type ScheduleEventSubscription,
} from '../services/calendarApi';
import {
  subscribeScheduleEventsAdmin,
  type AdminEventsSubHandle,
} from '../services/adminEventsStream';
import CollapsibleSection from './CollapsibleSection.vue';
import EventRunHistory from './events/EventRunHistory.vue';
import ScheduleEventDialog from './ScheduleEventDialog.vue';

const props = defineProps<{ profile: string }>();

const scheduleDialog = ref<InstanceType<typeof ScheduleEventDialog> | null>(null);
function openEdit(row: ScheduleEventSubscription) {
  scheduleDialog.value?.openEditSubscription(row);
}
const router = useRouter();
const settings = useSettingsStore();

const subscriptions = ref<ScheduleEventSubscription[]>([]);
const enabled = ref(false);
const loading = ref(false);
const errorMessage = ref('');

const sortedSubs = computed(() =>
  [...subscriptions.value].sort((a, b) => {
    const an = a.next_fire_at ?? Number.MAX_SAFE_INTEGER;
    const bn = b.next_fire_at ?? Number.MAX_SAFE_INTEGER;
    return an - bn;
  }),
);

let streamHandle: AdminEventsSubHandle | null = null;

function streamStart() {
  streamStop();
  if (!settings.agentUrl || !settings.authToken) return;
  loading.value = true;
  errorMessage.value = '';
  streamHandle = subscribeScheduleEventsAdmin(
    settings.agentUrl,
    settings.authToken,
    (snap) => {
      subscriptions.value = snap.subscriptions;
      enabled.value = snap.enabled;
      loading.value = false;
      errorMessage.value = '';
    },
  );
}

function streamStop() {
  if (streamHandle) {
    streamHandle.close();
    streamHandle = null;
  }
}

onMounted(() => { streamStart(); });

watch(
  () => settings.authToken,
  (token, prev) => { if (token && !prev) streamStart(); },
);

onBeforeUnmount(() => { streamStop(); });

function openConversation(id: string) {
  router.push({ name: 'conversation', params: { profile: props.profile, conversationId: id } });
}

function scheduleLabel(row: ScheduleEventSubscription): string {
  if (!row.rrule) return 'One-time';
  return row.rrule.replace(/^FREQ=/, '').replace(/;/g, ' · ');
}

function nextRun(row: ScheduleEventSubscription): string {
  if (!row.next_fire_at) return '—';
  return new Date(row.next_fire_at * 1000).toLocaleString();
}

function statusType(status: string): 'success' | 'info' | 'warning' | 'danger' {
  if (status === 'active') return 'success';
  if (status === 'paused') return 'warning';
  if (status === 'cancelled') return 'danger';
  return 'info';
}

async function togglePause(row: ScheduleEventSubscription) {
  const next = row.status === 'paused' ? 'active' : 'paused';
  try {
    await setScheduleEventStatus(settings.agentUrl, settings.authToken, row.id, next);
    ElMessage.success(next === 'paused' ? 'Schedule paused' : 'Schedule resumed');
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

async function confirmDelete(row: ScheduleEventSubscription) {
  try {
    await ElMessageBox.confirm(
      `Delete schedule event '${row.title}'?`,
      'Confirm delete',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await deleteCalendarEvent(settings.agentUrl, settings.authToken, row.id);
    ElMessage.success('Schedule event deleted');
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}
</script>

<template>
  <section class="sched-section">
    <CollapsibleSection title="Schedule Events" icon="mdi:calendar-clock" :count="enabled ? sortedSubs.length : 0">
    <p class="section-blurb">
      Time-based events from the <strong>Calendar &amp; Schedule</strong> feature.
      Each fires at its time (and, for a recurrence, at every following
      occurrence) — running its action in the conversation that created it.
      Create them from the calendar, or ask the assistant
      (e.g. "every weekday at 9am, summarize my unread email").
    </p>

    <p v-if="!enabled" class="muted">
      Calendar &amp; Schedule is currently turned off. Enable it on the
      <a class="conv-link" @click.prevent="router.push({ name: 'calendar-schedule', params: { profile } })">
        Calendar &amp; Schedule
      </a>
      page to start creating scheduled events.
    </p>

    <ElEmpty
      v-else-if="!loading && sortedSubs.length === 0"
      description="No schedule events yet."
    />

    <ElTable v-else-if="sortedSubs.length" :data="sortedSubs" stripe class="sched-table" row-key="id">
      <ElTableColumn type="expand">
        <template #default="{ row }">
          <EventRunHistory source-kind="schedule" :subscription-id="row.id" />
        </template>
      </ElTableColumn>
      <ElTableColumn prop="title" label="Title" min-width="160" />
      <ElTableColumn label="Schedule" min-width="180">
        <template #default="{ row }">
          <ElTag size="small" :type="row.rrule ? 'success' : 'info'" class="sched-tag">
            {{ scheduleLabel(row as ScheduleEventSubscription) }}
          </ElTag>
          <span class="muted">{{ (row as ScheduleEventSubscription).dtstart }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Action" min-width="220">
        <template #default="{ row }">
          <ElTooltip :content="row.action" placement="top">
            <span class="action-cell">{{ row.action }}</span>
          </ElTooltip>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Conversation" min-width="160">
        <template #default="{ row }">
          <a class="conv-link" @click.prevent="openConversation(row.conversation_id)">
            {{ row.conversation_title || '(unnamed)' }}
          </a>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Next run" min-width="170">
        <template #default="{ row }">
          <span class="muted">{{ nextRun(row as ScheduleEventSubscription) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Status" min-width="100">
        <template #default="{ row }">
          <ElTag :type="statusType(row.status)" size="small">{{ row.status }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Source" min-width="90">
        <template #default="{ row }">
          <span class="muted">{{ row.source }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Actions" min-width="250">
        <template #default="{ row }">
          <ElButton
            v-if="row.status === 'active' || row.status === 'paused'"
            size="small"
            @click="openEdit(row as ScheduleEventSubscription)"
          >
            <Icon icon="mdi:pencil-outline" /> Edit
          </ElButton>
          <ElButton
            v-if="row.status === 'active' || row.status === 'paused'"
            size="small"
            @click="togglePause(row as ScheduleEventSubscription)"
          >
            <Icon :icon="row.status === 'paused' ? 'mdi:play' : 'mdi:pause'" />
            {{ row.status === 'paused' ? 'Resume' : 'Pause' }}
          </ElButton>
          <ElButton size="small" type="danger" plain @click="confirmDelete(row as ScheduleEventSubscription)">
            <Icon icon="mdi:delete-outline" /> Delete
          </ElButton>
        </template>
      </ElTableColumn>
    </ElTable>

    <ScheduleEventDialog ref="scheduleDialog" />
    </CollapsibleSection>
  </section>
</template>

<style scoped>
.sched-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 24px;
  padding-top: 24px;
  border-top: 1px solid var(--border-color);
}

.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.section-header h2 {
  margin: 0;
  font-size: 1.125rem;
  color: var(--text-primary);
}

.section-icon {
  color: var(--primary-color);
  font-size: 1.25rem;
}

.section-blurb {
  margin: 0;
  color: var(--text-secondary);
  font-size: 0.875rem;
  line-height: 1.5;
}

.sched-table {
  width: 100%;
}

.action-cell {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  color: var(--text-primary);
}

.sched-tag {
  margin-right: 6px;
}

.conv-link {
  color: var(--primary-color);
  cursor: pointer;
  text-decoration: none;
}

.conv-link:hover {
  text-decoration: underline;
}

.muted {
  color: var(--text-tertiary);
  font-size: 0.8125rem;
}
</style>
