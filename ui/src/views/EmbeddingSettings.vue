<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue';
import { storeToRefs } from 'pinia';
import { useRouter } from 'vue-router';
import { ElButton, ElMessage, ElMessageBox } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { useEmbeddingStatusStore } from '../stores/embeddingStatus';
import {
  getEmbeddingConfig,
  applyEmbeddingConfig,
  fetchServiceCapabilities,
  EmbeddingFeaturesNotInstalledError,
  type EmbeddingConfig,
  type EmbeddingFeaturesNotInstalledDetail,
  type ServiceCapabilitiesResponse,
} from '../services/configApi';
import { fetchInstallCatalog, type InstallCatalog } from '../services/installCatalogApi';
import EmbeddingConfigForm from '../components/shared/EmbeddingConfigForm.vue';
import FeatureInstallDialog from '../components/shared/FeatureInstallDialog.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();
const embeddingStatusStore = useEmbeddingStatusStore();

// Subscribe reactively to the SSE-driven store. App.vue opens the
// stream globally; this page just reads.
const { status, phase, error: errorMsg, busy: isBusy } = storeToRefs(embeddingStatusStore);

const loading = ref(true);
const saving = ref(false);

// Seeded with the disabled shape so ``applyChanges`` has something valid to
// read before ``loadConfig`` resolves; the shared EmbeddingConfigForm renders
// only after that (it lives under ``v-else`` on ``loading``) and owns the
// field defaults and deployment-mode clamping from there on.
const form = ref<EmbeddingConfig>({
  enabled: false,
  provider: 'me5',
  hf_token: '',
  vectorstore: {
    provider: 'chroma',
    deployment_mode: 'native',
    qdrant: { deployment_mode: 'external', host: 'localhost', port: 6333, api_key: '', https: false },
    chroma: { deployment_mode: 'native', host: 'localhost', port: 8000, ssl: false, api_key: '', persist_path: '' },
  },
});

const serviceCapabilities = ref<ServiceCapabilitiesResponse | null>(null);
const installCatalog = ref<InstallCatalog | null>(null);

const phaseLabel = computed(() => {
  if (!phase.value) return '';
  return ({
    loading_model: 'Loading embedding model…',
    connecting_store: 'Connecting to vector store…',
    preparing_rebuild: 'Preparing rebuild…',
    rebuilding_places: 'Rebuilding Google Places type embeddings…',
    rebuilding_tools: 'Rebuilding tool & skill embeddings…',
    rebuilding_docs: 'Rebuilding documentation embeddings…',
  } as Record<string, string>)[phase.value] ?? phase.value;
});

const statusBadgeText = computed(() => {
  switch (status.value) {
    case 'ready': return 'Success';
    case 'initializing':
    case 'rebuilding':
      return 'Waiting';
    case 'failed': return 'Failed';
    default: return 'Disabled';
  }
});

const statusBadgeClass = computed(() => `status-badge status-${status.value}`);

async function loadConfig() {
  loading.value = true;
  try {
    // Form values are not part of the SSE state, so we still fetch
    // them once via REST. The runtime status (status / phase / error)
    // comes from the SSE store.
    const res = await getEmbeddingConfig(settingsStore.agentUrl, settingsStore.authToken);
    form.value = res.config;
    try {
      // ``/api/services/capabilities`` is admin-gated post-setup — the
      // token is required, or the call 401s and the deployment radio
      // silently disappears.
      serviceCapabilities.value = await fetchServiceCapabilities(
        settingsStore.agentUrl,
        settingsStore.authToken,
      );
    } catch {
      // Capability fetch is best-effort — the deployment radios just
      // won't render, the form falls back to External-only behaviour.
      serviceCapabilities.value = null;
    }
    try {
      const catalogRes = await fetchInstallCatalog(settingsStore.agentUrl);
      installCatalog.value = catalogRes.catalog;
    } catch {
      // Catalog is cosmetic — DeploymentModeRadio falls back to its
      // built-in labels when this is null.
      installCatalog.value = null;
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to load embedding config');
  } finally {
    loading.value = false;
  }
}

// Detect a busy→ready transition so we can confirm to the user that
// the rebuild they triggered actually finished. We only flash the
// success toast when the watcher saw a busy state immediately before;
// otherwise opening the page on an already-ready system would
// spuriously fire a "completed" notification.
const wasBusy = ref(false);
watch(status, (curr) => {
  if (isBusy.value) {
    wasBusy.value = true;
    return;
  }
  if (wasBusy.value && curr === 'ready') {
    ElMessage.success('Vector embedding update completed successfully.');
  } else if (wasBusy.value && curr === 'failed') {
    ElMessage.error(`Vector embedding update failed: ${errorMsg.value ?? 'unknown error'}`);
  }
  wasBusy.value = false;
});

// ── Feature-install dialog ────────────────────────────────────────────
// When ``applyChanges`` gets a 409 FeatureNotInstalled, the shared dialog
// pipes pip output from /api/features/install over SSE, then either prompts
// for a restart (when ``requires_restart=True`` features were installed — the
// embedding providers always are) or retries the apply automatically
// (vectorstore-only installs are hot-reloadable).
const installDialog = ref<InstanceType<typeof FeatureInstallDialog> | null>(null);

function openFeatureInstallDialog(detail: EmbeddingFeaturesNotInstalledDetail) {
  installDialog.value?.open({
    detail,
    title: 'Install vector embedding dependencies?',
    purpose: 'Enabling Vector Embedding',
    onInstalled: async (restartRequired) => {
      if (restartRequired) {
        // Heavy-init features (sentence-transformers + torch) can't be
        // hot-loaded in the live process. Persist the new config with
        // ``defer_apply`` so the boot path's
        // ``initialize_embedding_subsystem`` picks it up after restart —
        // otherwise the page would still show "Disabled" because the
        // enabled flag never made it to SQLite.
        try {
          await applyEmbeddingConfig(
            settingsStore.agentUrl,
            settingsStore.authToken,
            form.value,
            { deferApply: true },
          );
        } catch (e) {
          throw new Error(e instanceof Error ? e.message : 'Failed to save embedding config');
        }
        return {
          restart: 'Install complete. Your settings have been saved. Restart the Cremind '
            + 'server — the embedding model will load automatically on startup and this '
            + 'page will report "Success" when it\'s ready.',
        };
      }
      // Hot-loadable install (vectorstore-only). Retry the apply now
      // that the new module is importable — after the dialog closed, since
      // the apply reopens it if yet another group is missing.
      return { restart: null, after: runApply };
    },
  });
}

async function runApply() {
  saving.value = true;
  try {
    const res = await applyEmbeddingConfig(settingsStore.agentUrl, settingsStore.authToken, form.value);
    // Surface the immediate POST response, but the source of truth is
    // the SSE stream — the store will reflect subsequent transitions
    // automatically.
    if (res.status === 'failed') {
      ElMessage.error(`Embedding apply failed: ${res.error ?? 'unknown error'}`);
    } else if (res.status === 'disabled') {
      ElMessage.success('Vector Embedding disabled.');
    } else {
      ElMessage.success('Embedding update started — please wait for it to finish.');
    }
  } catch (e) {
    if (e instanceof EmbeddingFeaturesNotInstalledError) {
      // First apply hit the preflight gate. Open the install dialog;
      // the user confirms; we run the SSE install and retry from
      // ``confirmFeatureInstall``.
      openFeatureInstallDialog(e.detail);
      return;
    }
    ElMessage.error(e instanceof Error ? e.message : 'Failed to apply embedding config');
  } finally {
    saving.value = false;
  }
}

async function applyChanges() {
  if (form.value.enabled && form.value.provider === 'gemma' && !form.value.hf_token.trim()) {
    ElMessage.error('HF_TOKEN is required when the embedding provider is Gemma.');
    return;
  }

  try {
    await ElMessageBox.confirm(
      form.value.enabled
        ? 'Applying these changes will reload the embedding model and rebuild every embedding cache. Existing data in the new vector store will be replaced. The agent will be unavailable until the rebuild completes.'
        : 'Disabling Vector Embedding will turn off semantic search across the app. Cached vectors are kept untouched in the existing store, so re-enabling later is fast.',
      'Apply embedding changes?',
      { confirmButtonText: 'Apply', cancelButtonText: 'Cancel' },
    );
  } catch {
    return;
  }

  await runApply();
}

function goBack() {
  router.push(`/${props.profile}/settings`);
}

onMounted(() => {
  // Vector Embedding is admin-only. The router guard blocks
  // navigation in the normal case; this is a defense-in-depth fallback
  // so the page never tries to render or fetch as a non-admin profile.
  if (props.profile !== 'admin') {
    router.replace(`/${props.profile}/settings`);
    return;
  }
  loadConfig();
});
</script>

<template>
  <div class="embedding-settings">
    <div class="settings-container">
      <div class="settings-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Settings
        </button>
        <h1 class="settings-title">Vector Embedding</h1>
        <p class="settings-subtitle">
          Toggle semantic search and configure the embedding model + vector store.
        </p>
      </div>

      <div v-if="loading" class="loading-state">Loading…</div>

      <template v-else>
        <!-- Every field is the shared EmbeddingConfigForm (the Setup Wizard's
             embedding step renders the same one). What's Settings-only lives
             in its slots — the live status row above the fields, the rebuild
             warning under the store picker — plus the Apply flow below. -->
        <EmbeddingConfigForm
          v-model="form"
          :service-capabilities="serviceCapabilities"
          :install-catalog="installCatalog"
          :disabled="isBusy || saving"
        >
          <template #intro>
            <div class="status-row">
              <span class="status-label">Current status:</span>
              <span :class="statusBadgeClass">{{ statusBadgeText }}</span>
              <span v-if="phaseLabel" class="status-phase">— {{ phaseLabel }}</span>
            </div>
            <div v-if="errorMsg" class="error-banner">{{ errorMsg }}</div>
          </template>

          <template #enable-hint="{ enabled }">
            <div class="field-hint">
              {{ enabled
                  ? 'Configure the model and vector store below. Applying changes will reload + rebuild caches.'
                  : 'Semantic ranking is off. Cremind documentation search still works, but its relevance judge reviews the whole shared library plus up to 50 per-profile documents instead of a vector-ranked shortlist; long-term memory search returns the stored facts unranked; Google Places uses a fixed list of common place types. Settings → My Documents is hidden until it is back on; the document indexes profiles already built are kept and still searched by keyword.' }}
            </div>
          </template>

          <template #store-hint>
            <div class="field-hint">
              Switching stores triggers a full rebuild — the new store starts empty.
            </div>
          </template>
        </EmbeddingConfigForm>

        <!-- Outside the shared form on purpose: the form owns the fields, not
             the save. The button used to sit inside the ElForm and inherit its
             ``disabled``; ``:loading`` covers the same ground here, since a
             loading ElButton is already unclickable. -->
        <div class="actions">
          <ElButton
            type="primary"
            :loading="saving || isBusy"
            :disabled="isBusy"
            @click="applyChanges"
          >
            {{ isBusy ? 'Waiting…' : 'Apply Changes' }}
          </ElButton>
        </div>
      </template>
    </div>

    <!-- Feature install dialog (opened by ``applyChanges`` when the
         backend returns 409 FeatureNotInstalled). Streams pip output
         from /api/features/install over SSE. -->
    <FeatureInstallDialog ref="installDialog" />
  </div>
</template>

<style scoped>
.embedding-settings { width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color); padding: 24px; box-sizing: border-box; }
.settings-container { max-width: 720px; margin: 0 auto; }
.settings-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px;
}
.back-btn:hover { color: var(--primary-color); }
.settings-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 4px 0; color: var(--text-primary); }
.settings-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.loading-state { color: var(--text-secondary); padding: 24px 0; }

.status-row {
  display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
  font-size: 0.875rem;
}
.status-label { color: var(--text-secondary); }
.status-badge {
  display: inline-flex; align-items: center; padding: 2px 10px; border-radius: 999px;
  font-size: 0.75rem; font-weight: 600;
}
.status-ready { background: #e8f5e9; color: #2e7d32; }
.status-initializing, .status-rebuilding { background: #fff8e1; color: #b07300; }
.status-failed { background: #ffebee; color: #b03030; }
.status-disabled { background: var(--hover-bg); color: var(--text-secondary); }
.status-phase { color: var(--text-secondary); }

.error-banner {
  margin: 8px 0 16px 0; padding: 10px 14px;
  background: var(--surface-hover); border: 1px solid var(--el-color-danger); border-radius: 6px;
  color: var(--el-color-danger); font-size: 0.825rem;
}

/* The hints we pass into EmbeddingConfigForm's slots are compiled in this
   component's scope, so the shared form's own ``.field-hint`` rule can't
   reach them. */
.field-hint { margin-top: 4px; font-size: 0.775rem; color: var(--text-secondary); line-height: 1.4; }
.actions { margin-top: 24px; }
</style>
