<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue';
import { Icon } from '@iconify/vue';
import type { TodoState } from '../../stores/chat';

const props = defineProps<{ state: TodoState }>();

const MAX_KEY = 'todo_panel_maximized';
// `maximized` is a deliberate size preference, so it persists across reloads.
const maximized = ref(localStorage.getItem(MAX_KEY) === '1');
// `minimized` is session-only: the flag is global but todo state is
// per-conversation, so persisting it would pre-hide unrelated task lists.
const minimized = ref(false);

function minimize() {
  minimized.value = true;
}
function restore() {
  // Only un-hide — keep whatever size (normal/maximized) the panel had.
  minimized.value = false;
}
function toggleMaximized() {
  maximized.value = !maximized.value;
  try {
    localStorage.setItem(MAX_KEY, maximized.value ? '1' : '0');
  } catch {
    /* noop */
  }
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
  <button
    v-if="minimized"
    class="todo-pill"
    :class="{ bright }"
    title="Show tasks"
    aria-label="Show tasks"
    @click="restore"
  >
    <Icon icon="mdi:format-list-checks" class="todo-header-icon" />
    <span class="todo-title">Tasks</span>
    <span class="todo-count">{{ doneCount }}/{{ total }}</span>
  </button>

  <div v-else class="todo-panel" :class="{ bright, maximized }">
    <div class="todo-header">
      <Icon icon="mdi:format-list-checks" class="todo-header-icon" />
      <span class="todo-title">Tasks</span>
      <span class="todo-count">{{ doneCount }}/{{ total }}</span>
      <button
        class="todo-action"
        :title="maximized ? 'Restore size' : 'Maximize tasks'"
        :aria-label="maximized ? 'Restore size' : 'Maximize tasks'"
        @click="toggleMaximized"
      >
        <Icon :icon="maximized ? 'mdi:arrow-collapse' : 'mdi:arrow-expand'" />
      </button>
      <button
        class="todo-action"
        title="Minimize tasks"
        aria-label="Minimize tasks"
        @click="minimize"
      >
        <Icon icon="mdi:window-minimize" />
      </button>
    </div>
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
  transition: opacity 0.4s ease, box-shadow 0.4s ease, width 0.25s ease;
}

.todo-panel.bright {
  opacity: 1;
  box-shadow: 0 6px 20px rgba(37, 99, 235, 0.18);
}

.todo-panel:hover {
  opacity: 1;
}

/* Maximized: widen and fill the chat height (top → clear of the composer).
   bottom:116px clears the Workspace restore pill so it stays clickable. The
   width transitions smoothly; the max-height→top/bottom height change snaps. */
.todo-panel.maximized {
  width: 420px;
  bottom: 116px;
  max-height: none;
  opacity: 1;
}

.todo-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  user-select: none;
  flex-shrink: 0;
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

.todo-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 2px 4px;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  border-radius: 6px;
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  transition: background 0.15s ease, color 0.15s ease;
}

.todo-action:hover {
  background: var(--hover-bg);
  color: var(--text-primary);
}

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

/* Minimized: compact pill pinned top-right (matches the panel's theme + dim). */
.todo-pill {
  position: absolute;
  top: 12px;
  right: 16px;
  z-index: 6;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 999px;
  color: var(--text-primary);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  cursor: pointer;
  opacity: 0.55;
  transition: opacity 0.4s ease;
}

.todo-pill:hover,
.todo-pill.bright {
  opacity: 1;
}
</style>
