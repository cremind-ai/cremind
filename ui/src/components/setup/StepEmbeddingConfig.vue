<script setup lang="ts">
import EmbeddingConfigForm from '../shared/EmbeddingConfigForm.vue';
import type { EmbeddingSetupConfig, ServiceCapabilitiesResponse } from '../../services/configApi';
import type { InstallCatalog } from '../../services/installCatalogApi';

// The wizard's Vector Embedding step. Every field lives in the shared
// ``EmbeddingConfigForm`` (Settings → Vector Embedding renders the same one);
// what stays here is the setup-only framing: the step heading, the "what you
// get when enabled" pitch, and the skip-flavoured hint on the enable switch.

/** Still exported here because SetupWizard.vue types its ``embeddingConfig``
 *  ref by importing this name from this step. It resolves to the same
 *  transport shape the shared form exports under the same name — the one
 *  source of truth is ``EmbeddingSetupConfig`` in configApi. */
export type EmbeddingConfigPayload = EmbeddingSetupConfig;

defineProps<{
  config: Partial<EmbeddingConfigPayload>;
  // Per-service deployment-mode descriptor from /api/services/capabilities.
  // Null while still loading — the form mounts with the External fields
  // visible until it arrives.
  serviceCapabilities?: ServiceCapabilitiesResponse | null;
  // Install catalog (deployment / mode labels). Forwarded to
  // DeploymentModeRadio so service-mode labels stay in sync with the
  // install scripts.
  installCatalog?: InstallCatalog | null;
}>();

// The wizard owns the payload: it holds it in ``embeddingConfig`` and posts it
// with the rest of the setup body, so this step stays a pass-through and keeps
// its original ``update`` contract rather than adopting v-model.
const emit = defineEmits<{
  update: [config: EmbeddingConfigPayload];
}>();

function onUpdate(next: EmbeddingConfigPayload) {
  emit('update', next);
}
</script>

<template>
  <div class="step-embedding-config">
    <h3 class="step-title">Vector Embedding (Optional)</h3>
    <p class="step-description">
      Vector embedding lets Cremind understand your queries semantically. You can enable it now or skip and turn it on later.
    </p>

    <EmbeddingConfigForm
      :model-value="config"
      :service-capabilities="serviceCapabilities ?? null"
      :install-catalog="installCatalog ?? null"
      @update:model-value="onUpdate"
    >
      <template #intro>
        <div class="benefits-box">
          <strong>What you get when enabled:</strong>
          <ul>
            <li><strong>Automatic Skill Mode</strong> — Cremind picks the most relevant skills for each request instead of showing the LLM all of them.</li>
            <li><strong>Google Places</strong> filters 336 place types down to the most relevant for your query, reducing tokens.</li>
            <li><strong>Document &amp; tool search</strong> uses semantic similarity for more accurate results.</li>
          </ul>
          <div class="benefits-note">
            First start downloads a model (~500&nbsp;MB for ME5, ~1.2&nbsp;GB for Gemma).
          </div>
        </div>
      </template>

      <template #enable-hint="{ enabled }">
        <div class="field-hint">
          {{ enabled
            ? 'Configure the embedding model and vector store below.'
            : 'Skip this step. Automatic Skill Mode will be unavailable; Google Places will use a small static type list.' }}
        </div>
      </template>
    </EmbeddingConfigForm>
  </div>
</template>

<style scoped>
.step-embedding-config {
  padding: 8px 0;
}

.step-title {
  font-size: 1.1rem;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0 0 8px 0;
}

.step-description {
  color: var(--text-secondary);
  font-size: 0.875rem;
  margin: 0 0 16px 0;
  line-height: 1.5;
}

.benefits-box {
  margin: 0 0 24px 0;
  padding: 14px 16px;
  background: var(--hover-bg);
  border-radius: 8px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  line-height: 1.55;
}

.benefits-box ul {
  margin: 8px 0 8px 18px;
  padding: 0;
}

.benefits-box li {
  margin-bottom: 4px;
}

.benefits-box code {
  background: var(--surface-color);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 0.8rem;
}

.benefits-note {
  margin-top: 8px;
  font-size: 0.78rem;
  color: var(--text-secondary);
  opacity: 0.85;
}

/* Slot content is compiled in this component's scope, so the shared form's
   own ``.field-hint`` rule can't reach the hint we hand it. */
.field-hint {
  margin-top: 4px;
  font-size: 0.775rem;
  color: var(--text-secondary);
  line-height: 1.4;
}
</style>
