<script setup lang="ts">
/**
 * "Here is what this change would do" — the second step of every destructive
 * User Document Search change (moving the folder, exclude rules that drop
 * indexed files, deleting the index, rebuilding).
 *
 * The server answered the first request with `409 ConfirmationRequired`, a
 * plan and a token; confirming here repeats that request with the token (the
 * page does the sending — see `sendWithConfirm`). If the change grew while the
 * dialog was open the server asks again, and the page swaps the plan in with
 * `changed` set so the user is told why the dialog did not close.
 */
import { computed } from 'vue';
import { ElButton, ElDialog } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { ChangePlan } from '../../services/userdocsApi';
import { effectLabel } from '../../utils/userdocsView';

const props = withDefaults(defineProps<{
  modelValue: boolean;
  plan: ChangePlan | null;
  title?: string;
  message?: string;
  confirmLabel?: string;
  busy?: boolean;
  /** The server answered the confirm with a different plan. */
  changed?: boolean;
}>(), {
  title: 'Confirm this change',
  message: '',
  confirmLabel: 'Confirm',
  busy: false,
  changed: false,
});

const emit = defineEmits<{
  'update:modelValue': [open: boolean];
  confirm: [];
}>();

const effects = computed(() => props.plan?.effects ?? []);

function close() {
  if (!props.busy) emit('update:modelValue', false);
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="title"
    width="520px"
    :close-on-click-modal="!busy"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    append-to-body
    @update:model-value="(v: boolean) => { if (!v) close(); }"
  >
    <p v-if="changed" class="plan-changed">
      <Icon icon="mdi:refresh" />
      The change grew while this was open. Review the updated numbers.
    </p>
    <p v-if="message" class="plan-message">{{ message }}</p>
    <ul class="plan-effects">
      <li v-for="(effect, i) in effects" :key="i" :class="{ destructive: effect.kind.startsWith('purge') }">
        <Icon :icon="effect.kind.startsWith('purge') ? 'mdi:delete-outline' : 'mdi:database-refresh-outline'" />
        <span>{{ effectLabel(effect) }}</span>
      </li>
    </ul>
    <p class="plan-note">
      Only Cremind's index changes. Your files themselves are never moved, changed or deleted.
    </p>
    <template #footer>
      <ElButton :disabled="busy" @click="close">Cancel</ElButton>
      <ElButton
        :class="{ 'ud-danger': plan?.destructive }"
        :type="plan?.destructive ? undefined : 'primary'"
        :loading="busy"
        @click="emit('confirm')"
      >
        {{ confirmLabel }}
      </ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.plan-message { margin: 0 0 12px; font-size: 0.875rem; color: var(--text-secondary); line-height: 1.5; }
.plan-changed {
  display: flex; align-items: center; gap: 6px; margin: 0 0 12px;
  padding: 8px 10px; border-radius: 6px; font-size: 0.85rem;
  background: color-mix(in srgb, var(--warning-color) 10%, var(--surface-color));
  border: 1px solid color-mix(in srgb, var(--warning-color) 40%, var(--border-color));
  color: var(--text-primary);
}
.plan-effects { list-style: none; margin: 0 0 12px; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.plan-effects li {
  display: flex; align-items: flex-start; gap: 8px; font-size: 0.875rem;
  color: var(--text-primary); line-height: 1.45;
}
.plan-effects li :deep(svg) { flex: none; margin-top: 2px; color: var(--primary-color); }
.plan-effects li.destructive :deep(svg) { color: var(--danger-color); }
.plan-note { margin: 0; font-size: 0.8rem; color: var(--text-secondary); }

/* A destructive confirm in the danger token. Not ElButton type="danger": its
   hover/active shades are --el-color-danger-light-* variables the theme never
   redeclares, so they would stay light-theme colours in dark mode. */
.ud-danger {
  --el-button-text-color: #fff;
  --el-button-bg-color: var(--danger-color);
  --el-button-border-color: var(--danger-color);
  --el-button-hover-text-color: #fff;
  --el-button-hover-bg-color: color-mix(in srgb, var(--danger-color) 85%, #fff);
  --el-button-hover-border-color: color-mix(in srgb, var(--danger-color) 85%, #fff);
  --el-button-active-text-color: #fff;
  --el-button-active-bg-color: color-mix(in srgb, var(--danger-color) 85%, #000);
  --el-button-active-border-color: color-mix(in srgb, var(--danger-color) 85%, #000);
}
</style>
