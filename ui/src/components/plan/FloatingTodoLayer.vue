<script setup lang="ts">
import { computed, ref, watch, nextTick, onMounted, onBeforeUnmount } from 'vue';
import { useRoute } from 'vue-router';
import { useZIndex } from 'element-plus';
import TodoPanelWindow from './TodoPanelWindow.vue';
import { useTodoPanelsStore } from '../../stores/todoPanels';
import { useChatStore } from '../../stores/chat';
import { useEventRunsStore } from '../../stores/eventRuns';

const panels = useTodoPanelsStore();
const chat = useChatStore();
const eventRuns = useEventRunsStore();
const route = useRoute();
const { nextZIndex } = useZIndex();

// The conversation whose panels this layer is allowed to show. Panels are
// strictly view-scoped: the run-detail drawer (when open) takes precedence and
// shows only its run's conversation; otherwise a chat route shows its active
// conversation; everywhere else nothing renders. An event run therefore never
// pops a panel over another page — it only appears once its drawer is opened.
const contextCid = computed<string | null>(() => {
  if (eventRuns.activeRunId) return eventRuns.activeRun?.conversation_id ?? null;
  if (route.name === 'chat' || route.name === 'conversation') {
    return chat.activeConversationId;
  }
  return null;
});

const openPanels = computed(() => {
  const cid = contextCid.value;
  if (!cid) return [];
  return panels.panels.filter(p => !p.minimized && p.conversationId === cid);
});
const minimizedPanels = computed(() => {
  const cid = contextCid.value;
  if (!cid) return [];
  return panels.panels.filter(p => p.minimized && p.conversationId === cid);
});

// Focused = top-most among the currently-visible panels (per-view, so switching
// conversations doesn't leave the ring on a hidden panel).
const focusedKey = computed(() => {
  let top: { key: string; zIndex: number } | null = null;
  for (const p of openPanels.value) {
    if (!top || p.zIndex > top.zIndex) top = p;
  }
  return top?.key ?? null;
});

// The run drawer is `append-to-body` at an Element Plus z-index (~2000+). This
// layer is Teleported to <body> too (see template) so it competes with the
// drawer in the ROOT stacking context — otherwise #app's `z-index: 1` stacking
// context (style.css) would trap the panels below the drawer no matter how high
// their own z-index. In drawer context, lift the layer just above the drawer.
const drawerZ = ref<number | null>(null);
watch(
  () => !!eventRuns.activeRunId,
  (inDrawer) => {
    if (!inDrawer) {
      drawerZ.value = null;
      return;
    }
    // Provisional floor immediately (safely above a freshly-opened drawer), then
    // refine to just-above the drawer's actual z once it has mounted and grabbed
    // its own value (a pre-flush read would race the drawer and sit below it).
    drawerZ.value = 2100;
    nextTick(() => {
      if (eventRuns.activeRunId) drawerZ.value = nextZIndex();
    });
  },
  { immediate: true },
);
const layerZ = computed(() =>
  eventRuns.activeRunId ? (drawerZ.value ?? 2100) : 900,
);

const isElectron = typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__;

function onResize() {
  panels.clampAllToViewport();
}

onMounted(() => {
  // Clear the Electron titlebar (32px) when placing/clamping panels.
  panels.configureViewport(isElectron ? 44 : 12);
  window.addEventListener('resize', onResize);
});

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize);
});
</script>

<template>
  <!-- Teleported to <body> so the panels escape #app's z-index:1 stacking
       context and can sit above the append-to-body run drawer. -->
  <Teleport to="body">
    <div class="todo-layer" :style="{ zIndex: layerZ }">
      <TransitionGroup name="todo-window" tag="div" class="todo-window-group">
        <TodoPanelWindow
          v-for="p in openPanels"
          :key="p.key"
          :panel="p"
          :focused="p.key === focusedKey"
        />
      </TransitionGroup>

      <TransitionGroup name="todo-pill" tag="div" class="todo-pill-tray">
        <TodoPanelWindow
          v-for="p in minimizedPanels"
          :key="p.key"
          :panel="p"
          :focused="false"
        />
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.todo-layer {
  position: fixed;
  inset: 0;
  z-index: 900;
  pointer-events: none;
}

.todo-window-group {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.todo-pill-tray {
  position: absolute;
  right: 16px;
  bottom: 12px;
  display: flex;
  flex-wrap: wrap-reverse;
  justify-content: flex-end;
  gap: 8px;
  max-width: calc(100vw - 32px);
  pointer-events: none;
}

/* Entrance: grow from the chip (origin vars) or a soft pop from the cascade. */
.todo-window-enter-active {
  transition: transform 0.28s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.22s ease;
  transform-origin: top center;
}
.todo-window-enter-from {
  opacity: 0;
  transform: translate(var(--from-dx, 0px), var(--from-dy, -10px))
    scale(var(--from-scale, 0.92));
}
.todo-window-leave-active {
  transition: transform 0.24s cubic-bezier(0.4, 0, 1, 1), opacity 0.22s ease-in;
  transform-origin: top center;
}
.todo-window-leave-to {
  opacity: 0;
  transform: translate(var(--to-dx, 0px), var(--to-dy, 6px))
    scale(var(--to-scale, 0.92));
}

.todo-pill-enter-active,
.todo-pill-leave-active {
  transition: transform 0.18s ease, opacity 0.18s ease;
}
.todo-pill-enter-from,
.todo-pill-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
</style>
