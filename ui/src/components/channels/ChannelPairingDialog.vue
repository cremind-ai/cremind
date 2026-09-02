<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue';
import { ElButton, ElDialog, ElInput, ElMessage, ElMessageBox } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../../stores/settings';
import { useChannelsStore } from '../../stores/channels';
import {
  openChannelAuthStream,
  submitChannelAuthInput,
  type ChannelAuthEvent,
  type ChannelAuthStreamHandle,
} from '../../services/channelApi';

const props = defineProps<{
  modelValue: boolean;
  channelId: string;
  channelLabel: string;
}>();

const emit = defineEmits<{
  'update:modelValue': [open: boolean];
  'paired': [];
}>();

const settings = useSettingsStore();
const channelsStore = useChannelsStore();

type Status =
  | 'connecting'
  | 'qr'
  | 'code_required'
  | 'password_required'
  | 'ready'
  | 'disconnected'
  | 'unlinked'
  | 'error';

// How long the adapter gets to say anything at all before we assume the
// pairing flow is not coming. An adapter that is going to pair publishes its
// QR (or code prompt) within a second or two of starting, and the stream
// replays one already cached — so silence past this is the signature of a
// session the platform invalidated: the adapter keeps restoring it instead of
// pairing, and nothing is ever published. Generous enough to cover a cold
// sidecar install on a slow box.
const STUCK_AFTER_MS = 12_000;

const qrDataUrl = ref<string | null>(null);
const status = ref<Status>('connecting');
const errorMessage = ref<string | null>(null);
const stepError = ref<string | null>(null);
const phoneHint = ref<string | null>(null);
const codeInput = ref<string>('');
const passwordInput = ref<string>('');
const submitting = ref(false);
const stalled = ref(false);
const repairing = ref(false);
let handle: ChannelAuthStreamHandle | null = null;
let stuckTimer: ReturnType<typeof setTimeout> | null = null;

const titleSuffix = computed(() => {
  switch (status.value) {
    case 'qr': return 'Scan QR';
    case 'code_required': return 'Enter verification code';
    case 'password_required': return 'Enter 2FA password';
    case 'ready': return 'Paired';
    default: return '';
  }
});

// Offer the reset once the flow can no longer get anywhere on its own: the
// adapter went quiet, or it reported a dead/revoked session. Deliberately NOT
// offered while pairing is working (or already done) — resetting discards a
// good session and costs the user another scan.
const canReset = computed(() =>
  !repairing.value
  && (stalled.value
    || status.value === 'disconnected'
    || status.value === 'unlinked'
    || status.value === 'error'),
);

function armStuckTimer() {
  clearStuckTimer();
  stalled.value = false;
  stuckTimer = setTimeout(() => { stalled.value = true; }, STUCK_AFTER_MS);
}

function clearStuckTimer() {
  if (stuckTimer !== null) {
    clearTimeout(stuckTimer);
    stuckTimer = null;
  }
}

// The on-phone path to the QR scanner differs per platform.
const qrSteps = computed(() =>
  /zalo/i.test(props.channelLabel)
    ? 'Settings → scan the QR code'
    : 'Settings → Linked Devices → Link a Device',
);

function openStream() {
  closeStream();
  if (!props.channelId) return;
  status.value = 'connecting';
  errorMessage.value = null;
  stepError.value = null;
  phoneHint.value = null;
  qrDataUrl.value = null;
  codeInput.value = '';
  passwordInput.value = '';
  armStuckTimer();
  handle = openChannelAuthStream(
    settings.agentUrl,
    settings.authToken,
    props.channelId,
    handleEvent,
    (err) => {
      // Includes the "Adapter not running" 400 — the channel's receive loop
      // died, so there is nothing to pair with until it is restarted.
      clearStuckTimer();
      stalled.value = true;
      status.value = 'error';
      errorMessage.value = err instanceof Error ? err.message : String(err);
    },
  );
}

function handleEvent(event: ChannelAuthEvent) {
  // The adapter spoke, so the "went quiet" verdict no longer applies; each
  // branch below decides for itself whether this state still needs a reset.
  clearStuckTimer();
  stalled.value = false;
  if (event.kind === 'qr') {
    qrDataUrl.value = event.qr;
    status.value = 'qr';
    stepError.value = null;
  } else if (event.kind === 'code_required') {
    status.value = 'code_required';
    phoneHint.value = event.phone || null;
    stepError.value = event.error || null;
    submitting.value = false;
  } else if (event.kind === 'password_required') {
    status.value = 'password_required';
    stepError.value = event.error || null;
    submitting.value = false;
  } else if (event.kind === 'ready') {
    status.value = 'ready';
    qrDataUrl.value = null;
    stepError.value = null;
    emit('paired');
  } else if (event.kind === 'disconnected') {
    status.value = 'disconnected';
  } else if (event.kind === 'unlinked') {
    status.value = 'unlinked';
    errorMessage.value = event.detail || null;
  } else if (event.kind === 'error') {
    status.value = 'error';
    errorMessage.value = event.error || 'Pairing error.';
  }
}

async function resetAndPairAgain() {
  if (repairing.value || !props.channelId) return;
  try {
    await ElMessageBox.confirm(
      `This clears the saved ${props.channelLabel} session and starts pairing `
      + 'over, so you will need to link the account again. The channel and its '
      + 'groups are kept.',
      'Reset the saved session?',
      { type: 'warning', confirmButtonText: 'Reset & pair again', cancelButtonText: 'Cancel' },
    );
  } catch {
    return; // dismissed
  }
  repairing.value = true;
  closeStream();
  try {
    await channelsStore.repairChannel(props.channelId);
    // The adapter is new, so re-subscribe: it caches its first QR / code
    // prompt, which the fresh stream replays on connect.
    openStream();
  } catch (e) {
    status.value = 'error';
    stalled.value = true;
    errorMessage.value = e instanceof Error ? e.message : String(e);
    ElMessage.error(errorMessage.value);
  } finally {
    repairing.value = false;
  }
}

function closeStream() {
  clearStuckTimer();
  if (handle) {
    handle.close();
    handle = null;
  }
}

async function submitCode() {
  if (!codeInput.value.trim() || submitting.value) return;
  submitting.value = true;
  try {
    await submitChannelAuthInput(
      settings.agentUrl, settings.authToken, props.channelId,
      { code: codeInput.value.trim() },
    );
    // Server-side: success → next event will move us to `ready` or
    // `password_required`; failure → another `code_required` with `error`.
    codeInput.value = '';
  } catch (e) {
    submitting.value = false;
    ElMessage.error(e instanceof Error ? e.message : 'Failed to submit code');
  }
}

async function submitPassword() {
  if (!passwordInput.value || submitting.value) return;
  submitting.value = true;
  try {
    await submitChannelAuthInput(
      settings.agentUrl, settings.authToken, props.channelId,
      { password: passwordInput.value },
    );
    passwordInput.value = '';
  } catch (e) {
    submitting.value = false;
    ElMessage.error(e instanceof Error ? e.message : 'Failed to submit password');
  }
}

watch(
  () => [props.modelValue, props.channelId] as const,
  ([open]) => {
    if (open) openStream();
    else closeStream();
  },
  { immediate: true },
);

onBeforeUnmount(closeStream);

function handleClose() {
  emit('update:modelValue', false);
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="`Pair ${channelLabel}` + (titleSuffix ? ` — ${titleSuffix}` : '')"
    width="420px"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
    @close="handleClose"
  >
    <div class="pair">
      <!-- Connecting (and the stalled case: the adapter never said anything) -->
      <div v-if="status === 'connecting'" class="state" :class="{ warn: stalled }">
        <Icon v-if="!stalled" icon="mdi:loading" class="spin" />
        <Icon v-else icon="mdi:alert-circle-outline" />
        <span v-if="!stalled">Waiting for the adapter to start the pairing flow…</span>
        <span v-else>
          The adapter hasn't started a pairing flow. This usually means the
          saved session was invalidated elsewhere — for example the same
          account was paired in another environment — so it keeps trying to
          restore it instead of showing a new code.
        </span>
      </div>

      <!-- QR pairing (WhatsApp / Zalo) -->
      <div v-else-if="status === 'qr'" class="state">
        <p class="hint">
          Open <strong>{{ channelLabel }}</strong> on your phone →
          <strong>{{ qrSteps }}</strong>, then scan the code below.
        </p>
        <img v-if="qrDataUrl" :src="qrDataUrl" alt="Pairing QR" class="qr-image" />
      </div>

      <!-- Telegram code -->
      <div v-else-if="status === 'code_required'" class="state form">
        <p class="hint">
          Telegram just sent a verification code to
          <strong>{{ phoneHint || 'your phone' }}</strong>. Check your
          Telegram app (or SMS) and enter the code below.
        </p>
        <ElInput
          v-model="codeInput"
          placeholder="12345"
          maxlength="10"
          autofocus
          @keyup.enter="submitCode"
        />
        <p v-if="stepError" class="step-error">{{ stepError }}</p>
        <ElButton
          type="primary"
          :loading="submitting"
          :disabled="!codeInput.trim() || submitting"
          @click="submitCode"
        >Verify</ElButton>
      </div>

      <!-- Telegram 2FA -->
      <div v-else-if="status === 'password_required'" class="state form">
        <p class="hint">
          Your Telegram account has two-step verification enabled. Enter
          your cloud password.
        </p>
        <ElInput
          v-model="passwordInput"
          type="password"
          show-password
          placeholder="Cloud password"
          autofocus
          @keyup.enter="submitPassword"
        />
        <p v-if="stepError" class="step-error">{{ stepError }}</p>
        <ElButton
          type="primary"
          :loading="submitting"
          :disabled="!passwordInput || submitting"
          @click="submitPassword"
        >Verify</ElButton>
      </div>

      <!-- Done / disconnected / error -->
      <div v-else-if="status === 'ready'" class="state ok">
        <Icon icon="mdi:check-circle-outline" />
        <span>Paired. You can close this dialog.</span>
      </div>
      <div v-else-if="status === 'disconnected'" class="state warn">
        <Icon icon="mdi:alert-circle-outline" />
        <span>Session disconnected. Waiting for reconnect…</span>
      </div>
      <div v-else-if="status === 'unlinked'" class="state err">
        <Icon icon="mdi:link-variant-off" />
        <span>
          {{ errorMessage || 'This session was logged out from the platform.' }}
          Reset it below to pair again.
        </span>
      </div>
      <div v-else-if="status === 'error'" class="state err">
        <Icon icon="mdi:close-circle-outline" />
        <span>{{ errorMessage || 'Pairing failed.' }}</span>
      </div>

      <p v-if="canReset" class="reset-hint">
        Resetting clears the saved session and starts pairing over. The channel,
        its contacts and its groups are kept.
      </p>
    </div>

    <template #footer>
      <ElButton
        v-if="canReset || repairing"
        type="primary"
        :loading="repairing"
        @click="resetAndPairAgain"
      >
        Reset &amp; pair again
      </ElButton>
      <ElButton @click="handleClose">
        {{ status === 'ready' ? 'Done' : 'Close' }}
      </ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.pair { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 8px 0; }
.hint { font-size: 0.875rem; color: var(--text-secondary); margin: 0 0 4px 0; line-height: 1.5; text-align: center; }
.state { display: flex; flex-direction: column; align-items: center; gap: 12px; min-height: 200px; justify-content: center; font-size: 0.9rem; color: var(--text-secondary); width: 100%; }
.state.form { gap: 10px; }
.state.form .el-input, .state.form :deep(.el-input) { width: 240px; }
.state.ok { color: var(--el-color-success); }
.state.warn { color: var(--el-color-warning); }
.state.err { color: var(--el-color-danger); }
.qr-image { width: 240px; height: 240px; border: 1px solid var(--border-color); border-radius: 8px; background: white; padding: 8px; box-sizing: content-box; }
.step-error { color: var(--el-color-danger); font-size: 0.825rem; margin: 0; text-align: center; }
.reset-hint { font-size: 0.8rem; color: var(--text-secondary); margin: 0; text-align: center; line-height: 1.5; }
.spin { animation: spin 1s linear infinite; font-size: 24px; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
</style>
