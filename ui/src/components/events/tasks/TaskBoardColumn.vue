<script setup lang="ts">
import { Icon } from '@iconify/vue';

defineProps<{
  title: string;
  icon: string;
  count: number;
  /** Extra header text, e.g. the running count. */
  note?: string;
  /** Header accent tone, mapped to a theme color. */
  tone?: 'default' | 'primary' | 'warning' | 'success';
  /** Placeholder shown when the column has no items. */
  empty?: string;
}>();
</script>

<template>
  <section class="board-col">
    <header class="col-head" :class="`tone-${tone || 'default'}`">
      <Icon :icon="icon" class="col-icon" />
      <span class="col-title">{{ title }}</span>
      <span class="col-count">{{ count }}</span>
      <span v-if="note" class="col-note">{{ note }}</span>
      <span class="col-spacer" />
      <slot name="header-actions" />
    </header>
    <div class="col-body">
      <slot />
      <p v-if="count === 0" class="col-empty">{{ empty || 'Nothing here' }}</p>
    </div>
  </section>
</template>

<style scoped>
.board-col {
  flex: 1 1 0;
  min-width: 264px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--border-color);
  border-radius: 10px;
}
.col-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-color);
  border-top-left-radius: 10px;
  border-top-right-radius: 10px;
}
.col-icon {
  font-size: 1.05rem;
  color: var(--text-secondary);
}
.col-title {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--text-primary);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.col-count {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-secondary);
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 999px;
  padding: 0 7px;
  min-width: 20px;
  text-align: center;
}
.col-note {
  font-size: 0.75rem;
  color: var(--text-tertiary);
}
.col-spacer {
  flex: 1;
}
/* Tone tints color the icon + count badge to match run-status semantics. */
.tone-primary .col-icon { color: var(--primary-color); }
.tone-warning .col-icon,
.tone-warning .col-title { color: var(--warning-color, #e6a23c); }
.tone-warning .col-count {
  color: var(--warning-color, #e6a23c);
  border-color: var(--warning-color, #e6a23c);
}
.tone-success .col-icon { color: var(--success-color, #67c23a); }
.col-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.col-empty {
  margin: 8px 4px;
  font-size: 0.8125rem;
  color: var(--text-tertiary);
  text-align: center;
}
</style>
