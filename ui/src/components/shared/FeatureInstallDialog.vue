<script lang="ts">
import type { EmbeddingFeaturesNotInstalledDetail } from '../../services/configApi';

/** What the dialog does once pip succeeded (see `FeatureInstallRequest.onInstalled`). */
export interface FeatureInstallNext {
  /** Stay open on the restart prompt, worded with this text; null closes the dialog. */
  restart: string | null;
  /** Runs once the dialog has closed, and may open it again (a retried save
   *  can hit another missing group). Not named `then`: that would make the
   *  object a thenable, which `await` would call. */
  after?: () => Promise<unknown>;
}

export interface FeatureInstallRequest {
  /** The server's `409 FeatureNotInstalled` body: what is missing, per feature. */
  detail: EmbeddingFeaturesNotInstalledDetail;
  /** Dialog title. */
  title: string;
  /** What needs the dependencies, as the sentence's subject ("Enabling Vector Embedding"). */
  purpose: string;
  /**
   * Runs once the install succeeded. `restartRequired`: something installed
   * loads only after a server restart. Throwing shows the error in the dialog,
   * which then offers the install again.
   */
  onInstalled: (restartRequired: boolean) => Promise<FeatureInstallNext>;
}
</script>

<script setup lang="ts">
/**
 * The optional-dependency install dialog: a save answered `409
 * FeatureNotInstalled` with what is missing; this lists it, pipes pip's output
 * from `/api/features/install` over SSE, then hands over to the caller's
 * `onInstalled` — which retries its save and either closes the dialog or asks
 * for a server restart (heavy features load only in a fresh process), offered
 * right here. Mirrors the pattern on the Agents & Tools page.
 *
 * Used by Settings → Vector Embedding (enabling embedding) and Settings → My
 * Documents (allowing Documentation search, which needs the document readers).
 * Opened through the exposed `open(request)`.
 */
import { computed, ref, shallowRef } from 'vue';
import { ElButton, ElDialog } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../../stores/settings';
import { streamFeaturesInstall, type FeatureInstallEvent } from '../../services/configApi';
import { useServerRestart } from '../../composables/useServerRestart';

const settingsStore = useSettingsStore();
const serverRestart = useServerRestart();

const visible = ref(false);
const request = shallowRef<FeatureInstallRequest | null>(null);
const busy = ref(false);
const log = ref<string[]>([]);
const error = ref<string | null>(null);
/** Set once the install finished and the server must restart: the prompt's text. */
const restartMessage = ref<string | null>(null);

const detail = computed(() => request.value?.detail ?? null);
const title = computed(() => request.value?.title ?? 'Install dependencies');

const extras = computed(() => {
  const d = detail.value;
  if (!d) return [] as string[];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const entry of d.missing) {
    for (const grp of entry.extras) {
      if (!seen.has(grp)) {
        seen.add(grp);
        out.push(grp);
      }
    }
  }
  return out;
});

function open(next: FeatureInstallRequest) {
  request.value = next;
  log.value = [];
  error.value = null;
  restartMessage.value = null;
  busy.value = false;
  visible.value = true;
  // Resolve install_mode early so the Restart button knows whether to route
  // through the Electron IPC bridge or POST /api/system/restart.
  void serverRestart.loadInstallMode();
}

function close() {
  visible.value = false;
  request.value = null;
  log.value = [];
  error.value = null;
  restartMessage.value = null;
}

async function confirmInstall() {
  const req = request.value;
  if (!req || busy.value) return;
  busy.value = true;
  error.value = null;
  log.value = [];

  const handleEvent = (evt: FeatureInstallEvent) => {
    const prefix = evt.event === 'error' ? '✖' : evt.event === 'done' ? '✓' : '•';
    if (evt.message) {
      log.value.push(`${prefix} ${evt.message}`);
    }
  };

  try {
    const result = await streamFeaturesInstall(
      settingsStore.agentUrl,
      settingsStore.authToken,
      req.detail.missing.map((m) => m.feature_key),
      handleEvent,
    );
    if (!result.ok || result.failed.length) {
      error.value = result.error || `Install failed for: ${result.failed.join(', ')}`;
      busy.value = false;
      return;
    }
    const next = await req.onInstalled(!!result.restart_required);
    if (next.restart) {
      restartMessage.value = next.restart;
      busy.value = false;
      return;
    }
    close();
    if (next.after) await next.after();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Install stream failed';
    busy.value = false;
  }
}

async function restartFromDialog() {
  await serverRestart.loadInstallMode();
  await serverRestart.restart();
  // The page is about to be reloaded (Docker/Electron supervisor
  // respawns) or the connection will drop (no supervisor) — either
  // way, closing the dialog avoids leaving a stale "Install complete"
  // banner up if the page survives.
  if (serverRestart.phase.value === 'reconnected') {
    close();
  }
}

defineExpose({ open, close });
</script>

<template>
  <ElDialog
    v-model="visible"
    :title="title"
    width="560px"
    append-to-body
    :close-on-click-modal="!busy"
    :close-on-press-escape="!busy"
    :show-close="!busy"
  >
    <div v-if="detail" class="feature-install-body">
      <p>
        {{ request?.purpose }} requires the following optional
        dependency group<span v-if="extras.length !== 1">s</span>:
        <code>cremind[{{ extras.join(',') }}]</code>.
      </p>
      <ul class="feature-install-list">
        <li v-for="entry in detail.missing" :key="entry.feature_key">
          <code>{{ entry.feature_key }}</code>
          <span v-if="entry.requires_restart_after_install" class="feature-install-restart-tag">
            · restart required after install
          </span>
        </li>
      </ul>

      <div v-if="log.length" class="feature-install-log">
        <div v-for="(line, i) in log" :key="i">{{ line }}</div>
      </div>

      <p v-if="error" class="feature-install-error">
        {{ error }}
      </p>

      <p v-if="restartMessage" class="feature-install-restart">
        {{ restartMessage }}
      </p>
      <p
        v-if="restartMessage && serverRestart.error.value"
        class="feature-install-error"
      >
        Restart failed: {{ serverRestart.error.value }}
      </p>
    </div>
    <template #footer>
      <ElButton
        v-if="!restartMessage"
        @click="close"
        :disabled="busy"
      >
        Cancel
      </ElButton>
      <ElButton
        v-if="!restartMessage"
        type="primary"
        :loading="busy"
        @click="confirmInstall"
      >
        {{ error ? 'Retry install' : 'Install' }}
      </ElButton>
      <template v-if="restartMessage">
        <ElButton
          @click="close"
          :disabled="serverRestart.isBusy.value"
        >
          Close
        </ElButton>
        <!-- ``autofocus`` lands focus on Restart so a Tab-less user
             can hit Enter to proceed; the Close button is still one
             Tab away for anyone who wants to dismiss without
             restarting. The Element Plus button forwards the
             attribute to the underlying <button>. -->
        <ElButton
          type="primary"
          autofocus
          :loading="serverRestart.isBusy.value"
          :disabled="serverRestart.isBusy.value"
          @click="restartFromDialog"
        >
          <Icon icon="mdi:restart" style="margin-right: 4px" />
          Restart
        </ElButton>
      </template>
    </template>
  </ElDialog>
</template>

<style scoped>
.feature-install-body p { margin: 0 0 12px 0; font-size: 0.875rem; line-height: 1.5; }
.feature-install-body code { background: var(--surface-color); padding: 1px 4px; border-radius: 3px; font-size: 0.8rem; }
.feature-install-list { margin: 0 0 12px 18px; padding: 0; font-size: 0.825rem; color: var(--text-secondary); }
.feature-install-list li { margin-bottom: 2px; }
/* Bare text on the dialog body, so it needs a token: the fixed amber was
   near-unreadable against the dark-mode background. */
.feature-install-restart-tag { color: var(--el-color-warning); }
/* Same frame as the Agents & Tools install log: on ``--surface-color`` with
   no border the box was invisible against the light-mode dialog body, which
   made a streaming install look like nothing was happening. */
.feature-install-log {
  max-height: 240px; overflow-y: auto; margin: 12px 0;
  padding: 10px 12px; background: var(--bg-color);
  border: 1px solid var(--border-color);
  border-radius: 6px; color: var(--text-primary);
  font-family: var(--font-mono, monospace);
  font-size: 0.75rem; line-height: 1.4;
}
.feature-install-log > div { white-space: pre-wrap; }
/* Semantic tokens, not fixed hex: these banners sit inside the install dialog
   and hard-coded light tints rendered as near-white blocks in dark mode. */
.feature-install-error {
  margin: 8px 0 0 0; padding: 8px 12px;
  background: var(--surface-hover); border: 1px solid var(--el-color-danger); border-radius: 6px;
  color: var(--el-color-danger); font-size: 0.825rem;
}
.feature-install-restart {
  margin: 8px 0 0 0; padding: 10px 12px;
  background: var(--surface-hover); border: 1px solid var(--el-color-warning); border-radius: 6px;
  color: var(--el-color-warning); font-size: 0.825rem; line-height: 1.5;
}
</style>
