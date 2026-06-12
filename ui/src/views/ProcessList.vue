<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, watch } from 'vue';
import { useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import { ElTable, ElTableColumn, ElButton, ElTag, ElAlert, ElMessage, ElMessageBox, ElTooltip } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useProcessManagerStore } from '../stores/processManager';
import { useSettingsStore } from '../stores/settings';
import {
  DuplicateAutostartError,
  registerAutostart,
  runAutostart,
  stopProcess,
  unregisterAutostart,
  type ProcessRow,
} from '../services/processApi';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const store = useProcessManagerStore();
const settings = useSettingsStore();

onMounted(() => {
  // Defensive: make sure this tab has loaded the profile's token from
  // localStorage even if App.vue's auth gate hasn't finished running yet
  // (can happen on a hard reload or a new ``window.open`` tab — the async
  // ``checkProfileAuth`` may not resolve before this child's onMounted).
  if (!settings.authToken && props.profile) {
    settings.activateProfile(props.profile);
  }
  // Open the SSE stream; the first frame is a snapshot, subsequent frames
  // arrive on every state transition (spawn / exit / stop / autostart).
  store.streamStart();
});

// If the token becomes available after mount (auth gate finished async),
// (re)open the stream so the list populates without manual reload.
watch(
  () => settings.authToken,
  (token, prev) => {
    if (token && !prev) {
      store.streamStart();
    }
  },
);

onBeforeUnmount(() => {
  store.streamStop();
});

const exitedCount = computed(
  () => store.processes.filter((p: ProcessRow) => p.status === 'exited').length,
);

function goBack() {
  goBackToChat(router, props.profile);
}

function openTerminal(pid: string) {
  // A new browser tab gets its own WebSocket + xterm.js instance — the
  // backend fan-out keeps every tab in sync.
  const route = router.resolve({
    name: 'process-terminal',
    params: { profile: props.profile, pid },
  });
  window.open(route.href, '_blank');
}

async function confirmStop(row: ProcessRow) {
  try {
    await ElMessageBox.confirm(
      `Stop process ${row.process_id}?\n\n${row.command}`,
      'Confirm stop',
      { confirmButtonText: 'Stop', cancelButtonText: 'Cancel', type: 'warning' },
    );
  } catch {
    return;
  }
  await stopProcess(settings.agentUrl, settings.authToken, row.process_id);
  await store.refresh();
}

async function clearCompleted() {
  const exited = store.processes.filter((p: ProcessRow) => p.status === 'exited');
  for (const row of exited) {
    try {
      await stopProcess(settings.agentUrl, settings.authToken, row.process_id);
    } catch {
      // Best-effort — registry may have already evicted it.
    }
  }
  await store.refresh();
}

function formatTime(ts: number): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

function formatRelativeExpiry(ts: number | null): string {
  if (ts === null) return 'never';
  if (!ts) return '—';
  const diffMs = ts * 1000 - Date.now();
  if (diffMs <= 0) return 'expired';
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

type TagType = 'success' | 'info' | 'warning' | 'danger' | 'primary';

function statusColor(status: string): TagType {
  if (status === 'running') return 'success';
  if (status === 'exited') return 'info';
  if (status === 'failed_to_autostart') return 'warning';
  return 'primary';
}

async function doRegister(row: ProcessRow, force: boolean) {
  const created = await registerAutostart(
    settings.agentUrl, settings.authToken, row.process_id, force,
  );
  row.autostart_id = created.id;
  ElMessage.success('Registered to start with Cremind');
  await store.refresh();
}

async function toggleAutostart(row: ProcessRow) {
  if (row.autostart_id) {
    try {
      await ElMessageBox.confirm(
        `Unregister "Start with Cremind" for this process?\n\n${row.command}`,
        'Unregister autostart',
        { confirmButtonText: 'Unregister', cancelButtonText: 'Cancel', type: 'warning' },
      );
    } catch {
      return;
    }
    try {
      await unregisterAutostart(settings.agentUrl, settings.authToken, row.autostart_id);
      row.autostart_id = null;
      ElMessage.success('Autostart removed');
      await store.refresh();
    } catch (err) {
      ElMessage.error(err instanceof Error ? err.message : String(err));
    }
    return;
  }

  try {
    await doRegister(row, false);
  } catch (err) {
    if (err instanceof DuplicateAutostartError) {
      try {
        await ElMessageBox.confirm(
          `A command already registered to start with Cremind matches this one:\n\n${err.existing.command}\n\nRegister this one anyway?`,
          'Duplicate command',
          { confirmButtonText: 'Register anyway', cancelButtonText: 'Cancel', type: 'warning' },
        );
      } catch {
        return;
      }
      try {
        await doRegister(row, true);
      } catch (err2) {
        ElMessage.error(err2 instanceof Error ? err2.message : String(err2));
      }
      return;
    }
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

async function manualRun(row: ProcessRow) {
  if (!row.autostart_id) return;
  try {
    await runAutostart(settings.agentUrl, settings.authToken, row.autostart_id);
    ElMessage.success('Process started');
    await store.refresh();
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}
</script>

<template>
  <div class="process-page">
    <div class="process-container">
      <div class="process-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Chat
        </button>
        <h1 class="process-title">Process Manager</h1>
        <p class="process-subtitle">
          Long-running processes spawned by the Shell Executor tool. Click a
          row to open its live terminal in a new tab.
        </p>
      </div>

      <ElAlert
        v-if="exitedCount > 0"
        type="warning"
        show-icon
        :closable="false"
        class="warning-banner"
      >
        <div class="warning-row">
          <span>
            {{ exitedCount }} process{{ exitedCount === 1 ? '' : 'es' }} no
            longer active. The process ID is gone; the agent cannot send any
            more input.
          </span>
          <ElButton size="small" @click="clearCompleted">Clear completed</ElButton>
        </div>
      </ElAlert>

      <div v-if="store.error" class="error-row">{{ store.error }}</div>

      <ElTable
        :data="store.processes"
        empty-text="No long-running processes."
        class="process-table"
        stripe
      >
        <ElTableColumn label="Status" width="160">
          <template #default="{ row }">
            <ElTag :type="statusColor(row.status)" size="small" effect="light">
              {{ row.status }}{{ row.exit_code !== null && row.exit_code !== undefined ? ` (${row.exit_code})` : '' }}
            </ElTag>
            <span v-if="row.is_pty" class="pty-pill">pty</span>
            <ElTooltip
              v-if="row.status === 'failed_to_autostart' && row.last_error"
              :content="row.last_error"
              placement="top"
            >
              <Icon icon="mdi:alert" class="warn-icon" />
            </ElTooltip>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Process ID" width="120" prop="process_id" />
        <ElTableColumn label="Command" min-width="280">
          <template #default="{ row }">
            <span class="cmd" :title="row.command">{{ row.command }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Working Dir" min-width="200">
          <template #default="{ row }">
            <span class="cmd" :title="row.working_dir">{{ row.working_dir }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Created" width="180">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </ElTableColumn>
        <ElTableColumn label="Expires in" width="110">
          <template #default="{ row }">{{ formatRelativeExpiry(row.expire_at) }}</template>
        </ElTableColumn>
        <ElTableColumn label="Actions" width="320" align="right">
          <template #default="{ row }">
            <template v-if="row.status === 'failed_to_autostart'">
              <ElButton size="small" type="warning" @click="manualRun(row as ProcessRow)">
                <Icon icon="mdi:play" /> &nbsp;Run
              </ElButton>
              <ElButton size="small" @click="toggleAutostart(row as ProcessRow)">
                <Icon icon="mdi:star" /> &nbsp;Unregister
              </ElButton>
            </template>
            <template v-else>
              <ElTooltip
                :content="row.autostart_id ? 'Unregister from Cremind autostart' : 'Start with Cremind'"
                placement="top"
              >
                <ElButton size="small" :type="row.autostart_id ? 'warning' : 'default'" @click="toggleAutostart(row as ProcessRow)">
                  <Icon :icon="row.autostart_id ? 'mdi:star' : 'mdi:star-outline'" />
                </ElButton>
              </ElTooltip>
              <ElButton size="small" type="primary" @click="openTerminal(row.process_id)">
                <Icon icon="mdi:console" /> &nbsp;Terminal
              </ElButton>
              <ElButton size="small" @click="confirmStop(row as ProcessRow)">
                <Icon icon="mdi:stop" /> &nbsp;Stop
              </ElButton>
            </template>
          </template>
        </ElTableColumn>
      </ElTable>
    </div>
  </div>
</template>

<style scoped>
.process-page {
  width: 100%;
  height: 100%;
  overflow-y: auto;
  background: var(--bg-color);
  padding: 24px;
  box-sizing: border-box;
}
.process-container {
  max-width: 1200px;
  margin: 0 auto;
}
.process-header {
  margin-bottom: 24px;
}
.back-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 0.875rem;
  padding: 4px 0;
  margin-bottom: 16px;
  transition: color 0.2s;
}
.back-btn:hover {
  color: var(--primary-color);
}
.process-title {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0 0 4px 0;
}
.process-subtitle {
  color: var(--text-secondary);
  font-size: 0.875rem;
  margin: 0;
}
.warning-banner {
  margin-bottom: 16px;
}
.warning-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.error-row {
  color: #b91c1c;
  background: #fee2e2;
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 0.875rem;
  margin-bottom: 12px;
}
.process-table {
  background: var(--surface-color);
  border-radius: 8px;
}
.cmd {
  font-family: Consolas, Monaco, monospace;
  font-size: 0.85rem;
  color: var(--text-primary);
  display: inline-block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.pty-pill {
  display: inline-block;
  background: var(--hover-bg);
  color: var(--text-secondary);
  border-radius: 4px;
  font-size: 0.7rem;
  padding: 0 4px;
  margin-left: 6px;
  vertical-align: middle;
}
.warn-icon {
  color: var(--warning-color, #f59e0b);
  font-size: 1rem;
  margin-left: 6px;
  vertical-align: middle;
}
</style>
