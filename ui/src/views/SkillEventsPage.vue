<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import {
  ElBadge,
  ElButton,
  ElDialog,
  ElEmpty,
  ElInput,
  ElMessage,
  ElMessageBox,
  ElOption,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag,
  ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { useChatStore } from '../stores/chat';
import { useEventRunsStore } from '../stores/eventRuns';
import {
  deleteSubscription,
  getSkillEvents,
  simulateEvent,
  startListener,
  updateSubscription,
  type ListenerStatus,
  type SkillEventDeclaration,
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
import TasksBoard from '../components/events/tasks/TasksBoard.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const route = useRoute();
const settings = useSettingsStore();
const chatStore = useChatStore();
const eventRuns = useEventRunsStore();

const subscriptions = ref<SkillEventSubscription[]>([]);
const listenerByName = ref<Record<string, ListenerStatus>>({});
const loading = ref(false);
const errorMessage = ref('');

const simulateOpen = ref(false);
const simulateTarget = ref<SkillEventSubscription | null>(null);
const simulateFilename = ref('');
const simulateContent = ref('');

// ── edit dialog ────────────────────────────────────────────────────────────
const editOpen = ref(false);
const editBusy = ref(false);
const editTarget = ref<SkillEventSubscription | null>(null);
const editEventType = ref('');
const editAction = ref('');
const editTriggerOptions = ref<SkillEventDeclaration[]>([]);

// Always include the current trigger, even if the skill no longer declares it
// or the discovery call fails, so the dropdown never loses the saved value.
const editTriggerNames = computed(() => {
  const names = editTriggerOptions.value.map(e => e.name).filter(Boolean);
  if (editEventType.value && !names.includes(editEventType.value)) {
    names.unshift(editEventType.value);
  }
  return names;
});

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
  simulateTarget.value = row;
  simulateFilename.value = '';
  simulateContent.value = '';
  simulateOpen.value = true;
}

async function openEditSkill(row: SkillEventSubscription) {
  editTarget.value = row;
  editEventType.value = row.event_type;
  editAction.value = row.action;
  editTriggerOptions.value = [{ name: row.event_type }];
  editOpen.value = true;
  try {
    const info = await getSkillEvents(settings.agentUrl, settings.authToken, row.skill_name);
    if (info.events && info.events.length) editTriggerOptions.value = info.events;
  } catch {
    // Discovery failed — keep the current trigger as the only option.
  }
}

async function submitEditSkill() {
  if (!editTarget.value) return;
  if (!editEventType.value.trim()) { ElMessage.warning('Trigger is required'); return; }
  if (!editAction.value.trim()) { ElMessage.warning('Action is required'); return; }
  editBusy.value = true;
  try {
    await updateSubscription(settings.agentUrl, settings.authToken, editTarget.value.id, {
      event_type: editEventType.value,
      action: editAction.value.trim(),
    });
    ElMessage.success('Subscription updated');
    editOpen.value = false;
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  } finally {
    editBusy.value = false;
  }
}

async function fireSimulate() {
  if (!simulateTarget.value) return;
  if (!simulateContent.value.trim()) {
    ElMessage.warning('Content is required.');
    return;
  }
  // Open an SSE subscription to the target conversation BEFORE we fire
  // so the agent run streams into its bucket in real time even though we
  // are not currently viewing the conversation. The 'streaming' tracker
  // is auto-removed by the chat store when the run emits 'complete' or
  // 'error', so we don't have to clean up here.
  const targetCid = simulateTarget.value.conversation_id;
  if (targetCid) {
    chatStore.trackConversation(targetCid, 'streaming');
  }
  try {
    const result = await simulateEvent(
      settings.agentUrl,
      settings.authToken,
      simulateTarget.value.id,
      simulateContent.value,
      simulateFilename.value,
    );
    ElMessage.success(`Event fired — wrote ${result.path}`);
    simulateOpen.value = false;
  } catch (err) {
    if (targetCid) {
      chatStore.untrackConversation(targetCid, 'streaming');
    }
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
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

    <ElDialog v-model="editOpen" title="Edit skill event" width="560px">
      <p class="dialog-info" v-if="editTarget">
        Editing the subscription for <strong>{{ editTarget.skill_name }}</strong>.
      </p>
      <div class="sim-field">
        <label class="sim-label">Trigger</label>
        <ElSelect v-model="editEventType" placeholder="Select a trigger" style="width:100%">
          <ElOption v-for="name in editTriggerNames" :key="name" :label="name" :value="name" />
        </ElSelect>
      </div>
      <div class="sim-field">
        <label class="sim-label">Action</label>
        <ElInput
          v-model="editAction"
          type="textarea"
          :rows="6"
          placeholder="Natural-language instruction the assistant runs when the event fires."
        />
      </div>
      <template #footer>
        <ElButton @click="editOpen = false">Cancel</ElButton>
        <ElButton type="primary" :loading="editBusy" @click="submitEditSkill">Save</ElButton>
      </template>
    </ElDialog>
    </CollapsibleSection>

    <ElDialog v-model="simulateOpen" title="Simulate event" width="640px">
      <p class="dialog-info" v-if="simulateTarget">
        Fires <strong>{{ simulateTarget.event_type }}</strong> for
        <strong>{{ simulateTarget.skill_name }}</strong>.
        The file is written into the watched events folder; the watchdog picks
        it up just like a real event and deletes it after dispatch.
      </p>
      <div class="sim-field">
        <label class="sim-label">File name</label>
        <ElInput
          v-model="simulateFilename"
          placeholder="optional — e.g. my-test.md (auto-named if blank)"
        />
        <p class="sim-hint">
          Path components are stripped. <code>.md</code> is appended if missing.
        </p>
      </div>
      <div class="sim-field">
        <label class="sim-label">File content</label>
        <ElInput
          v-model="simulateContent"
          type="textarea"
          :rows="14"
          placeholder="The exact bytes that will be written to the .md file. Format depends on the skill — e.g. an imap-email event uses YAML frontmatter + markdown body."
        />
      </div>
      <template #footer>
        <ElButton @click="simulateOpen = false">Cancel</ElButton>
        <ElButton type="primary" @click="fireSimulate">Fire</ElButton>
      </template>
    </ElDialog>

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

.dialog-info {
  margin: 0 0 12px 0;
  color: var(--text-secondary);
  font-size: 0.875rem;
  line-height: 1.5;
}

.sim-field {
  margin-bottom: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.sim-label {
  font-size: 0.8125rem;
  color: var(--text-secondary);
  font-weight: 500;
}

.sim-hint {
  margin: 0;
  font-size: 0.75rem;
  color: var(--text-tertiary);
}

.sim-hint code {
  background: var(--surface-color);
  padding: 1px 4px;
  border-radius: 3px;
}
</style>
