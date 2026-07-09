<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton,
  ElDialog,
  ElEmpty,
  ElInput,
  ElMessage,
  ElMessageBox,
  ElOption,
  ElSelect,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
  ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import {
  deleteFileWatcher,
  updateFileWatcher,
  type FileWatcherSubscription,
  type FileWatcherUpdatePayload,
} from '../services/fileWatchersApi';

const TRIGGER_OPTIONS = ['created', 'modified', 'deleted', 'moved'];
const TARGET_OPTIONS: Array<{ value: 'file' | 'folder' | 'any'; label: string }> = [
  { value: 'any', label: 'Any' },
  { value: 'file', label: 'Files' },
  { value: 'folder', label: 'Folders' },
];
import {
  subscribeFileWatchersAdmin,
  type AdminEventsSubHandle,
} from '../services/adminEventsStream';
import CollapsibleSection from './CollapsibleSection.vue';
import EventRunHistory from './events/EventRunHistory.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settings = useSettingsStore();

const subscriptions = ref<FileWatcherSubscription[]>([]);
const loading = ref(false);
const errorMessage = ref('');

const sortedSubs = computed(() =>
  [...subscriptions.value].sort((a, b) => b.created_at - a.created_at),
);

let streamHandle: AdminEventsSubHandle | null = null;

function streamStart() {
  streamStop();
  if (!settings.agentUrl || !settings.authToken) return;
  loading.value = true;
  errorMessage.value = '';
  streamHandle = subscribeFileWatchersAdmin(
    settings.agentUrl,
    settings.authToken,
    (snap) => {
      subscriptions.value = snap.subscriptions;
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

onMounted(() => {
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

function openConversation(id: string) {
  router.push({
    name: 'conversation',
    params: { profile: props.profile, conversationId: id },
  });
}

async function confirmDelete(row: FileWatcherSubscription) {
  try {
    await ElMessageBox.confirm(
      `Delete file watcher '${row.name}' on ${row.root_path}?`,
      'Confirm delete',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await deleteFileWatcher(settings.agentUrl, settings.authToken, row.id);
    ElMessage.success('File watcher deleted');
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

function triggersOf(row: FileWatcherSubscription): string[] {
  return (row.event_types || '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);
}

// ── edit dialog ────────────────────────────────────────────────────────────
const editOpen = ref(false);
const editBusy = ref(false);
const editForm = ref({
  id: '',
  path: '',
  triggers: [] as string[],
  target_kind: 'any' as 'file' | 'folder' | 'any',
  extensions: [] as string[],
  recursive: true,
  action: '',
});

function openEdit(row: FileWatcherSubscription) {
  editForm.value = {
    id: row.id,
    path: row.root_path,
    triggers: triggersOf(row),
    target_kind: row.target_kind,
    extensions: (row.extensions || '').split(',').map(s => s.trim()).filter(Boolean),
    recursive: row.recursive,
    action: row.action,
  };
  editOpen.value = true;
}

async function submitEdit() {
  if (!editForm.value.action.trim()) { ElMessage.warning('Action is required'); return; }
  if (!editForm.value.triggers.length) { ElMessage.warning('At least one trigger is required'); return; }
  const payload: FileWatcherUpdatePayload = {
    path: editForm.value.path.trim(),
    triggers: editForm.value.triggers,
    target_kind: editForm.value.target_kind,
    extensions: editForm.value.extensions,
    recursive: editForm.value.recursive,
    action: editForm.value.action.trim(),
  };
  editBusy.value = true;
  try {
    await updateFileWatcher(settings.agentUrl, settings.authToken, editForm.value.id, payload);
    ElMessage.success('File watcher updated');
    editOpen.value = false;
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  } finally {
    editBusy.value = false;
  }
}

function extensionsLabel(row: FileWatcherSubscription): string {
  const exts = (row.extensions || '').trim();
  return exts || 'all';
}

function targetLabel(row: FileWatcherSubscription): string {
  if (row.target_kind === 'file') return 'files';
  if (row.target_kind === 'folder') return 'folders';
  return 'any';
}

function formatDate(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}
</script>

<template>
  <section class="fw-section">
    <CollapsibleSection title="File Watcher Events" icon="mdi:folder-eye-outline" :count="sortedSubs.length">
    <p class="section-blurb">
      Watch a directory for filesystem changes and run an action whenever a
      matching event fires (created, modified, deleted, moved). Subscriptions
      are made by the assistant when you ask for a watch
      (e.g. "when a python file changes in the 'Lee' directory, notify me").
    </p>

    <p v-if="errorMessage" class="error-banner">{{ errorMessage }}</p>

    <ElEmpty
      v-if="!loading && sortedSubs.length === 0"
      description="No active file watchers."
    />

    <ElTable v-else :data="sortedSubs" stripe class="fw-table" row-key="id">
      <ElTableColumn type="expand">
        <template #default="{ row }">
          <EventRunHistory source-kind="file_watcher" :subscription-id="row.id" />
        </template>
      </ElTableColumn>
      <ElTableColumn prop="name" label="Name" min-width="140" />
      <ElTableColumn label="Path" min-width="240">
        <template #default="{ row }">
          <ElTooltip :content="row.root_path" placement="top">
            <span class="path-cell">{{ row.root_path }}</span>
          </ElTooltip>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Triggers" min-width="200">
        <template #default="{ row }">
          <ElTag
            v-for="t in triggersOf(row as FileWatcherSubscription)"
            :key="t"
            size="small"
            class="trigger-tag"
          >
            {{ t }}
          </ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Target" min-width="100">
        <template #default="{ row }">
          {{ targetLabel(row as FileWatcherSubscription) }}
        </template>
      </ElTableColumn>
      <ElTableColumn label="Extensions" min-width="140">
        <template #default="{ row }">
          <span class="muted">{{ extensionsLabel(row as FileWatcherSubscription) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Recursive" min-width="100">
        <template #default="{ row }">
          <ElTag :type="row.recursive ? 'success' : 'info'" size="small">
            {{ row.recursive ? 'yes' : 'no' }}
          </ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Action" min-width="220">
        <template #default="{ row }">
          <ElTooltip :content="row.action" placement="top">
            <span class="action-cell">{{ row.action }}</span>
          </ElTooltip>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Conversation" min-width="180">
        <template #default="{ row }">
          <a
            class="conv-link"
            @click.prevent="openConversation(row.conversation_id)"
          >
            {{ row.conversation_title || '(unnamed)' }}
          </a>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Status" min-width="100">
        <template #default="{ row }">
          <ElTag :type="row.armed ? 'success' : 'warning'" size="small">
            {{ row.armed ? 'armed' : 'unarmed' }}
          </ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Created" min-width="140">
        <template #default="{ row }">
          <span class="muted">{{ formatDate(row.created_at) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Actions" min-width="180">
        <template #default="{ row }">
          <ElButton size="small" @click="openEdit(row as FileWatcherSubscription)">
            <Icon icon="mdi:pencil-outline" /> Edit
          </ElButton>
          <ElButton size="small" type="danger" plain @click="confirmDelete(row as FileWatcherSubscription)">
            <Icon icon="mdi:delete-outline" /> Delete
          </ElButton>
        </template>
      </ElTableColumn>
    </ElTable>

    <ElDialog v-model="editOpen" title="Edit file watcher" width="560px">
      <div class="fw-form">
        <label class="f-label">Path</label>
        <ElInput v-model="editForm.path" placeholder="Absolute path or relative to the working directory" />

        <label class="f-label">Triggers</label>
        <ElSelect
          v-model="editForm.triggers"
          multiple
          placeholder="Select filesystem events"
          style="width:100%"
        >
          <ElOption v-for="t in TRIGGER_OPTIONS" :key="t" :label="t" :value="t" />
        </ElSelect>

        <div class="f-row">
          <div class="f-col">
            <label class="f-label">Target</label>
            <ElSelect v-model="editForm.target_kind" style="width:100%">
              <ElOption v-for="o in TARGET_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
            </ElSelect>
          </div>
          <div class="f-col f-toggle-col">
            <label class="f-label">Recursive</label>
            <ElSwitch v-model="editForm.recursive" />
          </div>
        </div>

        <label class="f-label">Extensions</label>
        <ElSelect
          v-model="editForm.extensions"
          multiple
          filterable
          allow-create
          default-first-option
          :reserve-keyword="false"
          placeholder="Leave empty for all files — e.g. .py, .md"
          style="width:100%"
        >
          <ElOption v-for="e in editForm.extensions" :key="e" :label="e" :value="e" />
        </ElSelect>

        <label class="f-label">Action</label>
        <ElInput
          v-model="editForm.action"
          type="textarea"
          :rows="3"
          placeholder="Natural-language instruction the assistant runs when the watcher fires."
        />
      </div>
      <template #footer>
        <ElButton @click="editOpen = false">Cancel</ElButton>
        <ElButton type="primary" :loading="editBusy" @click="submitEdit">Save</ElButton>
      </template>
    </ElDialog>
    </CollapsibleSection>
  </section>
</template>

<style scoped>
.fw-section {
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

.error-banner {
  background: rgba(231, 76, 60, 0.12);
  color: var(--error-color, #e74c3c);
  padding: 8px 12px;
  border-radius: 6px;
  margin: 0;
  font-size: 0.875rem;
}

.fw-table {
  width: 100%;
}

.path-cell {
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-family: var(--font-mono, monospace);
  font-size: 0.8125rem;
  color: var(--text-primary);
}

.action-cell {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  color: var(--text-primary);
}

.trigger-tag {
  margin-right: 4px;
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

.fw-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.fw-form .f-label {
  font-size: 0.8125rem;
  color: var(--text-secondary);
}

.fw-form .f-row {
  display: flex;
  gap: 12px;
}

.fw-form .f-col {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.fw-form .f-toggle-col {
  flex: 0 0 auto;
}
</style>
