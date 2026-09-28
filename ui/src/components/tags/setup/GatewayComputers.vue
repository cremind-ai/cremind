<script setup lang="ts">
/**
 * "Gateway computers": the computers whose USB ports Cremind drives gateways
 * on for this profile — the computer the Cremind server runs on, and the
 * profile's own Cremind desktop computers. For each: whether it is ready
 * (as the computer itself reports it: its components, its USB access), what
 * stands in the way and what to do about it, and "Connect a gateway here".
 *
 * The admin also prepares the server's gateway components (progress, retry)
 * and decides which other profiles may use the server's USB ports. A
 * profile allowed there claims only unclaimed gateways; everything it
 * connects stays its own. "Set up a gateway computer" adds one of the
 * profile's own computers (the Cremind app on it); "Remove" takes one away.
 */
import { computed, ref } from 'vue';
import { ElButton, ElMessage, ElMessageBox, ElSwitch, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { TagsApiError } from '../../../services/tagsApi';
import type { GatewayHost, HostOperation } from '../../../services/tagsSetupApi';
import {
  hostBlock, hostKindLabel, hostName, hostOpProgressLabel, hostStatePill, isOperationTerminal, isoToMs,
  setupErrorMessage,
} from '../../../utils/tagsSetupFormat';
import { formatRelativeTime } from '../../../utils/relativeTime';

const props = defineProps<{ now: number }>();
const emit = defineEmits<{ (e: 'connect', hostId: string): void; (e: 'enroll'): void }>();

const setup = useTagsSetupStore();
/** `<host id>:<action>` while it runs. */
const busy = ref('');
const accessOpen = ref<Record<string, boolean>>({});

const hosts = computed(() => setup.hosts);

const PLATFORM: Record<string, string> = { windows: 'Windows', macos: 'macOS', linux: 'Linux' };

/** Gateways taken over from the older Cremind Connect on this computer, in words. */
function moved(h: GatewayHost): string {
  const m = h.migration ?? {};
  const parts: string[] = [];
  if (m.moved) parts.push(`${m.moved === 1 ? '1 gateway' : `${m.moved} gateways`} moved in from Cremind Connect`);
  if (m.failed) parts.push(`${m.failed} still moving (at the next start)`);
  if (m.rolled_back) parts.push(`${m.rolled_back} left with Cremind Connect (see the log)`);
  return parts.join('; ');
}

function facts(h: GatewayHost): string {
  const parts = [hostKindLabel(h.kind), PLATFORM[h.platform ?? ''] ?? '', h.version ? `Cremind ${h.version}` : ''];
  if (h.connections) parts.push(h.connections === 1 ? '1 of your gateways' : `${h.connections} of your gateways`);
  const seen = isoToMs(h.last_seen_at);
  if (!h.online && h.kind !== 'server' && seen) parts.push(`Last seen ${formatRelativeTime(seen, props.now)}`);
  return parts.filter(Boolean).join(' · ');
}

/** The newest preparation of this computer's components the page knows of. */
function preparation(h: GatewayHost): HostOperation | null {
  const known = Object.values(setup.hostOps).filter((o) => o.kind === 'host_prepare' && o.host_id === h.id);
  const active = setup.activeHostOps.filter((o) => o.kind === 'host_prepare' && o.host_id === h.id);
  const all = [...known, ...active.filter((a) => !known.some((k) => k.id === a.id))];
  all.sort((a, b) => (isoToMs(b.created_at) ?? 0) - (isoToMs(a.created_at) ?? 0));
  return all[0] ?? null;
}

function preparing(h: GatewayHost): boolean {
  const op = preparation(h);
  return !!op && !isOperationTerminal(op.state);
}

/** Screens wait for fonts: gateways work, but tags cannot show cards yet. */
function partial(h: GatewayHost): boolean {
  return h.online && h.state === 'running' && h.readiness?.state === 'partial';
}

function canPrepare(h: GatewayHost): boolean {
  return h.kind === 'server' && h.access.can_manage && (h.state === 'unavailable' || partial(h));
}

function canConnect(h: GatewayHost): boolean {
  return h.access.can_use && !hostBlock(h);
}

function failed(e: unknown) {
  ElMessage.error(e instanceof TagsApiError
    ? setupErrorMessage(e.code, { fallback: e.message })
    : 'Cremind could not be reached. Try again.');
}

async function prepare(h: GatewayHost) {
  busy.value = `${h.id}:prepare`;
  try {
    const op = await setup.prepareHost(h.id);
    setup.follow('hostop', op.id);
  } catch (e) {
    failed(e);
  } finally {
    busy.value = '';
  }
}

async function recheck(h: GatewayHost) {
  busy.value = `${h.id}:check`;
  try {
    await setup.loadHosts();
  } finally {
    busy.value = '';
  }
}

async function setAccess(h: GatewayHost, profileId: string, granted: boolean) {
  busy.value = `${h.id}:access:${profileId}`;
  try {
    const res = await setup.setHostAccess(h.id, profileId, granted);
    ElMessage.success(granted ? `${res.profile} may now use ${hostName(h)}` : `${res.profile} may no longer use ${hostName(h)}`);
  } catch (e) {
    failed(e);
  } finally {
    busy.value = '';
  }
}

async function remove(h: GatewayHost) {
  const yours = h.connections === 1 ? 'Its gateway stays yours' : h.connections > 1 ? 'Its gateways stay yours' : '';
  try {
    await ElMessageBox.confirm(
      `Remove ${hostName(h)}? It stops driving gateways for this profile at once.`
      + (yours ? ` ${yours}, offline until you move ${h.connections === 1 ? 'it' : 'them'} to another computer.` : ''),
      'Remove gateway computer',
      { confirmButtonText: 'Remove', cancelButtonText: 'Cancel', type: 'warning' },
    );
  } catch {
    return;
  }
  busy.value = `${h.id}:remove`;
  try {
    await setup.removeHost(h.id);
    ElMessage.success(`${hostName(h)} was removed`);
  } catch (e) {
    failed(e);
  } finally {
    busy.value = '';
  }
}

function toggleAccess(h: GatewayHost) {
  accessOpen.value = { ...accessOpen.value, [h.id]: !accessOpen.value[h.id] };
}

const grantedCount = (h: GatewayHost) => (h.access.profiles ?? []).filter((p) => p.granted).length;
</script>

<template>
  <section class="computers" aria-labelledby="gateway-computers-title">
    <div class="computers-head">
      <div>
        <h3 id="gateway-computers-title" class="computers-title">Gateway computers</h3>
        <p class="computers-sub">Cremind drives your gateways over USB from these computers, and keeps your tags updated even when this page is closed.</p>
      </div>
      <ElButton size="small" @click="emit('enroll')">
        <Icon icon="mdi:plus" class="btn-icon" aria-hidden="true" /> Set up a gateway computer
      </ElButton>
    </div>

    <p v-if="setup.hostsLoaded && !hosts.length" class="empty-note">
      This server does not report any computer it can drive gateways from. Update Cremind to use gateways
      with it.
    </p>

    <ul class="host-list" aria-label="Gateway computers">
      <li v-for="h in hosts" :key="h.id" class="host" :aria-label="hostName(h)">
        <Icon :icon="h.kind === 'server' ? 'mdi:server' : 'mdi:laptop'" class="host-icon" aria-hidden="true" />
        <div class="host-main">
          <div class="host-title-line">
            <span class="host-title">{{ hostName(h) }}</span>
            <ElTag :type="hostStatePill(h).type" size="small" effect="plain">{{ hostStatePill(h).label }}</ElTag>
          </div>
          <div class="host-meta">{{ facts(h) }}</div>
          <div v-if="moved(h)" class="host-meta">{{ moved(h) }}.</div>

          <p v-if="!h.access.can_use" class="host-note info">
            <Icon icon="mdi:lock-outline" aria-hidden="true" /><span>{{ h.access.reason }}</span>
          </p>
          <template v-else>
            <div v-if="hostBlock(h)" class="host-note warning" role="status">
              <Icon icon="mdi:alert-outline" aria-hidden="true" />
              <span><strong>{{ hostBlock(h)!.title }}.</strong> {{ hostBlock(h)!.text }}</span>
            </div>
            <div v-else-if="partial(h)" class="host-note info" role="status">
              <Icon icon="mdi:information-outline" aria-hidden="true" />
              <span>
                Gateways work here, but tag screens wait for their fonts.
                {{ h.access.can_manage ? 'Prepare the components to finish.' : 'The admin can prepare them in Settings → Tags.' }}
              </span>
            </div>
          </template>

          <div v-if="preparation(h)" class="prepare" aria-live="polite">
            <template v-if="preparing(h)">
              <p class="prepare-line"><Icon icon="mdi:loading" class="spin" aria-hidden="true" /> {{ hostOpProgressLabel(preparation(h)!) }}</p>
              <p v-if="preparation(h)!.log?.length" class="prepare-log">{{ preparation(h)!.log![preparation(h)!.log!.length - 1] }}</p>
            </template>
            <p v-else-if="preparation(h)!.state === 'failed'" class="prepare-line error" role="alert">
              {{ setupErrorMessage(preparation(h)!.error?.code, { fallback: preparation(h)!.error?.message }) }}
            </p>
            <p v-else-if="preparation(h)!.state === 'succeeded' && h.state === 'running'" class="prepare-line ok">
              <Icon icon="mdi:check-circle" aria-hidden="true" /> The components are ready.
            </p>
          </div>

          <div v-if="h.access.can_manage && h.access.profiles?.length" class="access">
            <button type="button" class="access-toggle" :aria-expanded="!!accessOpen[h.id]" @click="toggleAccess(h)">
              <Icon :icon="accessOpen[h.id] ? 'mdi:chevron-down' : 'mdi:chevron-right'" aria-hidden="true" />
              Other profiles on this computer's USB ports
              <span class="access-count">{{ grantedCount(h) }} of {{ h.access.profiles.length }} allowed</span>
            </button>
            <div v-if="accessOpen[h.id]" class="access-body">
              <p class="access-hint">
                An allowed profile can connect gateways plugged into {{ hostName(h) }}. It only ever claims
                unclaimed ones, and what it connects stays its own.
              </p>
              <ul class="access-list">
                <li v-for="p in h.access.profiles" :key="p.profile_id" class="access-row">
                  <span class="access-name">{{ p.profile }}</span>
                  <ElSwitch
                    :model-value="p.granted"
                    :loading="busy === `${h.id}:access:${p.profile_id}`"
                    :aria-label="`Let ${p.profile} use ${hostName(h)}'s USB ports`"
                    @update:model-value="(v: string | number | boolean) => setAccess(h, p.profile_id, !!v)"
                  />
                </li>
              </ul>
            </div>
          </div>
        </div>

        <div class="host-actions">
          <ElButton
            v-if="h.access.can_use"
            size="small"
            :type="canConnect(h) ? 'primary' : undefined"
            :disabled="!canConnect(h)"
            @click="emit('connect', h.id)"
          >
            <Icon icon="mdi:usb-port" class="btn-icon" aria-hidden="true" /> Connect a gateway here
          </ElButton>
          <ElButton
            v-if="canPrepare(h)"
            size="small"
            :type="h.state === 'unavailable' ? 'primary' : undefined"
            :loading="busy === `${h.id}:prepare` || preparing(h)"
            @click="prepare(h)"
          >
            {{ preparation(h)?.state === 'failed' ? 'Try again' : 'Prepare components' }}
          </ElButton>
          <ElButton
            v-else-if="h.access.can_use && hostBlock(h)?.action === 'retry'"
            size="small"
            :loading="busy === `${h.id}:check`"
            @click="recheck(h)"
          >
            Check again
          </ElButton>
          <ElButton v-if="h.kind === 'desktop'" size="small" :loading="busy === `${h.id}:remove`" @click="remove(h)">
            Remove
          </ElButton>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.computers { margin-top: 4px; }
.computers-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.computers-title {
  margin: 0 0 4px; font-size: 0.8rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--text-secondary);
}
.computers-sub { margin: 0 0 10px; font-size: 0.8rem; color: var(--text-tertiary); line-height: 1.45; max-width: 640px; }
.empty-note { margin: 0; font-size: 0.86rem; color: var(--text-secondary); }
.host-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.host {
  display: flex; align-items: flex-start; gap: 12px; flex-wrap: wrap;
  padding: 12px 14px; border: 1px solid var(--border-color); border-radius: 10px; background: var(--surface-color);
}
.host-icon { font-size: 26px; color: var(--primary-color); flex-shrink: 0; margin-top: 2px; }
.host-main { flex: 1; min-width: 220px; display: flex; flex-direction: column; gap: 4px; }
.host-title-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.host-title { font-weight: 600; color: var(--text-primary); overflow-wrap: anywhere; }
.host-meta { font-size: 0.8rem; color: var(--text-secondary); }
.host-note { margin: 2px 0 0; display: flex; align-items: flex-start; gap: 6px; font-size: 0.84rem; line-height: 1.5; color: var(--text-primary); }
.host-note :deep(svg) { flex-shrink: 0; margin-top: 3px; }
.host-note.warning :deep(svg) { color: var(--el-color-warning); }
.host-note.info :deep(svg) { color: var(--text-tertiary); }
.prepare { margin-top: 2px; }
.prepare-line { margin: 0; font-size: 0.84rem; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
.prepare-line.error { color: var(--el-color-danger); }
.prepare-line.ok { color: var(--el-color-success); }
.prepare-log { margin: 2px 0 0; font-size: 0.76rem; color: var(--text-tertiary); font-family: var(--font-mono, monospace); overflow-wrap: anywhere; }
.access { margin-top: 4px; }
.access-toggle {
  display: inline-flex; align-items: center; gap: 4px; padding: 0; border: 0; background: none; cursor: pointer;
  font-size: 0.82rem; color: var(--text-secondary);
}
.access-toggle:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; border-radius: 4px; }
.access-count { color: var(--text-tertiary); margin-left: 6px; }
.access-body { margin-top: 6px; padding-left: 18px; }
.access-hint { margin: 0 0 6px; font-size: 0.8rem; color: var(--text-tertiary); line-height: 1.45; }
.access-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.access-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; max-width: 360px; font-size: 0.86rem; }
.access-name { color: var(--text-primary); overflow-wrap: anywhere; }
.host-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.host-actions .el-button + .el-button { margin-left: 0; }
.btn-icon { margin-right: 6px; }
.spin { animation: gc-spin 1s linear infinite; }
@keyframes gc-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
