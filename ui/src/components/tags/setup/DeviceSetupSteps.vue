<script setup lang="ts">
/**
 * The steps of adding a bridge or a tag, shared by the Add dialogs: looking
 * for the device, choosing where it goes when more than one device heard it
 * (a bridge: gateways; a tag: the gateway itself, when it reaches tags, and
 * bridges), pairing progress, and what went wrong. The code step and the
 * success content belong to the dialog (slots `code` and `done`).
 */
import { computed, nextTick, ref, watch } from 'vue';
import { ElButton, ElRadio, ElRadioGroup, ElResult, ElStep, ElSteps, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import type { DiscoveryCandidate, Pairing } from '../../../services/tagsSetupApi';
import type { PairingStep } from '../../../composables/useDevicePairing';
import {
  candidateTitle, capacityFull, capacityLabel, operationProgressLabel, setupErrorMessage, shortSuffix,
  signalLabel, tagReach,
} from '../../../utils/tagsSetupFormat';

const props = defineProps<{
  role: 'bridge' | 'tag';
  step: PairingStep;
  candidates: DiscoveryCandidate[];
  recommended: string | null;
  chosen: string | null;
  pairing: Pairing | null;
  problem: { title: string; text: string; action: 'search' | 'retry' } | null;
  busy: string;
  choiceError: string;
  lookingFor: string;
  /** Stop was asked for: the pairing is being undone (or kept, if the device already took it). */
  stopping?: boolean;
}>();

const emit = defineEmits<{
  (e: 'update:chosen', v: string | null): void;
  (e: 'pair'): void;
  (e: 'search-again'): void;
  (e: 'again'): void;
}>();

const store = useTagsSetupStore();
const heading = ref<HTMLElement | null>(null);

const STEPS = computed(() => ['Scan the label', props.role === 'tag' ? 'Find the tag' : 'Find the bridge', 'Pair', 'Ready']);
const activeIndex = computed(() => {
  switch (props.step) {
    case 'code': return 0;
    case 'searching':
    case 'choose': return 1;
    case 'pairing': return 2;
    case 'done': return 4;
    default: return props.pairing ? 2 : 1;
  }
});

const headingText = computed(() => {
  switch (props.step) {
    case 'searching': return props.role === 'tag' ? 'Looking for your tag…' : 'Searching for your bridge…';
    case 'choose': return props.role === 'tag' ? 'Choose where this tag connects' : 'Choose a gateway for this bridge';
    case 'pairing': return props.role === 'tag' ? 'Adding your tag…' : 'Adding your bridge…';
    case 'problem': return props.problem?.title ?? '';
    default: return '';
  }
});

/** What a tag should be close to: "your gateway or one of your bridges". */
const reach = computed(() => tagReach(store.readiness));

/** A candidate as people know it: the gateway by its connection's title, a bridge by its name. */
function candidateName(c: DiscoveryCandidate): string {
  return candidateTitle(c, props.role, store.connections);
}

function candidateMeta(c: DiscoveryCandidate): string {
  const full = c.reason === 'bridge_full' || capacityFull(c.capacity);
  // A tag's choice mixes the gateway itself and bridges: say which each one is.
  const parts = [props.role === 'tag' ? (c.bridge_kind === 'gateway' ? 'Gateway' : 'Bridge') : '', signalLabel(c.rssi)];
  if (props.role === 'tag' && (c.capacity || full)) parts.push(full ? 'No room for more tags' : capacityLabel(c.capacity));
  if (!c.eligible && !full) {
    // The sentence without its advice ("Choose another one."): the list is the choice.
    parts.push(setupErrorMessage(c.reason ?? 'candidate_not_eligible', { role: props.role }).replace(/\s+Choose.*$/, ''));
  }
  return parts.filter(Boolean).join(' · ');
}

const progress = computed(() => {
  if (props.stopping) return 'Stopping… If the device already finished, it is kept.';
  return props.pairing ? operationProgressLabel(props.pairing) : 'Starting…';
});

watch(() => props.step, async (s) => {
  if (s === 'code' || s === 'done') return;
  await nextTick();
  heading.value?.focus();
});
</script>

<template>
  <div class="device-steps">
    <ElSteps :active="activeIndex" :process-status="step === 'problem' ? 'error' : 'process'" finish-status="success" align-center class="steps">
      <ElStep v-for="t in STEPS" :key="t" :title="t" />
    </ElSteps>

    <slot v-if="step === 'code'" name="code" />
    <slot v-else-if="step === 'done'" name="done" />

    <template v-else-if="step === 'problem' && problem">
      <h3 ref="heading" tabindex="-1" class="visually-hidden">{{ headingText }}</h3>
      <ElResult icon="warning" :title="problem.title" :sub-title="problem.text">
        <template #extra>
          <ElButton v-if="problem.action === 'search'" type="primary" :loading="busy === 'find'" @click="emit('search-again')">
            Search again
          </ElButton>
          <ElButton v-else type="primary" @click="emit('again')">Try again</ElButton>
        </template>
      </ElResult>
    </template>

    <div v-else class="step-body">
      <h3 ref="heading" tabindex="-1" class="step-heading">{{ headingText }}</h3>

      <template v-if="step === 'searching'">
        <p class="step-text" aria-live="polite">
          <Icon icon="mdi:loading" class="spin" aria-hidden="true" />
          <template v-if="role === 'tag'">
            Waiting for the tag to wake (tags check in about every 30 seconds). Keep it close to
            {{ reach }}.
          </template>
          <template v-else>
            Keep the bridge plugged in and close to the gateway. This can take a minute.
          </template>
        </p>
        <p v-if="lookingFor" class="step-hint">Looking for {{ role }} {{ shortSuffix(lookingFor) }}.</p>
      </template>

      <template v-else-if="step === 'choose'">
        <p class="step-text">
          {{ role === 'tag'
            ? 'More than one of your devices can hear your tag. Choose the one it should connect through:'
            : 'More than one gateway can hear your bridge. Choose the one it should join:' }}
        </p>
        <ElRadioGroup
          :model-value="chosen ?? ''"
          class="choices"
          :aria-label="role === 'tag' ? 'Where this tag connects' : 'Gateway for this bridge'"
          @update:model-value="(v) => emit('update:chosen', v ? String(v) : null)"
        >
          <ElRadio v-for="c in candidates" :key="c.id" :value="c.id" :disabled="!c.eligible" class="choice" border>
            <span class="choice-name">{{ candidateName(c) }}</span>
            <ElTag v-if="c.id === recommended && c.eligible" size="small" type="success" effect="plain">Recommended</ElTag>
            <span class="choice-meta">{{ candidateMeta(c) }}</span>
          </ElRadio>
        </ElRadioGroup>
        <p v-if="choiceError" class="step-error" role="alert">{{ choiceError }}</p>
        <div class="row-actions">
          <ElButton type="primary" :disabled="!chosen" :loading="busy === 'pair'" @click="emit('pair')">Pair</ElButton>
        </div>
      </template>

      <template v-else-if="step === 'pairing'">
        <p class="step-text" aria-live="polite">
          <Icon icon="mdi:loading" class="spin" aria-hidden="true" /> {{ progress }}
        </p>
        <p class="step-hint">
          {{ role === 'tag'
            ? 'Tags check in about every 30 seconds, so this can take a minute or two.'
            : 'This usually takes under a minute.' }}
          You can close this window; adding continues in the background.
        </p>
      </template>
    </div>
  </div>
</template>

<style scoped>
.device-steps { display: flex; flex-direction: column; gap: 14px; }
/* Centred steps: titles wrap under their icons, so they fit a phone too. */
.steps :deep(.el-step__title) { font-size: 0.76rem; line-height: 1.3; padding: 0 2px; margin-top: 4px; }
.steps :deep(.el-step__icon) { width: 22px; height: 22px; font-size: 12px; }
.steps :deep(.el-step.is-horizontal .el-step__line) { top: 10px; }
.step-body { display: flex; flex-direction: column; gap: 10px; }
.step-heading { margin: 0; font-size: 1.02rem; font-weight: 600; color: var(--text-primary); outline: none; }
.step-text { margin: 0; font-size: 0.88rem; line-height: 1.55; color: var(--text-primary); }
.step-hint { margin: 0; font-size: 0.78rem; color: var(--text-tertiary); line-height: 1.45; }
.step-error { margin: 0; font-size: 0.85rem; color: var(--el-color-danger); }
.row-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.spin { animation: device-spin 1s linear infinite; vertical-align: -2px; margin-right: 4px; }
@keyframes device-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
.choices { display: flex; flex-direction: column; align-items: stretch; gap: 8px; }
.choice { display: flex; align-items: center; height: auto; min-height: 40px; margin-right: 0; padding: 8px 12px; white-space: normal; }
.choice :deep(.el-radio__label) { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1; }
.choice-name { font-weight: 600; color: var(--text-primary); }
.choice-meta { font-size: 0.8rem; color: var(--text-secondary); }
.visually-hidden {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}
</style>
