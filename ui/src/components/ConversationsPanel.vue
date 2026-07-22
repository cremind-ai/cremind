<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import { ElButton, ElDialog, ElInput, ElMessage, ElMessageBox, ElOption, ElPopover, ElSelect } from 'element-plus';
import { useSettingsStore } from '../stores/settings';
import { useChatStore, type SavedConversation } from '../stores/chat';
import { useNotificationsStore } from '../stores/notifications';
import { useChannelsStore, MAIN_CHANNEL_TYPE, ALL_CHANNELS_FILTER } from '../stores/channels';
import { CONVERSATION_ID_REGEX } from '../services/conversationApi';
import ConversationListItem from './ConversationListItem.vue';

const route = useRoute();
const router = useRouter();
const settingsStore = useSettingsStore();
const chatStore = useChatStore();
const notificationsStore = useNotificationsStore();
const channelsStore = useChannelsStore();

const emit = defineEmits<{
  newChat: [];
}>();

// Lazy-load channels the first time the panel mounts with auth — the filter
// dropdown shows "main" until they load, which is the right default.
watch(
  () => [settingsStore.authToken, settingsStore.profileId] as const,
  ([token, profileId]) => {
    if (!token || !profileId) return;
    channelsStore.loadCatalog().catch(() => {});
    channelsStore.loadChannels().catch(() => {});
  },
  { immediate: true },
);

watch(
  () => channelsStore.activeFilter,
  () => {
    chatStore.applyChannelFilter(channelsStore.activeFilter).catch(() => {});
  },
);

// ── New-chat gating (verbatim from the old sidebar) ──
// New conversations are only allowed under the implicit ``main`` channel (or
// the ``all`` virtual filter, whose new chats land on Main). Specific external
// channels are inbound-only — the backend would 403 a client-created chat.
const isMainChannelSelected = computed(
  () => channelsStore.activeFilter === MAIN_CHANNEL_TYPE,
);
const isAllChannelsSelected = computed(
  () => channelsStore.activeFilter === ALL_CHANNELS_FILTER,
);
const canStartNewChat = computed(
  () => isMainChannelSelected.value || isAllChannelsSelected.value,
);

// True when the user is currently in a brand-new (not yet saved) chat.
const isOnCurrentChat = computed(() =>
  chatStore.activeConversationId === null
  || (chatStore.activeConversationId?.startsWith('temp-') ?? false),
);

const collapse = () => settingsStore.setConversationsPanelCollapsed(true);

const handleNewChat = () => {
  if (!canStartNewChat.value) return;
  emit('newChat');
};

const handleSwitchToCurrentChat = () => {
  if (isOnCurrentChat.value) return;
  if (!canStartNewChat.value) return;
  const profile = route.params.profile as string;
  router.push({ name: 'chat', params: { profile } });
};

const handleSwitchConversation = (id: string) => {
  const profile = route.params.profile as string;
  router.push({ name: 'conversation', params: { profile, conversationId: id } });
};

// ── Per-conversation state helpers (verbatim) ──
const unreadFor = (conversationId: string): number =>
  settingsStore.profileId
    ? notificationsStore.unreadCountForConversation(settingsStore.profileId, conversationId)
    : 0;

const hasErrorFor = (conversationId: string): boolean =>
  settingsStore.profileId
    ? notificationsStore.hasErrorForConversation(settingsStore.profileId, conversationId)
    : false;

const isStreamingConversation = (conversationId: string): boolean =>
  chatStore.streamingConversationIds.has(conversationId);

// ── Ticking "now" — drives relative timestamps AND midnight group boundaries
// off one ref, so the whole list re-renders consistently. ──
const nowMinute = ref(Date.now());
let nowTimer: ReturnType<typeof setInterval> | null = null;
const refreshNow = () => { nowMinute.value = Date.now(); };
onMounted(() => {
  nowTimer = setInterval(refreshNow, 60000);
  document.addEventListener('visibilitychange', refreshNow);
});
onBeforeUnmount(() => {
  if (nowTimer !== null) clearInterval(nowTimer);
  document.removeEventListener('visibilitychange', refreshNow);
});

// ── Search ──
const searchQuery = ref('');
// Threshold-gated: no point cluttering a short list with a search box.
const showSearch = computed(() => chatStore.conversations.length >= 8);
// A stale query silently emptying a freshly-switched channel list looks like
// data loss — clear it on filter switch.
watch(() => channelsStore.activeFilter, () => { searchQuery.value = ''; });

// ── Sort (by last activity) → filter (search) → group (by date) ──
const sortedConversations = computed(() =>
  [...chatStore.conversations].sort((a, b) => b.updatedAt - a.updatedAt),
);

const filteredConversations = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) return sortedConversations.value;
  return sortedConversations.value.filter(
    c => c.title.toLowerCase().includes(q) || c.id.toLowerCase().includes(q),
  );
});

interface ConversationGroup {
  key: string;
  label: string;
  items: SavedConversation[];
}

const groupedConversations = computed<ConversationGroup[]>(() => {
  const startOfToday = new Date(nowMinute.value);
  startOfToday.setHours(0, 0, 0, 0);
  const todayMs = startOfToday.getTime();
  const dayMs = 86_400_000;
  // Ordered most-recent boundary first so the first match wins.
  const groups: Array<ConversationGroup & { min: number }> = [
    { key: 'today', label: 'Today', min: todayMs, items: [] },
    { key: 'yesterday', label: 'Yesterday', min: todayMs - dayMs, items: [] },
    { key: 'week', label: 'This week', min: todayMs - 6 * dayMs, items: [] },
    { key: 'older', label: 'Older', min: -Infinity, items: [] },
  ];
  for (const conv of filteredConversations.value) {
    const g = groups.find(x => conv.updatedAt >= x.min);
    if (g) g.items.push(conv);
  }
  return groups
    .filter(g => g.items.length > 0)
    .map(({ key, label, items }) => ({ key, label, items }));
});

// ── Shared kebab menu (one popover, virtual-ref anchored to the clicked row) ──
const menuConvId = ref<string | null>(null);
const menuTriggerEl = ref<HTMLElement | null>(null);
const menuConvIsTemp = computed(() => menuConvId.value?.startsWith('temp-') ?? false);

const openMenu = (convId: string, el: HTMLElement) => {
  if (menuConvId.value === convId) { menuConvId.value = null; return; }
  menuTriggerEl.value = el;
  menuConvId.value = convId;
};

const closeMenu = () => { menuConvId.value = null; };

const deleteFromMenu = async () => {
  const id = menuConvId.value;
  menuConvId.value = null;
  if (!id) return;
  const conv = chatStore.conversations.find(c => c.id === id);
  const title = conv?.title || id;
  try {
    await ElMessageBox.confirm(
      `Delete “${title}”? This cannot be undone.`,
      'Delete conversation',
      { type: 'warning', confirmButtonText: 'Delete', cancelButtonText: 'Cancel' },
    );
  } catch {
    return; // cancelled
  }
  const wasActive = chatStore.activeConversationId === id;
  await chatStore.deleteConversation(id);
  if (wasActive) {
    const profile = route.params.profile as string;
    router.push({ name: 'chat', params: { profile } });
  }
};

const handleClearAllConversations = async () => {
  const n = chatStore.conversations.length;
  if (n === 0) return;
  // DELETE /api/conversations ignores the channel filter — say so when more
  // than one channel exists so the user knows the blast radius.
  const multiChannel = channelsStore.filterOptions.length > 1;
  const msg = multiChannel
    ? `Delete all ${n} conversations across all channels? This cannot be undone.`
    : `Delete all ${n} conversations? This cannot be undone.`;
  try {
    await ElMessageBox.confirm(msg, 'Clear all conversations', {
      type: 'warning', confirmButtonText: 'Delete all', cancelButtonText: 'Cancel',
    });
  } catch {
    return; // cancelled
  }
  await chatStore.clearAllConversations();
  const profile = route.params.profile as string;
  router.push({ name: 'chat', params: { profile } });
};

// ── Edit-conversation dialog (moved verbatim from the old sidebar) ──
const editingConvId = ref<string | null>(null);
const editIdInput = ref('');
const editTitleInput = ref('');
const editSubmitting = ref(false);

const isEditDialogOpen = computed({
  get: () => editingConvId.value !== null,
  set: (open) => { if (!open) editingConvId.value = null; },
});

const editIdInvalid = computed(() =>
  editIdInput.value !== '' && !CONVERSATION_ID_REGEX.test(editIdInput.value),
);

const editIsDirty = computed(() => {
  if (editingConvId.value === null) return false;
  const conv = chatStore.conversations.find(c => c.id === editingConvId.value);
  if (!conv) return false;
  return editIdInput.value !== conv.id || editTitleInput.value !== conv.title;
});

const openEditFromMenu = () => {
  const id = menuConvId.value;
  menuConvId.value = null;
  if (!id) return;
  const conv = chatStore.conversations.find(c => c.id === id);
  if (!conv) return;
  editingConvId.value = id;
  editIdInput.value = conv.id;
  editTitleInput.value = conv.title;
};

const handleSubmitEditConversation = async () => {
  if (editingConvId.value === null || editSubmitting.value) return;
  if (editIdInput.value === '' || editIdInvalid.value) return;
  const oldId = editingConvId.value;
  const newId = editIdInput.value;
  const newTitle = editTitleInput.value;
  const conv = chatStore.conversations.find(c => c.id === oldId);
  if (!conv) return;
  const idChanged = newId !== oldId;
  const titleChanged = newTitle !== conv.title;
  if (!idChanged && !titleChanged) {
    isEditDialogOpen.value = false;
    return;
  }
  editSubmitting.value = true;
  try {
    if (idChanged) {
      // Pass the title only when the user actually edited it; otherwise the
      // server resets the title to the new id (per the rename contract).
      const titleArg = titleChanged ? newTitle : undefined;
      await chatStore.changeConversationId(oldId, newId, titleArg);
      // If the renamed conversation was active, the chat store has already
      // updated activeConversationId — push the new URL so the route param
      // matches.
      if (chatStore.activeConversationId === newId
          && route.name === 'conversation'
          && route.params.conversationId === oldId) {
        const profile = route.params.profile as string;
        router.push({ name: 'conversation', params: { profile, conversationId: newId } });
      }
    } else {
      await chatStore.renameConversationTitle(oldId, newTitle);
    }
    isEditDialogOpen.value = false;
  } catch (e) {
    const msg = e instanceof Error ? e.message : 'Failed to update conversation';
    ElMessage.error(msg);
  } finally {
    editSubmitting.value = false;
  }
};
</script>

<template>
  <aside class="conversations-panel">
    <div class="panel-header">
      <span class="panel-title">Conversations</span>
      <div class="panel-header-actions">
        <button
          v-if="chatStore.conversations.length > 0"
          class="icon-button"
          title="Clear all conversations"
          @click="handleClearAllConversations"
        >
          <Icon icon="mdi:delete-outline" />
        </button>
        <button
          v-if="canStartNewChat"
          class="icon-button"
          title="New conversation"
          @click="handleNewChat"
        >
          <Icon icon="mdi:plus" />
        </button>
        <button class="icon-button" title="Hide conversations" @click="collapse">
          <Icon icon="mdi:chevron-double-left" />
        </button>
      </div>
    </div>

    <div v-if="channelsStore.filterOptions.length > 1" class="channel-filter">
      <ElSelect
        :model-value="channelsStore.activeFilter"
        size="small"
        @update:model-value="(v) => channelsStore.setFilter(String(v))"
      >
        <ElOption
          v-for="opt in channelsStore.filterOptions"
          :key="opt.value"
          :value="opt.value"
          :label="opt.label"
        >
          <span style="display:inline-flex;align-items:center;gap:6px">
            <Icon v-if="opt.icon" :icon="opt.icon" />
            {{ opt.label }}
          </span>
        </ElOption>
      </ElSelect>
    </div>

    <div v-if="showSearch" class="panel-search">
      <ElInput
        v-model="searchQuery"
        size="small"
        clearable
        placeholder="Search conversations"
      >
        <template #prefix>
          <Icon icon="mdi:magnify" />
        </template>
      </ElInput>
    </div>

    <div class="conversation-list" @scroll="closeMenu">
      <!-- Current / New Chat row (Main / All only), hidden while searching. -->
      <template v-if="canStartNewChat">
        <div
          v-if="!searchQuery"
          class="conversation-item current-chat"
          :class="{ active: isOnCurrentChat }"
          @click="handleSwitchToCurrentChat"
        >
          <Icon icon="mdi:message-text" class="conversation-icon" />
          <div class="conversation-info">
            <div class="conversation-title">{{ isOnCurrentChat ? 'Current Chat' : 'New Chat' }}</div>
            <div class="conversation-preview" v-if="isOnCurrentChat">{{ chatStore.messages.length }} messages</div>
          </div>
        </div>
      </template>
      <div v-else class="channel-empty-hint">
        New conversations are only created from inbound messages on this
        channel. Switch to <strong>Main</strong> to start a new chat.
      </div>

      <!-- Loading skeletons before the first list snapshot resolves. -->
      <template v-if="!chatStore.conversationsLoaded">
        <div v-for="n in 3" :key="'skeleton-' + n" class="skeleton-row">
          <div class="skeleton-icon" />
          <div class="skeleton-lines">
            <div class="skeleton-bar skeleton-bar-title" />
            <div class="skeleton-bar skeleton-bar-sub" />
          </div>
        </div>
      </template>

      <!-- Search: no results. -->
      <div
        v-else-if="searchQuery && filteredConversations.length === 0"
        class="channel-empty-hint"
      >
        No conversations match “{{ searchQuery }}”
      </div>

      <!-- Genuinely empty. -->
      <div
        v-else-if="!searchQuery && sortedConversations.length === 0 && canStartNewChat"
        class="list-empty"
      >
        <Icon icon="mdi:chat-plus-outline" class="list-empty-icon" />
        <div class="list-empty-title">No conversations yet</div>
        <div class="list-empty-hint">Start a new chat to begin</div>
      </div>

      <!-- Grouped conversation list. -->
      <template v-else>
        <template v-for="group in groupedConversations" :key="group.key">
          <div class="conversation-group-header">{{ group.label }}</div>
          <ConversationListItem
            v-for="conv in group.items"
            :key="conv.id"
            :conv="conv"
            :active="chatStore.activeConversationId === conv.id"
            :streaming="isStreamingConversation(conv.id)"
            :unread="unreadFor(conv.id)"
            :has-error="hasErrorFor(conv.id)"
            :now="nowMinute"
            :menu-open="menuConvId === conv.id"
            @select="handleSwitchConversation(conv.id)"
            @menu="(el) => openMenu(conv.id, el)"
          />
        </template>
      </template>
    </div>

    <!-- Shared row-action menu. -->
    <ElPopover
      :visible="menuConvId !== null"
      :virtual-ref="menuTriggerEl"
      virtual-triggering
      placement="bottom-end"
      :width="160"
      popper-class="conv-menu-popover"
      @update:visible="(v) => { if (!v) menuConvId = null; }"
    >
      <div class="conv-menu" role="menu">
        <button
          v-if="!menuConvIsTemp"
          type="button"
          role="menuitem"
          class="conv-menu-item"
          @click="openEditFromMenu"
        >
          <Icon icon="mdi:pencil-outline" class="conv-menu-icon" />
          <span>Edit…</span>
        </button>
        <button
          type="button"
          role="menuitem"
          class="conv-menu-item danger"
          @click="deleteFromMenu"
        >
          <Icon icon="mdi:delete-outline" class="conv-menu-icon" />
          <span>Delete</span>
        </button>
      </div>
    </ElPopover>

    <ElDialog
      v-model="isEditDialogOpen"
      title="Edit conversation"
      width="420px"
      :close-on-click-modal="!editSubmitting"
      append-to-body
    >
      <div class="edit-conversation-field">
        <label class="edit-conversation-label">ID</label>
        <ElInput
          v-model="editIdInput"
          placeholder="lowercase a-z, 0-9, '-', '_'"
          :disabled="editSubmitting"
          @keyup.enter="handleSubmitEditConversation"
        />
        <div class="edit-conversation-help" :class="{ invalid: editIdInvalid }">
          <template v-if="editIdInvalid">
            Must start with a-z or 0-9; only lowercase a-z, digits, '-', or '_'.
          </template>
          <template v-else>
            Renaming the id resets the title to match unless you also edit the title below.
          </template>
        </div>
      </div>
      <div class="edit-conversation-field">
        <label class="edit-conversation-label">Title</label>
        <ElInput
          v-model="editTitleInput"
          placeholder="Conversation title"
          :disabled="editSubmitting"
          @keyup.enter="handleSubmitEditConversation"
        />
      </div>
      <template #footer>
        <ElButton @click="isEditDialogOpen = false" :disabled="editSubmitting">Cancel</ElButton>
        <ElButton
          type="primary"
          :loading="editSubmitting"
          :disabled="editIdInput === '' || editIdInvalid || !editIsDirty"
          @click="handleSubmitEditConversation"
        >
          Save
        </ElButton>
      </template>
    </ElDialog>
  </aside>
</template>

<style scoped>
.conversations-panel {
  width: var(--conversations-panel-width);
  height: 100%;
  background: var(--sidebar-bg);
  border-right: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  flex-shrink: 0;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px 8px;
  flex-shrink: 0;
}

.panel-title {
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-tertiary);
}

.panel-header-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}

.icon-button {
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  transition: all 0.2s ease;
  padding: 0;
  font-size: 18px;
}

.icon-button:hover {
  background: var(--hover-bg);
  color: var(--primary-color);
}

.channel-filter {
  padding: 0 12px 8px 12px;
  flex-shrink: 0;
}
.channel-filter :deep(.el-select) { width: 100%; }

.panel-search {
  padding: 0 12px 8px 12px;
  flex-shrink: 0;
}
.panel-search :deep(.el-input__prefix) {
  display: inline-flex;
  align-items: center;
  color: var(--text-tertiary);
}

.conversation-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0 8px 8px;
}

.conversation-group-header {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--sidebar-bg);
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-tertiary);
  padding: 8px 4px 4px;
}

/* The Current/New Chat row reuses the item look without the list-item comp. */
.conversation-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.2s ease, border-color 0.2s ease;
  border: 1px solid transparent;
}
.conversation-item:hover {
  background: var(--hover-bg);
}
.conversation-item.active {
  background: var(--surface-hover);
  border-color: var(--border-color);
}
.conversation-icon {
  font-size: 18px;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.conversation-item.active .conversation-icon {
  color: var(--primary-color);
}
.conversation-info {
  flex: 1;
  min-width: 0;
}
.conversation-title {
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.conversation-preview {
  font-size: 0.75rem;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.channel-empty-hint {
  padding: 12px 14px;
  font-size: 0.78rem;
  color: var(--text-tertiary);
  line-height: 1.4;
  text-align: center;
}
.channel-empty-hint strong { color: var(--text-secondary); }

.list-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 32px 16px;
  text-align: center;
}
.list-empty-icon {
  font-size: 32px;
  color: var(--text-tertiary);
  opacity: 0.6;
  margin-bottom: 4px;
}
.list-empty-title {
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--text-secondary);
}
.list-empty-hint {
  font-size: 0.78rem;
  color: var(--text-tertiary);
}

/* Loading skeletons */
.skeleton-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
}
.skeleton-icon {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--hover-bg);
  flex-shrink: 0;
  animation: skeleton-pulse 1.4s ease-in-out infinite;
}
.skeleton-lines {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.skeleton-bar {
  height: 8px;
  border-radius: 4px;
  background: var(--hover-bg);
  animation: skeleton-pulse 1.4s ease-in-out infinite;
}
.skeleton-bar-title { width: 70%; }
.skeleton-bar-sub { width: 40%; }

@keyframes skeleton-pulse {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 1; }
}

/* Edit dialog fields */
.edit-conversation-field {
  margin-bottom: 14px;
}
.edit-conversation-field:last-child {
  margin-bottom: 0;
}
.edit-conversation-label {
  display: block;
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--text-secondary);
  margin-bottom: 6px;
}
.edit-conversation-help {
  font-size: 0.75rem;
  color: var(--text-tertiary);
  margin-top: 6px;
  line-height: 1.4;
}
.edit-conversation-help.invalid {
  color: var(--danger-color, #e74c3c);
}
</style>

<!-- Non-scoped: the row-action ElPopover content is teleported to <body>, so
     scoped styles would not reach the .conv-menu rows. -->
<style>
.conv-menu-popover.el-popover.el-popper {
  padding: 6px;
  min-width: 140px;
}

.conv-menu {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.conv-menu .conv-menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  background: transparent;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
  color: var(--text-primary);
  font-size: 0.875rem;
  transition: background 0.15s ease;
}

.conv-menu .conv-menu-item:hover {
  background: var(--hover-bg);
}

.conv-menu .conv-menu-item .conv-menu-icon {
  font-size: 18px;
  color: var(--text-secondary);
  flex-shrink: 0;
}

.conv-menu .conv-menu-item.danger,
.conv-menu .conv-menu-item.danger .conv-menu-icon {
  color: var(--danger-color, #e74c3c);
}
</style>
