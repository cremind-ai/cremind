<script setup lang="ts">
/**
 * "This computer": is Cremind Connect here, and which gateways does it look
 * after? In the desktop app the answer comes from the app itself (and it can
 * install, start or update its bundled Connect); in a browser, "Check this
 * computer" opens Connect through a probe session and shows what answered —
 * or the installer, with "Continue after installation".
 */
import { computed, onMounted } from 'vue';
import { ElButton } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { useConnectProbe } from '../../../composables/useConnectProbe';
import { connectionTitle, isNewerVersion } from '../../../utils/tagsSetupFormat';
import { formatRelativeTime } from '../../../utils/relativeTime';
import ConnectInstallOffer from './ConnectInstallOffer.vue';

const props = defineProps<{ profile: string; now: number }>();

const store = useTagsSetupStore();
const probe = useConnectProbe(() => props.profile);

const found = computed(() => probe.found.value);
const desktop = computed(() => probe.desktop.value);
const updateReady = computed(() => !!desktop.value?.installed
  && isNewerVersion(desktop.value.bundledVersion, desktop.value.version));
/** Desktop: install() installs, updates and (re)starts — named for what is missing most. */
const desktopAction = computed(() => {
  const d = desktop.value;
  if (!d?.installed) return 'Install Cremind Connect';
  if (!d.running) return 'Start Cremind Connect';
  return 'Update Cremind Connect';
});

/** The gateways Connect on this computer looks after (matched by installation). */
const gatewaysHere = computed(() => {
  const id = found.value?.installationId;
  if (!id) return [];
  return store.connections.filter((c) => c.computer?.installation_id === id).map(connectionTitle);
});

const foundText = computed(() => {
  const f = found.value;
  const version = f?.version ? ` ${f.version}` : '';
  return `Cremind Connect${version} is running on ${f?.computer || 'this computer'}.`;
});

const rememberedText = computed(() => {
  const r = probe.remembered.value;
  if (!r) return '';
  const when = formatRelativeTime(r.at, props.now);
  if (!r.found) return `Last check (${when}): Cremind Connect did not answer.`;
  const version = r.version ? ` ${r.version}` : '';
  return `Last check (${when}): Cremind Connect${version} on ${r.computer || 'this computer'}.`;
});

onMounted(() => {
  // The desktop app answers over IPC: no click (and no window) needed.
  if (probe.isDesktop) void probe.check();
});
</script>

<template>
  <div class="this-computer" role="group" aria-labelledby="this-computer-title">
    <Icon icon="mdi:laptop" class="pc-icon" aria-hidden="true" />
    <div class="pc-body">
      <div id="this-computer-title" class="pc-title">This computer</div>

      <div class="pc-status" aria-live="polite">
        <template v-if="probe.state.value === 'checking'">
          <p class="pc-text">
            <Icon icon="mdi:loading" class="spin" aria-hidden="true" />
            <span>
              Looking for Cremind Connect on this computer…
              <span v-if="!probe.isDesktop" class="muted">If your browser asks to open Cremind Connect, allow it.</span>
            </span>
          </p>
        </template>

        <template v-else-if="probe.state.value === 'found'">
          <p class="pc-text ok">
            <Icon icon="mdi:check-circle" aria-hidden="true" /><span>{{ foundText }}</span>
          </p>
          <p v-if="gatewaysHere.length" class="pc-sub">Looks after {{ gatewaysHere.join(', ') }}.</p>
          <p v-if="updateReady" class="pc-sub">A newer Cremind Connect ({{ desktop?.bundledVersion }}) comes with this app.</p>
        </template>

        <template v-else-if="probe.state.value === 'missing'">
          <template v-if="probe.isDesktop">
            <p class="pc-text">
              {{ desktop?.installed
                ? 'Cremind Connect is installed but not running.'
                : 'Cremind Connect is not installed on this computer yet. It comes with this app.' }}
            </p>
          </template>
          <template v-else>
            <p class="pc-text">Cremind Connect did not answer on this computer.</p>
            <ConnectInstallOffer />
          </template>
        </template>

        <template v-else-if="probe.state.value === 'failed'">
          <p class="pc-text error" role="alert">{{ probe.error.value }}</p>
        </template>

        <template v-else>
          <p class="pc-text">
            Cremind Connect is the small app that talks to your gateway over USB and keeps your tags
            updated in the background.
          </p>
          <p v-if="rememberedText" class="pc-sub">{{ rememberedText }}</p>
        </template>

        <p v-if="probe.error.value && probe.state.value !== 'failed'" class="pc-text error" role="alert">{{ probe.error.value }}</p>
      </div>
    </div>

    <div class="pc-actions">
      <template v-if="probe.isDesktop">
        <ElButton
          v-if="probe.state.value === 'missing' || updateReady"
          type="primary" size="small" :loading="probe.installing.value" @click="probe.installDesktop()"
        >
          {{ desktopAction }}
        </ElButton>
        <ElButton v-else-if="probe.state.value !== 'checking'" size="small" @click="probe.check()">Check again</ElButton>
      </template>
      <template v-else>
        <ElButton v-if="probe.state.value === 'missing'" type="primary" size="small" @click="probe.retry()">
          Continue after installation
        </ElButton>
        <ElButton
          v-else
          size="small"
          :loading="probe.state.value === 'checking'"
          @click="probe.check()"
        >
          {{ probe.state.value === 'idle' && !probe.remembered.value ? 'Check this computer' : 'Check again' }}
        </ElButton>
      </template>
    </div>
  </div>
</template>

<style scoped>
.this-computer {
  display: flex; align-items: flex-start; gap: 12px; flex-wrap: wrap;
  padding: 12px 14px; border: 1px solid var(--border-color); border-radius: 10px; background: var(--surface-color);
}
.pc-icon { font-size: 26px; color: var(--primary-color); flex-shrink: 0; margin-top: 2px; }
.pc-body { flex: 1; min-width: 220px; display: flex; flex-direction: column; gap: 4px; }
.pc-title { font-weight: 600; color: var(--text-primary); }
.pc-status { display: flex; flex-direction: column; gap: 6px; }
.pc-text { margin: 0; font-size: 0.86rem; line-height: 1.5; color: var(--text-primary); display: flex; align-items: flex-start; gap: 6px; }
.pc-text :deep(svg) { flex-shrink: 0; margin-top: 3px; }
.pc-text.ok { color: var(--el-color-success); }
.pc-text.error { color: var(--el-color-danger); }
.pc-sub { margin: 0; font-size: 0.8rem; color: var(--text-secondary); }
.muted { color: var(--text-tertiary); }
.pc-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.spin { animation: pc-spin 1s linear infinite; }
@keyframes pc-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
