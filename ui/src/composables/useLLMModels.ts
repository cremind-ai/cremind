import { ref } from 'vue';
import type { LLMModel } from '../services/configApi';

export interface ModelOption {
  label: string;
  value: string;
  provider: string;
  reasoning_effort?: string[];
}

export function useLLMModels() {
  const allModels = ref<ModelOption[]>([]);

  function rebuildModelList(
    providers: { name: string; display_name: string; models: LLMModel[] }[],
  ) {
    const models: ModelOption[] = [];
    for (const p of providers) {
      for (const m of p.models) {
        models.push({
          label: `${p.display_name} / ${m.display_name}`,
          value: `${p.name}/${m.id}`,
          provider: p.name,
          reasoning_effort: m.reasoning_effort,
        });
      }
    }
    allModels.value = models;
  }

  function getFilteredModels(providerName: string): ModelOption[] {
    if (!providerName) return allModels.value;
    return allModels.value.filter((m) => m.provider === providerName);
  }

  function getReasoningOptions(modelValue: string): string[] {
    const model = allModels.value.find((m) => m.value === modelValue);
    return model?.reasoning_effort || [];
  }

  return { allModels, rebuildModelList, getFilteredModels, getReasoningOptions };
}
