<script setup lang="ts">
/**
 * Reconnect an offline gateway: open Cremind Connect on this computer again
 * (a probe session, or the desktop app's own Connect) and say what that
 * means for this gateway. Connect finds its gateway again on any USB port by
 * itself, so no new setup is needed when this is the gateway's computer; when
 * it is another computer, the way forward is that computer — or "Recover on
 * this computer".
 */
import { computed, watch, watchEffect } from 'vue';
import { ElButton, ElDialog, ElResult } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { useConnectProbe } from '../../../composables/useConnectProbe';
import type { TagConnection } from '../../../services/tagsSetupApi';
import { connectionStatusPill, connectionTitle } from '../../../utils/tagsSetupFormat';
import ConnectInstallOffer from './ConnectInstallOffer.vue';

const props = defineProps<{ modelValue: boolean; connection: TagConnection | null; profile: string }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'recover', connection: TagConnection): void;
}>();

const store = useTagsSetupStore();
const probe = useConnectProbe(() => props.profile);

/** The live row (the list refreshes while the dialog is open). */
const live = computed(() => store.connections.find((c) => c.id === props.connection?.id) ?? props.connection);
const name = computed(() => (live.value ? connectionTitle(live.value) : 'the gateway'));
const homeComputer = computed(() => live.value?.computer?.name || 'its computer');
const sameComputer = computed(() => {
  const here = probe.found.value?.installationId;
  const home = live.value?.computer?.installation_id;
  return !here || !home || here === home;
});
const status = computed(() => (live.value ? connectionStatusPill(live.value) : null));
const back = computed(() => live.value?.status === 'connected');

watch(() => props.modelValue, (open) => {
  if (!open) return;
  probe.reset();
  // The desktop app can check at once; a browser needs the click below.
  if (probe.isDesktop) void probe.check();
});

// Once Connect runs here, re-read the list every few seconds so "Connected" shows as soon as it is back.
watchEffect((onCleanup) => {
  if (!props.modelValue || probe.state.value !== 'found' || back.value) return;
  const timer = setInterval(() => { void store.loadConnections(); }, 3000);
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
  <ElDialog :model-value="modelValue" :title="`Reconnect ${name}`" width="min(520px, calc(100vw - 24px))" append-to-body @update:model-value="(v: boolean) => { if (!v) close(); }">
    <div class="reconnect">
      <template v-if="back">
        <ElResult icon="success" title="The gateway is back" :sub-title="`${name} is connected again.`" />
      </template>

      <template v-else-if="probe.state.value === 'idle' || probe.state.value === 'failed'">
        <p class="text">
          {{ name }} was set up on <strong>{{ homeComputer }}</strong>. Make sure it is plugged in there,
          and that Cremind Connect is running.
        </p>
        <p class="text">If this is that computer, open Cremind Connect here:</p>
        <div class="actions">
          <ElButton type="primary" @click="probe.check()">
            <Icon icon="mdi:open-in-app" class="btn-icon" /> Open Cremind Connect
          </ElButton>
        </div>
        <p v-if="probe.error.value" class="error" role="alert">{{ probe.error.value }}</p>
      </template>

      <template v-else-if="probe.state.value === 'checking'">
        <p class="text" aria-live="polite">
          <Icon icon="mdi:loading" class="spin" aria-hidden="true" /> Opening Cremind Connect on this computer…
        </p>
      </template>

      <template v-else-if="probe.state.value === 'missing'">
        <p class="text">
          {{ probe.isDesktop && probe.desktop.value?.installed
            ? 'Cremind Connect is installed on this computer but not running.'
            : 'Cremind Connect did not answer on this computer.' }}
        </p>
        <div v-if="probe.isDesktop" class="actions">
          <ElButton type="primary" :loading="probe.installing.value" @click="probe.installDesktop()">
            {{ probe.desktop.value?.installed ? 'Start Cremind Connect' : 'Install Cremind Connect' }}
          </ElButton>
        </div>
        <template v-else>
          <ConnectInstallOffer />
          <div class="actions">
            <ElButton type="primary" @click="probe.retry()">Continue after installation</ElButton>
          </div>
        </template>
      </template>

      <template v-else-if="sameComputer">
        <p class="text ok"><Icon icon="mdi:check-circle" aria-hidden="true" /> Cremind Connect is running on this computer.</p>
        <p class="text">
          Plug the gateway in if it is not. It reconnects by itself, on any USB port; this can take a
          minute.
        </p>
        <p v-if="status" class="text muted" aria-live="polite">Status: {{ status.label }}</p>
      </template>

      <template v-else>
        <p class="text">
          This computer is <strong>{{ probe.found.value?.computer || 'not the gateway\'s computer' }}</strong>, but
          {{ name }} was set up on <strong>{{ homeComputer }}</strong>.
        </p>
        <p class="text">
          Plug the gateway into {{ homeComputer }} and make sure Cremind Connect runs there. If that computer
          is gone, move the gateway to this one instead.
        </p>
        <div class="actions">
          <ElButton type="primary" @click="recover">Recover on this computer</ElButton>
        </div>
      </template>
    </div>
    <template #footer>
      <ElButton @click="close">{{ back ? 'Done' : 'Close' }}</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.reconnect { display: flex; flex-direction: column; gap: 10px; }
.text { margin: 0; font-size: 0.88rem; line-height: 1.55; color: var(--text-primary); }
.text.ok { color: var(--el-color-success); display: flex; align-items: center; gap: 6px; }
.text.muted { color: var(--text-secondary); }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.btn-icon { margin-right: 6px; }
.error { margin: 0; font-size: 0.85rem; color: var(--el-color-danger); }
.spin { animation: rc-spin 1s linear infinite; vertical-align: -2px; }
@keyframes rc-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
