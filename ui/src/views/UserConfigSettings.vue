<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { useRouter } from 'vue-router';
import { ElButton, ElMessage, ElMessageBox } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../stores/settings';
import {
  getUserConfigSchema,
  getUserConfig,
  updateUserConfig,
  resetUserConfigKey,
  type UserConfigSchema,
} from '../services/configApi';
import ConfigGroupCard from '../components/config/ConfigGroupCard.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

const schema = ref<UserConfigSchema | null>(null);
const values = ref<Record<string, unknown>>({});
const defaults = ref<Record<string, unknown>>({});
/** Pending edits (full key → new value). Cleared after a successful save. */
const dirty = ref<Record<string, unknown>>({});
const loading = ref(false);
const saving = ref(false);

const hasChanges = computed(() => Object.keys(dirty.value).length > 0);

async function loadAll() {
  loading.value = true;
  try {
    const [schemaRes, valuesRes] = await Promise.all([
      getUserConfigSchema(settingsStore.agentUrl, settingsStore.authToken),
      getUserConfig(settingsStore.agentUrl, settingsStore.authToken),
    ]);
    schema.value = schemaRes;
    values.value = valuesRes.values;
    defaults.value = valuesRes.defaults;
    dirty.value = {};
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to load configuration');
  } finally {
    loading.value = false;
  }
}

function handleUpdate(key: string, value: unknown) {
  // Track the change locally so users can review and Save in one batch.
  dirty.value = { ...dirty.value, [key]: value };
  values.value = { ...values.value, [key]: value };
}

async function handleReset(key: string) {
  try {
    await ElMessageBox.confirm(
      `Reset "${key}" to its default value?`,
      'Reset value',
      { type: 'warning', confirmButtonText: 'Reset', cancelButtonText: 'Cancel' },
    );
  } catch {
    return;
  }
  try {
    await resetUserConfigKey(
      settingsStore.agentUrl,
      settingsStore.authToken,
      key,
    );
    // Clear local override and any pending edit for this key.
    const next = { ...dirty.value };
    delete next[key];
    dirty.value = next;
    values.value = { ...values.value, [key]: null };
    ElMessage.success('Reset to default');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to reset value');
  }
}

async function handleSave() {
  if (!hasChanges.value) return;
  saving.value = true;
  try {
    await updateUserConfig(
      settingsStore.agentUrl,
      settingsStore.authToken,
      dirty.value,
    );
    ElMessage.success('Configuration saved');
    dirty.value = {};
    await loadAll();
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to save configuration');
  } finally {
    saving.value = false;
  }
}

function goBack() {
  router.push(`/${props.profile}/settings`);
}

onMounted(loadAll);
</script>

<template>
  <div class="config-page">
    <div class="config-container">
      <div class="config-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Settings
        </button>
        <div class="header-row">
          <div>
            <h1 class="config-title">Config</h1>
            <p class="config-subtitle">
              Per-profile runtime tuning · Profile <strong>{{ profile }}</strong>
            </p>
          </div>
          <ElButton
            type="primary"
            size="default"
            :disabled="!hasChanges"
            :loading="saving"
            @click="handleSave"
          >
            <Icon icon="mdi:content-save" style="margin-right: 6px" />
            Save changes
          </ElButton>
        </div>
      </div>

      <div v-if="loading" class="loading">Loading…</div>
      <div v-else-if="schema">
        <ConfigGroupCard
          v-for="(group, groupKey) in schema.groups"
          :key="groupKey"
          :group-key="groupKey"
          :group="group"
          :values="values"
          :defaults="defaults"
          @update:value="handleUpdate"
          @reset="handleReset"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.config-page {
  width: 100%;
  height: 100%;
  overflow-y: auto;
  background: var(--bg-color);
  padding: 24px;
  box-sizing: border-box;
}
.config-container { max-width: 880px; margin: 0 auto; }
.config-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.header-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.config-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.config-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.loading {
  display: flex; align-items: center; justify-content: center;
  padding: 60px 0; color: var(--text-secondary);
}
</style>
