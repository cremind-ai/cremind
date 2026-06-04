<script setup lang="ts">
import { ElSwitch, ElTag, ElButton, ElPopconfirm } from 'element-plus';
import { Icon } from '@iconify/vue';

defineProps<{
  name: string;
  statusTag: { label: string; type: 'success' | 'warning' | 'info' | 'danger' };
  expanded: boolean;
  enabled: boolean;
  showAuthenticate?: boolean;
  showUnlink?: boolean;
  showReconnect?: boolean;
  showRemove?: boolean;
  removeName?: string;
}>();

const emit = defineEmits<{
  'toggle-expand': [];
  'update:enabled': [value: boolean];
  authenticate: [];
  unlink: [];
  reconnect: [];
  remove: [];
}>();
</script>

<template>
  <div class="item-header" @click="emit('toggle-expand')">
    <div class="item-info">
      <span class="item-name">{{ name }}</span>
      <ElTag :type="statusTag.type" size="small">{{ statusTag.label }}</ElTag>
    </div>
    <div class="item-actions" @click.stop>
      <ElButton v-if="showReconnect" size="small" type="warning" @click="emit('reconnect')">Reconnect</ElButton>
      <ElButton v-if="showAuthenticate" size="small" type="primary" @click="emit('authenticate')">Auth</ElButton>
      <ElButton v-if="showUnlink" size="small" @click="emit('unlink')">Unlink</ElButton>
      <ElPopconfirm v-if="showRemove" :title="`Remove '${removeName || name}'?`" @confirm="emit('remove')">
        <template #reference>
          <ElButton size="small" type="danger"><Icon icon="mdi:delete" /></ElButton>
        </template>
      </ElPopconfirm>
      <ElSwitch :model-value="enabled" @update:model-value="emit('update:enabled', $event as boolean)" size="small" />
      <Icon :icon="expanded ? 'mdi:chevron-up' : 'mdi:chevron-down'" class="expand-icon" />
    </div>
  </div>
</template>

<style scoped>
.item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
}

.item-info { display: flex; align-items: center; gap: 8px; }
.item-name { font-weight: 600; font-size: 0.9rem; }
.item-actions { display: flex; align-items: center; gap: 6px; }
.expand-icon { font-size: 18px; color: var(--text-tertiary); cursor: pointer; }
</style>
