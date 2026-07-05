<script setup lang="ts">
import { computed } from 'vue';
import { ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { EventRunStatus } from '../../services/eventRunsApi';

const props = defineProps<{ status: EventRunStatus }>();

type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info';

const meta = computed<{ type: TagType; label: string; icon: string; dark: boolean }>(() => {
  switch (props.status) {
    case 'running':
      return { type: 'primary', label: 'Running', icon: 'mdi:loading', dark: false };
    case 'pending':
      // The prominent one — the run is waiting for the user's reply.
      return { type: 'warning', label: 'Needs input', icon: 'mdi:account-clock', dark: true };
    case 'completed':
      return { type: 'success', label: 'Completed', icon: 'mdi:check-circle-outline', dark: false };
    case 'failed':
      return { type: 'danger', label: 'Failed', icon: 'mdi:alert-circle-outline', dark: false };
    case 'cancelled':
      return { type: 'info', label: 'Cancelled', icon: 'mdi:cancel', dark: false };
    default:
      return { type: 'info', label: String(props.status), icon: 'mdi:help-circle-outline', dark: false };
  }
});
</script>

<template>
  <ElTag :type="meta.type" :effect="meta.dark ? 'dark' : 'light'" size="small" round>
    <span class="run-status">
      <Icon :icon="meta.icon" :class="{ spin: status === 'running' }" />
      {{ meta.label }}
    </span>
  </ElTag>
</template>

<style scoped>
.run-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.spin {
  animation: run-spin 1s linear infinite;
}
@keyframes run-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
