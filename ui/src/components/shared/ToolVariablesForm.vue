<script setup lang="ts">
import { ElForm, ElFormItem, ElInput, ElInputNumber, ElSelect, ElOption, ElSwitch, ElTag } from 'element-plus';

const props = withDefaults(defineProps<{
  fields: Record<string, { description: string; type: string; secret: boolean; configured: boolean; enum?: string[]; default?: unknown }>;
  values: Record<string, string>;
  title?: string;
}>(), {
  title: 'Tool Variables',
});

const emit = defineEmits<{
  'update:values': [values: Record<string, string>];
}>();

function updateValue(key: string, value: string) {
  emit('update:values', { ...props.values, [key]: value });
}
</script>

<template>
  <div class="tool-variables-form">
    <h4 class="config-section-title">{{ props.title }}</h4>
    <ElForm label-position="top" size="small">
      <ElFormItem v-for="(field, key) in fields" :key="key">
        <template #label>
          {{ field.description || key }}
          <ElTag v-if="field.configured" type="success" size="small" class="field-tag">Set</ElTag>
        </template>
        <ElSwitch
          v-if="field.type === 'boolean'"
          :model-value="(values[key] || '').toLowerCase() === 'true'"
          @update:model-value="updateValue(key as string, String($event))"
        />
        <ElSelect
          v-else-if="field.enum && field.enum.length > 0"
          :model-value="values[key] || ''"
          @update:model-value="updateValue(key as string, $event)"
          size="small"
          style="width: 100%;"
        >
          <ElOption v-for="opt in field.enum" :key="opt" :label="opt" :value="opt" />
        </ElSelect>
        <ElInputNumber
          v-else-if="field.type === 'number'"
          :model-value="values[key] ? Number(values[key]) : undefined"
          @update:model-value="updateValue(key as string, String($event ?? ''))"
          :controls="false"
          size="small"
          style="width: 100%;"
          :placeholder="field.default != null ? `Default: ${field.default}` : undefined"
        />
        <ElInput
          v-else
          :model-value="values[key] || ''"
          @update:model-value="updateValue(key as string, $event)"
          :type="field.secret ? 'password' : 'text'"
          :show-password="field.secret"
          :placeholder="field.configured ? '(already set)' : `Enter ${field.description || key}`"
        />
      </ElFormItem>
    </ElForm>
  </div>
</template>

<style scoped>
.config-section-title {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.03em;
  margin: 0 0 8px 0;
}

.field-tag { margin-left: 8px; }
</style>
