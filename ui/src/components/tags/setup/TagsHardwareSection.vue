<script setup lang="ts">
/**
 * Settings → Tags, "Your hardware" (simple setup, cremind-tag
 * docs/connect-setup.md): this computer, the three ways in — Connect gateway,
 * Add bridge (once a gateway is connected), Add tag (once a bridge is ready) —
 * setups still running (Continue / Cancel, also after a page refresh), and the
 * profile's gateways, bridges and tags with what can be done to each.
 *
 * Plain words only: no serial ports, mesh addresses, epochs or credential
 * ids; a device id appears only as a short suffix when a device has no name.
 * The page polls through the tagsSetup store (TagsSettings runs the poll).
 */
import { computed, ref } from 'vue';
import { ElButton, ElCard, ElMessage, ElMessageBox } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { useTagsStore } from '../../../stores/tags';
import { TagsApiError } from '../../../services/tagsApi';
import type { SetupDevice, TagConnection } from '../../../services/tagsSetupApi';
import type { PairingResume } from '../../../composables/useDevicePairing';
import {
  batteryLevel, capacityFull, capacityLabel, connectionStatusPill, connectionTitle, deliveryLabel,
  deviceStatePill, isOperationTerminal, isWaitingForWake, isoToMs, setupDeviceTitle,
  setupErrorMessage, type PendingSetup, type Pill, type RowAction,
} from '../../../utils/tagsSetupFormat';
import { formatRelativeTime } from '../../../utils/relativeTime';
import TagPreviewImage from '../TagPreviewImage.vue';
import ThisComputerCard from './ThisComputerCard.vue';
import HardwareDeviceRow from './HardwareDeviceRow.vue';
import ConnectGatewayDialog from './ConnectGatewayDialog.vue';
import AddBridgeDialog from './AddBridgeDialog.vue';
import AddTagDialog from './AddTagDialog.vue';
import RemoveDeviceDialog from './RemoveDeviceDialog.vue';
import RecoverDialog from './RecoverDialog.vue';
import ReconnectGatewayDialog from './ReconnectGatewayDialog.vue';

const props = defineProps<{ profile: string; now: number }>();

const setup = useTagsSetupStore();
const tagsStore = useTagsStore();

const connectOpen = ref(false);
const connectResume = ref<string | null>(null);
const bridgeOpen = ref(false);
const bridgeResume = ref<PairingResume | null>(null);
const tagOpen = ref(false);
const tagResume = ref<PairingResume | null>(null);
const removeOpen = ref(false);
const removeDevice = ref<SetupDevice | null>(null);
const removeConnection = ref<TagConnection | null>(null);
const recoverOpen = ref(false);
const recoverConnection = ref<TagConnection | null>(null);
const recoverSession = ref<string | null>(null);
const recoverRecovery = ref<string | null>(null);
const reconnectOpen = ref(false);
const reconnectConnection = ref<TagConnection | null>(null);
/** `<device id>:<action>` while it runs. */
const busy = ref('');

const readiness = computed(() => setup.readiness);
const connections = computed(() => setup.connections);
const multiGateway = computed(() => connections.value.length > 1);
/** The one next step, highlighted: connect, then a bridge, then tags. */
const nextStep = computed(() => {
  if (!readiness.value.canAddBridge && !connections.value.length) return 'connect';
  if (readiness.value.canAddBridge && !setup.bridges.length) return 'bridge';
  return readiness.value.canAddTag ? 'tag' : '';
});

// ── dialogs ──

function openConnect(resumeId: string | null = null) {
  connectResume.value = resumeId;
  connectOpen.value = true;
}
function openAddBridge(resume: PairingResume | null = null) {
  bridgeResume.value = resume;
  bridgeOpen.value = true;
}
function openAddTag(resume: PairingResume | null = null) {
  tagResume.value = resume;
  tagOpen.value = true;
}
function openRecover(connection: TagConnection | null, resume: { sessionId?: string; recoveryId?: string } = {}) {
  recoverConnection.value = connection;
  recoverSession.value = resume.sessionId ?? null;
  recoverRecovery.value = resume.recoveryId ?? null;
  recoverOpen.value = true;
}
function openRemove(device: SetupDevice, connection: TagConnection) {
  removeDevice.value = device;
  removeConnection.value = connection;
  removeOpen.value = true;
}

// ── setups still running ──

async function continuePending(p: PendingSetup) {
  switch (p.kind) {
    case 'connect': openConnect(p.id); break;
    case 'recover': openRecover(null, { sessionId: p.id }); break;
    case 'recover_gateway': openRecover(null, { recoveryId: p.id }); break;
    case 'pair_bridge': openAddBridge({ kind: 'pairing', id: p.id }); break;
    case 'pair_tag': openAddTag({ kind: 'pairing', id: p.id }); break;
    case 'discovery':
      try {
        const d = await setup.loadDiscovery(p.id);
        if (d.role === 'bridge') openAddBridge({ kind: 'discovery', id: p.id });
        else openAddTag({ kind: 'discovery', id: p.id });
      } catch (e) {
        failed(e);
      }
      break;
  }
}

function pendingCancellable(p: PendingSetup): boolean {
  return p.kind === 'connect' || p.kind === 'recover' || p.kind === 'pair_bridge' || p.kind === 'pair_tag';
}

async function cancelPending(p: PendingSetup) {
  busy.value = `pending:${p.id}`;
  try {
    if (p.kind === 'connect' || p.kind === 'recover') await setup.cancelSession(p.id);
    else await setup.cancelPairing(p.id);
    await setup.loadConnections();
    ElMessage.success('Cancelled');
  } catch (e) {
    failed(e);
  } finally {
    busy.value = '';
  }
}

// ── rows ──

function lastContact(iso: string | null, prefix = 'Last contact'): string {
  const ms = isoToMs(iso);
  return ms ? `${prefix} ${formatRelativeTime(ms, props.now)}` : 'Not in contact yet';
}

/** A row's key: the device id, or its binding while it has no device record yet. */
const rowKey = (d: SetupDevice) => d.id ?? `binding:${d.binding_id}`;

/** Rename, Pause/Resume, Remove — nothing once a removal is under way, or
 *  while the device has no record to act on yet. */
function pauseActions(d: SetupDevice, paused: boolean): RowAction[] {
  if (d.state === 'removal_pending' || !d.id) return [];
  return [
    { key: 'rename', label: 'Rename', icon: 'mdi:pencil-outline' },
    paused
      ? { key: 'resume', label: 'Resume', icon: 'mdi:play-circle-outline' }
      : { key: 'pause', label: 'Pause', icon: 'mdi:pause-circle-outline' },
    { key: 'remove', label: 'Remove', icon: 'mdi:delete-outline', danger: true },
  ];
}

function gatewayRow(c: TagConnection) {
  const bridges = c.bridges.length;
  const tags = c.tags.length;
  const meta = [
    c.computer?.name ? `On ${c.computer.name}` : '',
    `${bridges === 1 ? '1 bridge' : `${bridges} bridges`} · ${tags === 1 ? '1 tag' : `${tags} tags`}`,
    c.status === 'offline' ? lastContact(c.last_seen_at, 'Last seen') : '',
  ];
  let note: { tone: 'warning' | 'danger' | 'info'; text: string } | null = null;
  if (c.status === 'offline') note = { tone: 'warning', text: 'Updates wait until the gateway is plugged in and Cremind Connect runs on its computer.' };
  else if (c.status === 'recovery_pending') note = { tone: 'info', text: 'Some devices are still moving to this gateway\'s new computer. They finish when they wake up.' };
  else if (c.status === 'removal_pending') note = { tone: 'info', text: 'Removal finishes as soon as the gateway can be reached.' };
  else if (c.status === 'setting_up') note = { tone: 'info', text: 'Finishing setup…' };
  const paused = c.paused || c.gateway.paused;
  const actions: RowAction[] = c.status === 'removal_pending' ? [] : [
    ...(c.status === 'offline' ? [
      { key: 'reconnect', label: 'Reconnect', icon: 'mdi:connection', inline: true, primary: true },
      { key: 'recover', label: 'Recover on this computer', icon: 'mdi:laptop' },
    ] : []),
    ...pauseActions(c.gateway, paused),
  ];
  return { icon: 'mdi:usb-port', title: connectionTitle(c), pill: connectionStatusPill(c), meta, note, actions };
}

function bridgeRow(b: SetupDevice, c: TagConnection) {
  const meta = [
    capacityFull(b.capacity) ? `Full: ${capacityLabel(b.capacity)}` : capacityLabel(b.capacity),
    multiGateway.value ? `Via ${connectionTitle(c)}` : '',
    b.state === 'offline' ? lastContact(b.last_contact_at) : '',
  ];
  const note = b.fontpack_ok === false
    ? { tone: 'warning' as const, text: 'This bridge needs a font update: connect it to this computer with USB.' }
    : b.state === 'removal_pending'
      ? { tone: 'info' as const, text: 'Removal finishes as soon as the bridge can be reached.' }
      : null;
  return { icon: 'mdi:access-point', title: setupDeviceTitle(b), pill: deviceStatePill(b), meta, note, actions: pauseActions(b, b.paused) };
}

/** An operation still running on this device (e.g. its removal waiting for it to wake). */
function activityPill(d: SetupDevice): Pill | null {
  if (!d.id) return null;
  const op = setup.activeOperations.find((o) => o.device?.id === d.id && !isOperationTerminal(o.state));
  if (!op || !isWaitingForWake(op)) return null;
  return { label: 'Waiting for the tag to wake', type: 'info' };
}

function tagRow(t: SetupDevice) {
  const battery = batteryLevel(t.battery_mv);
  const leaving = t.state === 'removal_pending';
  const meta = leaving
    ? [lastContact(t.last_contact_at)]
    : [battery?.label ?? '', lastContact(t.last_contact_at), deliveryLabel(t.delivery, t.paused)];
  const ready = t.state === 'ready' && !t.paused;
  const note = leaving
    ? { tone: 'info' as const, text: 'Removal finishes the next time the tag wakes.' }
    : battery?.low
      ? { tone: 'warning' as const, text: 'The battery is low. Replace it soon.' }
      : t.delivery?.status === 'failed'
        ? { tone: 'warning' as const, text: 'The last update did not arrive. Keep the tag near its bridge; it is tried again.' }
        : null;
  const actions: RowAction[] = leaving || !t.id ? [] : [
    {
      key: 'test', label: 'Send test', icon: 'mdi:send-outline', inline: true, disabled: !ready,
      reason: t.paused ? 'Resume the tag first' : 'The tag is not ready yet',
    },
    ...pauseActions(t, t.paused),
  ];
  return { icon: 'mdi:tablet-dashboard', title: setupDeviceTitle(t), pill: deviceStatePill(t), extraPill: activityPill(t), meta, note, actions };
}

/** The stored screen preview, when the tags overview knows this tag. */
function preview(t: SetupDevice) {
  const known = tagsStore.devices.find((d) => d.id === t.id);
  return {
    revision: known?.previews?.displayed ?? (t.delivery?.displayed_revision || null),
    width: known?.width ?? null,
    height: known?.height ?? null,
  };
}

// ── actions ──

function failed(e: unknown, role?: 'gateway' | 'bridge' | 'tag') {
  ElMessage.error(e instanceof TagsApiError
    ? setupErrorMessage(e.code, { role, fallback: e.message })
    : 'Cremind could not be reached. Try again.');
}

async function rename(d: SetupDevice & { id: string }, current: string) {
  let value: string;
  try {
    const res = await ElMessageBox.prompt(`A name for this ${d.kind}, as you will see it in Cremind.`, `Rename ${d.kind}`, {
      inputValue: current,
      confirmButtonText: 'Rename',
      cancelButtonText: 'Cancel',
      inputValidator: (v: string) => {
        const n = (v ?? '').trim().length;
        return (n >= 1 && n <= 128) || 'Enter 1 to 128 characters';
      },
    });
    value = (res as { value: string }).value ?? '';
  } catch {
    return;
  }
  busy.value = `${d.id}:rename`;
  try {
    await setup.rename(d.id, value);
    ElMessage.success('Renamed');
  } catch (e) {
    failed(e, d.kind);
  } finally {
    busy.value = '';
  }
}

async function setPaused(d: SetupDevice & { id: string }, paused: boolean, title: string) {
  if (paused && d.kind === 'gateway') {
    try {
      await ElMessageBox.confirm(
        `Pause ${title}? Nothing goes to its bridges and tags until you resume it. Everything stays set up.`,
        'Pause gateway',
        { confirmButtonText: 'Pause', cancelButtonText: 'Cancel', type: 'warning' },
      );
    } catch {
      return;
    }
  }
  busy.value = `${d.id}:${paused ? 'pause' : 'resume'}`;
  try {
    await setup.setPaused(d.id, paused);
    ElMessage.success(paused ? `${title} is paused` : `${title} is back on`);
  } catch (e) {
    failed(e, d.kind);
  } finally {
    busy.value = '';
  }
}

async function sendTest(d: SetupDevice & { id: string }) {
  busy.value = `${d.id}:test`;
  try {
    await setup.sendTest(d.id);
    ElMessage.success('Test card on its way. It shows at the tag\'s next check-in, within about 30 seconds.');
  } catch (e) {
    failed(e, 'tag');
  } finally {
    busy.value = '';
  }
}

function onAction(key: string, d: SetupDevice, c: TagConnection) {
  // Reconnect and Recover act on the connection; the rest need the device's record.
  if (key === 'reconnect') {
    reconnectConnection.value = c;
    reconnectOpen.value = true;
    return;
  }
  if (key === 'recover') {
    openRecover(c);
    return;
  }
  const id = d.id;
  if (!id) return;
  const dev = { ...d, id };
  const title = d.kind === 'gateway' ? connectionTitle(c) : setupDeviceTitle(d);
  switch (key) {
    case 'rename': void rename(dev, d.kind === 'gateway' ? connectionTitle(c) : (d.name || '')); break;
    case 'pause': void setPaused(dev, true, title); break;
    case 'resume': void setPaused(dev, false, title); break;
    case 'test': void sendTest(dev); break;
    case 'remove': openRemove(d, c); break;
  }
}

const busyFor = (d: SetupDevice) => (d.id && busy.value.startsWith(`${d.id}:`) ? busy.value.slice(d.id.length + 1) : '');
</script>

<template>
  <ElCard shadow="never" class="section-card hardware">
    <template #header>
      <div>
        <span class="section-title">Your hardware</span>
        <p class="section-sub">
          The gateway plugs into a computer, bridges carry updates around your home, and tags show
          them.
        </p>
      </div>
    </template>

    <ThisComputerCard :profile="profile" :now="now" />

    <div v-if="setup.pending.length" class="pending" role="status" aria-label="Setups in progress">
      <div v-for="p in setup.pending" :key="`${p.kind}:${p.id}`" class="pending-row">
        <Icon icon="mdi:progress-clock" class="pending-icon" aria-hidden="true" />
        <div class="pending-text">
          <strong>{{ p.title }}</strong>
          <span>{{ p.detail }}</span>
        </div>
        <div class="pending-actions">
          <ElButton size="small" type="primary" @click="continuePending(p)">Continue</ElButton>
          <ElButton
            v-if="pendingCancellable(p)" size="small" :loading="busy === `pending:${p.id}`"
            @click="cancelPending(p)"
          >Cancel</ElButton>
        </div>
      </div>
    </div>

    <div class="primary-actions">
      <div class="primary-action">
        <ElButton :type="nextStep === 'connect' ? 'primary' : undefined" @click="openConnect()">
          <Icon icon="mdi:usb-port" class="btn-icon" aria-hidden="true" /> Connect gateway
        </ElButton>
      </div>
      <div class="primary-action">
        <ElButton
          :type="nextStep === 'bridge' ? 'primary' : undefined"
          :disabled="!readiness.canAddBridge"
          :aria-describedby="readiness.canAddBridge ? undefined : 'add-bridge-reason'"
          @click="openAddBridge()"
        >
          <Icon icon="mdi:access-point" class="btn-icon" aria-hidden="true" /> Add bridge
        </ElButton>
        <p v-if="!readiness.canAddBridge" id="add-bridge-reason" class="reason">{{ readiness.addBridgeReason }}</p>
      </div>
      <div class="primary-action">
        <ElButton
          :type="nextStep === 'tag' ? 'primary' : undefined"
          :disabled="!readiness.canAddTag"
          :aria-describedby="readiness.canAddTag ? undefined : 'add-tag-reason'"
          @click="openAddTag()"
        >
          <Icon icon="mdi:tablet-dashboard" class="btn-icon" aria-hidden="true" /> Add tag
        </ElButton>
        <p v-if="!readiness.canAddTag" id="add-tag-reason" class="reason">{{ readiness.addTagReason }}</p>
      </div>
    </div>

    <p v-if="setup.loadError" class="stale" role="status">Could not refresh: {{ setup.loadError }}</p>

    <template v-if="connections.length">
      <h3 class="list-title">Gateways <span class="count">{{ connections.length }}</span></h3>
      <ul class="hw-list" aria-label="Gateways">
        <HardwareDeviceRow
          v-for="c in connections"
          :key="c.id"
          v-bind="gatewayRow(c)"
          :busy="busyFor(c.gateway)"
          @action="(k: string) => onAction(k, c.gateway, c)"
        />
      </ul>

      <template v-if="setup.bridges.length">
        <h3 class="list-title">Bridges <span class="count">{{ setup.bridges.length }}</span></h3>
        <ul class="hw-list" aria-label="Bridges">
          <HardwareDeviceRow
            v-for="b in setup.bridges"
            :key="rowKey(b.device)"
            v-bind="bridgeRow(b.device, b.connection)"
            :busy="busyFor(b.device)"
            @action="(k: string) => onAction(k, b.device, b.connection)"
          />
        </ul>
      </template>

      <template v-if="setup.tags.length">
        <h3 class="list-title">Tags <span class="count">{{ setup.tags.length }}</span></h3>
        <ul class="hw-list" aria-label="Tags">
          <HardwareDeviceRow
            v-for="t in setup.tags"
            :key="rowKey(t.device)"
            v-bind="tagRow(t.device)"
            :busy="busyFor(t.device)"
            @action="(k: string) => onAction(k, t.device, t.connection)"
          >
            <template v-if="t.device.id" #lead>
              <div class="thumb">
                <TagPreviewImage
                  :device-id="t.device.id"
                  kind="displayed"
                  label="Screen"
                  hide-revision
                  :epoch="t.device.generation"
                  v-bind="preview(t.device)"
                />
              </div>
            </template>
          </HardwareDeviceRow>
        </ul>
      </template>
    </template>

    <div v-else-if="setup.loaded" class="empty">
      <Icon icon="mdi:usb-port" class="empty-icon" aria-hidden="true" />
      <p>
        No gateway yet. Plug your gateway into this computer, then choose
        <strong>Connect gateway</strong>.
      </p>
    </div>

    <ConnectGatewayDialog v-model="connectOpen" :resume-session-id="connectResume" @add-bridge="openAddBridge()" />
    <AddBridgeDialog v-model="bridgeOpen" :resume="bridgeResume" @add-tag="openAddTag()" />
    <AddTagDialog v-model="tagOpen" :resume="tagResume" />
    <RemoveDeviceDialog v-model="removeOpen" :device="removeDevice" :connection="removeConnection" />
    <RecoverDialog
      v-model="recoverOpen"
      :connection="recoverConnection"
      :resume-session-id="recoverSession"
      :resume-recovery-id="recoverRecovery"
    />
    <ReconnectGatewayDialog
      v-model="reconnectOpen"
      :connection="reconnectConnection"
      :profile="profile"
      @recover="(c: TagConnection) => openRecover(c)"
    />
  </ElCard>
</template>

<style scoped>
.section-card { margin-bottom: 16px; }
.section-title { font-weight: 600; color: var(--text-primary); }
.section-sub { margin: 4px 0 0; font-size: 0.8rem; color: var(--text-tertiary); max-width: 600px; line-height: 1.45; }
.btn-icon { margin-right: 6px; }
.pending { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
.pending-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 12px; border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--primary-color) 40%, transparent);
  background: color-mix(in srgb, var(--primary-color) 7%, var(--surface-color));
}
.pending-icon { font-size: 1.2rem; color: var(--primary-color); flex-shrink: 0; }
.pending-text { flex: 1; min-width: 200px; display: flex; flex-direction: column; font-size: 0.86rem; color: var(--text-primary); }
.pending-text span { color: var(--text-secondary); font-size: 0.82rem; }
.pending-actions { display: flex; gap: 6px; }
.pending-actions .el-button + .el-button { margin-left: 0; }
.primary-actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; align-items: flex-start; }
.primary-action { display: flex; flex-direction: column; gap: 4px; max-width: 260px; }
.reason { margin: 0; font-size: 0.76rem; color: var(--text-tertiary); line-height: 1.4; }
.stale { margin: 10px 0 0; font-size: 0.8rem; color: var(--el-color-danger); }
.list-title {
  display: flex; align-items: center; gap: 8px; margin: 20px 0 8px;
  font-size: 0.8rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-secondary);
}
.count {
  font-size: 0.72rem; font-weight: 600; padding: 0 7px; border-radius: 10px;
  background: var(--hover-bg); color: var(--text-secondary);
}
.hw-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.thumb { width: 96px; }
.thumb :deep(.tag-preview-caption) { display: none; }
.thumb :deep(.tag-preview-frame) { max-height: 80px; }
.thumb :deep(.tag-preview-empty) { padding: 6px; font-size: 0.7rem; }
.empty {
  display: flex; align-items: center; gap: 12px; margin-top: 16px; padding: 16px;
  border: 1px dashed var(--border-color); border-radius: 10px; color: var(--text-secondary); font-size: 0.88rem;
}
.empty p { margin: 0; line-height: 1.5; }
.empty-icon { font-size: 28px; color: var(--primary-color); flex-shrink: 0; }
</style>
