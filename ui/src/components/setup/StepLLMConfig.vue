<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import { ElForm, ElFormItem, ElSelect, ElOption, ElSwitch } from 'element-plus';
import { listLLMProviders, getProviderModels } from '../../services/configApi';
import { useLLMModels } from '../../composables/useLLMModels';
import ProviderConfigFields, { type ProviderWithState } from '../shared/ProviderConfigFields.vue';
import ModelGroupFields from '../shared/ModelGroupFields.vue';

const props = defineProps<{
  agentUrl: string;
  // Admin JWT for loading the provider/model catalog during a per-profile
  // setup (empty on first-run setup, where the endpoints are open).
  token?: string;
  config: Record<string, string>;
}>();

const emit = defineEmits<{
  update: [config: Record<string, string>];
}>();

const { rebuildModelList, allModels } = useLLMModels();

const providers = ref<ProviderWithState[]>([]);
// Main reasoning model (``high``), plus optional Low-Performance (``low``),
// Vision (``vision``), and Audio (``audio``) groups — mirrors Settings → LLM
// Providers.
const highProvider = ref(extractProvider(props.config['model_group.high']) || props.config.default_provider || 'groq');
const modelGroupHigh = ref(props.config['model_group.high'] || '');
const reasoningEffortHigh = ref<string | null>(props.config['model_group.high.reasoning_effort'] || null);
const lowProvider = ref(extractProvider(props.config['model_group.low']) || '');
const modelGroupLow = ref(props.config['model_group.low'] || '');
const reasoningEffortLow = ref<string | null>(props.config['model_group.low.reasoning_effort'] || null);
const visionProvider = ref(extractProvider(props.config['model_group.vision']) || '');
const modelGroupVision = ref(props.config['model_group.vision'] || '');
const visionEnabled = ref(props.config['model_group.vision.enabled'] === 'true');
const audioProvider = ref(extractProvider(props.config['model_group.audio']) || '');
const modelGroupAudio = ref(props.config['model_group.audio'] || '');
const audioEnabled = ref(props.config['model_group.audio.enabled'] === 'true');
const apiKeyProvider = ref('');
const loading = ref(false);

const selectedApiKeyProvider = computed(() =>
  providers.value.find(p => p.name === apiKeyProvider.value) || null,
);

/** Extract provider name from a model group value like "groq/mixtral-8x7b". */
function extractProvider(groupValue: string | undefined): string {
  if (!groupValue) return '';
  const idx = groupValue.indexOf('/');
  return idx > 0 ? groupValue.substring(0, idx) : groupValue;
}

// Per-group model filtering, reasoning options, and the "clear stale model on
// provider change" behavior now live inside <ModelGroupFields>.

onMounted(async () => {
  loading.value = true;
  try {
    const res = await listLLMProviders(props.agentUrl, props.token ?? '');
    providers.value = res.providers.map((p) => {
      // Determine active auth method
      const authMethods = p.auth_methods || [];
      let selectedMethod = p.active_auth_method || '';
      if (!selectedMethod && authMethods.length > 0) {
        const defaultMethod = authMethods.find(m => m.is_default);
        selectedMethod = defaultMethod ? defaultMethod.id : authMethods[0].id;
      }
      // Check if config already has an auth_method for this provider
      const savedMethod = props.config[`${p.name}.auth_method`];
      if (savedMethod) selectedMethod = savedMethod;

      return {
        ...p,
        apiKey: props.config[`${p.name}.api_key`] || '',
        configValues: Object.fromEntries(
          Object.keys(p.config_fields || {}).map(k => [k, props.config[`${p.name}.${k}`] || ''])
        ),
        models: [],
        selectedAuthMethod: selectedMethod,
        authFieldValues: Object.fromEntries(
          authMethods.flatMap(am =>
            Object.keys(am.fields).map(k => [k, props.config[`${p.name}.${k}`] || ''])
          )
        ),
      } as ProviderWithState;
    });

    for (const p of providers.value) {
      try {
        const modelRes = await getProviderModels(props.agentUrl, props.token ?? '', p.name);
        p.models = modelRes.models;
      } catch { /* Provider catalog may not exist */ }
    }

    rebuildModelList(providers.value);
  } catch {
    // Fallback: show empty provider cards
  } finally {
    loading.value = false;
  }
});

function emitConfig() {
  const config: Record<string, string> = {
    // Derive default_provider from the main model's provider
    default_provider: highProvider.value || 'groq',
  };
  if (modelGroupHigh.value) config['model_group.high'] = modelGroupHigh.value;
  if (reasoningEffortHigh.value) config['model_group.high.reasoning_effort'] = reasoningEffortHigh.value;
  if (modelGroupLow.value) config['model_group.low'] = modelGroupLow.value;
  if (reasoningEffortLow.value) config['model_group.low.reasoning_effort'] = reasoningEffortLow.value;
  if (modelGroupVision.value) config['model_group.vision'] = modelGroupVision.value;
  // Always emit the vision feature toggle so turning it off also persists.
  config['model_group.vision.enabled'] = String(visionEnabled.value);
  if (modelGroupAudio.value) config['model_group.audio'] = modelGroupAudio.value;
  // Always emit the audio feature toggle so turning it off also persists.
  config['model_group.audio.enabled'] = String(audioEnabled.value);

  for (const p of providers.value) {
    // Emit auth_method selection
    if (p.auth_methods && p.auth_methods.length > 0 && p.selectedAuthMethod) {
      config[`${p.name}.auth_method`] = p.selectedAuthMethod;
      // Emit field values for the selected auth method
      const activeMethod = p.auth_methods.find(m => m.id === p.selectedAuthMethod);
      if (activeMethod) {
        for (const [key, value] of Object.entries(p.authFieldValues)) {
          if (value && key in activeMethod.fields) {
            config[`${p.name}.${key}`] = value;
          }
        }
      }
    }
    // Legacy: emit api_key if set
    if (p.apiKey) config[`${p.name}.api_key`] = p.apiKey;
    // Legacy: emit config_fields values
    for (const [key, value] of Object.entries(p.configValues)) {
      if (value) config[`${p.name}.${key}`] = value;
    }
  }

  emit('update', config);
}

watch(
  [
    highProvider, modelGroupHigh, reasoningEffortHigh,
    lowProvider, modelGroupLow, reasoningEffortLow,
    visionProvider, modelGroupVision, visionEnabled,
    audioProvider, modelGroupAudio, audioEnabled,
  ],
  emitConfig,
);
watch(providers, emitConfig, { deep: true });
</script>

<template>
  <div class="step-llm-config">
    <h3 class="step-title">LLM Provider Configuration</h3>
    <p class="step-description">
      Configure at least one LLM provider with an API key, then choose the
      <strong>model</strong> the assistant runs on.
    </p>

    <div v-if="loading" class="loading-state">Loading providers...</div>

    <template v-else>
      <div class="api-keys-section">
        <h4 class="section-subtitle">API Keys</h4>
        <p class="step-description">
          Pick a provider to configure its credentials. You can switch providers and configure as many as you like — all entries are saved together when you finish setup.
        </p>
        <ElForm label-position="top" class="groups-form">
          <ElFormItem label="Provider">
            <ElSelect v-model="apiKeyProvider" placeholder="Select a provider to configure">
              <ElOption v-for="p in providers" :key="p.name" :label="p.display_name" :value="p.name" />
            </ElSelect>
          </ElFormItem>

          <div v-if="selectedApiKeyProvider" class="provider-config-inline">
            <ProviderConfigFields :provider="selectedApiKeyProvider" />
          </div>
        </ElForm>
      </div>

      <div class="model-groups-section">
        <h4 class="section-subtitle">Model</h4>

        <div class="group-block">
          <p class="step-description">
            The main model the assistant uses for reasoning, tool calls, and replies.
          </p>
          <ModelGroupFields
            :providers="providers"
            :all-models="allModels"
            v-model:provider="highProvider"
            v-model:model="modelGroupHigh"
            v-model:reasoning-effort="reasoningEffortHigh"
          />
        </div>

        <div class="group-block">
          <p class="step-description">
            <strong>Low-Performance Model (optional).</strong> A cheaper/faster model
            for lightweight background tasks (e.g. the skill-event matching gate).
            Leave empty to fall back to the main model.
          </p>
          <ModelGroupFields
            :providers="providers"
            :all-models="allModels"
            clearable
            model-placeholder="Select model (defaults to main)"
            v-model:provider="lowProvider"
            v-model:model="modelGroupLow"
            v-model:reasoning-effort="reasoningEffortLow"
          />
        </div>

        <div class="group-block">
          <div class="group-title-row">
            <p class="step-description" style="margin: 0;">
              <strong>Specialized Vision Model (optional).</strong> Turn on to use a
              dedicated vision model for image understanding when your main model
              can't see images. Leave empty to fall back to the main model.
            </p>
            <ElSwitch v-model="visionEnabled" />
          </div>
          <ModelGroupFields
            v-if="visionEnabled"
            :providers="providers"
            :all-models="allModels"
            :use-vision="true"
            :show-reasoning="false"
            clearable
            model-placeholder="Select vision model (defaults to main)"
            v-model:provider="visionProvider"
            v-model:model="modelGroupVision"
          />
        </div>

        <div class="group-block">
          <div class="group-title-row">
            <p class="step-description" style="margin: 0;">
              <strong>Specialized Audio Model (optional).</strong> Turn on to use a
              dedicated audio model for audio understanding when your main model
              can't process audio. Leave empty to fall back to the main model.
            </p>
            <ElSwitch v-model="audioEnabled" />
          </div>
          <ModelGroupFields
            v-if="audioEnabled"
            :providers="providers"
            :all-models="allModels"
            :use-audio="true"
            :show-reasoning="false"
            clearable
            model-placeholder="Select audio model (defaults to main)"
            v-model:provider="audioProvider"
            v-model:model="modelGroupAudio"
          />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.step-llm-config { padding: 8px 0; }

.step-title {
  font-size: 1.1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 8px 0;
}

.step-description {
  color: var(--text-secondary); font-size: 0.875rem; margin: 0 0 20px 0; line-height: 1.5;
}

.loading-state { text-align: center; padding: 40px; color: var(--text-secondary); }

.api-keys-section { margin-bottom: 24px; }

.provider-config-inline {
  margin: 0 0 16px 0;
  padding: 12px 16px;
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 8px;
  background: var(--surface-color, #fafafa);
  max-width: 480px;
}

.model-groups-section { margin-top: 8px; }

.section-subtitle {
  font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 12px 0;
}

.group-block {
  margin-bottom: 24px;
  padding: 16px;
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 8px;
  background: var(--surface-color, #fafafa);
}

.group-block .step-description { margin-bottom: 12px; }

.group-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.groups-form { max-width: 480px; }
</style>
