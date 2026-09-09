<script setup lang="ts">
/**
 * Codex sign-in by device code.
 *
 * The Codex CLI's own ``codex login --device-auth`` flow, driven from the
 * browser: the server holds the live SDK handle (and the ``codex app-server``
 * child it spawned) for up to fifteen minutes and this dialog holds nothing but
 * the ``login_id``. The markup deliberately mirrors the Gemini device flow in
 * ``shared/ProviderConfigFields.vue`` — a user who has signed a provider in
 * once should recognise this screen, not learn a second one.
 *
 * Because the session lives only in the server's memory, a restart mid-login
 * answers 404: that is not an error to retry silently, it is a session that no
 * longer exists, so the dialog says so and offers to start over.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { ElButton, ElDialog } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../../stores/settings';
import { useCopyToClipboard } from '../../composables/useCopyToClipboard';
import {
  cancelCodexDeviceLogin, getCodexDeviceLogin, startCodexDeviceLogin,
  type CodingAgentStatus,
} from '../../services/configApi';

const props = defineProps<{
  modelValue: boolean;
  agent: CodingAgentStatus;
}>();

const emit = defineEmits<{
  'update:modelValue': [value: boolean];
  /** The login completed. The page re-probes and reloads; the dialog stays
   *  open so the user sees which account they landed on. */
  done: [];
}>();

/** The code is valid for fifteen minutes, so a slow poll costs nothing and a
 *  fast one would only hammer the server for the whole wait. */
const POLL_MS = 3000;

const settings = useSettingsStore();
const { copy, isCopied } = useCopyToClipboard();

type Phase = 'starting' | 'pending' | 'success' | 'error' | 'cancelled';

const phase = ref<Phase>('starting');
const loginId = ref('');
const verificationUrl = ref('');
const userCode = ref('');
const detail = ref('');
const account = ref<Record<string, unknown> | null>(null);
const cancelling = ref(false);

let poller: ReturnType<typeof setInterval> | null = null;

function stopPolling() {
  if (poller !== null) {
    clearInterval(poller);
    poller = null;
  }
}

function messageOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

const accountLine = computed(() => {
  const info = account.value;
  if (!info) return '';
  const parts: string[] = [];
  for (const key of ['email', 'plan_type', 'subscription_type', 'type']) {
    const value = info[key];
    if (typeof value === 'string' && value.trim()) parts.push(value.trim());
  }
  return parts.join(' · ');
});

async function start() {
  stopPolling();
  phase.value = 'starting';
  detail.value = '';
  account.value = null;
  loginId.value = '';
  verificationUrl.value = '';
  userCode.value = '';
  try {
    const res = await startCodexDeviceLogin(settings.agentUrl, settings.authToken);
    // The route answers 200 with `error` when the CLI was reachable but the
    // flow refused to start — a reason beats a bare failure.
    if (res.error) {
      phase.value = 'error';
      detail.value = res.error;
      return;
    }
    loginId.value = res.login_id;
    verificationUrl.value = res.verification_url;
    userCode.value = res.user_code;
    phase.value = 'pending';
    poller = setInterval(() => { void tick(); }, POLL_MS);
  } catch (e) {
    phase.value = 'error';
    detail.value = messageOf(e);
  }
}

async function tick() {
  if (!loginId.value) return;
  try {
    const res = await getCodexDeviceLogin(settings.agentUrl, settings.authToken, loginId.value);
    if (res.verification_url) verificationUrl.value = res.verification_url;
    if (res.user_code) userCode.value = res.user_code;
    detail.value = res.detail ?? '';
    if (res.status === 'success') {
      stopPolling();
      phase.value = 'success';
      account.value = res.account ?? null;
      emit('done');
      return;
    }
    if (res.status === 'error' || res.status === 'cancelled') {
      stopPolling();
      phase.value = res.status;
      return;
    }
    phase.value = res.status;
  } catch (e) {
    // Includes the 404 a server restart produces, which the API client already
    // words as an interrupted sign-in. Either way there is nothing left to poll.
    stopPolling();
    phase.value = 'error';
    detail.value = messageOf(e);
  }
}

async function cancel() {
  stopPolling();
  if (loginId.value) {
    cancelling.value = true;
    try {
      await cancelCodexDeviceLogin(settings.agentUrl, settings.authToken, loginId.value);
    } finally {
      cancelling.value = false;
    }
  }
  phase.value = 'cancelled';
  close();
}

function close() {
  emit('update:modelValue', false);
}

/** The dialog is controlled, so its own close button only reports intent — the
 *  page owns the flag that actually hides it. */
function onVisibilityChange(value: boolean) {
  if (!value) close();
}

/** Leaving a pending flow behind would keep the server's app-server child alive
 *  for the rest of the fifteen minutes, so closing releases it. */
function releaseIfPending() {
  stopPolling();
  if (loginId.value && (phase.value === 'pending' || phase.value === 'starting')) {
    void cancelCodexDeviceLogin(settings.agentUrl, settings.authToken, loginId.value);
    loginId.value = '';
  }
}

watch(() => props.modelValue, (open) => {
  if (open) void start();
  else releaseIfPending();
});

onBeforeUnmount(releaseIfPending);
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="`Sign in to ${agent.display_name}`"
    width="520px"
    :close-on-click-modal="false"
    @update:model-value="onVisibilityChange"
  >
    <p class="device-intro">
      This signs the <strong>{{ agent.display_name }}</strong> command-line tool in
      on the Cremind server. Open the link, enter the code, and leave this dialog
      open until it confirms.
    </p>

    <div v-if="phase === 'starting'" class="device-code-status">
      <span class="polling-indicator">Starting the sign-in…</span>
    </div>

    <div v-else-if="phase === 'pending'" class="device-code-active">
      <div class="device-code-step">
        <span class="device-code-label">Visit:</span>
        <a :href="verificationUrl" target="_blank" rel="noopener" class="device-code-link">
          {{ verificationUrl }}
        </a>
        <button
          type="button"
          class="copy-icon-btn"
          :class="{ copied: isCopied('uri') }"
          :title="isCopied('uri') ? 'Copied!' : 'Copy'"
          @click="copy(verificationUrl, 'uri')"
        >
          <Icon :icon="isCopied('uri') ? 'mdi:check' : 'mdi:content-copy'" />
        </button>
      </div>
      <div class="device-code-step">
        <span class="device-code-label">Code:</span>
        <code class="device-code-value">{{ userCode }}</code>
        <button
          type="button"
          class="copy-icon-btn"
          :class="{ copied: isCopied('code') }"
          :title="isCopied('code') ? 'Copied!' : 'Copy'"
          @click="copy(userCode, 'code')"
        >
          <Icon :icon="isCopied('code') ? 'mdi:check' : 'mdi:content-copy'" />
        </button>
      </div>
      <div class="device-code-status">
        <span class="polling-indicator">Waiting for authorization…</span>
      </div>
    </div>

    <div v-else-if="phase === 'success'" class="device-code-done">
      Signed in{{ accountLine ? ` as ${accountLine}` : '' }}.
    </div>

    <div v-else class="device-code-failed">
      {{ detail || (phase === 'cancelled' ? 'Sign-in cancelled.' : 'The sign-in did not complete.') }}
    </div>

    <p v-if="detail && phase === 'pending'" class="device-detail">{{ detail }}</p>

    <template #footer>
      <ElButton
        v-if="phase === 'pending' || phase === 'starting'"
        :loading="cancelling"
        @click="cancel"
      >Cancel sign-in</ElButton>
      <ElButton
        v-if="phase === 'error' || phase === 'cancelled'"
        type="primary"
        @click="start"
      >Start again</ElButton>
      <ElButton
        v-if="phase !== 'pending' && phase !== 'starting'"
        :type="phase === 'success' ? 'primary' : 'default'"
        @click="close"
      >Close</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
/* Same shapes as the provider device-code flow in ProviderConfigFields.vue —
   one sign-in screen, rendered twice. */
.device-intro {
  margin: 0 0 12px 0;
  font-size: 0.85rem;
  line-height: 1.5;
  color: var(--text-secondary);
}

.device-code-active {
  padding: 12px;
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 8px;
  background: var(--surface-color, #fafafa);
  margin: 8px 0;
}

.device-code-step {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.device-code-label {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-primary);
  min-width: 40px;
}

.device-code-link {
  color: var(--primary-color, #409eff);
  text-decoration: none;
  font-size: 0.85rem;
  word-break: break-all;
}
.device-code-link:hover { text-decoration: underline; }

.copy-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  cursor: pointer;
  padding: 2px;
  border-radius: 4px;
  font-size: 0.95rem;
  color: var(--text-secondary);
  transition: color 0.15s ease;
}
.copy-icon-btn:hover { color: var(--primary-color); }
.copy-icon-btn.copied { color: var(--success-color); }

.device-code-value {
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--text-primary);
  background: var(--bg-color, #fff);
  padding: 2px 10px;
  border-radius: 4px;
  border: 1px solid var(--border-color, #e4e7ed);
  letter-spacing: 2px;
}

.device-code-status { margin-top: 4px; }

.polling-indicator {
  font-size: 0.8rem;
  color: var(--text-secondary);
}
.polling-indicator::before {
  content: '';
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--warning-color, #e6a23c);
  margin-right: 6px;
  animation: pulse 1.5s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; }
}

.device-code-done {
  font-size: 0.85rem;
  color: var(--success-color, #67c23a);
  padding: 8px 0;
}

.device-code-failed {
  font-size: 0.85rem;
  line-height: 1.5;
  color: var(--danger-color, #f56c6c);
  padding: 8px 0;
}

.device-detail {
  margin: 6px 0 0 0;
  font-size: 0.78rem;
  line-height: 1.45;
  color: var(--text-tertiary);
}
</style>
