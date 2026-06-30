<script setup lang="ts">
import { ElForm, ElFormItem, ElInput, ElSelect, ElOption, ElSwitch, ElButton } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { LLMProvider } from '../../services/configApi';
import type { ModelOption } from '../../composables/useLLMModels';

withDefaults(defineProps<{
  description: string;
  systemPrompt: string;
  llmProvider: string;
  llmModel: string;
  reasoningEffort: string;
  fullReasoning: boolean;
  providers: LLMProvider[];
  getFilteredModels: (providerName: string) => ModelOption[];
  getReasoningOptions: (modelValue: string) => string[];
  showFullReasoning?: boolean;
}>(), {
  showFullReasoning: true,
});

const emit = defineEmits<{
  'update:description': [value: string];
  'update:systemPrompt': [value: string];
  'update:llmProvider': [value: string];
  'update:llmModel': [value: string];
  'update:reasoningEffort': [value: string];
  'update:fullReasoning': [value: boolean];
  /** User clicked a per-field reset; parent should call resetToolLLMKeys. */
  'reset-field': [field: 'description' | 'system_prompt' | 'llm_provider' | 'llm_model' | 'reasoning_effort' | 'full_reasoning'];
}>();

function resetProviderModel() {
  emit('update:llmProvider', '');
  emit('update:llmModel', '');
  emit('reset-field', 'llm_provider');
  emit('reset-field', 'llm_model');
}

</script>

<template>
  <div class="llm-parameters-form">
    <ElForm label-position="top" size="small">
      <ElFormItem label="Description">
        <div style="display: flex; align-items: center; gap: 8px; width: 100%;">
          <ElInput
            :model-value="description"
            @update:model-value="emit('update:description', $event)"
            placeholder="Description"
            style="flex: 1;"
          />
          <ElButton v-if="description" text size="small" @click="emit('update:description', ''); emit('reset-field', 'description')">
            <Icon icon="mdi:refresh" style="margin-right: 2px;" /> Reset
          </ElButton>
        </div>
      </ElFormItem>
      <ElFormItem label="System Prompt">
        <div style="display: flex; align-items: flex-start; gap: 8px; width: 100%;">
          <ElInput
            :model-value="systemPrompt"
            @update:model-value="emit('update:systemPrompt', $event)"
            type="textarea"
            :rows="3"
            placeholder="Custom system prompt"
            style="flex: 1;"
          />
          <ElButton v-if="systemPrompt" text size="small" @click="emit('update:systemPrompt', ''); emit('reset-field', 'system_prompt')">
            <Icon icon="mdi:refresh" style="margin-right: 2px;" /> Reset
          </ElButton>
        </div>
      </ElFormItem>
      <ElFormItem label="LLM Provider">
        <div style="display: flex; align-items: center; gap: 8px; width: 100%;">
          <ElSelect
            :model-value="llmProvider"
            @update:model-value="emit('update:llmProvider', $event)"
            placeholder="Default (from model group)"
            clearable
            style="flex: 1;"
          >
            <ElOption v-for="p in providers" :key="p.name" :label="p.display_name" :value="p.name" />
          </ElSelect>
          <ElButton v-if="llmProvider || llmModel" text size="small" @click="resetProviderModel">
            <Icon icon="mdi:refresh" style="margin-right: 2px;" /> Reset
          </ElButton>
        </div>
      </ElFormItem>
      <ElFormItem label="LLM Model">
        <ElSelect
          :model-value="llmModel"
          @update:model-value="emit('update:llmModel', $event)"
          filterable
          clearable
          placeholder="Default (from model group)"
          style="width: 100%"
        >
          <ElOption v-for="m in getFilteredModels(llmProvider)" :key="m.value" :label="m.label" :value="m.value" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem v-if="getReasoningOptions(llmModel).length > 0" label="Reasoning Effort">
        <div style="display: flex; align-items: center; gap: 8px; width: 100%;">
          <ElSelect
            :model-value="reasoningEffort"
            @update:model-value="emit('update:reasoningEffort', $event)"
            placeholder="Default"
            clearable
            style="flex: 1;"
          >
            <ElOption v-for="opt in getReasoningOptions(llmModel)" :key="opt" :label="opt" :value="opt" />
          </ElSelect>
          <ElButton v-if="reasoningEffort" text size="small" @click="emit('update:reasoningEffort', ''); emit('reset-field', 'reasoning_effort')">
            <Icon icon="mdi:refresh" style="margin-right: 2px;" /> Reset
          </ElButton>
        </div>
      </ElFormItem>
      <ElFormItem v-if="showFullReasoning" label="Full Reasoning">
        <div style="display: flex; align-items: center; gap: 8px;">
          <ElSwitch
            :model-value="fullReasoning"
            @update:model-value="emit('update:fullReasoning', $event as boolean)"
          />
          <span style="font-size: 12px; color: var(--text-tertiary);">
            Process tool results through LLM before returning to main agent
          </span>
        </div>
      </ElFormItem>
    </ElForm>
  </div>
</template>
