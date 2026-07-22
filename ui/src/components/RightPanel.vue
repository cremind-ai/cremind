<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { Icon } from '@iconify/vue';
import { ElMessage, ElTooltip } from 'element-plus';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import FileTreePanel from './FileTreePanel.vue';
import TerminalPanel from './TerminalPanel.vue';
import HorizontalResizableDivider from './HorizontalResizableDivider.vue';

const panel = useTerminalPanelStore();
const splitContainer = ref<HTMLElement | null>(null);
const creatingTerminal = ref(false);

const showTerminal = computed(() => panel.openTerminals.length > 0);
const topFlex = computed(() =>
  showTerminal.value ? `0 0 ${panel.splitRatio * 100}%` : '1 1 auto',
);

async function onNewTerminal() {
  if (creatingTerminal.value) return;
  creatingTerminal.value = true;
  try {
    await panel.newTerminal();
  } catch (err) {
    ElMessage.error(
      err instanceof Error ? err.message : 'Failed to open a new terminal.',
    );
  } finally {
    creatingTerminal.value = false;
  }
}

// Re-attach any user terminals that survived a page reload.
onMounted(() => {
  panel.restoreTerminals();
});
</script>

<template>
  <div class="right-panel" :class="{ collapsed: panel.collapsed }">
    <template v-if="panel.collapsed">
      <ElTooltip content="Expand panel" placement="left" :show-after="300">
        <button class="collapsed-strip" @click="panel.setCollapsed(false)">
          <Icon icon="mdi:chevron-double-left" class="collapsed-chevron" />
          <Icon icon="mdi:dock-right" class="collapsed-icon" />
        </button>
      </ElTooltip>
    </template>
    <template v-else>
      <div class="right-panel-header">
        <Icon icon="mdi:dock-right" class="header-icon" />
        <span class="header-title">Workspace</span>
        <ElTooltip content="New terminal" placement="bottom" :show-after="300">
          <button
            class="header-action"
            :disabled="creatingTerminal"
            @click="onNewTerminal"
          >
            <Icon icon="mdi:console-line" />
          </button>
        </ElTooltip>
        <ElTooltip content="Collapse panel" placement="bottom" :show-after="300">
          <button class="header-action" @click="panel.setCollapsed(true)">
            <Icon icon="mdi:chevron-double-right" />
          </button>
        </ElTooltip>
        <ElTooltip content="Hide panel" placement="bottom" :show-after="300">
          <button class="header-action" @click="panel.minimize()">
            <Icon icon="mdi:window-minimize" />
          </button>
        </ElTooltip>
      </div>
      <div ref="splitContainer" class="right-panel-body">
        <div class="tree-section" :style="{ flex: topFlex }">
          <FileTreePanel />
        </div>
        <template v-if="showTerminal">
          <HorizontalResizableDivider
            :containerEl="splitContainer"
            @update:ratio="panel.setSplitRatio"
          />
          <div class="terminal-section">
            <TerminalPanel />
          </div>
        </template>
      </div>
    </template>
  </div>
</template>

<style scoped>
.right-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: #0b1220;
  color: #e5e7eb;
  overflow: hidden;
}
.right-panel-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  background: #0f172a;
  border-bottom: 1px solid #1f2937;
  flex-shrink: 0;
  font-size: 0.78rem;
  color: #94a3b8;
}
.header-icon {
  font-size: 1rem;
  flex-shrink: 0;
}
.header-title {
  flex: 1 1 auto;
  font-weight: 500;
  color: #cbd5f5;
}
.header-action {
  background: transparent;
  border: none;
  color: #94a3b8;
  padding: 2px 6px;
  cursor: pointer;
  border-radius: 3px;
  font-size: 1rem;
  display: inline-flex;
  align-items: center;
}
.header-action:hover {
  color: #e5e7eb;
  background: #1e293b;
}
.header-action:disabled {
  opacity: 0.5;
  cursor: default;
}
.header-action:disabled:hover {
  color: #94a3b8;
  background: transparent;
}
.right-panel-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.tree-section {
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.terminal-section {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.collapsed-strip {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  background: #0f172a;
  border: none;
  border-left: 1px solid #1f2937;
  color: #94a3b8;
  cursor: pointer;
  font-size: 1.05rem;
}
.collapsed-strip:hover {
  color: #e5e7eb;
  background: #1e293b;
}
.collapsed-chevron {
  font-size: 1rem;
}
.collapsed-icon {
  font-size: 1.1rem;
}
</style>
