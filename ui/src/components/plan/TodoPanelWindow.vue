<script setup lang="ts">
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue';
import { Icon } from '@iconify/vue';
import TodoPanelBody from './TodoPanelBody.vue';
import { useTodoPanelsStore, type TodoPanelWindow } from '../../stores/todoPanels';

const props = defineProps<{ panel: TodoPanelWindow; focused: boolean }>();

const panels = useTodoPanelsStore();

const PANEL_W = 300;

const doneCount = computed(
  () => props.panel.state.items.filter(t => t.status === 'completed').length,
);
const total = computed(() => props.panel.state.items.length);
const isRunning = computed(() => props.panel.status === 'running');
const isCompleted = computed(() => props.panel.status === 'completed');

// Brightness pulse driven from the body's flash watcher.
const bright = ref(false);

// One-shot focus pop when this window is raised to the front.
const focusPop = ref(false);
watch(
  () => props.focused,
  (now, was) => {
    if (now && !was) {
      focusPop.value = true;
      setTimeout(() => {
        focusPop.value = false;
      }, 240);
    }
  },
);

onMounted(() => {
  // Tidy the store's one-shot enter origin after the enter transition has run.
  if (props.panel.origin) {
    setTimeout(() => panels.clearOrigin(props.panel.key), 320);
  }
});

// Position + animation CSS vars. `origin` drives the grow-from-chip ENTER;
// `exitOrigin` (set just before removal) drives the collapse-toward-chip LEAVE.
// Reactive so the exit vars are on the element before the leave transition runs.
const rootStyle = computed(() => {
  const cx = props.panel.x + PANEL_W / 2;
  const cy = props.panel.y + 20;
  const style: Record<string, string | number> = {
    left: `${props.panel.x}px`,
    top: `${props.panel.y}px`,
    zIndex: props.panel.zIndex,
  };
  const o = props.panel.origin;
  if (o) {
    style['--from-dx'] = `${o.x - cx}px`;
    style['--from-dy'] = `${o.y - cy}px`;
    style['--from-scale'] = '0.25';
  }
  const e = props.panel.exitOrigin;
  if (e) {
    style['--to-dx'] = `${e.x - cx}px`;
    style['--to-dy'] = `${e.y - cy}px`;
    style['--to-scale'] = '0.25';
  }
  return style;
});

function raise() {
  panels.focusPanel(props.panel.key);
}

function toggleMaximized() {
  panels.setMaximized(props.panel.key, !props.panel.maximized);
}

function minimize() {
  panels.setMinimized(props.panel.key, true);
}

function restore() {
  panels.setMinimized(props.panel.key, false);
}

// Resolve the center of this panel's chip (on the final bubble), if it's in the
// DOM, so close collapses the panel toward it.
function chipCenter(): { x: number; y: number } | null {
  const mid = props.panel.messageId;
  if (!mid) return null;
  const el = document.querySelector(`[data-todo-chip="${CSS.escape(mid)}"]`);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

function close() {
  panels.closePanel(props.panel.key, { toward: chipCenter() });
}

// ── drag-to-move (pointer events, mirrors ResizableDivider) ───────────────
const dragging = ref(false);
let startX = 0;
let startY = 0;
let baseX = 0;
let baseY = 0;

function onHeaderPointerDown(ev: PointerEvent) {
  // Let the header buttons handle their own clicks.
  if ((ev.target as HTMLElement).closest('button')) return;
  ev.preventDefault();
  dragging.value = true;
  startX = ev.clientX;
  startY = ev.clientY;
  baseX = props.panel.x;
  baseY = props.panel.y;
  document.body.classList.add('dragging-todo-window');
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
  window.addEventListener('pointercancel', onUp);
}

function onMove(ev: PointerEvent) {
  if (!dragging.value) return;
  panels.setPosition(
    props.panel.key,
    baseX + (ev.clientX - startX),
    baseY + (ev.clientY - startY),
  );
}

function onUp() {
  if (!dragging.value) return;
  dragging.value = false;
  document.body.classList.remove('dragging-todo-window');
  window.removeEventListener('pointermove', onMove);
  window.removeEventListener('pointerup', onUp);
  window.removeEventListener('pointercancel', onUp);
}

onBeforeUnmount(onUp);
</script>

<template>
  <!-- Minimized: a compact pill in the tray. -->
  <button
    v-if="panel.minimized"
    class="todo-pill"
    :class="{ bright: isRunning }"
    :title="panel.title"
    @click="restore"
  >
    <Icon icon="mdi:format-list-checks" class="todo-header-icon" />
    <span class="todo-title">{{ panel.title }}</span>
    <Icon v-if="isRunning" icon="mdi:loading" class="todo-spin" />
    <span v-else class="todo-count">{{ doneCount }}/{{ total }}</span>
  </button>

  <!-- Full window. -->
  <div
    v-else
    class="todo-window"
    :class="{ bright, focused, maximized: panel.maximized, dragging, 'focus-pop': focusPop }"
    :style="rootStyle"
    @pointerdown.capture="raise"
  >
    <div class="todo-header" @pointerdown="onHeaderPointerDown">
      <Icon icon="mdi:format-list-checks" class="todo-header-icon" />
      <span class="todo-title" :title="panel.subtitle || panel.title">{{ panel.title }}</span>
      <Icon
        v-if="isRunning"
        icon="mdi:loading"
        class="todo-status-icon todo-spin"
      />
      <Icon
        v-else-if="isCompleted"
        icon="mdi:check-circle"
        class="todo-status-icon ok"
      />
      <span class="todo-count">{{ doneCount }}/{{ total }}</span>
      <button
        class="todo-action"
        :title="panel.maximized ? 'Restore size' : 'Maximize'"
        :aria-label="panel.maximized ? 'Restore size' : 'Maximize'"
        @click="toggleMaximized"
      >
        <Icon :icon="panel.maximized ? 'mdi:arrow-collapse' : 'mdi:arrow-expand'" />
      </button>
      <button class="todo-action" title="Minimize" aria-label="Minimize" @click="minimize">
        <Icon icon="mdi:window-minimize" />
      </button>
      <button class="todo-action" title="Close" aria-label="Close" @click="close">
        <Icon icon="mdi:close" />
      </button>
    </div>

    <p v-if="panel.subtitle" class="todo-subtitle" :title="panel.subtitle">{{ panel.subtitle }}</p>

    <TodoPanelBody :state="panel.state" @update:bright="bright = $event" />
  </div>
</template>

<style scoped>
.todo-window {
  position: absolute;
  width: 300px;
  max-width: calc(100vw - 24px);
  max-height: 55vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 10px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  opacity: 0.78;
  pointer-events: auto;
  transition: opacity 0.25s ease, box-shadow 0.25s ease, border-color 0.25s ease,
    width 0.2s ease;
}

.todo-window:hover,
.todo-window.bright {
  opacity: 1;
}

.todo-window.focused {
  opacity: 1;
  border-color: color-mix(in srgb, var(--primary-color) 45%, var(--border-color));
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.22), 0 0 0 1px rgba(37, 99, 235, 0.25);
}

.todo-window.bright {
  box-shadow: 0 6px 20px rgba(37, 99, 235, 0.18);
}

.todo-window.maximized {
  width: 460px;
  max-height: 72vh;
}

/* Never transition left/top — position is driven live during drag. */
.todo-window.dragging {
  transition: none;
  user-select: none;
}

.todo-window.focus-pop {
  animation: todoFocusPop 0.22s ease;
}

@keyframes todoFocusPop {
  0% { transform: scale(1); }
  45% { transform: scale(1.015); }
  100% { transform: scale(1); }
}

.todo-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  user-select: none;
  cursor: grab;
  touch-action: none;
  flex-shrink: 0;
  border-bottom: 1px solid var(--border-color);
}

.todo-window.dragging .todo-header {
  cursor: grabbing;
}

.todo-header-icon {
  font-size: 16px;
  color: var(--primary-color);
  flex-shrink: 0;
}

.todo-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 150px;
}

.todo-status-icon {
  font-size: 15px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}
.todo-status-icon.ok {
  color: var(--success-color);
}

.todo-count {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-left: auto;
  font-variant-numeric: tabular-nums;
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

.todo-subtitle {
  margin: 0;
  padding: 6px 12px 0;
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex-shrink: 0;
}

/* Minimized pill (docked in the layer's tray). */
.todo-pill {
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
  pointer-events: auto;
  opacity: 0.85;
  transition: opacity 0.25s ease;
  max-width: 220px;
}

.todo-pill:hover,
.todo-pill.bright {
  opacity: 1;
}

.todo-pill .todo-title {
  max-width: 120px;
}

.todo-spin {
  animation: todo-spin 1s linear infinite;
}

@keyframes todo-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
