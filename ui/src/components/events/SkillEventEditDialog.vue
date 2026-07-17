<script setup lang="ts">
/**
 * Edit dialog for a skill-event subscription. Extracted from SkillEventsPage so
 * both the Events table and the Tasks board can open it. Open via `open(row)`;
 * emits `saved` after a successful update (parents may ignore it — the admin SSE
 * refreshes the tables/board).
 */
import { computed, ref } from 'vue';
import { ElButton, ElDialog, ElInput, ElMessage, ElOption, ElSelect } from 'element-plus';
import { useSettingsStore } from '../../stores/settings';
import {
  getSkillEvents,
  updateSubscription,
  type SkillEventDeclaration,
  type SkillEventSubscription,
} from '../../services/skillEventsApi';

const settings = useSettingsStore();
const emit = defineEmits<{ (e: 'saved'): void }>();

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

async function open(row: SkillEventSubscription) {
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

async function submit() {
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
      <ElButton type="primary" :loading="editBusy" @click="submit">Save</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
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
</style>
