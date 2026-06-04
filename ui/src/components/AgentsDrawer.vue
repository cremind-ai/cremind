<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';
import { useSettingsStore } from '../stores/settings';
import { storeToRefs } from 'pinia';
import { ElDrawer, ElButton, ElNotification, ElTag, ElSwitch } from 'element-plus';
import { Icon } from '@iconify/vue';
import { fetchAgents, getAuthUrl, unlinkAgent, reconnectAgent, toggleAgentEnabled, type RemoteAgent } from '../services/agentApi';
import { openSettingsStateStream, type SettingsStateStreamHandle } from '../services/settingsStateStream';

const props = defineProps<{
  modelValue: boolean;
}>();

const emit = defineEmits<{
  'update:modelValue': [value: boolean];
}>();

const settingsStore = useSettingsStore();
const { agentUrl, profileId: storedProfileId, authToken } = storeToRefs(settingsStore);

const agents = ref<RemoteAgent[]>([]);
const loading = ref(false);
const error = ref('');

const isElectron = computed(() => {
  return typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__;
});

const drawerClass = computed(() => {
  return isElectron.value ? 'electron-drawer' : '';
});

const profileId = computed(() => {
  return storedProfileId.value || 'default';
});

async function loadAgents() {
  loading.value = true;
  error.value = '';
  try {
    agents.value = await fetchAgents(agentUrl.value, authToken.value);
  } catch (e: any) {
    error.value = e.message || 'Failed to load agents';
    agents.value = [];
  } finally {
    loading.value = false;
  }
}

watch(() => props.modelValue, (open) => {
  if (open) loadAgents();
});

// Live updates: the settings-state SSE stream pings whenever any agent is
// registered, enabled/disabled, authed/unlinked, or reconnected. Refetch
// on each ping so the drawer stays current without a manual refresh
// button. Stream is shared cross-tab by `createSharedStream` so opening
// multiple drawers / Settings pages doesn't multiply the connection.
let settingsStream: SettingsStateStreamHandle | null = null;

function openSettingsStream() {
  if (settingsStream) return;
  if (!authToken.value || !profileId.value) return;
  settingsStream = openSettingsStateStream(
    agentUrl.value, authToken.value, profileId.value,
    () => { loadAgents(); },
  );
}

function closeSettingsStream() {
  if (!settingsStream) return;
  try { settingsStream.close(); } catch { /* ignore */ }
  settingsStream = null;
}

watch(
  () => [authToken.value, profileId.value] as const,
  ([token, profile]) => {
    closeSettingsStream();
    if (token && profile) openSettingsStream();
  },
  { immediate: true },
);

async function handleLink(agent: RemoteAgent) {
  try {
    const url = await getAuthUrl(agentUrl.value, authToken.value, agent.name);
    window.open(url, '_blank');
    ElNotification({
      title: 'Authentication Started',
      message: `Complete authentication in the new tab for "${agent.name}". Then refresh the agent list.`,
      type: 'info',
      duration: 5000,
    });
  } catch (e: any) {
    ElNotification({
      title: 'Link Failed',
      message: e.message || 'Could not initiate authentication',
      type: 'error',
      duration: 4000,
    });
  }
}

async function handleToggleEnabled(agent: RemoteAgent, enabled: boolean) {
  try {
    await toggleAgentEnabled(agentUrl.value, authToken.value, agent.name, enabled);
    agent.enabled = enabled;
    ElNotification({
      title: enabled ? 'Enabled' : 'Disabled',
      message: `"${agent.name}" has been ${enabled ? 'enabled' : 'disabled'}.`,
      type: 'success',
      duration: 3000,
    });
  } catch (e: any) {
    // Revert on failure
    agent.enabled = !enabled;
    ElNotification({
      title: 'Toggle Failed',
      message: e.message || 'Could not toggle agent',
      type: 'error',
      duration: 4000,
    });
  }
}

async function handleUnlink(agent: RemoteAgent) {
  try {
    await unlinkAgent(agentUrl.value, authToken.value, agent.name);
    ElNotification({
      title: 'Unlinked',
      message: `"${agent.name}" has been unlinked.`,
      type: 'success',
      duration: 3000,
    });
    await loadAgents();
  } catch (e: any) {
    ElNotification({
      title: 'Unlink Failed',
      message: e.message || 'Could not unlink agent',
      type: 'error',
      duration: 4000,
    });
  }
}

async function handleReconnect(agent: RemoteAgent) {
  try {
    await reconnectAgent(agentUrl.value, authToken.value, agent.name);
    ElNotification({
      title: 'Reconnected',
      message: `"${agent.name}" has been reconnected successfully.`,
      type: 'success',
      duration: 3000,
    });
    await loadAgents();
  } catch (e: any) {
    ElNotification({
      title: 'Reconnect Failed',
      message: e.message || 'Could not reconnect to agent',
      type: 'error',
      duration: 4000,
    });
    await loadAgents();
  }
}

function badgeType(badgeClass: string): 'success' | 'danger' | 'warning' | 'info' {
  if (badgeClass === 'badge-success') return 'success';
  if (badgeClass === 'badge-danger') return 'danger';
  if (badgeClass === 'badge-warning') return 'warning';
  return 'info';
}

function badgeLabel(agent: RemoteAgent): string {
  if (agent.connection_error) return 'Error';
  if (agent.badge_class === 'badge-success') return 'Linked';
  if (agent.badge_class === 'badge-danger') return 'Not Linked';
  if (agent.badge_class === 'badge-warning') return 'Expired';
  return 'No Auth';
}

const handleClose = () => {
  emit('update:modelValue', false);
};

// Listen for auth completion from the OAuth callback tab
function handleAuthMessage(event: MessageEvent) {
  if (event.origin !== window.location.origin) return;
  if (event.data?.type === 'a2a-auth-complete') {
    loadAgents();
    ElNotification({
      title: 'Authentication Complete',
      message: 'Agent linked successfully.',
      type: 'success',
      duration: 3000,
    });
  }
}

onMounted(() => {
  window.addEventListener('message', handleAuthMessage);
});

onUnmounted(() => {
  window.removeEventListener('message', handleAuthMessage);
  closeSettingsStream();
});
</script>

<template>
  <ElDrawer
    :model-value="modelValue"
    @update:model-value="emit('update:modelValue', $event)"
    :size="420"
    direction="rtl"
    :custom-class="drawerClass"
    :show-close="false"
  >
    <template #header>
      <div class="drawer-header">
        <ElButton @click="handleClose" circle class="back-button">
          <template #icon><Icon icon="mdi:arrow-left" /></template>
        </ElButton>
        <span class="drawer-title">Agents</span>
      </div>
    </template>

    <div class="agents-content">
      <div v-if="profileId !== 'default'" class="profile-badge">
        <Icon icon="mdi:account" />
        <span>{{ profileId }}</span>
      </div>

      <!-- Loading -->
      <div v-if="loading && agents.length === 0" class="agents-state">
        <Icon icon="mdi:loading" class="spin" />
        <span>Loading agents...</span>
      </div>

      <!-- Error -->
      <div v-else-if="error" class="agents-state error">
        <Icon icon="mdi:alert-circle-outline" />
        <span>{{ error }}</span>
        <ElButton size="small" @click="loadAgents">Retry</ElButton>
      </div>

      <!-- Empty -->
      <div v-else-if="agents.length === 0" class="agents-state">
        <Icon icon="mdi:robot-off-outline" />
        <span>No remote agents registered</span>
      </div>

      <!-- Agent list -->
      <div v-else class="agent-list">
        <div v-for="agent in agents" :key="agent.name" class="agent-item" :class="{ 'agent-disabled': !agent.enabled, 'agent-error': !!agent.connection_error }">
          <div class="agent-item-header">
            <ElSwitch
              :model-value="agent.enabled"
              size="small"
              @change="(val: string | number | boolean) => handleToggleEnabled(agent, Boolean(val))"
              class="agent-enable-switch"
            />
            <div class="agent-item-avatar" :class="{ 'avatar-error': !!agent.connection_error }">
              <Icon :icon="agent.connection_error ? 'mdi:robot-off' : 'mdi:robot'" />
            </div>
            <div class="agent-item-info">
              <div class="agent-item-name">
                {{ agent.name }}
                <ElTag v-if="agent.is_default" size="small" type="success" effect="plain" round class="default-tag">
                  default
                </ElTag>
              </div>
              <div class="agent-item-url">{{ agent.url }}</div>
            </div>
            <ElTag :type="agent.connection_error ? 'danger' : badgeType(agent.badge_class)" size="small" effect="dark" round>
              {{ badgeLabel(agent) }}
            </ElTag>
          </div>

          <div v-if="agent.connection_error" class="agent-item-error">
            <Icon icon="mdi:alert-circle-outline" />
            <span>{{ agent.connection_error }}</span>
          </div>

          <div v-else class="agent-item-desc">{{ agent.description }}</div>

          <div v-if="agent.expiration_info && !agent.connection_error" class="agent-item-expiry">
            <Icon icon="mdi:clock-outline" />
            <span>{{ agent.expiration_info.relative }}</span>
          </div>

          <div class="agent-item-actions">
            <ElButton
              v-if="agent.connection_error"
              type="warning"
              size="small"
              @click="handleReconnect(agent)"
            >
              <Icon icon="mdi:refresh" style="margin-right: 4px;" />
              Reconnect
            </ElButton>
            <ElButton
              v-if="agent.show_authenticate && !agent.connection_error"
              type="primary"
              size="small"
              @click="handleLink(agent)"
            >
              <Icon icon="mdi:link-variant" style="margin-right: 4px;" />
              Link
            </ElButton>
            <ElButton
              v-if="agent.show_unlink && !agent.connection_error"
              type="danger"
              size="small"
              @click="handleUnlink(agent)"
            >
              <Icon icon="mdi:link-variant-off" style="margin-right: 4px;" />
              Unlink
            </ElButton>
          </div>
        </div>
      </div>
    </div>
  </ElDrawer>
</template>

<style scoped>
.drawer-header {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
}

.drawer-title {
  flex: 1;
  font-size: 1.1rem;
  font-weight: 600;
  color: var(--text-primary);
}

.agents-content {
  padding: 0 4px;
}

.profile-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  margin-bottom: 16px;
  background: var(--surface-hover, #f1f5f9);
  border-radius: 8px;
  font-size: 0.8rem;
  color: var(--text-secondary);
}

.agents-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 40px 20px;
  color: var(--text-tertiary);
  font-size: 0.875rem;
  text-align: center;
}

.agents-state.error {
  color: var(--danger-color, #ef4444);
}

.agents-state .iconify {
  font-size: 32px;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.spin {
  animation: spin 1s linear infinite;
}

.agent-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.agent-item {
  padding: 16px;
  border: 1px solid var(--border-color);
  border-radius: 10px;
  background: var(--surface-color);
  transition: box-shadow 0.2s ease;
}

.agent-item:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.agent-item-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.agent-item-avatar {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: var(--primary-color);
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  flex-shrink: 0;
}

.agent-item-info {
  flex: 1;
  min-width: 0;
}

.agent-item-name {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.agent-item-url {
  font-size: 0.7rem;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.agent-item-desc {
  font-size: 0.8rem;
  color: var(--text-secondary);
  line-height: 1.5;
  margin-bottom: 8px;
}

.agent-item-expiry {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 0.75rem;
  color: var(--text-tertiary);
  margin-bottom: 10px;
}

.agent-item.agent-disabled {
  opacity: 0.5;
}

.agent-item.agent-error {
  border-color: var(--el-color-danger-light-5, #fab6b6);
  background: var(--el-color-danger-light-9, #fef0f0);
}

.avatar-error {
  background: var(--el-color-danger, #f56c6c) !important;
}

.agent-item-error {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 0.75rem;
  color: var(--el-color-danger, #f56c6c);
  line-height: 1.4;
  margin-bottom: 8px;
}

.agent-item-error .iconify {
  flex-shrink: 0;
  margin-top: 1px;
}

.agent-enable-switch {
  flex-shrink: 0;
}

.default-tag {
  margin-left: 6px;
  font-size: 0.65rem;
  vertical-align: middle;
}

.agent-item-actions {
  display: flex;
  gap: 8px;
}

/* Offset drawer in Electron mode */
:deep(.electron-drawer) {
  top: 32px !important;
  height: calc(100% - 32px) !important;
}
</style>
