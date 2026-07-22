<script setup lang="ts">
import { computed } from 'vue';
import { Icon } from '@iconify/vue';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import { useSettingsStore } from '../stores/settings';
import { closeTerminal } from '../services/terminalApi';
import type { TerminalAttachment } from '../stores/chat';
import TerminalSession from './TerminalSession.vue';

const panel = useTerminalPanelStore();
const settings = useSettingsStore();

const tabs = computed(() => panel.openTerminals);
const activePid = computed(() => panel.activePid);
const activeTab = computed(() =>
  tabs.value.find(t => t.processId === activePid.value),
);

function handleClose(term: TerminalAttachment) {
  // A user-created terminal owns a real backend shell — closing its tab
  // terminates it. Agent (exec_shell) tabs just detach; stopping is separate.
  if (term.kind === 'terminal') {
    closeTerminal(settings.agentUrl, settings.authToken, term.processId).catch(
      () => {
        /* already gone / offline — the tab still closes below */
      },
    );
  }
  panel.closeTab(term.processId);
}
</script>

<template>
  <div class="terminal-panel">
    <div class="panel-header">
      <div class="tabs" role="tablist">
        <button
          v-for="term in tabs"
          :key="term.processId"
          class="tab"
          :class="{ active: term.processId === activePid }"
          :title="term.command"
          role="tab"
          @click="panel.setActive(term.processId)"
        >
          <Icon icon="mdi:console" class="tab-icon" />
          <span class="tab-label">{{ term.commandShort || term.command }}</span>
          <span
            class="tab-close"
            role="button"
            :aria-label="`Close ${term.commandShort}`"
            @click.stop="handleClose(term)"
          >
            <Icon icon="mdi:close" />
          </span>
        </button>
      </div>
    </div>
    <div class="panel-body">
      <keep-alive>
        <TerminalSession
          v-if="activePid"
          :key="activePid"
          :pid="activePid"
          :kind="activeTab?.kind || 'process'"
          :show-header="false"
        />
      </keep-alive>
    </div>
  </div>
</template>

<style scoped>
.terminal-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: #0b1220;
  color: #e5e7eb;
  overflow: hidden;
}
.panel-header {
  display: flex;
  align-items: stretch;
  background: #0f172a;
  border-bottom: 1px solid #1f2937;
  flex-shrink: 0;
}
.tabs {
  display: flex;
  overflow-x: auto;
  flex: 1 1 auto;
  min-width: 0;
}
.tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  background: transparent;
  border: none;
  border-right: 1px solid #1f2937;
  color: #94a3b8;
  font-family: Consolas, Monaco, monospace;
  font-size: 0.8rem;
  cursor: pointer;
  white-space: nowrap;
  max-width: 240px;
}
.tab.active {
  background: #1e1e1e;
  color: #e5e7eb;
}
.tab:hover:not(.active) {
  background: #1e293b;
  color: #cbd5f5;
}
.tab-icon {
  flex-shrink: 0;
  font-size: 1rem;
}
.tab-label {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 180px;
}
.tab-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 3px;
  color: #64748b;
  flex-shrink: 0;
}
.tab-close:hover {
  background: #334155;
  color: #e5e7eb;
}
.panel-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
</style>
