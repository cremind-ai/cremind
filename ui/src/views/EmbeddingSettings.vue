<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue';
import { storeToRefs } from 'pinia';
import { useRouter } from 'vue-router';
import {
  ElForm, ElFormItem, ElInput, ElInputNumber, ElSelect, ElOption,
  ElSwitch, ElButton, ElMessage, ElMessageBox, ElDialog,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { useEmbeddingStatusStore } from '../stores/embeddingStatus';
import {
  getEmbeddingConfig,
  applyEmbeddingConfig,
  fetchServiceCapabilities,
  streamFeaturesInstall,
  EmbeddingFeaturesNotInstalledError,
  type DeploymentMode,
  type EmbeddingConfig,
  type EmbeddingFeaturesNotInstalledDetail,
  type FeatureInstallEvent,
  type ServiceCapability,
  type ServiceCapabilitiesResponse,
} from '../services/configApi';
import { fetchInstallCatalog, type InstallCatalog } from '../services/installCatalogApi';
import { useServerRestart } from '../composables/useServerRestart';
import DeploymentModeRadio from '../components/setup/DeploymentModeRadio.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();
const embeddingStatusStore = useEmbeddingStatusStore();
const serverRestart = useServerRestart();

// Subscribe reactively to the SSE-driven store. App.vue opens the
// stream globally; this page just reads.
const { status, phase, error: errorMsg, busy: isBusy } = storeToRefs(embeddingStatusStore);

const loading = ref(true);
const saving = ref(false);

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
const qdrantCapability = computed(() => serviceCapabilities.value?.services?.qdrant ?? null);
const chromaCapability = computed(() => serviceCapabilities.value?.services?.chroma ?? null);
const dockerAvailable = computed(() => serviceCapabilities.value?.docker_available ?? false);

// Lifted from StepEmbeddingConfig.vue's ``pickInitialMode``: when the
// saved deployment_mode doesn't survive the install-mode rule filter
// (e.g. previous setup saved ``external`` for Qdrant but the install
// mode now restricts to Docker-only), snap to the first effectively
// allowed mode so the form renders the right sub-fields.
function pickInitialMode(
  saved: DeploymentMode | undefined,
  cap: ServiceCapability | null,
  preferred: DeploymentMode,
): DeploymentMode {
  if (!cap) return saved ?? preferred;
  const effective = cap.supported_modes.filter((mode) =>
    mode === 'docker' ? dockerAvailable.value : true,
  );
  if (saved && effective.includes(saved)) return saved;
  if (effective.includes(preferred)) return preferred;
  return effective[0] ?? preferred;
}

const showGemmaToken = computed(() => form.value.enabled && form.value.provider === 'gemma');
const showQdrant = computed(() => form.value.enabled && form.value.vectorstore.provider === 'qdrant');
const showChroma = computed(() => form.value.enabled && form.value.vectorstore.provider === 'chroma');
const qdrantMode = computed(() => form.value.vectorstore.qdrant.deployment_mode);
const chromaMode = computed(() => form.value.vectorstore.chroma.deployment_mode);

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

// When capabilities arrive after the form has already mounted, clamp
// any deployment_mode that the live mode list no longer supports. Same
// shape as StepEmbeddingConfig.vue.
watch([qdrantCapability, dockerAvailable], () => {
  const cap = qdrantCapability.value;
  if (!cap) return;
  const effective = cap.supported_modes.filter((m) =>
    m === 'docker' ? dockerAvailable.value : true,
  );
  if (effective.length && !effective.includes(form.value.vectorstore.qdrant.deployment_mode)) {
    form.value.vectorstore.qdrant.deployment_mode = pickInitialMode(
      form.value.vectorstore.qdrant.deployment_mode, cap, 'external',
    );
  }
});
watch([chromaCapability, dockerAvailable], () => {
  const cap = chromaCapability.value;
  if (!cap) return;
  const effective = cap.supported_modes.filter((m) =>
    m === 'docker' ? dockerAvailable.value : true,
  );
  if (effective.length && !effective.includes(form.value.vectorstore.chroma.deployment_mode)) {
    form.value.vectorstore.chroma.deployment_mode = pickInitialMode(
      form.value.vectorstore.chroma.deployment_mode, cap, 'native',
    );
  }
});

// When the user keeps editing, mirror the active provider's
// deployment_mode up to the top-level vectorstore key so the persisted
// shape stays consistent.
watch(form, (val) => {
  const provider = val.vectorstore.provider;
  val.vectorstore.deployment_mode = val.vectorstore[provider].deployment_mode;
}, { deep: true });

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
// Mirrors the pattern in AgentsToolsSettings.vue: when ``applyChanges``
// gets a 409 FeatureNotInstalled, open this dialog, pipe pip output
// from /api/features/install over SSE, and either prompt for a restart
// (when ``requires_restart=True`` features were installed — the
// embedding providers always are) or retry the apply automatically
// (vectorstore-only installs are hot-reloadable).
const featureInstallOpen = ref(false);
const featureInstallDetail = ref<EmbeddingFeaturesNotInstalledDetail | null>(null);
const featureInstallBusy = ref(false);
const featureInstallLog = ref<string[]>([]);
const featureInstallError = ref<string | null>(null);
const featureInstallRestartRequired = ref(false);

const featureInstallExtras = computed(() => {
  const detail = featureInstallDetail.value;
  if (!detail) return [] as string[];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const entry of detail.missing) {
    for (const grp of entry.extras) {
      if (!seen.has(grp)) {
        seen.add(grp);
        out.push(grp);
      }
    }
  }
  return out;
});

function openFeatureInstallDialog(detail: EmbeddingFeaturesNotInstalledDetail) {
  featureInstallDetail.value = detail;
  featureInstallLog.value = [];
  featureInstallError.value = null;
  featureInstallRestartRequired.value = false;
  featureInstallBusy.value = false;
  featureInstallOpen.value = true;
}

function closeFeatureInstallDialog() {
  featureInstallOpen.value = false;
  featureInstallDetail.value = null;
  featureInstallLog.value = [];
  featureInstallError.value = null;
  featureInstallRestartRequired.value = false;
}

async function confirmFeatureInstall() {
  const detail = featureInstallDetail.value;
  if (!detail || featureInstallBusy.value) return;
  featureInstallBusy.value = true;
  featureInstallError.value = null;
  featureInstallLog.value = [];

  const handleEvent = (evt: FeatureInstallEvent) => {
    const prefix = evt.event === 'error' ? '✖' : evt.event === 'done' ? '✓' : '•';
    if (evt.message) {
      featureInstallLog.value.push(`${prefix} ${evt.message}`);
    }
  };

  try {
    const result = await streamFeaturesInstall(
      settingsStore.agentUrl,
      settingsStore.authToken,
      detail.missing.map((m) => m.feature_key),
      handleEvent,
    );
    if (!result.ok || result.failed.length) {
      featureInstallError.value =
        result.error || `Install failed for: ${result.failed.join(', ')}`;
      featureInstallBusy.value = false;
      return;
    }
    if (result.restart_required) {
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
        featureInstallError.value =
          e instanceof Error ? e.message : 'Failed to save embedding config';
        featureInstallBusy.value = false;
        return;
      }
      featureInstallRestartRequired.value = true;
      featureInstallBusy.value = false;
      return;
    }
    // Hot-loadable install (vectorstore-only). Retry the apply now
    // that the new module is importable.
    featureInstallOpen.value = false;
    featureInstallDetail.value = null;
    await runApply();
  } catch (e) {
    featureInstallError.value = e instanceof Error ? e.message : 'Install stream failed';
    featureInstallBusy.value = false;
  }
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
  // Resolve install_mode early so the install dialog's Restart button
  // knows whether to route through the Electron IPC bridge or
  // POST /api/system/restart.
  serverRestart.loadInstallMode();
});

async function restartFromInstallDialog() {
  await serverRestart.restart();
  // The page is about to be reloaded (Docker/Electron supervisor
  // respawns) or the connection will drop (no supervisor) — either
  // way, closing the dialog avoids leaving a stale "Install complete"
  // banner up if the page survives.
  if (serverRestart.phase.value === 'reconnected') {
    closeFeatureInstallDialog();
  }
}
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
        <div class="status-row">
          <span class="status-label">Current status:</span>
          <span :class="statusBadgeClass">{{ statusBadgeText }}</span>
          <span v-if="phaseLabel" class="status-phase">— {{ phaseLabel }}</span>
        </div>
        <div v-if="errorMsg" class="error-banner">{{ errorMsg }}</div>

        <ElForm label-position="top" class="config-form" :disabled="isBusy || saving">
          <ElFormItem label="Enable Vector Embedding">
            <ElSwitch v-model="form.enabled" :disabled="isBusy || saving" />
            <div class="field-hint">
              {{ form.enabled
                  ? 'Configure the model and vector store below. Applying changes will reload + rebuild caches.'
                  : 'Embedding-dependent features (Automatic Skill Mode, semantic Google Places filtering, doc search) are disabled.' }}
            </div>
          </ElFormItem>

          <template v-if="form.enabled">
            <div class="section-divider"></div>
            <h3 class="section-title">Embedding Model</h3>

            <ElFormItem label="Model">
              <ElSelect v-model="form.provider" style="width: 100%">
                <ElOption value="me5" label="ME5 — Multilingual E5 Base (no auth, 768 dims)" />
                <ElOption value="gemma" label="Gemma 300M — Google (requires HF_TOKEN, 768 dims)" />
              </ElSelect>
            </ElFormItem>

            <ElFormItem v-if="showGemmaToken" label="HuggingFace Token (HF_TOKEN)">
              <ElInput
                v-model="form.hf_token"
                type="password"
                show-password
                placeholder="hf_..."
              />
              <div class="field-hint">
                Required to download the gated <code>google/embeddinggemma-300m</code> model.
              </div>
            </ElFormItem>

            <div class="section-divider"></div>
            <h3 class="section-title">Vector Store</h3>

            <ElFormItem label="Provider">
              <ElSelect v-model="form.vectorstore.provider" style="width: 100%">
                <ElOption value="qdrant" label="Qdrant" />
                <ElOption value="chroma" label="ChromaDB" />
              </ElSelect>
              <div class="field-hint">
                Switching stores triggers a full rebuild — the new store starts empty.
              </div>
            </ElFormItem>

            <template v-if="showQdrant">
              <ElFormItem v-if="qdrantCapability" label="Qdrant Deployment">
                <DeploymentModeRadio
                  v-model="form.vectorstore.qdrant.deployment_mode"
                  :service="qdrantCapability"
                  :docker-available="dockerAvailable"
                  :catalog="installCatalog"
                />
              </ElFormItem>
              <template v-if="qdrantMode === 'docker'">
                <div class="info-box">
                  Cremind will start a <code>qdrant/qdrant</code> container alongside
                  itself and connect on <code>qdrant:6333</code>.
                </div>
              </template>
              <template v-else>
                <ElFormItem label="Qdrant Host">
                  <ElInput v-model="form.vectorstore.qdrant.host" placeholder="localhost" />
                </ElFormItem>
                <ElFormItem label="Qdrant Port">
                  <ElInputNumber v-model="form.vectorstore.qdrant.port" :min="1" :max="65535" />
                </ElFormItem>
                <ElFormItem label="API Key (optional)">
                  <ElInput v-model="form.vectorstore.qdrant.api_key" type="password" show-password />
                </ElFormItem>
                <ElFormItem label="Use HTTPS">
                  <ElSwitch v-model="form.vectorstore.qdrant.https" />
                </ElFormItem>
              </template>
            </template>

            <template v-if="showChroma">
              <ElFormItem v-if="chromaCapability" label="ChromaDB Deployment">
                <DeploymentModeRadio
                  v-model="form.vectorstore.chroma.deployment_mode"
                  :service="chromaCapability"
                  :docker-available="dockerAvailable"
                  :catalog="installCatalog"
                />
              </ElFormItem>

              <template v-if="chromaMode === 'docker'">
                <div class="info-box">
                  Cremind will start a <code>chromadb/chroma</code> container alongside
                  itself and connect on <code>chroma:8000</code>.
                </div>
              </template>
              <template v-else-if="chromaMode === 'native'">
                <ElFormItem label="Persist Path">
                  <ElInput
                    v-model="form.vectorstore.chroma.persist_path"
                    placeholder="Leave blank for <working_dir>/storage/chroma"
                  />
                  <div class="field-hint">
                    Cremind runs the <code>chromadb</code> Python library in-process — no separate service.
                  </div>
                </ElFormItem>
              </template>
              <template v-else>
                <ElFormItem label="Chroma Host">
                  <ElInput v-model="form.vectorstore.chroma.host" placeholder="localhost" />
                </ElFormItem>
                <ElFormItem label="Chroma Port">
                  <ElInputNumber v-model="form.vectorstore.chroma.port" :min="1" :max="65535" />
                </ElFormItem>
                <ElFormItem label="Use SSL">
                  <ElSwitch v-model="form.vectorstore.chroma.ssl" />
                </ElFormItem>
                <ElFormItem label="API Key (optional)">
                  <ElInput v-model="form.vectorstore.chroma.api_key" type="password" show-password />
                </ElFormItem>
              </template>
            </template>
          </template>

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
        </ElForm>
      </template>
    </div>

    <!-- Feature install dialog (opened by ``applyChanges`` when the
         backend returns 409 FeatureNotInstalled). Streams pip output
         from /api/features/install over SSE. Mirrors the pattern used
         on the Agents & Tools page. -->
    <ElDialog
      v-model="featureInstallOpen"
      :title="featureInstallDetail ? 'Install vector embedding dependencies?' : 'Install dependencies'"
      width="560px"
      :close-on-click-modal="!featureInstallBusy"
      :close-on-press-escape="!featureInstallBusy"
      :show-close="!featureInstallBusy"
    >
      <div v-if="featureInstallDetail" class="feature-install-body">
        <p>
          Enabling Vector Embedding requires the following optional
          dependency group<span v-if="featureInstallExtras.length !== 1">s</span>:
          <code>cremind[{{ featureInstallExtras.join(',') }}]</code>.
        </p>
        <ul class="feature-install-list">
          <li v-for="entry in featureInstallDetail.missing" :key="entry.feature_key">
            <code>{{ entry.feature_key }}</code>
            <span v-if="entry.requires_restart_after_install" class="feature-install-restart-tag">
              · restart required after install
            </span>
          </li>
        </ul>

        <div v-if="featureInstallLog.length" class="feature-install-log">
          <div v-for="(line, i) in featureInstallLog" :key="i">{{ line }}</div>
        </div>

        <p v-if="featureInstallError" class="feature-install-error">
          {{ featureInstallError }}
        </p>

        <p v-if="featureInstallRestartRequired" class="feature-install-restart">
          Install complete. Your settings have been saved. Restart the
          Cremind server — the embedding model will load automatically on
          startup and this page will report "Success" when it's ready.
        </p>
        <p
          v-if="featureInstallRestartRequired && serverRestart.error.value"
          class="feature-install-error"
        >
          Restart failed: {{ serverRestart.error.value }}
        </p>
      </div>
      <template #footer>
        <ElButton
          v-if="!featureInstallRestartRequired"
          @click="closeFeatureInstallDialog"
          :disabled="featureInstallBusy"
        >
          Cancel
        </ElButton>
        <ElButton
          v-if="!featureInstallRestartRequired"
          type="primary"
          :loading="featureInstallBusy"
          @click="confirmFeatureInstall"
        >
          {{ featureInstallError ? 'Retry install' : 'Install' }}
        </ElButton>
        <template v-if="featureInstallRestartRequired">
          <ElButton
            @click="closeFeatureInstallDialog"
            :disabled="serverRestart.isBusy.value"
          >
            Close
          </ElButton>
          <!-- ``autofocus`` lands focus on Restart so a Tab-less user
               can hit Enter to proceed; the Close button is still one
               Tab away for anyone who wants to dismiss without
               restarting. The Element Plus button forwards the
               attribute to the underlying <button>. -->
          <ElButton
            type="primary"
            autofocus
            :loading="serverRestart.isBusy.value"
            :disabled="serverRestart.isBusy.value"
            @click="restartFromInstallDialog"
          >
            <Icon icon="mdi:restart" style="margin-right: 4px" />
            Restart
          </ElButton>
        </template>
      </template>
    </ElDialog>
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
  background: #fff4f4; border: 1px solid #f5a3a3; border-radius: 6px;
  color: #b03030; font-size: 0.825rem;
}

.config-form { max-width: 540px; }
.section-divider { border-top: 1px solid var(--border-color); margin: 18px 0 14px; }
.section-title { font-size: 0.95rem; font-weight: 600; color: var(--text-primary); margin: 0 0 10px 0; }
.field-hint { margin-top: 4px; font-size: 0.775rem; color: var(--text-secondary); line-height: 1.4; }
.field-hint code { background: var(--surface-color); padding: 1px 4px; border-radius: 3px; font-size: 0.78rem; }
.info-box {
  margin: 8px 0 16px 0; padding: 12px 16px;
  background: var(--hover-bg); border-radius: 8px;
  font-size: 0.825rem; color: var(--text-secondary); line-height: 1.5;
}
.info-box code { background: var(--surface-color); padding: 1px 4px; border-radius: 3px; font-size: 0.8rem; }
.actions { margin-top: 24px; }

.feature-install-body p { margin: 0 0 12px 0; font-size: 0.875rem; line-height: 1.5; }
.feature-install-body code { background: var(--surface-color); padding: 1px 4px; border-radius: 3px; font-size: 0.8rem; }
.feature-install-list { margin: 0 0 12px 18px; padding: 0; font-size: 0.825rem; color: var(--text-secondary); }
.feature-install-list li { margin-bottom: 2px; }
.feature-install-restart-tag { color: #b07300; }
.feature-install-log {
  max-height: 240px; overflow-y: auto; margin: 12px 0;
  padding: 10px 12px; background: var(--surface-color);
  border-radius: 6px; font-family: var(--font-mono, monospace);
  font-size: 0.75rem; line-height: 1.4;
}
.feature-install-log > div { white-space: pre-wrap; }
.feature-install-error {
  margin: 8px 0 0 0; padding: 8px 12px;
  background: #fff4f4; border: 1px solid #f5a3a3; border-radius: 6px;
  color: #b03030; font-size: 0.825rem;
}
.feature-install-restart {
  margin: 8px 0 0 0; padding: 10px 12px;
  background: #fff8e1; border-radius: 6px;
  color: #b07300; font-size: 0.825rem; line-height: 1.5;
}
</style>
