<script setup lang="ts">
/**
 * Updates settings page — single unified status + Update button. Shell
 * and backend updates are coordinated through the ``useUpdate``
 * composable so this view shows exactly the same state as the global
 * UpdateBanner.
 *
 * Preferences (auto-update toggle, channel display) remain Electron-
 * only; under the web build they're grayed out.
 */

import { computed, onMounted, onBeforeUnmount, ref } from 'vue';
import { useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import {
  ElButton, ElCard, ElDialog, ElEmpty, ElMessageBox, ElSwitch, ElTag, ElRadio, ElRadioGroup,
  ElSelect, ElOption,
} from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../stores/settings';
import { useUpdate } from '../composables/useUpdate';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

const APP_VERSION =
  typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : '';

const { state, isElectron, checkNow, applyUpdate, applyShellRestart } = useUpdate();

// ── Manual "Check for updates" ───────────────────────────────────────────

const checking = ref(false);
const lastCheckedAt = ref<Date | null>(null);

async function doCheck() {
  checking.value = true;
  try {
    await checkNow();
    lastCheckedAt.value = new Date();
  } finally {
    checking.value = false;
  }
}

// "5 seconds ago" / "3 minutes ago" — re-derived from ``lastCheckedAt``
// every 10 seconds so the label stays roughly fresh without a re-render
// per second.
const now = ref(Date.now());

const lastCheckedLabel = computed(() => {
  if (!lastCheckedAt.value) return 'Not checked yet';
  const seconds = Math.max(0, Math.floor((now.value - lastCheckedAt.value.getTime()) / 1000));
  if (seconds < 5) return 'Just now';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return lastCheckedAt.value.toLocaleString();
});

// ── Preferences (Electron only) ──────────────────────────────────────────

const autoUpdate = computed({
  get: () => settingsStore.autoUpdate,
  set: (value: boolean) => {
    settingsStore.setAutoUpdate(value);
  },
});

// Release channel is fixed at install time. We display it read-only.
const installChannel = computed<string>(() => {
  if (isElectron() && window.cremind?.config?.channel) {
    return window.cremind.config.channel;
  }
  return 'unknown';
});

// ── Status panel ─────────────────────────────────────────────────────────

type BadgeTone = 'success' | 'info' | 'warning' | 'danger';

const statusBadge = computed<{ label: string; tone: BadgeTone }>(() => {
  switch (state.value.phase) {
    case 'idle': return { label: 'Up to date', tone: 'success' };
    case 'available':
      return {
        label: state.value.latestVersion
          ? `Update available: v${state.value.latestVersion}`
          : 'Update available',
        tone: 'warning',
      };
    case 'applying': return { label: 'Updating…', tone: 'info' };
    case 'restart_required': return { label: 'Restart to finish', tone: 'warning' };
    case 'done': return { label: 'Updated', tone: 'success' };
    case 'failed': return { label: 'Update failed', tone: 'danger' };
    case 'blocked': return { label: 'Too old to upgrade', tone: 'danger' };
  }
});

const installedVersion = computed(() =>
  state.value.currentVersion ?? APP_VERSION ?? 'unknown',
);

function onPrimaryButton() {
  if (state.value.phase === 'restart_required') {
    void applyShellRestart();
  } else {
    void applyUpdate();
  }
}

const primaryButtonLabel = computed(() => {
  switch (state.value.phase) {
    case 'restart_required': return 'Restart now';
    case 'failed': return 'Retry';
    default: return 'Update now';
  }
});

// ── Test-channel version picker ──────────────────────────────────────────
//
// Only the test channel exposes a list of releases (the backend returns an
// empty list on production/dev), so the picker is strictly test-only. It
// lets a tester switch to a specific PR's RC — including an older one than
// the current install — which the latest-only "Update now" can't do.

// Prefer the backend-reported channel: on Docker/web there is no Electron
// config, so installChannel would read 'unknown'.
const activeChannel = computed<string>(
  () => state.value.channel ?? installChannel.value,
);
// Always show the picker on the test channel — even when the list is
// empty — so a maintainer who expects to see it isn't left guessing
// whether the feature was removed. The empty-state inside the card
// explains why no versions are listed and points at the likely cause
// (installed wheel older than the latest RC tag format).
const showVersionPicker = computed(() => activeChannel.value === 'test');

const selectedVersion = ref<string>('');

function fmtPublished(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString();
}

function versionLabel(v: { version: string; published_at: string }): string {
  const installed = v.version === installedVersion.value ? '  (installed)' : '';
  const date = fmtPublished(v.published_at);
  return date ? `${v.version} — ${date}${installed}` : `${v.version}${installed}`;
}

const canInstallSelected = computed(
  () =>
    !!selectedVersion.value
    && selectedVersion.value !== installedVersion.value
    && state.value.phase !== 'applying',
);

function installSelectedVersion() {
  if (canInstallSelected.value) {
    void applyUpdate(selectedVersion.value);
  }
}

// ── Uninstall (Electron-only) ────────────────────────────────────────────
//
// Settings → Updates is the natural home for app lifecycle, so the
// "Danger zone" Uninstall card lives here too. The flow is:
//   1. User clicks Uninstall — modal prompts keep vs purge.
//   2. We confirm with a second prompt (purge is destructive).
//   3. Main process spawns install/uninstall.{sh,ps1} via the IPC bridge.
//   4. Log lines stream into a modal so the user can watch progress.
//   5. On exit 0, we drop the cached agentUrl and relaunch into the
//      first-run installer (or just close — main also resets the config).

const uninstallDialogOpen = ref(false);
const uninstallMode = ref<'keep' | 'purge'>('keep');
const uninstallRunning = ref(false);
const uninstallLogOpen = ref(false);
const uninstallLogLines = ref<string[]>([]);
const uninstallFinished = ref<{ exitCode: number; mode?: 'keep' | 'purge' } | null>(null);

function appendUninstallLog(entry: { stream: string; line: string }) {
  // The script writes whole-line chunks but spawn sometimes coalesces
  // multiple lines into one ``data`` event. Split on newlines so the
  // log view scrolls one row per script line.
  const text = entry.line ?? '';
  for (const raw of text.split(/\r?\n/)) {
    if (raw === '' && uninstallLogLines.value.length === 0) continue;
    uninstallLogLines.value.push(raw);
  }
}

function handleUninstallDone(result: { exitCode: number; mode?: 'keep' | 'purge'; error?: string }) {
  uninstallRunning.value = false;
  uninstallFinished.value = { exitCode: result.exitCode, mode: result.mode };
  if (result.error) {
    uninstallLogLines.value.push(`[error] ${result.error}`);
  }
  // Detach the listeners now that we're done so they don't survive into
  // a later run on the same view instance.
  if (window.cremind?.installer?.offUninstallLog) {
    window.cremind.installer.offUninstallLog(appendUninstallLog);
  }
  if (window.cremind?.installer?.offUninstallDone) {
    window.cremind.installer.offUninstallDone(handleUninstallDone);
  }
}

async function openUninstallDialog() {
  if (!isElectron() || !window.cremind?.installer?.uninstall) {
    return;
  }
  uninstallMode.value = 'keep';
  uninstallDialogOpen.value = true;
}

async function confirmAndRunUninstall() {
  const mode = uninstallMode.value;
  const action = mode === 'purge' ? 'permanently delete all Cremind data' : 'uninstall Cremind (data preserved)';
  try {
    await ElMessageBox.confirm(
      `This will ${action}. Continue?`,
      'Uninstall Cremind',
      {
        type: mode === 'purge' ? 'error' : 'warning',
        confirmButtonText: mode === 'purge' ? 'Remove all data' : 'Uninstall',
        cancelButtonText: 'Cancel',
        confirmButtonClass: mode === 'purge' ? 'el-button--danger' : '',
      },
    );
  } catch {
    return; // user cancelled
  }
  uninstallDialogOpen.value = false;
  uninstallLogLines.value = [];
  uninstallFinished.value = null;
  uninstallLogOpen.value = true;
  uninstallRunning.value = true;

  if (window.cremind?.installer?.onUninstallLog) {
    window.cremind.installer.onUninstallLog(appendUninstallLog);
  }
  if (window.cremind?.installer?.onUninstallDone) {
    window.cremind.installer.onUninstallDone(handleUninstallDone);
  }

  try {
    await window.cremind!.installer!.uninstall!(mode);
  } catch (err: any) {
    uninstallRunning.value = false;
    uninstallFinished.value = { exitCode: -1, mode };
    uninstallLogLines.value.push(`[error] ${err?.message ?? String(err)}`);
  }
}

function closeUninstallLog() {
  // While the script is running we won't let the user close — once it
  // finishes we drop the listeners and either relaunch (purge) or just
  // close the dialog (keep).
  if (uninstallRunning.value) return;
  uninstallLogOpen.value = false;

  if (uninstallFinished.value && uninstallFinished.value.exitCode === 0) {
    // Main process also resets the config; we relay so the next route
    // navigation hits the first-run installer.
    void window.cremind?.setConfig({ agentUrl: '', deploymentType: '' });
    // A relaunch isn't strictly necessary — navigating to /setup achieves
    // the same UI state — but it also clears any cached subprocess
    // handles and forces the install-marker re-check.
    void router.push('/setup');
  }
}

// ── Lifecycle ────────────────────────────────────────────────────────────

onMounted(() => {
  void doCheck();
  setInterval(() => { now.value = Date.now(); }, 10_000);
});

onBeforeUnmount(() => {
  if (window.cremind?.installer?.offUninstallLog) {
    window.cremind.installer.offUninstallLog(appendUninstallLog);
  }
  if (window.cremind?.installer?.offUninstallDone) {
    window.cremind.installer.offUninstallDone(handleUninstallDone);
  }
});

function goBack() {
  goBackToChat(router, props.profile);
}
</script>

<template>
  <div class="updates-page">
    <div class="updates-container">
      <div class="updates-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Chat
        </button>
        <h1 class="updates-title">Updates</h1>
        <p class="updates-subtitle">
          Check for new versions of Cremind and install them with one click.
        </p>
      </div>

      <!-- ── Status panel ───────────────────────────────────────────── -->
      <ElCard class="section-card" shadow="never">
        <div class="status-line">
          <Icon icon="mdi:package-variant-closed" class="status-icon" />
          <div class="status-text">
            <div class="status-title">Cremind</div>
            <div class="status-meta">
              v{{ installedVersion }}
            </div>
          </div>
          <ElTag :type="statusBadge.tone" effect="light" round>
            {{ statusBadge.label }}
          </ElTag>
        </div>

        <div class="check-row">
          <span class="last-checked">Last checked: {{ lastCheckedLabel }}</span>
          <ElButton
            type="primary"
            :loading="checking"
            @click="doCheck"
          >
            <Icon icon="mdi:refresh" />
            <span style="margin-left: 6px">Check for updates</span>
          </ElButton>
        </div>
      </ElCard>

      <!-- ── Update available / action card ─────────────────────────── -->
      <ElCard
        v-if="state.phase === 'available' || state.phase === 'failed' || state.phase === 'restart_required'"
        class="section-card detail-card"
        shadow="never"
      >
        <h3 class="section-title">
          <template v-if="state.phase === 'available'">Update to v{{ state.latestVersion }}</template>
          <template v-else-if="state.phase === 'restart_required'">Restart to finish update</template>
          <template v-else>Update failed</template>
        </h3>
        <p class="section-body">
          <template v-if="state.phase === 'available' && state.currentVersion && state.latestVersion">
            Move Cremind from v{{ state.currentVersion }} to v{{ state.latestVersion }}.
            Click <strong>Update now</strong> and Cremind will install the new version,
            run migrations, and restart automatically. No commands needed.
          </template>
          <template v-else-if="state.phase === 'restart_required'">
            The downloaded update is staged. Click <strong>Restart now</strong>
            to apply it.
          </template>
          <template v-else>
            {{ state.error }}
          </template>
        </p>
        <div class="action-row">
          <ElButton type="primary" @click="onPrimaryButton">
            <Icon
              :icon="state.phase === 'restart_required' ? 'mdi:restart' : 'mdi:download'"
            />
            <span style="margin-left: 6px">{{ primaryButtonLabel }}</span>
          </ElButton>
          <a
            v-if="state.releaseUrl"
            :href="state.releaseUrl"
            target="_blank"
            rel="noopener"
            class="notes-link"
          >
            View release notes
          </a>
        </div>
      </ElCard>

      <!-- ── Blocked card (backend too old) ──────────────────────────── -->
      <ElCard
        v-else-if="state.phase === 'blocked'"
        class="section-card detail-card blocked"
        shadow="never"
      >
        <h3 class="section-title">Too old to upgrade in place</h3>
        <p class="section-body">
          {{ state.error }} See the release notes for the legacy export path.
        </p>
        <p class="section-hint" v-if="state.releaseUrl">
          <a :href="state.releaseUrl" target="_blank" rel="noopener">View release notes</a>
        </p>
      </ElCard>

      <!-- ── Test-channel version picker (test channel only) ─────────── -->
      <ElCard
        v-if="showVersionPicker"
        class="section-card"
        shadow="never"
      >
        <h3 class="section-title">Switch to a specific test version</h3>
        <p class="section-hint">
          Install a particular release candidate instead of the latest — handy
          when another PR's RC has appeared and you want to test a specific one.
        </p>
        <template v-if="state.availableVersions.length > 0">
          <div class="version-picker-row">
            <ElSelect
              v-model="selectedVersion"
              placeholder="Select a test release"
              :disabled="state.phase === 'applying'"
              class="version-select"
              filterable
            >
              <ElOption
                v-for="v in state.availableVersions"
                :key="v.version"
                :label="versionLabel(v)"
                :value="v.version"
              />
            </ElSelect>
            <ElButton
              type="primary"
              :disabled="!canInstallSelected"
              @click="installSelectedVersion"
            >
              <Icon icon="mdi:swap-horizontal" />
              <span style="margin-left: 6px">Install selected version</span>
            </ElButton>
          </div>
          <p class="section-hint">
            Installing an older build than the one you're on may not run cleanly
            over a database a newer build already migrated. A backup is taken and
            rolled back automatically if the switch fails.
          </p>
        </template>
        <ElEmpty
          v-else
          description="No test releases available."
          :image-size="80"
        >
          <p class="section-hint version-picker-empty-hint">
            The backend hasn't reported any matching RC prereleases on GitHub.
            This usually means the installed <code>cremind</code> wheel is older
            than the most recent RC and doesn't recognize the current tag
            format — reinstall from the test channel to refresh, or cut a
            fresher RC.
          </p>
        </ElEmpty>
      </ElCard>

      <!-- ── Preferences ───────────────────────────────────────────── -->
      <ElCard class="section-card" shadow="never">
        <h3 class="section-title">Preferences</h3>

        <div class="pref-row" :class="{ disabled: !isElectron() }">
          <div class="pref-info">
            <div class="pref-label">Automatically check for updates</div>
            <div class="pref-hint">
              When on, the desktop app checks for updates each time it starts.
              When off, use "Check for updates" above to check manually.
            </div>
          </div>
          <ElSwitch
            v-model="autoUpdate"
            :disabled="!isElectron()"
          />
        </div>

        <div class="pref-row info-only">
          <div class="pref-info">
            <div class="pref-label">Release channel</div>
            <div class="pref-hint">
              Set at install time. Re-run the installer to switch channels.
              Each channel only sees its own releases.
            </div>
          </div>
          <ElTag effect="plain">{{ activeChannel }}</ElTag>
        </div>

        <p v-if="!isElectron()" class="pref-disabled-note">
          The auto-update toggle only applies to the desktop app. Web UI
          users still get the one-click Update button above.
        </p>
      </ElCard>

      <!-- ── Danger zone: Uninstall (Electron only) ─────────────────── -->
      <ElCard
        v-if="isElectron()"
        class="section-card danger-card"
        shadow="never"
      >
        <h3 class="section-title danger-title">
          <Icon icon="mdi:alert-octagon-outline" />
          <span style="margin-left: 6px">Uninstall Cremind</span>
        </h3>
        <p class="section-body">
          Remove Cremind from this machine. Choose <strong>Keep data</strong>
          to preserve your conversations and settings (a future reinstall
          will pick up where you left off), or <strong>Remove all data</strong>
          to wipe everything Cremind stored on this machine. Your User
          Working Directory (documents you've created) is never touched.
        </p>
        <div class="action-row">
          <ElButton type="danger" plain @click="openUninstallDialog">
            <Icon icon="mdi:delete-outline" />
            <span style="margin-left: 6px">Uninstall…</span>
          </ElButton>
        </div>
      </ElCard>
    </div>

    <!-- ── Uninstall mode picker dialog ─────────────────────────────── -->
    <ElDialog
      v-model="uninstallDialogOpen"
      title="Uninstall Cremind"
      width="480px"
      :close-on-click-modal="false"
    >
      <p class="section-body">
        Choose what happens to the data Cremind has stored on this machine.
        The User Working Directory (your documents) is preserved either way.
      </p>
      <ElRadioGroup v-model="uninstallMode" class="uninstall-options">
        <ElRadio value="keep" size="large">
          <div class="uninstall-option">
            <div class="uninstall-option-title">Keep existing data</div>
            <div class="uninstall-option-hint">
              Remove the binaries but preserve <code>.env</code>,
              <code>bootstrap.toml</code>, <code>storage/</code>, and tokens.
              A future install reuses them.
            </div>
          </div>
        </ElRadio>
        <ElRadio value="purge" size="large">
          <div class="uninstall-option">
            <div class="uninstall-option-title">Remove all data</div>
            <div class="uninstall-option-hint">
              Delete the entire System Directory. For Docker installs,
              also runs <code>docker compose down -v</code> to remove
              named volumes. Cannot be undone.
            </div>
          </div>
        </ElRadio>
      </ElRadioGroup>
      <template #footer>
        <ElButton @click="uninstallDialogOpen = false">Cancel</ElButton>
        <ElButton
          :type="uninstallMode === 'purge' ? 'danger' : 'primary'"
          @click="confirmAndRunUninstall"
        >
          Continue
        </ElButton>
      </template>
    </ElDialog>

    <!-- ── Uninstall progress / log dialog ─────────────────────────── -->
    <ElDialog
      v-model="uninstallLogOpen"
      :title="uninstallRunning ? 'Uninstalling…' : 'Uninstall finished'"
      width="640px"
      :close-on-click-modal="false"
      :show-close="!uninstallRunning"
      :before-close="(done: () => void) => { if (!uninstallRunning) done(); }"
    >
      <div class="uninstall-log">
        <div v-for="(line, idx) in uninstallLogLines" :key="idx" class="uninstall-log-line">{{ line }}</div>
      </div>
      <div v-if="uninstallFinished" class="uninstall-finished-line">
        <template v-if="uninstallFinished.exitCode === 0">
          <Icon icon="mdi:check-circle" class="finished-ok" />
          <span style="margin-left: 6px">
            Cremind has been
            {{ uninstallFinished.mode === 'purge' ? 'uninstalled and all data removed' : 'uninstalled (data preserved)' }}.
          </span>
        </template>
        <template v-else>
          <Icon icon="mdi:alert-circle" class="finished-fail" />
          <span style="margin-left: 6px">Uninstall failed (exit code {{ uninstallFinished.exitCode }}).</span>
        </template>
      </div>
      <template #footer>
        <ElButton
          type="primary"
          :disabled="uninstallRunning"
          @click="closeUninstallLog"
        >
          {{
            uninstallRunning ? 'Working…'
              : uninstallFinished?.exitCode === 0 ? 'Continue to Setup'
              : 'Close'
          }}
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.updates-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.updates-container { max-width: 720px; margin: 0 auto; }
.updates-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.updates-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.updates-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }

.section-card { margin-bottom: 12px; background: var(--surface-color); }
.section-title { font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 8px 0; }
.section-body { font-size: 0.875rem; color: var(--text-secondary); margin: 0 0 12px 0; line-height: 1.5; }
.section-hint { font-size: 0.8rem; color: var(--text-tertiary); margin: 8px 0 0 0; }
.section-hint a { color: var(--primary-color); }

.version-picker-row {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 12px;
  flex-wrap: wrap;
}
.version-select { flex: 1; min-width: 240px; }
.version-picker-empty-hint { text-align: center; max-width: 480px; margin: 8px auto 0; }

.detail-card { border-left: 3px solid var(--el-color-warning); }
.detail-card.blocked { border-left-color: var(--el-color-danger); }

.status-line {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 0;
}
.status-icon {
  font-size: 22px; color: var(--text-tertiary); flex-shrink: 0;
}
.status-text { flex: 1; min-width: 0; }
.status-title { font-size: 0.9rem; font-weight: 600; color: var(--text-primary); }
.status-meta { font-size: 0.8rem; color: var(--text-tertiary); margin-top: 2px; }

.check-row {
  display: flex; align-items: center; justify-content: space-between;
  margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border-color);
  gap: 12px;
}
.last-checked { font-size: 0.8rem; color: var(--text-tertiary); }

.action-row {
  display: flex; align-items: center; gap: 12px;
}
.notes-link {
  font-size: 0.8rem; color: var(--primary-color);
}

.pref-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--border-color);
}
.pref-row:last-child { border-bottom: none; }
.pref-row.disabled .pref-label,
.pref-row.disabled .pref-hint { opacity: 0.55; }
.pref-info { flex: 1; min-width: 0; }
.pref-label {
  font-size: 0.9rem; font-weight: 500; color: var(--text-primary);
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
.pref-hint { font-size: 0.8rem; color: var(--text-tertiary); margin-top: 4px; line-height: 1.4; }
.pref-disabled-note {
  font-size: 0.8rem; color: var(--text-tertiary); font-style: italic;
  margin: 12px 0 0 0;
}
.info-only { opacity: 0.9; }

.danger-card {
  border-left: 3px solid var(--el-color-danger);
  margin-top: 24px;
}
.danger-title { color: var(--el-color-danger); display: flex; align-items: center; }
.uninstall-options { display: flex; flex-direction: column; gap: 12px; margin-top: 8px; }
.uninstall-option { display: flex; flex-direction: column; gap: 2px; padding: 4px 0; }
.uninstall-option-title { font-weight: 600; color: var(--text-primary); }
.uninstall-option-hint { font-size: 0.8rem; color: var(--text-tertiary); line-height: 1.4; }
.uninstall-log {
  max-height: 320px; overflow-y: auto; background: var(--bg-color);
  border: 1px solid var(--border-color); border-radius: 4px;
  padding: 8px 12px; font-family: var(--font-mono, monospace);
  font-size: 0.78rem; line-height: 1.5; color: var(--text-primary);
}
.uninstall-log-line { white-space: pre-wrap; word-break: break-word; }
.uninstall-finished-line {
  display: flex; align-items: center; margin-top: 12px;
  font-size: 0.875rem; color: var(--text-primary);
}
.finished-ok { color: var(--el-color-success); font-size: 20px; }
.finished-fail { color: var(--el-color-danger); font-size: 20px; }
</style>
