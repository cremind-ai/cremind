<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import {
  ElButton, ElCard, ElCheckbox, ElInput, ElMessage, ElMessageBox,
  ElRadioButton, ElRadioGroup, ElTable, ElTableColumn, ElTag,
} from 'element-plus';

import { useSettingsStore } from '../stores/settings';
import { getHubUrl } from '../services/runtimeConfig';
import { uploadBlueprintToHub } from '../services/hubPublish';
import {
  deleteBlueprint, downloadBlueprint, exportBlueprint, getExportable,
  importBlueprintFromHub, listBlueprints,
  type BlueprintEntry, type ExportableResponse,
  type SettingItem, type SkillItem, type ToolItem, type ListenerItem,
  type EventItemGroups, type SkillEventItem,
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
const selectedSettings = ref<Record<string, boolean>>({});
const selectedEvents = ref<Record<string, boolean>>({});
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

const skillItems = computed<SkillItem[]>(() => (exportable.value?.components?.skills?.items as SkillItem[]) ?? []);
const toolItems = computed<ToolItem[]>(() => (exportable.value?.components?.tools?.items as ToolItem[]) ?? []);
const settingItems = computed<SettingItem[]>(() => (exportable.value?.components?.settings?.items as SettingItem[]) ?? []);
const listenerItems = computed<ListenerItem[]>(() => (exportable.value?.components?.listeners?.items as ListenerItem[]) ?? []);
const eventGroups = computed<EventItemGroups>(() => {
  const g = exportable.value?.components?.events?.items as EventItemGroups | undefined;
  return {
    schedule: g?.schedule ?? [],
    file_watcher: g?.file_watcher ?? [],
    skill_event: g?.skill_event ?? [],
  };
});
const allEventItems = computed(() => [
  ...eventGroups.value.schedule,
  ...eventGroups.value.file_watcher,
  ...eventGroups.value.skill_event,
]);

function fmtSetting(v: any): string {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  return String(v);
}

function creds() {
  return { url: settings.agentUrl, token: settings.authToken };
}

function countSelected(map: Record<string, boolean>, ids: string[]): number {
  return ids.filter(id => map[id]).length;
}

function componentDetail(key: string): string {
  const c = exportable.value?.components?.[key];
  if (!c) return '';
  if (key === 'settings') {
    const total = settingItems.value.length;
    const sel = countSelected(selectedSettings.value, settingItems.value.map(s => s.key));
    return `${sel} of ${total} setting(s)`;
  }
  if (key === 'skills') return skillItems.value.map(i => i.name).join(', ');
  if (key === 'events') {
    const g = eventGroups.value;
    const s = countSelected(selectedEvents.value, g.schedule.map(e => e.id));
    const w = countSelected(selectedEvents.value, g.file_watcher.map(e => e.id));
    const e = countSelected(selectedEvents.value, g.skill_event.map(x => x.id));
    return `${s}/${g.schedule.length} schedule, ${w}/${g.file_watcher.length} watcher, ${e}/${g.skill_event.length} skill event`;
  }
  if (key === 'llm') {
    const s = c.summary ?? {};
    const groups = s.model_groups ?? {};
    const models = Object.entries(groups).map(([k, v]) => `${k}=${v}`).join(', ');
    return `provider: ${s.default_provider ?? '-'}${models ? ` · ${models}` : ''}`;
  }
  if (key === 'tools') return toolItems.value.map(i => i.name).join(', ');
  if (key === 'listeners') return listenerItems.value.map(i => i.skill_name).join(', ') || `${listenerItems.value.length} listener(s)`;
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
  const settingSel: Record<string, boolean> = {};
  for (const s of settingItems.value) settingSel[s.key] = true;
  selectedSettings.value = settingSel;
  const eventSel: Record<string, boolean> = {};
  for (const e of allEventItems.value) eventSel[e.id] = true;
  selectedEvents.value = eventSel;
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

// Selected skill events (their referenced skills must ship alongside them).
const selectedSkillEvents = computed<SkillEventItem[]>(() =>
  eventGroups.value.skill_event.filter(e => selectedEvents.value[e.id]),
);

// Events/listeners reference skills, so keep skills included when they are.
const needsSkills = computed(() =>
  (selected.value.events && selectedSkillEvents.value.length > 0)
  || (selected.value.listeners && listenerItems.value.length > 0),
);

async function doExport() {
  const c = creds();
  if (!c.url) return;

  const settingKeys = settingItems.value.filter(s => selectedSettings.value[s.key]).map(s => s.key);
  const eventIds = allEventItems.value.filter(e => selectedEvents.value[e.id]).map(e => e.id);

  let components = COMPONENT_ORDER.filter(k => selected.value[k]);
  // Drop a selected component that has items but none checked — it would export an empty doc.
  if (components.includes('settings') && settingItems.value.length > 0 && settingKeys.length === 0) {
    components = components.filter(k => k !== 'settings');
    ElMessage.info('Changed settings skipped — no individual setting selected.');
  }
  if (components.includes('events') && allEventItems.value.length > 0 && eventIds.length === 0) {
    components = components.filter(k => k !== 'events');
    ElMessage.info('Events skipped — no individual event selected.');
  }

  if (needsSkills.value && !components.includes('skills') && availableComponents.value.includes('skills')) {
    components.push('skills');
    selected.value.skills = true;
    // Auto-check the skills the selected skill events reference so they actually ship.
    for (const e of selectedSkillEvents.value) selectedSkills.value[e.skill_slug] = true;
    ElMessage.info('Skills included automatically — events/listeners reference them.');
  }
  if (components.length === 0) {
    ElMessage.warning('Select at least one component to export.');
    return;
  }
  const skills = skillItems.value.filter(s => selectedSkills.value[s.slug]).map(s => s.slug);
  const tools = toolItems.value.filter(t => selectedTools.value[t.tool_id]).map(t => t.tool_id);
  exporting.value = true;
  try {
    const res = await exportBlueprint(c.url, c.token, {
      components,
      skills: components.includes('skills') ? skills : undefined,
      tools: components.includes('tools') ? tools : undefined,
      settings: components.includes('settings') ? settingKeys : undefined,
      events: components.includes('events') ? eventIds : undefined,
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
                    <template v-if="(s.secret_variables || []).length"> · needs: {{ (s.secret_variables || []).join(', ') }}</template>
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
                      <template v-if="(t.secret_variables || []).length"> · needs: {{ (t.secret_variables || []).join(', ') }}</template>
                      <template v-if="t.disabled_leaves"> · {{ t.disabled_leaves }} sub-tool(s) disabled</template>
                    </span>
                  </ElCheckbox>
                  <div v-if="t.description && t.description !== t.name" class="bp-hint bp-tool-desc">{{ t.description }}</div>
                </div>
              </div>

              <!-- settings: per-key detail + selection -->
              <div v-if="key === 'settings' && selected['settings']" class="bp-settings">
                <table class="bp-settings-table">
                  <thead>
                    <tr><th></th><th>Setting</th><th>Type</th><th>Default</th><th>Exported</th><th></th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="s in settingItems" :key="s.key">
                      <td class="bp-st-check"><ElCheckbox v-model="selectedSettings[s.key]" /></td>
                      <td>
                        <div class="bp-st-label">{{ s.label }}</div>
                        <div class="bp-hint">
                          {{ s.key }}<template v-if="s.group_label"> · {{ s.group_label }}</template>
                        </div>
                      </td>
                      <td><span class="bp-kind">{{ s.type }}</span></td>
                      <td class="bp-st-val">{{ fmtSetting(s.default) }}</td>
                      <td class="bp-st-val"><strong>{{ fmtSetting(s.value) }}</strong></td>
                      <td>
                        <ElTag v-if="s.unknown" type="warning" size="small" disable-transitions>not in this build</ElTag>
                        <ElTag v-else-if="s.is_default" type="info" size="small" disable-transitions>same as default</ElTag>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <!-- events: per-item detail + selection, grouped by kind -->
              <div v-if="key === 'events' && selected['events']" class="bp-events">
                <div v-if="eventGroups.schedule.length" class="bp-event-group">
                  <div class="bp-event-title">Schedules</div>
                  <ElCheckbox v-for="s in eventGroups.schedule" :key="s.id" v-model="selectedEvents[s.id]">
                    {{ s.title || '(untitled)' }}
                    <span class="bp-hint">
                      · {{ s.rrule ? s.rrule : (s.all_day ? 'one-time · all day' : 'one-time') }}
                      <template v-if="s.timezone"> · {{ s.timezone }}</template>
                    </span>
                  </ElCheckbox>
                </div>
                <div v-if="eventGroups.file_watcher.length" class="bp-event-group">
                  <div class="bp-event-title">File watchers</div>
                  <ElCheckbox v-for="w in eventGroups.file_watcher" :key="w.id" v-model="selectedEvents[w.id]">
                    {{ w.name }}
                    <span class="bp-hint">
                      · {{ w.root_path }}<template v-if="w.recursive"> · recursive</template>
                    </span>
                  </ElCheckbox>
                </div>
                <div v-if="eventGroups.skill_event.length" class="bp-event-group">
                  <div class="bp-event-title">Skill events</div>
                  <ElCheckbox v-for="e in eventGroups.skill_event" :key="e.id" v-model="selectedEvents[e.id]">
                    {{ e.skill_name || e.skill_slug }} / {{ e.event_type }}
                  </ElCheckbox>
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
.bp-settings { margin: 4px 0 4px 24px; overflow-x: auto; }
.bp-settings-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
.bp-settings-table th { text-align: left; font-weight: 600; color: var(--text-secondary); padding: 4px 8px; border-bottom: 1px solid var(--el-border-color-lighter, rgba(128,128,128,0.2)); }
.bp-settings-table td { padding: 4px 8px; border-bottom: 1px solid var(--el-border-color-lighter, rgba(128,128,128,0.12)); vertical-align: top; }
.bp-st-check { width: 28px; }
.bp-st-label { font-weight: 600; }
.bp-st-val { font-family: var(--el-font-family-mono, monospace); white-space: nowrap; }
.bp-events { display: flex; flex-direction: column; gap: 8px; margin: 4px 0 4px 24px; }
.bp-event-group { display: flex; flex-direction: column; gap: 2px; }
.bp-event-title { font-weight: 600; font-size: 0.8rem; color: var(--text-secondary); }
.bp-meta { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.bp-warn { color: var(--el-color-warning); font-size: 0.78rem; margin: 0 0 12px 0; }
.bp-import-mode { margin-bottom: 12px; }
.bp-hub-import { display: flex; gap: 8px; align-items: center; }
.bp-hint { color: var(--text-tertiary); font-size: 0.78rem; margin: 8px 0 0 0; }
</style>
