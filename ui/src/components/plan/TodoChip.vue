<script setup lang="ts">
import { computed } from 'vue';
import { Icon } from '@iconify/vue';
import type { ChatMessage } from '../../stores/chat';
import { useChatStore } from '../../stores/chat';
import { useTodoPanelsStore, messagePanelKey } from '../../stores/todoPanels';

const props = defineProps<{
  message: ChatMessage;
  conversationId?: string | null;
}>();

const panels = useTodoPanelsStore();

const items = computed(() => props.message.planTodos ?? []);
const total = computed(() => items.value.length);
const done = computed(() => items.value.filter(t => t.status === 'completed').length);
const completed = computed(() => props.message.planStage === 'completed');
// Stable id shared with the floating panel's key (`msg:<id>`) and used as the
// close-toward target — matches `backendId ?? id` (see openPanel).
const messageId = computed(() => props.message.backendId ?? props.message.id);

function openPanel(ev: MouseEvent) {
  const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
  const cid = props.conversationId ?? useChatStore().activeConversationId ?? '';
  panels.upsertPanel(
    {
      key: messagePanelKey(messageId.value),
      source: 'message',
      conversationId: cid,
      messageId: messageId.value,
      title: 'Plan tasks',
      subtitle: props.message.timestamp
        ? props.message.timestamp.toLocaleString()
        : undefined,
      todos: items.value,
    },
    {
      origin: { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
      reveal: true,
    },
  );
}
</script>

<template>
  <button
    class="todo-chip"
    :class="{ completed }"
    :data-todo-chip="messageId"
    :title="`Show plan tasks (${done}/${total})`"
    @click="openPanel"
  >
    <Icon icon="mdi:format-list-checks" class="todo-chip-icon" />
    <span class="todo-chip-label">{{ done }}/{{ total }} tasks</span>
  </button>
</template>

<style scoped>
.todo-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
  padding: 6px 12px;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 999px;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.4;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
}

.todo-chip:hover {
  background: var(--hover-bg);
  color: var(--text-primary);
  border-color: color-mix(in srgb, var(--primary-color) 40%, var(--border-color));
}

.todo-chip-icon {
  font-size: 14px;
  color: var(--primary-color);
}

.todo-chip.completed .todo-chip-icon {
  color: var(--success-color);
}

.todo-chip-label {
  font-variant-numeric: tabular-nums;
}
</style>
