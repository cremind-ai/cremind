<script setup lang="ts">
/**
 * Add tag (connect-setup.md §8.3): the tag's setup code → every ready bridge
 * listens for it ("Waiting for the tag to wake" is normal: a tag checks in
 * about every 30 s) → with exactly one bridge that can take it the tag is
 * paired at once, else the person chooses (recommended preselected) → "Tag
 * ready", with Send test.
 *
 * The profile's first tag: when Tags are off for this profile, the success
 * step offers "Show this profile's activity on this tag", on by default, and
 * turns Tags on when the dialog is finished. The preference is never touched
 * otherwise (re-pairing, a later tag, or the switch turned off).
 */
import { computed, nextTick, ref, watch } from 'vue';
import { ElButton, ElDialog, ElInput, ElMessage, ElResult, ElSwitch } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../../stores/tags';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { useDevicePairing, type PairingResume } from '../../../composables/useDevicePairing';
import { TagsApiError } from '../../../services/tagsApi';
import { setupDeviceTitle, setupErrorMessage } from '../../../utils/tagsSetupFormat';
import SetupCodeInput from './SetupCodeInput.vue';
import DeviceSetupSteps from './DeviceSetupSteps.vue';

const props = defineProps<{ modelValue: boolean; resume?: PairingResume | null }>();
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>();

const tagsStore = useTagsStore();
const setup = useTagsSetupStore();
const {
  codeText, parsed, name, codeError, choiceError, discovery, pairing, candidates, chosen, busy, stopping, step,
  problem, lookingFor, find, pair, cancel, again, resume, reset,
} = useDevicePairing('tag');

const codeInput = ref<InstanceType<typeof SetupCodeInput> | null>(null);
const turnOn = ref(true);
const finishing = ref(false);
const testing = ref(false);

const device = computed(() => pairing.value?.device ?? null);
const title = computed(() => (device.value ? setupDeviceTitle(device.value) : 'Your tag'));
/** Only for the profile's first tag, and only while Tags are off. */
const offerTurnOn = computed(() => !!pairing.value?.first_tag && tagsStore.settings?.enabled === false);
/** Promise cards only when Tags are on (the first-tag switch speaks for itself). */
const doneText = computed(() => (tagsStore.settings?.enabled
  ? `${title.value} is added. Its screen shows your cards from now on.`
  : `${title.value} is added and ready.`));

watch(() => props.modelValue, async (open) => {
  if (!open) return;
  reset();
  turnOn.value = true;
  if (props.resume) resume(props.resume);
  await nextTick();
  if (step.value === 'code') codeInput.value?.focus();
});

async function sendTest() {
  const id = device.value?.id;
  if (!id) return;
  testing.value = true;
  try {
    await setup.sendTest(id);
    ElMessage.success('Test card on its way. It shows at the tag\'s next check-in, within about 30 seconds.');
  } catch (e) {
    ElMessage.error(e instanceof TagsApiError ? setupErrorMessage(e.code, { role: 'tag', fallback: e.message }) : 'The test card could not be sent.');
  } finally {
    testing.value = false;
  }
}

/** Same code again; a setup resumed after a refresh has no code, so back to the label. */
function searchAgain() {
  again();
  if (parsed.value) void find();
}

function close() {
  codeInput.value?.stopCamera();
  reset();
  emit('update:modelValue', false);
}

/** Done (or closing the success step): apply the first-tag switch, then close. */
async function finish() {
  if (offerTurnOn.value && turnOn.value) {
    finishing.value = true;
    try {
      await tagsStore.saveSettings({ enabled: true });
      ElMessage.success('Tags turned on for this profile');
    } catch (e) {
      ElMessage.error(e instanceof Error ? e.message : 'Tags could not be turned on');
      finishing.value = false;
      return;
    }
    finishing.value = false;
  }
  close();
}

function beforeClose(_done: () => void) {
  if (step.value === 'done') void finish();
  else close();
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    title="Add a tag"
    width="min(560px, calc(100vw - 24px))"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
  >
    <DeviceSetupSteps
      role="tag"
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
          Find the label on the back of the tag. Scan its QR code, upload a photo of it, or type the
          code. Keep the tag close to one of your bridges.
        </p>
        <SetupCodeInput
          ref="codeInput"
          v-model="codeText"
          role="tag"
          :server-error="codeError"
          @parsed="(v) => (parsed = v)"
          @submit="find()"
        />
        <label class="field-label" for="add-tag-name">Name <span class="optional">(optional)</span></label>
        <ElInput id="add-tag-name" v-model="name" maxlength="128" placeholder="Kitchen" @keyup.enter="parsed && find()" />
      </template>

      <template #done>
        <ElResult icon="success" title="Tag ready" :sub-title="doneText">
          <template #extra>
            <div class="done-actions">
              <ElButton v-if="device?.id" :loading="testing" @click="sendTest">
                <Icon icon="mdi:send-outline" class="btn-icon" /> Send test
              </ElButton>
              <ElButton type="primary" :loading="finishing" @click="finish">Done</ElButton>
            </div>
          </template>
        </ElResult>
        <div v-if="offerTurnOn" class="first-tag">
          <div>
            <label class="first-tag-title" for="first-tag-switch">Show this profile's activity on this tag</label>
            <p class="first-tag-hint">
              Replies, questions waiting for you and automation results become cards on your tags. You
              can change this any time in Settings → Tags.
            </p>
          </div>
          <ElSwitch id="first-tag-switch" v-model="turnOn" aria-label="Show this profile's activity on this tag" />
        </div>
      </template>
    </DeviceSetupSteps>

    <template #footer>
      <template v-if="step === 'code'">
        <ElButton @click="close">Cancel</ElButton>
        <ElButton type="primary" :disabled="!parsed" :loading="busy === 'find'" @click="find()">Find tag</ElButton>
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
.field-label { display: block; margin: 14px 0 6px; font-size: 0.82rem; font-weight: 600; color: var(--text-secondary); }
.optional { font-weight: 400; color: var(--text-tertiary); }
.done-actions { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
.done-actions .el-button + .el-button { margin-left: 0; }
.btn-icon { margin-right: 6px; }
.first-tag {
  display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 12px 14px;
  border-radius: 8px; border: 1px solid var(--border-color); background: var(--surface-color);
}
.first-tag-title { font-weight: 600; font-size: 0.9rem; color: var(--text-primary); }
.first-tag-hint { margin: 4px 0 0; font-size: 0.8rem; color: var(--text-tertiary); line-height: 1.45; }
</style>
