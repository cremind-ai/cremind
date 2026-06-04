<script setup lang="ts">
import { ElForm, ElFormItem, ElInput, ElInputNumber, ElSelect, ElOption, ElSwitch, ElTag } from 'element-plus';
import type { JsonSchema, JsonSchemaProperty } from '../../services/agentApi';
import type { ProfileValue } from '../../stores/settings';

const props = defineProps<{
  schema: JsonSchema;
  argValues: Record<string, ProfileValue>;
}>();

const emit = defineEmits<{
  'update:argValues': [values: Record<string, ProfileValue>];
}>();

function updateValue(propName: string, value: ProfileValue) {
  emit('update:argValues', { ...props.argValues, [propName]: value });
}

function sortedProperties(): [string, JsonSchemaProperty][] {
  const entries = Object.entries(props.schema.properties);
  const required = new Set(props.schema.required ?? []);
  return entries.sort((a, b) => {
    const aReq = required.has(a[0]) ? 0 : 1;
    const bReq = required.has(b[0]) ? 0 : 1;
    return aReq - bReq;
  });
}

function isRequired(propName: string): boolean {
  return props.schema.required?.includes(propName) ?? false;
}

function requiredFieldCount(): number {
  return props.schema.required?.length ?? 0;
}
</script>

<template>
  <div class="tool-arguments-form">
    <h4 class="config-section-title">
      Tool Arguments
      <ElTag v-if="requiredFieldCount() > 0" size="small" type="danger" effect="plain" style="margin-left: 8px;">
        {{ requiredFieldCount() }} required
      </ElTag>
    </h4>
    <ElForm label-position="top" size="small">
      <ElFormItem v-for="[propName, propDef] in sortedProperties()" :key="propName">
        <template #label>
          {{ propName }}
          <span v-if="isRequired(propName)" class="required-marker">*</span>
          <span v-if="propDef.description" class="arg-field-desc">{{ propDef.description }}</span>
        </template>
        <ElInputNumber
          v-if="propDef.type === 'number'"
          :model-value="(argValues[propName] as number)"
          @update:model-value="updateValue(propName, $event ?? 0)"
          :controls="false"
          size="small"
          style="width: 100%;"
        />
        <ElSwitch
          v-else-if="propDef.type === 'boolean'"
          :model-value="!!argValues[propName]"
          @update:model-value="updateValue(propName, $event)"
        />
        <ElSelect
          v-else-if="propDef.enum && propDef.enum.length > 0"
          :model-value="String(argValues[propName] ?? '')"
          @update:model-value="updateValue(propName, $event)"
          size="small"
          style="width: 100%;"
        >
          <ElOption v-for="opt in propDef.enum" :key="String(opt)" :label="String(opt)" :value="String(opt)" />
        </ElSelect>
        <ElInput
          v-else
          :model-value="String(argValues[propName] ?? '')"
          @update:model-value="updateValue(propName, $event)"
          :placeholder="propDef.description || propName"
          size="small"
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

.required-marker { color: var(--el-color-danger); margin-left: 2px; }

.arg-field-desc {
  display: block;
  font-size: 0.75rem;
  font-weight: 400;
  color: var(--text-secondary);
  line-height: 1.4;
  margin-top: 2px;
}
</style>
