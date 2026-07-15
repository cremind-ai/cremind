<script setup lang="ts">
import { ElForm, ElFormItem, ElInput, ElInputNumber, ElSelect, ElOption, ElSwitch, ElTag } from 'element-plus';

const props = withDefaults(defineProps<{
  fields: Record<string, { description: string; type: string; secret: boolean; configured: boolean; required?: boolean; enum?: string[]; default?: unknown; dynamic_options?: boolean }>;
  values: Record<string, string>;
  title?: string;
  // Live option lists for `dynamic_options` fields, keyed by variable name.
  // Fetched by the parent from GET /api/tools/{id}/variable-options.
  dynamicOptions?: Record<string, { options: { id: string; label?: string }[]; error?: string | null }>;
  dynamicLoading?: boolean;
}>(), {
  title: 'Tool Variables',
  dynamicOptions: () => ({}),
  dynamicLoading: false,
});

const emit = defineEmits<{
  'update:values': [values: Record<string, string>];
}>();

function updateValue(key: string, value: string) {
  emit('update:values', { ...props.values, [key]: value });
}

function optionsFor(key: string): { id: string; label?: string }[] {
  return props.dynamicOptions?.[key]?.options ?? [];
}

// Placeholder for a `dynamic_options` combobox. A field with a concrete default
// (e.g. permission mode → "bypassPermissions") shows it; a field whose default
// is empty (e.g. model → Claude Code's own default) shows generic guidance.
function dynamicPlaceholder(field: { default?: unknown }): string {
  const def = field.default;
  return def != null && def !== '' ? `Default: ${def}` : 'Default (pick or type one)';
}
</script>

<template>
  <div class="tool-variables-form">
    <h4 class="config-section-title">{{ props.title }}</h4>
    <ElForm label-position="top" size="small">
      <ElFormItem v-for="(field, key) in fields" :key="key">
        <template #label>
          {{ field.description || key }}
          <ElTag v-if="field.required" type="danger" size="small" class="field-tag">Required</ElTag>
          <ElTag v-else type="info" size="small" effect="plain" class="field-tag">Optional</ElTag>
          <ElTag v-if="field.secret" type="warning" size="small" effect="plain" class="field-tag">Secret</ElTag>
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
        <ElSelect
          v-else-if="field.dynamic_options && (optionsFor(key as string).length > 0 || dynamicLoading)"
          :model-value="values[key] || ''"
          @update:model-value="updateValue(key as string, $event ?? '')"
          filterable
          allow-create
          default-first-option
          clearable
          :loading="dynamicLoading"
          size="small"
          style="width: 100%;"
          :placeholder="dynamicPlaceholder(field)"
        >
          <ElOption
            v-for="opt in optionsFor(key as string)"
            :key="opt.id"
            :label="opt.label || opt.id"
            :value="opt.id"
          />
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
