<script setup lang="ts">
import { ref, computed } from 'vue';
import { ElForm, ElFormItem, ElInput, ElTag, ElButton, ElRadioGroup, ElRadio, ElMessage } from 'element-plus';
import type { LLMProvider, LLMModel, ProviderConfigField, AuthMethod } from '../../services/configApi';
import { startDeviceCodeFlow, pollDeviceCode } from '../../services/configApi';
import { useSettingsStore } from '../../stores/settings';

export interface ProviderWithState extends LLMProvider {
  apiKey: string;
  configValues: Record<string, string>;
  models: LLMModel[];
  selectedAuthMethod: string;
  authFieldValues: Record<string, string>;
}

const props = withDefaults(defineProps<{
  provider: ProviderWithState;
  showConfiguredBadge?: boolean;
  showSaveButtons?: boolean;
  saving?: boolean;
}>(), {
  showConfiguredBadge: false,
  showSaveButtons: false,
  saving: false,
});

const emit = defineEmits<{
  'save-key': [];
  'save-config': [];
  'save-provider': [];
  'remove-config': [];
}>();

const settingsStore = useSettingsStore();

// Device code flow state
const deviceCodeLoading = ref(false);
const deviceCodeData = ref<{ verification_uri: string; user_code: string; device_code: string } | null>(null);
const deviceCodePolling = ref(false);
const deviceCodeComplete = ref(false);
let pollTimer: ReturnType<typeof setTimeout> | null = null;

const hasMultipleAuthMethods = computed(() =>
  (props.provider.auth_methods?.length ?? 0) > 1
);

const activeAuthMethod = computed<AuthMethod | null>(() => {
  const methods = props.provider.auth_methods;
  if (!methods || methods.length === 0) return null;
  return methods.find(m => m.id === props.provider.selectedAuthMethod)
    || methods.find(m => m.is_default)
    || methods[0];
});

const isNoAuth = computed(() =>
  activeAuthMethod.value?.kind === 'none'
);

const isDeviceCode = computed(() =>
  activeAuthMethod.value?.kind === 'device_code'
);

const isConfigured = computed(() => {
  const am = activeAuthMethod.value;
  if (!am) {
    return props.provider.apiKey || props.provider.configured
      || (props.provider.config_fields && Object.values(props.provider.configValues).some(v => v));
  }
  if (am.kind === 'none') return true;
  if (am.kind === 'device_code') return props.provider.configured || deviceCodeComplete.value;
  return Object.entries(am.fields).every(([key, field]) => {
    if (!field.required) return true;
    return field.configured || !!props.provider.authFieldValues[key];
  });
});

function onAuthMethodChange(methodId: string) {
  props.provider.selectedAuthMethod = methodId;
  // Reset device code state when switching
  deviceCodeData.value = null;
  deviceCodeComplete.value = false;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  deviceCodePolling.value = false;
}

async function startDeviceLogin() {
  deviceCodeLoading.value = true;
  deviceCodeData.value = null;
  deviceCodeComplete.value = false;
  try {
    const data = await startDeviceCodeFlow(settingsStore.agentUrl, settingsStore.authToken);
    deviceCodeData.value = data;
    // Start polling
    deviceCodePolling.value = true;
    pollForToken(data.device_code, (data as any).interval || 5);
  } catch (e) {
    ElMessage.error(`Failed to start device login: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    deviceCodeLoading.value = false;
  }
}

async function pollForToken(deviceCode: string, interval: number) {
  if (!deviceCodePolling.value) return;
  try {
    const result = await pollDeviceCode(settingsStore.agentUrl, settingsStore.authToken, deviceCode);
    if (result.status === 'complete') {
      deviceCodePolling.value = false;
      deviceCodeComplete.value = true;
      props.provider.configured = true;
      // Setup-wizard path: the backend cannot persist to llm_config until the
      // profile exists, so it returns the token here for completeSetup to
      // bundle. Stash it on the provider so the wizard's emitConfig picks it up.
      if (result.access_token) {
        props.provider.apiKey = result.access_token;
      }
      ElMessage.success(`${props.provider.display_name} authorized successfully`);
      return;
    }
    if (result.status === 'expired') {
      deviceCodePolling.value = false;
      ElMessage.warning('Device code expired. Please try again.');
      deviceCodeData.value = null;
      return;
    }
    if (result.status === 'error') {
      deviceCodePolling.value = false;
      ElMessage.error(`Authorization failed: ${result.error}`);
      deviceCodeData.value = null;
      return;
    }
    // Still pending — poll again
    const nextInterval = result.slow_down ? (interval + 5) * 1000 : interval * 1000;
    pollTimer = setTimeout(() => pollForToken(deviceCode, result.slow_down ? interval + 5 : interval), nextInterval);
  } catch {
    // Network error, retry
    pollTimer = setTimeout(() => pollForToken(deviceCode, interval), interval * 1000);
  }
}
</script>

<template>
  <div class="provider-config-fields">
    <div class="provider-status">
      <ElTag v-if="isConfigured" type="success" size="small">Configured</ElTag>
      <ElTag v-else-if="isNoAuth" type="info" size="small">No Key Needed</ElTag>
      <ElTag v-else type="warning" size="small">Not Configured</ElTag>
    </div>

    <!-- Auth methods flow -->
    <template v-if="provider.auth_methods && provider.auth_methods.length > 0">
      <!-- Auth method radio group (only shown when >1 method) -->
      <div v-if="hasMultipleAuthMethods" class="auth-method-selector">
        <ElRadioGroup
          :model-value="provider.selectedAuthMethod"
          @update:model-value="onAuthMethodChange($event as string)"
          size="small"
        >
          <ElRadio
            v-for="am in provider.auth_methods"
            :key="am.id"
            :value="am.id"
          >
            {{ am.label }}
            <span v-if="am.hint" class="auth-hint">{{ am.hint }}</span>
          </ElRadio>
        </ElRadioGroup>
      </div>

      <!-- Instructions for the active auth method -->
      <div v-if="activeAuthMethod?.instructions" class="auth-instructions">
        {{ activeAuthMethod.instructions }}
      </div>

      <!-- Device code flow (e.g. GitHub Copilot) -->
      <template v-if="isDeviceCode">
        <div v-if="!deviceCodeData && !deviceCodeComplete" class="device-code-start">
          <ElButton type="primary" :loading="deviceCodeLoading" @click="startDeviceLogin" size="small">
            Start Device Login
          </ElButton>
        </div>

        <div v-else-if="deviceCodeData && !deviceCodeComplete" class="device-code-active">
          <div class="device-code-step">
            <span class="device-code-label">Visit:</span>
            <a :href="deviceCodeData.verification_uri" target="_blank" rel="noopener" class="device-code-link">
              {{ deviceCodeData.verification_uri }}
            </a>
          </div>
          <div class="device-code-step">
            <span class="device-code-label">Code:</span>
            <code class="device-code-value">{{ deviceCodeData.user_code }}</code>
          </div>
          <div class="device-code-status">
            <span v-if="deviceCodePolling" class="polling-indicator">Waiting for authorization...</span>
          </div>
        </div>

        <div v-else-if="deviceCodeComplete" class="device-code-done">
          Authorized successfully.
        </div>
      </template>

      <!-- Regular fields for non-device-code, non-none auth methods -->
      <template v-else-if="activeAuthMethod && activeAuthMethod.kind !== 'none' && !isDeviceCode">
        <ElForm label-position="top" size="small" class="config-fields-form">
          <ElFormItem
            v-for="(field, key) in activeAuthMethod.fields"
            :key="key"
            :required="field.required"
          >
            <template #label>
              {{ field.description || key }}
              <ElTag v-if="showConfiguredBadge && field.configured" type="success" size="small" style="margin-left: 6px;">Set</ElTag>
            </template>
            <ElInput
              v-if="field.type === 'json'"
              :model-value="provider.authFieldValues[key]"
              @update:model-value="provider.authFieldValues[key] = $event"
              type="textarea"
              :rows="showSaveButtons ? 4 : 3"
              :placeholder="showConfiguredBadge && field.configured ? '(already set — paste new value to replace)' : `Paste ${field.description}`"
            />
            <div v-else-if="field.secret" :class="showSaveButtons ? 'key-input-row' : ''">
              <ElInput
                :model-value="provider.authFieldValues[key]"
                @update:model-value="provider.authFieldValues[key] = $event"
                type="password"
                show-password
                :placeholder="showConfiguredBadge && field.configured ? '(already set)' : `Enter ${field.description}`"
              />
            </div>
            <ElInput
              v-else
              :model-value="provider.authFieldValues[key]"
              @update:model-value="provider.authFieldValues[key] = $event"
              :placeholder="field.default ? `Default: ${field.default}` : `Enter ${field.description}`"
            />
          </ElFormItem>
          <ElButton v-if="showSaveButtons" type="primary" :loading="saving" @click="emit('save-provider')" size="small">Save</ElButton>
        </ElForm>
      </template>

      <div v-else-if="isNoAuth" class="no-auth-info">
        No credentials needed for this provider.
      </div>
    </template>

    <!-- Legacy fallback -->
    <template v-else>
      <ElForm v-if="provider.requires_api_key" label-position="top" size="small" class="key-form">
        <ElFormItem label="API Key">
          <div :class="showSaveButtons ? 'key-input-row' : ''">
            <ElInput
              :model-value="provider.apiKey"
              @update:model-value="provider.apiKey = $event"
              type="password"
              show-password
              :placeholder="`Enter ${provider.display_name} API key`"
            />
            <ElButton v-if="showSaveButtons" type="primary" :loading="saving" @click="emit('save-key')" size="small">Save</ElButton>
          </div>
        </ElFormItem>
      </ElForm>

      <template v-if="provider.config_fields && Object.keys(provider.config_fields).length > 0">
        <ElForm label-position="top" size="small" class="config-fields-form">
          <ElFormItem
            v-for="(field, key) in (provider.config_fields as Record<string, ProviderConfigField>)"
            :key="key"
            :required="field.required"
          >
            <template #label>
              {{ field.description || key }}
              <ElTag v-if="showConfiguredBadge && field.configured" type="success" size="small" style="margin-left: 6px;">Set</ElTag>
            </template>
            <ElInput
              v-if="field.type === 'json'"
              :model-value="provider.configValues[key]"
              @update:model-value="provider.configValues[key] = $event"
              type="textarea"
              :rows="showSaveButtons ? 4 : 3"
              :placeholder="showConfiguredBadge && field.configured ? '(already set — paste new JSON to replace)' : `Paste ${field.description}`"
            />
            <ElInput
              v-else-if="field.secret"
              :model-value="provider.configValues[key]"
              @update:model-value="provider.configValues[key] = $event"
              type="password"
              show-password
              :placeholder="showConfiguredBadge && field.configured ? '(already set)' : `Enter ${field.description}`"
            />
            <ElInput
              v-else
              :model-value="provider.configValues[key]"
              @update:model-value="provider.configValues[key] = $event"
              :placeholder="field.default ? `Default: ${field.default}` : `Enter ${field.description}`"
            />
          </ElFormItem>
          <ElButton v-if="showSaveButtons" type="primary" :loading="saving" @click="emit('save-config')" size="small">Save Configuration</ElButton>
        </ElForm>
      </template>
    </template>

    <div v-if="showSaveButtons && provider.configured" class="remove-config-row">
      <ElButton type="danger" plain size="small" :loading="saving" @click="emit('remove-config')">
        Remove Configuration
      </ElButton>
    </div>

    <div v-if="!showSaveButtons && provider.models.length > 0" class="model-count">
      {{ provider.models.length }} model(s) available
    </div>
  </div>
</template>

<style scoped>
.provider-status { margin-bottom: 8px; }

.auth-method-selector { margin-bottom: 12px; }
.auth-method-selector .el-radio { display: flex; align-items: flex-start; margin-bottom: 4px; }
.auth-hint { font-size: 0.75rem; color: var(--text-tertiary); margin-left: 4px; }

.auth-instructions {
  font-size: 0.8rem;
  color: var(--text-secondary);
  line-height: 1.5;
  padding: 8px 12px;
  margin-bottom: 10px;
  background: var(--bg-color-overlay, rgba(0, 0, 0, 0.02));
  border-left: 3px solid var(--primary-color, #409eff);
  border-radius: 0 4px 4px 0;
}

.key-form { max-width: 100%; }
.key-input-row { display: flex; gap: 8px; width: 100%; }
.key-input-row .el-input { flex: 1; }

.config-fields-form { max-width: 100%; margin-top: 8px; }

.no-auth-info {
  font-size: 0.85rem;
  color: var(--text-secondary);
  padding: 8px 0;
}

/* Device code flow styles */
.device-code-start { margin: 8px 0; }

.device-code-active {
  padding: 12px;
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 8px;
  background: var(--surface-color, #fafafa);
  margin: 8px 0;
}

.device-code-step {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.device-code-label {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-primary);
  min-width: 40px;
}

.device-code-link {
  color: var(--primary-color, #409eff);
  text-decoration: none;
  font-size: 0.85rem;
}
.device-code-link:hover { text-decoration: underline; }

.device-code-value {
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--text-primary);
  background: var(--bg-color, #fff);
  padding: 2px 10px;
  border-radius: 4px;
  border: 1px solid var(--border-color, #e4e7ed);
  letter-spacing: 2px;
}

.device-code-status { margin-top: 4px; }

.polling-indicator {
  font-size: 0.8rem;
  color: var(--text-secondary);
}
.polling-indicator::before {
  content: '';
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--warning-color, #e6a23c);
  margin-right: 6px;
  animation: pulse 1.5s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; }
}

.device-code-done {
  font-size: 0.85rem;
  color: var(--success-color, #67c23a);
  padding: 8px 0;
}

.remove-config-row {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-color, #e4e7ed);
}

.model-count {
  font-size: 0.8rem;
  color: var(--text-tertiary);
  margin-top: 4px;
}
</style>
