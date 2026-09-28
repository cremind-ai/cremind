<script setup lang="ts">
/**
 * Add bridge (connect-setup.md §8.2): which gateway it joins (asked only when
 * there are several) → the bridge's setup code → the gateway listens for it
 * → pair (at once when exactly one gateway heard it) → "Bridge ready". A
 * bridge carries updates to tags farther from the gateway (an older gateway,
 * which reaches no tag itself, needs one for every tag).
 *
 * A bridge whose fonts do not match gets "This bridge needs a font update:
 * connect it to this computer with USB" instead of a technical error.
 */
import { computed, nextTick, ref, watch } from 'vue';
import { ElButton, ElDialog, ElInput, ElOption, ElResult, ElSelect } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { useDevicePairing, type PairingResume } from '../../../composables/useDevicePairing';
import { connectionTitle, setupDeviceTitle } from '../../../utils/tagsSetupFormat';
import SetupCodeInput from './SetupCodeInput.vue';
import DeviceSetupSteps from './DeviceSetupSteps.vue';

const props = defineProps<{ modelValue: boolean; resume?: PairingResume | null }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'add-tag'): void;
}>();

const setup = useTagsSetupStore();
const {
  codeText, parsed, name, codeError, choiceError, discovery, pairing, candidates, chosen, busy, stopping, step,
  problem, lookingFor, find, pair, cancel, again, resume, reset,
} = useDevicePairing('bridge');

const codeInput = ref<InstanceType<typeof SetupCodeInput> | null>(null);
const gatewayId = ref('');

const gateways = computed(() => setup.readiness.connected);
const device = computed(() => pairing.value?.device ?? null);
const title = computed(() => (device.value ? setupDeviceTitle(device.value) : 'Your bridge'));
const needsFonts = computed(() => device.value?.fontpack_ok === false);

watch(() => props.modelValue, async (open) => {
  if (!open) return;
  reset();
  gatewayId.value = gateways.value.length === 1 ? gateways.value[0].id : '';
  if (props.resume) resume(props.resume);
  await nextTick();
  if (step.value === 'code' && gateways.value.length <= 1) codeInput.value?.focus();
});

function search() {
  void find(gatewayId.value || undefined);
}

/** Same code again; a setup resumed after a refresh has no code, so back to the label. */
function searchAgain() {
  again();
  if (parsed.value && gatewayId.value) search();
}

function close() {
  codeInput.value?.stopCamera();
  reset();
  emit('update:modelValue', false);
}

function addTag() {
  close();
  emit('add-tag');
}

function beforeClose(_done: () => void) {
  close();
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    title="Add a bridge"
    width="min(560px, calc(100vw - 24px))"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
  >
    <DeviceSetupSteps
      role="bridge"
      :step="step"
      :candidates="candidates"
      :recommended="discovery?.recommended ?? null"
      :chosen="chosen"
      :pairing="pairing"
      :problem="problem"
      :busy="busy"
      :choice-error="choiceError"
      :looking-for="lookingFor"
      :stopping="stopping"
      @update:chosen="(v) => (chosen = v)"
      @pair="chosen && pair(chosen)"
      @search-again="searchAgain"
      @again="again()"
    >
      <template #code>
        <p class="intro-text">
          Plug the bridge into power near the gateway. Then scan the QR code on its label, upload a
          photo of it, or type the code.
        </p>
        <div v-if="gateways.length > 1" class="field">
          <label class="field-label" for="add-bridge-gateway">Gateway</label>
          <ElSelect id="add-bridge-gateway" v-model="gatewayId" placeholder="Which gateway should it join?" class="full">
            <ElOption
              v-for="g in gateways" :key="g.id" :value="g.id"
              :label="g.computer?.name ? `${connectionTitle(g)} on ${g.computer.name}` : connectionTitle(g)"
            />
          </ElSelect>
          <p class="field-hint">The bridge joins this gateway's network. Pick the one closest to it.</p>
        </div>
        <SetupCodeInput
          ref="codeInput"
          v-model="codeText"
          role="bridge"
          :server-error="codeError"
          @parsed="(v) => (parsed = v)"
          @submit="gatewayId && search()"
        />
        <label class="field-label" for="add-bridge-name">Name <span class="optional">(optional)</span></label>
        <ElInput id="add-bridge-name" v-model="name" maxlength="128" placeholder="Hall" @keyup.enter="parsed && gatewayId && search()" />
      </template>

      <template #done>
        <ElResult icon="success" title="Bridge ready" :sub-title="`${title} is added. Tags near it can connect through it now.`">
          <template #extra>
            <div class="done-actions">
              <ElButton type="primary" @click="addTag">
                <Icon icon="mdi:plus" class="btn-icon" /> Add tag
              </ElButton>
              <ElButton @click="close">Done</ElButton>
            </div>
          </template>
        </ElResult>
        <p v-if="needsFonts" class="font-note" role="status">
          <Icon icon="mdi:format-font" aria-hidden="true" />
          This bridge needs a font update: connect it to this computer with USB.
        </p>
      </template>
    </DeviceSetupSteps>

    <template #footer>
      <template v-if="step === 'code'">
        <ElButton @click="close">Cancel</ElButton>
        <ElButton type="primary" :disabled="!parsed || !gatewayId" :loading="busy === 'find'" @click="search">Find bridge</ElButton>
      </template>
      <template v-else-if="step === 'searching' || step === 'choose'">
        <ElButton @click="again()">Back</ElButton>
        <ElButton @click="close">Cancel</ElButton>
      </template>
      <template v-else-if="step === 'pairing'">
        <ElButton v-if="!stopping" :loading="busy === 'cancel'" @click="cancel()">Stop adding</ElButton>
        <ElButton @click="close">Close</ElButton>
      </template>
      <template v-else-if="step === 'problem'">
        <ElButton @click="close">Close</ElButton>
      </template>
    </template>
  </ElDialog>
</template>

<style scoped>
.intro-text { margin: 0 0 10px; font-size: 0.88rem; line-height: 1.55; color: var(--text-primary); }
.field { margin-bottom: 12px; }
.field-label { display: block; margin: 14px 0 6px; font-size: 0.82rem; font-weight: 600; color: var(--text-secondary); }
.field .field-label { margin-top: 0; }
.field-hint { margin: 6px 0 0; font-size: 0.78rem; color: var(--text-tertiary); }
.optional { font-weight: 400; color: var(--text-tertiary); }
.full { width: 100%; }
.done-actions { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
.done-actions .el-button + .el-button { margin-left: 0; }
.btn-icon { margin-right: 6px; }
.font-note {
  display: flex; align-items: flex-start; gap: 8px; margin: 0; padding: 10px 12px; border-radius: 8px;
  font-size: 0.86rem; line-height: 1.45; color: var(--text-primary);
  border: 1px solid color-mix(in srgb, var(--el-color-warning) 55%, transparent);
  background: color-mix(in srgb, var(--el-color-warning) 12%, var(--surface-color));
}
</style>
