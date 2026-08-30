<script setup lang="ts">
// The Group chat page: room list on the left, the selected room on the right.
//
// A room is one shared timeline that several profiles' agents read from their
// own hidden seats. There is no per-token streaming here by design — a post
// appears whole when its author's turn ends — but a turn in progress still
// shows its work: each busy member gets a live card carrying the steps it is
// taking, for whoever is allowed to look behind that member's seat.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton, ElNotification, ElOption, ElSelect, ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useGroupChatStore } from '../stores/groupChat';
import { useSettingsStore } from '../stores/settings';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import ResizableDivider from '../components/ResizableDivider.vue';
import ConversationMemoryPanel from '../components/ConversationMemoryPanel.vue';
import ConversationUsagePanel from '../components/ConversationUsagePanel.vue';
import GroupList from '../components/groupchat/GroupList.vue';
import GroupComposer from '../components/groupchat/GroupComposer.vue';
import GroupLiveTurnCard from '../components/groupchat/GroupLiveTurnCard.vue';
import GroupMessageBubble from '../components/groupchat/GroupMessageBubble.vue';
import GroupRightPanel from '../components/groupchat/GroupRightPanel.vue';
import GroupRoutingChip from '../components/groupchat/GroupRoutingChip.vue';
import { senderAvatarColor } from '../components/groupchat/senderHue';
import { isOwnWebPost } from '../components/groupchat/senderIdentity';
import { readRouting, type GroupRouting } from '../services/groupChatApi';

const props = defineProps<{
  profile?: string;
  groupId?: string;
}>();

const router = useRouter();
const store = useGroupChatStore();
const settingsStore = useSettingsStore();
const terminalPanel = useTerminalPanelStore();

const groupListRef = ref<InstanceType<typeof GroupList> | null>(null);
const timelineRef = ref<HTMLElement | null>(null);

const activeGroup = computed(() => store.activeGroup);
const messages = computed(() => store.activeMessages);

// The colour rides along so the roster, the bubbles and the workspace tabs all
// name a member the same way.
const members = computed(() =>
  (activeGroup.value?.members ?? []).map((p) => ({
    profile: p, name: store.nameFor(p), color: senderAvatarColor(p),
  })),
);

// Which posts sit on the right. The routing chip annotates the message that
// woke the agents — usually a human post, and usually the viewer's own — so it
// has to follow its bubble across the timeline rather than stay in the left
// gutter under empty space.
const ownMessageIds = computed(() => {
  const viewer = settingsStore.profileId;
  const own = new Set<string>();
  for (const message of messages.value) {
    if (isOwnWebPost(message, viewer)) own.add(message.id);
  }
  return own;
});

// Profiles, not names: the live card resolves its own name, colour and steps
// from the profile, and a name is not a key — two agents may share one.
const thinkingSeats = computed(() => store.thinkingProfiles(props.groupId ?? null));

const canPost = computed(() => store.canPost(activeGroup.value));

const streamStatus = computed(() =>
  props.groupId ? (store.streamStatusByGroup[props.groupId] ?? 'connecting') : 'closed',
);

const streamStatusLabel = computed(() => {
  switch (streamStatus.value) {
    case 'open': return 'Live';
    case 'reconnecting': return 'Reconnecting…';
    case 'closed': return 'Disconnected';
    default: return 'Connecting…';
  }
});

// Parsed once per timeline render: `readRouting` allocates, and the template
// would otherwise call it twice for every bubble on screen. Only the posts that
// actually woke somebody land in here — `readRouting` drops a capped row, whose
// chip would claim turns that a cap had already silenced.
const routingByMessage = computed(() => {
  const out: Record<string, GroupRouting> = {};
  for (const message of messages.value) {
    const routing = readRouting(message);
    if (routing) out[message.id] = routing;
  }
  return out;
});

// ── per-agent workspace ──
//
// The seats this viewer may look behind: its own, and every member's for the
// admin — the store mirrors the server's own rule, so a tab that renders is a
// tab whose file tree, terminals, memory and usage all answer 200. A member
// with no seat conversation yet (its shadow has never been created) has no
// workspace to show at all.
const visibleSeats = computed(() => {
  const group = activeGroup.value;
  return store.visibleSeatProfiles(group)
    .map((profile) => ({
      profile,
      name: store.nameFor(profile),
      seatId: store.seatIdFor(group, profile),
    }))
    .filter((seat): seat is { profile: string; name: string; seatId: string } => (
      !!seat.seatId
    ));
});

const selectedSeatProfile = ref<string | null>(null);

const selectedSeat = computed(() =>
  visibleSeats.value.find((s) => s.profile === selectedSeatProfile.value)
  ?? visibleSeats.value[0]
  ?? null,
);

// Default to the viewer's own agent — the one whose files it is here to see —
// and only fall back to the first seat for an admin who is not a member.
watch(visibleSeats, (seats) => {
  if (seats.length === 0) {
    selectedSeatProfile.value = null;
    return;
  }
  if (seats.some((s) => s.profile === selectedSeatProfile.value)) return;
  const own = seats.find((s) => s.profile === settingsStore.profileId);
  selectedSeatProfile.value = (own ?? seats[0]).profile;
}, { immediate: true });

// Seed each seat's cwd so the file tree opens on the directory that agent is
// actually working in, without a request per seat.
//
// Only the GET of a group carries `working_directory`; a `group_updated` frame
// re-merges the member rows without it. Skipping the absent ones (rather than
// writing an empty string) is what keeps the seeded value across such a merge —
// and `setConversationCwd` is a no-op for an unchanged path, so the agent's own
// `cwd` frames stay authoritative.
watch(() => activeGroup.value?.member_rows, (rows) => {
  for (const row of rows ?? []) {
    if (row.shadow_conversation_id && row.working_directory) {
      terminalPanel.setConversationCwd(row.shadow_conversation_id, row.working_directory);
    }
  }
}, { immediate: true, deep: true });

const COLLAPSED_PANEL_WIDTH = 36;
const showRightPanel = computed(
  () => !terminalPanel.minimized && visibleSeats.value.length > 0,
);
const showMinimizedPill = computed(
  () => terminalPanel.minimized && visibleSeats.value.length > 0,
);
const rightPanelWidth = computed(() =>
  terminalPanel.collapsed ? COLLAPSED_PANEL_WIDTH : terminalPanel.panelWidth,
);

// Without the tab strip (panel hidden, or collapsed to its strip) the memory
// and usage buttons would be bound to an agent nothing on screen names, so the
// picker takes over the labelling job.
const showSeatPicker = computed(() => (
  visibleSeats.value.length > 1
  && (terminalPanel.minimized || terminalPanel.collapsed)
));

const memoryPanelOpen = ref(false);
const usagePanelOpen = ref(false);

// ── navigation ──
const selectGroup = (groupId: string) => {
  if (groupId === props.groupId) return;
  router.push({
    name: 'group-chat-room',
    params: { profile: props.profile, groupId },
  });
};

const openSettings = () => {
  if (!props.groupId) return;
  router.push({
    name: 'group-chat-settings',
    params: { profile: props.profile, groupId: props.groupId },
  });
};

const scrollToBottom = () => {
  nextTick(() => {
    const el = timelineRef.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
};

const enterGroup = async (groupId: string) => {
  try {
    await store.openGroup(groupId);
    scrollToBottom();
  } catch (e) {
    ElNotification({
      title: 'Group chat',
      message: e instanceof Error ? e.message : 'Failed to open the group',
      type: 'error',
    });
  }
};

onMounted(async () => {
  void store.loadAgentNames();
  try {
    await store.loadGroups();
  } catch (e) {
    ElNotification({
      title: 'Group chat',
      message: e instanceof Error ? e.message : 'Failed to load groups',
      type: 'error',
    });
    return;
  }
  if (props.groupId) {
    await enterGroup(props.groupId);
  } else if (store.groups.length > 0) {
    // The bare /group-chat route is a landing slot, not a page of its own —
    // drop straight into the first room the way Chat opens a conversation.
    router.replace({
      name: 'group-chat-room',
      params: { profile: props.profile, groupId: store.groups[0].id },
    });
  }
});

watch(() => props.groupId, async (newId, oldId) => {
  if (newId === oldId) return;
  if (newId) {
    await enterGroup(newId);
    return;
  }
  store.closeGroup();
  // Landed back on the bare route (a deleted room, or a bad id we bounced
  // off). Pick up the next room rather than leaving an empty page behind.
  if (store.groups.length > 0) {
    router.replace({
      name: 'group-chat-room',
      params: { profile: props.profile, groupId: store.groups[0].id },
    });
  }
});

// A `deleted` frame (or a delete from another tab) drops the room from the
// store while the URL still points at it. Fall back to the list rather than
// leaving a dead room on screen.
watch(
  () => [props.groupId, store.groupsLoaded, store.groups.length] as const,
  ([groupId, loaded]) => {
    if (!groupId || !loaded) return;
    if (store.groups.some((g) => g.id === groupId)) return;
    router.replace({ name: 'group-chat', params: { profile: props.profile } });
  },
);

// New posts should land in view; a reader scrolled up is left alone.
watch(() => messages.value.length, (next, previous) => {
  if (next <= (previous ?? 0)) return;
  const el = timelineRef.value;
  if (!el) return;
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
  if (nearBottom) scrollToBottom();
});

onBeforeUnmount(() => {
  store.closeGroup();
  // Leaving the room releases the workspace: the focus points at a member's
  // hidden seat, and the Conversations page must go back to showing its own
  // conversation's files and terminals.
  terminalPanel.setFocusConversation(null);
});

const handleSend = async (text: string) => {
  try {
    await store.sendMessage(text);
    scrollToBottom();
  } catch (e) {
    ElNotification({
      title: 'Group chat',
      message: e instanceof Error ? e.message : 'Failed to post the message',
      type: 'error',
    });
  }
};
</script>

<template>
  <div class="group-chat-view">
    <GroupList
      ref="groupListRef"
      :groups="store.groups"
      :active-group-id="props.groupId ?? null"
      :loading="store.loading"
      @select="selectGroup"
    />

    <section class="room">
      <template v-if="activeGroup">
        <!-- Bound to the agent selected in the workspace panel, so "memory" and
             "usage" always mean the agent whose files are on screen. -->
        <div v-if="visibleSeats.length > 0" class="seat-tools">
          <ElSelect
            v-if="showSeatPicker"
            v-model="selectedSeatProfile"
            size="small"
            class="seat-picker"
          >
            <ElOption
              v-for="seat in visibleSeats"
              :key="seat.profile"
              :value="seat.profile"
              :label="seat.name"
            />
          </ElSelect>
          <button
            class="seat-tool-btn"
            :title="`View ${selectedSeat?.name}'s conversation memory`"
            @click="memoryPanelOpen = true"
          >
            <Icon icon="mdi:brain" />
          </button>
          <button
            class="seat-tool-btn"
            :title="`View ${selectedSeat?.name}'s token usage & cost`"
            @click="usagePanelOpen = true"
          >
            <Icon icon="mdi:chart-box-outline" />
          </button>
        </div>

        <header class="room-header">
          <div class="room-title-row">
            <h2 class="room-name">{{ activeGroup.name }}</h2>
            <ElTooltip :content="streamStatusLabel" placement="bottom" :show-after="300">
              <span class="stream-dot" :class="`dot-${streamStatus}`" />
            </ElTooltip>
            <ElButton
              v-if="store.isAdmin"
              size="small"
              text
              title="Group settings"
              @click="openSettings"
            >
              <Icon icon="mdi:cog-outline" />
            </ElButton>
          </div>
          <div class="member-chips">
            <span v-if="members.length === 0" class="no-members">No members yet</span>
            <span v-for="member in members" :key="member.profile" class="member-chip">
              <span class="member-dot" :style="{ background: member.color }" />
              {{ member.name }}
            </span>
          </div>
        </header>

        <!-- The timeline and the restore pill share one positioning context, so
             the pill floats over the LAST posts and stops where the composer
             starts. Anchored to the room instead, it had to guess the
             composer's height and sat on top of the textarea the moment that
             grew. -->
        <div class="timeline-region">
        <div
          ref="timelineRef"
          class="timeline"
          :class="{ 'timeline-under-pill': showMinimizedPill }"
        >
          <div
            v-if="messages.length === 0 && thinkingSeats.length === 0"
            class="empty-state"
          >
            <div class="empty-card">
              <Icon icon="mdi:forum-outline" class="empty-icon" />
              <div class="empty-title">Nothing posted yet</div>
              <div class="empty-subtitle">
                Say something and every member agent decides for itself whether
                it was addressed.
              </div>
            </div>
          </div>
          <TransitionGroup v-else name="message-list" tag="div" class="messages-list">
            <!-- The post and its routing chip are ONE keyed block: the chip is a
                 footnote to that post, and keying them separately lets the move
                 animation slide them apart. -->
            <div
              v-for="message in messages"
              :key="message.id"
              class="post-block"
              :class="{ 'own-block': ownMessageIds.has(message.id) }"
            >
              <GroupMessageBubble :message="message" />
              <div v-if="routingByMessage[message.id]" class="routing-row">
                <GroupRoutingChip :routing="routingByMessage[message.id]" />
              </div>
            </div>
            <GroupLiveTurnCard
              v-for="profile in thinkingSeats"
              :key="`thinking-${profile}`"
              :group="activeGroup"
              :profile="profile"
            />
          </TransitionGroup>
        </div>

          <button
            v-if="showMinimizedPill"
            class="workspace-restore-pill"
            title="Show the agent workspace panel"
            @click="terminalPanel.restore()"
          >
            <Icon icon="mdi:dock-right" />
            <span>{{ selectedSeat?.name || 'Agent' }} workspace</span>
          </button>
        </div>

        <GroupComposer
          :members="members"
          :disabled="!canPost"
          :sending="store.sending"
          disabled-hint="Only the admin and this group's member profiles can post here."
          @send="handleSend"
        />
      </template>

      <div v-else class="room-empty">
        <Icon icon="mdi:forum-outline" class="empty-icon" />
        <h2>No group selected</h2>
        <p v-if="store.isAdmin">
          Create a group, add the profiles that should share it, and everyone
          you post to is answered by whichever agent decides it was addressed.
        </p>
        <p v-else>
          You are not a member of any group yet. Ask the admin profile to add
          you to one.
        </p>
        <ElButton
          v-if="store.isAdmin"
          type="primary"
          @click="groupListRef?.openDialog()"
        >
          Create a group
        </ElButton>
      </div>
    </section>

    <template v-if="showRightPanel">
      <ResizableDivider
        v-if="!terminalPanel.collapsed"
        @update:width="terminalPanel.setWidth"
      />
      <GroupRightPanel
        class="right-panel-host"
        :style="{ width: rightPanelWidth + 'px' }"
        :group-id="props.groupId ?? null"
        :seats="visibleSeats"
        v-model="selectedSeatProfile"
      />
    </template>

    <!-- A seat's owner and the admin may read its memory, but folding it on
         demand is owner-only and 403s here, so the button stays off. -->
    <ConversationMemoryPanel
      v-model="memoryPanelOpen"
      :conversation-id="selectedSeat?.seatId ?? null"
      :allow-trigger="false"
      :title="`${selectedSeat?.name || 'Agent'} — memory`"
    />

    <ConversationUsagePanel
      v-model="usagePanelOpen"
      :conversation-id="selectedSeat?.seatId ?? null"
    />
  </div>
</template>

<style scoped>
.group-chat-view {
  display: flex;
  height: 100%;
  width: 100%;
  overflow: hidden;
}

.room {
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-color);
}

.right-panel-host {
  flex-shrink: 0;
  height: 100%;
}

/* Floating agent tools, opposite the room title. Kept out of the header row so
   the room name and the member chips keep the full width they had. */
.seat-tools {
  position: absolute;
  top: 10px;
  right: 12px;
  z-index: 6;
  display: flex;
  align-items: center;
  gap: 6px;
}

.seat-picker {
  width: 130px;
}

.seat-tool-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  background: var(--surface-color);
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  cursor: pointer;
}

.seat-tool-btn:hover {
  border-color: var(--primary-color);
  color: var(--primary-color);
}

.seat-tool-btn :deep(svg) { font-size: 17px; }

/* Owns the space between the header and the composer, and nothing else — which
   is what lets the pill inside it be positioned without knowing how tall the
   composer is. */
.timeline-region {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.workspace-restore-pill {
  position: absolute;
  right: 16px;
  bottom: 16px;
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

.workspace-restore-pill:hover {
  border-color: var(--primary-color);
  color: #e5e7eb;
}

/* Aligned under the bubble's text, not under its avatar, so the chip reads as
   a footnote to what was said rather than as another speaker. */
.room-header {
  flex-shrink: 0;
  /* Aligned with the timeline's gutter, so the room name sits over the posts
     rather than to the left of them. */
  padding: 12px 20px;
  border-bottom: 1px solid var(--border-color);
  background: var(--surface-color);
}

.room-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.room-name {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 700;
  color: var(--text-primary);
}

.stream-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-tertiary);
  flex-shrink: 0;
}

.stream-dot.dot-open { background: var(--success-color, #22c55e); }
.stream-dot.dot-connecting,
.stream-dot.dot-reconnecting { background: var(--warning-color, #f59e0b); }
.stream-dot.dot-closed { background: var(--text-tertiary); }

.member-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.member-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--text-secondary);
  background: var(--surface-hover);
  border: 1px solid var(--border-color);
}

.member-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.no-members {
  font-size: 0.75rem;
  color: var(--text-tertiary);
}

.timeline {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 20px;
  scroll-behavior: smooth;
}

/* The pill is opaque and floats over this, so scrolled to the bottom it would
   otherwise cover the corner of the last post. Only while it is shown: an
   unconditional gap would be a permanent hole under every room. */
.timeline-under-pill {
  padding-bottom: 56px;
}

/* Position:relative so a leaving post, which the transition takes out of flow,
   is positioned against the list rather than against the page. */
.messages-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
  position: relative;
}

.post-block {
  display: flex;
  flex-direction: column;
}

/* Aligned under the bubble's text, past the 32px avatar and its 10px gap —
   mirrored for a post that sits on the right. `text-align` rather than
   `align-items`, which would make .message-row shrink to fit and change the
   bubble layout itself. */
.routing-row {
  padding: 4px 0 0 42px;
}

.post-block.own-block .routing-row {
  padding: 4px 42px 0 0;
  text-align: right;
}

.empty-state {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.empty-card {
  text-align: center;
  max-width: 420px;
  padding: 0 24px;
}

.empty-card .empty-icon {
  font-size: 64px;
  color: var(--text-tertiary);
  opacity: 0.5;
}

.empty-title {
  margin-top: 12px;
  font-size: 1.05rem;
  font-weight: 600;
  color: var(--text-secondary);
}

.empty-subtitle {
  margin-top: 6px;
  font-size: 0.85rem;
  line-height: 1.6;
  color: var(--text-tertiary);
}

.room-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 24px;
  text-align: center;
  color: var(--text-secondary);
}

.room-empty h2 {
  margin: 0;
  font-size: 1.1rem;
  font-weight: 600;
  color: var(--text-primary);
}

.room-empty p {
  margin: 0 0 8px 0;
  max-width: 420px;
  font-size: 0.85rem;
  line-height: 1.6;
}

.empty-icon {
  font-size: 42px;
  color: var(--text-tertiary);
}
</style>
