<script setup lang="ts">
import LLMConfigForm, { type LLMConfigValidity } from '../shared/LLMConfigForm.vue';

/**
 * Setup Wizard step: LLM providers + models.
 *
 * Nothing but the step's heading lives here — the form itself is the shared
 * `<LLMConfigForm>` that Settings → LLM Providers also renders, so the wizard
 * can never fall behind on model roles, auth flows or copy. It is mounted in
 * *controlled* mode: no request is saved from this step, the whole
 * configuration comes back as one flat record that `SetupWizard` bundles into
 * `POST /api/config/setup`.
 */
const props = defineProps<{
  agentUrl: string;
  // Admin JWT for loading the provider/model catalog during a per-profile
  // setup (empty on first-run setup, where the endpoints are open).
  token?: string;
  config: Record<string, string>;
}>();

const emit = defineEmits<{
  update: [config: Record<string, string>];
  // Forwarded verbatim: the wizard gates its Next button on this (a profile
  // created without a main model can never answer anything), and only the
  // form has the provider catalog needed to judge it.
  validity: [validity: LLMConfigValidity];
}>();
</script>

<template>
  <div class="step-llm-config">
    <h3 class="step-title">LLM Provider Configuration</h3>
    <p class="step-description">
      Configure at least one LLM provider with an API key, then choose the
      <strong>model</strong> the assistant runs on.
    </p>

    <LLMConfigForm
      variant="step"
      :agent-url="props.agentUrl"
      :token="props.token ?? ''"
      :initial-config="props.config"
      @update:config="emit('update', $event)"
      @update:validity="emit('validity', $event)"
    />
  </div>
</template>

<style scoped>
.step-llm-config { padding: 8px 0; }

.step-title {
  font-size: 1.1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 8px 0;
}

.step-description {
  color: var(--text-secondary); font-size: 0.875rem; margin: 0 0 20px 0; line-height: 1.5;
}
</style>
