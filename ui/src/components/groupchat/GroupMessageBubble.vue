<script setup lang="ts">
// One post in a group room.
//
// Reads as the two-party chat's bubble — same anatomy, same radii, the same copy
// affordance and the same rendered Markdown — because they are the same act, and
// a room that looked like a different product would be a worse one. What differs
// is only what a room has and a two-party chat does not: N speakers rather than
// two, so every left-hand post is identified by a name and a per-sender colour
// instead of by "Agent"; three kinds rather than roles; and the badges that show
// where a post came in from and how far into a chain of agents it is.
//
// The viewer's OWN posts sit on the right in the familiar blue, so the timeline
// still reads as a conversation you are part of rather than a log you are
// watching.
import { computed, onMounted, ref } from 'vue';
import { Icon } from '@iconify/vue';
import { ElMessage } from 'element-plus';
import { useSettingsStore } from '../../stores/settings';
import { useGroupChatStore } from '../../stores/groupChat';
import { useTerminalPanelStore } from '../../stores/terminalPanel';
import { getProcess } from '../../services/processApi';
import { createChatMarked } from '../../utils/markdown';
import { copyTextToClipboard } from '../../utils/clipboard';
import vLinkBlank from '../../directives/v-link-blank';
import ThinkingProcess from '../ThinkingProcess.vue';
import MessageUsageChip from '../MessageUsageChip.vue';
import { senderAvatarColor, senderInitial } from './senderHue';
import { isOwnWebPost } from './senderIdentity';
import type { TerminalAttachment } from '../../stores/chat';
import type { GroupMessage } from '../../services/groupChatApi';

const props = defineProps<{ message: GroupMessage }>();

const settingsStore = useSettingsStore();
const store = useGroupChatStore();
const terminalPanel = useTerminalPanelStore();

// Resolve file paths and /api/ URLs to the backend origin (same rule as
// MessageBubble — a group post can carry the very same links).
const resolveApiUrl = (href: string): string => {
  if (!href) return href;
  const base = settingsStore.agentUrl.replace(/\/$/, '');
  if (href.startsWith('/api/')) {
    return base + href;
  }
  if (!href.startsWith('http://') && !href.startsWith('https://')) {
    return `${base}/api/files/open?path=${encodeURIComponent(href)}`;
  }
  return href;
};

const marked = createChatMarked(resolveApiUrl);

const parsedContent = computed(() => {
  if (!props.message.content) return '';
  return marked.parse(props.message.content) as string;
});

// Shared with the live turn card so an agent keeps one colour whether it is
// still working or has posted (see senderHue.ts).
const avatarColor = computed(() => senderAvatarColor(
  props.message.sender_profile || props.message.sender_name || '?',
));

const initial = computed(() => senderInitial(
  props.message.sender_name || props.message.sender_profile || '?',
));

const isAgent = computed(() => props.message.sender_kind === 'agent');
const isSystem = computed(() => props.message.sender_kind === 'system');

// Shared with the timeline, which aligns this post's routing chip to the same
// side (see senderIdentity.ts).
const isOwnPost = computed(
  () => isOwnWebPost(props.message, settingsStore.profileId),
);

const kindLabel = computed(() => {
  if (isAgent.value) return 'agent';
  if (isSystem.value) return 'system';
  return 'user';
});

// Hop 0 is a human post and the normal case, so only a relayed agent post
// carries the badge — it is the loop guard made visible.
const hopLabel = computed(() =>
  (isAgent.value && props.message.hop > 0) ? `hop ${props.message.hop}` : null,
);

const timeLabel = computed(() =>
  new Date(props.message.created_at).toLocaleTimeString(),
);

const copied = ref(false);

const copyToClipboard = async () => {
  const ok = await copyTextToClipboard(props.message.content || '');
  if (!ok) return;
  copied.value = true;
  setTimeout(() => { copied.value = false; }, 2000);
};

// The reasoning trace lives on the source agent row, so a post without one
// (a human message, or an agent post made through send_group_message from
// outside the room) has no steps to show.
//
// Authorship is checked here as well as at the endpoint: a room shares what an
// agent SAID, not the tool arguments and results behind it, so the trace is
// author-or-admin only. Without this the panel would be offered on every peer's
// post and could do nothing but render a 403.
const canViewSteps = computed(() => {
  if (!isAgent.value || !props.message.source_message_id) return false;
  return store.isAdmin || props.message.sender_profile === settingsStore.profileId;
});

// A turn that spoke mid-flight posts one row per segment and the backend hangs
// the steps on the last of them, so that is where the panel goes — otherwise
// the same trace would appear under every segment of the one answer.
const showReasoning = computed(
  () => canViewSteps.value && store.isLastSegment(props.message.group_id, props.message),
);

const trace = computed(() => store.traceFor(props.message.group_id, props.message));

const terminals = computed<TerminalAttachment[]>(
  () => (showReasoning.value ? trace.value?.terminalAttachments ?? [] : []),
);

// What the turn cost, shown the way the two-party chat shows it. Behind the same
// gate as the steps and on the same (last) segment: the tokens were spent once,
// by one turn, so repeating the count under each of its segments would read as
// several turns' worth.
//
// Addressed by the SEAT's ids, not the room row's. Usage records are keyed by
// the seat message the turn wrote, in the seat conversation — which is exactly
// what `source_message_id`/`source_conversation_id` point at, and which the
// usage endpoint lets this same viewer read.
const usageMessage = computed(() => {
  if (!showReasoning.value) return null;
  const tokenUsage = trace.value?.tokenUsage;
  const id = props.message.source_message_id;
  if (!tokenUsage || !id) return null;
  return { id, tokenUsage };
});

// The steps normally arrive with the row itself (the timeline inlines them) or
// are handed over by the turn this tab watched. This covers what neither
// reaches: a row replayed out of the room's ring, which moves the stream cursor
// past what the catch-up fetch would have re-read. `canViewSteps` already gates
// it, so a member never asks for a peer's trace.
onMounted(() => {
  if (!showReasoning.value) return;
  if (trace.value?.loaded || trace.value?.loading) return;
  void store.loadTrace(props.message.group_id, props.message);
});

const openTerminal = async (term: TerminalAttachment) => {
  // A terminal lives in an in-memory registry that a restart or a TTL clears,
  // so a chip on an old post often points at a process that is gone. Probe
  // first and say so, rather than opening a dead panel.
  try {
    await getProcess(settingsStore.agentUrl, settingsStore.authToken, term.processId);
  } catch {
    ElMessage.warning('This terminal is no longer available.');
    return;
  }
  const seatId = props.message.source_conversation_id
    ?? store.seatIdFor(store.activeGroup, props.message.sender_profile || '');
  if (!seatId) return;
  terminalPanel.openTerminalFor(seatId, term);
};
</script>

<template>
  <div
    class="message-row"
    :class="{ 'own-row': isOwnPost, 'peer-row': !isOwnPost, 'system-row': isSystem }"
  >
    <div
      class="message-avatar"
      :class="{ 'own-avatar': isOwnPost }"
      :style="isOwnPost ? undefined : { background: avatarColor }"
      :title="message.sender_profile || message.sender_name"
    >
      <Icon v-if="isOwnPost" icon="mdi:account-circle" />
      <template v-else>{{ initial }}</template>
    </div>

    <div
      class="message-bubble"
      :class="{
        'own-post': isOwnPost,
        'peer-post': !isOwnPost && !isSystem,
        'system-post': isSystem,
      }"
      :style="(!isOwnPost && !isSystem) ? { borderLeftColor: avatarColor } : undefined"
    >
      <div class="message-header">
        <span class="sender-name">{{ message.sender_name }}</span>
        <span class="kind-tag" :class="`kind-${kindLabel}`">{{ kindLabel }}</span>
        <span v-if="hopLabel" class="meta-tag hop-tag">{{ hopLabel }}</span>
        <span class="message-time">{{ timeLabel }}</span>
        <button
          v-if="message.content"
          type="button"
          class="copy-btn"
          :class="{ copied }"
          :title="copied ? 'Copied!' : 'Copy to clipboard'"
          @click.stop="copyToClipboard"
        >
          <Icon :icon="copied ? 'mdi:check' : 'mdi:content-copy'" />
        </button>
      </div>

      <div class="message-content">
        <div
          v-if="message.content"
          class="text-content marked-content"
          v-html="parsedContent"
          v-link-blank
        ></div>

        <!-- Token usage + estimated cost, resolved against the seat the turn
             ran in — the same chip the two-party chat puts under an answer. -->
        <MessageUsageChip
          v-if="usageMessage"
          :message="usageMessage"
          :conversation-id="message.source_conversation_id"
        />

        <template v-if="showReasoning">
          <ThinkingProcess
            v-if="trace?.thinkingSteps.length"
            :steps="trace.thinkingSteps"
            :is-streaming="false"
            :conversation-id="message.source_conversation_id"
          />
          <div v-else-if="trace?.loaded" class="steps-note">No steps recorded</div>
          <div v-if="trace?.error" class="steps-error">{{ trace.error }}</div>
        </template>

        <div v-if="terminals.length" class="file-carousel-section">
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
  </div>
</template>

<style scoped>
.message-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  animation: messageSlideIn 0.3s ease;
}

/* The viewer's own post reads right-to-left, exactly as in the two-party chat. */
.own-row {
  flex-direction: row-reverse;
}

.system-row {
  opacity: 0.8;
}

@keyframes messageSlideIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
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

.own-avatar {
  background: linear-gradient(135deg, var(--primary-color), var(--primary-light));
  font-size: 1.2rem;
}

.message-bubble {
  padding: 10px;
  width: fit-content;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}

.own-post {
  max-width: 75%;
  background: var(--primary-color);
  color: white;
  border-radius: 18px 18px 4px 18px;
  box-shadow: 0 1px 6px rgba(37, 99, 235, 0.2);
}

.own-post:hover {
  box-shadow: 0 2px 10px rgba(37, 99, 235, 0.28);
}

.peer-post,
.system-post {
  max-width: 85%;
  background: var(--surface-color);
  color: var(--text-primary);
  box-shadow: 0 1px 8px rgba(0, 0, 0, 0.06);
}

/* The accent is the speaker's own colour, so the stripe, the avatar and the
   workspace tab for one agent are all the same hue. */
.peer-post {
  border-radius: 18px 18px 18px 4px;
  border-left: 3px solid var(--primary-color);
}

.peer-post:hover {
  transform: translateY(-1px);
  box-shadow: 0 3px 12px rgba(0, 0, 0, 0.1);
}

/* A room notice is not somebody speaking: no accent, no lift, softer corners. */
.system-post {
  border-radius: 12px;
  border: 1px dashed var(--border-color);
}

[data-theme="dark"] .own-post {
  box-shadow: 0 1px 6px rgba(59, 130, 246, 0.3);
}

[data-theme="dark"] .own-post:hover {
  box-shadow: 0 2px 10px rgba(59, 130, 246, 0.4);
}

[data-theme="dark"] .peer-post {
  box-shadow: 0 1px 8px rgba(0, 0, 0, 0.2);
}

[data-theme="dark"] .peer-post:hover {
  box-shadow: 0 3px 12px rgba(0, 0, 0, 0.3);
}

.message-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.7rem;
  margin-bottom: 4px;
}

.sender-name {
  font-weight: 600;
  font-size: 0.82rem;
}

.peer-post .sender-name,
.system-post .sender-name {
  color: var(--text-primary);
}

.kind-tag,
.meta-tag {
  padding: 0 6px;
  border-radius: 8px;
  font-size: 0.65rem;
  font-weight: 600;
  line-height: 16px;
  background: var(--surface-hover);
  color: var(--text-tertiary);
  border: 1px solid var(--border-color);
}

.kind-tag.kind-agent {
  color: var(--primary-color);
  border-color: var(--primary-color);
  background: rgba(37, 99, 235, 0.08);
}

.kind-tag.kind-system {
  color: var(--warning-color, #f59e0b);
  border-color: var(--warning-color, #f59e0b);
  background: rgba(245, 158, 11, 0.1);
}

/* On the blue bubble the surface-toned chips disappear, so they invert too. */
.own-post .kind-tag,
.own-post .meta-tag {
  background: rgba(255, 255, 255, 0.18);
  border-color: rgba(255, 255, 255, 0.3);
  color: rgba(255, 255, 255, 0.92);
}

.hop-tag {
  font-variant-numeric: tabular-nums;
}

.message-time {
  margin-left: auto;
}

.peer-post .message-time,
.system-post .message-time {
  color: var(--text-tertiary);
}

.own-post .message-time {
  color: rgba(255, 255, 255, 0.7);
}

.copy-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  cursor: pointer;
  padding: 2px;
  border-radius: 4px;
  font-size: 0.85rem;
  opacity: 0;
  transition: opacity 0.15s ease, color 0.15s ease;
  color: var(--text-tertiary);
}

.own-post .copy-btn {
  color: rgba(255, 255, 255, 0.7);
}

.message-bubble:hover .copy-btn {
  opacity: 1;
}

.copy-btn:hover {
  color: var(--primary-color);
}

.own-post .copy-btn:hover {
  color: rgba(255, 255, 255, 0.95);
}

.copy-btn.copied {
  opacity: 1;
  color: var(--success-color);
}

.text-content {
  margin: 0;
  word-wrap: break-word;
}

/* The neutral Markdown rules are shared with the two-party chat
   (styles/markdown.css). Only the inversions for the blue bubble live here. */
.own-post :deep(.marked-content code) {
  background: rgba(255, 255, 255, 0.2);
  color: white;
  border: 1px solid rgba(255, 255, 255, 0.3);
}

.own-post :deep(.marked-content pre) {
  background: rgba(0, 0, 0, 0.15);
  border: 1px solid rgba(255, 255, 255, 0.2);
}

.own-post :deep(.marked-content blockquote) {
  background: rgba(255, 255, 255, 0.12);
  border-left-color: rgba(255, 255, 255, 0.6);
}

.own-post :deep(.marked-content a) {
  color: #bfdbfe;
  text-decoration: underline;
}

.own-post :deep(.marked-content a:hover) {
  color: white;
}

.steps-note {
  margin-top: 10px;
  padding: 5px 12px;
  border: 1px dashed var(--border-color);
  border-radius: 8px;
  font-size: 0.78rem;
  color: var(--text-tertiary);
}

.steps-error {
  margin-top: 6px;
  font-size: 0.75rem;
  color: var(--el-color-danger);
}

/* Terminal chips — same carousel the two-party bubble uses for its artefacts. */
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
