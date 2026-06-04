<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElButton, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import TerminalSession from '../components/TerminalSession.vue';

const props = defineProps<{ profile: string; pid: string }>();
const router = useRouter();
const settings = useSettingsStore();

const sessionRef = ref<InstanceType<typeof TerminalSession> | null>(null);

// Reactive proxies that read from the exposed refs on TerminalSession. The
// component uses defineExpose so we can reach .status, .command, etc. from here.
const status = computed(() => (sessionRef.value?.status as any)?.value ?? 'connecting');
const command = computed(() => (sessionRef.value?.command as any)?.value ?? '');
const workingDir = computed(() => (sessionRef.value?.workingDir as any)?.value ?? '');
const isPty = computed(() => Boolean((sessionRef.value?.isPty as any)?.value));
const exitCode = computed(() => (sessionRef.value?.exitCode as any)?.value ?? null);

onMounted(() => {
  // Defensive token load — same rationale as ProcessList.vue.
  if (!settings.authToken && props.profile) {
    settings.activateProfile(props.profile);
  }
});

async function confirmStop() {
  await sessionRef.value?.confirmStop();
}

function goBack() {
  router.push({ name: 'process-list', params: { profile: props.profile } });
}

type TagType = 'success' | 'info' | 'warning' | 'danger' | 'primary';

function statusColor(s: string): TagType {
  if (s === 'running') return 'success';
  if (s === 'exited') return 'info';
  if (s === 'disconnected') return 'warning';
  return 'primary';
}
</script>

<template>
  <div class="terminal-page">
    <div class="terminal-header">
      <button class="back-btn" @click="goBack">
        <Icon icon="mdi:arrow-left" />
        Process list
      </button>
      <div class="header-meta">
        <h1 class="title">Process {{ pid }}</h1>
        <ElTag :type="statusColor(status)" size="small" effect="light">
          {{ status }}{{ exitCode !== null ? ` (${exitCode})` : '' }}
        </ElTag>
        <span v-if="isPty" class="pty-pill">pty</span>
        <code class="cmd" :title="command">{{ command || '…' }}</code>
        <span class="cwd" :title="workingDir">
          <Icon icon="mdi:folder-outline" />
          <span class="cwd-text">{{ workingDir || '—' }}</span>
        </span>
      </div>
      <ElButton type="danger" plain :disabled="status !== 'running'" @click="confirmStop">
        <Icon icon="mdi:stop" /> &nbsp;Stop
      </ElButton>
    </div>

    <div v-if="status === 'exited'" class="exited-banner">
      This process has exited. Stdin is no longer accepted; the agent's next
      <code>exec_shell_output</code> call will reflect the exit.
    </div>

    <TerminalSession ref="sessionRef" :pid="pid" :show-header="false" class="session-host" />
  </div>
</template>

<style scoped>
.terminal-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #111;
  color: #e5e7eb;
}
.terminal-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 16px;
  border-bottom: 1px solid #1f2937;
  background: #0f172a;
}
.back-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  color: #94a3b8;
  cursor: pointer;
  font-size: 0.875rem;
}
.back-btn:hover {
  color: #e2e8f0;
}
.header-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  overflow: hidden;
}
.title {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
  color: #f8fafc;
  white-space: nowrap;
  flex-shrink: 0;
}
.pty-pill {
  background: #1e293b;
  color: #94a3b8;
  border-radius: 4px;
  padding: 0 6px;
  font-size: 0.7rem;
  flex-shrink: 0;
}
.cmd {
  font-family: Consolas, Monaco, monospace;
  font-size: 0.8rem;
  color: #cbd5f5;
  background: transparent;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1 1 auto;
  min-width: 0;
}
.cwd {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: Consolas, Monaco, monospace;
  font-size: 0.8rem;
  color: #94a3b8;
  white-space: nowrap;
  overflow: hidden;
  flex: 0 1 auto;
  min-width: 0;
}
.cwd-text {
  overflow: hidden;
  text-overflow: ellipsis;
}
.exited-banner {
  padding: 10px 16px;
  background: #78350f;
  color: #fde68a;
  font-size: 0.85rem;
}
.session-host {
  flex: 1;
  min-height: 0;
}
</style>
