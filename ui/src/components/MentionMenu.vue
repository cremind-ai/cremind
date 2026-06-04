<script setup lang="ts">
import { computed } from 'vue';

export interface MentionItem {
  name: string;
  description?: string;
}

const props = defineProps<{
  visible: boolean;
  items: MentionItem[];
  top: number;
  left: number;
  activeIndex: number;
  prefix: '$' | '@';
}>();

const emit = defineEmits<{
  select: [item: MentionItem];
}>();

const style = computed(() => ({
  top: `${props.top}px`,
  left: `${props.left}px`,
}));
</script>

<template>
  <Teleport to="body">
    <ul
      v-if="visible && items.length > 0"
      class="mention-menu"
      :style="style"
      role="listbox"
      @mousedown.prevent
    >
      <li
        v-for="(item, idx) in items"
        :key="item.name"
        :class="['mention-item', { active: idx === activeIndex }]"
        role="option"
        :aria-selected="idx === activeIndex"
        @click="emit('select', item)"
      >
        <span class="mention-name">{{ prefix }}{{ item.name }}</span>
        <span v-if="item.description" class="mention-desc">{{ item.description }}</span>
      </li>
    </ul>
  </Teleport>
</template>

<style scoped>
.mention-menu {
  position: fixed;
  z-index: 3000;
  margin: 0;
  padding: 4px 0;
  list-style: none;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  min-width: 220px;
  max-width: 360px;
  max-height: 240px;
  overflow-y: auto;
  font-size: 13px;
  transform: translateY(-100%);
}

.mention-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 10px;
  cursor: pointer;
  color: var(--text-primary);
}

.mention-item:hover,
.mention-item.active {
  background: rgba(37, 99, 235, 0.1);
}

.mention-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
  color: var(--primary-color);
}

.mention-desc {
  font-size: 11.5px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
