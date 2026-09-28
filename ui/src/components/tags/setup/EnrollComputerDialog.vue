<script setup lang="ts">
/**
 * Set up a gateway computer: the computer a gateway plugs into, running the
 * Cremind app, drives it for this Cremind (which may run in a container or
 * elsewhere and cannot see that computer's USB ports).
 *
 * An `enroll_host` setup session → its link (`cremind://tags/setup?…`) opens
 * in the Cremind app on that computer (in the desktop app itself, "Set up this
 * computer" hands it over directly) → the app shows the server, the profile
 * and four words and asks for approval → this page asks whether the computer
 * shows the same words → the computer receives its own credential → done.
 *
 * The link holds a one-time secret: it stays in this dialog's memory only —
 * never in the store, the URL or browser storage. Closing the dialog before
 * the computer finished cancels the setup.
 */
import { computed, ref, watch } from 'vue';
import { ElButton, ElDialog, ElMessage, ElResult } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { TagsApiError } from '../../../services/tagsApi';
import { openSetupLink, tagsHostBridge } from '../../../services/desktopBridge';
import { enrollFailure, enrollStep, isSessionTerminal, setupErrorMessage } from '../../../utils/tagsSetupFormat';

const props = defineProps<{ modelValue: boolean; resumeSessionId?: string | null }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'connect', hostId: string): void;
}>();

const store = useTagsSetupStore();
const sessionId = ref<string | null>(null);
/** The setup link: memory only (see the module docstring). */
const launchUrl = ref<string | null>(null);
const busy = ref('');
const error = ref('');
const copied = ref(false);
const thisComputer = ref<{
  available: boolean; reason?: string; needsRuntime?: boolean; enrolled: boolean; server?: string;
} | null>(null);
/** Installing the gateway components into this desktop app (its last lines of progress). */
const preparing = ref(false);
const prepareLog = ref<string[]>([]);

const session = computed(() => (sessionId.value ? store.sessions[sessionId.value] ?? null : null));
const step = computed(() => (sessionId.value ? enrollStep(session.value) : 'intro'));
const computerName = computed(() => session.value?.computer?.name || 'the computer');
const words = computed(() => (session.value?.verification_phrase || '').split(' ').filter(Boolean));
const lost = computed(() => (sessionId.value ? store.lost[sessionId.value] : undefined));
const failure = computed(() => {
  if (lost.value === 'expired') return enrollFailure({ state: 'expired', error: null });
  if (lost.value) return 'This setup no longer exists. Start again.';
  return step.value === 'failed' ? enrollFailure(session.value) : '';
});
const hostId = computed(() => session.value?.host_id ?? null);

watch(() => props.modelValue, async (open) => {
  if (!open) return;
  error.value = '';
  copied.value = false;
  launchUrl.value = null;
  sessionId.value = null;
  thisComputer.value = null;
  if (props.resumeSessionId) {
    sessionId.value = props.resumeSessionId;
    store.follow('session', props.resumeSessionId);
  }
  const bridge = tagsHostBridge();
  if (bridge) {
    try {
      thisComputer.value = await bridge.status();
    } catch {
      thisComputer.value = null;
    }
  }
});

watch(step, (s) => {
  if (s === 'done') {
    launchUrl.value = null;
    void store.loadHosts();
  }
});

function refused(e: unknown) {
  error.value = e instanceof TagsApiError
    ? setupErrorMessage(e.code, { fallback: e.message })
    : 'Cremind could not be reached. Try again.';
}

async function start() {
  busy.value = 'start';
  error.value = '';
  try {
    const { session: created, launchUrl: link } = await store.startSession('enroll_host');
    sessionId.value = created.id;
    launchUrl.value = link;
    store.follow('session', created.id);
  } catch (e) {
    refused(e);
  } finally {
    busy.value = '';
  }
}

async function prepareHere() {
  const bridge = tagsHostBridge();
  if (!bridge?.prepare) return;
  preparing.value = true;
  error.value = '';
  prepareLog.value = [];
  const stop = bridge.onPrepareLog?.((entry) => { prepareLog.value = [...prepareLog.value, entry.line].slice(-4); });
  try {
    const res = await bridge.prepare();
    if (!res.ok) error.value = res.error || 'The gateway components could not be installed.';
    thisComputer.value = await bridge.status();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'The gateway components could not be installed.';
  } finally {
    stop?.();
    preparing.value = false;
  }
}

async function openHere() {
  if (!launchUrl.value) return;
  const res = await openSetupLink(launchUrl.value);
  if (!res.ok) error.value = res.error || 'The Cremind app could not be opened on this computer.';
}

async function copyLink() {
  if (!launchUrl.value) return;
  try {
    await navigator.clipboard.writeText(launchUrl.value);
    copied.value = true;
    ElMessage.success('Setup link copied. It works once, for five minutes.');
  } catch {
    error.value = 'The link could not be copied here. Open it on this computer instead.';
  }
}

async function confirmWords() {
  if (!sessionId.value) return;
  busy.value = 'confirm';
  try {
    await store.confirmSession(sessionId.value);
  } catch (e) {
    refused(e);
  } finally {
    busy.value = '';
  }
}

async function cancel(): Promise<boolean> {
  const id = sessionId.value;
  if (!id || isSessionTerminal(session.value?.state) || session.value?.state === 'redeeming') return true;
  busy.value = 'cancel';
  try {
    await store.cancelSession(id);
    return true;
  } catch (e) {
    refused(e);
    return false;
  } finally {
    busy.value = '';
  }
}

function close() {
  if (sessionId.value) store.unfollow('session', sessionId.value);
  launchUrl.value = null;
  emit('update:modelValue', false);
}

async function requestClose() {
  if (step.value !== 'done' && step.value !== 'failed' && !(await cancel())) return;
  close();
}

function beforeClose(_done: () => void) {
  void requestClose();
}

function again() {
  if (sessionId.value) store.unfollow('session', sessionId.value);
  sessionId.value = null;
  launchUrl.value = null;
  void start();
}

function connectThere() {
  const id = hostId.value;
  close();
  if (id) emit('connect', id);
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    title="Set up a gateway computer"
    width="min(580px, calc(100vw - 24px))"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
  >
    <div class="step">
      <template v-if="step === 'intro'">
        <div class="intro">
          <Icon icon="mdi:laptop" class="intro-icon" aria-hidden="true" />
          <div>
            <p class="text">
              A gateway computer is the computer your gateway plugs into, with the Cremind app installed. Cremind
              drives the gateway from there, over the internet or your network — no port is opened on it.
            </p>
            <p class="text muted">
              Use this when Cremind runs somewhere that cannot see the gateway's USB port, such as a container or
              another machine.
            </p>
          </div>
        </div>
        <p v-if="thisComputer && !thisComputer.available && thisComputer.reason" class="text muted">{{ thisComputer.reason }}</p>
      </template>

      <template v-else-if="step === 'open'">
        <p class="text">Open this setup on the computer your gateway plugs into:</p>
        <div class="choices">
          <div class="choice">
            <Icon icon="mdi:monitor" class="choice-icon" aria-hidden="true" />
            <div class="choice-body">
              <strong>This computer</strong>
              <span class="muted">{{ thisComputer?.available ? 'The Cremind app sets it up.' : 'Needs the Cremind app installed here.' }}</span>
            </div>
            <ElButton
              v-if="thisComputer?.needsRuntime && tagsHostBridge()?.prepare"
              type="primary" :loading="preparing" @click="prepareHere"
            >
              Install gateway components
            </ElButton>
            <ElButton v-else type="primary" :disabled="!launchUrl || thisComputer?.available === false" @click="openHere">
              {{ tagsHostBridge() ? 'Set up this computer' : 'Open in the Cremind app' }}
            </ElButton>
          </div>
          <div class="choice">
            <Icon icon="mdi:laptop" class="choice-icon" aria-hidden="true" />
            <div class="choice-body">
              <strong>Another computer</strong>
              <span class="muted">Copy the link and open it there, or run <code>cremind tags host enroll</code> and paste it.</span>
            </div>
            <ElButton :disabled="!launchUrl" @click="copyLink">
              <Icon :icon="copied ? 'mdi:check' : 'mdi:content-copy'" class="btn-icon" aria-hidden="true" />
              {{ copied ? 'Copied' : 'Copy link' }}
            </ElButton>
          </div>
        </div>
        <p v-if="preparing || prepareLog.length" class="prepare-log" aria-live="polite">{{ prepareLog[prepareLog.length - 1] }}</p>
        <p v-if="!launchUrl" class="text muted">
          The link is shown only once. To open this setup on a computer, start again.
        </p>
        <p class="text muted" aria-live="polite"><Icon icon="mdi:loading" class="spin" aria-hidden="true" /> Waiting for the Cremind app…</p>
      </template>

      <template v-else-if="step === 'approve' || step === 'confirm'">
        <p class="text">
          <template v-if="step === 'approve'">Approve on <strong>{{ computerName }}</strong>. Before you do, check that it shows these words:</template>
          <template v-else>Does <strong>{{ computerName }}</strong> show the same four words?</template>
        </p>
        <div class="words" aria-label="Verification words">
          <span v-for="w in words" :key="w" class="word">{{ w }}</span>
        </div>
        <p v-if="step === 'confirm'" class="text muted">
          Only continue if they match: then that computer, and only that one, drives gateways for this profile.
        </p>
        <p v-else class="text muted" aria-live="polite"><Icon icon="mdi:loading" class="spin" aria-hidden="true" /> Waiting for approval on {{ computerName }}…</p>
      </template>

      <template v-else-if="step === 'finish'">
        <p class="text" aria-live="polite"><Icon icon="mdi:loading" class="spin" aria-hidden="true" /> Finishing on {{ computerName }}…</p>
      </template>

      <ElResult
        v-else-if="step === 'done'"
        icon="success"
        :title="`${computerName} is a gateway computer`"
        sub-title="Plug your gateway into it, then connect it from there. The Cremind app keeps it running."
      />

      <ElResult v-else icon="error" title="The computer was not set up" :sub-title="failure" />

      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </div>

    <template #footer>
      <template v-if="step === 'intro'">
        <ElButton @click="close">Cancel</ElButton>
        <ElButton type="primary" :loading="busy === 'start'" @click="start">Start</ElButton>
      </template>
      <template v-else-if="step === 'confirm'">
        <ElButton :loading="busy === 'cancel'" @click="requestClose">No, cancel</ElButton>
        <ElButton type="primary" :loading="busy === 'confirm'" @click="confirmWords">Yes, they match</ElButton>
      </template>
      <template v-else-if="step === 'done'">
        <ElButton @click="close">Done</ElButton>
        <ElButton v-if="hostId" type="primary" @click="connectThere">Connect a gateway there</ElButton>
      </template>
      <template v-else-if="step === 'failed'">
        <ElButton @click="close">Close</ElButton>
        <ElButton type="primary" :loading="busy === 'start'" @click="again">Start again</ElButton>
      </template>
      <template v-else>
        <ElButton :loading="busy === 'cancel'" @click="requestClose">Cancel setup</ElButton>
      </template>
    </template>
  </ElDialog>
</template>

<style scoped>
.step { display: flex; flex-direction: column; gap: 12px; }
.text { margin: 0; font-size: 0.88rem; line-height: 1.55; color: var(--text-primary); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.text.muted, .muted { color: var(--text-secondary); }
.intro { display: flex; gap: 14px; align-items: flex-start; }
.intro > div { display: flex; flex-direction: column; gap: 8px; }
.intro-icon { font-size: 36px; color: var(--primary-color); flex-shrink: 0; }
.choices { display: flex; flex-direction: column; gap: 8px; }
.choice {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 10px 12px;
  border: 1px solid var(--border-color); border-radius: 8px;
}
.choice-icon { font-size: 24px; color: var(--primary-color); flex-shrink: 0; }
.choice-body { flex: 1; min-width: 200px; display: flex; flex-direction: column; gap: 2px; font-size: 0.86rem; color: var(--text-primary); }
.choice-body .muted { font-size: 0.8rem; }
.choice-body code { font-size: 0.78rem; }
.words { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; padding: 6px 0; }
.word {
  font-size: 1.05rem; font-weight: 600; letter-spacing: 0.02em; padding: 6px 12px; border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--primary-color) 45%, transparent);
  background: color-mix(in srgb, var(--primary-color) 8%, var(--surface-color)); color: var(--text-primary);
}
.error { margin: 0; font-size: 0.85rem; color: var(--el-color-danger); }
.prepare-log { margin: 0; font-size: 0.76rem; color: var(--text-tertiary); font-family: var(--font-mono, monospace); overflow-wrap: anywhere; }
.btn-icon { margin-right: 6px; }
.spin { animation: en-spin 1s linear infinite; }
@keyframes en-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
