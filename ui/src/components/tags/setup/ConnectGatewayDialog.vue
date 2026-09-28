<script setup lang="ts">
/**
 * Connect gateway (connect-setup.md §8.1): plug the gateway into this
 * computer → a `connect_gateway` setup session → Cremind Connect opens from
 * its link → the person approves in the Connect window and confirms here that
 * both show the same four words → Connect finishes → "Gateway connected".
 *
 * Closing the dialog before Connect redeemed the session cancels it; after
 * that the setup finishes in the background. A setup still running after a
 * page refresh is resumed from its id (`resumeSessionId`).
 *
 * In the desktop app with a local backend, the success step also offers to
 * keep Cremind running in the background, so tags keep updating after the
 * window closes.
 */
import { computed, nextTick, ref, watch } from 'vue';
import { ElButton, ElDialog, ElMessage, ElResult } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { useSettingsStore } from '../../../stores/settings';
import { useConnectSession } from '../../../composables/useConnectSession';
import { backgroundBridge, isLocalBackend } from '../../../services/connectBridge';
import { connectionTitle, shortSuffix } from '../../../utils/tagsSetupFormat';
import ConnectSessionSteps from './ConnectSessionSteps.vue';

const props = defineProps<{ modelValue: boolean; resumeSessionId?: string | null }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'add-bridge'): void;
}>();

const store = useTagsSetupStore();
const settingsStore = useSettingsStore();
const connect = useConnectSession();
const plugButton = ref<InstanceType<typeof ElButton> | null>(null);

const offerBackground = ref(false);
const enablingBackground = ref(false);
const backgroundDone = ref(false);

const create = () => store.startSession('connect_gateway');

const connection = computed(() => {
  const id = connect.session.value?.companion_id;
  return id ? store.connections.find((c) => c.id === id) ?? null : null;
});
const doneText = computed(() => {
  const s = connect.session.value;
  const name = connection.value ? connectionTitle(connection.value) : `Gateway ${shortSuffix(s?.gateway?.short_id)}`;
  return `${name} is connected through Cremind Connect on ${s?.computer?.name || 'this computer'}.`;
});

watch(() => props.modelValue, async (open) => {
  if (!open) return;
  offerBackground.value = false;
  backgroundDone.value = false;
  if (props.resumeSessionId) connect.attach(props.resumeSessionId);
  await nextTick();
  if (!props.resumeSessionId) (plugButton.value?.$el as HTMLElement | undefined)?.focus();
});

watch(() => connect.step.value, async (step) => {
  if (step !== 'done') return;
  const bg = backgroundBridge();
  if (!bg || !isLocalBackend(settingsStore.agentUrl)) return;
  try {
    const status = await bg.status();
    offerBackground.value = status.applicable && !status.enabled;
  } catch {
    offerBackground.value = false;
  }
});

async function start() {
  await connect.begin(create);
}

async function retry() {
  await connect.begin(create);
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
  connect.detach();
  emit('update:modelValue', false);
}

/** The × and Esc: cancel what Connect has not taken over yet, then close. */
async function requestClose() {
  if (connect.cancellable.value && !(await connect.cancel())) return;
  close();
}

/** The dialog closes through the parent's v-model, never through `done`. */
function beforeClose(_done: () => void) {
  void requestClose();
}

async function cancelSetup() {
  if (await connect.cancel()) close();
}

function addBridge() {
  close();
  emit('add-bridge');
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    title="Connect a gateway"
    width="min(560px, calc(100vw - 24px))"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
  >
    <ConnectSessionSteps
      :session="connect.session.value"
      :step="connect.step.value"
      operation="connect_gateway"
      :waited-long="connect.waitedLong.value"
      :has-link="connect.hasLink.value"
      :busy="connect.busy.value"
      :action-error="connect.actionError.value"
      :launch-error="connect.launchError.value"
      @reopen="connect.reopen(create)"
      @confirm="connect.confirm()"
      @cancel="cancelSetup"
      @retry="retry"
    >
      <template #plug>
        <div class="intro">
          <Icon icon="mdi:usb-port" class="intro-icon" aria-hidden="true" />
          <div>
            <h3 class="intro-title">Plug the gateway into this computer</h3>
            <p class="intro-text">
              Use its USB cable. Cremind Connect, a small app on this computer, then looks after the
              gateway and keeps your tags updated, even when this page is closed.
            </p>
            <p class="intro-text muted">If Cremind Connect is not installed yet, the next step helps you install it.</p>
          </div>
        </div>
        <p v-if="connect.actionError.value" class="error" role="alert">{{ connect.actionError.value }}</p>
      </template>

      <template #done>
        <ElResult icon="success" title="Gateway connected" :sub-title="doneText">
          <template #extra>
            <div class="done-actions">
              <ElButton type="primary" @click="addBridge">
                <Icon icon="mdi:plus" class="btn-icon" /> Add bridge
              </ElButton>
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
      </template>
    </ConnectSessionSteps>

    <template #footer>
      <template v-if="connect.step.value === 'plug'">
        <ElButton @click="close">Cancel</ElButton>
        <ElButton ref="plugButton" type="primary" :loading="connect.busy.value === 'start'" @click="start">
          It is plugged in, continue
        </ElButton>
      </template>
      <template v-else-if="connect.cancellable.value && connect.step.value !== 'confirm'">
        <ElButton :loading="connect.busy.value === 'cancel'" @click="cancelSetup">Cancel setup</ElButton>
      </template>
      <template v-else-if="connect.step.value === 'finish'">
        <ElButton @click="close">Close (setup continues)</ElButton>
      </template>
      <template v-else-if="connect.step.value === 'failed'">
        <ElButton @click="close">Close</ElButton>
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
</style>
