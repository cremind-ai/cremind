<script setup lang="ts">
/**
 * Admin: every gateway, bridge and tag the companions report, grouped by
 * kind. Tags carry the ownership actions (claim / change owner, assign a
 * bridge, release); bridges the mesh maintenance commands (configure,
 * identify, install the font pack, remove); every device can be renamed or
 * forgotten. Everything here acts at once and queues work on the companion.
 */
import { computed, ref } from 'vue';
import {
  ElButton, ElCard, ElDialog, ElDropdown, ElDropdownItem, ElDropdownMenu, ElMessage, ElMessageBox,
  ElOption, ElSelect, ElTable, ElTableColumn, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../stores/tags';
import { TagsApiError, type TagCompanion, type TagDevice } from '../../services/tagsApi';
import TagClaimDialog from './TagClaimDialog.vue';
import { formatRelativeTime } from '../../utils/relativeTime';
import { formatTimestamp } from '../../utils/usageFormat';
import {
  BATTERY_LOW_MV, commandLabel, deviceStatusPill, deviceTitle, formatBattery, panelLabel,
} from '../../utils/tagsFormat';

const props = defineProps<{
  devices: TagDevice[];
  companions: TagCompanion[];
  profiles: string[];
  now: number;
}>();
const emit = defineEmits<{ (e: 'changed'): void }>();

const store = useTagsStore();
const busy = ref<string | null>(null);
const claimOpen = ref(false);
const claimTag = ref<TagDevice | null>(null);
const assignOpen = ref(false);
const assignTag = ref<TagDevice | null>(null);
const assignBridge = ref('');
const assigning = ref(false);

const gateways = computed(() => props.devices.filter((d) => d.kind === 'gateway'));
const bridges = computed(() => props.devices.filter((d) => d.kind === 'bridge'));
const tags = computed(() => props.devices.filter((d) => d.kind === 'tag'));
const multiCompanion = computed(() => props.companions.length > 1);

const byId = computed(() => new Map(props.devices.map((d) => [d.id, d])));
const companionName = (id: string) => props.companions.find((c) => c.id === id)?.name || id.slice(0, 8);
const bridgeName = (id: string | null) => {
  if (!id) return '';
  const b = byId.value.get(id);
  return b ? deviceTitle(b) : 'unknown bridge';
};
const bridgesOf = (companionId: string) => bridges.value.filter((b) => b.companion_id === companionId);
const tagsOn = (bridgeId: string) => tags.value.filter((t) => t.bridge_device_id === bridgeId).length;

// ── tags ──

function openClaim(tag: TagDevice) {
  claimTag.value = tag;
  claimOpen.value = true;
}

function openAssign(tag: TagDevice) {
  assignTag.value = tag;
  assignBridge.value = tag.bridge_device_id ?? '';
  assignOpen.value = true;
}

async function assign() {
  const tag = assignTag.value;
  if (!tag || !assignBridge.value) return;
  assigning.value = true;
  try {
    await store.assign(tag.id, assignBridge.value);
    ElMessage.success(`${deviceTitle(tag)} moves to ${bridgeName(assignBridge.value)}`);
    assignOpen.value = false;
    emit('changed');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to assign the bridge');
  } finally {
    assigning.value = false;
  }
}

async function release(tag: TagDevice) {
  try {
    await ElMessageBox.confirm(
      `Take ${deviceTitle(tag)} away from ${tag.owner_profile}? Its screen is cleared and nothing more is sent to it until it is claimed again.`,
      'Release tag',
      { type: 'warning', confirmButtonText: 'Release', cancelButtonText: 'Cancel', confirmButtonClass: 'el-button--danger' },
    );
  } catch { return; }
  await act(tag.id, async () => {
    await store.release(tag.id);
    ElMessage.success(`${deviceTitle(tag)} released`);
  });
}

// ── any device ──

async function act(id: string, fn: () => Promise<void>) {
  busy.value = id;
  try {
    await fn();
    emit('changed');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'The action failed');
  } finally {
    busy.value = null;
  }
}

async function rename(device: TagDevice) {
  let value = '';
  try {
    const res = await ElMessageBox.prompt(`A name for this ${device.kind}.`, `Rename ${device.kind}`, {
      inputValue: device.name,
      inputPlaceholder: device.hw_id,
      confirmButtonText: 'Rename',
      cancelButtonText: 'Cancel',
      inputValidator: (v: string) => {
        const n = (v ?? '').trim().length;
        return (n >= 1 && n <= 128) || 'Enter 1 to 128 characters';
      },
    });
    value = ((res as { value: string }).value ?? '').trim();
  } catch { return; }
  await act(device.id, async () => {
    await store.renameHardware(device.id, value);
    ElMessage.success('Renamed');
  });
}

async function forget(device: TagDevice) {
  const extra = device.kind === 'bridge' && tagsOn(device.id)
    ? ` The ${tagsOn(device.id)} tag(s) on it lose their bridge.` : '';
  try {
    await ElMessageBox.confirm(
      `Forget ${deviceTitle(device)}? Cremind drops it from the inventory with its history; if the companion reports it again it comes back as a new device.${extra}`,
      `Forget ${device.kind}`,
      { type: 'warning', confirmButtonText: 'Forget', cancelButtonText: 'Cancel', confirmButtonClass: 'el-button--danger' },
    );
  } catch { return; }
  busy.value = device.id;
  try {
    await store.forget(device.id);
    ElMessage.success(`${deviceTitle(device)} forgotten`);
    emit('changed');
  } catch (e) {
    if (e instanceof TagsApiError && e.code === 'tag_owned') {
      ElMessage.warning('A profile still owns this tag. Release it first, then forget it.');
    } else {
      ElMessage.error(e instanceof Error ? e.message : 'Failed to forget');
    }
  } finally {
    busy.value = null;
  }
}

async function queue(device: TagDevice, kind: string) {
  let args: Record<string, string>;
  if (kind === 'refresh_tag') args = { tag_id: device.hw_id };
  else if (kind === 'install_fontpack') args = { bridge_hw_id: device.hw_id };
  else args = { hw_id: device.hw_id };
  if (kind === 'remove_bridge') {
    try {
      await ElMessageBox.confirm(
        `Remove ${deviceTitle(device)} from the mesh? It stops relaying to its ${tagsOn(device.id)} tag(s) until it is provisioned again.`,
        'Remove bridge',
        { type: 'warning', confirmButtonText: 'Remove', cancelButtonText: 'Cancel', confirmButtonClass: 'el-button--danger' },
      );
    } catch { return; }
  }
  if (kind === 'install_fontpack') {
    try {
      await ElMessageBox.confirm(
        'Installing the font pack is operator-assisted: connect the bridge\'s maintenance port (USB) to the companion\'s computer and keep it powered until the command finishes.',
        'Install font pack',
        { type: 'info', confirmButtonText: 'Queue install', cancelButtonText: 'Cancel' },
      );
    } catch { return; }
  }
  await act(device.id, async () => {
    await store.queueCommand(device.companion_id, kind, args);
    ElMessage.success(`${commandLabel(kind)} queued for ${deviceTitle(device)}`);
  });
}

function onTagCommand(tag: TagDevice, cmd: string) {
  if (cmd === 'assign') openAssign(tag);
  else if (cmd === 'release') void release(tag);
  else if (cmd === 'rename') void rename(tag);
  else if (cmd === 'forget') void forget(tag);
  else void queue(tag, cmd);
}

function onBridgeCommand(bridge: TagDevice, cmd: string) {
  if (cmd === 'rename') void rename(bridge);
  else if (cmd === 'forget') void forget(bridge);
  else void queue(bridge, cmd);
}

function onGatewayCommand(gateway: TagDevice, cmd: string) {
  if (cmd === 'rename') void rename(gateway);
  else if (cmd === 'forget') void forget(gateway);
}
</script>

<template>
  <ElCard shadow="never" class="section-card">
    <template #header>
      <div>
        <span class="section-title">Inventory</span>
        <p class="section-sub">
          What the companions report. Tags are shared hardware: claiming one decides which profile
          it shows cards for.
        </p>
      </div>
    </template>

    <h4 class="group-title">
      <Icon icon="mdi:tablet-dashboard" /> Tags <span class="count">{{ tags.length }}</span>
    </h4>
    <ElTable :data="tags" size="small" row-key="id" empty-text="No tags reported yet" class="inv-table">
      <ElTableColumn label="Tag" min-width="170">
        <template #default="{ row }">
          <div class="strong">{{ deviceTitle(row as TagDevice) }}</div>
          <div class="muted small">
            <span class="mono">{{ row.hw_id }}</span>
            <span v-if="panelLabel(row as TagDevice)"> · {{ panelLabel(row as TagDevice) }}</span>
            <span v-if="multiCompanion"> · {{ companionName(row.companion_id) }}</span>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Owner" min-width="110">
        <template #default="{ row }">
          <span v-if="row.owner_profile" class="owner">{{ row.owner_profile }}</span>
          <span v-else class="muted">unclaimed</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Bridge" min-width="110">
        <template #default="{ row }">
          <span v-if="row.bridge_device_id">{{ bridgeName(row.bridge_device_id) }}</span>
          <span v-else class="muted">—</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Status" width="130">
        <template #default="{ row }">
          <div class="pills">
            <ElTag :type="deviceStatusPill(row.status).type" size="small" effect="plain">{{ deviceStatusPill(row.status).label }}</ElTag>
            <ElTag v-if="row.clear_required" type="warning" size="small" effect="plain">clearing</ElTag>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Battery" width="84">
        <template #default="{ row }">
          <span :class="{ danger: row.battery_mv != null && row.battery_mv < BATTERY_LOW_MV }">{{ formatBattery(row.battery_mv) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="FW" width="70">
        <template #default="{ row }">{{ row.fw || '—' }}</template>
      </ElTableColumn>
      <ElTableColumn label="Epoch" width="64" align="right">
        <template #default="{ row }"><span class="num">{{ row.epoch }}</span></template>
      </ElTableColumn>
      <ElTableColumn label="Contact" width="96">
        <template #default="{ row }">
          <span v-if="row.last_contact_at" :title="formatTimestamp(row.last_contact_at)">{{ formatRelativeTime(row.last_contact_at, now) }}</span>
          <span v-else class="muted">never</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="" width="170" align="right">
        <template #default="{ row }">
          <div class="row-actions">
            <ElButton size="small" :type="row.owner_profile ? 'default' : 'primary'" :loading="busy === row.id" @click="openClaim(row as TagDevice)">
              {{ row.owner_profile ? 'Owner…' : 'Claim' }}
            </ElButton>
            <ElDropdown trigger="click" @command="(c: string) => onTagCommand(row as TagDevice, c)">
              <ElButton size="small" :aria-label="`More actions for ${deviceTitle(row as TagDevice)}`">
                <Icon icon="mdi:dots-horizontal" />
              </ElButton>
              <template #dropdown>
                <ElDropdownMenu>
                  <ElDropdownItem command="assign" :disabled="bridgesOf(row.companion_id).length === 0">Assign bridge…</ElDropdownItem>
                  <ElDropdownItem command="release" :disabled="!row.owner_profile">Release</ElDropdownItem>
                  <ElDropdownItem command="refresh_tag" :disabled="!row.owner_profile">Refresh</ElDropdownItem>
                  <ElDropdownItem command="identify">Identify</ElDropdownItem>
                  <ElDropdownItem command="rename" divided>Rename…</ElDropdownItem>
                  <ElDropdownItem command="forget" :disabled="!!row.owner_profile">
                    {{ row.owner_profile ? 'Forget (release first)' : 'Forget' }}
                  </ElDropdownItem>
                </ElDropdownMenu>
              </template>
            </ElDropdown>
          </div>
        </template>
      </ElTableColumn>
    </ElTable>

    <h4 class="group-title">
      <Icon icon="mdi:access-point-network" /> Bridges <span class="count">{{ bridges.length }}</span>
    </h4>
    <ElTable :data="bridges" size="small" row-key="id" empty-text="No bridges reported yet" class="inv-table">
      <ElTableColumn label="Bridge" min-width="170">
        <template #default="{ row }">
          <div class="strong">{{ deviceTitle(row as TagDevice) }}</div>
          <div class="muted small">
            <span class="mono">{{ row.hw_id }}</span>
            <span v-if="multiCompanion"> · {{ companionName(row.companion_id) }}</span>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Status" width="100">
        <template #default="{ row }">
          <ElTag :type="deviceStatusPill(row.status).type" size="small" effect="plain">{{ deviceStatusPill(row.status).label }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Tags" width="64" align="right">
        <template #default="{ row }"><span class="num">{{ tagsOn(row.id) }}</span></template>
      </ElTableColumn>
      <ElTableColumn label="Mesh addr" width="90">
        <template #default="{ row }"><span class="num">{{ row.info?.addr ?? '—' }}</span></template>
      </ElTableColumn>
      <ElTableColumn label="Font pack" min-width="130">
        <template #default="{ row }">
          <span v-if="row.info?.fontpack_id" class="mono">{{ row.info.fontpack_id }}</span>
          <span v-else class="muted">none</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="FW" width="70">
        <template #default="{ row }">{{ row.fw || '—' }}</template>
      </ElTableColumn>
      <ElTableColumn label="" width="170" align="right">
        <template #default="{ row }">
          <div class="row-actions">
            <ElButton size="small" :loading="busy === row.id" @click="queue(row as TagDevice, 'identify')">Identify</ElButton>
            <ElDropdown trigger="click" @command="(c: string) => onBridgeCommand(row as TagDevice, c)">
              <ElButton size="small" :aria-label="`More actions for ${deviceTitle(row as TagDevice)}`">
                <Icon icon="mdi:dots-horizontal" />
              </ElButton>
              <template #dropdown>
                <ElDropdownMenu>
                  <ElDropdownItem command="configure_bridge">Configure</ElDropdownItem>
                  <ElDropdownItem command="install_fontpack">Install font pack…</ElDropdownItem>
                  <ElDropdownItem command="remove_bridge">Remove from mesh…</ElDropdownItem>
                  <ElDropdownItem command="rename" divided>Rename…</ElDropdownItem>
                  <ElDropdownItem command="forget">Forget</ElDropdownItem>
                </ElDropdownMenu>
              </template>
            </ElDropdown>
          </div>
        </template>
      </ElTableColumn>
    </ElTable>

    <h4 class="group-title">
      <Icon icon="mdi:router-wireless" /> Gateways <span class="count">{{ gateways.length }}</span>
    </h4>
    <ElTable :data="gateways" size="small" row-key="id" empty-text="No gateways reported yet" class="inv-table">
      <ElTableColumn label="Gateway" min-width="200">
        <template #default="{ row }">
          <div class="strong">{{ deviceTitle(row as TagDevice) }}</div>
          <div class="muted small"><span class="mono">{{ row.hw_id }}</span></div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Companion" min-width="120">
        <template #default="{ row }">{{ companionName(row.companion_id) }}</template>
      </ElTableColumn>
      <ElTableColumn label="Status" width="100">
        <template #default="{ row }">
          <ElTag :type="deviceStatusPill(row.status).type" size="small" effect="plain">{{ deviceStatusPill(row.status).label }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Port" width="100">
        <template #default="{ row }"><span class="mono">{{ row.info?.port ?? '—' }}</span></template>
      </ElTableColumn>
      <ElTableColumn label="FW" width="70">
        <template #default="{ row }">{{ row.fw || '—' }}</template>
      </ElTableColumn>
      <ElTableColumn label="" width="90" align="right">
        <template #default="{ row }">
          <ElDropdown trigger="click" @command="(c: string) => onGatewayCommand(row as TagDevice, c)">
            <ElButton size="small" :loading="busy === row.id" :aria-label="`More actions for ${deviceTitle(row as TagDevice)}`">
              <Icon icon="mdi:dots-horizontal" />
            </ElButton>
            <template #dropdown>
              <ElDropdownMenu>
                <ElDropdownItem command="rename">Rename…</ElDropdownItem>
                <ElDropdownItem command="forget">Forget</ElDropdownItem>
              </ElDropdownMenu>
            </template>
          </ElDropdown>
        </template>
      </ElTableColumn>
    </ElTable>

    <TagClaimDialog
      v-model="claimOpen"
      :tag="claimTag"
      :bridges="claimTag ? bridgesOf(claimTag.companion_id) : []"
      :profiles="profiles"
      @done="emit('changed')"
    />

    <ElDialog v-model="assignOpen" :title="assignTag ? `Assign ${deviceTitle(assignTag)} to a bridge` : 'Assign bridge'" width="440px" append-to-body>
      <label class="field-label">Bridge</label>
      <ElSelect v-model="assignBridge" placeholder="Pick the bridge" class="full" aria-label="Bridge">
        <ElOption
          v-for="b in assignTag ? bridgesOf(assignTag.companion_id) : []"
          :key="b.id" :label="deviceTitle(b)" :value="b.id"
        />
      </ElSelect>
      <p class="field-hint">
        The tag is re-keyed for the new bridge; cards already on their way to it move along.
      </p>
      <template #footer>
        <ElButton @click="assignOpen = false">Cancel</ElButton>
        <ElButton type="primary" :loading="assigning" :disabled="!assignBridge" @click="assign">Assign</ElButton>
      </template>
    </ElDialog>
  </ElCard>
</template>

<style scoped>
.section-card { margin-bottom: 16px; }
.section-title { font-weight: 600; color: var(--text-primary); }
.section-sub { margin: 4px 0 0; font-size: 0.8rem; color: var(--text-tertiary); max-width: 600px; line-height: 1.45; }
.group-title {
  display: flex; align-items: center; gap: 6px; margin: 18px 0 8px;
  font-size: 0.9rem; font-weight: 600; color: var(--text-primary);
}
.group-title:first-of-type { margin-top: 0; }
.group-title :deep(svg) { color: var(--primary-color); font-size: 1.05rem; }
.count {
  font-size: 0.72rem; font-weight: 600; min-width: 20px; height: 20px; padding: 0 6px;
  display: inline-grid; place-items: center; border-radius: 10px;
  background: var(--hover-bg); color: var(--text-secondary);
}
.inv-table { width: 100%; }
.strong { font-weight: 600; color: var(--text-primary); }
.owner { color: var(--text-primary); font-weight: 500; }
.muted { color: var(--text-tertiary); }
.small { font-size: 0.75rem; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.75rem; }
.num { font-variant-numeric: tabular-nums; }
.danger { color: var(--el-color-danger); }
.pills { display: flex; flex-wrap: wrap; gap: 4px; }
.row-actions { display: inline-flex; gap: 6px; align-items: center; }
.row-actions .el-button + .el-button { margin-left: 0; }
.field-label { display: block; margin-bottom: 6px; font-size: 0.82rem; font-weight: 600; color: var(--text-secondary); }
.field-hint { margin: 8px 0 0; font-size: 0.78rem; color: var(--text-tertiary); line-height: 1.45; }
.full { width: 100%; }
</style>
