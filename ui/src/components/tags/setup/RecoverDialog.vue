<script setup lang="ts">
/**
 * Move a gateway to another computer: the computer it was connected through
 * is gone or replaced (or it was set up with the older Cremind Connect).
 * Choose a gateway computer, plug the gateway in there, and Cremind searches
 * for it; recovering it there revokes the old computer's access, moves the
 * connection, and re-secures every bridge and tag. The progress is shown per
 * device; one that is asleep or out of reach stays "Recovery pending" until
 * it answers.
 *
 * Resumed from the page: a move still running (`resumeOperationId`), or the
 * recovery of the devices (`resumeRecoveryId`).
 */
import { computed, ref, watch } from 'vue';
import { ElButton, ElDialog, ElRadio, ElRadioGroup, ElResult, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { useGatewayConnect } from '../../../composables/useGatewayConnect';
import type { TagConnection } from '../../../services/tagsSetupApi';
import {
  candidateView, connectionTitle, hostName, hostOpProgressLabel, hostStatePill, isOperationTerminal,
  operationProgressLabel, plugInstruction, problemText, setupErrorMessage, type Pill,
} from '../../../utils/tagsSetupFormat';

const props = defineProps<{
  modelValue: boolean;
  connection: TagConnection | null;
  resumeOperationId?: string | null;
  resumeRecoveryId?: string | null;
}>();
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>();

const store = useTagsSetupStore();
const flow = useGatewayConnect();
const picked = ref<string | undefined>(undefined);
const recoveryId = ref<string | null>(null);

const recovery = computed(() => (recoveryId.value ? store.recoveries[recoveryId.value] ?? null : null));
const gatewayName = computed(() => (props.connection ? connectionTitle(props.connection) : 'the gateway'));
const deviceId = computed(() => props.connection?.gateway?.device_id ?? null);
const host = computed(() => flow.host.value);
const step = computed(() => flow.step.value);
const plug = computed(() => plugInstruction(host.value, gatewayName.value));
/** Where it runs now: offered last, since moving is about another computer. */
const choices = computed(() => {
  const current = props.connection?.host_id ?? null;
  return [...store.hosts].sort((a, b) => Number(a.id === current) - Number(b.id === current));
});

/** Our gateway in what the search found (and what that means). */
const ours = computed(() => {
  const d = flow.decision.value;
  if (d?.kind !== 'found' || !deviceId.value) return null;
  return d.candidates.find((c) => c.device_id === deviceId.value) ?? null;
});
const searchOutcome = computed(() => {
  const d = flow.decision.value;
  if (!d || d.kind === 'searching') return null;
  const c = ours.value;
  if (c?.state === 'recovery_required') return { kind: 'recover' as const, candidate: c };
  if (c?.state === 'already_connected') {
    return { kind: 'note' as const, title: 'Already connected here', text: `${gatewayName.value} already works through ${hostName(host.value)}. Nothing needs to move.` };
  }
  if (c) {
    const view = candidateView(c);
    const text = view.problem ? problemText(view.problem, host.value) : null;
    return { kind: 'note' as const, title: text?.title ?? view.pill.label, text: text?.text ?? c.message };
  }
  if (d.kind === 'found') {
    return {
      kind: 'note' as const, title: 'Not this gateway',
      text: `The gateway plugged into ${hostName(host.value)} is not ${gatewayName.value}. Plug in the right one, then search again.`,
    };
  }
  if (d.kind === 'problem') return { kind: 'note' as const, ...problemText(d.problem, host.value) };
  if (d.kind === 'failed') {
    return { kind: 'note' as const, title: 'The search did not finish', text: setupErrorMessage(d.error?.code, { fallback: d.error?.message, host: host.value }) };
  }
  return { kind: 'note' as const, title: 'Search cancelled', text: 'Search again when the gateway is plugged in.' };
});

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
  if (r.state === 'succeeded') {
    return { icon: 'success' as const, title: 'Moved', text: `${gatewayName.value} and its devices now work through ${hostName(host.value)}.` };
  }
  if (r.state === 'pending_device') {
    return { icon: 'info' as const, title: 'Almost done', text: 'Devices that are asleep or out of reach finish on their own the next time they answer.' };
  }
  if (r.state === 'failed') return { icon: 'error' as const, title: 'The move did not finish', text: setupErrorMessage(r.error?.code, { role: 'gateway', fallback: r.error?.message }) };
  if (r.state === 'cancelled') return { icon: 'warning' as const, title: 'Cancelled', text: 'Nothing was changed.' };
  return { icon: 'info' as const, title: 'Moving your devices…', text: operationProgressLabel(r) };
});

function followRecovery(id: string) {
  recoveryId.value = id;
  store.follow('recovery', id);
}

watch(() => props.modelValue, (open) => {
  if (!open) return;
  recoveryId.value = null;
  flow.choose(null);
  if (props.resumeRecoveryId) {
    followRecovery(props.resumeRecoveryId);
    return;
  }
  if (props.resumeOperationId) {
    flow.attach(props.resumeOperationId);
    return;
  }
  const usable = choices.value.filter((h) => h.access.can_use);
  picked.value = usable[0]?.id;
  if (usable.length === 1) flow.choose(usable[0].id);
});

// Once the connection moved, its recovery is what to follow.
watch(() => flow.connection.value, (op) => {
  if (op?.state === 'succeeded' && op.recovery_id && op.recovery_id !== recoveryId.value) followRecovery(op.recovery_id);
});

function close() {
  flow.detach();
  // A recovery still running goes on server-side; the page shows it.
  if (recoveryId.value) store.unfollow('recovery', recoveryId.value);
  recoveryId.value = null;
  emit('update:modelValue', false);
}

function beforeClose(_done: () => void) {
  close();
}

async function recoverHere() {
  const outcome = searchOutcome.value;
  if (outcome?.kind === 'recover') await flow.connect(outcome.candidate.id, { recover: true });
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="`Move ${gatewayName} to another computer`"
    width="min(580px, calc(100vw - 24px))"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
  >
    <!-- The devices moving over -->
    <template v-if="recoveryId">
      <ElResult :icon="summary.icon" :title="summary.title" :sub-title="summary.text" />
      <ul v-if="recovery?.devices.length" class="devices" aria-label="Devices">
        <li v-for="(d, i) in recovery.devices" :key="d.id ?? `device-${i}`" class="device">
          <Icon :icon="d.kind === 'gateway' ? 'mdi:usb-port' : d.kind === 'bridge' ? 'mdi:access-point' : 'mdi:tablet-dashboard'" aria-hidden="true" />
          <span class="device-name">{{ d.name || d.kind }}</span>
          <ElTag :type="devicePill(d.state).type" size="small" effect="plain">{{ devicePill(d.state).label }}</ElTag>
        </li>
      </ul>
    </template>

    <div v-else-if="step === 'choose'" class="step">
      <p class="text">
        Use this when the computer {{ gatewayName }} was connected through is gone or has been replaced.
        Which computer will it be plugged into now?
      </p>
      <ElRadioGroup v-model="picked" class="host-choices" aria-label="Gateway computer">
        <ElRadio v-for="h in choices" :key="h.id" :value="h.id" :disabled="!h.access.can_use" class="host-choice" border>
          <span class="choice-title">{{ hostName(h) }}</span>
          <ElTag :type="hostStatePill(h).type" size="small" effect="plain">{{ hostStatePill(h).label }}</ElTag>
          <span v-if="h.id === connection?.host_id" class="choice-note">It is connected through this computer now.</span>
          <span v-else-if="!h.access.can_use" class="choice-note">{{ h.access.reason }}</span>
        </ElRadio>
      </ElRadioGroup>
    </div>

    <div v-else-if="step === 'plug' || step === 'searching' || step === 'results'" class="step">
      <div class="intro">
        <Icon icon="mdi:swap-horizontal" class="intro-icon" aria-hidden="true" />
        <div>
          <h3 class="intro-title">{{ plug.before }}<strong>{{ plug.name }}</strong>{{ plug.after }}</h3>
          <p class="intro-text">
            Cremind finds it there and moves it, with its bridges and tags. The old computer can no longer
            reach them afterwards.
          </p>
        </div>
      </div>
      <div v-if="step === 'plug' && flow.blocked.value" class="problem" role="alert">
        <Icon icon="mdi:alert-outline" class="problem-icon" aria-hidden="true" />
        <div><strong>{{ flow.blocked.value.title }}</strong><p>{{ flow.blocked.value.text }}</p></div>
      </div>
      <p v-if="step === 'searching'" class="text" aria-live="polite">
        <Icon icon="mdi:loading" class="spin" aria-hidden="true" />
        {{ flow.scan.value ? hostOpProgressLabel(flow.scan.value) : 'Looking at the USB ports…' }}
      </p>
      <template v-if="step === 'results' && searchOutcome">
        <p v-if="searchOutcome.kind === 'recover'" class="text ok" role="status">
          <Icon icon="mdi:check-circle" aria-hidden="true" /> Found {{ gatewayName }} on {{ hostName(host) }}.
        </p>
        <div v-else class="problem" role="alert">
          <Icon icon="mdi:alert-outline" class="problem-icon" aria-hidden="true" />
          <div><strong>{{ searchOutcome.title }}</strong><p>{{ searchOutcome.text }}</p></div>
        </div>
      </template>
      <p v-if="flow.actionError.value" class="error" role="alert">{{ flow.actionError.value }}</p>
    </div>

    <div v-else-if="step === 'connecting'" class="step" aria-live="polite">
      <p class="text"><Icon icon="mdi:loading" class="spin" aria-hidden="true" />
        {{ flow.connection.value ? hostOpProgressLabel(flow.connection.value) : 'Moving the gateway…' }}</p>
    </div>

    <div v-else-if="step === 'failed'" class="step">
      <ElResult
        icon="error"
        :title="flow.failure.value?.problem ? problemText(flow.failure.value.problem, host).title : 'The gateway was not moved'"
        :sub-title="flow.failure.value?.text ?? ''"
      />
    </div>

    <template #footer>
      <template v-if="recoveryId">
        <ElButton @click="close">{{ recovery && !isOperationTerminal(recovery.state) && recovery.state !== 'pending_device' ? 'Close (it continues)' : 'Close' }}</ElButton>
      </template>
      <template v-else-if="step === 'choose'">
        <ElButton @click="close">Cancel</ElButton>
        <ElButton type="primary" :disabled="!picked" @click="flow.choose(picked ?? null)">Continue</ElButton>
      </template>
      <template v-else-if="step === 'plug' || step === 'results'">
        <ElButton @click="close">Cancel</ElButton>
        <ElButton
          :type="step === 'plug' ? 'primary' : undefined" :disabled="!!flow.blocked.value"
          :loading="flow.busy.value === 'search'" @click="flow.search()"
        >
          {{ step === 'plug' ? 'Search for it' : 'Search again' }}
        </ElButton>
        <ElButton
          v-if="searchOutcome?.kind === 'recover'" type="primary" :loading="flow.busy.value === 'connect'"
          @click="recoverHere"
        >
          Move it here
        </ElButton>
      </template>
      <template v-else-if="step === 'failed'">
        <ElButton @click="close">Close</ElButton>
        <ElButton type="primary" @click="flow.again()">Try again</ElButton>
      </template>
      <template v-else>
        <ElButton @click="close">Close (it continues)</ElButton>
      </template>
    </template>
  </ElDialog>
</template>

<style scoped>
.step { display: flex; flex-direction: column; gap: 10px; }
.text { margin: 0; font-size: 0.88rem; line-height: 1.55; color: var(--text-primary); }
.text.ok { color: var(--el-color-success); display: flex; align-items: center; gap: 6px; }
.intro { display: flex; gap: 14px; align-items: flex-start; }
.intro-icon { font-size: 36px; color: var(--primary-color); flex-shrink: 0; }
.intro-title { margin: 0 0 6px; font-size: 1.02rem; font-weight: 500; color: var(--text-primary); line-height: 1.4; }
.intro-text { margin: 0; font-size: 0.88rem; line-height: 1.55; color: var(--text-secondary); }
.host-choices { display: flex; flex-direction: column; align-items: stretch; gap: 8px; }
.host-choice { height: auto; min-height: 40px; padding: 8px 12px; margin-right: 0; white-space: normal; }
.host-choice :deep(.el-radio__label) { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.choice-title { font-weight: 600; color: var(--text-primary); }
.choice-note { flex-basis: 100%; font-size: 0.8rem; color: var(--text-tertiary); }
.problem {
  display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--el-color-warning) 45%, transparent);
  background: color-mix(in srgb, var(--el-color-warning) 8%, var(--surface-color));
}
.problem-icon { font-size: 1.3rem; color: var(--el-color-warning); flex-shrink: 0; }
.problem strong { font-size: 0.9rem; color: var(--text-primary); }
.problem p { margin: 4px 0 0; font-size: 0.86rem; line-height: 1.5; color: var(--text-primary); }
.error { margin: 0; font-size: 0.85rem; color: var(--el-color-danger); }
.devices { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.device {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px;
  border: 1px solid var(--border-color); font-size: 0.88rem; color: var(--text-primary);
}
.device-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.spin { animation: rv-spin 1s linear infinite; vertical-align: -2px; }
@keyframes rv-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
