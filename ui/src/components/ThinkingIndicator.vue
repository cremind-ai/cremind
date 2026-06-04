<script setup lang="ts">
import { Icon } from '@iconify/vue';

withDefaults(defineProps<{ label?: string }>(), {
  label: 'Agent is thinking...',
});
</script>

<template>
  <div class="thinking-indicator">
    <Icon icon="mdi:loading" class="spinner-icon" />
    <span class="text">{{ label }}</span>
  </div>
</template>

<style scoped>
.thinking-indicator {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  margin: 8px 0;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  color: var(--text-secondary);
  font-size: 0.875rem;
  max-width: fit-content;
}

.spinner-icon {
  font-size: 16px;
  color: var(--primary-color);
  animation: spin 1s linear infinite;
}

.text {
  font-size: 0.875em;
  animation: pulse-opacity 2s ease-in-out infinite;
}

@keyframes pulse-opacity {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.6;
  }
}
</style>
