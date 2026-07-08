<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import {
  ElButton, ElCard, ElCheckbox, ElInput, ElMessage, ElMessageBox, ElTable, ElTableColumn,
} from 'element-plus';

import { useSettingsStore } from '../stores/settings';
import {
  deleteBlueprint, downloadBlueprint, exportBlueprint, getExportable, listBlueprints,
  type BlueprintEntry, type ExportableResponse,
} from '../services/blueprintApi';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settings = useSettingsStore();

const COMPONENT_ORDER = ['persona', 'tools', 'llm', 'settings', 'skills', 'events', 'listeners'];
const COMPONENT_LABELS: Record<string, string> = {
  persona: 'Agent persona', tools: 'Tools', llm: 'LLM provider', settings: 'Changed settings',
  skills: 'Skills', events: 'Events', listeners: 'Listeners',
};

const exportable = ref<ExportableResponse | null>(null);
const selected = ref<Record<string, boolean>>({});
const selectedSkills = ref<Record<string, boolean>>({});
const name = ref('');
const description = ref('');
const archives = ref<BlueprintEntry[]>([]);
const loading = ref(false);
const exporting = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

const availableComponents = computed(() =>
  COMPONENT_ORDER.filter(k => exportable.value?.components?.[k]?.available),
);

const skillItems = computed(() => exportable.value?.components?.skills?.items ?? []);

function creds() {
  return { url: settings.agentUrl, token: settings.authToken };
}

function componentDetail(key: string): string {
  const c = exportable.value?.components?.[key];
  if (!c) return '';
  if (key === 'settings') return `${c.count ?? 0} setting(s) changed`;
  if (key === 'skills') return (c.items ?? []).map((i: any) => i.name).join(', ');
  if (key === 'events') {
    const n = c.counts ?? {};
    return `${n.schedule ?? 0} schedule, ${n.file_watcher ?? 0} watcher, ${n.skill_event ?? 0} skill event`;
  }
  if (key === 'llm') return `provider: ${c.summary?.default_provider ?? '-'}`;
  if (key === 'tools') return `${c.count ?? 0} tool(s)`;
  if (key === 'listeners') return `${(c.items ?? []).length} listener(s)`;
  if (key === 'persona') return `agent: ${c.summary?.agent_name ?? '-'}`;
  return '';
}

async function loadExportable() {
  const c = creds();
  if (!c.url) return;
  exportable.value = await getExportable(c.url, c.token);
  const sel: Record<string, boolean> = {};
  for (const k of availableComponents.value) sel[k] = true;
  selected.value = sel;
  const skillSel: Record<string, boolean> = {};
  for (const s of skillItems.value) skillSel[s.slug] = true;
  selectedSkills.value = skillSel;
}

async function loadArchives() {
  const c = creds();
  if (!c.url) return;
  loading.value = true;
  try {
    archives.value = await listBlueprints(c.url, c.token);
  } finally {
    loading.value = false;
  }
}

// Events/listeners reference skills, so keep skills included when they are.
const needsSkills = computed(() =>
  (selected.value.events && (exportable.value?.components?.events?.counts?.skill_event ?? 0) > 0)
  || (selected.value.listeners && (exportable.value?.components?.listeners?.items ?? []).length > 0),
);

async function doExport() {
  const c = creds();
  if (!c.url) return;
  let components = COMPONENT_ORDER.filter(k => selected.value[k]);
  if (needsSkills.value && !components.includes('skills') && availableComponents.value.includes('skills')) {
    components.push('skills');
    selected.value.skills = true;
    ElMessage.info('Skills included automatically — events/listeners reference them.');
  }
  if (components.length === 0) {
    ElMessage.warning('Select at least one component to export.');
    return;
  }
  const skills = skillItems.value.filter((s: any) => selectedSkills.value[s.slug]).map((s: any) => s.slug);
  exporting.value = true;
  try {
    const res = await exportBlueprint(c.url, c.token, {
      components,
      skills: components.includes('skills') ? skills : undefined,
      name: name.value || undefined,
      display_name: name.value || undefined,
      description: description.value || undefined,
    });
    ElMessage.success(`Exported ${res.file.name}`);
    for (const w of res.warnings || []) ElMessage.warning(w);
    await loadArchives();
  } catch (e: any) {
    ElMessage.error(e?.message ?? 'Export failed');
  } finally {
    exporting.value = false;
  }
}

async function onDownload(entry: BlueprintEntry) {
  const c = creds();
  try {
    await downloadBlueprint(c.url, c.token, entry.name);
  } catch (e: any) {
    ElMessage.error(e?.message ?? 'Download failed');
  }
}

async function onDelete(entry: BlueprintEntry) {
  const c = creds();
  try {
    await ElMessageBox.confirm(`Delete blueprint "${entry.name}"?`, 'Confirm', { type: 'warning' });
  } catch { return; }
  try {
    await deleteBlueprint(c.url, c.token, entry.name);
    await loadArchives();
  } catch (e: any) {
    ElMessage.error(e?.message ?? 'Delete failed');
  }
}

function pickFile() {
  fileInput.value?.click();
}

async function onFileChosen(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;
  // Hand the chosen file to the wizard via sessionStorage (upload happens there).
  const buf = await file.arrayBuffer();
  (window as any).__cremindBlueprintUpload = { name: file.name, bytes: buf };
  router.push(`/${props.profile}/settings/blueprints/import`);
}

function goBack() {
  router.push(`/${props.profile}/settings`);
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

onMounted(async () => {
  await loadExportable();
  await loadArchives();
});
</script>

<template>
  <div class="bp-page">
    <div class="bp-container">
      <div class="bp-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" /> Back to Settings
        </button>
        <h1 class="bp-title">Blueprints</h1>
        <p class="bp-subtitle">
          Package this profile's design (persona, tools, skills, LLM choice, settings, events)
          into a shareable file — no secrets included.
        </p>
      </div>

      <!-- Export -->
      <ElCard class="bp-card" shadow="never">
        <template #header><strong>Export this profile</strong></template>
        <div v-if="availableComponents.length === 0" class="bp-empty">
          Nothing to export yet — customize the persona, tools, LLM, settings, or skills first.
        </div>
        <template v-else>
          <div class="bp-checklist">
            <div v-for="key in availableComponents" :key="key" class="bp-row">
              <ElCheckbox v-model="selected[key]">
                <span class="bp-label">{{ COMPONENT_LABELS[key] }}</span>
              </ElCheckbox>
              <span class="bp-detail">{{ componentDetail(key) }}</span>
              <div v-if="key === 'skills' && selected['skills']" class="bp-subskills">
                <ElCheckbox v-for="s in skillItems" :key="s.slug" v-model="selectedSkills[s.slug]">
                  {{ s.name }}
                  <span class="bp-hint">
                    {{ s.builtin ? '(built-in — settings only)' : `(${fmtBytes(s.approx_bytes || 0)})` }}
                    <template v-if="(s.secret_variables || []).length"> · needs: {{ s.secret_variables.join(', ') }}</template>
                  </span>
                </ElCheckbox>
              </div>
            </div>
          </div>
          <div class="bp-meta">
            <ElInput v-model="name" placeholder="Blueprint name (e.g. customer-service)" />
            <ElInput v-model="description" placeholder="One-line description" />
          </div>
          <p class="bp-warn">
            Persona and action text are exported verbatim — review them for any secret you may have pasted in.
          </p>
          <ElButton type="primary" :loading="exporting" @click="doExport">Export blueprint</ElButton>
        </template>
      </ElCard>

      <!-- Archives -->
      <ElCard class="bp-card" shadow="never">
        <template #header><strong>Exported blueprints</strong></template>
        <ElTable v-loading="loading" :data="archives" size="small" empty-text="No blueprints yet">
          <ElTableColumn prop="name" label="Name" min-width="180" />
          <ElTableColumn label="Profile" width="120">
            <template #default="{ row }">{{ row.manifest?.source_profile ?? '-' }}</template>
          </ElTableColumn>
          <ElTableColumn label="Size" width="90">
            <template #default="{ row }">{{ fmtBytes(row.size_bytes) }}</template>
          </ElTableColumn>
          <ElTableColumn label="Actions" width="170">
            <template #default="{ row }">
              <ElButton size="small" @click="onDownload(row as BlueprintEntry)">Download</ElButton>
              <ElButton size="small" type="danger" plain @click="onDelete(row as BlueprintEntry)">Delete</ElButton>
            </template>
          </ElTableColumn>
        </ElTable>
      </ElCard>

      <!-- Import -->
      <ElCard class="bp-card" shadow="never">
        <template #header><strong>Import a blueprint</strong></template>
        <p class="bp-detail">
          Import applies a blueprint's design to <strong>this profile ({{ props.profile }})</strong>.
          Create a fresh profile first if you don't want to change an existing one.
        </p>
        <input
          ref="fileInput" type="file" accept=".cremind-blueprint"
          style="display:none" @change="onFileChosen"
        />
        <ElButton type="primary" plain @click="pickFile">
          <Icon icon="mdi:upload" /> Choose a .cremind-blueprint file…
        </ElButton>
      </ElCard>
    </div>
  </div>
</template>

<style scoped>
.bp-page { width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color); padding: 24px; box-sizing: border-box; }
.bp-container { max-width: 760px; margin: 0 auto; }
.bp-header { margin-bottom: 24px; }
.back-btn { display: flex; align-items: center; gap: 6px; background: none; border: none; color: var(--text-secondary); cursor: pointer; font-size: 0.875rem; padding: 4px 0; margin-bottom: 12px; }
.back-btn:hover { color: var(--primary-color); }
.bp-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 4px 0; color: var(--text-primary); }
.bp-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.bp-card { margin-bottom: 16px; }
.bp-empty { color: var(--text-secondary); font-size: 0.9rem; }
.bp-checklist { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
.bp-row { display: flex; flex-direction: column; gap: 2px; }
.bp-label { font-weight: 600; }
.bp-detail { color: var(--text-secondary); font-size: 0.8rem; margin-left: 24px; }
.bp-subskills { display: flex; flex-direction: column; gap: 2px; margin: 4px 0 4px 24px; }
.bp-hint { color: var(--text-tertiary); font-size: 0.75rem; }
.bp-meta { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.bp-warn { color: var(--el-color-warning); font-size: 0.78rem; margin: 0 0 12px 0; }
</style>
