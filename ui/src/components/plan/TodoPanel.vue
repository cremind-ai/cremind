<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue';
import { Icon } from '@iconify/vue';
import type { TodoState } from '../../stores/chat';

const props = defineProps<{ state: TodoState }>();

const COLLAPSE_KEY = 'todo_panel_collapsed';
const collapsed = ref(localStorage.getItem(COLLAPSE_KEY) === '1');
function toggleCollapsed() {
  collapsed.value = !collapsed.value;
  localStorage.setItem(COLLAPSE_KEY, collapsed.value ? '1' : '0');
}

const doneCount = computed(() => props.state.items.filter(t => t.status === 'completed').length);
const total = computed(() => props.state.items.length);

// Highlight-then-fade: brighten the panel + flash the changed rows on each
// update, then dim back so the streaming reasoning stays the visual focus.
const bright = ref(false);
const flashIds = ref<Set<string>>(new Set());
let fadeTimer: ReturnType<typeof setTimeout> | null = null;

watch(
  () => props.state.updateSeq,
  () => {
    bright.value = true;
    flashIds.value = new Set(props.state.changedIds);
    if (fadeTimer) clearTimeout(fadeTimer);
    fadeTimer = setTimeout(() => {
      bright.value = false;
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
  <div class="todo-panel" :class="{ bright }">
    <div class="todo-header" @click="toggleCollapsed">
      <Icon icon="mdi:format-list-checks" class="todo-header-icon" />
      <span class="todo-title">Tasks</span>
      <span class="todo-count">{{ doneCount }}/{{ total }}</span>
      <Icon
        :icon="collapsed ? 'mdi:chevron-up' : 'mdi:chevron-down'"
        class="todo-chevron"
      />
    </div>
    <ul v-if="!collapsed" class="todo-list">
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
  </div>
</template>

<style scoped>
.todo-panel {
  position: absolute;
  top: 12px;
  right: 16px;
  width: 280px;
  max-width: calc(100% - 32px);
  max-height: 45%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 10px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  z-index: 6;
  opacity: 0.55;
  transition: opacity 0.4s ease, box-shadow 0.4s ease;
}

.todo-panel.bright {
  opacity: 1;
  box-shadow: 0 6px 20px rgba(37, 99, 235, 0.18);
}

.todo-panel:hover {
  opacity: 1;
}

.todo-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  cursor: pointer;
  user-select: none;
  border-bottom: 1px solid var(--border-color);
}

.todo-header-icon {
  font-size: 16px;
  color: var(--primary-color);
}

.todo-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.todo-count {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-left: auto;
}

.todo-chevron {
  font-size: 16px;
  color: var(--text-tertiary);
}

.todo-list {
  list-style: none;
  margin: 0;
  padding: 6px;
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
  background: rgba(37, 99, 235, 0.10);
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
