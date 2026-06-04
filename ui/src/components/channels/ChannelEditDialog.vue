<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import {
  ElButton, ElDialog, ElForm, ElFormItem, ElInput, ElOption,
  ElRadioButton, ElRadioGroup, ElSelect, ElTag,
} from 'element-plus';
import type {
  ChannelCatalogEntry, ChannelCatalogMode, CreateChannelPayload,
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
const dialogAuthMode = ref<'none' | 'otp' | 'password'>('none');
const dialogResponseMode = ref<'detail' | 'normal'>('normal');
const dialogPassword = ref<string>('');
const dialogConfig = reactive<Record<string, any>>({});

const catalogEntry = computed<ChannelCatalogEntry | null>(() => {
  return props.catalog[props.channelType] || null;
});

const modeEntry = computed<ChannelCatalogMode | null>(() => {
  const entry = catalogEntry.value;
  if (!entry) return null;
  return entry.modes.find((m) => m.id === dialogMode.value) || entry.modes[0] || null;
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
      dialogAuthMode.value = (props.initial.auth_mode || entry.auth_modes?.[0] || 'none') as any;
      dialogResponseMode.value = (props.initial.response_mode || entry.default_response_mode || 'normal') as any;
      dialogPassword.value = (props.initial.config?.password as string) || '';
      for (const k of Object.keys(dialogConfig)) delete dialogConfig[k];
      for (const [k, v] of Object.entries(props.initial.config || {})) {
        if (k === 'password') continue;
        dialogConfig[k] = v;
      }
    } else {
      // Prefer the first implemented mode as the default — picking an
      // unimplemented one and letting the user hit Connect only to get a
      // backend 400 is bad UX.
      const firstImplemented = entry.modes.find((m) => m.implemented !== false);
      dialogMode.value = (firstImplemented || entry.modes[0])?.id || 'bot';
      dialogAuthMode.value = (entry.auth_modes?.[0] as any) || 'none';
      dialogResponseMode.value = (entry.default_response_mode as any) || 'normal';
      dialogPassword.value = '';
      for (const k of Object.keys(dialogConfig)) delete dialogConfig[k];
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
  if (dialogAuthMode.value === 'password' && dialogPassword.value) {
    config.password = dialogPassword.value;
  }
  emit('submit', {
    channel_type: props.channelType,
    mode: dialogMode.value,
    auth_mode: dialogAuthMode.value,
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

        <ElFormItem
          v-if="(catalogEntry.auth_modes?.length || 0) > 1"
          label="Authentication"
        >
          <ElSelect v-model="dialogAuthMode" style="width: 100%">
            <ElOption
              v-for="m in (catalogEntry.auth_modes || ['none'])"
              :key="m" :value="m" :label="m"
            />
          </ElSelect>
        </ElFormItem>

        <ElFormItem v-if="dialogAuthMode === 'password'" label="Password">
          <ElInput
            v-model="dialogPassword"
            type="password"
            show-password
            placeholder="Senders must reply with this exact password to chat"
          />
        </ElFormItem>

        <ElFormItem label="Reply detail">
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
