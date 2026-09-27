<script setup lang="ts">
/**
 * A new connector credential, shown ONCE: Cremind keeps only a hash of the
 * secret, so this dialog is the only place to copy it. Used for a profile's
 * content credential and for a companion's hardware credential (register /
 * rotate). Closing asks first, so the secret is not lost to a stray click.
 */
import { computed } from 'vue';
import { ElButton, ElDialog, ElMessageBox } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useCopyToClipboard } from '../../composables/useCopyToClipboard';
import type { TagSecretPayload } from '../../services/tagsApi';

const props = defineProps<{
  modelValue: boolean;
  payload: TagSecretPayload | null;
  title: string;
  /** One sentence on what this credential lets the companion do. */
  purpose: string;
}>();
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>();

const { copy, isCopied } = useCopyToClipboard();

const rows = computed(() => (props.payload
  ? [
    { key: 'authorization', label: 'Authorization header', value: props.payload.authorization, primary: true },
    { key: 'id', label: 'Credential id', value: props.payload.credential.id, primary: false },
    { key: 'secret', label: 'Secret', value: props.payload.secret, primary: false },
  ]
  : []));

async function requestClose() {
  try {
    await ElMessageBox.confirm(
      'The secret cannot be shown again. If you have not copied it, you will need to create a new credential.',
      'Close without the secret?',
      { type: 'warning', confirmButtonText: 'I have copied it', cancelButtonText: 'Back' },
    );
  } catch { return; }
  emit('update:modelValue', false);
}

/** The header's × asks too; the dialog closes through requestClose, not `done`. */
function beforeClose(_done: () => void) {
  void requestClose();
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="title"
    width="600px"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :before-close="beforeClose"
  >
    <div v-if="payload" class="secret-body">
      <div class="callout" role="status">
        <Icon icon="mdi:key-alert-outline" class="callout-icon" />
        <span><strong>Copy it now — this is the only time the secret is shown.</strong> {{ purpose }}</span>
      </div>

      <div v-for="row in rows" :key="row.key" class="secret-row">
        <div class="secret-label">{{ row.label }}</div>
        <div class="secret-value-row">
          <code class="secret-value" :class="{ primary: row.primary }">{{ row.value }}</code>
          <ElButton size="small" :type="row.primary ? 'primary' : 'default'" @click="copy(row.value, row.key)">
            <Icon :icon="isCopied(row.key) ? 'mdi:check' : 'mdi:content-copy'" class="btn-icon" />
            {{ isCopied(row.key) ? 'Copied' : 'Copy' }}
          </ElButton>
        </div>
      </div>

      <div class="hint">
        <Icon icon="mdi:console" class="hint-icon" />
        <span>
          On the computer the companion runs on, run <code>cremind-tag connect</code> and paste the
          Authorization value when it asks — it carries both the id and the secret.
        </span>
      </div>
    </div>
    <template #footer>
      <ElButton type="primary" @click="requestClose">Done</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.secret-body { display: flex; flex-direction: column; gap: 14px; color: var(--text-primary); }
.callout {
  display: flex; align-items: flex-start; gap: 8px; padding: 10px 12px; border-radius: 8px;
  font-size: 0.85rem; line-height: 1.45; color: var(--text-primary);
  border: 1px solid color-mix(in srgb, var(--el-color-warning) 55%, transparent);
  background: color-mix(in srgb, var(--el-color-warning) 12%, var(--surface-color));
}
.callout-icon { flex-shrink: 0; font-size: 1.15rem; color: var(--el-color-warning); margin-top: 1px; }
.secret-label { font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); margin-bottom: 4px; }
.secret-value-row { display: flex; align-items: flex-start; gap: 8px; }
.secret-value {
  flex: 1; min-width: 0; padding: 7px 10px; border-radius: 6px;
  border: 1px solid var(--border-color); background: var(--hover-bg); color: var(--text-primary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.8rem;
  word-break: break-all; user-select: all;
}
.secret-value.primary { border-color: color-mix(in srgb, var(--primary-color) 55%, transparent); }
.btn-icon { margin-right: 4px; }
.hint { display: flex; align-items: flex-start; gap: 8px; font-size: 0.82rem; color: var(--text-secondary); line-height: 1.5; }
.hint-icon { flex-shrink: 0; font-size: 1.05rem; margin-top: 2px; }
.hint code {
  background: var(--hover-bg); color: var(--text-primary);
  padding: 1px 6px; border-radius: 4px; font-size: 0.85em;
}
</style>
