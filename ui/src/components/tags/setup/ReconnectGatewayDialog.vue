<script setup lang="ts">
/**
 * Reconnect an offline gateway: say where it belongs and what that computer
 * reports. A gateway computer finds its gateway again on any USB port by
 * itself, so plugging it back in is all it takes; when that computer is gone,
 * the way forward is moving the gateway to another one. A gateway still set
 * up with the older Cremind Connect is moved the same way.
 */
import { computed, watchEffect } from 'vue';
import { ElButton, ElDialog, ElResult } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import type { TagConnection } from '../../../services/tagsSetupApi';
import {
  connectionStatusPill, connectionTitle, hostBlock, hostName, plugInstruction,
} from '../../../utils/tagsSetupFormat';

const props = defineProps<{ modelValue: boolean; connection: TagConnection | null }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'recover', connection: TagConnection): void;
}>();

const store = useTagsSetupStore();

/** The live row (the list refreshes while the dialog is open). */
const live = computed(() => store.connections.find((c) => c.id === props.connection?.id) ?? props.connection);
const name = computed(() => (live.value ? connectionTitle(live.value) : 'the gateway'));
const back = computed(() => live.value?.status === 'connected');
const status = computed(() => (live.value ? connectionStatusPill(live.value) : null));
const hostId = computed(() => live.value?.host_id ?? live.value?.computer?.host_id ?? null);
const host = computed(() => (hostId.value ? store.hosts.find((h) => h.id === hostId.value) ?? null : null));
/** Set up with the older Cremind Connect (no gateway computer runs it). */
const legacy = computed(() => !!live.value && !hostId.value);
const block = computed(() => (host.value ? hostBlock(host.value) : null));
/** The computer reports the gateway on one of its ports right now. */
const seen = computed(() => {
  const device = live.value?.gateway?.device_id;
  return !!device && !!host.value?.gateways.some((g) => g.device_id === device);
});
const plug = computed(() => plugInstruction(host.value, name.value));

// While it is open, re-read every few seconds so "Connected" shows as soon as it is back.
watchEffect((onCleanup) => {
  if (!props.modelValue || back.value) return;
  const timer = setInterval(() => {
    void store.loadConnections();
    void store.loadHosts();
  }, 3000);
  onCleanup(() => clearInterval(timer));
});

function close() {
  emit('update:modelValue', false);
}

function recover() {
  if (!live.value) return;
  close();
  emit('recover', live.value);
}
</script>

<template>
  <ElDialog
    :model-value="modelValue" :title="`Reconnect ${name}`" width="min(520px, calc(100vw - 24px))" append-to-body
    @update:model-value="(v: boolean) => { if (!v) close(); }"
  >
    <div class="reconnect">
      <template v-if="back">
        <ElResult icon="success" title="The gateway is back" :sub-title="`${name} is connected again.`" />
      </template>

      <template v-else-if="legacy">
        <p class="text">
          {{ name }} is still set up with the older Cremind Connect
          <template v-if="live?.computer?.name">on <strong>{{ live.computer.name }}</strong></template>.
          Move it to a computer where Cremind drives it itself: plug it in there and Cremind takes it over,
          with its bridges and tags.
        </p>
      </template>

      <template v-else-if="!host">
        <p class="text">
          The computer {{ name }} was connected through is no longer set up for this profile. Move it to
          another gateway computer.
        </p>
      </template>

      <template v-else-if="block">
        <div class="problem" role="alert">
          <Icon icon="mdi:alert-outline" class="problem-icon" aria-hidden="true" />
          <div><strong>{{ block.title }}</strong><p>{{ block.text }}</p></div>
        </div>
        <p class="text muted">If {{ hostName(host) }} is gone for good, move the gateway to another computer.</p>
      </template>

      <template v-else-if="seen">
        <p class="text ok"><Icon icon="mdi:check-circle" aria-hidden="true" /> {{ hostName(host) }} sees the gateway on its USB ports.</p>
        <p class="text">It reconnects by itself; this can take a minute.</p>
        <p v-if="status" class="text muted" aria-live="polite">Status: {{ status.label }}</p>
      </template>

      <template v-else>
        <p class="text">{{ plug.before }}<strong>{{ plug.name }}</strong>{{ plug.after }}</p>
        <p class="text">It reconnects by itself, on any USB port; this can take a minute.</p>
        <p v-if="status" class="text muted" aria-live="polite">Status: {{ status.label }}</p>
      </template>
    </div>
    <template #footer>
      <ElButton v-if="!back" @click="recover">Move to another computer</ElButton>
      <ElButton :type="back ? 'primary' : undefined" @click="close">{{ back ? 'Done' : 'Close' }}</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.reconnect { display: flex; flex-direction: column; gap: 10px; }
.text { margin: 0; font-size: 0.88rem; line-height: 1.55; color: var(--text-primary); }
.text.ok { color: var(--el-color-success); display: flex; align-items: center; gap: 6px; }
.text.muted { color: var(--text-secondary); }
.problem {
  display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--el-color-warning) 45%, transparent);
  background: color-mix(in srgb, var(--el-color-warning) 8%, var(--surface-color));
}
.problem-icon { font-size: 1.3rem; color: var(--el-color-warning); flex-shrink: 0; }
.problem strong { font-size: 0.9rem; color: var(--text-primary); }
.problem p { margin: 4px 0 0; font-size: 0.86rem; line-height: 1.5; color: var(--text-primary); }
</style>
