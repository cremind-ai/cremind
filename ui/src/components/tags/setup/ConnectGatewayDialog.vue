<script setup lang="ts">
/**
 * Connect gateway: choose the gateway computer (skipped when there is only
 * one) → "Plug the gateway into Office PC, where Cremind is running." →
 * Cremind searches that computer's USB ports → the gateway it found (the
 * only free one preselected; one of yours driven from elsewhere can be moved
 * here) → Cremind connects it: checks it again, sets up its worker, claims
 * it → "Gateway connected".
 *
 * Every step runs on the server, so closing the dialog loses nothing: a
 * connection still running shows on the page with Continue, and resumes from
 * its id (`resumeOperationId`). What a computer can do comes from its own
 * report; each problem (no gateway, USB access, busy, firmware, components,
 * offline, owned elsewhere, recovery) has its own words and way forward.
 *
 * In the desktop app, the success step also offers to keep Cremind running
 * in the background, so tags keep updating after the window closes.
 */
import { computed, nextTick, ref, watch } from 'vue';
import { ElButton, ElDialog, ElInput, ElMessage, ElRadio, ElRadioGroup, ElResult, ElStep, ElSteps, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { useGatewayConnect } from '../../../composables/useGatewayConnect';
import { backgroundBridge } from '../../../services/desktopBridge';
import { TagsApiError } from '../../../services/tagsApi';
import type { GatewayCandidate } from '../../../services/tagsSetupApi';
import {
  CONNECT_STAGES, candidateView, connectStageIndex, connectionTitle, hostName, hostOpProgressLabel,
  hostStatePill, isOperationTerminal, plugInstruction, problemText, setupErrorMessage,
} from '../../../utils/tagsSetupFormat';

const props = defineProps<{ modelValue: boolean; hostId?: string | null; resumeOperationId?: string | null }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'add-bridge'): void;
  /** A gateway of this profile was moved here: follow its devices. */
  (e: 'recovering', recoveryId: string): void;
}>();

const store = useTagsSetupStore();
const flow = useGatewayConnect();
const picked = ref<string | undefined>(undefined);
const selected = ref<string | undefined>(undefined);
const name = ref('');
const searchButton = ref<InstanceType<typeof ElButton> | null>(null);
const preparing = ref(false);

const offerBackground = ref(false);
const enablingBackground = ref(false);
const backgroundDone = ref(false);

const host = computed(() => flow.host.value);
const plug = computed(() => plugInstruction(host.value));
const selectable = computed(() => store.hosts);
const usable = computed(() => store.usableHosts);
const step = computed(() => flow.step.value);
const decision = computed(() => flow.decision.value);
const found = computed<GatewayCandidate[]>(() => (decision.value?.kind === 'found' ? decision.value.candidates : []));
const chosen = computed(() => found.value.find((c) => c.id === selected.value) ?? null);
const problem = computed(() => (decision.value?.kind === 'problem' ? problemText(decision.value.problem, host.value) : null));
const connectionOp = computed(() => flow.connection.value);
const stageIndex = computed(() => connectStageIndex(connectionOp.value));
const connection = computed(() => {
  const id = connectionOp.value?.companion_id;
  return id ? store.connections.find((c) => c.id === id) ?? null : null;
});
const doneText = computed(() => {
  const gateway = connection.value ? connectionTitle(connection.value) : 'The gateway';
  return `${gateway} is connected through ${hostName(host.value)}. Cremind keeps your tags updated from there.`;
});
const title = computed(() => (step.value === 'choose' ? 'Connect a gateway' : `Connect a gateway to ${hostName(host.value)}`));

watch(() => props.modelValue, async (open) => {
  if (!open) return;
  offerBackground.value = false;
  backgroundDone.value = false;
  selected.value = undefined;
  name.value = '';
  preparing.value = false;
  flow.choose(null);
  if (props.resumeOperationId) {
    flow.attach(props.resumeOperationId);
    return;
  }
  const only = usable.value.length === 1 ? usable.value[0].id : null;
  const start = props.hostId ?? only;
  if (start) flow.choose(start);
  picked.value = start ?? usable.value[0]?.id;
  await nextTick();
  (searchButton.value?.$el as HTMLElement | undefined)?.focus();
});

// A search that found exactly one free gateway preselects it.
watch(decision, (d) => {
  if (d?.kind === 'found' && !found.value.some((c) => c.id === selected.value)) selected.value = d.preselect ?? undefined;
});

watch(step, async (s) => {
  if (s !== 'done') return;
  const op = connectionOp.value;
  if (op?.recover && op.recovery_id) {
    close();
    emit('recovering', op.recovery_id);
    return;
  }
  const bg = backgroundBridge();
  if (!bg) return;
  try {
    const status = await bg.status();
    offerBackground.value = status.applicable && !status.enabled;
  } catch {
    offerBackground.value = false;
  }
});

function proceed() {
  if (picked.value) flow.choose(picked.value);
}

async function connectChosen() {
  const c = chosen.value;
  if (!c) return;
  await flow.connect(c.id, { name: name.value });
}

async function recoverHere(c: GatewayCandidate) {
  await flow.connect(c.id, { recover: true });
}

async function prepare() {
  const h = host.value;
  if (!h) return;
  preparing.value = true;
  try {
    const op = await store.prepareHost(h.id);
    store.follow('hostop', op.id);
    ElMessage.info(`Preparing the components on ${hostName(h)}. This page shows when they are ready.`);
  } catch (e) {
    ElMessage.error(e instanceof TagsApiError ? setupErrorMessage(e.code, { fallback: e.message }) : 'Cremind could not be reached. Try again.');
  } finally {
    preparing.value = false;
  }
}

async function enableBackground() {
  const bg = backgroundBridge();
  if (!bg) return;
  enablingBackground.value = true;
  try {
    const res = await bg.enable();
    if (res.ok) {
      backgroundDone.value = true;
      offerBackground.value = false;
    } else {
      ElMessage.error(res.error || 'Cremind could not be set to keep running.');
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Cremind could not be set to keep running.');
  } finally {
    enablingBackground.value = false;
  }
}

function close() {
  flow.detach();
  emit('update:modelValue', false);
}

async function cancelConnection() {
  if (await flow.cancel()) close();
}

function addBridge() {
  close();
  emit('add-bridge');
}

/** The dialog closes through the parent's v-model: what runs on the server goes on. */
function beforeClose(_done: () => void) {
  close();
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="title"
    width="min(580px, calc(100vw - 24px))"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
  >
    <!-- Which computer -->
    <div v-if="step === 'choose'" class="step">
      <template v-if="selectable.length">
        <p class="text">Which computer is the gateway plugged into? Cremind drives it over USB from there.</p>
        <ElRadioGroup v-model="picked" class="host-choices" aria-label="Gateway computer">
          <ElRadio
            v-for="h in selectable" :key="h.id" :value="h.id" :disabled="!h.access.can_use" class="host-choice" border
          >
            <span class="choice-title">{{ hostName(h) }}</span>
            <ElTag :type="hostStatePill(h).type" size="small" effect="plain">{{ hostStatePill(h).label }}</ElTag>
            <span v-if="!h.access.can_use" class="choice-note">{{ h.access.reason }}</span>
          </ElRadio>
        </ElRadioGroup>
      </template>
      <p v-else class="text">
        Cremind does not report a computer it can drive gateways from yet. Update Cremind, then try again.
      </p>
    </div>

    <!-- Plug in -->
    <div v-else-if="step === 'plug'" class="step">
      <div class="intro">
        <Icon icon="mdi:usb-port" class="intro-icon" aria-hidden="true" />
        <div>
          <h3 class="intro-title">{{ plug.before }}<strong>{{ plug.name }}</strong>{{ plug.after }}</h3>
          <p class="intro-text">
            Use its USB cable. Cremind then looks after the gateway from {{ hostName(host) }} and keeps your
            tags updated, even when this page is closed.
          </p>
        </div>
      </div>
      <div v-if="flow.blocked.value" class="problem" role="alert">
        <Icon icon="mdi:alert-outline" class="problem-icon" aria-hidden="true" />
        <div>
          <strong>{{ flow.blocked.value.title }}</strong>
          <p>{{ flow.blocked.value.text }}</p>
          <ElButton v-if="flow.blocked.value.action === 'prepare'" size="small" type="primary" :loading="preparing" @click="prepare">
            Prepare components
          </ElButton>
          <ElButton v-else-if="flow.blocked.value.action === 'retry'" size="small" @click="store.loadHosts()">Check again</ElButton>
        </div>
      </div>
      <p v-if="flow.actionError.value" class="error" role="alert">{{ flow.actionError.value }}</p>
    </div>

    <!-- Searching -->
    <div v-else-if="step === 'searching'" class="step" aria-live="polite">
      <p class="text"><Icon icon="mdi:loading" class="spin" aria-hidden="true" />
        {{ flow.scan.value ? hostOpProgressLabel(flow.scan.value) : 'Looking at the USB ports…' }}</p>
    </div>

    <!-- What the search found -->
    <div v-else-if="step === 'results'" class="step">
      <template v-if="decision?.kind === 'found'">
        <p class="text">Found on {{ hostName(host) }}:</p>
        <ElRadioGroup v-model="selected" class="candidates" aria-label="Gateways found">
          <div v-for="c in found" :key="c.id" class="candidate">
            <ElRadio v-if="candidateView(c).action === 'connect'" :value="c.id" class="candidate-pick">
              <span class="choice-title">{{ candidateView(c).title }}</span>
            </ElRadio>
            <span v-else class="choice-title static">{{ candidateView(c).title }}</span>
            <ElTag :type="candidateView(c).pill.type" size="small" effect="plain">{{ candidateView(c).pill.label }}</ElTag>
            <p class="candidate-detail">
              {{ candidateView(c).problem ? problemText(candidateView(c).problem!, host).text : candidateView(c).detail }}
            </p>
            <ElButton
              v-if="candidateView(c).action === 'recover'" size="small" type="primary"
              :loading="flow.busy.value === 'connect'" @click="recoverHere(c)"
            >
              Move it here
            </ElButton>
          </div>
        </ElRadioGroup>
        <div v-if="chosen" class="name-field">
          <label for="gateway-name" class="name-label">Name (optional)</label>
          <ElInput id="gateway-name" v-model="name" maxlength="128" :placeholder="`${hostName(host)} gateway`" />
        </div>
      </template>
      <div v-else-if="problem" class="problem" role="alert">
        <Icon icon="mdi:alert-outline" class="problem-icon" aria-hidden="true" />
        <div>
          <strong>{{ problem.title }}</strong>
          <p>{{ problem.text }}</p>
        </div>
      </div>
      <p v-else-if="decision?.kind === 'failed'" class="error" role="alert">
        {{ setupErrorMessage(decision.error?.code, { fallback: decision.error?.message, host }) }}
      </p>
      <p v-else-if="decision?.kind === 'cancelled'" class="text">The search was cancelled.</p>
      <p v-if="flow.actionError.value" class="error" role="alert">{{ flow.actionError.value }}</p>
    </div>

    <!-- Connecting -->
    <div v-else-if="step === 'connecting'" class="step" aria-live="polite">
      <ElSteps :active="stageIndex" finish-status="success" align-center class="stages">
        <ElStep v-for="s in CONNECT_STAGES" :key="s" :title="s" />
      </ElSteps>
      <p class="text center">{{ connectionOp ? hostOpProgressLabel(connectionOp) : 'Connecting…' }}</p>
      <p class="text muted center">This takes a few seconds; it goes on if you close this window.</p>
      <p v-if="flow.actionError.value" class="error" role="alert">{{ flow.actionError.value }}</p>
    </div>

    <!-- Done -->
    <div v-else-if="step === 'done'" class="step">
      <ElResult icon="success" title="Gateway connected" :sub-title="doneText">
        <template #extra>
          <div class="done-actions">
            <ElButton type="primary" @click="addBridge"><Icon icon="mdi:plus" class="btn-icon" /> Add bridge</ElButton>
            <ElButton @click="close">Done</ElButton>
          </div>
        </template>
      </ElResult>
      <div v-if="offerBackground" class="background-offer" role="status">
        <Icon icon="mdi:power-sleep" class="bg-icon" aria-hidden="true" />
        <span class="bg-text">Keep Cremind running in the background so tags keep updating when this window is closed.</span>
        <ElButton size="small" type="primary" :loading="enablingBackground" @click="enableBackground">Keep running</ElButton>
      </div>
      <p v-else-if="backgroundDone" class="bg-done" role="status">
        <Icon icon="mdi:check" aria-hidden="true" /> Cremind keeps running in the background.
      </p>
    </div>

    <!-- Failed -->
    <div v-else class="step">
      <ElResult
        icon="error"
        :title="flow.failure.value?.problem ? problemText(flow.failure.value.problem, host).title : 'The gateway was not connected'"
        :sub-title="flow.failure.value?.text ?? ''"
      />
    </div>

    <template #footer>
      <template v-if="step === 'choose'">
        <ElButton @click="close">Cancel</ElButton>
        <ElButton type="primary" :disabled="!picked" @click="proceed">Continue</ElButton>
      </template>
      <template v-else-if="step === 'plug'">
        <ElButton v-if="usable.length > 1" @click="flow.choose(null)">Another computer</ElButton>
        <ElButton @click="close">Cancel</ElButton>
        <ElButton
          ref="searchButton" type="primary" :disabled="!!flow.blocked.value"
          :loading="flow.busy.value === 'search'" @click="flow.search()"
        >
          Search for gateways
        </ElButton>
      </template>
      <template v-else-if="step === 'searching'">
        <ElButton @click="close">Close</ElButton>
      </template>
      <template v-else-if="step === 'results'">
        <ElButton @click="close">Cancel</ElButton>
        <ElButton :loading="flow.busy.value === 'search'" @click="flow.search()">Search again</ElButton>
        <ElButton
          v-if="decision?.kind === 'found'" type="primary" :disabled="!chosen"
          :loading="flow.busy.value === 'connect'" @click="connectChosen"
        >
          Connect
        </ElButton>
      </template>
      <template v-else-if="step === 'connecting'">
        <ElButton v-if="flow.cancellable.value" :loading="flow.busy.value === 'cancel'" @click="cancelConnection">Cancel</ElButton>
        <ElButton @click="close">{{ connectionOp && !isOperationTerminal(connectionOp.state) ? 'Close (it continues)' : 'Close' }}</ElButton>
      </template>
      <template v-else-if="step === 'failed'">
        <ElButton @click="close">Close</ElButton>
        <ElButton type="primary" @click="flow.again()">Try again</ElButton>
      </template>
    </template>
  </ElDialog>
</template>

<style scoped>
.step { display: flex; flex-direction: column; gap: 10px; }
.text { margin: 0; font-size: 0.88rem; line-height: 1.55; color: var(--text-primary); }
.text.muted { color: var(--text-secondary); }
.text.center { text-align: center; }
.intro { display: flex; gap: 14px; align-items: flex-start; }
.intro-icon { font-size: 36px; color: var(--primary-color); flex-shrink: 0; }
.intro-title { margin: 0 0 6px; font-size: 1.02rem; font-weight: 500; color: var(--text-primary); line-height: 1.4; }
.intro-text { margin: 0; font-size: 0.88rem; line-height: 1.55; color: var(--text-secondary); }
.host-choices, .candidates { display: flex; flex-direction: column; align-items: stretch; gap: 8px; }
.host-choice { height: auto; min-height: 40px; padding: 8px 12px; margin-right: 0; white-space: normal; }
.host-choice :deep(.el-radio__label) { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.choice-title { font-weight: 600; color: var(--text-primary); }
.choice-title.static { margin-left: 2px; }
.choice-note { flex-basis: 100%; font-size: 0.8rem; color: var(--text-tertiary); }
.candidate {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 10px 12px;
  border: 1px solid var(--border-color); border-radius: 8px;
}
.candidate-pick { margin-right: 0; }
.candidate-detail { flex-basis: 100%; margin: 0; font-size: 0.82rem; line-height: 1.5; color: var(--text-secondary); }
.name-field { display: flex; flex-direction: column; gap: 4px; max-width: 360px; }
.name-label { font-size: 0.82rem; color: var(--text-secondary); }
.problem {
  display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--el-color-warning) 45%, transparent);
  background: color-mix(in srgb, var(--el-color-warning) 8%, var(--surface-color));
}
.problem-icon { font-size: 1.3rem; color: var(--el-color-warning); flex-shrink: 0; }
.problem strong { font-size: 0.9rem; color: var(--text-primary); }
.problem p { margin: 4px 0 8px; font-size: 0.86rem; line-height: 1.5; color: var(--text-primary); }
.error { margin: 0; font-size: 0.85rem; color: var(--el-color-danger); }
.stages { margin: 6px 0 4px; }
.done-actions { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
.done-actions .el-button + .el-button { margin-left: 0; }
.btn-icon { margin-right: 6px; }
.background-offer {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 12px; border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--primary-color) 40%, transparent);
  background: color-mix(in srgb, var(--primary-color) 7%, var(--surface-color));
}
.bg-icon { font-size: 1.2rem; color: var(--primary-color); flex-shrink: 0; }
.bg-text { flex: 1; min-width: 200px; font-size: 0.86rem; color: var(--text-primary); line-height: 1.45; }
.bg-done { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 0.86rem; color: var(--el-color-success); }
.spin { animation: cg-spin 1s linear infinite; vertical-align: -2px; }
@keyframes cg-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
