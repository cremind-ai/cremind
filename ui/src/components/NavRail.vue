<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import { ElBadge, ElNotification, ElPopover, ElTooltip } from 'element-plus';
import { useSettingsStore } from '../stores/settings';
import { useChatStore } from '../stores/chat';
import { useNotificationsStore } from '../stores/notifications';
import AgentCard from './AgentCard.vue';
import NotificationList from './NotificationList.vue';
import { openNotificationsStream, type NotificationStreamHandle } from '../services/notificationsStream';
import { listProfiles } from '../services/configApi';
import { NAV_ITEMS, SETTINGS_ITEM, type NavItem } from '../constants/navigation';

const route = useRoute();
const router = useRouter();
const settingsStore = useSettingsStore();
const chatStore = useChatStore();
const notificationsStore = useNotificationsStore();

const emit = defineEmits<{
  logout: [];
}>();

// ── Nav model ──
const isAdminProfile = computed(() => (route.params.profile as string) === 'admin');

const chatItem = computed(() => NAV_ITEMS.find(i => i.id === 'chat')!);

// Route destinations shown as rail icons (excludes Chat, rendered separately,
// and the notifications bell, which is a popover trigger).
const otherRailItems = computed(() =>
  NAV_ITEMS.filter(
    i => i.kind === 'route'
      && i.id !== 'chat'
      && (i.placement ?? 'rail') === 'rail'
      && (!i.adminOnly || isAdminProfile.value),
  ),
);

// Items explicitly demoted to the "More" popover. Empty in v1 — the popover
// and its rail trigger only render once something lands here, so a growing
// feature set folds into "More" instead of shrinking anything.
const overflowItems = computed(() =>
  NAV_ITEMS.filter(
    i => i.placement === 'overflow' && (!i.adminOnly || isAdminProfile.value),
  ),
);

const settingsItem = SETTINGS_ITEM;

const isActive = (item: NavItem): boolean => {
  const name = route.name as string;
  if (item.routeName === name) return true;
  return item.activeRouteNames?.includes(name) ?? false;
};

const handleNavClick = (item: NavItem) => {
  if (item.kind !== 'route' || !item.routeName) return;
  const profile = route.params.profile as string;
  if (!profile) return;
  // Clicking Chat while already on a chat route toggles the conversations
  // panel (VS Code activity-bar behaviour); from elsewhere it navigates.
  if (item.id === 'chat' && (route.name === 'chat' || route.name === 'conversation')) {
    settingsStore.setConversationsPanelCollapsed(!settingsStore.conversationsPanelCollapsed);
    return;
  }
  router.push({ name: item.routeName, params: { profile } });
};

// ── Overflow "More" popover ──
const morePopoverVisible = ref(false);
const moreTriggerRef = ref<HTMLElement | null>(null);

const handleOverflowClick = (item: NavItem) => {
  morePopoverVisible.value = false;
  if (item.kind !== 'route' || !item.routeName) return;
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: item.routeName, params: { profile } });
};

// ── Theme + settings ──
const toggleTheme = () => {
  settingsStore.setTheme(settingsStore.theme === 'dark' ? 'light' : 'dark');
};

const handleOpenSettings = () => {
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'settings', params: { profile } });
};

// ── Agent popover ──
const agentPopoverVisible = ref(false);
const agentTriggerRef = ref<HTMLElement | null>(null);

// ── Account menu ──
const userMenuTriggerRef = ref<HTMLElement | null>(null);
const userMenuVisible = ref(false);
const otherProfilesExist = ref(false);
const currentProfileName = computed(() => (route.params.profile as string) || '');
const hasMultipleProfiles = computed(() => otherProfilesExist.value);

const handleOpenProfile = () => {
  userMenuVisible.value = false;
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'profile-settings', params: { profile } });
};

const handleSwitchProfile = () => {
  userMenuVisible.value = false;
  router.push('/');
};

const handleOpenUpdates = () => {
  userMenuVisible.value = false;
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'updates', params: { profile } });
};

const handleOpenAbout = () => {
  userMenuVisible.value = false;
  const profile = route.params.profile as string;
  if (!profile) return;
  router.push({ name: 'about', params: { profile } });
};

const handleLogout = () => {
  userMenuVisible.value = false;
  emit('logout');
};

// Whether a profile beyond the always-present admin exists — gates the
// "Switch profile" item in the account menu.
watch(
  () => [settingsStore.authToken, settingsStore.profileId] as const,
  ([token, profileId]) => {
    if (!token || !profileId) return;
    listProfiles(settingsStore.agentUrl, token)
      .then(({ profiles }) => { otherProfilesExist.value = profiles.length > 1; })
      .catch(() => { otherProfilesExist.value = false; });
  },
  { immediate: true },
);

// ── Notifications: bell popover, badge, arrival animation, SSE stream ──
const bellPopoverVisible = ref(false);
const notificationsTriggerRef = ref<HTMLElement | null>(null);

const totalUnread = computed(() =>
  settingsStore.profileId ? notificationsStore.totalUnread(settingsStore.profileId) : 0,
);

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

let notificationsStream: NotificationStreamHandle | null = null;

const closeNotificationsStream = () => {
  if (notificationsStream !== null) {
    notificationsStream.close();
    notificationsStream = null;
  }
};

// Open the notifications stream reactively to auth state. Watching the auth
// deps with immediate:true fires once on mount (returning early if no token
// yet) and again the moment the token becomes available, so server-triggered
// runs (skill events) reach the rail even on a direct deep-link load.
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
        // the rail can lazily open that SSE and the streaming-dot lights
        // up even for conversations the user has never visited this session.
        if (entry.kind === 'started') {
          // Event runs never emit 'started' (their hidden conversation must not
          // be lazy-tracked into the sidebar) — this only fires for chat runs.
          console.log('[debug:notif] handling started for', entry.conversation_id);
          const { runtime } = chatStore.ensureBucket(entry.conversation_id);
          runtime.isStreaming = true;
          runtime.startedAt = Date.now();
          chatStore.trackConversation(entry.conversation_id, 'streaming');
          return;
        }
        // Event-run notifications never lazy-track their hidden conversation
        // (they must not appear in the sidebar). A run awaiting the user's
        // reply raises a sticky toast that deep-links to the run detail.
        if (entry.kind === 'event_run_pending' && entry.event_run_id) {
          const profile = route.params.profile as string;
          const runId = entry.event_run_id;
          ElNotification({
            title: 'Event run needs your input',
            message: entry.message_preview || entry.conversation_title,
            type: 'warning',
            duration: 0,
            onClick: () => {
              if (profile) {
                router.push({ name: 'skill-events', params: { profile }, query: { run: runId } });
              }
            },
          });
        }
        // The user is already watching this conversation — record the
        // notification but pre-mark it seen so the bell badge doesn't bump
        // and the arrival animation stays quiet.
        const isActiveConv = entry.conversation_id !== ''
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
          seen: isActiveConv,
          channelType: entry.channel_type,
          senderId: entry.sender_id,
          senderName: entry.sender_name,
          otp: entry.otp,
          skillId: entry.skill_id,
          skillName: entry.skill_name,
          eventRunId: entry.event_run_id,
          sourceKind: entry.source_kind,
        });
        if (!isActiveConv) triggerBellAnimation(priority);
      },
    );
  },
  { immediate: true },
);

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

onMounted(() => {
  notificationsStore.hydrate();
});

onBeforeUnmount(closeNotificationsStream);
</script>

<template>
  <aside class="nav-rail">
    <!-- Agent avatar + connection status; click opens the full card. AgentCard
         renders its own status tooltip in compact mode, so no wrapper tooltip. -->
    <div class="rail-top">
      <div ref="agentTriggerRef" class="rail-agent" @click="agentPopoverVisible = !agentPopoverVisible">
        <AgentCard
          :agentCard="chatStore.agentCard"
          :isConnected="chatStore.isConnected"
          :compact="true"
        />
      </div>
      <ElPopover
        :visible="agentPopoverVisible"
        :virtual-ref="agentTriggerRef"
        virtual-triggering
        placement="right-start"
        :width="260"
        popper-class="rail-agent-popover"
        @update:visible="agentPopoverVisible = $event"
      >
        <AgentCard
          :agentCard="chatStore.agentCard"
          :isConnected="chatStore.isConnected"
          :compact="false"
          @connect="chatStore.connect"
          @disconnect="chatStore.disconnect"
        />
      </ElPopover>
    </div>

    <!-- Primary destinations -->
    <div class="rail-middle">
      <ElTooltip content="Chat" placement="right" :show-after="300">
        <button
          class="rail-item"
          :class="{ active: isActive(chatItem) }"
          @click="handleNavClick(chatItem)"
        >
          <Icon :icon="chatItem.icon" class="rail-icon" />
        </button>
      </ElTooltip>

      <!-- Notifications bell (standalone so its template ref is an element,
           not a v-for array). -->
      <ElTooltip content="Notifications" placement="right" :show-after="300" :disabled="bellPopoverVisible">
        <button
          ref="notificationsTriggerRef"
          class="rail-item"
          @click="bellPopoverVisible = !bellPopoverVisible"
        >
          <ElBadge
            :value="totalUnread"
            :hidden="totalUnread === 0"
            :max="99"
            class="rail-badge"
            :class="{ 'badge-pulse-high': bellPulse === 'high' }"
          >
            <Icon
              :key="bellAnimKey"
              icon="mdi:bell-outline"
              class="rail-icon bell-icon"
              :class="{
                'bell-shake-normal': bellPulse === 'normal',
                'bell-shake-high': bellPulse === 'high',
              }"
            />
          </ElBadge>
        </button>
      </ElTooltip>

      <ElTooltip
        v-for="item in otherRailItems"
        :key="item.id"
        :content="item.label"
        placement="right"
        :show-after="300"
      >
        <button
          class="rail-item"
          :class="{ active: isActive(item) }"
          @click="handleNavClick(item)"
        >
          <Icon :icon="item.icon" class="rail-icon" />
        </button>
      </ElTooltip>
    </div>

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

    <!-- Bottom utilities -->
    <div class="rail-bottom">
      <template v-if="overflowItems.length > 0">
        <ElTooltip content="More" placement="right" :show-after="300" :disabled="morePopoverVisible">
          <button ref="moreTriggerRef" class="rail-item" @click="morePopoverVisible = !morePopoverVisible">
            <Icon icon="mdi:dots-horizontal" class="rail-icon" />
          </button>
        </ElTooltip>
        <ElPopover
          :visible="morePopoverVisible"
          :virtual-ref="moreTriggerRef"
          virtual-triggering
          placement="right-end"
          :width="200"
          popper-class="rail-menu-popover"
          @update:visible="morePopoverVisible = $event"
        >
          <div class="rail-menu" role="menu">
            <button
              v-for="item in overflowItems"
              :key="item.id"
              type="button"
              role="menuitem"
              class="rail-menu-item"
              @click="handleOverflowClick(item)"
            >
              <Icon :icon="item.icon" class="rail-menu-icon" />
              <span>{{ item.label }}</span>
            </button>
          </div>
        </ElPopover>
      </template>

      <ElTooltip
        :content="settingsStore.theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'"
        placement="right"
        :show-after="300"
      >
        <button class="rail-item" @click="toggleTheme">
          <Icon
            :icon="settingsStore.theme === 'dark' ? 'mdi:weather-night' : 'mdi:weather-sunny'"
            class="rail-icon"
          />
        </button>
      </ElTooltip>

      <ElTooltip :content="settingsItem.label" placement="right" :show-after="300">
        <button
          class="rail-item"
          :class="{ active: isActive(settingsItem) }"
          @click="handleOpenSettings"
        >
          <Icon :icon="settingsItem.icon" class="rail-icon" />
        </button>
      </ElTooltip>

      <ElTooltip content="Account" placement="right" :show-after="300" :disabled="userMenuVisible">
        <button
          ref="userMenuTriggerRef"
          class="rail-item"
          @click="userMenuVisible = !userMenuVisible"
        >
          <Icon icon="mdi:account-circle-outline" class="rail-icon" />
        </button>
      </ElTooltip>
      <ElPopover
        :visible="userMenuVisible"
        :virtual-ref="userMenuTriggerRef"
        virtual-triggering
        placement="right-end"
        :width="200"
        popper-class="rail-menu-popover"
        @update:visible="userMenuVisible = $event"
      >
        <div class="rail-menu" role="menu">
          <div class="rail-menu-profile">{{ currentProfileName }}</div>
          <button type="button" role="menuitem" class="rail-menu-item" @click="handleOpenProfile">
            <Icon icon="mdi:account-outline" class="rail-menu-icon" />
            <span>Profile</span>
          </button>
          <button
            v-if="hasMultipleProfiles"
            type="button"
            role="menuitem"
            class="rail-menu-item"
            @click="handleSwitchProfile"
          >
            <Icon icon="mdi:account-switch-outline" class="rail-menu-icon" />
            <span>Switch profile</span>
          </button>
          <button type="button" role="menuitem" class="rail-menu-item" @click="handleOpenUpdates">
            <Icon icon="mdi:download" class="rail-menu-icon" />
            <span>Updates</span>
          </button>
          <button type="button" role="menuitem" class="rail-menu-item" @click="handleOpenAbout">
            <Icon icon="mdi:information-outline" class="rail-menu-icon" />
            <span>About</span>
          </button>
          <div class="rail-menu-divider" />
          <button type="button" role="menuitem" class="rail-menu-item danger" @click="handleLogout">
            <Icon icon="mdi:logout" class="rail-menu-icon" />
            <span>Logout</span>
          </button>
        </div>
      </ElPopover>
    </div>
  </aside>
</template>

<style scoped>
.nav-rail {
  width: var(--rail-width);
  height: 100%;
  background: var(--surface-color);
  border-right: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 6px;
  gap: 4px;
  flex-shrink: 0;
  box-sizing: border-box;
}

.rail-top {
  flex-shrink: 0;
  display: flex;
  justify-content: center;
}

.rail-agent {
  cursor: pointer;
  display: flex;
  justify-content: center;
  border-radius: 8px;
  padding: 2px 0;
}
/* Strip AgentCard's own chrome — in the rail it's just the avatar. */
.rail-agent :deep(.agent-card) {
  border: none;
  background: transparent;
}
.rail-agent :deep(.agent-card.compact .agent-header) {
  padding: 6px 0;
}

.rail-middle {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  width: 100%;
  /* Hide the scrollbar — the rail is too narrow for one to look right. */
  scrollbar-width: none;
}
.rail-middle::-webkit-scrollbar {
  display: none;
}

.rail-bottom {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  width: 100%;
}

.rail-item {
  position: relative;
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 8px;
  padding: 0;
  transition: background 0.15s ease, color 0.15s ease;
}

.rail-item:hover {
  background: var(--hover-bg);
  color: var(--primary-color);
}

.rail-item.active {
  color: var(--primary-color);
  background: var(--surface-hover);
}

/* Left accent bar for the active destination — reaches the rail's edge. */
.rail-item.active::before {
  content: '';
  position: absolute;
  left: -6px;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 20px;
  border-radius: 0 3px 3px 0;
  background: var(--primary-color);
}

.rail-icon {
  font-size: 20px;
}

.rail-badge {
  display: inline-flex;
  align-items: center;
}
.rail-badge :deep(.el-badge__content) {
  border: none;
  font-size: 10px;
  height: 14px;
  line-height: 14px;
  padding: 0 4px;
  z-index: 2;
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
</style>

<!-- Non-scoped: rail popover menus are teleported to <body>, so scoped styles
     would not reach their rows. Mirrors the old sidebar's account-menu block. -->
<style>
.rail-menu-popover.el-popover.el-popper {
  padding: 6px;
}

.rail-menu {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.rail-menu-profile {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 4px 10px 6px;
}

.rail-menu .rail-menu-item {
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

.rail-menu .rail-menu-item:hover {
  background: var(--hover-bg);
}

.rail-menu .rail-menu-item .rail-menu-icon {
  font-size: 18px;
  color: var(--text-secondary);
  flex-shrink: 0;
}

.rail-menu-divider {
  height: 1px;
  background: var(--border-color);
  margin: 4px 6px;
}

.rail-menu .rail-menu-item.danger,
.rail-menu .rail-menu-item.danger .rail-menu-icon {
  color: var(--danger-color, #e74c3c);
}
</style>
