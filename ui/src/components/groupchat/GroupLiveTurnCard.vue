<script setup lang="ts">
// One member's turn while it is still running.
//
// Replaces the bare "X is thinking…" row the room used to show. A room is the
// one place where several agents work at once, and a spinner per member says
// only that they are busy — not that one is waiting on a shell and another has
// been reading the same file for a minute. The steps arrive over the room's own
// stream (`seat_event`), so this costs no extra connection.
//
// What a viewer may watch is the server's decision: it never sends a seat frame
// for a profile the viewer may not look behind. The permission check here only
// keeps the empty panel from being drawn — a member watching a peer gets the
// name and the spinner, exactly as before.
import { computed } from 'vue';
import { Icon } from '@iconify/vue';
import { useGroupChatStore } from '../../stores/groupChat';
import { useTerminalPanelStore } from '../../stores/terminalPanel';
import ThinkingProcess from '../ThinkingProcess.vue';
import MessageUsageChip from '../MessageUsageChip.vue';
import { senderAvatarColor, senderInitial } from './senderHue';
import type { TerminalAttachment } from '../../stores/chat';
import type { GroupChat } from '../../services/groupChatApi';

const props = defineProps<{ group: GroupChat | null; profile: string }>();

const store = useGroupChatStore();
const terminalPanel = useTerminalPanelStore();

const name = computed(() => store.nameFor(props.profile));
const avatarColor = computed(() => senderAvatarColor(props.profile || name.value));
const initial = computed(() => senderInitial(name.value));

const live = computed(() => store.liveTurnFor(props.group?.id ?? null, props.profile));

const mayWatch = computed(
  () => store.visibleSeatProfiles(props.group).includes(props.profile),
);

const steps = computed(() => live.value?.thinkingSteps ?? []);
const terminals = computed<TerminalAttachment[]>(
  () => live.value?.terminalAttachments ?? [],
);

// Tokens as they are spent. The seat's `token_usage` frames are already
// mirrored into the room, so this costs nothing new — and the count carries
// over to the finished post (`adoptLiveTurn`), so the chip does not blink out
// when the answer lands. Cost stays absent until then: the usage records that
// price it are written at the end of the turn.
const usageMessage = computed(() => {
  const tokenUsage = live.value?.tokenUsage;
  if (!mayWatch.value || !tokenUsage) return null;
  return { id: `live-${props.profile}`, tokenUsage };
});

// The tab is already open — the store opens it into the seat's bucket the
// moment the process starts — so this only brings it to the front.
const openTerminal = (term: TerminalAttachment) => {
  const seatId = live.value?.conversationId
    ?? store.seatIdFor(props.group, props.profile);
  if (!seatId) return;
  terminalPanel.openTerminalFor(seatId, term);
};
</script>

<template>
  <div class="message-row">
    <div
      class="message-avatar"
      :style="{ background: avatarColor }"
      :title="profile"
    >{{ initial }}</div>

    <!-- The same bubble a finished post gets, so the card does not visibly
         become a different object when the turn ends and the answer lands. -->
    <div class="message-bubble live-post" :style="{ borderLeftColor: avatarColor }">
      <div class="live-header">
        <span class="sender-name">{{ name }}</span>
        <Icon icon="mdi:loading" class="spinner-icon" />
        <span class="live-label">is thinking...</span>
      </div>

      <!-- No `request-sent-at`: the room learns of a turn from its first step,
           so the only start time available here IS that step, and passing it
           would label every first step "0ms". -->
      <ThinkingProcess
        v-if="mayWatch && steps.length"
        :steps="steps"
        :is-streaming="true"
        :conversation-id="live?.conversationId ?? null"
      />

      <MessageUsageChip
        v-if="usageMessage"
        :message="usageMessage"
        :conversation-id="live?.conversationId ?? null"
      />

      <div v-if="mayWatch && terminals.length" class="file-carousel-section">
        <div class="file-carousel">
          <div
            v-for="term in terminals"
            :key="`term-${term.processId}`"
            class="file-chip terminal-chip"
            :title="term.command"
            role="button"
            @click="openTerminal(term)"
          >
            <Icon icon="mdi:console" class="file-chip-icon terminal-icon" />
            <span class="file-chip-name">{{ term.commandShort || term.command }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.message-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.message-avatar {
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  font-size: 0.85rem;
  font-weight: 700;
  margin-top: 2px;
  user-select: none;
  transition: transform 0.2s ease;
}

.message-avatar:hover {
  transform: scale(1.05);
}

.message-bubble {
  padding: 10px;
  width: fit-content;
  max-width: 85%;
}

.live-post {
  background: var(--surface-color);
  color: var(--text-primary);
  border-radius: 18px 18px 18px 4px;
  border-left: 3px solid var(--primary-color);
  box-shadow: 0 1px 8px rgba(0, 0, 0, 0.06);
}

[data-theme="dark"] .live-post {
  box-shadow: 0 1px 8px rgba(0, 0, 0, 0.2);
}

.live-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.7rem;
  margin-bottom: 4px;
}

.sender-name {
  font-weight: 600;
  font-size: 0.82rem;
  color: var(--text-primary);
}

.spinner-icon {
  font-size: 14px;
  color: var(--primary-color);
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.live-label {
  font-size: 0.75rem;
  color: var(--text-tertiary);
  animation: pulse-opacity 2s ease-in-out infinite;
}

@keyframes pulse-opacity {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

/* Terminal chips — identical to the ones a finished post carries, so a shell
   opened mid-turn does not change shape when the turn ends. */
.file-carousel-section {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border-color);
}

.file-carousel {
  display: flex;
  align-items: center;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 4px;
  scrollbar-width: thin;
  scrollbar-color: var(--border-color) transparent;
}

.file-carousel::-webkit-scrollbar {
  height: 4px;
}

.file-carousel::-webkit-scrollbar-thumb {
  background: var(--border-color);
  border-radius: 2px;
}

.file-chip {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  min-width: 140px;
  max-width: 200px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  flex-shrink: 0;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.file-chip.terminal-chip {
  background: #0f172a;
  border-color: #1f2937;
  cursor: pointer;
}

.file-chip.terminal-chip:hover {
  border-color: var(--primary-color);
  background: #111a2e;
}

.file-chip-icon {
  font-size: 1.3em;
  flex-shrink: 0;
}

.file-chip-icon.terminal-icon {
  color: #22c55e;
}

.file-chip-name {
  font-size: 0.78rem;
  font-weight: 500;
  color: #cbd5f5;
  font-family: Consolas, Monaco, 'Courier New', monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
