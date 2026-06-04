<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue';
import { Icon } from '@iconify/vue';

export type FileContextAction =
  | 'download'
  | 'rename'
  | 'delete'
  | 'upload'
  | 'mkdir'
  | 'refresh';

export interface FileContextMenuItem {
  action: FileContextAction;
  label: string;
  icon: string;
  danger?: boolean;
}

const props = defineProps<{
  x: number;
  y: number;
  items: FileContextMenuItem[];
}>();

const emit = defineEmits<{
  (e: 'pick', action: FileContextAction): void;
  (e: 'close'): void;
}>();

function pick(action: FileContextAction) {
  emit('pick', action);
}

function onWindowMouseDown(ev: MouseEvent) {
  // Close on outside click. Inside-click is delivered to the menu button,
  // which fires its own ``pick`` (and we close in response) — DON'T close
  // here on inside-click or the button receives no click event.
  const target = ev.target as Element | null;
  if (target && target.closest('.ctx-menu')) return;
  emit('close');
}

function onKey(ev: KeyboardEvent) {
  if (ev.key === 'Escape') emit('close');
}

onMounted(() => {
  // Defer registration to the next tick so the right-click that *opened*
  // this menu doesn't immediately close it.
  setTimeout(() => {
    window.addEventListener('mousedown', onWindowMouseDown, true);
  }, 0);
  window.addEventListener('keydown', onKey);
});
onBeforeUnmount(() => {
  window.removeEventListener('mousedown', onWindowMouseDown, true);
  window.removeEventListener('keydown', onKey);
});
</script>

<template>
  <Teleport to="body">
    <div
      class="ctx-menu"
      :style="{ left: props.x + 'px', top: props.y + 'px' }"
      role="menu"
      @mousedown.stop
      @click.stop
      @contextmenu.prevent
    >
      <button
        v-for="item in props.items"
        :key="item.action"
        class="ctx-item"
        :class="{ danger: item.danger }"
        role="menuitem"
        @click="pick(item.action)"
      >
        <Icon class="ctx-icon" :icon="item.icon" />
        <span class="ctx-label">{{ item.label }}</span>
      </button>
    </div>
  </Teleport>
</template>

<style scoped>
.ctx-menu {
  position: fixed;
  z-index: 9999;
  min-width: 168px;
  padding: 4px 0;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
  font-size: 0.82rem;
  color: var(--text-primary);
  user-select: none;
}
[data-theme="dark"] .ctx-menu {
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
}
.ctx-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 12px;
  background: transparent;
  border: none;
  color: inherit;
  text-align: left;
  cursor: pointer;
  font-family: inherit;
}
.ctx-item:hover {
  background: var(--hover-bg);
}
.ctx-item.danger {
  color: var(--danger-color);
}
.ctx-item.danger:hover {
  background: var(--danger-color);
  color: #fff;
}
.ctx-icon {
  font-size: 1rem;
  flex-shrink: 0;
}
.ctx-label {
  flex: 1 1 auto;
}
</style>
