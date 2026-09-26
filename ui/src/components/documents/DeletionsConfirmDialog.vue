<script setup lang="ts">
/**
 * A mass deletion is held, not applied: when a large share of the indexed
 * files vanishes at once, the engine hides them from search and waits for the
 * user (`awaiting_confirmation(mass_delete)`). An unplugged drive or a share
 * that did not mount looks exactly like "the user deleted everything", and
 * acting on the wrong guess would throw away an index that took hours to build.
 *
 * "Remove them" purges the rows (`confirm_deletions`); "Keep them" leaves them
 * hidden and re-checked on every scan for 14 days (`reject_deletions`).
 */
import { ElButton, ElDialog } from 'element-plus';
import { Icon } from '@iconify/vue';
import { formatCount } from '../../utils/documentsView';

withDefaults(defineProps<{
  modelValue: boolean;
  missing: number;
  total: number;
  root?: string | null;
  busy?: boolean;
}>(), { root: null, busy: false });

const emit = defineEmits<{
  'update:modelValue': [open: boolean];
  remove: [];
  keep: [];
}>();
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    title="Files disappeared from your folder"
    width="520px"
    :close-on-click-modal="!busy"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    append-to-body
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <div class="del-summary">
      <Icon icon="mdi:folder-alert-outline" class="del-icon" />
      <p>
        <strong>{{ formatCount(missing) }} of {{ formatCount(total) }} files</strong>
        vanished from {{ root || 'your documents folder' }} at the same time.
        Until you decide, they are hidden from search — nothing has been removed yet.
      </p>
    </div>
    <ul class="del-options">
      <li>
        <strong>Keep them</strong> if a drive, network share or mount is disconnected.
        They come back as soon as it does, and are re-checked on every scan for 14 days.
      </li>
      <li>
        <strong>Remove them</strong> if you really deleted or moved them out of the folder.
        Their entries leave the index; your files are not touched.
      </li>
    </ul>
    <template #footer>
      <ElButton :disabled="busy" @click="emit('keep')">Keep them</ElButton>
      <ElButton class="doc-danger" :loading="busy" @click="emit('remove')">Remove them</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.del-summary { display: flex; gap: 10px; align-items: flex-start; }
.del-summary p { margin: 0; font-size: 0.875rem; line-height: 1.5; color: var(--text-primary); }
.del-icon { font-size: 24px; color: var(--warning-color); flex: none; }
.del-options {
  margin: 14px 0 0; padding-left: 18px; font-size: 0.85rem; line-height: 1.5;
  color: var(--text-secondary); display: flex; flex-direction: column; gap: 6px;
}
.del-options strong { color: var(--text-primary); }

/* See ConfirmPlanDialog: the danger token, not ElButton type="danger". */
.doc-danger {
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
