<script setup lang="ts">
/**
 * The admin gate for Documentation search, on the Vector Embedding page.
 *
 * The feature rides the server-wide embedding model and vector store, so the
 * admin allows it here; each profile then turns it on for itself under
 * Settings → My Documents. The card saves on its own (`PUT /api/documentation-search/admin`)
 * — never through the embedding form's Apply, which always rebuilds every
 * embedding cache and refuses chat while it runs.
 *
 * It renders even while Vector Embedding is off, read-only, so the admin can
 * see that profiles' indexes are kept (searchable by keyword only) rather than
 * wonder where the setting went.
 *
 * Allowing it when the document readers (PDF, Office, images) are not
 * installed answers `409 FeatureNotInstalled`; that is handed to the page's
 * install dialog (`feature-missing`), which calls `save()` again once the
 * install is done.
 */
import { computed, onMounted, ref, watch } from 'vue';
import {
  ElButton, ElInputNumber, ElMessage, ElSwitch, ElTable, ElTableColumn,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../../stores/settings';
import { useEmbeddingStatusStore } from '../../stores/embeddingStatus';
import {
  getDocumentsAdmin,
  putDocumentsAdmin,
  DocumentsApiError,
  type DocumentsAdminPolicy,
  type DocumentsAdminView,
  type DocumentsFeatureMissing,
} from '../../services/documentsApi';

const emit = defineEmits<{
  'feature-missing': [detail: { missing: DocumentsFeatureMissing[]; message: string }];
}>();

const settingsStore = useSettingsStore();
const embeddingStatus = useEmbeddingStatusStore();

const view = ref<DocumentsAdminView | null>(null);
const loadError = ref('');
const saving = ref(false);
const fieldErrors = ref<Record<string, string>>({});

/** The form, in the units people think in: budgets in GB, the rest as stored. */
interface GateForm {
  allowed: boolean;
  storage_budget_gb: number;
  per_profile_budget_gb: number;
  vision_daily_cap_default: number;
  max_file_mb: number;
  workers: number;
  vector_capacity_mb: number;
  db_capacity_mb: number;
}

const GB = 1024;
const form = ref<GateForm | null>(null);

type NumberKey = Exclude<keyof GateForm, 'allowed'>;

/** The numeric fields, with the server's bounds (app/documents/settings.py). */
const NUMBER_FIELDS: {
  key: NumberKey;
  policyKey: keyof DocumentsAdminPolicy;
  label: string;
  min: number;
  max?: number;
  step: number;
  precision?: number;
}[] = [
  { key: 'storage_budget_gb', policyKey: 'storage_budget_mb', label: 'Storage budget, all profiles (GB)', min: 0.25, step: 1, precision: 2 },
  { key: 'per_profile_budget_gb', policyKey: 'per_profile_budget_mb', label: 'Per-profile budget (GB, 0 = none)', min: 0, step: 1, precision: 2 },
  { key: 'vision_daily_cap_default', policyKey: 'vision_daily_cap_default', label: 'Photos described per day (default)', min: 0, max: 1_000_000, step: 100 },
  { key: 'max_file_mb', policyKey: 'max_file_mb', label: 'Largest file read (MB)', min: 1, max: 4096, step: 1 },
  { key: 'workers', policyKey: 'workers', label: 'Indexing workers', min: 1, max: 8, step: 1 },
  { key: 'vector_capacity_mb', policyKey: 'vector_capacity_mb', label: 'Vector store capacity (MB, 0 = measure)', min: 0, step: 1024 },
  { key: 'db_capacity_mb', policyKey: 'db_capacity_mb', label: 'Database disk capacity (MB, 0 = measure)', min: 0, step: 1024 },
];

function toForm(p: DocumentsAdminPolicy): GateForm {
  return {
    allowed: p.allowed,
    storage_budget_gb: Math.round((p.storage_budget_mb / GB) * 100) / 100,
    per_profile_budget_gb: Math.round((p.per_profile_budget_mb / GB) * 100) / 100,
    vision_daily_cap_default: p.vision_daily_cap_default,
    max_file_mb: p.max_file_mb,
    workers: p.workers,
    vector_capacity_mb: p.vector_capacity_mb,
    db_capacity_mb: p.db_capacity_mb,
  };
}

function toPolicy(f: GateForm): DocumentsAdminPolicy {
  return {
    allowed: f.allowed,
    storage_budget_mb: Math.round(f.storage_budget_gb * GB),
    per_profile_budget_mb: Math.round(f.per_profile_budget_gb * GB),
    vision_daily_cap_default: Math.round(f.vision_daily_cap_default),
    max_file_mb: Math.round(f.max_file_mb),
    workers: Math.round(f.workers),
    vector_capacity_mb: Math.round(f.vector_capacity_mb),
    db_capacity_mb: Math.round(f.db_capacity_mb),
  };
}

/** Only the fields that changed — the server rejects nothing it was not sent,
 *  and an untouched budget never gets re-rounded through GB. */
const patch = computed<Partial<DocumentsAdminPolicy>>(() => {
  if (!view.value || !form.value) return {};
  const saved = view.value.policy;
  const next = toPolicy(form.value);
  const savedAsForm = toPolicy(toForm(saved));
  const out: Partial<DocumentsAdminPolicy> = {};
  for (const key of Object.keys(next) as (keyof DocumentsAdminPolicy)[]) {
    if (next[key] !== savedAsForm[key]) (out as Record<string, unknown>)[key] = next[key];
  }
  return out;
});
const dirty = computed(() => Object.keys(patch.value).length > 0);

const readOnly = computed(() => !view.value?.embedding_enabled);

const statusLine = computed(() => {
  const v = view.value;
  if (!v) return '';
  if (!v.policy.allowed) return 'Not allowed — no profile can turn it on.';
  if (v.reason === 'embedding_disabled') {
    return 'Allowed, but paused while Vector Embedding is off. Indexes are kept and searchable by keyword.';
  }
  return 'Allowed — each profile turns it on for itself under Settings → My Documents.';
});

async function reload() {
  loadError.value = '';
  try {
    const v = await getDocumentsAdmin(settingsStore.agentUrl, settingsStore.authToken);
    view.value = v;
    form.value = toForm(v.policy);
    fieldErrors.value = {};
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Could not load the Documentation search settings.';
  }
}

/** Save the changed fields. Returns whether it saved. Also the retry the
 *  install dialog runs after installing the missing document readers. */
async function save(): Promise<boolean> {
  if (!dirty.value || saving.value) return false;
  saving.value = true;
  fieldErrors.value = {};
  try {
    const v = await putDocumentsAdmin(settingsStore.agentUrl, settingsStore.authToken, patch.value);
    view.value = v;
    form.value = toForm(v.policy);
    ElMessage.success('Documentation search settings saved.');
    return true;
  } catch (e) {
    if (e instanceof DocumentsApiError) {
      if (e.code === 'FeatureNotInstalled' && e.missing?.length) {
        emit('feature-missing', { missing: e.missing, message: e.message });
        return false;
      }
      if (e.code === 'ValidationFailed' && e.details) {
        fieldErrors.value = e.details;
      }
    }
    ElMessage.error(e instanceof Error ? e.message : 'Could not save the settings.');
    return false;
  } finally {
    saving.value = false;
  }
}

function discard() {
  if (view.value) form.value = toForm(view.value.policy);
  fieldErrors.value = {};
}

onMounted(reload);
// The read-only state follows Vector Embedding being switched on or off.
watch(() => embeddingStatus.enabled, () => { void reload(); });

defineExpose({ save, reload });
</script>

<template>
  <section class="gate">
    <header class="gate-head">
      <Icon icon="mdi:file-search-outline" class="gate-icon" />
      <div>
        <h4 class="gate-title">Documentation search</h4>
        <p class="gate-sub">
          Lets each profile index a folder of its own files so the agent can search them and cite them.
        </p>
      </div>
    </header>

    <p v-if="loadError" class="gate-note bad">{{ loadError }}</p>

    <template v-else-if="view && form">
      <p v-if="readOnly" class="gate-note">
        <Icon icon="mdi:information-outline" />
        <span>
          Vector Embedding is off, so Documentation search cannot run and these settings are
          read-only. Profiles that had it on keep their indexes, searchable by keyword only.
        </span>
      </p>
      <p v-else class="gate-status">{{ statusLine }}</p>

      <!-- Plain fields rather than an ElForm: this card sits inside the
           embedding page's own ElForm (the ``after-enable`` slot), and a form
           nested in a form is invalid HTML. The outer form's ``disabled``
           (an embedding apply in flight) still reaches these controls. -->
      <div class="gate-form">
        <div class="gate-field">
          <label class="gate-label">Allow Documentation search</label>
          <ElSwitch v-model="form.allowed" :disabled="readOnly || saving" />
          <p v-if="fieldErrors.allowed" class="field-error">{{ fieldErrors.allowed }}</p>
          <p v-if="form.allowed && view.feature.missing.length" class="field-hint">
            The document readers (PDF, Office, photos) are not installed yet — saving offers to
            install them. Plain-text files index without them.
          </p>
        </div>

        <div class="gate-grid">
          <div v-for="f in NUMBER_FIELDS" :key="f.key" class="gate-field">
            <label class="gate-label">{{ f.label }}</label>
            <ElInputNumber
              v-model="form[f.key]"
              :min="f.min"
              :max="f.max"
              :step="f.step"
              :precision="f.precision"
              :disabled="readOnly || saving"
              controls-position="right"
            />
            <p v-if="fieldErrors[f.policyKey]" class="field-error">{{ fieldErrors[f.policyKey] }}</p>
          </div>
        </div>
        <p class="field-hint">
          Indexing pauses before the budget or the disk runs out — deletes and search keep working.
          Set the capacities when the vector store or database lives on a volume this server cannot
          measure (a fixed-size Docker volume or Kubernetes PVC).
        </p>
      </div>

      <div v-if="!readOnly" class="gate-actions">
        <ElButton type="primary" :loading="saving" :disabled="!dirty" @click="save">
          Save document search settings
        </ElButton>
        <ElButton v-if="dirty" :disabled="saving" @click="discard">Discard</ElButton>
      </div>

      <div v-if="view.profiles.length" class="gate-profiles">
        <h5>Profiles using it</h5>
        <ElTable :data="view.profiles" size="small" class="gate-table">
          <ElTableColumn prop="profile" label="Profile" />
          <ElTableColumn label="My Documents" width="140">
            <template #default="{ row }">{{ row.local_enabled ? 'On' : 'Off' }}</template>
          </ElTableColumn>
          <ElTableColumn label="Google Drive" width="140">
            <template #default="{ row }">{{ row.drive_enabled ? 'On' : 'Off' }}</template>
          </ElTableColumn>
        </ElTable>
      </div>
    </template>

    <p v-else class="gate-note">Loading…</p>
  </section>
</template>

<style scoped>
.gate {
  margin: 4px 0 18px; padding: 14px 16px; border-radius: 10px;
  border: 1px solid var(--border-color); background: var(--surface-color);
}
.gate-head { display: flex; gap: 10px; align-items: flex-start; margin-bottom: 8px; }
.gate-icon { font-size: 22px; color: var(--primary-color); flex: none; margin-top: 1px; }
.gate-title { margin: 0; font-size: 0.95rem; font-weight: 600; color: var(--text-primary); }
.gate-sub { margin: 2px 0 0; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45; }
.gate-status { margin: 0 0 8px; font-size: 0.82rem; color: var(--text-secondary); }
.gate-note {
  display: flex; gap: 8px; align-items: flex-start; margin: 0 0 10px;
  padding: 8px 10px; border-radius: 6px; font-size: 0.82rem; line-height: 1.45;
  background: color-mix(in srgb, var(--primary-color) 8%, var(--surface-color));
  border: 1px solid color-mix(in srgb, var(--primary-color) 30%, var(--border-color));
  color: var(--text-primary);
}
.gate-note :deep(svg) { flex: none; margin-top: 2px; color: var(--primary-color); }
.gate-note.bad {
  background: color-mix(in srgb, var(--danger-color) 8%, var(--surface-color));
  border-color: color-mix(in srgb, var(--danger-color) 35%, var(--border-color));
}
.gate-form { display: flex; flex-direction: column; gap: 12px; }
.gate-field { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }
.gate-label { font-size: 0.82rem; color: var(--text-primary); }
.gate-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px 16px; }
.gate-grid :deep(.el-input-number) { width: 100%; }
.field-hint { margin: 0; font-size: 0.775rem; color: var(--text-secondary); line-height: 1.4; }
.field-error { margin: 0; font-size: 0.75rem; color: var(--danger-color); }
.gate-actions { display: flex; gap: 8px; margin-top: 12px; }
.gate-actions :deep(.el-button + .el-button) { margin-left: 0; }
.gate-profiles { margin-top: 16px; }
.gate-profiles h5 {
  margin: 0 0 6px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--text-tertiary);
}
.gate-table { width: 100%; }
</style>
