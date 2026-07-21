<script setup lang="ts">
import { computed } from 'vue';
import { ElInput, ElInputNumber, ElSwitch, ElSelect, ElOption, ElButton, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { UserConfigField } from '../../services/configApi';

const props = defineProps<{
  fieldKey: string;
  field: UserConfigField;
  /** The user-set override (or null when no override exists). */
  value: unknown;
  /** The resolved default (TOML or schema fallback). */
  defaultValue: unknown;
}>();

const emit = defineEmits<{
  (e: 'update:value', value: unknown): void;
  (e: 'reset'): void;
}>();

/** Effective value the UI should display: stored override falls back to default. */
const displayValue = computed(() => (props.value === null || props.value === undefined ? props.defaultValue : props.value));

/** True when the user has an override that differs from the default. */
const isModified = computed(() => {
  if (props.value === null || props.value === undefined) return false;
  return props.value !== props.defaultValue;
});

const fieldLabel = computed(() => props.field.label || formatLabel(props.fieldKey));
const fieldDefault = computed(() => formatValue(props.defaultValue));

/** IANA zones for a `format: 'timezone'` field, with the "auto" sentinel first. */
const timezoneOptions = computed<string[]>(() => {
  let zones: string[] = [];
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf;
    if (typeof supported === 'function') zones = supported('timeZone');
  } catch {
    zones = [];
  }
  return ['auto', ...zones];
});

function formatLabel(key: string): string {
  return key
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'on' : 'off';
  return String(value);
}

function handleNumber(val: number | null | undefined) {
  emit('update:value', val ?? null);
}
function handleString(val: string) {
  emit('update:value', val);
}
function handleBoolean(val: string | number | boolean) {
  emit('update:value', Boolean(val));
}
function handleEnum(val: string) {
  emit('update:value', val);
}
</script>

<template>
  <div class="config-field-row">
    <div class="field-meta">
      <div class="field-header">
        <span class="field-label">{{ fieldLabel }}</span>
        <ElTag v-if="isModified" type="warning" size="small" effect="plain">Modified</ElTag>
      </div>
      <p v-if="field.description" class="field-description">{{ field.description }}</p>
      <p class="field-default">Default: <code>{{ fieldDefault }}</code></p>
    </div>

    <div class="field-control">
      <ElInputNumber
        v-if="field.type === 'number'"
        :model-value="displayValue as number"
        :min="field.min"
        :max="field.max"
        :step="field.step ?? 1"
        :controls="false"
        size="small"
        class="number-input"
        @update:model-value="handleNumber"
      />
      <ElSwitch
        v-else-if="field.type === 'boolean'"
        :model-value="!!displayValue"
        @update:model-value="handleBoolean"
      />
      <ElSelect
        v-else-if="field.type === 'enum'"
        :model-value="displayValue as string"
        size="small"
        class="enum-input"
        @update:model-value="handleEnum"
      >
        <ElOption v-for="opt in field.enum" :key="opt" :label="opt" :value="opt" />
      </ElSelect>
      <ElSelect
        v-else-if="field.format === 'timezone'"
        :model-value="displayValue as string"
        filterable
        size="small"
        class="timezone-input"
        @update:model-value="handleString"
      >
        <ElOption
          v-for="tz in timezoneOptions"
          :key="tz"
          :label="tz === 'auto' ? 'Auto (inherit / server default)' : tz"
          :value="tz"
        />
      </ElSelect>
      <ElInput
        v-else
        :model-value="displayValue as string"
        size="small"
        class="string-input"
        @update:model-value="handleString"
      />

      <ElButton
        v-if="isModified"
        size="small"
        text
        class="reset-btn"
        @click="emit('reset')"
      >
        <Icon icon="mdi:restore" />
        Reset
      </ElButton>
    </div>
  </div>
</template>

<style scoped>
.config-field-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 16px;
  align-items: center;
  padding: 14px 0;
  border-bottom: 1px solid var(--border-color, #eee);
}
.config-field-row:last-child {
  border-bottom: none;
}
.field-meta { min-width: 0; }
.field-header { display: flex; align-items: center; gap: 8px; }
.field-label { font-weight: 600; color: var(--text-primary); font-size: 0.9rem; }
.field-description { margin: 4px 0 2px; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.4; }
.field-default { margin: 0; font-size: 0.75rem; color: var(--text-tertiary); }
.field-default code {
  background: var(--hover-bg, #f5f5f5);
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 0.7rem;
}
.field-control { display: flex; align-items: center; gap: 8px; }
.number-input { width: 140px; }
.enum-input { width: 160px; }
.timezone-input { width: 220px; }
.string-input { width: 220px; }
.reset-btn { color: var(--text-secondary); }
.reset-btn:hover { color: var(--primary-color); }
</style>
