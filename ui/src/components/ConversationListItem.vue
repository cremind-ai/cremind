<script setup lang="ts">
import { computed } from 'vue';
import { Icon } from '@iconify/vue';
import { ElBadge } from 'element-plus';
import type { SavedConversation } from '../stores/chat';
import { formatRelativeTime } from '../utils/relativeTime';

const props = defineProps<{
  conv: SavedConversation;
  active: boolean;
  streaming: boolean;
  unread: number;
  hasError: boolean;
  /** Ticking "now" (ms) shared by the whole list so timestamps stay in sync. */
  now: number;
  /** True while this row's kebab menu is open — keeps the kebab visible. */
  menuOpen: boolean;
}>();

const emit = defineEmits<{
  select: [];
  menu: [trigger: HTMLElement];
}>();

const relTime = computed(() => formatRelativeTime(props.conv.updatedAt, props.now));

const msgLabel = computed(() => {
  const n = props.conv.messageCount ?? 0;
  return n === 1 ? '1 msg' : `${n} msgs`;
});

const openMenu = (e: MouseEvent) => {
  emit('menu', e.currentTarget as HTMLElement);
};
</script>

<template>
  <ElBadge
    :value="unread"
    :hidden="unread === 0"
    :max="9"
    :type="hasError ? 'danger' : 'primary'"
    class="conversation-badge-wrap"
  >
    <div
      class="conversation-item"
      :class="{ active, streaming, 'menu-open': menuOpen }"
      @click="emit('select')"
    >
      <Icon icon="mdi:message-text-outline" class="conversation-icon" />
      <span v-if="streaming" class="streaming-dot" title="Streaming" />
      <div class="conversation-info">
        <div class="conversation-title" :title="conv.title">{{ conv.title }}</div>
        <div class="conversation-preview">{{ relTime }} · {{ msgLabel }}</div>
      </div>
      <button
        class="conv-menu-btn"
        :class="{ 'is-open': menuOpen }"
        title="Conversation actions"
        @click.stop="openMenu"
      >
        <Icon icon="mdi:dots-vertical" />
      </button>
    </div>
  </ElBadge>
</template>

<style scoped>
/* ElBadge wraps content in an inline-block; force it to fill the panel width
   so the conversation row stays full-width. content-visibility lets offscreen
   rows skip layout/paint in long lists (progressive enhancement). */
.conversation-badge-wrap {
  display: block;
  width: 100%;
  content-visibility: auto;
  contain-intrinsic-size: auto 52px;
}

.conversation-badge-wrap :deep(.el-badge__content) {
  z-index: 2;
}

.conversation-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.2s ease, border-color 0.2s ease;
  border: 1px solid transparent;
}

.conversation-item:hover {
  background: var(--hover-bg);
}

.conversation-item.active {
  background: var(--surface-hover);
  border-color: var(--border-color);
}

.conversation-icon {
  font-size: 18px;
  color: var(--text-secondary);
  flex-shrink: 0;
}

.conversation-item.active .conversation-icon {
  color: var(--primary-color);
}

.streaming-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--primary-color);
  animation: conv-pulse 1.4s ease-in-out infinite;
  flex-shrink: 0;
  margin-left: -4px;
}

@keyframes conv-pulse {
  0%, 100% { opacity: 0.4; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1); }
}

.conversation-info {
  flex: 1;
  min-width: 0;
}

.conversation-title {
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.conversation-preview {
  font-size: 0.75rem;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Reserve the kebab's width so the title never reflows on hover — the button
   is always laid out, only its visibility toggles. */
.conv-menu-btn {
  width: 20px;
  height: 20px;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  padding: 0;
  flex-shrink: 0;
  font-size: 16px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease, background 0.15s ease, color 0.15s ease;
}

.conversation-item:hover .conv-menu-btn,
.conversation-item:focus-within .conv-menu-btn,
.conv-menu-btn.is-open {
  opacity: 1;
  pointer-events: auto;
}

.conv-menu-btn:hover {
  color: var(--primary-color);
  background: var(--hover-bg);
}
</style>
