<script setup lang="ts">
/**
 * The admin gate for Documentation search: the "Administrator settings"
 * section of Settings → My Documents, rendered for the admin only (both
 * `/api/documentation-search/admin` routes are admin-only).
 *
 * Server-wide, unlike the rest of that page: whether profiles may use the
 * feature at all, the storage budgets, the indexing workers, and which
 * profiles use it. Each profile then turns it on for itself on its own My
 * Documents page. The page itself only exists while Vector Embedding is on —
 * the feature rides the server-wide embedding model and vector store — so
 * this section never has to explain an embedding that is off. It saves on its
 * own (`PUT /api/documentation-search/admin`), apart from the page's
 * per-profile settings.
 *
 * Allowing it when the document readers (PDF, Office, images) are not
 * installed answers `409 FeatureNotInstalled`; that is handed to the page's
 * install dialog (`feature-missing`), which calls `save()` again once the
 * install is done. Once allowed, missing readers can be installed on their
 * own (`install-readers`), after which the page calls `reload()`.
 */
import { computed, onMounted, ref } from 'vue';
import {
  ElButton, ElInputNumber, ElMessage, ElSwitch, ElTable, ElTableColumn,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../../stores/settings';
import {
  getDocumentsAdmin,
  putDocumentsAdmin,
  DocumentsApiError,
  type DocumentsAdminPolicy,
  type DocumentsAdminView,
  type DocumentsFeatureMissing,
} from '../../services/documentsApi';

/** What is missing, in the shape of the server's 409 body. */
type MissingReaders = { missing: DocumentsFeatureMissing[]; message: string };

const emit = defineEmits<{
  /** A save needs the document readers first; the page installs them and calls `save()` again. */
  'feature-missing': [detail: MissingReaders];
  /** The admin asked to install the missing readers (nothing to save); the page calls `reload()` after. */
  'install-readers': [detail: MissingReaders];
  /** Saved: the policy every profile's page reads has changed. */
  saved: [view: DocumentsAdminView];
}>();

const settingsStore = useSettingsStore();

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

const statusLine = computed(() => {
  const v = view.value;
  if (!v) return '';
  if (!v.policy.allowed) return 'Not allowed — no profile can turn it on.';
  return 'Allowed — each profile turns it on for itself on its own My Documents page.';
});

/** Allowed already, but PDF, Office and photo files wait for their readers. */
const readersMissing = computed(() => !!view.value?.policy.allowed && !!view.value.feature.missing.length);

function installReaders() {
  const missing = view.value?.feature.missing ?? [];
  if (!missing.length) return;
  emit('install-readers', {
    missing,
    message: 'Documentation search reads PDF, Office and photo files with optional document readers.',
  });
}

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
    emit('saved', v);
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

defineExpose({ save, reload });
</script>

<template>
  <section class="gate" aria-labelledby="documents-admin-title">
    <header class="gate-head">
      <Icon icon="mdi:shield-account-outline" class="gate-icon" />
      <div>
        <h2 id="documents-admin-title" class="gate-title">
          Administrator settings
          <span class="gate-badge">Whole server</span>
        </h2>
        <p class="gate-sub">
          Whether profiles may use Documentation search, and the limits they all share. It applies
          to every profile on this server; only the admin sees it.
        </p>
      </div>
    </header>

    <p v-if="loadError" class="gate-note bad">{{ loadError }}</p>

    <template v-else-if="view && form">
      <p class="gate-status">{{ statusLine }}</p>

      <!-- Plain fields rather than an ElForm: the numbers lay themselves out
           in the grid below, and the server's field errors are shown under
           each field by hand — nothing here needs ElForm's own validation. -->
      <div class="gate-form">
        <div class="gate-field">
          <label class="gate-label">Allow Documentation search</label>
          <ElSwitch v-model="form.allowed" :disabled="saving" aria-label="Allow Documentation search" />
          <p v-if="fieldErrors.allowed" class="field-error">{{ fieldErrors.allowed }}</p>
          <p v-if="readersMissing && form.allowed" class="field-hint">
            The document readers (PDF, Office, photos) are not installed, so those files wait;
            plain-text files index normally.
            <ElButton link type="primary" size="small" :disabled="saving" @click="installReaders">
              Install them
            </ElButton>
          </p>
          <p v-else-if="form.allowed && view.feature.missing.length" class="field-hint">
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
              :disabled="saving"
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

      <div class="gate-actions">
        <ElButton type="primary" :loading="saving" :disabled="!dirty" @click="save">
          Save administrator settings
        </ElButton>
        <ElButton v-if="dirty" :disabled="saving" @click="discard">Discard</ElButton>
      </div>

      <div v-if="view.profiles.length" class="gate-profiles">
        <h5>Profiles using it</h5>
        <ElTable :data="view.profiles" size="small" class="gate-table">
          <ElTableColumn prop="profile" label="Profile" />
          <ElTableColumn label="Working directory" width="160">
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
/* A page card like its neighbours on My Documents, set apart by an accent
   edge: it is the one section that acts on every profile. */
.gate {
  padding: 16px 18px; border-radius: 10px;
  border: 1px solid var(--border-color); border-left: 3px solid var(--primary-color);
  background: var(--surface-color);
}
.gate-head { display: flex; gap: 10px; align-items: flex-start; margin-bottom: 10px; }
.gate-icon { font-size: 22px; color: var(--primary-color); flex: none; margin-top: 1px; }
.gate-title {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin: 0; font-size: 1rem; font-weight: 600; color: var(--text-primary);
}
.gate-badge {
  padding: 1px 8px; border-radius: 999px; font-size: 0.7rem; font-weight: 600;
  letter-spacing: 0.02em; color: var(--primary-color);
  background: color-mix(in srgb, var(--primary-color) 12%, var(--surface-color));
}
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
