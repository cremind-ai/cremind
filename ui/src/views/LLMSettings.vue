<script setup lang="ts">
import { useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import LLMConfigForm from '../components/shared/LLMConfigForm.vue';

/**
 * Settings → LLM Providers.
 *
 * A page shell: header, back link and layout only. Everything else — provider
 * credentials, custom providers, browser OAuth and all five model roles — is
 * the shared `<LLMConfigForm>`, which the Setup Wizard's LLM step renders too
 * (there in controlled mode). Mounted `self-saving` here, so the form owns its
 * own PUTs (per-provider Save, model-groups Save, custom-provider CRUD).
 */
const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

function goBack() {
  router.push(`/${props.profile}/settings`);
}
</script>

<template>
  <div class="llm-settings-page">
    <div class="page-container">
      <div class="page-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" /> Back to Settings
        </button>
        <h1 class="page-title">LLM Providers</h1>
      </div>

      <LLMConfigForm
        variant="page"
        self-saving
        allow-browser-oauth
        allow-custom-providers
        show-configured-badge
        :agent-url="settingsStore.agentUrl"
        :token="settingsStore.authToken"
      />
    </div>
  </div>
</template>

<style scoped>
.llm-settings-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}

.page-container { max-width: 720px; margin: 0 auto; }
.page-header { margin-bottom: 24px; }

.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 12px;
}
.back-btn:hover { color: var(--primary-color); }

.page-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0; }
</style>
