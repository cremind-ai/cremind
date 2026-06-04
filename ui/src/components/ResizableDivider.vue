<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue';

const emit = defineEmits<{
  (e: 'update:width', px: number): void;
}>();

const dragging = ref(false);

function onPointerMove(ev: PointerEvent) {
  if (!dragging.value) return;
  // The right panel extends from the pointer to the right edge of the window.
  const newWidth = window.innerWidth - ev.clientX;
  emit('update:width', newWidth);
}

function onPointerUp() {
  if (!dragging.value) return;
  dragging.value = false;
  document.body.classList.remove('resizing-terminal-panel');
  window.removeEventListener('pointermove', onPointerMove);
  window.removeEventListener('pointerup', onPointerUp);
}

function onPointerDown(ev: PointerEvent) {
  ev.preventDefault();
  dragging.value = true;
  document.body.classList.add('resizing-terminal-panel');
  window.addEventListener('pointermove', onPointerMove);
  window.addEventListener('pointerup', onPointerUp);
}

onBeforeUnmount(() => {
  if (dragging.value) onPointerUp();
});
</script>

<template>
  <div
    class="resizable-divider"
    role="separator"
    aria-orientation="vertical"
    :class="{ dragging }"
    @pointerdown="onPointerDown"
  />
</template>

<style scoped>
.resizable-divider {
  width: 6px;
  flex-shrink: 0;
  cursor: col-resize;
  background: transparent;
  border-left: 1px solid var(--border-color, #1f2937);
  transition: background 0.15s ease;
  user-select: none;
  touch-action: none;
}
.resizable-divider:hover,
.resizable-divider.dragging {
  background: var(--primary-color, #3b82f6);
  border-left-color: var(--primary-color, #3b82f6);
}
</style>

<style>
/* Global: suppress text selection + show a grab cursor while dragging, so
   resizing over the xterm canvas or markdown content doesn't flicker. */
body.resizing-terminal-panel {
  cursor: col-resize !important;
  user-select: none !important;
}
body.resizing-terminal-panel * {
  user-select: none !important;
}
</style>
