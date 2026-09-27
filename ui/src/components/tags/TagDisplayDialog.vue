<script setup lang="ts">
/**
 * "Display note": pin a short note on one tag (POST …/display). The server
 * sanitises the text and refuses anything that looks like a one-time code
 * (422 `otp_refused`) — shown here in the dialog, next to the text, so the
 * user can edit and resend. `clear_pending` (409) means the screen is still
 * being blanked after a change of owner.
 */
import { computed, ref, watch } from 'vue';
import {
  ElButton, ElDialog, ElInput, ElMessage, ElOption, ElSelect, ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../stores/tags';
import { TagsApiError, type TagDelivery, type TagDevice } from '../../services/tagsApi';
import { cardIconGlyph, deviceTitle, iconLabel } from '../../utils/tagsFormat';

const props = defineProps<{
  modelValue: boolean;
  device: TagDevice | null;
  icons: string[];
}>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'sent', delivery: TagDelivery): void;
}>();

const store = useTagsStore();

const TTL_OPTIONS = [
  { label: '15 minutes', value: 900 },
  { label: '1 hour', value: 3600 },
  { label: '4 hours', value: 4 * 3600 },
  { label: '12 hours', value: 12 * 3600 },
  { label: '1 day', value: 86400 },
  { label: '3 days', value: 3 * 86400 },
  { label: '7 days', value: 7 * 86400 },
];

const title = ref('');
const body = ref('');
const icon = ref('push_pin');
const ttl = ref(86400);
const sending = ref(false);
/** A refusal to show inside the dialog: { code, message }. */
const problem = ref<{ code: string; message: string } | null>(null);

const iconChoices = computed(() => (props.icons.length ? props.icons : ['push_pin', 'info']));

watch(() => props.modelValue, (open) => {
  if (!open) return;
  title.value = '';
  body.value = '';
  icon.value = iconChoices.value.includes('push_pin') ? 'push_pin' : iconChoices.value[0];
  ttl.value = 86400;
  problem.value = null;
});

// Editing the text clears a refusal about it.
watch([title, body], () => { if (problem.value?.code === 'otp_refused') problem.value = null; });

const canSend = computed(() => !!title.value.trim() && !sending.value);

function close() {
  emit('update:modelValue', false);
}

async function send() {
  const device = props.device;
  if (!device || !canSend.value) return;
  sending.value = true;
  problem.value = null;
  try {
    const delivery = await store.display(device.id, {
      title: title.value.trim(),
      ...(body.value.trim() ? { body: body.value.trim() } : {}),
      icon: icon.value,
      ttl_s: ttl.value,
    });
    ElMessage.success(`Note queued for ${deviceTitle(device)}`);
    emit('sent', delivery);
    close();
  } catch (e) {
    if (e instanceof TagsApiError && e.code === 'otp_refused') {
      problem.value = {
        code: e.code,
        message: 'This text looks like it contains a one-time code. Codes are never shown on a tag — remove the code and send again.',
      };
    } else if (e instanceof TagsApiError && e.code === 'clear_pending') {
      problem.value = { code: e.code, message: e.message };
    } else if (e instanceof TagsApiError && e.code && e.code.startsWith('invalid_')) {
      problem.value = { code: e.code, message: e.message };
    } else {
      ElMessage.error(e instanceof Error ? e.message : 'Failed to send the note');
    }
  } finally {
    sending.value = false;
  }
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="device ? `Display a note on ${deviceTitle(device)}` : 'Display a note'"
    width="520px"
    append-to-body
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <div class="note-form">
      <div class="field">
        <label class="field-label" for="tag-note-title">Title</label>
        <ElInput
          id="tag-note-title"
          v-model="title"
          maxlength="120"
          show-word-limit
          placeholder="Back at 3 pm"
          @keyup.enter="send"
        />
      </div>
      <div class="field">
        <label class="field-label" for="tag-note-body">Text <span class="optional">(optional)</span></label>
        <ElInput
          id="tag-note-body"
          v-model="body"
          type="textarea"
          :rows="3"
          maxlength="400"
          show-word-limit
          placeholder="A line or two under the title"
        />
      </div>
      <div class="field">
        <label class="field-label">Icon</label>
        <div class="icon-grid" role="radiogroup" aria-label="Icon">
          <ElTooltip
            v-for="name in iconChoices"
            :key="name"
            :content="iconLabel(name)"
            placement="top"
            :show-after="300"
          >
            <button
              type="button"
              class="icon-choice"
              :class="{ selected: icon === name }"
              role="radio"
              :aria-checked="icon === name"
              :aria-label="iconLabel(name)"
              @click="icon = name"
            >
              <Icon :icon="cardIconGlyph(name)" />
            </button>
          </ElTooltip>
        </div>
      </div>
      <div class="field">
        <label class="field-label">Keep it on the tag for</label>
        <ElSelect v-model="ttl" class="ttl-select">
          <ElOption v-for="opt in TTL_OPTIONS" :key="opt.value" :label="opt.label" :value="opt.value" />
        </ElSelect>
        <p class="field-hint">After that the note drops off the screen at the next redraw.</p>
      </div>

      <div v-if="problem" class="callout callout-danger" role="alert">
        <Icon :icon="problem.code === 'otp_refused' ? 'mdi:shield-key-outline' : 'mdi:alert-circle-outline'" class="callout-icon" />
        <span>{{ problem.message }}</span>
      </div>
    </div>

    <template #footer>
      <ElButton @click="close">Cancel</ElButton>
      <ElButton type="primary" :loading="sending" :disabled="!canSend" @click="send">
        <Icon v-if="!sending" icon="mdi:send-outline" class="btn-icon" /> Display
      </ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.note-form { display: flex; flex-direction: column; gap: 14px; }
.field-label {
  display: block; margin-bottom: 6px;
  font-size: 0.82rem; font-weight: 600; color: var(--text-secondary);
}
.optional { font-weight: 400; color: var(--text-tertiary); }
.field-hint { margin: 6px 0 0; font-size: 0.78rem; color: var(--text-tertiary); }
.ttl-select { width: 200px; }
.icon-grid { display: flex; flex-wrap: wrap; gap: 6px; }
.icon-choice {
  width: 34px; height: 34px; display: flex; align-items: center; justify-content: center;
  border: 1px solid var(--border-color); border-radius: 8px;
  background: var(--surface-color); color: var(--text-secondary);
  cursor: pointer; font-size: 18px; transition: border-color 0.15s, color 0.15s;
}
.icon-choice:hover { border-color: var(--primary-color); color: var(--primary-color); }
.icon-choice.selected {
  border-color: var(--primary-color); color: var(--primary-color);
  background: color-mix(in srgb, var(--primary-color) 14%, var(--surface-color));
}
.callout {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 10px 12px; border-radius: 8px; font-size: 0.85rem; line-height: 1.45;
}
.callout-danger {
  border: 1px solid color-mix(in srgb, var(--el-color-danger) 55%, transparent);
  background: color-mix(in srgb, var(--el-color-danger) 12%, var(--surface-color));
  color: var(--text-primary);
}
.callout-icon { flex-shrink: 0; font-size: 1.1rem; margin-top: 1px; color: var(--el-color-danger); }
.btn-icon { margin-right: 4px; }
</style>
