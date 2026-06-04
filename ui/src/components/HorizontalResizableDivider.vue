<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue';

const props = defineProps<{
  containerEl: HTMLElement | null;
}>();

const emit = defineEmits<{
  (e: 'update:ratio', r: number): void;
}>();

const dragging = ref(false);

function onPointerMove(ev: PointerEvent) {
  if (!dragging.value || !props.containerEl) return;
  const rect = props.containerEl.getBoundingClientRect();
  if (rect.height <= 0) return;
  const ratio = (ev.clientY - rect.top) / rect.height;
  emit('update:ratio', ratio);
}

function onPointerUp() {
  if (!dragging.value) return;
  dragging.value = false;
  document.body.classList.remove('resizing-tree-panel');
  window.removeEventListener('pointermove', onPointerMove);
  window.removeEventListener('pointerup', onPointerUp);
}

function onPointerDown(ev: PointerEvent) {
  ev.preventDefault();
  dragging.value = true;
  document.body.classList.add('resizing-tree-panel');
  window.addEventListener('pointermove', onPointerMove);
  window.addEventListener('pointerup', onPointerUp);
}

onBeforeUnmount(() => {
  if (dragging.value) onPointerUp();
});
</script>

<template>
  <div
    class="horizontal-resizable-divider"
    role="separator"
    aria-orientation="horizontal"
    :class="{ dragging }"
    @pointerdown="onPointerDown"
  />
</template>

<style scoped>
.horizontal-resizable-divider {
  height: 6px;
  flex-shrink: 0;
  cursor: row-resize;
  background: transparent;
  border-top: 1px solid var(--border-color, #1f2937);
  border-bottom: 1px solid var(--border-color, #1f2937);
  transition: background 0.15s ease;
  user-select: none;
  touch-action: none;
}
.horizontal-resizable-divider:hover,
.horizontal-resizable-divider.dragging {
  background: var(--primary-color, #3b82f6);
  border-color: var(--primary-color, #3b82f6);
}
</style>

<style>
body.resizing-tree-panel {
  cursor: row-resize !important;
  user-select: none !important;
}
body.resizing-tree-panel * {
  user-select: none !important;
}
</style>
