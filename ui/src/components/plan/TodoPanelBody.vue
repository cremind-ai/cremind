<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue';
import { Icon } from '@iconify/vue';
import type { TodoState } from '../../stores/chat';

const props = defineProps<{ state: TodoState }>();
const emit = defineEmits<{ (e: 'update:bright', value: boolean): void }>();

// Highlight-then-fade: flash the changed rows on each update and tell the parent
// window chrome to brighten with them, then dim back so a streaming reasoning
// panel behind stays the visual focus.
const flashIds = ref<Set<string>>(new Set());
let fadeTimer: ReturnType<typeof setTimeout> | null = null;

watch(
  () => props.state.updateSeq,
  () => {
    emit('update:bright', true);
    flashIds.value = new Set(props.state.changedIds);
    if (fadeTimer) clearTimeout(fadeTimer);
    fadeTimer = setTimeout(() => {
      emit('update:bright', false);
      flashIds.value = new Set();
    }, 2500);
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  if (fadeTimer) clearTimeout(fadeTimer);
});

function statusIcon(status: string): string {
  if (status === 'completed') return 'mdi:check-circle';
  if (status === 'in_progress') return 'mdi:progress-clock';
  return 'mdi:checkbox-blank-circle-outline';
}
</script>

<template>
  <ul class="todo-list">
    <li
      v-for="item in state.items"
      :key="item.id"
      class="todo-item"
      :class="[item.status, { flash: flashIds.has(item.id) }]"
    >
      <Icon :icon="statusIcon(item.status)" class="todo-item-icon" />
      <span class="todo-item-text">{{ item.content }}</span>
    </li>
  </ul>
</template>

<style scoped>
.todo-list {
  list-style: none;
  margin: 0;
  padding: 6px;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

.todo-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--text-primary);
  transition: background 1.2s ease;
}

.todo-item.flash {
  background: rgba(37, 99, 235, 0.1);
}

.todo-item-icon {
  flex-shrink: 0;
  font-size: 16px;
  margin-top: 1px;
  color: var(--text-tertiary);
}

.todo-item.in_progress .todo-item-icon {
  color: var(--primary-color);
}

.todo-item.completed .todo-item-icon {
  color: var(--success-color);
}

.todo-item.completed .todo-item-text {
  color: var(--text-tertiary);
  text-decoration: line-through;
}

.todo-item-text {
  line-height: 1.4;
  word-break: break-word;
}
</style>
