<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import {
  ElAlert, ElButton, ElCard, ElCheckbox, ElInput, ElMessage, ElMessageBox, ElStep, ElSteps,
} from 'element-plus';

import { useBlueprintImport } from '../composables/useBlueprintImport';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const bp = useBlueprintImport();

const reviewed = ref(false);
const llmSecrets = reactive<Record<string, string>>({});
const toolSecrets = reactive<Record<string, Record<string, string>>>({});
const skillSecrets = reactive<Record<string, Record<string, string>>>({});
const watcherPaths = reactive<Record<string, string>>({});
const listenersConfirmed = reactive<Record<string, boolean>>({});

const uploading = ref(false);
const uploadError = ref<string | null>(null);

const plan = bp.plan;
const currentIndex = bp.currentStepIndex;
const currentStep = computed(() => plan.value[currentIndex.value] ?? null);
const allDone = computed(() => plan.value.length > 0 && currentIndex.value >= plan.value.length);
const manifest = computed(() => bp.session.value?.manifest ?? null);

function reqsOf(key: string): any[] {
  return currentStep.value?.key === key ? (currentStep.value?.requirements ?? []) : [];
}

async function tryUploadStashed() {
  const stashed = (window as any).__cremindBlueprintUpload;
  if (!stashed) {
    await bp.rehydrate();
    return;
  }
  delete (window as any).__cremindBlueprintUpload;
  uploading.value = true;
  uploadError.value = null;
  try {
    const file = new File([stashed.bytes], stashed.name, { type: 'application/octet-stream' });
    await bp.upload(file, false);
  } catch (e: any) {
    // A session may already exist — offer to replace it.
    if (String(e?.message || '').toLowerCase().includes('already in progress')) {
      try {
        await ElMessageBox.confirm(
          'An import is already in progress. Abort it and start this one?', 'Import in progress',
          { type: 'warning', confirmButtonText: 'Replace' },
        );
        const file = new File([stashed.bytes], stashed.name, { type: 'application/octet-stream' });
        await bp.upload(file, true);
      } catch {
        uploadError.value = 'Import cancelled.';
      }
    } else {
      uploadError.value = e?.message ?? 'Upload failed';
    }
  } finally {
    uploading.value = false;
  }
}

async function submitStep() {
  const step = currentStep.value;
  if (!step) return;
  const key = step.key;
  try {
    if (key === 'llm') {
      await bp.step('llm', { secrets: { ...llmSecrets } });
    } else if (key === 'tools') {
      await bp.step('tools', { secrets: toolSecrets });
    } else if (key === 'skills') {
      await bp.step('skills', { secrets: skillSecrets, conflicts: {} });
    } else if (key === 'events') {
      await bp.step('events', { watcher_paths: { ...watcherPaths } });
    } else if (key === 'listeners') {
      const confirmed = Object.keys(listenersConfirmed).filter(k => listenersConfirmed[k]);
      await bp.step('listeners', { confirmed });
    } else {
      await bp.step(key, {});
    }
  } catch {
    // error surfaced via bp.error
  }
}

async function skipStep() {
  const step = currentStep.value;
  if (!step) return;
  try {
    await bp.skip(step.key);
  } catch { /* surfaced */ }
}

async function doFinalize() {
  try {
    await bp.finalize();
  } catch (e: any) {
    ElMessage.error(e?.message ?? 'Finalize failed');
  }
}

async function doAbort() {
  try {
    await ElMessageBox.confirm(
      `Stop importing? Steps already applied stay in profile "${props.profile}". `
      + 'Delete the profile yourself if you want a clean slate.',
      'Stop import', { type: 'warning', confirmButtonText: 'Stop import' },
    );
  } catch { return; }
  await bp.abort(false);
  router.push(`/${props.profile}/settings/blueprints`);
}

function finish() {
  bp.reset();
  const target = bp.report.value?.profile;
  router.push(target ? `/${target}/chat` : `/${props.profile}/settings/blueprints`);
}

const skippable = computed(() => !!currentStep.value);

onMounted(tryUploadStashed);
</script>

<template>
  <div class="bpw-page">
    <div class="bpw-container">
      <div class="bpw-header">
        <h1 class="bpw-title">Import blueprint</h1>
        <p v-if="manifest" class="bpw-subtitle">
          {{ manifest.display_name }} — by {{ manifest.author || 'unknown' }}, made with Cremind {{ manifest.app_version }}
        </p>
      </div>

      <div v-if="uploading" class="bpw-center">
        <Icon icon="mdi:loading" class="spin" /> Uploading & validating…
      </div>
      <ElAlert v-else-if="uploadError" type="error" :closable="false" :title="uploadError" />

      <template v-else-if="bp.report.value">
        <ElCard shadow="never" class="bpw-card">
          <template #header><strong>Import complete</strong></template>
          <p>Imported into profile <strong>{{ bp.report.value.profile }}</strong>.</p>
          <div v-if="(bp.report.value.applied || []).length" class="bpw-report-group">
            <h4>Applied ({{ bp.report.value.applied.length }})</h4>
            <ul><li v-for="(a, i) in bp.report.value.applied" :key="i">{{ a }}</li></ul>
          </div>
          <div v-if="(bp.report.value.needs_attention || []).length" class="bpw-report-group warn">
            <h4>Needs attention</h4>
            <ul><li v-for="(n, i) in bp.report.value.needs_attention" :key="i">{{ n }}</li></ul>
          </div>
          <ElButton type="primary" @click="finish">Done</ElButton>
        </ElCard>
      </template>

      <!-- Review / confirm before applying anything to the current profile. -->
      <template v-else-if="bp.session.value && !reviewed">
        <ElCard shadow="never" class="bpw-card">
          <template #header><strong>Review blueprint</strong></template>
          <p class="bpw-hint" v-if="manifest">{{ manifest.description || 'No description provided.' }}</p>
          <p>
            This will apply the blueprint's design to profile
            <strong>{{ props.profile }}</strong>, changing any settings, tools, skills,
            LLM choice, and events it includes.
          </p>
          <ElAlert
            type="warning" :closable="false" show-icon
            title="Import into a profile you created for this purpose — matching settings in this profile will be overwritten."
            class="bpw-warn"
          />
          <p class="bpw-hint">Included: {{ Object.keys(manifest?.components || {}).join(', ') }}</p>
          <div class="bpw-actions">
            <ElButton type="primary" @click="reviewed = true">Begin import into "{{ props.profile }}"</ElButton>
            <ElButton text type="danger" @click="doAbort">Cancel</ElButton>
          </div>
        </ElCard>
      </template>

      <template v-else-if="bp.session.value">
        <ElSteps :active="currentIndex" finish-status="success" align-center class="bpw-steps">
          <ElStep v-for="s in plan" :key="s.key" :title="s.title" />
        </ElSteps>

        <ElAlert
          v-for="(w, i) in (bp.session.value.warnings || [])" :key="i"
          type="warning" :closable="false" :title="w.message" class="bpw-warn"
        />
        <ElAlert v-if="bp.error.value" type="error" :closable="false" :title="bp.error.value" class="bpw-warn" />

        <ElCard v-if="!allDone && currentStep" shadow="never" class="bpw-card">
          <template #header><strong>{{ currentStep.title }}</strong></template>

          <!-- settings (notify) -->
          <template v-if="currentStep.key === 'settings'">
            <p class="bpw-hint">These settings will be applied:</p>
            <table class="bpw-table">
              <thead><tr><th>Setting</th><th>Default</th><th>Blueprint</th></tr></thead>
              <tbody>
                <tr v-for="row in (currentStep.preview?.settings || [])" :key="row.key">
                  <td>{{ row.key }}</td><td>{{ row.target_default }}</td><td><strong>{{ row.blueprint_value }}</strong></td>
                </tr>
              </tbody>
            </table>
          </template>

          <!-- persona (notify) -->
          <template v-else-if="currentStep.key === 'persona'">
            <p class="bpw-hint">Agent name: <strong>{{ currentStep.preview?.agent_name || '-' }}</strong></p>
            <pre class="bpw-excerpt">{{ currentStep.preview?.persona_excerpt }}</pre>
          </template>

          <!-- llm -->
          <template v-else-if="currentStep.key === 'llm'">
            <p class="bpw-hint">Provide API key(s) for the blueprint's LLM provider(s), or skip and add them later.</p>
            <div v-for="req in reqsOf('llm')" :key="req.provider" class="bpw-provider">
              <div class="bpw-provider-name">{{ req.provider }}</div>
              <ElAlert v-if="req.sdk_missing" type="info" :closable="false" show-icon
                :title="`The ${req.provider} SDK is not installed here — install it in Settings → LLM after import.`" />
              <div v-for="field in (req.fields || [])" :key="field" class="bpw-field">
                <label>{{ field }}</label>
                <ElInput v-model="llmSecrets[`${req.provider}.${field}`]" type="password" show-password
                  :placeholder="`${req.provider} ${field}`" />
              </div>
            </div>
          </template>

          <!-- tools -->
          <template v-else-if="currentStep.key === 'tools'">
            <p class="bpw-hint">Some tools need secret values. Enter them or skip to set later.</p>
            <div v-for="req in reqsOf('tools')" :key="req.tool_id" class="bpw-provider">
              <div class="bpw-provider-name">{{ req.tool_id }}</div>
              <div v-for="v in (req.variables || [])" :key="v" class="bpw-field">
                <label>{{ v }}</label>
                <ElInput
                  :model-value="(toolSecrets[req.tool_id] || {})[v] || ''"
                  type="password" show-password :placeholder="v"
                  @update:model-value="(val) => { (toolSecrets[req.tool_id] ||= {})[v] = val }"
                />
              </div>
            </div>
          </template>

          <!-- skills -->
          <template v-else-if="currentStep.key === 'skills'">
            <p class="bpw-hint">Skills from this blueprint:</p>
            <div v-for="req in reqsOf('skills')" :key="req.slug" class="bpw-provider">
              <div class="bpw-provider-name">
                {{ req.name }}
                <span v-if="req.conflict === 'builtin'" class="bpw-badge">built-in — kept local</span>
                <span v-else-if="!req.bundled" class="bpw-badge">config only</span>
              </div>
              <div v-for="v in (req.secret_variables || [])" :key="v" class="bpw-field">
                <label>{{ v }}</label>
                <ElInput
                  :model-value="(skillSecrets[req.slug] || {})[v] || ''"
                  type="password" show-password :placeholder="v"
                  @update:model-value="(val) => { (skillSecrets[req.slug] ||= {})[v] = val }"
                />
              </div>
            </div>
          </template>

          <!-- events (notify + optional watcher paths) -->
          <template v-else-if="currentStep.key === 'events'">
            <p class="bpw-hint">These events will run after import:</p>
            <ul class="bpw-list">
              <li v-for="(s, i) in (currentStep.preview?.schedule || [])" :key="'s'+i">
                schedule: {{ s.title }}<template v-if="s.rrule"> ({{ s.rrule }})</template>
              </li>
              <li v-for="(e, i) in (currentStep.preview?.skill_event || [])" :key="'e'+i">
                skill event: {{ e.skill_slug }} / {{ e.event_type }}
              </li>
            </ul>
            <template v-if="reqsOf('events').length">
              <p class="bpw-hint">Confirm folder paths for file watchers on this machine:</p>
              <div v-for="req in reqsOf('events')" :key="req.name" class="bpw-field">
                <label>{{ req.name }} <span v-if="!req.exists" class="bpw-badge warn">path missing</span></label>
                <ElInput
                  :model-value="watcherPaths[req.name] ?? req.suggested_root_path ?? ''"
                  :placeholder="req.source_root_path"
                  @update:model-value="(val) => { watcherPaths[req.name] = val }"
                />
              </div>
            </template>
          </template>

          <!-- listeners -->
          <template v-else-if="currentStep.key === 'listeners'">
            <p class="bpw-hint">Register skill listener processes. Unchecked ones start on the next server restart.</p>
            <ElCheckbox
              v-for="req in reqsOf('listeners')" :key="req.skill_dir"
              :model-value="listenersConfirmed[req.skill_dir] ?? true"
              @update:model-value="(val) => { listenersConfirmed[req.skill_dir] = !!val }"
            >Start "{{ req.skill_dir }}" now</ElCheckbox>
          </template>

          <div class="bpw-actions">
            <ElButton type="primary" :loading="bp.busy.value" @click="submitStep">
              {{ currentStep.kind === 'notify' ? 'Continue' : 'Apply' }}
            </ElButton>
            <ElButton v-if="skippable" :disabled="bp.busy.value" @click="skipStep">Skip / set up later</ElButton>
          </div>
        </ElCard>

        <ElCard v-else-if="allDone" shadow="never" class="bpw-card">
          <template #header><strong>Finish import</strong></template>
          <p class="bpw-hint">All steps applied. Finalize to arm events and generate the report.</p>
          <ElButton type="primary" :loading="bp.busy.value" @click="doFinalize">Finalize import</ElButton>
        </ElCard>

        <div class="bpw-abort">
          <ElButton text type="danger" @click="doAbort">Abort import</ElButton>
        </div>
      </template>

      <div v-else class="bpw-center">
        <p>No import in progress.</p>
        <ElButton @click="router.push(`/${props.profile}/settings/blueprints`)">Back to Blueprints</ElButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bpw-page { width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color); padding: 24px; box-sizing: border-box; }
.bpw-container { max-width: 760px; margin: 0 auto; }
.bpw-header { margin-bottom: 20px; }
.bpw-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 4px 0; color: var(--text-primary); }
.bpw-subtitle { color: var(--text-secondary); font-size: 0.85rem; margin: 0; }
.bpw-steps { margin-bottom: 20px; }
.bpw-card { margin-bottom: 16px; }
.bpw-warn { margin-bottom: 10px; }
.bpw-hint { color: var(--text-secondary); font-size: 0.85rem; margin: 0 0 10px 0; }
.bpw-provider { border: 1px solid var(--border-color); border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; }
.bpw-provider-name { font-weight: 600; margin-bottom: 6px; }
.bpw-field { display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px; }
.bpw-field label { font-size: 0.8rem; color: var(--text-secondary); }
.bpw-badge { font-size: 0.7rem; background: var(--hover-bg); border-radius: 6px; padding: 1px 6px; margin-left: 6px; color: var(--text-secondary); }
.bpw-badge.warn { color: var(--el-color-warning); }
.bpw-table { width: 100%; font-size: 0.82rem; border-collapse: collapse; }
.bpw-table th, .bpw-table td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border-color); }
.bpw-excerpt { background: var(--hover-bg); border-radius: 8px; padding: 10px; font-size: 0.78rem; white-space: pre-wrap; max-height: 220px; overflow: auto; }
.bpw-list { font-size: 0.82rem; color: var(--text-secondary); margin: 0 0 10px 0; padding-left: 18px; }
.bpw-actions { display: flex; gap: 10px; margin-top: 12px; }
.bpw-abort { margin-top: 8px; text-align: center; }
.bpw-report-group { margin: 10px 0; }
.bpw-report-group h4 { margin: 0 0 4px 0; font-size: 0.9rem; }
.bpw-report-group.warn h4 { color: var(--el-color-warning); }
.bpw-report-group ul { margin: 0; padding-left: 18px; font-size: 0.82rem; color: var(--text-secondary); }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.bpw-center { text-align: center; color: var(--text-secondary); padding: 40px 0; }
</style>
