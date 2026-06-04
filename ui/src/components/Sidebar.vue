<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import { ElBadge, ElButton, ElDialog, ElInput, ElMessage, ElOption, ElPopover, ElSelect, ElSwitch, ElTooltip } from 'element-plus';
import { useSettingsStore } from '../stores/settings';
import { useChatStore } from '../stores/chat';
import { useNotificationsStore } from '../stores/notifications';
import { useChannelsStore, MAIN_CHANNEL_TYPE, ALL_CHANNELS_FILTER } from '../stores/channels';
import AgentCard from './AgentCard.vue';
import NotificationList from './NotificationList.vue';
import { openNotificationsStream, type NotificationStreamHandle } from '../services/notificationsStream';
import { CONVERSATION_ID_REGEX } from '../services/conversationApi';

const route = useRoute();
const router = useRouter();
const settingsStore = useSettingsStore();
const chatStore = useChatStore();
const notificationsStore = useNotificationsStore();
const channelsStore = useChannelsStore();

// Lazy-load channels the first time the sidebar mounts with auth — the
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

const handleOpenChannels = () => {
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'channels-page', params: { profile } });
};

const handleOpenAbout = () => {
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'about', params: { profile } });
};

const handleOpenUpdates = () => {
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'updates', params: { profile } });
};

// New conversations are only allowed under the implicit ``main`` channel.
// External channels (Telegram, etc.) only ever spawn conversations from
// inbound platform messages — the UI never POSTs to /api/conversations on
// their behalf, and the backend would reject it with 403 anyway.
const isMainChannelSelected = computed(
  () => channelsStore.activeFilter === MAIN_CHANNEL_TYPE,
);

const isAllChannelsSelected = computed(
  () => channelsStore.activeFilter === ALL_CHANNELS_FILTER,
);

// New chats always belong to Main, but they're also creatable from the
// ``all`` virtual filter (the resulting conversation just appears in the
// All list once it's saved). Specific external-channel filters (Telegram,
// etc.) stay read-only — those conversations can only be spawned by
// inbound platform messages.
const canStartNewChat = computed(
  () => isMainChannelSelected.value || isAllChannelsSelected.value,
);

let notificationsStream: NotificationStreamHandle | null = null;

onMounted(() => {
  notificationsStore.hydrate();
});

// Open the notifications stream reactively to auth state. The Sidebar mounts
// before App.vue's onMounted finishes activating the profile on direct page
// load, so a one-shot onMounted subscription would silently abort with no
// authToken and never retry. Watching the auth deps with immediate:true
// fires once on mount (returning early if no token yet) and again the moment
// the token becomes available, so server-triggered runs (skill events) can
// reach the sidebar even when the user navigated straight to a deep link.
const closeNotificationsStream = () => {
  if (notificationsStream !== null) {
    notificationsStream.close();
    notificationsStream = null;
  }
};

// Bell arrival animation. Holds the priority of the most-recent unseen
// arrival; cleared after the keyframe duration so a subsequent arrival of
// the same priority can re-trigger the animation. ``bellAnimKey`` forces a
// node rebind so the keyframe restarts even when ``bellPulse`` doesn't change.
const bellPulse = ref<'high' | 'normal' | null>(null);
const bellAnimKey = ref(0);
let bellPulseTimer: ReturnType<typeof setTimeout> | null = null;

const triggerBellAnimation = (priority: 'high' | 'normal') => {
  if (bellPulseTimer !== null) clearTimeout(bellPulseTimer);
  bellPulse.value = priority;
  bellAnimKey.value += 1;
  // High runs ~1.2s × 3 cycles, normal runs ~0.6s × 1 cycle (see CSS below).
  const duration = priority === 'high' ? 3600 : 600;
  bellPulseTimer = setTimeout(() => {
    bellPulse.value = null;
    bellPulseTimer = null;
  }, duration);
};

watch(
  () => [settingsStore.authToken, settingsStore.profileId] as const,
  ([token, profileId]) => {
    closeNotificationsStream();
    if (!token || !profileId) return;
    notificationsStream = openNotificationsStream(
      settingsStore.agentUrl,
      token,
      Date.now(),
      (entry) => {
        console.log('[debug:notif] received entry', {
          kind: entry.kind,
          conversation_id: entry.conversation_id,
          id: entry.id,
          created_at: entry.created_at,
        });
        // Server-triggered runs (skill events) have no client POST that
        // would open the per-conversation SSE. The 'started' kind exists so
        // the sidebar can lazily open that SSE and the streaming-dot lights
        // up even for conversations the user has never visited this session.
        if (entry.kind === 'started') {
          console.log('[debug:notif] handling started for', entry.conversation_id);
          const { runtime } = chatStore.ensureBucket(entry.conversation_id);
          runtime.isStreaming = true;
          runtime.startedAt = Date.now();
          chatStore.trackConversation(entry.conversation_id, 'streaming');
          return;
        }
        // The user is already watching this conversation — record the
        // notification but pre-mark it seen so the bell badge doesn't bump
        // and the arrival animation stays quiet.
        const isActive = entry.conversation_id !== ''
          && entry.conversation_id === chatStore.activeConversationId;
        const priority: 'high' | 'normal' = entry.priority === 'high' ? 'high' : 'normal';
        notificationsStore.push(profileId, {
          id: entry.id,
          conversationId: entry.conversation_id,
          conversationTitle: entry.conversation_title,
          messagePreview: entry.message_preview,
          kind: entry.kind,
          priority,
          createdAt: entry.created_at,
          seen: isActive,
          channelType: entry.channel_type,
          senderId: entry.sender_id,
          senderName: entry.sender_name,
          otp: entry.otp,
          skillId: entry.skill_id,
          skillName: entry.skill_name,
        });
        if (!isActive) triggerBellAnimation(priority);
      },
    );
  },
  { immediate: true },
);

onBeforeUnmount(closeNotificationsStream);

const bellPopoverVisible = ref(false);
const notificationsTriggerRef = ref<HTMLDivElement | null>(null);

const totalUnread = computed(() =>
  settingsStore.profileId ? notificationsStore.totalUnread(settingsStore.profileId) : 0,
);

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

const handleSelectNotification = (conversationId: string) => {
  bellPopoverVisible.value = false;
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'conversation', params: { profile, conversationId } });
};

const handleSelectSkillNotification = (skillId: string) => {
  bellPopoverVisible.value = false;
  const profile = route.params.profile as string;
  if (!profile || !skillId) return;
  router.push({
    name: 'tools-skills-settings',
    params: { profile },
    query: { skillId, tour: '1' },
  });
};

const handleDismissNotification = (id: string) => {
  if (settingsStore.profileId) {
    notificationsStore.dismiss(settingsStore.profileId, id);
  }
};

// True when the user is currently in a brand-new (not yet saved) chat.
// Either nothing has been sent (active === null) or a temp-id is in flight.
const isOnCurrentChat = computed(() =>
  chatStore.activeConversationId === null
  || (chatStore.activeConversationId?.startsWith('temp-') ?? false),
);

const emit = defineEmits<{
  openSettings: [];
  newChat: [];
  logout: [];
}>();

const handleThemeToggle = (val: string | number | boolean) => {
  const isDark = val === true;
  settingsStore.setTheme(isDark ? 'dark' : 'light');
};

const handleNewChat = () => {
  // Defence in depth — the button is hidden under specific external
  // filters, but a stale event handler or external programmatic call
  // shouldn't be able to spawn a new chat that the backend would just
  // reject anyway. ``all`` is allowed because new chats from there land
  // on Main.
  if (!canStartNewChat.value) return;
  emit('newChat');
};

const handleOpenSettings = () => {
  // Navigate to settings page instead of opening drawer
  emit('openSettings');
};

const handleOpenProcessManager = () => {
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'process-list', params: { profile } });
};

const handleOpenEvents = () => {
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'skill-events', params: { profile } });
};

// Developer page is admin-only — backend's require_admin would 403
// non-admin profiles, and the route guard already redirects them.
// Hiding the sidebar row keeps them from ever seeing the entry point.
const isAdminProfile = computed(() => (route.params.profile as string) === 'admin');

const handleOpenDeveloper = () => {
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'developer', params: { profile } });
};

const handleLogout = () => {
  emit('logout');
};

const handleSwitchToCurrentChat = () => {
  // No-op if we're already on the current/new chat (whether at the empty
  // landing slot or in a still-temp-id new chat). Otherwise navigate to /.
  if (isOnCurrentChat.value) return;
  // Same channel guard as ``handleNewChat`` — under a specific external
  // filter the empty new-chat slot would just produce a 403 on send.
  if (!canStartNewChat.value) return;
  const profile = route.params.profile as string;
  router.push({ name: 'chat', params: { profile } });
};

const handleSwitchConversation = (id: string) => {
  const profile = route.params.profile as string;
  router.push({ name: 'conversation', params: { profile, conversationId: id } });
};

const handleDeleteConversation = async (id: string, event: Event) => {
  event.stopPropagation();
  const wasActive = chatStore.activeConversationId === id;
  await chatStore.deleteConversation(id);
  if (wasActive) {
    const profile = route.params.profile as string;
    router.push({ name: 'chat', params: { profile } });
  }
};

// Edit-conversation dialog state. Only one conversation is edited at a time,
// so a single ref to the original id (or null when closed) is enough.
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

const handleOpenEditConversation = (id: string, event: Event) => {
  event.stopPropagation();
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

const handleClearAllConversations = async () => {
  if (chatStore.conversations.length === 0) return;
  if (confirm('Clear all saved conversations?')) {
    await chatStore.clearAllConversations();
    const profile = route.params.profile as string;
    router.push({ name: 'chat', params: { profile } });
  }
};

const sortedConversations = computed(() => {
  return [...chatStore.conversations].sort((a, b) => b.createdAt - a.createdAt);
});

const isCollapsed = computed(() => settingsStore.sidebarCollapsed);

const toggleCollapsed = () => {
  settingsStore.setSidebarCollapsed(!isCollapsed.value);
};

const toggleThemeFromIcon = () => {
  settingsStore.setTheme(settingsStore.theme === 'dark' ? 'light' : 'dark');
};
</script>

<template>
  <aside class="sidebar" :class="{ collapsed: isCollapsed }">
    <!-- Agent Connection Panel -->
    <div class="sidebar-section agent-section">
      <AgentCard
        :agentCard="chatStore.agentCard"
        :isConnected="chatStore.isConnected"
        :compact="isCollapsed"
        @connect="chatStore.connect"
        @disconnect="chatStore.disconnect"
      >
        <template #header-action>
          <ElTooltip
            :content="isCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
            placement="right"
            :show-after="300"
          >
            <button class="collapse-button" @click.stop="toggleCollapsed">
              <Icon :icon="isCollapsed ? 'mdi:chevron-double-right' : 'mdi:chevron-double-left'" />
            </button>
          </ElTooltip>
        </template>
      </AgentCard>
    </div>

    <!-- Conversation History -->
    <div class="sidebar-section conversations-section">
      <div class="section-header" v-if="!isCollapsed">
        <span class="section-title">Conversations</span>
        <div class="section-header-actions">
          <button class="icon-button" @click="handleClearAllConversations" title="Clear all conversations" v-if="chatStore.conversations.length > 0">
            <Icon icon="mdi:delete-outline" />
          </button>
          <button
            v-if="canStartNewChat"
            class="icon-button"
            @click="handleNewChat"
            title="New conversation"
          >
            <Icon icon="mdi:plus" />
          </button>
        </div>
      </div>
      <div
        v-if="!isCollapsed && channelsStore.filterOptions.length > 1"
        class="channel-filter"
      >
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
      <div class="section-header collapsed-header" v-else>
        <ElTooltip
          v-if="canStartNewChat"
          content="New conversation"
          placement="right"
          :show-after="300"
        >
          <button class="icon-button" @click="handleNewChat">
            <Icon icon="mdi:plus" />
          </button>
        </ElTooltip>
      </div>
      <div class="conversation-list">
        <!-- Current Chat / New Chat row — shown on Main and on the ``All``
             virtual filter (new chats from there land on Main). Specific
             external-channel filters are inbound-only; new conversations
             there are spawned by platform messages, not by the user
             clicking this row. -->
        <ElTooltip
          v-if="canStartNewChat"
          :content="isOnCurrentChat ? 'Current Chat' : 'New Chat'"
          placement="right"
          :show-after="300"
          :disabled="!isCollapsed"
        >
          <div
            class="conversation-item"
            :class="{ active: isOnCurrentChat }"
            @click="handleSwitchToCurrentChat"
          >
            <Icon icon="mdi:message-text" class="conversation-icon" />
            <div class="conversation-info" v-if="!isCollapsed">
              <div class="conversation-title">{{ isOnCurrentChat ? 'Current Chat' : 'New Chat' }}</div>
              <div class="conversation-preview" v-if="isOnCurrentChat">{{ chatStore.messages.length }} messages</div>
            </div>
          </div>
        </ElTooltip>
        <div
          v-else-if="!isCollapsed"
          class="channel-empty-hint"
        >
          New conversations are only created from inbound messages on
          this channel. Switch to <strong>Main</strong> to start a new
          chat.
        </div>
        <!-- Saved conversations -->
        <ElTooltip
          v-for="conv in sortedConversations"
          :key="conv.id"
          :content="conv.title"
          placement="right"
          :show-after="300"
          :disabled="!isCollapsed"
        >
          <ElBadge
            :value="unreadFor(conv.id)"
            :hidden="unreadFor(conv.id) === 0"
            :max="9"
            :type="hasErrorFor(conv.id) ? 'danger' : 'primary'"
            class="conversation-badge-wrap"
          >
            <div
              class="conversation-item"
              :class="{ active: chatStore.activeConversationId === conv.id, streaming: isStreamingConversation(conv.id) }"
              @click="handleSwitchConversation(conv.id)"
            >
              <Icon icon="mdi:message-text-outline" class="conversation-icon" />
              <span
                v-if="isStreamingConversation(conv.id)"
                class="streaming-dot"
                title="Streaming"
              />
              <div class="conversation-info" v-if="!isCollapsed">
                <div class="conversation-title">{{ conv.title }}</div>
                <div class="conversation-preview">{{ conv.messageCount ?? 0 }} messages</div>
              </div>
              <button v-if="!isCollapsed && !conv.id.startsWith('temp-')" class="edit-conversation-btn" @click="handleOpenEditConversation(conv.id, $event)" title="Edit conversation">
                <Icon icon="mdi:pencil-outline" />
              </button>
              <button v-if="!isCollapsed" class="delete-conversation-btn" @click="handleDeleteConversation(conv.id, $event)" title="Delete conversation">
                <Icon icon="mdi:close" />
              </button>
            </div>
          </ElBadge>
        </ElTooltip>
      </div>
    </div>

    <!-- Bottom Actions -->
    <div class="sidebar-section bottom-section">
      <ElTooltip
        content="Notifications"
        placement="right"
        :show-after="300"
        :disabled="!isCollapsed"
      >
        <div
          ref="notificationsTriggerRef"
          class="settings-row notifications-row"
          @click="bellPopoverVisible = !bellPopoverVisible"
        >
          <ElBadge
            :value="totalUnread"
            :hidden="totalUnread === 0"
            :max="99"
            class="notifications-badge"
            :class="{ 'badge-pulse-high': bellPulse === 'high' }"
          >
            <Icon
              :key="bellAnimKey"
              icon="mdi:bell-outline"
              class="settings-icon bell-icon"
              :class="{
                'bell-shake-normal': bellPulse === 'normal',
                'bell-shake-high': bellPulse === 'high',
              }"
            />
          </ElBadge>
          <span class="settings-label" v-if="!isCollapsed">Notifications</span>
          <Icon icon="mdi:chevron-right" class="chevron-icon" v-if="!isCollapsed" />
        </div>
      </ElTooltip>
      <ElPopover
        :visible="bellPopoverVisible"
        :virtual-ref="notificationsTriggerRef"
        virtual-triggering
        placement="right-end"
        :width="320"
        @update:visible="bellPopoverVisible = $event"
      >
        <NotificationList
          @select="handleSelectNotification"
          @select-skill="handleSelectSkillNotification"
          @dismiss="handleDismissNotification"
        />
      </ElPopover>
      <ElTooltip content="Process Manager" placement="right" :show-after="300" :disabled="!isCollapsed">
        <div class="settings-row" @click="handleOpenProcessManager">
          <Icon icon="mdi:console" class="settings-icon" />
          <span class="settings-label" v-if="!isCollapsed">Process Manager</span>
          <Icon icon="mdi:chevron-right" class="chevron-icon" v-if="!isCollapsed" />
        </div>
      </ElTooltip>
      <ElTooltip v-if="isAdminProfile" content="Developer" placement="right" :show-after="300" :disabled="!isCollapsed">
        <div class="settings-row" @click="handleOpenDeveloper">
          <Icon icon="mdi:bug-outline" class="settings-icon" />
          <span class="settings-label" v-if="!isCollapsed">Developer</span>
          <Icon icon="mdi:chevron-right" class="chevron-icon" v-if="!isCollapsed" />
        </div>
      </ElTooltip>
      <ElTooltip content="Events" placement="right" :show-after="300" :disabled="!isCollapsed">
        <div class="settings-row" @click="handleOpenEvents">
          <Icon icon="mdi:lightning-bolt-outline" class="settings-icon" />
          <span class="settings-label" v-if="!isCollapsed">Events</span>
          <Icon icon="mdi:chevron-right" class="chevron-icon" v-if="!isCollapsed" />
        </div>
      </ElTooltip>
      <ElTooltip content="Channels" placement="right" :show-after="300" :disabled="!isCollapsed">
        <div class="settings-row" @click="handleOpenChannels">
          <Icon icon="mdi:link-variant" class="settings-icon" />
          <span class="settings-label" v-if="!isCollapsed">Channels</span>
          <Icon icon="mdi:chevron-right" class="chevron-icon" v-if="!isCollapsed" />
        </div>
      </ElTooltip>
      <ElTooltip content="Updates" placement="right" :show-after="300" :disabled="!isCollapsed">
        <div class="settings-row" @click="handleOpenUpdates">
          <Icon icon="mdi:download" class="settings-icon" />
          <span class="settings-label" v-if="!isCollapsed">Updates</span>
          <Icon icon="mdi:chevron-right" class="chevron-icon" v-if="!isCollapsed" />
        </div>
      </ElTooltip>
      <ElTooltip content="About" placement="right" :show-after="300" :disabled="!isCollapsed">
        <div class="settings-row" @click="handleOpenAbout">
          <Icon icon="mdi:information-outline" class="settings-icon" />
          <span class="settings-label" v-if="!isCollapsed">About</span>
          <Icon icon="mdi:chevron-right" class="chevron-icon" v-if="!isCollapsed" />
        </div>
      </ElTooltip>
      <ElTooltip
        :content="settingsStore.theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'"
        placement="right"
        :show-after="300"
        :disabled="!isCollapsed"
      >
        <div
          class="theme-row"
          :class="{ clickable: isCollapsed }"
          @click="isCollapsed ? toggleThemeFromIcon() : null"
        >
          <Icon
            :icon="settingsStore.theme === 'dark' ? 'mdi:weather-night' : 'mdi:weather-sunny'"
            class="theme-icon"
          />
          <span class="theme-label" v-if="!isCollapsed">Dark Mode</span>
          <ElSwitch
            v-if="!isCollapsed"
            :model-value="settingsStore.theme === 'dark'"
            @change="handleThemeToggle"
            size="small"
          />
        </div>
      </ElTooltip>
      <ElTooltip content="Settings" placement="right" :show-after="300" :disabled="!isCollapsed">
        <div class="settings-row" @click="handleOpenSettings">
          <Icon icon="mdi:cog" class="settings-icon" />
          <span class="settings-label" v-if="!isCollapsed">Settings</span>
          <Icon icon="mdi:chevron-right" class="chevron-icon" v-if="!isCollapsed" />
        </div>
      </ElTooltip>
      <ElTooltip content="Logout" placement="right" :show-after="300" :disabled="!isCollapsed">
        <div class="settings-row logout-row" @click="handleLogout">
          <Icon icon="mdi:logout" class="settings-icon" />
          <span class="settings-label" v-if="!isCollapsed">Logout</span>
          <Icon icon="mdi:chevron-right" class="chevron-icon" v-if="!isCollapsed" />
        </div>
      </ElTooltip>
    </div>

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
.sidebar {
  width: var(--sidebar-width);
  height: 100%;
  background: var(--sidebar-bg);
  border-right: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  flex-shrink: 0;
  transition: width 0.2s ease;
}

.sidebar.collapsed {
  width: var(--sidebar-width-collapsed);
}

.collapse-button {
  width: 28px;
  height: 28px;
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

.collapse-button:hover {
  background: var(--hover-bg);
  color: var(--primary-color);
}

.sidebar-section {
  padding: 12px;
  border-bottom: 1px solid var(--border-color);
}

.sidebar-section:last-child {
  border-bottom: none;
}

.agent-section {
  padding: 0;
}

.conversations-section {
  flex: 1;
  overflow-y: auto;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  padding: 0 4px;
}

.section-header-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}

.section-title {
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-tertiary);
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
}

.icon-button:hover {
  background: var(--hover-bg);
  color: var(--primary-color);
}

.channel-filter {
  padding: 0 12px 8px 12px;
}
.channel-filter :deep(.el-select) { width: 100%; }

.channel-empty-hint {
  padding: 12px 14px;
  font-size: 0.78rem;
  color: var(--text-tertiary);
  line-height: 1.4;
  text-align: center;
}
.channel-empty-hint strong { color: var(--text-secondary); }

.conversation-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

/* ElBadge wraps content in an inline-block; force it to fill the sidebar
   width so the conversation row stays full-width. */
.conversation-badge-wrap {
  display: block;
  width: 100%;
}

.conversation-badge-wrap :deep(.el-badge__content) {
  z-index: 2;
}

/* Notifications row in the bottom actions */
.notifications-row {
  align-items: center;
}

.notifications-badge {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
}

.notifications-badge :deep(.el-badge__content) {
  border: none;
  font-size: 10px;
  height: 14px;
  line-height: 14px;
  padding: 0 4px;
}

.bell-icon {
  display: inline-block;
  transform-origin: 50% 0;
}

.bell-shake-normal {
  animation: bell-shake-normal 0.6s ease;
}

.bell-shake-high {
  animation: bell-shake-high 1.2s ease 3;
  color: #f59e0b;
}

.badge-pulse-high :deep(.el-badge__content) {
  animation: badge-pulse-high 0.6s ease 3;
  background: #ef4444 !important;
}

@keyframes bell-shake-normal {
  0%, 100% { transform: rotate(0deg); }
  20% { transform: rotate(-10deg); }
  40% { transform: rotate(8deg); }
  60% { transform: rotate(-6deg); }
  80% { transform: rotate(4deg); }
}

@keyframes bell-shake-high {
  0%, 100% { transform: rotate(0deg) scale(1); }
  15% { transform: rotate(-20deg) scale(1.15); }
  30% { transform: rotate(18deg) scale(1.15); }
  45% { transform: rotate(-16deg) scale(1.1); }
  60% { transform: rotate(14deg) scale(1.1); }
  75% { transform: rotate(-10deg) scale(1.05); }
  90% { transform: rotate(6deg) scale(1.02); }
}

@keyframes badge-pulse-high {
  0%, 100% { transform: scale(1); opacity: 1; }
  50% { transform: scale(1.35); opacity: 0.85; }
}

.streaming-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--primary-color);
  animation: pulse 1.4s ease-in-out infinite;
  flex-shrink: 0;
  margin-left: -4px;
}

.conversation-item.streaming .streaming-dot {
  background: var(--primary-color);
}

@keyframes pulse {
  0%, 100% { opacity: 0.4; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1); }
}

.conversation-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s ease;
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

.conversation-item .delete-conversation-btn,
.conversation-item .edit-conversation-btn {
  display: none;
  width: 20px;
  height: 20px;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  padding: 0;
  flex-shrink: 0;
  font-size: 14px;
}

.conversation-item:hover .delete-conversation-btn,
.conversation-item:hover .edit-conversation-btn {
  display: flex;
}

.conversation-item .delete-conversation-btn:hover {
  color: var(--error-color, #e74c3c);
  background: var(--hover-bg);
}

.conversation-item .edit-conversation-btn:hover {
  color: var(--primary-color);
  background: var(--hover-bg);
}

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
  color: var(--error-color, #e74c3c);
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

.sidebar-spacer {
  flex: 1;
}

.bottom-section {
  padding: 8px;
  background: var(--surface-color);
}

.settings-row,
.theme-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 6px;
  transition: all 0.2s ease;
  cursor: pointer;
}

.settings-row:hover {
  background: var(--hover-bg);
}

.logout-row .settings-icon,
.logout-row .settings-label {
  color: var(--error-color, #e74c3c);
}

.settings-icon,
.theme-icon {
  font-size: 18px;
  color: var(--text-secondary);
  flex-shrink: 0;
}

.settings-label,
.theme-label {
  flex: 1;
  font-size: 0.875rem;
  color: var(--text-primary);
}

.chevron-icon {
  font-size: 16px;
  color: var(--text-tertiary);
}

.theme-row {
  cursor: default;
}

.theme-row:hover {
  background: transparent;
}

.theme-row.clickable {
  cursor: pointer;
}

.theme-row.clickable:hover {
  background: var(--hover-bg);
}

/* Collapsed-mode layout overrides */
.sidebar.collapsed .sidebar-section {
  padding: 8px 0;
}

.sidebar.collapsed .agent-section {
  padding: 0;
}

.sidebar.collapsed .bottom-section {
  padding: 8px 0;
}

.sidebar.collapsed .conversations-section {
  padding: 8px 0;
}

.sidebar.collapsed .collapsed-header {
  display: flex;
  justify-content: center;
  margin-bottom: 6px;
  padding: 0;
}

.sidebar.collapsed .settings-row,
.sidebar.collapsed .theme-row,
.sidebar.collapsed .conversation-item {
  justify-content: center;
  padding: 10px 0;
  gap: 0;
}

/* Responsive */
@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    left: -280px;
    top: 0;
    z-index: 1000;
    transition: left 0.3s ease;
    box-shadow: 2px 0 8px rgba(0, 0, 0, 0.1);
  }
  
  .sidebar.open {
    left: 0;
  }
}
</style>
