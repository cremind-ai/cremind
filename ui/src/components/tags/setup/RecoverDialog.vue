<script setup lang="ts">
/**
 * Recover on this computer (connect-setup.md §8.4): the gateway's old
 * computer is gone or replaced. POST /api/tags/recoveries opens a `recover`
 * setup session — the same Connect steps as Connect gateway (open, approve,
 * confirm the words) — and then Cremind Connect on this computer takes the
 * gateway over and re-secures every bridge and tag. The progress is shown per
 * device; one that is asleep or out of reach stays "Recovery pending" until
 * it answers.
 */
import { computed, ref, watch } from 'vue';
import { ElButton, ElDialog, ElResult, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { useConnectSession } from '../../../composables/useConnectSession';
import type { TagConnection } from '../../../services/tagsSetupApi';
import {
  connectionTitle, isOperationTerminal, operationProgressLabel, setupErrorMessage, type Pill,
} from '../../../utils/tagsSetupFormat';
import ConnectSessionSteps from './ConnectSessionSteps.vue';

const props = defineProps<{
  modelValue: boolean;
  connection: TagConnection | null;
  resumeSessionId?: string | null;
  resumeRecoveryId?: string | null;
}>();
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>();

const store = useTagsSetupStore();
const connect = useConnectSession();
const recoveryId = ref<string | null>(null);
const startError = ref('');

const recovery = computed(() => (recoveryId.value ? store.recoveries[recoveryId.value] ?? null : null));
const companionId = computed(() => props.connection?.id || recovery.value?.companion_id || connect.session.value?.companion_id || '');
const gatewayName = computed(() => (props.connection ? connectionTitle(props.connection) : 'the gateway'));
/** The device list replaces the Connect steps once Connect has taken over (or
 *  when resumed from the list). A recovery still waiting for Connect (its
 *  session expired or was closed) starts again from "plug in". */
const showDevices = computed(() => !!recovery.value && recovery.value.state !== 'waiting_for_connect'
  && (connect.step.value === 'done' || !connect.sessionId.value));

const DEVICE_STATE: Record<string, Pill> = {
  pending: { label: 'Waiting', type: 'info' },
  rekeyed: { label: 'Done', type: 'success' },
  recovery_pending: { label: 'Recovery pending', type: 'warning' },
  failed: { label: 'Failed', type: 'danger' },
};
const devicePill = (state: string): Pill => DEVICE_STATE[state] ?? { label: state.replace(/_/g, ' '), type: 'info' };

const summary = computed(() => {
  const r = recovery.value;
  if (!r) return { icon: 'info' as const, title: 'Starting…', text: '' };
  if (r.state === 'succeeded') return { icon: 'success' as const, title: 'Recovered on this computer', text: `${gatewayName.value} and its devices now work through this computer.` };
  if (r.state === 'pending_device') {
    return { icon: 'info' as const, title: 'Almost done', text: 'Devices that are asleep or out of reach finish on their own the next time they answer.' };
  }
  if (r.state === 'failed') return { icon: 'error' as const, title: 'Recovery did not finish', text: setupErrorMessage(r.error?.code, { role: 'gateway', fallback: r.error?.message }) };
  if (r.state === 'cancelled') return { icon: 'warning' as const, title: 'Recovery cancelled', text: 'Nothing was changed.' };
  return { icon: 'info' as const, title: 'Moving your devices to this computer…', text: operationProgressLabel(r) };
});

function followRecovery(id: string) {
  recoveryId.value = id;
  store.follow('recovery', id);
}

async function create() {
  const res = await store.startRecovery(companionId.value);
  followRecovery(res.recovery.id);
  return { session: res.session, launchUrl: res.launchUrl };
}

watch(() => props.modelValue, (open) => {
  if (!open) return;
  startError.value = '';
  recoveryId.value = null;
  if (props.resumeRecoveryId) followRecovery(props.resumeRecoveryId);
  if (props.resumeSessionId) connect.attach(props.resumeSessionId);
});

// Once Connect redeemed the session, its operation is the recovery to follow.
watch(() => connect.session.value?.operation_id, (id) => {
  if (id && id !== recoveryId.value) followRecovery(id);
});

async function start() {
  if (!companionId.value) {
    startError.value = 'Start again from the gateway\'s "Recover on this computer" action.';
    return;
  }
  await connect.begin(create);
}

function close() {
  connect.detach();
  // A recovery still running goes on server-side; the list shows it.
  if (recoveryId.value) store.unfollow('recovery', recoveryId.value);
  recoveryId.value = null;
  emit('update:modelValue', false);
}

async function requestClose() {
  if (connect.cancellable.value && !(await connect.cancel())) return;
  close();
}

function beforeClose(_done: () => void) {
  void requestClose();
}

async function cancelSetup() {
  if (await connect.cancel()) close();
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="`Recover ${gatewayName} on this computer`"
    width="min(560px, calc(100vw - 24px))"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
  >
    <template v-if="showDevices && recovery">
      <ElResult :icon="summary.icon" :title="summary.title" :sub-title="summary.text" />
      <ul v-if="recovery.devices.length" class="devices" aria-label="Devices">
        <li v-for="(d, i) in recovery.devices" :key="d.id ?? `device-${i}`" class="device">
          <Icon :icon="d.kind === 'gateway' ? 'mdi:usb-port' : d.kind === 'bridge' ? 'mdi:access-point' : 'mdi:tablet-dashboard'" aria-hidden="true" />
          <span class="device-name">{{ d.name || d.kind }}</span>
          <ElTag :type="devicePill(d.state).type" size="small" effect="plain">{{ devicePill(d.state).label }}</ElTag>
        </li>
      </ul>
    </template>

    <ConnectSessionSteps
      v-else
      :session="connect.session.value"
      :step="connect.step.value"
      operation="recover"
      :waited-long="connect.waitedLong.value"
      :has-link="connect.hasLink.value"
      :busy="connect.busy.value"
      :action-error="connect.actionError.value"
      :launch-error="connect.launchError.value"
      @reopen="connect.reopen(create)"
      @confirm="connect.confirm()"
      @cancel="cancelSetup"
      @retry="start"
    >
      <template #plug>
        <div class="intro">
          <Icon icon="mdi:laptop" class="intro-icon" aria-hidden="true" />
          <div>
            <h3 class="intro-title">Plug {{ gatewayName }} into this computer</h3>
            <p class="intro-text">
              Use this when the computer the gateway used to be connected to is gone or has been
              replaced. Cremind Connect on this computer takes the gateway over, and your bridges and
              tags move with it.
            </p>
            <p class="intro-text muted">The old computer can no longer reach them afterwards.</p>
          </div>
        </div>
        <p v-if="startError || connect.actionError.value" class="error" role="alert">{{ startError || connect.actionError.value }}</p>
      </template>
      <template #done>
        <ElResult icon="success" title="Cremind Connect took over the gateway" sub-title="Moving your devices to this computer…" />
      </template>
    </ConnectSessionSteps>

    <template #footer>
      <template v-if="!showDevices && connect.step.value === 'plug'">
        <ElButton @click="close">Cancel</ElButton>
        <ElButton type="primary" :loading="connect.busy.value === 'start'" @click="start">It is plugged in, continue</ElButton>
      </template>
      <template v-else-if="!showDevices && connect.cancellable.value && connect.step.value !== 'confirm'">
        <ElButton :loading="connect.busy.value === 'cancel'" @click="cancelSetup">Cancel</ElButton>
      </template>
      <template v-else>
        <ElButton @click="close">{{ recovery && !isOperationTerminal(recovery.state) && recovery.state !== 'pending_device' ? 'Close (it continues)' : 'Close' }}</ElButton>
      </template>
    </template>
  </ElDialog>
</template>

<style scoped>
.intro { display: flex; gap: 14px; align-items: flex-start; }
.intro-icon { font-size: 36px; color: var(--primary-color); flex-shrink: 0; }
.intro-title { margin: 0 0 6px; font-size: 1.02rem; font-weight: 600; color: var(--text-primary); }
.intro-text { margin: 0 0 6px; font-size: 0.88rem; line-height: 1.55; color: var(--text-primary); }
.intro-text.muted { color: var(--text-secondary); }
.error { margin: 8px 0 0; font-size: 0.85rem; color: var(--el-color-danger); }
.devices { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.device {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px;
  border: 1px solid var(--border-color); font-size: 0.88rem; color: var(--text-primary);
}
.device-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
