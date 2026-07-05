<script setup lang="ts">
import { computed, provide, ref, watch } from 'vue';
import {
  ElDrawer,
  ElButton,
  ElMessage,
  ElMessageBox,
  ElAlert,
  ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useEventRunsStore } from '../../stores/eventRuns';
import { useChatStore } from '../../stores/chat';
import { useSettingsStore } from '../../stores/settings';
import { useTerminalPanelStore } from '../../stores/terminalPanel';
import ChatWindow from '../ChatWindow.vue';
import MessageInput from '../MessageInput.vue';
import RightPanel from '../RightPanel.vue';
import ResizableDivider from '../ResizableDivider.vue';
import ConversationUsagePanel from '../ConversationUsagePanel.vue';
import EventRunStatusTag from './EventRunStatusTag.vue';
import { OpenTerminalKey } from '../../composables/terminalTarget';
import { formatTimestamp, formatTokensCompact, formatUsd } from '../../utils/usageFormat';

const MAXIMIZED_STORAGE_KEY = 'eventRunDrawerMaximized';
const COLLAPSED_WORKSPACE_WIDTH = 36;

const store = useEventRunsStore();
const chat = useChatStore();
const settings = useSettingsStore();
const terminalPanel = useTerminalPanelStore();

const run = computed(() => store.activeRun);
const open = computed({
  get: () => store.activeRunId != null,
  set: (v: boolean) => { if (!v) store.closeRun(); },
});

const cid = computed(() => run.value?.conversation_id ?? null);
const messages = computed(() => (cid.value ? chat.messagesByConversation[cid.value] ?? [] : []));
const isStreaming = computed(() => (cid.value ? !!chat.runtimes[cid.value]?.isStreaming : false));
const cwd = computed(() => {
  const id = cid.value;
  if (!id) return run.value?.trigger_payload?.cwd ?? '';
  return terminalPanel.cwdByConversation[id] ?? '';
});

const usagePanelOpen = ref(false);

// Maximized reveals the full RightPanel workspace (file tree + terminal), like
// the main chat page. Persisted so the preference sticks across runs/sessions.
const maximized = ref(localStorage.getItem(MAXIMIZED_STORAGE_KEY) === '1');
function toggleMaximized() {
  maximized.value = !maximized.value;
  try { localStorage.setItem(MAXIMIZED_STORAGE_KEY, maximized.value ? '1' : '0'); } catch { /* noop */ }
}

const drawerSize = computed(() => {
  const w = typeof window !== 'undefined' ? window.innerWidth : 1200;
  if (maximized.value) return Math.round(w * 0.96);
  return Math.min(860, Math.round(w * 0.92));
});

const workspaceWidth = computed(() =>
  terminalPanel.collapsed ? COLLAPSED_WORKSPACE_WIDTH : terminalPanel.panelWidth,
);

// Terminal chips from the run's messages open into the run-focused workspace
// (the store buckets them under the focused run, so they never leak into the
// main chat), auto-maximizing so the terminal is visible.
provide(OpenTerminalKey, (term) => {
  maximized.value = true;
  try { localStorage.setItem(MAXIMIZED_STORAGE_KEY, '1'); } catch { /* noop */ }
  terminalPanel.openTerminal(term);
});

// ── lifecycle: focus the run's workspace + track its SSE while open ──
let trackedCid: string | null = null;
watch(cid, async (id, prev) => {
  if (prev && prev !== id) {
    chat.untrackConversation(prev, 'manual');
  }
  if (id) {
    chat.trackConversation(id, 'manual');
    terminalPanel.setFocusConversation(id);
    trackedCid = id;
    await chat.loadConversationIntoBucket(id);
    // Seed the runtime task id so Cancel targets a server-started run.
    const rt = chat.runtimes[id];
    if (rt && run.value?.run_id && !rt.currentTaskId) {
      rt.currentTaskId = run.value.run_id;
    }
  } else {
    terminalPanel.setFocusConversation(null);
    trackedCid = null;
  }
}, { immediate: true });

function onClose() {
  if (trackedCid) {
    chat.untrackConversation(trackedCid, 'manual');
    trackedCid = null;
  }
  // Release the workspace focus so the main chat page shows its own
  // conversation's cwd/terminals again.
  terminalPanel.setFocusConversation(null);
}

function onSend(payload: { text: string; attachments: { name: string; path: string }[] }) {
  if (!cid.value) return;
  chat.sendMessage(payload.text, {
    conversationId: cid.value,
    attachments: payload.attachments,
    reasoning: settings.reasoningEnabled ?? true,
  });
}

function onStop() {
  if (cid.value) chat.stopMessage(cid.value);
}

async function cancelRun() {
  if (cid.value) await chat.stopMessage(cid.value);
}

async function deleteRun() {
  const r = run.value;
  if (!r) return;
  try {
    await ElMessageBox.confirm(
      'Delete this run and its conversation? Usage totals are kept in Usage & Cost.',
      'Delete run',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await store.removeRun(r.id);
    ElMessage.success('Run deleted');
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}
</script>

<template>
  <ElDrawer
    v-model="open"
    :size="drawerSize"
    direction="rtl"
    :with-header="false"
    destroy-on-close
    append-to-body
    @closed="onClose"
  >
    <div v-if="run" class="run-drawer">
      <!-- Header -->
      <div class="run-header">
        <div class="run-title-row">
          <EventRunStatusTag :status="run.status" />
          <span class="run-label">{{ run.label }}</span>
          <div class="header-actions">
            <ElButton
              v-if="run.status === 'running'"
              size="small"
              @click="cancelRun"
            >
              <Icon icon="mdi:stop-circle-outline" /> Cancel
            </ElButton>
            <ElButton size="small" @click="usagePanelOpen = true">
              <Icon icon="mdi:chart-box-outline" /> Usage
            </ElButton>
            <ElButton size="small" type="danger" plain @click="deleteRun">
              <Icon icon="mdi:delete-outline" />
            </ElButton>
            <ElTooltip
              :content="maximized ? 'Restore size' : 'Maximize — show workspace'"
              placement="bottom"
              :show-after="300"
            >
              <ElButton size="small" text @click="toggleMaximized">
                <Icon :icon="maximized ? 'mdi:arrow-collapse' : 'mdi:arrow-expand'" />
              </ElButton>
            </ElTooltip>
            <ElButton size="small" text @click="open = false">
              <Icon icon="mdi:close" />
            </ElButton>
          </div>
        </div>
        <div class="run-meta">
          <span>Fired {{ formatTimestamp(run.created_at) }}</span>
          <span v-if="run.finished_at">· Finished {{ formatTimestamp(run.finished_at) }}</span>
          <span>· {{ formatTokensCompact(run.usage.total_tokens) }} tokens</span>
          <span>· {{ formatUsd(run.usage.total_usd) }}</span>
          <span v-if="cwd" class="run-cwd" :title="cwd">· cwd: {{ cwd }}</span>
        </div>
      </div>

      <!-- Pending / failed banners -->
      <ElAlert
        v-if="run.status === 'pending'"
        type="warning"
        :closable="false"
        show-icon
        class="pending-banner"
      >
        <template #title>
          The run is waiting for your reply: {{ run.pending_question }}
        </template>
      </ElAlert>
      <ElAlert
        v-else-if="run.status === 'failed' && run.error"
        type="error"
        :closable="false"
        show-icon
        class="pending-banner"
        :title="run.error"
      />

      <!-- Main: chat column (+ optional workspace when maximized) -->
      <div class="run-main" :class="{ maximized }">
        <div class="run-chat">
          <div class="run-body">
            <ChatWindow
              :messages="messages"
              :is-streaming="isStreaming"
              :conversation-id="cid"
            />
          </div>
          <div class="run-footer">
            <MessageInput
              :conversation-id="cid"
              :is-processing="isStreaming"
              @send="onSend"
              @stop="onStop"
            />
          </div>
        </div>

        <!-- Workspace (file tree + terminal), scoped to this run's conversation -->
        <template v-if="maximized">
          <ResizableDivider
            v-if="!terminalPanel.collapsed"
            @update:width="terminalPanel.setWidth"
          />
          <RightPanel
            class="run-workspace"
            :style="{ width: workspaceWidth + 'px' }"
          />
        </template>
      </div>
    </div>

    <ConversationUsagePanel v-model="usagePanelOpen" :conversation-id="cid" />
  </ElDrawer>
</template>

<style scoped>
.run-drawer {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.run-header {
  padding: 12px 16px 8px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.run-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.run-label {
  font-weight: 600;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.run-meta {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.run-cwd {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pending-banner {
  margin: 8px 16px 0;
  width: auto;
}
.run-main {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.run-main.maximized {
  flex-direction: row;
}
.run-chat {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.run-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.run-footer {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 8px 12px;
}
.run-workspace {
  flex-shrink: 0;
  height: 100%;
}
</style>
