<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import {
  ElForm, ElFormItem, ElInput, ElInputNumber, ElButton, ElCheckbox, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import type { ProviderWithState } from './ProviderConfigFields.vue';
import type { CustomProviderModel } from '../../services/configApi';

/**
 * Create/edit form for a user-defined OpenAI-API-compatible "custom provider":
 * a display name, an API Base URL, an API key, and a manually-entered model
 * list. Used inline on the LLM Providers page for both creating a new provider
 * and editing an existing one (base URL + models + key are editable; delete is
 * offered in edit mode). Emits `submit` with the assembled payload — the parent
 * owns the actual API call, validation messaging, and reload.
 *
 * Per model the user declares the capabilities the app can't infer for an
 * arbitrary endpoint: whether it accepts images (Vision), whether it accepts
 * audio (Audio), whether it supports Reasoning Effort (enables the effort
 * selector + skips the injected think-tool), and its per-1M-token prices
 * (drives cost tracking).
 */
const props = withDefaults(defineProps<{
  mode: 'create' | 'edit';
  provider?: ProviderWithState | null;
  saving?: boolean;
}>(), {
  provider: null,
  saving: false,
});

const emit = defineEmits<{
  (e: 'submit', payload: { display_name: string; base_url: string; api_key?: string; models: CustomProviderModel[] }): void;
  (e: 'delete'): void;
  (e: 'cancel'): void;
}>();

const displayName = ref('');
const baseUrl = ref('');
const apiKey = ref('');
const models = ref<CustomProviderModel[]>([]);

function emptyModel(): CustomProviderModel {
  return {
    id: '', display_name: '', vision: false, audio: false, supports_reasoning: false,
    input_price_per_1m: null, output_price_per_1m: null,
    cache_read_price_per_1m: null, cache_write_price_per_1m: null,
  };
}

/** (Re)load the form from the target provider (edit) or reset it (create). */
function loadFromProvider() {
  if (props.mode === 'edit' && props.provider) {
    displayName.value = props.provider.display_name || '';
    baseUrl.value = props.provider.base_url || '';
    apiKey.value = '';
    models.value = (props.provider.models || []).map((m: any) => ({
      id: m.id,
      display_name: m.display_name || m.id,
      vision: !!m.vision,
      audio: !!m.audio,
      // The models API surfaces reasoning_effort only for reasoning-capable
      // models, so its presence is the round-trip signal for the checkbox.
      supports_reasoning: !!(m.reasoning_effort && m.reasoning_effort.length),
      input_price_per_1m: m.input_price_per_1m ?? null,
      output_price_per_1m: m.output_price_per_1m ?? null,
      cache_read_price_per_1m: m.cache_read_price_per_1m ?? null,
      cache_write_price_per_1m: m.cache_write_price_per_1m ?? null,
    }));
  } else {
    displayName.value = '';
    baseUrl.value = '';
    apiKey.value = '';
    models.value = [];
  }
  if (models.value.length === 0) models.value = [emptyModel()];
}

// Reload when the target provider changes (switching between custom providers)
// or the mode flips.
watch(() => [props.mode, props.provider?.name], loadFromProvider, { immediate: true });

function addModel() {
  models.value.push(emptyModel());
}

function removeModel(index: number) {
  models.value.splice(index, 1);
  if (models.value.length === 0) models.value.push(emptyModel());
}

const keyConfigured = computed(() => props.mode === 'edit' && !!props.provider?.configured);

const canSubmit = computed(() =>
  displayName.value.trim().length > 0 &&
  baseUrl.value.trim().length > 0 &&
  models.value.some(m => m.id.trim().length > 0),
);

function submit() {
  const cleanModels = models.value
    .map(m => ({
      id: m.id.trim(),
      display_name: (m.display_name || m.id).trim(),
      vision: !!m.vision,
      audio: !!m.audio,
      supports_reasoning: !!m.supports_reasoning,
      input_price_per_1m: m.input_price_per_1m,
      output_price_per_1m: m.output_price_per_1m,
      cache_read_price_per_1m: m.cache_read_price_per_1m,
      cache_write_price_per_1m: m.cache_write_price_per_1m,
    }))
    .filter(m => m.id.length > 0);
  emit('submit', {
    display_name: displayName.value.trim(),
    base_url: baseUrl.value.trim(),
    api_key: apiKey.value ? apiKey.value : undefined,
    models: cleanModels,
  });
}
</script>

<template>
  <div class="custom-provider-form">
    <ElForm label-position="top">
      <ElFormItem label="Provider Name" required>
        <ElInput v-model="displayName" placeholder="e.g. My Company LLM" />
      </ElFormItem>

      <ElFormItem label="API Base URL" required>
        <ElInput v-model="baseUrl" placeholder="https://api.example.com/v1" />
        <div class="field-hint">Must be an OpenAI API-compatible endpoint (the base URL passed to the OpenAI client).</div>
      </ElFormItem>

      <ElFormItem>
        <template #label>
          API Key
          <ElTag v-if="keyConfigured" type="success" size="small" effect="light" round>Configured</ElTag>
        </template>
        <ElInput
          v-model="apiKey"
          type="password"
          show-password
          :placeholder="keyConfigured ? 'Leave blank to keep the current key' : 'Enter API key'"
        />
      </ElFormItem>

      <ElFormItem label="Models">
        <div class="field-hint">
          Add each model this provider exposes. Check Reasoning Effort only for models with native
          step-by-step reasoning; leave a price blank to skip cost tracking for that component.
        </div>
        <div class="models-editor">
          <div v-for="(m, i) in models" :key="i" class="model-card">
            <div class="model-card-head">
              <span class="model-card-title">Model {{ i + 1 }}</span>
              <ElButton link type="danger" title="Remove model" @click="removeModel(i)">
                <Icon icon="mdi:close" />
              </ElButton>
            </div>
            <div class="model-grid">
              <label class="fld">
                <span class="fld-label">Model ID</span>
                <ElInput v-model="m.id" placeholder="model-id" size="small" />
              </label>
              <label class="fld">
                <span class="fld-label">Display Name</span>
                <ElInput v-model="m.display_name" placeholder="Display name" size="small" />
              </label>
              <label class="fld fld-check">
                <span class="fld-label">Vision</span>
                <ElCheckbox v-model="m.vision">Accepts images</ElCheckbox>
              </label>
              <label class="fld fld-check">
                <span class="fld-label">Audio</span>
                <ElCheckbox v-model="m.audio">Accepts audio</ElCheckbox>
              </label>
              <label class="fld fld-check">
                <span class="fld-label">Reasoning Effort</span>
                <ElCheckbox v-model="m.supports_reasoning">Supported</ElCheckbox>
              </label>
              <label class="fld">
                <span class="fld-label">Input $ / 1M tokens</span>
                <ElInputNumber v-model="m.input_price_per_1m" :min="0" :step="0.1" :controls="false" size="small" placeholder="—" />
              </label>
              <label class="fld">
                <span class="fld-label">Output $ / 1M tokens</span>
                <ElInputNumber v-model="m.output_price_per_1m" :min="0" :step="0.1" :controls="false" size="small" placeholder="—" />
              </label>
              <label class="fld">
                <span class="fld-label">Cache Read $ / 1M tokens</span>
                <ElInputNumber v-model="m.cache_read_price_per_1m" :min="0" :step="0.1" :controls="false" size="small" placeholder="—" />
              </label>
              <label class="fld">
                <span class="fld-label">Cache Write $ / 1M tokens</span>
                <ElInputNumber v-model="m.cache_write_price_per_1m" :min="0" :step="0.1" :controls="false" size="small" placeholder="—" />
              </label>
            </div>
          </div>
        </div>
        <ElButton link type="primary" class="add-model-btn" @click="addModel">
          <Icon icon="mdi:plus" /> Add model
        </ElButton>
      </ElFormItem>

      <div class="form-actions">
        <ElButton type="primary" :loading="saving" :disabled="!canSubmit" @click="submit">
          {{ mode === 'create' ? 'Create Provider' : 'Save Changes' }}
        </ElButton>
        <ElButton v-if="mode === 'create'" :disabled="saving" @click="emit('cancel')">Cancel</ElButton>
        <ElButton v-if="mode === 'edit'" type="danger" plain :disabled="saving" @click="emit('delete')">
          Delete Provider
        </ElButton>
      </div>
    </ElForm>
  </div>
</template>

<style scoped>
.custom-provider-form {
  margin-top: 8px;
}
.field-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.4;
  margin-bottom: 8px;
}
.models-editor {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.model-card {
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  padding: 10px 12px;
}
.model-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.model-card-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}
.model-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 12px;
}
.fld {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.fld-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.fld :deep(.el-input-number) {
  width: 100%;
}
.fld-check {
  justify-content: flex-end;
}
.add-model-btn {
  margin-top: 10px;
}
.form-actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}
</style>
