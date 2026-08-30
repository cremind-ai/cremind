<script setup lang="ts">
// The room's workspace, tabbed by agent.
//
// Several agents work at once in a room, each from its own hidden seat with its
// own working directory and its own terminals. One file tree and one terminal
// stack are enough to show them all — the panel store already scopes the whole
// workspace to a "focus conversation", which is exactly how the event-run
// drawer borrows it — but only if the user can never be in doubt about WHICH
// agent they are looking at. A cwd or a shell attributed to the wrong agent is
// worse than no panel at all, so the tab strip and the agent name in the panel
// header are the point of this component, not decoration on it.
import { computed, onBeforeUnmount, watch } from 'vue';
import { ElTooltip } from 'element-plus';
import RightPanel from '../RightPanel.vue';
import { useGroupChatStore } from '../../stores/groupChat';
import { useTerminalPanelStore } from '../../stores/terminalPanel';
import { senderAvatarColor, senderInitial } from './senderHue';

const props = defineProps<{
  groupId: string | null;
  /**
   * The seats this viewer may look behind, in member order — its own, plus
   * every other member's for the admin. Passed in rather than derived here
   * because the view needs the same list for the picker it offers while this
   * panel is hidden.
   */
  seats: { profile: string; name: string; seatId: string }[];
  /** Selected agent, as a profile id. Owned by the view for the same reason. */
  modelValue: string | null;
}>();

const emit = defineEmits<{ (e: 'update:modelValue', profile: string): void }>();

const store = useGroupChatStore();
const terminalPanel = useTerminalPanelStore();

// Falling back to the first seat keeps the panel on something real while the
// view settles its selection (a room switch replaces the whole seat list).
const selected = computed(
  () => props.seats.find((s) => s.profile === props.modelValue) ?? props.seats[0] ?? null,
);

const panelTitle = computed(() =>
  selected.value ? `${selected.value.name} — workspace` : 'Workspace',
);

const isThinking = (profile: string) =>
  (store.agentStatusByGroup[props.groupId ?? ''] ?? {})[profile] === 'thinking';

// Terminals are bucketed per conversation, so a tab can advertise work waiting
// behind it: an agent that opened a shell while the user was reading another
// agent's tree would otherwise leave no trace anywhere on screen.
const terminalCount = (seatId: string) =>
  (terminalPanel.focusTerminalsByConversation[seatId] ?? []).length;

// Selecting a tab is one store call: cwd, file tree and terminal tabs all read
// through the focus. Immediate, because mounting the panel IS the selection.
watch(
  () => selected.value?.seatId ?? null,
  (seatId) => { terminalPanel.setFocusConversation(seatId); },
  { immediate: true },
);

// Hiding the panel (or leaving the room) hands the workspace back to whatever
// the rest of the app is showing. A seat focus left behind would point the
// Conversations page at another profile's hidden conversation.
onBeforeUnmount(() => { terminalPanel.setFocusConversation(null); });
</script>

<template>
  <div class="group-right-panel">
    <!-- One agent needs no tabs: the panel header already names it. The strip
         is also pointless at 36px, so the collapsed panel drops it. -->
    <div v-if="seats.length > 1 && !terminalPanel.collapsed" class="agent-tabs">
      <button
        v-for="seat in seats"
        :key="seat.profile"
        type="button"
        class="agent-tab"
        :class="{ active: seat.profile === selected?.profile }"
        :title="`${seat.name} (${seat.profile})`"
        @click="emit('update:modelValue', seat.profile)"
      >
        <span
          class="tab-avatar"
          :style="{ background: senderAvatarColor(seat.profile) }"
        >{{ senderInitial(seat.name) }}</span>
        <span class="tab-name">{{ seat.name }}</span>
        <ElTooltip
          v-if="isThinking(seat.profile)"
          :content="`${seat.name} is working`"
          placement="bottom"
          :show-after="300"
        >
          <span class="tab-thinking" />
        </ElTooltip>
        <ElTooltip
          v-if="terminalCount(seat.seatId) > 0"
          :content="`${terminalCount(seat.seatId)} open terminal(s)`"
          placement="bottom"
          :show-after="300"
        >
          <span class="tab-badge">{{ terminalCount(seat.seatId) }}</span>
        </ElTooltip>
      </button>
    </div>

    <RightPanel class="panel-host" :title="panelTitle" />
  </div>
</template>

<style scoped>
.group-right-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  /* Same ground as RightPanel, so the strip reads as part of the panel rather
     than as a floating toolbar over the room. */
  background: #0b1220;
  overflow: hidden;
}

.agent-tabs {
  display: flex;
  flex-shrink: 0;
  gap: 4px;
  padding: 6px 6px 0 6px;
  overflow-x: auto;
  background: #0f172a;
  border-bottom: 1px solid #1f2937;
}

.agent-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
  max-width: 180px;
  padding: 5px 10px;
  background: transparent;
  border: 1px solid transparent;
  border-bottom: none;
  border-radius: 6px 6px 0 0;
  color: #94a3b8;
  font-size: 0.78rem;
  cursor: pointer;
}

.agent-tab:hover {
  background: #1e293b;
  color: #e5e7eb;
}

.agent-tab.active {
  background: #0b1220;
  border-color: #1f2937;
  color: #e5e7eb;
}

.tab-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  border-radius: 50%;
  color: white;
  font-size: 0.62rem;
  font-weight: 700;
  user-select: none;
}

.tab-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tab-thinking {
  width: 7px;
  height: 7px;
  flex-shrink: 0;
  border-radius: 50%;
  background: var(--warning-color, #f59e0b);
  animation: tab-pulse 1.2s ease-in-out infinite;
}

@keyframes tab-pulse {
  0%, 100% { opacity: 0.35; }
  50% { opacity: 1; }
}

.tab-badge {
  flex-shrink: 0;
  min-width: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: #1e293b;
  border: 1px solid #334155;
  color: #cbd5f5;
  font-size: 0.62rem;
  line-height: 15px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.panel-host {
  flex: 1 1 auto;
  min-height: 0;
}
</style>
