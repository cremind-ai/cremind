<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import {
  ElButton, ElDialog, ElForm, ElFormItem, ElInput, ElOption,
  ElRadioButton, ElRadioGroup, ElSelect, ElTag,
} from 'element-plus';
import NotificationFilterEditor from './NotificationFilterEditor.vue';
import {
  defaultNotificationFilter,
  type ChannelCatalogEntry, type ChannelCatalogMode, type CreateChannelPayload,
} from '../../services/channelApi';

const props = defineProps<{
  modelValue: boolean;
  catalog: Record<string, ChannelCatalogEntry>;
  channelType: string;
  // Pre-fill values when editing an existing channel or draft. Pass null
  // (or omit) to add a fresh entry.
  initial?: CreateChannelPayload | null;
  // Editing changes the dialog title and primary-button label.
  editing?: boolean;
  // Forwarded to the primary button so the parent can show a spinner
  // while a server request is in flight.
  submitting?: boolean;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'submit', payload: CreateChannelPayload): void;
}>();

const dialogMode = ref<string>('');
const dialogResponseMode = ref<'detail' | 'normal'>('normal');
const dialogConfig = reactive<Record<string, any>>({});

const catalogEntry = computed<ChannelCatalogEntry | null>(() => {
  return props.catalog[props.channelType] || null;
});

const modeEntry = computed<ChannelCatalogMode | null>(() => {
  const entry = catalogEntry.value;
  if (!entry) return null;
  return entry.modes.find((m) => m.id === dialogMode.value) || entry.modes[0] || null;
});

// Notification mode has no conversation: it shows a filter editor instead of
// auth/response controls, which are irrelevant to a push-only channel.
const isNotification = computed(() => dialogMode.value === 'notification');

// Access-authentication methods — the SAME set for every mode (stored in
// config.subscribe_auth). Labels are phrased per mode: notification gates who
// may *subscribe*, conversational gates who may *chat*.
const authOptions = computed(() => (isNotification.value ? [
  { value: 'open', label: 'Open — anyone can /start to subscribe' },
  { value: 'passcode', label: 'Passcode — /start <passcode> required' },
  { value: 'otp', label: 'One-time code — you share a code per subscriber' },
  { value: 'approval', label: 'Approval — you approve each subscriber' },
  { value: 'allowlist', label: 'Allowlist only — no self-subscribe' },
] : [
  { value: 'open', label: 'Open — anyone can message the bot' },
  { value: 'passcode', label: 'Passcode — sender sends a passcode to chat' },
  { value: 'otp', label: 'One-time code — you relay a code to the sender' },
  { value: 'approval', label: 'Approval — you approve each sender before they can chat' },
  { value: 'allowlist', label: 'Allowlist only — only approved senders may chat' },
]));

// Seed the notification filter the first time the user switches to notification
// mode so the editor has an object to bind to (create path clears dialogConfig).
// subscribe_auth is mode-independent and is seeded on dialog open below.
watch(isNotification, (on) => {
  if (on && !dialogConfig.notification_filter) {
    dialogConfig.notification_filter = defaultNotificationFilter();
  }
});

// Reset / pre-fill the form whenever the dialog opens. Watching
// modelValue rather than the props themselves avoids spurious resets
// while the user is typing — once open, parent updates to ``initial``
// or ``channelType`` are intentionally ignored.
watch(
  () => props.modelValue,
  (open) => {
    if (!open) return;
    const entry = props.catalog[props.channelType];
    if (!entry) return;

    if (props.initial) {
      dialogMode.value = props.initial.mode || entry.modes[0]?.id || 'bot';
      dialogResponseMode.value = (props.initial.response_mode || entry.default_response_mode || 'normal') as any;
      const initConfig = (props.initial.config || {}) as Record<string, any>;
      for (const k of Object.keys(dialogConfig)) delete dialogConfig[k];
      for (const [k, v] of Object.entries(initConfig)) {
        if (k === 'password') continue;  // legacy conversational key → mapped below
        dialogConfig[k] = v;
      }
      // Legacy conversational passcode lived in config.password.
      if (!dialogConfig.subscribe_passcode && initConfig.password) {
        dialogConfig.subscribe_passcode = initConfig.password;
      }
      // Resolve the unified access method, mirroring the server's back-compat:
      // explicit config.subscribe_auth → legacy auth_mode column → a configured
      // passcode → open.
      if (!dialogConfig.subscribe_auth) {
        const legacy = (props.initial.auth_mode || '').toLowerCase();
        dialogConfig.subscribe_auth =
          legacy === 'password' ? 'passcode'
            : legacy === 'otp' ? 'otp'
              : dialogConfig.subscribe_passcode ? 'passcode'
                : 'open';
      }
    } else {
      // Prefer the first implemented mode as the default — picking an
      // unimplemented one and letting the user hit Connect only to get a
      // backend 400 is bad UX.
      const firstImplemented = entry.modes.find((m) => m.implemented !== false);
      dialogMode.value = (firstImplemented || entry.modes[0])?.id || 'bot';
      dialogResponseMode.value = (entry.default_response_mode as any) || 'normal';
      for (const k of Object.keys(dialogConfig)) delete dialogConfig[k];
      dialogConfig.subscribe_auth = 'open';
    }
  },
  { immediate: true },
);

const title = computed(() => {
  if (props.editing) return 'Edit channel';
  return `Connect ${catalogEntry.value?.display_name || ''}`;
});

const primaryLabel = computed(() => (props.editing ? 'Save' : 'Connect'));

function handleClose() {
  emit('update:modelValue', false);
}

function handleSubmit() {
  if (!catalogEntry.value) return;
  const config: Record<string, any> = { ...dialogConfig };
  // Access auth (config.subscribe_auth) is shared by every mode.
  config.subscribe_auth = dialogConfig.subscribe_auth || 'open';
  if (isNotification.value) {
    config.notification_filter = dialogConfig.notification_filter || defaultNotificationFilter();
  } else {
    // Never leak a notification-only filter into a conversational channel.
    delete config.notification_filter;
  }
  emit('submit', {
    channel_type: props.channelType,
    mode: dialogMode.value,
    // auth_mode is deprecated (superseded by config.subscribe_auth); send a
    // neutral value so the column no longer drives the gate.
    auth_mode: 'none',
    response_mode: dialogResponseMode.value,
    enabled: true,
    config,
  });
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="title"
    width="540px"
    @update:model-value="(v) => emit('update:modelValue', v)"
    @close="handleClose"
  >
    <div v-if="catalogEntry">
      <ElForm label-position="top" @submit.prevent>
        <ElFormItem
          v-if="catalogEntry.modes.length > 1"
          label="Mode"
        >
          <ElRadioGroup v-model="dialogMode">
            <ElRadioButton
              v-for="m in catalogEntry.modes"
              :key="m.id"
              :value="m.id"
              :disabled="m.implemented === false"
            >
              {{ m.label }}
              <ElTag
                v-if="m.implemented === false"
                type="info"
                size="small"
                effect="plain"
                style="margin-left: 6px"
              >coming soon</ElTag>
            </ElRadioButton>
          </ElRadioGroup>
        </ElFormItem>

        <div v-if="modeEntry?.instructions" class="instructions">
          <pre>{{ modeEntry.instructions }}</pre>
        </div>

        <ElFormItem
          v-for="(field, name) in modeEntry?.fields || {}"
          v-show="name !== 'subscribe_passcode'"
          :key="name"
          :label="field.description"
        >
          <ElInput
            v-model="dialogConfig[name]"
            :placeholder="(field as any).required ? 'Required' : 'Optional'"
            :type="(field as any).secret ? 'password' : 'text'"
            :show-password="!!(field as any).secret"
          />
        </ElFormItem>

        <!-- Access authentication — the same control for every mode
             (stored in config.subscribe_auth). -->
        <ElFormItem :label="isNotification ? 'Subscription authentication' : 'Authentication'">
          <ElSelect v-model="dialogConfig.subscribe_auth" style="width: 100%">
            <ElOption
              v-for="opt in authOptions"
              :key="opt.value"
              :value="opt.value"
              :label="opt.label"
            />
          </ElSelect>
        </ElFormItem>

        <ElFormItem
          v-if="dialogConfig.subscribe_auth === 'passcode'"
          label="Passcode"
        >
          <ElInput
            v-model="dialogConfig.subscribe_passcode"
            type="password"
            show-password
            :placeholder="isNotification
              ? 'Senders must send /start <passcode> to subscribe'
              : 'Senders must send this passcode to start chatting'"
          />
        </ElFormItem>

        <ElFormItem v-if="isNotification" label="Notification filter">
          <NotificationFilterEditor v-model="dialogConfig.notification_filter" />
        </ElFormItem>

        <ElFormItem v-else label="Reply detail">
          <ElRadioGroup v-model="dialogResponseMode">
            <ElRadioButton value="normal">Final answer only</ElRadioButton>
            <ElRadioButton value="detail">Include thinking</ElRadioButton>
          </ElRadioGroup>
        </ElFormItem>
      </ElForm>
    </div>

    <template #footer>
      <ElButton @click="handleClose">Cancel</ElButton>
      <ElButton type="primary" :loading="submitting" @click="handleSubmit">
        {{ primaryLabel }}
      </ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.instructions {
  background: var(--hover-bg); border-radius: 6px; padding: 10px 12px;
  margin-bottom: 12px;
}
.instructions pre {
  margin: 0; white-space: pre-wrap; font-family: inherit;
  font-size: 0.85rem; color: var(--text-secondary);
}
</style>
