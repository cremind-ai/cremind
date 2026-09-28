<script setup lang="ts">
/**
 * The steps of a Cremind Connect setup session, shared by Connect gateway and
 * Recover on this computer: waiting for Connect to open (with the installer
 * after 8 s), approving in the Connect window, confirming that both show the
 * same four words, and the short finish. The first step (plug in) and the
 * success content belong to the dialog (slots `plug` and `done`).
 *
 * Focus follows the step heading, and the heading and status lines are read
 * out as they change.
 */
import { computed, nextTick, ref, watch } from 'vue';
import { ElButton, ElResult, ElStep, ElSteps } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { SetupSession } from '../../../services/tagsSetupApi';
import {
  connectStepIndex, finishLabel, setupErrorMessage, shortSuffix, type ConnectStep,
} from '../../../utils/tagsSetupFormat';
import ConnectInstallOffer from './ConnectInstallOffer.vue';

const props = defineProps<{
  session: SetupSession | null;
  step: ConnectStep;
  operation: 'connect_gateway' | 'recover';
  waitedLong: boolean;
  hasLink: boolean;
  busy: string;
  actionError: string;
  launchError: string;
}>();

const emit = defineEmits<{
  (e: 'reopen'): void;
  (e: 'confirm'): void;
  (e: 'cancel'): void;
  (e: 'retry'): void;
}>();

const heading = ref<HTMLElement | null>(null);
const lastActive = ref(0);

const STEP_TITLES = computed(() => ['Plug in', 'Open Connect', 'Approve', 'Confirm', props.operation === 'recover' ? 'Recover' : 'Connect']);
const activeIndex = computed(() => connectStepIndex(props.step, lastActive.value));
watch(() => props.step, (s) => {
  if (s !== 'failed' && s !== 'done') lastActive.value = connectStepIndex(s, lastActive.value);
}, { immediate: true });

const computerName = computed(() => props.session?.computer?.name || 'this computer');
const phrase = computed(() => props.session?.verification_phrase || '');

const failure = computed(() => {
  const s = props.session;
  if (!s) return { title: '', text: '' };
  if (s.state === 'expired') {
    return { title: 'This setup expired', text: 'A setup has to be finished within five minutes. Start again when you are ready.' };
  }
  if (s.state === 'cancelled') {
    return { title: 'Setup cancelled', text: s.error ? setupErrorMessage(s.error.code, { fallback: s.error.message }) : 'Nothing was changed.' };
  }
  return {
    title: 'Setup did not finish',
    text: setupErrorMessage(s.error?.code, { role: 'gateway', fallback: s.error?.message }),
  };
});

const headingText = computed(() => {
  switch (props.step) {
    case 'open': return 'Waiting for Cremind Connect…';
    case 'approve': return 'Approve in the Cremind Connect window';
    case 'confirm': return 'Do the words match?';
    case 'finish': return props.session ? finishLabel(props.session) : 'Finishing…';
    case 'failed': return failure.value.title;
    default: return '';
  }
});

watch(() => props.step, async (s) => {
  if (s === 'plug' || s === 'done') return;
  await nextTick();
  heading.value?.focus();
});
</script>

<template>
  <div class="connect-steps">
    <ElSteps :active="activeIndex" :process-status="step === 'failed' ? 'error' : 'process'" finish-status="success" align-center class="steps">
      <ElStep v-for="t in STEP_TITLES" :key="t" :title="t" />
    </ElSteps>
    <p class="visually-hidden" aria-live="polite">
      Step {{ Math.min(activeIndex + 1, STEP_TITLES.length) }} of {{ STEP_TITLES.length }}: {{ STEP_TITLES[Math.min(activeIndex, STEP_TITLES.length - 1)] }}
    </p>

    <slot v-if="step === 'plug'" name="plug" />

    <template v-else-if="step === 'done'">
      <slot name="done" />
    </template>

    <template v-else-if="step === 'failed'">
      <ElResult icon="error" :title="failure.title" :sub-title="failure.text">
        <template #extra>
          <ElButton type="primary" :loading="busy === 'start'" @click="emit('retry')">Try again</ElButton>
        </template>
      </ElResult>
      <p v-if="actionError" class="step-error" role="alert">{{ actionError }}</p>
      <h3 ref="heading" tabindex="-1" class="visually-hidden">{{ headingText }}</h3>
    </template>

    <div v-else class="step-body">
      <h3 ref="heading" tabindex="-1" class="step-heading">{{ headingText }}</h3>

      <template v-if="step === 'open'">
        <p class="step-text" aria-live="polite">
          <Icon icon="mdi:loading" class="spin" aria-hidden="true" />
          A Cremind Connect window should open on this computer. If the browser asks whether to open
          Cremind Connect, allow it.
        </p>
        <div class="row-actions">
          <ElButton text type="primary" :loading="busy === 'start'" @click="emit('reopen')">
            <Icon icon="mdi:open-in-app" class="btn-icon" /> Open Cremind Connect again
          </ElButton>
        </div>
        <p v-if="launchError" class="step-error" role="alert">{{ launchError }}</p>
        <div v-if="waitedLong" class="callout" role="status">
          <p class="callout-title">Cremind Connect has not answered yet.</p>
          <p class="step-text">
            It is the small app that talks to your gateway over USB. If it is not installed on this
            computer yet, install it now:
          </p>
          <ConnectInstallOffer @installed="emit('reopen')" />
          <div class="row-actions">
            <ElButton type="primary" :loading="busy === 'start'" @click="emit('reopen')">Continue after installation</ElButton>
          </div>
        </div>
      </template>

      <template v-else-if="step === 'approve'">
        <p class="step-text">
          Cremind Connect on <strong>{{ computerName }}</strong> is asking to
          {{ operation === 'recover' ? 'take over the gateway' : 'connect the gateway' }}.
          Check the gateway it shows, then choose <strong>Approve</strong> there.
        </p>
        <p v-if="phrase" class="step-text">
          It shows these words: <span class="phrase-inline">{{ phrase }}</span>
        </p>
        <p class="step-text muted" aria-live="polite">
          <Icon icon="mdi:loading" class="spin" aria-hidden="true" /> Waiting for your approval…
        </p>
      </template>

      <template v-else-if="step === 'confirm'">
        <dl class="facts">
          <div><dt>Computer</dt><dd>{{ computerName }}</dd></div>
          <div v-if="session?.gateway"><dt>Gateway</dt><dd>Gateway {{ shortSuffix(session.gateway.short_id) }}</dd></div>
        </dl>
        <p class="phrase" aria-label="Verification words">{{ phrase }}</p>
        <p class="step-text">Does the Cremind Connect window show the same words?</p>
        <div class="row-actions">
          <ElButton type="primary" :loading="busy === 'confirm'" @click="emit('confirm')">
            {{ operation === 'recover' ? 'Yes, recover' : 'Yes, connect' }}
          </ElButton>
          <ElButton :loading="busy === 'cancel'" @click="emit('cancel')">No, cancel</ElButton>
        </div>
        <p class="step-hint">If the words are different, cancel: another computer may be trying to connect.</p>
      </template>

      <template v-else-if="step === 'finish'">
        <p class="step-text" aria-live="polite">
          <Icon icon="mdi:loading" class="spin" aria-hidden="true" /> This takes a few seconds.
        </p>
      </template>

      <p v-if="actionError" class="step-error" role="alert">{{ actionError }}</p>
    </div>
  </div>
</template>

<style scoped>
.connect-steps { display: flex; flex-direction: column; gap: 14px; }
/* Centred steps: titles wrap under their icons, so five fit a phone too. */
.steps :deep(.el-step__title) { font-size: 0.76rem; line-height: 1.3; padding: 0 2px; margin-top: 4px; }
.steps :deep(.el-step__icon) { width: 22px; height: 22px; font-size: 12px; }
.steps :deep(.el-step.is-horizontal .el-step__line) { top: 10px; }
.step-body { display: flex; flex-direction: column; gap: 10px; }
.step-heading { margin: 0; font-size: 1.02rem; font-weight: 600; color: var(--text-primary); outline: none; }
.step-text { margin: 0; font-size: 0.88rem; line-height: 1.55; color: var(--text-primary); }
.step-text.muted { color: var(--text-secondary); }
.step-hint { margin: 0; font-size: 0.78rem; color: var(--text-tertiary); line-height: 1.45; }
.step-error { margin: 0; font-size: 0.85rem; color: var(--el-color-danger); }
.row-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.row-actions .el-button + .el-button { margin-left: 0; }
.btn-icon { margin-right: 6px; }
.spin { animation: connect-spin 1s linear infinite; vertical-align: -2px; margin-right: 4px; }
@keyframes connect-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
.callout {
  display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--primary-color) 40%, transparent);
  background: color-mix(in srgb, var(--primary-color) 7%, var(--surface-color));
}
.callout-title { margin: 0; font-weight: 600; font-size: 0.9rem; color: var(--text-primary); }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px 16px; margin: 0; }
.facts dt { font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-tertiary); }
.facts dd { margin: 2px 0 0; font-size: 0.92rem; color: var(--text-primary); }
.phrase {
  margin: 4px 0; padding: 16px; border-radius: 10px; text-align: center;
  font-size: 1.6rem; font-weight: 700; letter-spacing: 0.02em; line-height: 1.35;
  color: var(--text-primary); background: var(--hover-bg); border: 1px solid var(--border-color);
}
.phrase-inline { font-weight: 600; }
.visually-hidden {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}
</style>
