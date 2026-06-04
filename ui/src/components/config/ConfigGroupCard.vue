<script setup lang="ts">
import { ElCard } from 'element-plus';
import type { UserConfigGroup } from '../../services/configApi';
import ConfigFieldRow from './ConfigFieldRow.vue';

const props = defineProps<{
  groupKey: string;
  group: UserConfigGroup;
  /** Map of full keys ("group.field") to user-set values (or null). */
  values: Record<string, unknown>;
  defaults: Record<string, unknown>;
}>();

const emit = defineEmits<{
  (e: 'update:value', key: string, value: unknown): void;
  (e: 'reset', key: string): void;
}>();

function fullKey(fieldName: string): string {
  return `${props.groupKey}.${fieldName}`;
}
</script>

<template>
  <ElCard class="config-group-card" shadow="never">
    <template #header>
      <div class="group-header">
        <h3 class="group-title">{{ group.label }}</h3>
        <p v-if="group.description" class="group-description">{{ group.description }}</p>
      </div>
    </template>

    <ConfigFieldRow
      v-for="(field, fieldName) in group.fields"
      :key="fieldName"
      :field-key="fieldName"
      :field="field"
      :value="values[fullKey(fieldName)] ?? null"
      :default-value="defaults[fullKey(fieldName)]"
      @update:value="(v) => emit('update:value', fullKey(fieldName), v)"
      @reset="emit('reset', fullKey(fieldName))"
    />
  </ElCard>
</template>

<style scoped>
.config-group-card {
  background: var(--surface-color);
  margin-bottom: 16px;
}
.group-header { padding: 0; }
.group-title { font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 4px 0; }
.group-description { font-size: 0.85rem; color: var(--text-secondary); margin: 0; line-height: 1.4; }
</style>
