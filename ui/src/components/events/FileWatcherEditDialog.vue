<script setup lang="ts">
/**
 * Edit dialog for a file-watcher subscription. Extracted from FileWatcherSection
 * so both the Events table and the Tasks board can open it. Open via `open(row)`;
 * emits `saved` after a successful update (parents may ignore it — the admin SSE
 * refreshes the tables/board).
 */
import { ref } from 'vue';
import {
  ElButton,
  ElDialog,
  ElInput,
  ElMessage,
  ElOption,
  ElSelect,
  ElSwitch,
} from 'element-plus';
import { useSettingsStore } from '../../stores/settings';
import {
  updateFileWatcher,
  type FileWatcherSubscription,
  type FileWatcherUpdatePayload,
} from '../../services/fileWatchersApi';

const TRIGGER_OPTIONS = ['created', 'modified', 'deleted', 'moved'];
const TARGET_OPTIONS: Array<{ value: 'file' | 'folder' | 'any'; label: string }> = [
  { value: 'any', label: 'Any' },
  { value: 'file', label: 'Files' },
  { value: 'folder', label: 'Folders' },
];

const settings = useSettingsStore();
const emit = defineEmits<{ (e: 'saved'): void }>();

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

function open(row: FileWatcherSubscription) {
  editForm.value = {
    id: row.id,
    path: row.root_path,
    triggers: (row.event_types || '').split(',').map(s => s.trim()).filter(Boolean),
    target_kind: row.target_kind,
    extensions: (row.extensions || '').split(',').map(s => s.trim()).filter(Boolean),
    recursive: row.recursive,
    action: row.action,
  };
  editOpen.value = true;
}

async function submit() {
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
    emit('saved');
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  } finally {
    editBusy.value = false;
  }
}

defineExpose({ open });
</script>

<template>
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
      <ElButton type="primary" :loading="editBusy" @click="submit">Save</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
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
