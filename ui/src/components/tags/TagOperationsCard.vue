<script setup lang="ts">
/**
 * Admin: hardware operations on one companion — scan for unprovisioned
 * bridges (then provision one it found, or any UUID), collect diagnostics —
 * and the recent command queue with each command's status and result. The
 * page polls faster while any command is queued or running.
 */
import { computed, ref, watch } from 'vue';
import {
  ElButton, ElCard, ElInput, ElInputNumber, ElMessage, ElMessageBox, ElOption, ElSelect,
  ElTable, ElTableColumn, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../stores/tags';
import type { TagCommand, TagCompanion, TagDevice } from '../../services/tagsApi';
import { formatRelativeTime } from '../../utils/relativeTime';
import { formatTimestamp } from '../../utils/usageFormat';
import {
  commandLabel, commandStatusPill, deviceTitle, isCommandActive, scanResults,
} from '../../utils/tagsFormat';

const props = defineProps<{
  companions: TagCompanion[];
  commands: TagCommand[];
  devices: TagDevice[];
  now: number;
}>();
const emit = defineEmits<{ (e: 'changed'): void }>();

const store = useTagsStore();
const companionId = ref('');
const duration = ref(60);
const uuid = ref('');
const bridgeName = ref('');
const busy = ref<string | null>(null);

watch(() => props.companions, (list) => {
  if (list.some((c) => c.id === companionId.value)) return;
  companionId.value = (list.find((c) => c.online) ?? list[0])?.id ?? '';
}, { immediate: true });

const companion = computed(() => props.companions.find((c) => c.id === companionId.value) ?? null);
const companionName = (id: string) => props.companions.find((c) => c.id === id)?.name || id.slice(0, 8);

function latest(kind: string): TagCommand | null {
  return props.commands
    .filter((c) => c.kind === kind && c.companion_id === companionId.value)
    .sort((a, b) => b.created_at - a.created_at)[0] ?? null;
}

const lastScan = computed(() => latest('scan_unprovisioned'));
const lastDiagnostics = computed(() => latest('collect_diagnostics'));
const found = computed(() => (lastScan.value?.status === 'succeeded' ? scanResults(lastScan.value.result) : []));
const knownBridgeIds = computed(() => new Set(props.devices.filter((d) => d.kind === 'bridge').map((d) => d.hw_id)));

async function queue(kind: string, args?: Record<string, any>, done?: string) {
  if (!companionId.value) return;
  busy.value = kind;
  try {
    await store.queueCommand(companionId.value, kind, args);
    ElMessage.success(done || `${commandLabel(kind)} queued`);
    emit('changed');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : `Failed to queue ${commandLabel(kind)}`);
  } finally {
    busy.value = null;
  }
}

function scan() {
  void queue('scan_unprovisioned', { duration_s: duration.value },
    `Scanning for ${duration.value} s — results appear here when the companion reports back`);
}

async function provision(id: string, suggested = '') {
  let name = suggested;
  try {
    const res = await ElMessageBox.prompt(`Provision bridge ${id} into this companion's mesh. A name for it (optional):`, 'Provision bridge', {
      inputValue: suggested,
      inputPlaceholder: 'Kitchen bridge',
      confirmButtonText: 'Provision',
      cancelButtonText: 'Cancel',
      inputValidator: (v: string) => (v ?? '').trim().length <= 128 || 'At most 128 characters',
    });
    name = ((res as { value: string }).value ?? '').trim();
  } catch { return; }
  void queue('provision_bridge', { uuid: id, ...(name ? { name } : {}) }, 'Provisioning queued');
}

function provisionTyped() {
  const id = uuid.value.trim();
  if (!id) return;
  void queue('provision_bridge', { uuid: id, ...(bridgeName.value.trim() ? { name: bridgeName.value.trim() } : {}) },
    'Provisioning queued');
  uuid.value = '';
  bridgeName.value = '';
}

function argsSummary(c: TagCommand): string {
  const a = c.args || {};
  const target = a.tag_id ?? a.hw_id ?? a.bridge_hw_id ?? a.uuid;
  if (target) {
    const dev = props.devices.find((d) => d.hw_id === target);
    const label = dev ? deviceTitle(dev) : String(target);
    return a.epoch != null ? `${label} · epoch ${a.epoch}` : label;
  }
  if (a.duration_s) return `${a.duration_s} s`;
  return '';
}

function pretty(value: unknown): string {
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

const sortedCommands = computed(() => [...props.commands].sort((a, b) => b.created_at - a.created_at));
</script>

<template>
  <ElCard shadow="never" class="section-card">
    <template #header>
      <div>
        <span class="section-title">Operations</span>
        <p class="section-sub">
          Queued on a companion and run by it; results come back on its next poll.
        </p>
      </div>
    </template>

    <p v-if="companions.length === 0" class="muted">Register a companion first.</p>
    <template v-else>
      <div class="op-row">
        <label class="field-label" for="tag-op-companion">Companion</label>
        <ElSelect id="tag-op-companion" v-model="companionId" class="companion-select" aria-label="Companion">
          <ElOption v-for="c in companions" :key="c.id" :label="c.name" :value="c.id">
            <span>{{ c.name }}</span>
            <span class="opt-meta">{{ c.online ? 'online' : 'offline' }}</span>
          </ElOption>
        </ElSelect>
        <ElTag v-if="companion" :type="companion.online ? 'success' : 'info'" size="small" effect="plain">
          {{ companion.online ? 'online' : 'offline' }}
        </ElTag>
      </div>

      <div class="op-grid">
        <div class="op-box">
          <div class="op-title"><Icon icon="mdi:radar" /> Scan for new bridges</div>
          <p class="op-hint">Listens for unprovisioned bridges' beacons.</p>
          <div class="inline">
            <ElInputNumber v-model="duration" :min="5" :max="600" :step="15" controls-position="right" class="dur" aria-label="Scan duration in seconds" />
            <span class="muted small">seconds</span>
            <ElButton type="primary" :loading="busy === 'scan_unprovisioned'" :disabled="!!lastScan && isCommandActive(lastScan.status)" @click="scan">Scan</ElButton>
          </div>
          <div v-if="lastScan" class="op-result">
            <div class="result-head">
              <ElTag :type="commandStatusPill(lastScan.status).type" size="small" effect="plain">{{ commandStatusPill(lastScan.status).label }}</ElTag>
              <span class="muted small">{{ formatRelativeTime(lastScan.created_at, now) }}</span>
            </div>
            <p v-if="lastScan.status === 'failed'" class="error-text">{{ lastScan.error || 'The scan failed.' }}</p>
            <template v-else-if="lastScan.status === 'succeeded'">
              <p v-if="found.length === 0" class="muted small">No unprovisioned bridges found.</p>
              <ul v-else class="found-list">
                <li v-for="f in found" :key="f.uuid">
                  <code class="mono">{{ f.uuid }}</code>
                  <span v-if="f.name" class="muted small">{{ f.name }}</span>
                  <span v-if="f.rssi != null" class="muted small">{{ f.rssi }} dBm</span>
                  <ElTag v-if="knownBridgeIds.has(f.uuid)" type="info" size="small" effect="plain">known</ElTag>
                  <ElButton size="small" text type="primary" @click="provision(f.uuid, f.name || '')">Provision…</ElButton>
                </li>
              </ul>
            </template>
            <p v-else class="muted small">Waiting for the companion…</p>
          </div>
        </div>

        <div class="op-box">
          <div class="op-title"><Icon icon="mdi:plus-network-outline" /> Provision a bridge</div>
          <p class="op-hint">Adds a bridge to the mesh by its UUID (from a scan or its label).</p>
          <ElInput v-model="uuid" placeholder="Bridge UUID" maxlength="64" class="mb" aria-label="Bridge UUID" />
          <ElInput v-model="bridgeName" placeholder="Name (optional)" maxlength="128" class="mb" aria-label="Bridge name" />
          <ElButton :loading="busy === 'provision_bridge'" :disabled="!uuid.trim()" @click="provisionTyped">Provision</ElButton>
        </div>

        <div class="op-box">
          <div class="op-title"><Icon icon="mdi:stethoscope" /> Diagnostics</div>
          <p class="op-hint">Asks the companion for a snapshot of its gateway, mesh and queue.</p>
          <ElButton :loading="busy === 'collect_diagnostics'" :disabled="!!lastDiagnostics && isCommandActive(lastDiagnostics.status)" @click="queue('collect_diagnostics', {})">Collect diagnostics</ElButton>
          <div v-if="lastDiagnostics" class="op-result">
            <div class="result-head">
              <ElTag :type="commandStatusPill(lastDiagnostics.status).type" size="small" effect="plain">{{ commandStatusPill(lastDiagnostics.status).label }}</ElTag>
              <span class="muted small">{{ formatRelativeTime(lastDiagnostics.created_at, now) }}</span>
            </div>
            <p v-if="lastDiagnostics.error" class="error-text">{{ lastDiagnostics.error }}</p>
            <pre v-if="lastDiagnostics.result" class="json">{{ pretty(lastDiagnostics.result) }}</pre>
          </div>
        </div>
      </div>
    </template>

    <h4 class="group-title">Commands</h4>
    <ElTable :data="sortedCommands" size="small" row-key="id" empty-text="No commands yet" class="cmd-table">
      <ElTableColumn type="expand" width="32">
        <template #default="{ row }">
          <div class="cmd-detail">
            <div class="muted small">
              <code class="mono">{{ row.id }}</code> · requested by {{ row.requested_by || '—' }}
              · expires {{ formatTimestamp(row.expires_at) }}
            </div>
            <p v-if="row.error" class="error-text">{{ row.error }}</p>
            <pre class="json">{{ pretty({ args: row.args, result: row.result }) }}</pre>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Command" min-width="150">
        <template #default="{ row }">
          <div class="strong">{{ commandLabel(row.kind) }}</div>
          <div class="muted small">{{ argsSummary(row as TagCommand) }}</div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Companion" min-width="110">
        <template #default="{ row }">{{ companionName(row.companion_id) }}</template>
      </ElTableColumn>
      <ElTableColumn label="Status" width="110">
        <template #default="{ row }">
          <ElTag :type="commandStatusPill(row.status).type" size="small" effect="plain">{{ commandStatusPill(row.status).label }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Queued" width="100">
        <template #default="{ row }">
          <span :title="formatTimestamp(row.created_at)">{{ formatRelativeTime(row.created_at, now) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Finished" width="100">
        <template #default="{ row }">
          <span v-if="row.completed_at" :title="formatTimestamp(row.completed_at)">{{ formatRelativeTime(row.completed_at, now) }}</span>
          <span v-else class="muted">—</span>
        </template>
      </ElTableColumn>
    </ElTable>
  </ElCard>
</template>

<style scoped>
.section-card { margin-bottom: 16px; }
.section-title { font-weight: 600; color: var(--text-primary); }
.section-sub { margin: 4px 0 0; font-size: 0.8rem; color: var(--text-tertiary); }
.op-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
.field-label { font-size: 0.82rem; font-weight: 600; color: var(--text-secondary); }
.companion-select { width: 240px; }
.opt-meta { float: right; color: var(--text-tertiary); font-size: 0.8rem; }
.op-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.op-box {
  border: 1px solid var(--border-color); border-radius: 10px; padding: 12px;
  background: var(--surface-color); color: var(--text-primary); min-width: 0;
}
.op-title { display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: 0.9rem; }
.op-title :deep(svg) { color: var(--primary-color); }
.op-hint { margin: 4px 0 10px; font-size: 0.78rem; color: var(--text-tertiary); line-height: 1.4; }
.inline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.dur { width: 110px; }
.mb { margin-bottom: 8px; }
.op-result { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border-color); }
.result-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.found-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.found-list li { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.group-title { margin: 18px 0 8px; font-size: 0.9rem; font-weight: 600; color: var(--text-primary); }
.cmd-table { width: 100%; }
.cmd-detail { padding: 4px 12px 8px 40px; }
.json {
  margin: 6px 0 0; padding: 8px 10px; border-radius: 6px; max-height: 240px; overflow: auto;
  background: var(--hover-bg); color: var(--text-primary); border: 1px solid var(--border-color);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.75rem;
  white-space: pre-wrap; word-break: break-word;
}
.strong { font-weight: 600; color: var(--text-primary); }
.muted { color: var(--text-tertiary); }
.small { font-size: 0.75rem; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.75rem; }
.error-text { margin: 4px 0 0; color: var(--el-color-danger); font-size: 0.82rem; }
</style>
