<script setup lang="ts">
import { Icon } from '@iconify/vue';

withDefaults(defineProps<{
  icon: string;
  label: string;
  value: string;
  sub?: string;
  accent?: 'primary' | 'success' | 'warning' | 'danger';
}>(), { accent: 'primary' });
</script>

<template>
  <div class="stat-tile">
    <div class="stat-icon" :class="`accent-${accent}`">
      <Icon :icon="icon" />
    </div>
    <div class="stat-body">
      <div class="stat-label">{{ label }}</div>
      <div class="stat-value">{{ value }}</div>
      <div v-if="sub" class="stat-sub">{{ sub }}</div>
    </div>
  </div>
</template>

<style scoped>
.stat-tile {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 16px 18px;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  min-width: 0;
}
.stat-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 42px;
  height: 42px;
  border-radius: 10px;
  flex-shrink: 0;
}
.stat-icon :deep(svg) { font-size: 22px; }
.accent-primary { background: color-mix(in srgb, var(--primary-color) 14%, transparent); color: var(--primary-color); }
.accent-success { background: color-mix(in srgb, var(--success-color, #10b981) 14%, transparent); color: var(--success-color, #10b981); }
.accent-warning { background: color-mix(in srgb, var(--warning-color, #f59e0b) 14%, transparent); color: var(--warning-color, #f59e0b); }
.accent-danger  { background: color-mix(in srgb, var(--danger-color, #ef4444) 14%, transparent); color: var(--danger-color, #ef4444); }
.stat-body { min-width: 0; }
.stat-label { font-size: 0.78rem; color: var(--text-secondary); margin-bottom: 2px; }
.stat-value { font-size: 1.45rem; font-weight: 700; color: var(--text-primary); line-height: 1.1; }
.stat-sub { font-size: 0.74rem; color: var(--text-tertiary, var(--text-secondary)); margin-top: 3px; }
</style>
