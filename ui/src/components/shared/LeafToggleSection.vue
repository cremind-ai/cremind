<script setup lang="ts">
import { computed } from 'vue';
import { ElSwitch, ElButton } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { ToolLeaf } from '../../services/configApi';

const props = defineProps<{
  leaves: ToolLeaf[];
  loading: boolean;
  /** MCP server is disconnected — sub-tools can't be listed. */
  disconnected: boolean;
  /** Parent tool's enabled state. When false, choices still persist but the
   *  section is dimmed with an explanatory note. */
  parentEnabled: boolean;
}>();

const emit = defineEmits<{
  /** Toggle one sub-tool. */
  toggle: [leafName: string, enabled: boolean];
  /** Enable/disable all sub-tools at once. */
  setAll: [enabled: boolean];
}>();

const enabledCount = computed(() => props.leaves.filter(l => l.enabled).length);
const allEnabled = computed(() => props.leaves.length > 0 && enabledCount.value === props.leaves.length);
const allDisabled = computed(() => enabledCount.value === 0);
</script>

<template>
  <div class="config-section leaf-section">
    <div class="leaf-header">
      <h4 class="config-section-title">Sub-tools</h4>
      <span v-if="!loading && !disconnected && leaves.length" class="leaf-count">
        {{ enabledCount }}/{{ leaves.length }} enabled
      </span>
      <div v-if="!loading && !disconnected && leaves.length" class="leaf-bulk">
        <ElButton size="small" text :disabled="allEnabled" @click="emit('setAll', true)">Enable all</ElButton>
        <ElButton size="small" text :disabled="allDisabled" @click="emit('setAll', false)">Disable all</ElButton>
      </div>
    </div>

    <p v-if="loading" class="leaf-note">Loading sub-tools…</p>
    <p v-else-if="disconnected" class="leaf-note">
      <Icon icon="mdi:alert-circle-outline" /> Reconnect this server to manage its sub-tools.
    </p>
    <template v-else>
      <p v-if="!parentEnabled" class="leaf-note">
        Sub-tool settings apply when the tool is enabled.
      </p>
      <div :class="{ 'leaf-dimmed': !parentEnabled }">
        <div v-for="leaf in leaves" :key="leaf.leaf_name" class="leaf-row">
          <div class="leaf-info">
            <span class="leaf-name">{{ leaf.name }}</span>
            <span v-if="leaf.description && leaf.description !== leaf.name" class="leaf-desc">
              {{ leaf.description }}
            </span>
          </div>
          <ElSwitch
            :model-value="leaf.enabled"
            size="small"
            @update:model-value="emit('toggle', leaf.leaf_name, $event as boolean)"
          />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.leaf-header { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.leaf-count { font-size: 0.78rem; color: var(--text-tertiary); }
.leaf-bulk { margin-left: auto; display: flex; gap: 4px; }
.leaf-note { font-size: 0.8rem; color: var(--text-tertiary); margin: 4px 0; }
.leaf-dimmed { opacity: 0.55; }
.leaf-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 0;
  border-top: 1px solid var(--border-color, #2a2a2a);
}
.leaf-info { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.leaf-name { font-size: 0.85rem; font-weight: 500; }
.leaf-desc { font-size: 0.75rem; color: var(--text-tertiary); }
</style>
