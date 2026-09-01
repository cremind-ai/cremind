<script setup lang="ts">
import { computed, watch } from 'vue';
import { ElForm, ElFormItem, ElSelect, ElOption } from 'element-plus';
import type { ModelOption } from '../../composables/useLLMModels';

/**
 * Provider / Model / (optional) Reasoning-Effort picker for a single LLM model
 * group (``high`` / ``low`` / ``vision`` / ``audio``). Shared by the Settings →
 * LLM Providers page and the Setup Wizard's LLM step so both stay consistent.
 *
 * ``allModels`` is passed in rather than pulled from ``useLLMModels`` because
 * that composable holds per-call state — a child instance would see an empty
 * list. The parent builds the list once (``rebuildModelList``) and hands it down.
 */
const props = withDefaults(defineProps<{
  /** Provider options for the Provider dropdown. */
  providers: { name: string; display_name: string }[];
  /** Flattened model list built by the parent's ``rebuildModelList``. This is
   *  what the picker OFFERS, so it is scoped to each provider's active auth
   *  method. */
  allModels: ModelOption[];
  /** Superset of ``allModels``: every model the parent has ever fetched, used
   *  only to NAME a bound model. The two diverge while an auth method is being
   *  previewed — the offered list re-scopes to that method, but a model saved
   *  under the other one must keep its label and its reasoning options. */
  knownModels?: ModelOption[];
  /** Selected provider name (v-model:provider). */
  provider: string;
  /** Selected model group value "provider/model" (v-model:model). */
  model: string;
  /** Selected reasoning effort (v-model:reasoningEffort); null when unset. */
  reasoningEffort?: string | null;
  /** Restrict the model list to vision-capable models. */
  useVision?: boolean;
  /** Restrict the model list to audio-capable models. */
  useAudio?: boolean;
  /** Allow clearing provider/model (optional groups: low, vision, audio). */
  clearable?: boolean;
  /** Render the Reasoning Effort row when the selected model supports it. */
  showReasoning?: boolean;
  modelPlaceholder?: string;
}>(), {
  knownModels: () => [],
  reasoningEffort: null,
  useVision: false,
  useAudio: false,
  clearable: false,
  showReasoning: true,
  modelPlaceholder: 'Select model',
});

const emit = defineEmits<{
  'update:provider': [value: string];
  'update:model': [value: string];
  'update:reasoningEffort': [value: string | null];
}>();

/** Extract provider name from a model group value like "groq/mixtral-8x7b". */
function extractProvider(groupValue: string): string {
  if (!groupValue) return '';
  const idx = groupValue.indexOf('/');
  return idx > 0 ? groupValue.substring(0, idx) : groupValue;
}

const filteredModels = computed<ModelOption[]>(() => {
  if (props.useVision) {
    return props.allModels.filter(
      (m) => m.vision && (!props.provider || m.provider === props.provider),
    );
  }
  if (props.useAudio) {
    return props.allModels.filter(
      (m) => m.audio && (!props.provider || m.provider === props.provider),
    );
  }
  if (!props.provider) return props.allModels;
  return props.allModels.filter((m) => m.provider === props.provider);
});

/** Describe a bound model: the offered list first, then everything the parent
 *  has ever fetched (see the ``knownModels`` prop). */
function describeModel(value: string): ModelOption | undefined {
  if (!value) return undefined;
  return props.allModels.find((m) => m.value === value)
    || props.knownModels.find((m) => m.value === value);
}

const reasoningOptions = computed<string[]>(() => {
  if (!props.model) return [];
  const found = describeModel(props.model);
  if (found) return found.reasoning_effort || [];
  // Nothing describes the bound model any more (a catalog dropped it). Keep the
  // row alive on the effort that is bound so it stays visible and clearable —
  // hiding it would make a saved effort look gone while it still gets saved.
  return props.reasoningEffort ? [props.reasoningEffort] : [];
});

/** The bound model when the offered list doesn't contain it — surfaced as its
 *  own option so the picker shows a name instead of the bare "provider/id". */
const boundModelOption = computed<ModelOption | null>(() => {
  if (!props.model || filteredModels.value.some((m) => m.value === props.model)) return null;
  return describeModel(props.model)
    || { label: props.model, value: props.model, provider: extractProvider(props.model) };
});

const providerProxy = computed({
  get: () => props.provider,
  set: (v: string) => emit('update:provider', v || ''),
});
const modelProxy = computed({
  get: () => props.model,
  set: (v: string) => emit('update:model', v || ''),
});
const effortProxy = computed<string | null>({
  get: () => props.reasoningEffort ?? null,
  set: (v: string | null) => emit('update:reasoningEffort', v || null),
});

// When the provider changes, drop a model (and its effort) that no longer
// belongs to it — mirrors the original per-section watchers in LLMSettings.
watch(() => props.provider, (newProvider, oldProvider) => {
  if (oldProvider && newProvider !== oldProvider) {
    if (extractProvider(props.model) !== newProvider) {
      emit('update:model', '');
      emit('update:reasoningEffort', null);
    }
  }
});

// Clear the effort when the newly-selected model doesn't support one.
watch(() => props.model, () => {
  if (reasoningOptions.value.length === 0 && props.reasoningEffort) {
    emit('update:reasoningEffort', null);
  }
});
</script>

<template>
  <ElForm label-position="top" class="model-group-fields">
    <ElFormItem label="Provider">
      <ElSelect v-model="providerProxy" placeholder="Select provider" :clearable="clearable">
        <ElOption v-for="p in providers" :key="p.name" :label="p.display_name" :value="p.name" />
      </ElSelect>
    </ElFormItem>

    <ElFormItem label="Model">
      <ElSelect v-model="modelProxy" filterable :clearable="clearable" :placeholder="modelPlaceholder">
        <ElOption
          v-if="boundModelOption"
          :key="boundModelOption.value"
          :label="boundModelOption.label"
          :value="boundModelOption.value"
        />
        <ElOption v-for="m in filteredModels" :key="m.value" :label="m.label" :value="m.value" />
      </ElSelect>
    </ElFormItem>

    <ElFormItem v-if="showReasoning && reasoningOptions.length > 0" label="Reasoning Effort">
      <ElSelect v-model="effortProxy" placeholder="Select reasoning effort" clearable>
        <ElOption v-for="opt in reasoningOptions" :key="opt" :label="opt" :value="opt" />
      </ElSelect>
    </ElFormItem>
  </ElForm>
</template>
