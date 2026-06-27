<script setup lang="ts">
import { ref, watch, nextTick, computed } from 'vue';
import type { ChatMessage } from '../stores/chat';
import { useChatStore } from '../stores/chat';
import MessageBubble from './MessageBubble.vue';
import ThinkingIndicator from './ThinkingIndicator.vue';
import { Icon } from '@iconify/vue';

const props = defineProps<{
  messages: ChatMessage[];
  isStreaming?: boolean;
}>();

const chatStore = useChatStore();

const scrollContainer = ref<HTMLElement | null>(null);

// Auto-scroll to bottom when new messages arrive
watch(
  () => props.messages.length,
  async () => {
    await nextTick();
    scrollToBottom();
  }
);

// Also scroll when streaming state changes
watch(
  () => props.isStreaming,
  async () => {
    await nextTick();
    scrollToBottom();
  }
);

// Auto-scroll when streaming content changes (thinking steps, text, artifacts)
const streamingContentKey = computed(() => {
  if (!props.isStreaming || props.messages.length === 0) return 0;
  const last = props.messages[props.messages.length - 1];
  return (last.content?.length || 0) + (last.thinkingSteps?.length || 0) * 100;
});

const thinkingLabel = computed(() => 'Agent is thinking...');

// Compaction suggestion popup (suggest-only; never forced).
const compaction = computed(() => chatStore.compactionSuggestion);
const compacting = ref(false);

const formatTokens = (n: number): string =>
  n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);

const doCompact = async () => {
  const id = chatStore.activeConversationId;
  if (!id || compacting.value) return;
  compacting.value = true;
  try {
    await chatStore.compactConversation(id);
  } finally {
    compacting.value = false;
  }
};

const dismissCompaction = () => {
  const id = chatStore.activeConversationId;
  if (id) chatStore.dismissCompaction(id);
};

watch(streamingContentKey, async () => {
  if (props.isStreaming) {
    await nextTick();
    scrollToBottom();
  }
});

const scrollToBottom = () => {
  if (scrollContainer.value) {
    scrollContainer.value.scrollTop = scrollContainer.value.scrollHeight;
  }
};
</script>

<template>
  <div class="chat-window" ref="scrollContainer">
    <div v-if="messages.length === 0" class="empty-state">
      <div class="empty-card">
        <div class="empty-icon">
          <Icon icon="mdi:message-text-outline" />
        </div>
        <h3 class="empty-title">No messages yet</h3>
        <p class="empty-subtitle">Send a message to get started</p>
      </div>
    </div>
    
    <TransitionGroup v-else name="message-list" tag="div" class="messages-list">
      <MessageBubble 
        v-for="message in messages" 
        :key="message.id" 
        :message="message"
      />
    </TransitionGroup>

    <ThinkingIndicator v-if="isStreaming" :label="thinkingLabel" />

    <!-- Compaction suggestion: subtle, non-blocking; suggest-only (never forced). -->
    <Transition name="compaction-fade">
      <div v-if="compaction && !isStreaming" class="compaction-banner">
        <Icon icon="mdi:archive-arrow-down-outline" class="compaction-icon" />
        <div class="compaction-text">
          <strong>This chat is getting long.</strong>
          Compacting summarizes earlier turns to free context (saving
          ~{{ formatTokens(compaction.estimatedSavings) }} of
          {{ formatTokens(compaction.currentTokens) }} tokens), keeping replies fast
          and focused. If you're mid-task, you can ignore this and compact right
          after you're done — your context won't be lost until you do.
        </div>
        <div class="compaction-actions">
          <button class="compaction-btn primary" :disabled="compacting" @click="doCompact">
            {{ compacting ? 'Compacting…' : 'Compact' }}
          </button>
          <button class="compaction-btn" :disabled="compacting" @click="dismissCompaction">
            Dismiss
          </button>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.chat-window {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 20px;
  background: var(--bg-color);
  display: flex;
  flex-direction: column;
  position: relative;
  scroll-behavior: smooth;
}

.empty-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  position: relative;
}

.compaction-banner {
  position: sticky;
  bottom: 8px;
  margin: 8px 4px 0;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 10px;
  background: var(--surface-color, #fff);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
  font-size: 0.82rem;
  line-height: 1.45;
  color: var(--text-secondary);
}
.compaction-icon { font-size: 1.1rem; margin-top: 1px; color: var(--primary-color); flex-shrink: 0; }
.compaction-text { flex: 1; }
.compaction-text strong { color: var(--text-primary); }
.compaction-actions { display: flex; gap: 6px; flex-shrink: 0; align-self: center; }
.compaction-btn {
  border: 1px solid var(--border-color, #e4e7ed);
  background: transparent;
  color: var(--text-secondary);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 0.78rem;
  cursor: pointer;
}
.compaction-btn.primary {
  background: var(--primary-color);
  border-color: var(--primary-color);
  color: #fff;
}
.compaction-btn:disabled { opacity: 0.6; cursor: default; }
.compaction-fade-enter-active, .compaction-fade-leave-active { transition: opacity 0.2s ease; }
.compaction-fade-enter-from, .compaction-fade-leave-to { opacity: 0; }

.empty-card {
  max-width: 400px;
  text-align: center;
}

.empty-icon {
  width: 64px;
  height: 64px;
  margin: 0 auto 20px;
  color: var(--text-tertiary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 48px;
}

.empty-title {
  margin: 0 0 8px 0;
  font-size: 1.25em;
  font-weight: 600;
  color: var(--text-primary);
}

.empty-subtitle {
  margin: 0;
  color: var(--text-secondary);
  font-size: 0.95em;
  line-height: 1.6;
}

.messages-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
  position: relative;
}
</style>
