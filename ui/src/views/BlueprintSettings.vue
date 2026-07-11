<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import {
  ElButton, ElCard, ElCheckbox, ElInput, ElMessage, ElMessageBox,
  ElRadioButton, ElRadioGroup, ElTable, ElTableColumn,
} from 'element-plus';

import { useSettingsStore } from '../stores/settings';
import { getHubUrl } from '../services/runtimeConfig';
import { uploadBlueprintToHub } from '../services/hubPublish';
import {
  deleteBlueprint, downloadBlueprint, exportBlueprint, getExportable,
  importBlueprintFromHub, listBlueprints,
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
const selectedTools = ref<Record<string, boolean>>({});
const name = ref('');
const description = ref('');
const archives = ref<BlueprintEntry[]>([]);
const loading = ref(false);
const exporting = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

// Upload to Cremind Hub (Method 2 browser hand-off).
const hubUrl = ref('');
const uploading = reactive<Record<string, boolean>>({});

// Import mode: from a local file, or downloaded from the Cremind Hub.
const importMode = ref<'file' | 'hub'>('file');
const hubLink = ref('');
const importingHub = ref(false);

const availableComponents = computed(() =>
  COMPONENT_ORDER.filter(k => exportable.value?.components?.[k]?.available),
);

const skillItems = computed(() => exportable.value?.components?.skills?.items ?? []);
const toolItems = computed(() => exportable.value?.components?.tools?.items ?? []);

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
  if (key === 'tools') return (c.items ?? []).map((i: any) => i.name).join(', ');
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
  const toolSel: Record<string, boolean> = {};
  for (const t of toolItems.value) toolSel[t.tool_id] = true;
  selectedTools.value = toolSel;
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
  const tools = toolItems.value.filter((t: any) => selectedTools.value[t.tool_id]).map((t: any) => t.tool_id);
  exporting.value = true;
  try {
    const res = await exportBlueprint(c.url, c.token, {
      components,
      skills: components.includes('skills') ? skills : undefined,
      tools: components.includes('tools') ? tools : undefined,
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

async function onUpload(entry: BlueprintEntry) {
  const c = creds();
  if (!hubUrl.value) {
    ElMessage.error('Could not resolve the Cremind Hub URL.');
    return;
  }
  uploading[entry.name] = true;
  try {
    await uploadBlueprintToHub({
      agentUrl: c.url,
      authToken: c.token,
      hubUrl: hubUrl.value,
      name: entry.name,
      displayName: entry.manifest?.display_name,
    });
    ElMessage.success('Uploaded to Cremind Hub — open it there to publish');
  } catch (e: any) {
    ElMessage.error(e?.message ?? 'Upload failed');
  } finally {
    uploading[entry.name] = false;
  }
}

async function onImportFromHub() {
  const c = creds();
  const link = hubLink.value.trim();
  if (!link) {
    ElMessage.warning('Enter a Cremind Hub link or blueprint name');
    return;
  }
  importingHub.value = true;
  try {
    await importBlueprintFromHub(c.url, c.token, link, false);
    router.push(`/${props.profile}/settings/blueprints/import`);
  } catch (e: any) {
    // Mirror the file-upload path: offer to replace an in-progress import.
    if (/in progress|session_exists|session_busy|already/i.test(e?.message ?? '')) {
      try {
        await ElMessageBox.confirm(
          'Another blueprint import is in progress. Replace it?', 'Confirm', { type: 'warning' },
        );
        await importBlueprintFromHub(c.url, c.token, link, true);
        router.push(`/${props.profile}/settings/blueprints/import`);
        return;
      } catch { /* cancelled */ }
    }
    ElMessage.error(e?.message ?? 'Import failed');
  } finally {
    importingHub.value = false;
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
  try {
    hubUrl.value = await getHubUrl(creds().url);
  } catch { /* publish button will report if unavailable */ }
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
              <div v-if="key === 'tools' && selected['tools']" class="bp-subtools">
                <div v-for="t in toolItems" :key="t.tool_id" class="bp-subtool">
                  <ElCheckbox v-model="selectedTools[t.tool_id]">
                    {{ t.name }}
                    <span class="bp-kind">{{ t.kind === 'a2a' ? 'A2A' : t.kind === 'mcp' ? 'MCP' : 'built-in' }}</span>
                    <span class="bp-hint">
                      <template v-if="t.settings_count"> · {{ t.settings_count }} setting(s)</template>
                      <template v-if="(t.secret_variables || []).length"> · needs: {{ t.secret_variables.join(', ') }}</template>
                      <template v-if="t.disabled_leaves"> · {{ t.disabled_leaves }} sub-tool(s) disabled</template>
                    </span>
                  </ElCheckbox>
                  <div v-if="t.description && t.description !== t.name" class="bp-hint bp-tool-desc">{{ t.description }}</div>
                </div>
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
          <ElTableColumn label="Actions" width="280">
            <template #default="{ row }">
              <ElButton size="small" @click="onDownload(row as BlueprintEntry)">Download</ElButton>
              <ElButton
                size="small" type="primary" plain
                :loading="uploading[(row as BlueprintEntry).name]"
                @click="onUpload(row as BlueprintEntry)"
              >
                Upload to Hub
              </ElButton>
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
        <div class="bp-import-mode">
          <ElRadioGroup v-model="importMode" size="small">
            <ElRadioButton label="file">From a file</ElRadioButton>
            <ElRadioButton label="hub">From Cremind Hub</ElRadioButton>
          </ElRadioGroup>
        </div>

        <template v-if="importMode === 'file'">
          <input
            ref="fileInput" type="file" accept=".cremind-blueprint"
            style="display:none" @change="onFileChosen"
          />
          <ElButton type="primary" plain @click="pickFile">
            <Icon icon="mdi:upload" /> Choose a .cremind-blueprint file…
          </ElButton>
        </template>

        <template v-else>
          <div class="bp-hub-import">
            <ElInput
              v-model="hubLink"
              placeholder="https://hub.cremind.io/blueprints/my-blueprint (or a blueprint name)"
            />
            <ElButton type="primary" :loading="importingHub" @click="onImportFromHub">
              Import from hub
            </ElButton>
          </div>
          <p class="bp-hint">Cremind downloads the blueprint from the Hub and runs the import wizard.</p>
        </template>
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
.bp-subtools { display: flex; flex-direction: column; gap: 6px; margin: 4px 0 4px 24px; }
.bp-subtool { display: flex; flex-direction: column; gap: 0; }
.bp-kind { display: inline-block; margin-left: 6px; padding: 0 6px; border-radius: 8px; font-size: 0.65rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-secondary); background: var(--el-fill-color-light, rgba(128, 128, 128, 0.15)); vertical-align: middle; }
.bp-tool-desc { margin: 0 0 0 24px; line-height: 1.4; white-space: normal; }
.bp-hint { color: var(--text-tertiary); font-size: 0.75rem; }
.bp-meta { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.bp-warn { color: var(--el-color-warning); font-size: 0.78rem; margin: 0 0 12px 0; }
.bp-import-mode { margin-bottom: 12px; }
.bp-hub-import { display: flex; gap: 8px; align-items: center; }
.bp-hint { color: var(--text-tertiary); font-size: 0.78rem; margin: 8px 0 0 0; }
</style>
