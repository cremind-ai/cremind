<script setup lang="ts">
/**
 * Remove a gateway, bridge or tag (POST …/devices/{id}/unpair). Access is
 * revoked at once; cleaning up the device itself needs it to be reachable, so
 * it shows "Removal pending" until then. A bridge lists the tags that stop
 * receiving updates until they are added to another bridge or removed; a
 * gateway takes everything connected through it with it.
 */
import { computed, ref } from 'vue';
import { ElButton, ElDialog, ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { TagsApiError } from '../../../services/tagsApi';
import type { SetupDevice, TagConnection } from '../../../services/tagsSetupApi';
import { connectionTitle, setupDeviceTitle, setupErrorMessage } from '../../../utils/tagsSetupFormat';

const props = defineProps<{
  modelValue: boolean;
  device: SetupDevice | null;
  connection: TagConnection | null;
}>();
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>();

const store = useTagsSetupStore();
const removing = ref(false);
const error = ref('');

const kind = computed(() => props.device?.kind ?? 'tag');
const title = computed(() => {
  if (!props.device) return '';
  return kind.value === 'gateway' && props.connection ? connectionTitle(props.connection) : setupDeviceTitle(props.device);
});

/** A bridge's tags, by name. */
const affectedTags = computed(() => {
  const d = props.device;
  if (!d || d.kind !== 'bridge') return [];
  const ids = d.affected_tag_ids ?? props.connection?.tags.filter((t) => t.bridge_id === d.id).map((t) => t.id) ?? [];
  const all = store.tags.map((t) => t.device);
  return ids.map((id) => {
    const t = all.find((x) => x.id === id);
    return t ? setupDeviceTitle(t) : 'A tag';
  });
});

const counts = computed(() => {
  const c = props.connection;
  if (!c) return '';
  const b = c.bridges.length;
  const t = c.tags.length;
  const part = (n: number, one: string, many: string) => (n === 1 ? `1 ${one}` : `${n} ${many}`);
  if (!b && !t) return '';
  return [b ? part(b, 'bridge', 'bridges') : '', t ? part(t, 'tag', 'tags') : ''].filter(Boolean).join(' and ');
});

async function remove() {
  const d = props.device;
  if (!d?.id) return;
  removing.value = true;
  error.value = '';
  try {
    await store.unpair(d.id);
    ElMessage.success(`Removing ${title.value}. It finishes as soon as the device can be reached.`);
    emit('update:modelValue', false);
  } catch (e) {
    error.value = e instanceof TagsApiError
      ? setupErrorMessage(e.code, { role: d.kind, fallback: e.message })
      : 'Cremind could not be reached. Try again.';
  } finally {
    removing.value = false;
  }
}

function close() {
  error.value = '';
  emit('update:modelValue', false);
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="`Remove ${title}?`"
    width="min(500px, calc(100vw - 24px))"
    append-to-body
    @update:model-value="(v: boolean) => { if (!v) close(); }"
  >
    <div v-if="device" class="remove-body">
      <template v-if="kind === 'tag'">
        <p>The tag stops receiving updates right away. Its screen is cleared and it shows a new setup code, so it can be added again later.</p>
      </template>
      <template v-else-if="kind === 'bridge'">
        <p>The bridge leaves your network.</p>
        <template v-if="affectedTags.length">
          <p>These tags use it and stop receiving updates until you add them again through another bridge, or remove them:</p>
          <ul class="affected">
            <li v-for="(t, i) in affectedTags" :key="i"><Icon icon="mdi:tablet-dashboard" aria-hidden="true" /> {{ t }}</li>
          </ul>
        </template>
        <p v-else>No tags use it.</p>
      </template>
      <template v-else>
        <p>
          The gateway and everything connected through it<template v-if="counts"> ({{ counts }})</template>
          are removed from this profile. Their access stops right away.
        </p>
      </template>
      <p class="pending-note">
        <Icon icon="mdi:progress-clock" aria-hidden="true" />
        <span>
          If the {{ kind }} is switched off, asleep or out of reach, it shows <strong>Removal pending</strong>
          and the removal finishes the next time it can be reached.
        </span>
      </p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </div>
    <template #footer>
      <ElButton @click="close">Cancel</ElButton>
      <ElButton type="danger" :loading="removing" @click="remove">Remove</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.remove-body { display: flex; flex-direction: column; gap: 10px; font-size: 0.88rem; line-height: 1.55; color: var(--text-primary); }
.remove-body p { margin: 0; }
.affected { margin: 0; padding-left: 4px; list-style: none; display: flex; flex-direction: column; gap: 4px; }
.affected li { display: flex; align-items: center; gap: 6px; }
.pending-note { display: flex; gap: 8px; align-items: flex-start; font-size: 0.82rem; color: var(--text-secondary); }
.pending-note :deep(svg) { flex-shrink: 0; margin-top: 3px; }
.error { color: var(--el-color-danger); font-size: 0.85rem; }
</style>
