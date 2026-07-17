<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import {
  ElBadge,
  ElButton,
  ElEmpty,
  ElMessage,
  ElMessageBox,
  ElRadioButton,
  ElRadioGroup,
  ElTable,
  ElTableColumn,
  ElTag,
  ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { useEventRunsStore } from '../stores/eventRuns';
import {
  deleteSubscription,
  startListener,
  type ListenerStatus,
  type SkillEventSubscription,
} from '../services/skillEventsApi';
import {
  subscribeSkillEventsAdmin,
  subscribeEventRunsAdmin,
  type AdminEventsSubHandle,
} from '../services/adminEventsStream';
import FileWatcherSection from '../components/FileWatcherSection.vue';
import ScheduleEventsSection from '../components/ScheduleEventsSection.vue';
import CollapsibleSection from '../components/CollapsibleSection.vue';
import EventRunHistory from '../components/events/EventRunHistory.vue';
import EventRunDetailDrawer from '../components/events/EventRunDetailDrawer.vue';
import SkillEventEditDialog from '../components/events/SkillEventEditDialog.vue';
import SkillEventSimulateDialog from '../components/events/SkillEventSimulateDialog.vue';
import TasksBoard from '../components/events/tasks/TasksBoard.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const route = useRoute();
const settings = useSettingsStore();
const eventRuns = useEventRunsStore();

const subscriptions = ref<SkillEventSubscription[]>([]);
const listenerByName = ref<Record<string, ListenerStatus>>({});
const loading = ref(false);
const errorMessage = ref('');

const skillEditDialog = ref<InstanceType<typeof SkillEventEditDialog> | null>(null);
const skillSimulateDialog = ref<InstanceType<typeof SkillEventSimulateDialog> | null>(null);

const sortedSubs = computed(() =>
  [...subscriptions.value].sort((a, b) => b.created_at - a.created_at),
);

let streamHandle: AdminEventsSubHandle | null = null;
let runsStreamHandle: AdminEventsSubHandle | null = null;

function streamStart() {
  streamStop();
  if (!settings.agentUrl || !settings.authToken) return;
  loading.value = true;
  errorMessage.value = '';
  streamHandle = subscribeSkillEventsAdmin(
    settings.agentUrl,
    settings.authToken,
    (snap) => {
      subscriptions.value = snap.subscriptions;
      listenerByName.value = snap.listeners;
      loading.value = false;
      errorMessage.value = '';
    },
  );
  // Feed the shared event-runs store for all three sections' child tables.
  runsStreamHandle = subscribeEventRunsAdmin(
    settings.agentUrl,
    settings.authToken,
    (snap) => eventRuns.applySnapshot(snap.runs, snap.summaries),
  );
}

function streamStop() {
  if (streamHandle) {
    streamHandle.close();
    streamHandle = null;
  }
  if (runsStreamHandle) {
    runsStreamHandle.close();
    runsStreamHandle = null;
  }
}

// Deep link: /:profile/events?run=<id> opens the run detail drawer.
watch(
  () => route.query.run,
  (runId) => {
    if (typeof runId === 'string' && runId) {
      eventRuns.openRunById(runId);
    }
  },
  { immediate: true },
);

// Keep the URL in sync with the open drawer (so it's shareable / back-navigable).
watch(
  () => eventRuns.activeRunId,
  (id) => {
    const current = typeof route.query.run === 'string' ? route.query.run : undefined;
    if (id && current !== id) {
      router.replace({ query: { ...route.query, run: id } });
    } else if (!id && current) {
      const q = { ...route.query };
      delete q.run;
      router.replace({ query: q });
    }
  },
);

onMounted(() => {
  if (!settings.authToken && props.profile) {
    settings.activateProfile(props.profile);
  }
  streamStart();
});

watch(
  () => settings.authToken,
  (token, prev) => {
    if (token && !prev) {
      streamStart();
    }
  },
);

onBeforeUnmount(() => {
  streamStop();
});

function goBack() {
  goBackToChat(router, props.profile);
}

function openConversation(id: string) {
  router.push({ name: 'conversation', params: { profile: props.profile, conversationId: id } });
}

async function confirmDelete(row: SkillEventSubscription) {
  try {
    await ElMessageBox.confirm(
      `Delete subscription for ${row.skill_name} → ${row.event_type}?`,
      'Confirm delete',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await deleteSubscription(settings.agentUrl, settings.authToken, row.id);
    ElMessage.success('Subscription deleted');
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

function openSimulate(row: SkillEventSubscription) {
  skillSimulateDialog.value?.open(row);
}

function openEditSkill(row: SkillEventSubscription) {
  skillEditDialog.value?.open(row);
}

async function startListenerFor(skillName: string) {
  try {
    await startListener(settings.agentUrl, settings.authToken, skillName);
    ElMessage.success(`${skillName} listener started`);
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

function listenerLabel(skillName: string): { running: boolean; text: string } {
  const status = listenerByName.value[skillName];
  if (!status) return { running: false, text: 'unknown' };
  if (status.running) return { running: true, text: 'running' };
  if (status.last_heartbeat) {
    const ageMin = Math.round((Date.now() / 1000 - status.last_heartbeat) / 60);
    return { running: false, text: `down (last beat ${ageMin}m ago)` };
  }
  return { running: false, text: 'never started' };
}

// NB: skill-event subscription `created_at` is epoch SECONDS (unlike EventRun
// timestamps, which are ms), hence the ×1000. Param name kept for the callers.
function formatDate(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}

function onViewChange(mode: string | number | boolean | undefined) {
  settings.setEventsViewMode(mode === 'tasks' ? 'tasks' : 'events');
}
</script>

<template>
  <div class="page">
    <header class="page-header">
      <button class="icon-button" @click="goBack" title="Back">
        <Icon icon="mdi:arrow-left" />
      </button>
      <h2>Events</h2>
      <ElRadioGroup
        :model-value="settings.eventsViewMode"
        size="small"
        @change="(v: any) => onViewChange(v)"
      >
        <ElRadioButton value="events">
          <Icon icon="mdi:table" /> Events
        </ElRadioButton>
        <ElRadioButton value="tasks">
          <Icon icon="mdi:view-column-outline" /> Tasks
        </ElRadioButton>
      </ElRadioGroup>
    </header>

    <p v-if="errorMessage" class="error-banner">{{ errorMessage }}</p>

    <TasksBoard v-if="settings.eventsViewMode === 'tasks'" :profile="profile" />

    <template v-else>
    <CollapsibleSection title="Skill Events" icon="mdi:lightning-bolt-outline" :count="sortedSubs.length">
    <p class="page-blurb">
      Each subscription re-runs its conversation with the saved <em>action</em> whenever a new
      event file appears in the skill's <code>events/&lt;event_type&gt;/</code> folder.
      Subscriptions are made by the assistant when you ask for an automation
      (e.g. "when a new email arrives, summarize it").
    </p>

    <ElEmpty v-if="!loading && sortedSubs.length === 0" description="No active subscriptions." />

    <ElTable v-else :data="sortedSubs" stripe class="subs-table" row-key="id">
      <ElTableColumn type="expand">
        <template #default="{ row }">
          <EventRunHistory source-kind="skill_event" :subscription-id="row.id" />
        </template>
      </ElTableColumn>
      <ElTableColumn prop="skill_name" label="Skill" min-width="120" />
      <ElTableColumn label="Trigger" min-width="120">
        <template #default="{ row }">
          {{ row.event_type }}
          <ElBadge
            v-if="eventRuns.pendingCountForSubscription('skill_event', row.id) > 0"
            :value="eventRuns.pendingCountForSubscription('skill_event', row.id)"
            type="warning"
            class="pending-badge"
          />
        </template>
      </ElTableColumn>
      <ElTableColumn label="Action" min-width="260">
        <template #default="{ row }">
          <ElTooltip :content="row.action" placement="top">
            <span class="action-cell">{{ row.action }}</span>
          </ElTooltip>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Conversation" min-width="180">
        <template #default="{ row }">
          <a class="conv-link" @click.prevent="openConversation(row.conversation_id)">
            {{ row.conversation_title || '(unnamed)' }}
          </a>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Listener" min-width="180">
        <template #default="{ row }">
          <ElTag :type="listenerLabel(row.skill_name).running ? 'success' : 'warning'" size="small">
            {{ listenerLabel(row.skill_name).text }}
          </ElTag>
          <ElButton
            v-if="!listenerLabel(row.skill_name).running"
            link size="small"
            @click="startListenerFor(row.skill_name)"
          >
            Start
          </ElButton>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Created" min-width="140">
        <template #default="{ row }">
          <span class="muted">{{ formatDate(row.created_at) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Actions" min-width="250">
        <template #default="{ row }">
          <ElButton size="small" @click="openEditSkill(row as SkillEventSubscription)">
            <Icon icon="mdi:pencil-outline" /> Edit
          </ElButton>
          <ElButton size="small" @click="openSimulate(row as SkillEventSubscription)">
            <Icon icon="mdi:flask-outline" /> Simulate
          </ElButton>
          <ElButton size="small" type="danger" plain @click="confirmDelete(row as SkillEventSubscription)">
            <Icon icon="mdi:delete-outline" /> Delete
          </ElButton>
        </template>
      </ElTableColumn>
    </ElTable>

    <SkillEventEditDialog ref="skillEditDialog" />
    </CollapsibleSection>

    <SkillEventSimulateDialog ref="skillSimulateDialog" />

    <FileWatcherSection :profile="profile" />

    <ScheduleEventsSection :profile="profile" />
    </template>

    <!-- Run-detail drawer (hosted once; opened from any section or the board). -->
    <EventRunDetailDrawer />
  </div>
</template>

<style scoped>
.page {
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: 100%;
  overflow-y: auto;
}

.page-header {
  display: flex;
  align-items: center;
  gap: 12px;
}

.page-header h2 {
  margin: 0;
  flex: 1;
  font-size: 1.25rem;
  color: var(--text-primary);
}

.icon-button {
  width: 32px;
  height: 32px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  transition: background 0.2s ease;
}

.icon-button:hover {
  background: var(--hover-bg);
  color: var(--primary-color);
}

.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.section-header .section-title {
  margin: 0;
  font-size: 1.125rem;
  color: var(--text-primary);
}

.section-icon {
  color: var(--primary-color);
  font-size: 1.25rem;
}

.page-blurb {
  margin: 0;
  color: var(--text-secondary);
  font-size: 0.875rem;
  line-height: 1.5;
}

.page-blurb code {
  background: var(--surface-color);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 0.85em;
}

.error-banner {
  background: rgba(231, 76, 60, 0.12);
  color: var(--error-color, #e74c3c);
  padding: 8px 12px;
  border-radius: 6px;
  margin: 0;
  font-size: 0.875rem;
}

.subs-table {
  width: 100%;
}

.action-cell {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  color: var(--text-primary);
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
