<script setup lang="ts">
/**
 * Open the VNC desktop that ships with the desktop container image.
 *
 * Reaching it used to be folklore: the port lives in a compose file, the
 * Kubernetes path lives in the chart, and whether a tunnel has to exist first
 * depends on how TLS was switched on. The server settles all of that in one
 * descriptor (``/api/system/environment``'s ``vnc`` block) and this card turns
 * it into a button — a browser tab on the web, a real window under Electron.
 *
 * The password is fetched separately and only on demand: it comes from
 * ``/api/config/install-secrets``, which also carries the database
 * credentials, and nobody should be holding those because a card happened to
 * render a row they never opened.
 */
import { computed, onMounted, ref } from 'vue';
import { ElButton, ElCard, ElMessage, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../../stores/settings';
import { useCopyToClipboard } from '../../composables/useCopyToClipboard';
import DeploymentSteps from '../shared/DeploymentSteps.vue';
import {
  fetchInstallSecrets,
  fetchSystemEnvironment,
  type InstallSecrets,
  type TlsInstructionStep,
  type VncAccess,
} from '../../services/configApi';
import { novncDisplayUrl, novncOpenUrl } from '../../utils/vncDesktop';

const settingsStore = useSettingsStore();

const MASK = '••••••••';

const vnc = ref<VncAccess | null>(null);
const secrets = ref<InstallSecrets | null>(null);
const loadingSecrets = ref(false);
const revealed = ref(false);

// Not navigator.clipboard: a Docker install is reached over plain HTTP, which
// is not a secure context, and that API is simply undefined there. The
// composable falls back to execCommand.
const { copy, isCopied } = useCopyToClipboard();

const accessTag = computed<{ label: string; type: 'success' | 'info' | 'warning' }>(() => {
  switch (vnc.value?.access) {
    case 'same_origin': return { label: 'Same origin', type: 'success' };
    case 'port_forward': return { label: 'Needs a tunnel', type: 'warning' };
    default: return { label: 'Own port', type: 'info' };
  }
});

/** Where the desktop answers *for this browser*. The server names the shape;
 *  only the page knows which address reached the server in the first place. */
const displayUrl = computed(() => (
  vnc.value ? novncDisplayUrl(vnc.value, window.location) : null
));

const tunnelCommands = computed(() => vnc.value?.port_forward_commands ?? []);

// One note per label, one command per line: DeploymentSteps only puts a copy
// button on a command, which is exactly the split wanted here — the label is
// prose, the kubectl line is the thing that must land on the clipboard byte
// for byte.
const tunnelSteps = computed<TlsInstructionStep[]>(() => tunnelCommands.value.flatMap((cmd) => [
  { kind: 'note' as const, text: cmd.label },
  { kind: 'command' as const, text: cmd.command },
]));

/** The address the tunnel makes reachable — stated by the server, because a
 *  tunnel always lands on the machine running kubectl rather than on whatever
 *  host this page was served from. */
const tunnelOpenUrl = computed(() => tunnelCommands.value[0]?.open_url || displayUrl.value);

const passwordText = computed(() => {
  if (!secrets.value) return MASK;
  if (!secrets.value.vnc_password) return 'not available';
  return revealed.value ? secrets.value.vnc_password : MASK;
});

/** Only after a fetch, and only if it found one — an install whose secrets the
 *  server cannot read still gets a card, just without this row filled in. */
const resolution = computed(() => secrets.value?.resolution || '');

/** Deferred until the first reveal or copy. See the file docstring: this
 *  endpoint carries far more than the VNC password. */
async function ensureSecrets() {
  if (secrets.value || loadingSecrets.value) return;
  loadingSecrets.value = true;
  try {
    // Resolves to an ``available: false`` stub rather than rejecting.
    secrets.value = await fetchInstallSecrets(
      settingsStore.agentUrl,
      settingsStore.authToken,
    );
  } finally {
    loadingSecrets.value = false;
  }
}

async function togglePassword() {
  if (!revealed.value) await ensureSecrets();
  revealed.value = !revealed.value;
}

async function copyPassword() {
  await ensureSecrets();
  const password = secrets.value?.vnc_password;
  if (!password) {
    ElMessage.warning('This server does not report a VNC password.');
    return;
  }
  if (!(await copy(password, 'password'))) ElMessage.error('Could not copy the password');
}

async function copyUrl() {
  const url = displayUrl.value;
  if (!url) return;
  if (!(await copy(url, 'url'))) ElMessage.error('Could not copy the URL');
}

async function openDesktop() {
  const display = displayUrl.value;
  if (!display) return;
  const url = novncOpenUrl(display);
  const openInWindow = window.cremind?.openVncDesktop;
  if (openInWindow) {
    const result = await openInWindow(url);
    if (!result?.ok) {
      ElMessage.error(result?.error || 'Could not open the desktop window.');
    }
    return;
  }
  // ``noopener`` makes the spec return null on success as well as on failure,
  // so the return value cannot be tested — a blocked popup is something the
  // browser reports to the user itself.
  window.open(url, '_blank', 'noopener');
}

onMounted(async () => {
  try {
    const env = await fetchSystemEnvironment(
      settingsStore.agentUrl,
      settingsStore.authToken,
    );
    vnc.value = env.vnc ?? null;
  } catch {
    // An install with no desktop is the common case, and this card is one of
    // several on the page — a failed read here means "no card", not an error
    // banner the user cannot act on. The Environment card above reports the
    // same failure with a Retry.
    vnc.value = null;
  }
});
</script>

<template>
  <ElCard v-if="vnc?.enabled && vnc.access" class="desktop-card" shadow="never">
    <template #header>
      <div class="desktop-card-header">
        <Icon icon="mdi:monitor" class="desktop-card-icon" />
        <span>VNC Desktop</span>
        <ElTag :type="accessTag.type" size="small" effect="plain">{{ accessTag.label }}</ElTag>
      </div>
    </template>

    <div class="desktop-actions">
      <ElButton type="primary" :disabled="!displayUrl" @click="openDesktop">
        <Icon icon="mdi:open-in-new" />
        Open desktop
      </ElButton>
      <span v-if="vnc.access === 'port_forward'" class="desktop-hint">
        Needs the tunnel below — start it first, or the page will not load.
      </span>
    </div>

    <p v-if="vnc.scheme_note" class="desktop-note">{{ vnc.scheme_note }}</p>

    <dl class="desktop-grid">
      <dt>noVNC URL</dt>
      <dd class="desktop-value">
        <span class="desktop-mono">{{ displayUrl }}</span>
        <button
          type="button"
          class="icon-btn"
          :class="{ copied: isCopied('url') }"
          :title="isCopied('url') ? 'Copied!' : 'Copy URL'"
          aria-label="Copy the noVNC URL"
          @click="copyUrl"
        ><Icon :icon="isCopied('url') ? 'mdi:check' : 'mdi:content-copy'" /></button>
      </dd>

      <dt>VNC password</dt>
      <dd class="desktop-value">
        <span class="desktop-mono">{{ passwordText }}</span>
        <button
          type="button"
          class="icon-btn"
          :title="revealed ? 'Hide password' : 'Show password'"
          :aria-label="revealed ? 'Hide password' : 'Show password'"
          :disabled="loadingSecrets"
          @click="togglePassword"
        ><Icon :icon="revealed ? 'mdi:eye-off-outline' : 'mdi:eye-outline'" /></button>
        <button
          type="button"
          class="icon-btn"
          :class="{ copied: isCopied('password') }"
          :title="isCopied('password') ? 'Copied!' : 'Copy password'"
          aria-label="Copy the VNC password"
          :disabled="loadingSecrets"
          @click="copyPassword"
        ><Icon :icon="isCopied('password') ? 'mdi:check' : 'mdi:content-copy'" /></button>
      </dd>

      <template v-if="resolution">
        <dt>Resolution</dt>
        <dd>{{ resolution }}</dd>
      </template>
    </dl>

    <template v-if="tunnelSteps.length">
      <h4 class="desktop-subtitle">Reach it from your machine</h4>
      <DeploymentSteps :steps="tunnelSteps" flat />
      <p v-if="tunnelOpenUrl" class="desktop-note">
        Then open <span class="desktop-mono">{{ tunnelOpenUrl }}</span> — or the
        button above, which goes to the same address.
      </p>
    </template>
  </ElCard>
</template>

<style scoped>
.desktop-card {
  background: var(--card-bg, var(--bg-color));
  margin-bottom: 16px;
}
.desktop-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--text-primary);
}
.desktop-card-icon { font-size: 20px; color: var(--primary-color); }

.desktop-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.desktop-hint {
  font-size: 0.85rem;
  color: var(--text-secondary);
}
.desktop-note {
  margin: 0 0 12px 0;
  font-size: 0.85rem;
  line-height: 1.55;
  color: var(--text-secondary);
}

.desktop-grid {
  display: grid;
  grid-template-columns: minmax(120px, max-content) 1fr;
  gap: 6px 20px;
  margin: 0;
  font-size: 0.875rem;
  align-items: baseline;
}
.desktop-grid dt { color: var(--text-secondary); }
.desktop-grid dd {
  margin: 0;
  color: var(--text-primary);
  word-break: break-word;
}
.desktop-value {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.desktop-mono {
  font-family: var(--font-mono, ui-monospace, Consolas, monospace);
  font-size: 0.82rem;
  word-break: break-all;
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  cursor: pointer;
  padding: 1px;
  border-radius: 4px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  transition: color 0.15s ease;
}
.icon-btn:hover { color: var(--primary-color); }
.icon-btn.copied { color: var(--success-color); }
.icon-btn:disabled { cursor: default; opacity: 0.5; }

.desktop-subtitle {
  font-size: 0.925rem;
  font-weight: 600;
  color: var(--text-primary);
  margin: 16px 0 0 0;
}
</style>
