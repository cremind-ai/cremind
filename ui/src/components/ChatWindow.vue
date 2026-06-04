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

// Indicator label reflects whether the backend is currently summarizing
const thinkingLabel = computed(() => {
  const val = chatStore.isSummarizing ? 'Agent is summarizing...' : 'Agent is thinking...';
  console.log('[indicator] thinkingLabel computed ->', val, 'isSummarizing=', chatStore.isSummarizing);
  return val;
});

watch(() => chatStore.isSummarizing, (v) => {
  console.log('[indicator] isSummarizing changed ->', v);
});

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
