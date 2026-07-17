<script setup lang="ts">
import { onMounted, onBeforeUnmount, computed, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElNotification } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useChatStore } from '../stores/chat';
import { useSettingsStore } from '../stores/settings';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import { useChannelsStore } from '../stores/channels';
import ChatWindow from '../components/ChatWindow.vue';
import MessageInput from '../components/MessageInput.vue';
import RightPanel from '../components/RightPanel.vue';
import ResizableDivider from '../components/ResizableDivider.vue';
import ConversationMemoryPanel from '../components/ConversationMemoryPanel.vue';
import ConversationUsagePanel from '../components/ConversationUsagePanel.vue';
import AgentActivityPanel from '../components/agent/AgentActivityPanel.vue';
import PlanBanner from '../components/plan/PlanBanner.vue';
import PlanApprovalDialog from '../components/plan/PlanApprovalDialog.vue';
import AskUserQuestionDialog from '../components/plan/AskUserQuestionDialog.vue';

const props = defineProps<{
  profile?: string;
  conversationId?: string;
}>();

const router = useRouter();
const chatStore = useChatStore();
const settingsStore = useSettingsStore();
const terminalPanel = useTerminalPanelStore();
const channelsStore = useChannelsStore();

// Channel context for the active conversation. Drives the read-only banner
// and disables MessageInput for any non-main (i.e. external) conversation.
//
// Reads ``chatStore.activeChannelId`` (a sticky per-conversation cache),
// NOT the filtered ``chatStore.conversations`` list. Switching the sidebar
// filter to "Main" drops the Telegram conversation from that list while
// the URL still points at it; reading from the filtered list would flip
// the banner off and re-enable MessageInput by mistake.
const activeChannel = computed(() => {
  const channelId = chatStore.activeChannelId;
  if (!channelId) return null;
  return channelsStore.channelById(channelId) || null;
});

// "External" covers two cases:
//   1) An existing conversation whose channel is non-main (Telegram, etc.).
//   2) The empty new-chat slot (no active conversation id) while the
//      sidebar filter is set to a specific external channel — there's
//      nothing to type into here because new conversations are only
//      allowed under Main, and the backend would 403 the create anyway.
//      The ``all`` virtual filter is treated like Main for this slot:
//      new chats spawn under Main but show up in the All list.
const isExternalChannel = computed(() => {
  const ch = activeChannel.value;
  if (ch && ch.channel_type !== 'main') return true;
  if (!chatStore.activeConversationId
      && channelsStore.activeFilter !== 'main'
      && channelsStore.activeFilter !== 'all') {
    return true;
  }
  return false;
});

const externalChannelLabel = computed(() => {
  const ch = activeChannel.value;
  if (ch) {
    return channelsStore.catalog[ch.channel_type]?.display_name || ch.channel_type;
  }
  // New-chat slot under a specific external filter — fall back to the
  // filter type. ``all`` is not external (new chats go to Main).
  const filter = channelsStore.activeFilter;
  if (filter && filter !== 'main' && filter !== 'all') {
    return channelsStore.catalog[filter]?.display_name || filter;
  }
  return '';
});

// The right panel hosts the file tree (always available) plus the optional
// terminal section. It's open by default and can be hidden via its minimize
// button; the restore pill brings it back regardless of terminal count.
// The collapse button shrinks it to a thin strip while keeping it visible.
const COLLAPSED_PANEL_WIDTH = 36;
const showRightPanel = computed(() => !terminalPanel.minimized);
const showMinimizedPill = computed(() => terminalPanel.minimized);
const rightPanelWidth = computed(() =>
  terminalPanel.collapsed ? COLLAPSED_PANEL_WIDTH : terminalPanel.panelWidth,
);

// Detect if running in Electron
const isElectron = computed(() => {
  return typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__;
});

// Per-conversation memory panel (short-term for this chat + long-term for the
// profile). Only meaningful for a saved conversation, so the button is hidden
// on the empty new-chat slot.
const memoryPanelOpen = ref(false);
const showMemoryButton = computed(() => !!chatStore.activeConversationId);
const openMemoryPanel = () => { memoryPanelOpen.value = true; };

const usagePanelOpen = ref(false);
const openUsagePanel = () => { usagePanelOpen.value = true; };

onMounted(async () => {
  // Right panel (file tree) is visible by default on entering Conversations.
  terminalPanel.restore();
  // Guard: the event-run drawer focuses the workspace on a hidden run
  // conversation while open; ensure returning to the main chat always shows
  // the active conversation's workspace, never a stale run focus.
  terminalPanel.setFocusConversation(null);

  // Connect if we have active credentials but aren't connected yet
  if (settingsStore.profileId && settingsStore.authToken && !chatStore.isConnected) {
    await handleConnect();
  }

  // Deep-link: if URL contains a conversationId, load it
  if (props.conversationId) {
    if (chatStore.activeConversationId !== props.conversationId) {
      try {
        await chatStore.switchConversation(props.conversationId);
      } catch (e) {
        router.replace({ name: 'chat', params: { profile: props.profile } });
      }
    } else {
      // Re-attach the 'active' tracker after a previous unmount (e.g. user
      // came back from Settings to the same conversation). Idempotent.
      chatStore.trackConversation(props.conversationId, 'active');
    }
  } else if (chatStore.activeConversationId) {
    // Bare chat route (no conversationId) but a conversation is still active
    // from before we navigated away (e.g. returned from Settings to the
    // landing page, not the conversation). Clear it so the active highlight
    // and "New Chat" state stay consistent with the URL — the route watcher
    // below only fires on prop *changes*, not on this fresh mount.
    chatStore.switchToNewChat();
  }
});

// Watch route param changes (in-app navigation between conversations)
watch(() => props.conversationId, async (newId, oldId) => {
  if (newId === oldId) return;
  if (newId) {
    if (chatStore.activeConversationId !== newId) {
      try {
        await chatStore.switchConversation(newId);
      } catch (e) {
        router.replace({ name: 'chat', params: { profile: props.profile } });
      }
    }
  } else {
    // Navigated to /:profile (no conversation) - switch to new chat
    chatStore.switchToNewChat();
  }
});

// Sync URL when activeConversationId changes (e.g., after first message creates a conversation)
watch(() => chatStore.activeConversationId, (newId) => {
  const currentRouteConvId = props.conversationId;
  if (newId && newId !== currentRouteConvId) {
    router.replace({ name: 'conversation', params: { profile: props.profile, conversationId: newId } });
  } else if (!newId && currentRouteConvId) {
    router.replace({ name: 'chat', params: { profile: props.profile } });
  }
});

// Drop the 'active' tracker on unmount so navigating to Settings doesn't
// keep an idle SSE open. Any 'streaming' tracker (live run in flight) keeps
// the connection alive on its own — this only releases the view's hold.
onBeforeUnmount(() => {
  const id = chatStore.activeConversationId;
  if (id) chatStore.untrackConversation(id, 'active');
});

const handleConnect = async () => {
  try {
    await chatStore.connect();
    ElNotification({
      title: 'Connected',
      message: `Connected to ${chatStore.agentName}`,
      type: 'success',
      duration: 2000,
    });
  } catch (error: any) {
    ElNotification({
      title: 'Connection Failed',
      message: error.message || 'Failed to connect to agent',
      type: 'error',
      duration: 4000,
    });
  }
};

const handleSendMessage = async (
  payload: { text: string; attachments: { name: string; path: string }[] },
) => {
  try {
    await chatStore.sendMessage(payload.text, {
      mode: settingsStore.chatMode,
      attachments: payload.attachments,
    });
  } catch (error: any) {
    ElNotification({
      title: 'Error',
      message: error.message || 'Failed to send message',
      type: 'error',
    });
  }
};

// ── plan mode dialogs ──
const questionDialogOpen = ref(false);
const planDialogOpen = ref(false);

// Auto-open the question form when a new question set arrives (identity = the
// createdAt stamp), so dismissing it doesn't immediately reopen.
watch(
  () => chatStore.activePendingQuestion?.createdAt,
  (stamp) => {
    if (stamp) questionDialogOpen.value = true;
  },
);

const acceptPlan = async () => {
  planDialogOpen.value = false;
  await chatStore.sendMessage(
    'I accept the plan. Please execute it to completion.',
    { mode: 'plan', planAction: 'accept' },
  );
};

const cancelPlan = async () => {
  planDialogOpen.value = false;
  const cid = chatStore.activeConversationId;
  if (cid) await chatStore.cancelPlanApproval(cid);
};
</script>

<template>
  <div class="chat-view" :class="{ 'has-titlebar': isElectron, split: showRightPanel }">
    <div class="chat-section">
      <button
        v-if="showMemoryButton"
        class="memory-button"
        :title="'View conversation memory'"
        @click="openMemoryPanel"
      >
        <Icon icon="mdi:brain" />
      </button>

      <button
        v-if="showMemoryButton"
        class="memory-button usage-button"
        :title="'View token usage & cost'"
        @click="openUsagePanel"
      >
        <Icon icon="mdi:chart-box-outline" />
      </button>

      <ChatWindow
        :messages="chatStore.messages"
        :isStreaming="chatStore.isStreaming"
      />

      <PlanBanner
        v-if="!isExternalChannel"
        @review="planDialogOpen = true"
        @answer="questionDialogOpen = true"
        @accept="acceptPlan"
        @cancel="cancelPlan"
      />

      <div v-if="isExternalChannel" class="readonly-banner">
        <Icon icon="mdi:lock-outline" />
        <span v-if="chatStore.activeConversationId">
          Read-only — incoming messages from <strong>{{ externalChannelLabel }}</strong>.
          Replies are sent automatically.
        </span>
        <span v-else>
          New conversations on <strong>{{ externalChannelLabel }}</strong> are
          only created from inbound platform messages. Switch the sidebar
          filter to <strong>Main</strong> to start a new chat.
        </span>
      </div>
      <MessageInput
        v-else
        :disabled="!chatStore.isConnected || chatStore.isStreaming"
        :isProcessing="chatStore.isStreaming"
        :mode="settingsStore.chatMode"
        @update:mode="settingsStore.setChatMode(settingsStore.profileId, $event)"
        @send="handleSendMessage"
        @stop="chatStore.stopMessage()"
      />

      <div
        v-if="chatStore.activeAgentActivity"
        class="floating-panels"
      >
        <AgentActivityPanel
          v-if="chatStore.activeAgentActivity"
          :state="chatStore.activeAgentActivity"
          @dismiss="chatStore.dismissAgentActivity(chatStore.activeConversationId!)"
        />
      </div>

      <button
        v-if="showMinimizedPill"
        class="terminal-restore-pill"
        :title="'Show workspace panel'"
        @click="terminalPanel.restore()"
      >
        <Icon icon="mdi:dock-right" />
        <span>
          Workspace<template v-if="terminalPanel.openTerminals.length > 0">
            ({{ terminalPanel.openTerminals.length }})</template>
        </span>
      </button>
    </div>

    <template v-if="showRightPanel">
      <ResizableDivider
        v-if="!terminalPanel.collapsed"
        @update:width="terminalPanel.setWidth"
      />
      <RightPanel
        class="right-panel-host"
        :style="{ width: rightPanelWidth + 'px' }"
      />
    </template>

    <ConversationMemoryPanel
      v-model="memoryPanelOpen"
      :conversation-id="chatStore.activeConversationId"
    />

    <ConversationUsagePanel
      v-model="usagePanelOpen"
      :conversation-id="chatStore.activeConversationId"
    />

    <AskUserQuestionDialog v-model="questionDialogOpen" />

    <PlanApprovalDialog
      v-model="planDialogOpen"
      @accept="acceptPlan"
      @cancel="cancelPlan"
    />
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 100%;
  overflow: hidden;
}

/* When the terminal panel is visible, switch to a horizontal split layout. */
.chat-view.split {
  flex-direction: row;
}

.chat-section {
  position: relative;
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

/* Floating stack for the Agent Activity panel. Absolutely positioned top-right
   of the chat column. (The todo panels moved to the app-global
   FloatingTodoLayer window manager, which supports multiple overlapping
   panels + per-turn history chips.) */
.floating-panels {
  position: absolute;
  top: 12px;
  right: 16px;
  z-index: 6;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
  max-width: calc(100% - 32px);
  max-height: calc(100% - 140px);
  pointer-events: none;
}

/* Re-enable interaction on the panels themselves (the wrapper is click-through
   so it never blocks the chat behind the empty gap between panels). */
.floating-panels > * {
  pointer-events: auto;
}

.right-panel-host {
  flex-shrink: 0;
  height: 100%;
}

.terminal-restore-pill {
  position: absolute;
  right: 16px;
  bottom: 72px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: #0f172a;
  color: #cbd5f5;
  border: 1px solid #1f2937;
  border-radius: 999px;
  font-size: 0.8rem;
  cursor: pointer;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
  z-index: 5;
}
.terminal-restore-pill:hover {
  border-color: var(--primary-color);
  color: #e5e7eb;
}

.readonly-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  background: var(--hover-bg);
  border-top: 1px solid var(--border-color);
  font-size: 0.85rem;
  color: var(--text-secondary);
}
.readonly-banner :deep(svg) { font-size: 16px; }

/* Floating memory toggle, top-right of the chat area. */
.memory-button {
  position: absolute;
  top: 12px;
  left: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  background: var(--surface-color);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  cursor: pointer;
  z-index: 6;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
}
.memory-button:hover {
  border-color: var(--primary-color);
  color: var(--primary-color);
}
.memory-button :deep(svg) { font-size: 18px; }
.usage-button { left: 58px; }
</style>
