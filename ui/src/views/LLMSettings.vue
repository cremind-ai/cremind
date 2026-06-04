<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { ElForm, ElFormItem, ElSelect, ElOption, ElButton, ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import {
  listLLMProviders,
  getProviderModels,
  updateProvider,
  deleteProviderConfig,
  getModelGroups,
  updateModelGroups,
} from '../services/configApi';
import { useLLMModels } from '../composables/useLLMModels';
import ProviderConfigFields, { type ProviderWithState } from '../components/shared/ProviderConfigFields.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();
const { rebuildModelList, getFilteredModels, getReasoningOptions } = useLLMModels();

const providers = ref<ProviderWithState[]>([]);
const modelGroups = ref<Record<string, string>>({ high: '', low: '' });
const reasoningEfforts = ref<Record<string, string | null>>({ high: null, low: null });
const highProvider = ref('');
const lowProvider = ref('');
const apiKeyProvider = ref('');
const loading = ref(false);
const saving = ref(false);

/** Extract provider name from a model group value like "groq/mixtral-8x7b". */
function extractProvider(groupValue: string): string {
  if (!groupValue) return '';
  const idx = groupValue.indexOf('/');
  return idx > 0 ? groupValue.substring(0, idx) : groupValue;
}

/** Build a ProviderWithState from an API LLMProvider response. */
function buildProviderState(p: any): ProviderWithState {
  // Determine the active auth method
  const authMethods = p.auth_methods || [];
  let selectedMethod = p.active_auth_method || '';
  if (!selectedMethod && authMethods.length > 0) {
    const defaultMethod = authMethods.find((m: any) => m.is_default);
    selectedMethod = defaultMethod ? defaultMethod.id : authMethods[0].id;
  }

  return {
    ...p,
    apiKey: '',
    configValues: { ...(p.current_values || {}) },
    models: [],
    selectedAuthMethod: selectedMethod,
    authFieldValues: {},
  };
}

const selectedApiKeyProvider = computed(() =>
  providers.value.find(p => p.name === apiKeyProvider.value) || null
);

const highFilteredModels = computed(() => getFilteredModels(highProvider.value));
const lowFilteredModels = computed(() => getFilteredModels(lowProvider.value));
const highModelReasoningOptions = computed(() => getReasoningOptions(modelGroups.value.high));
const lowModelReasoningOptions = computed(() => getReasoningOptions(modelGroups.value.low));

// When provider changes, clear model selection if the current model doesn't belong to the new provider
watch(highProvider, (newProvider, oldProvider) => {
  if (oldProvider && newProvider !== oldProvider) {
    const currentProvider = extractProvider(modelGroups.value.high);
    if (currentProvider !== newProvider) {
      modelGroups.value.high = '';
      reasoningEfforts.value.high = null;
    }
  }
});
watch(lowProvider, (newProvider, oldProvider) => {
  if (oldProvider && newProvider !== oldProvider) {
    const currentProvider = extractProvider(modelGroups.value.low);
    if (currentProvider !== newProvider) {
      modelGroups.value.low = '';
      reasoningEfforts.value.low = null;
    }
  }
});

// Clear reasoning effort when model changes and new model doesn't support it
watch(() => modelGroups.value.high, () => {
  if (highModelReasoningOptions.value.length === 0) reasoningEfforts.value.high = null;
});
watch(() => modelGroups.value.low, () => {
  if (lowModelReasoningOptions.value.length === 0) reasoningEfforts.value.low = null;
});

onMounted(async () => {
  loading.value = true;
  try {
    const [provRes, groupRes] = await Promise.all([
      listLLMProviders(settingsStore.agentUrl, settingsStore.authToken),
      getModelGroups(settingsStore.agentUrl, settingsStore.authToken),
    ]);

    providers.value = provRes.providers.map(buildProviderState);

    modelGroups.value = groupRes.model_groups;

    // Initialize per-group providers from stored model group values
    highProvider.value = extractProvider(groupRes.model_groups.high) || groupRes.default_provider || '';
    lowProvider.value = extractProvider(groupRes.model_groups.low) || groupRes.default_provider || '';

    for (const p of providers.value) {
      try {
        const modelRes = await getProviderModels(settingsStore.agentUrl, settingsStore.authToken, p.name);
        p.models = modelRes.models;
      } catch { /* ignore */ }
    }

    rebuildModelList(providers.value);
    reasoningEfforts.value = groupRes.reasoning_efforts || { high: null, low: null };
  } catch (e) {
    ElMessage.error('Failed to load LLM settings');
  } finally {
    loading.value = false;
  }
});

/** Save provider credentials using the new auth_methods flow. */
async function saveProvider(provider: ProviderWithState) {
  const config: Record<string, string> = {};

  // If using auth_methods (new flow)
  if (provider.auth_methods && provider.auth_methods.length > 0) {
    config.auth_method = provider.selectedAuthMethod;
    // Include field values for the selected auth method
    const activeMethod = provider.auth_methods.find(m => m.id === provider.selectedAuthMethod);
    if (activeMethod) {
      for (const [key, value] of Object.entries(provider.authFieldValues)) {
        if (value && key in activeMethod.fields) {
          config[key] = value;
        }
      }
    }
  }

  if (Object.keys(config).length <= 1 && !config.auth_method) return;
  // Validate JSON fields
  if (provider.auth_methods) {
    const activeMethod = provider.auth_methods.find(m => m.id === provider.selectedAuthMethod);
    if (activeMethod) {
      for (const [key, value] of Object.entries(provider.authFieldValues)) {
        if (value && activeMethod.fields[key]?.type === 'json') {
          try {
            JSON.parse(value);
          } catch {
            ElMessage.error(`Invalid JSON for ${activeMethod.fields[key].description || key}`);
            return;
          }
        }
      }
    }
  }

  saving.value = true;
  try {
    await updateProvider(settingsStore.agentUrl, settingsStore.authToken, provider.name, config);
    provider.configured = true;
    ElMessage.success(`${provider.display_name} configuration saved`);
  } catch (e) {
    ElMessage.error(`Failed to save: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

/** Legacy: save API key only. */
async function saveProviderKey(provider: ProviderWithState) {
  if (!provider.apiKey) return;
  saving.value = true;
  try {
    await updateProvider(settingsStore.agentUrl, settingsStore.authToken, provider.name, {
      api_key: provider.apiKey,
    });
    provider.configured = true;
    ElMessage.success(`${provider.display_name} API key saved`);
  } catch (e) {
    ElMessage.error(`Failed to save: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

/** Legacy: save config fields. */
async function saveProviderConfig(provider: ProviderWithState) {
  const config: Record<string, string> = {};
  for (const [key, value] of Object.entries(provider.configValues)) {
    if (value) config[key] = value;
  }
  if (Object.keys(config).length === 0) return;

  for (const [key, value] of Object.entries(config)) {
    if (provider.config_fields?.[key]?.type === 'json') {
      try {
        JSON.parse(value);
      } catch {
        ElMessage.error(`Invalid JSON for ${provider.config_fields[key].description || key}`);
        return;
      }
    }
  }

  saving.value = true;
  try {
    await updateProvider(settingsStore.agentUrl, settingsStore.authToken, provider.name, config);
    for (const key of Object.keys(config)) {
      if (provider.config_fields?.[key]) {
        provider.config_fields[key].configured = true;
      }
    }
    provider.configured = true;
    ElMessage.success(`${provider.display_name} configuration saved`);
  } catch (e) {
    ElMessage.error(`Failed to save: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

async function removeProviderConfiguration(provider: ProviderWithState) {
  if (!confirm(`Remove all stored credentials for ${provider.display_name}?`)) return;
  saving.value = true;
  try {
    await deleteProviderConfig(settingsStore.agentUrl, settingsStore.authToken, provider.name);
    provider.configured = false;
    provider.apiKey = '';
    provider.authFieldValues = {};
    provider.configValues = {};
    // Reset configured status on auth method fields
    if (provider.auth_methods) {
      for (const am of provider.auth_methods) {
        for (const field of Object.values(am.fields)) {
          field.configured = false;
        }
      }
    }
    if (provider.config_fields) {
      for (const field of Object.values(provider.config_fields)) {
        (field as any).configured = false;
      }
    }
    ElMessage.success(`${provider.display_name} configuration removed`);
  } catch (e) {
    ElMessage.error(`Failed to remove: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

async function saveModelGroups() {
  saving.value = true;
  try {
    // Derive default_provider from the high group's provider
    const derivedDefaultProvider = highProvider.value || lowProvider.value || '';
    await updateModelGroups(
      settingsStore.agentUrl,
      settingsStore.authToken,
      modelGroups.value,
      derivedDefaultProvider,
      reasoningEfforts.value,
    );
    ElMessage.success('Model groups updated');
  } catch (e) {
    ElMessage.error('Failed to save model groups');
  } finally {
    saving.value = false;
  }
}

function goBack() {
  router.push(`/${props.profile}/settings`);
}
</script>

<template>
  <div class="llm-settings-page">
    <div class="page-container">
      <div class="page-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" /> Back to Settings
        </button>
        <h1 class="page-title">LLM Providers</h1>
      </div>

      <div v-if="loading" class="loading-state">Loading...</div>

      <template v-else>
        <!-- API Keys Section -->
        <div class="section">
          <h2 class="section-title">API Keys</h2>
          <p class="section-description">Configure API keys and credentials for your LLM providers.</p>

          <ElForm label-position="top" class="groups-form">
            <ElFormItem label="Provider">
              <ElSelect v-model="apiKeyProvider" placeholder="Select a provider to configure">
                <ElOption v-for="p in providers" :key="p.name" :label="p.display_name" :value="p.name" />
              </ElSelect>
            </ElFormItem>

            <div v-if="selectedApiKeyProvider" class="provider-config-inline">
              <ProviderConfigFields
                :provider="selectedApiKeyProvider"
                :show-configured-badge="true"
                :show-save-buttons="true"
                :saving="saving"
                @save-provider="saveProvider(selectedApiKeyProvider!)"
                @save-key="saveProviderKey(selectedApiKeyProvider!)"
                @save-config="saveProviderConfig(selectedApiKeyProvider!)"
                @remove-config="removeProviderConfiguration(selectedApiKeyProvider!)"
              />
            </div>
          </ElForm>
        </div>

        <!-- High Group -->
        <div class="section">
          <h2 class="section-title">High Group (Reasoning)</h2>
          <p class="section-description">Select the provider and model used for reasoning tasks.</p>

          <ElForm label-position="top" class="groups-form">
            <ElFormItem label="Provider">
              <ElSelect v-model="highProvider" placeholder="Select provider">
                <ElOption v-for="p in providers" :key="p.name" :label="p.display_name" :value="p.name" />
              </ElSelect>
            </ElFormItem>

            <ElFormItem label="Model">
              <ElSelect v-model="modelGroups.high" filterable placeholder="Select reasoning model">
                <ElOption v-for="m in highFilteredModels" :key="m.value" :label="m.label" :value="m.value" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem v-if="highModelReasoningOptions.length > 0" label="Reasoning Effort">
              <ElSelect v-model="reasoningEfforts.high" placeholder="Select reasoning effort" clearable>
                <ElOption v-for="opt in highModelReasoningOptions" :key="opt" :label="opt" :value="opt" />
              </ElSelect>
            </ElFormItem>
          </ElForm>
        </div>

        <!-- Low Group -->
        <div class="section">
          <h2 class="section-title">Low Group (Tools)</h2>
          <p class="section-description">Select the provider and model used for tool calls.</p>

          <ElForm label-position="top" class="groups-form">
            <ElFormItem label="Provider">
              <ElSelect v-model="lowProvider" placeholder="Select provider">
                <ElOption v-for="p in providers" :key="p.name" :label="p.display_name" :value="p.name" />
              </ElSelect>
            </ElFormItem>

            <ElFormItem label="Model">
              <ElSelect v-model="modelGroups.low" filterable placeholder="Select tools model">
                <ElOption v-for="m in lowFilteredModels" :key="m.value" :label="m.label" :value="m.value" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem v-if="lowModelReasoningOptions.length > 0" label="Reasoning Effort">
              <ElSelect v-model="reasoningEfforts.low" placeholder="Select reasoning effort" clearable>
                <ElOption v-for="opt in lowModelReasoningOptions" :key="opt" :label="opt" :value="opt" />
              </ElSelect>
            </ElFormItem>
          </ElForm>
        </div>

        <ElButton type="primary" :loading="saving" @click="saveModelGroups">Save Model Groups</ElButton>
      </template>
    </div>
  </div>
</template>

<style scoped>
.llm-settings-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}

.page-container { max-width: 720px; margin: 0 auto; }
.page-header { margin-bottom: 24px; }

.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 12px;
}
.back-btn:hover { color: var(--primary-color); }

.page-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0; }
.loading-state { text-align: center; padding: 40px; color: var(--text-secondary); }

.section { margin-bottom: 32px; }
.section-title { font-size: 1.1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 8px 0; }
.section-description { color: var(--text-secondary); font-size: 0.85rem; margin: 0 0 16px 0; }

.groups-form { max-width: 480px; }

.provider-config-inline {
  margin: 0 0 16px 0;
  padding: 12px 16px;
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 8px;
  background: var(--surface-color, #fafafa);
  max-width: 480px;
}
</style>
