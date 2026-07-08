<script setup lang="ts">
import { ElCard } from 'element-plus';
import ItemCardHeader from './ItemCardHeader.vue';

/**
 * Shared card shell for a tool/skill row.
 *
 * Wraps the reusable `ItemCardHeader` (enable toggle + status tag + optional
 * action buttons) in an `ElCard` and exposes an expandable config body via the
 * default slot. Every `ItemCardHeader` prop/event passes straight through via
 * `$attrs`, so callers wire the header exactly as before; only `expanded` is a
 * declared prop, because the shell uses it to gate the body.
 *
 * Reused by the Setup Wizard's tool step (`StepToolConfig.vue`) and the
 * Settings "Tools & Skills" page (`AgentsToolsSettings.vue`). Each keeps its
 * own — deliberately different — config body in the default slot (the wizard
 * shows setup-time LLM/argument forms; Settings shows Save/leaf-toggle/
 * long-running-process controls).
 */
defineProps<{ expanded: boolean }>();
defineOptions({ inheritAttrs: false });
</script>

<template>
  <ElCard class="item-card" shadow="hover">
    <!-- All ItemCardHeader props/events flow through $attrs; cast to any so the
         checker doesn't demand them re-declared on this passthrough shell. -->
    <ItemCardHeader v-bind="($attrs as any)" :expanded="expanded" />
    <!-- Always-visible banner between header and body (e.g. the wizard's
         pip-extras "Installs: cremind[…]" hint). -->
    <slot name="banner" />
    <div v-if="expanded" class="item-config">
      <slot />
    </div>
  </ElCard>
</template>

<style scoped>
.item-card { background: var(--surface-color); }
.item-config {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-color, #e4e7ed);
}
</style>
