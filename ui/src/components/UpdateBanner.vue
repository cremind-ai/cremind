<script setup lang="ts">
/**
 * Unified update banner — one card for "a new version of Cremind is
 * available," regardless of whether the change is the Electron shell,
 * the Python backend, or both. All state and actions live in the
 * shared ``useUpdate`` composable so the Updates page renders the
 * same status.
 *
 * Mounted globally from App.vue so it appears on every route except
 * the installer (which has its own progress UI).
 */

import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUpdate } from '../composables/useUpdate'

const route = useRoute()
const router = useRouter()

const { state, isElectron, applyUpdate, applyShellRestart, dismiss } = useUpdate()

const dismissedBanner = ref(false)

// ── Modal lifecycle ──────────────────────────────────────────────────────
//
// The modal is open whenever the upgrade is in flight or has reached
// a terminal state the user still has to acknowledge (done / failed /
// restart_required).

const modalOpen = computed(() =>
  state.value.phase === 'applying'
  || state.value.phase === 'restart_required'
  || state.value.phase === 'failed'
  || state.value.phase === 'done',
)

const upgradeInFlight = computed(() => state.value.phase === 'applying')

// Auto-scroll the log to the bottom as new lines arrive.
const logEl = ref<HTMLElement | null>(null)
watch(
  () => state.value.log.length,
  () => {
    nextTick(() => {
      const el = logEl.value
      if (el) el.scrollTop = el.scrollHeight
    })
  },
)

// ── Banner visibility ───────────────────────────────────────────────────

const bannerVisible = computed(() => {
  if (dismissedBanner.value) return false
  return state.value.phase === 'available' || state.value.phase === 'blocked'
})

// ── Done state auto-close ───────────────────────────────────────────────
//
// After a successful upgrade, hold the success view for a few seconds
// so the user sees confirmation, then dismiss back to ``idle``. The
// composable already re-fetched ``/api/upgrade/check``, so the banner
// stays hidden if the new backend reports up_to_date.

watch(
  () => state.value.phase,
  (p) => {
    if (p === 'done') {
      setTimeout(() => dismiss(), 4000)
    }
  },
)

// ── "Manage in Updates" link ────────────────────────────────────────────

const updatesHref = computed(() => {
  const profile = route.params.profile
  if (typeof profile !== 'string' || !profile) return ''
  return `/${profile}/updates`
})

function openUpdates() {
  if (updatesHref.value) router.push(updatesHref.value)
}

function closeModalIfTerminal() {
  // Only allow closing in a terminal state; during the upgrade the
  // backend may be mid-restart and dismissing the modal would strand
  // the user with no banner and no way back.
  if (
    state.value.phase === 'done'
    || state.value.phase === 'failed'
    || state.value.phase === 'restart_required'
  ) {
    dismiss()
  }
}

function onPrimaryButton() {
  if (state.value.phase === 'restart_required') {
    void applyShellRestart()
  } else if (state.value.phase === 'failed') {
    void applyUpdate()
  } else if (state.value.canApply) {
    void applyUpdate()
  }
}

const primaryButtonLabel = computed(() => {
  switch (state.value.phase) {
    case 'restart_required': return 'Restart now'
    case 'failed': return 'Retry'
    default: return 'Update now'
  }
})

function logLineClass(line: string): string {
  if (line.startsWith('[shell]')) return 'log-shell'
  if (line.startsWith('[backend]')) return 'log-backend'
  return ''
}
</script>

<template>
  <!-- Banner — single unified card. -->
  <div
    v-if="bannerVisible"
    class="update-banner-stack"
    :class="{ 'has-titlebar': isElectron() }"
  >
    <div
      v-if="state.phase === 'available'"
      class="update-banner available"
    >
      <span class="dot" />
      <div class="text">
        <strong>Cremind update available</strong>
        <template v-if="state.currentVersion && state.latestVersion">
          — v{{ state.currentVersion }} → v{{ state.latestVersion }}.
        </template>
        <template v-else-if="state.latestVersion">
          — v{{ state.latestVersion }} is ready to install.
        </template>
      </div>
      <button class="link primary" @click="onPrimaryButton">
        {{ primaryButtonLabel }}
      </button>
      <a
        v-if="state.releaseUrl"
        class="link"
        :href="state.releaseUrl"
        target="_blank"
        rel="noopener"
      >
        Notes
      </a>
      <button
        v-if="updatesHref"
        class="link"
        @click="openUpdates"
      >
        Manage
      </button>
      <button
        class="dismiss"
        title="Dismiss"
        @click="dismissedBanner = true"
      >×</button>
    </div>

    <div v-if="state.phase === 'blocked'" class="update-banner blocked">
      <span class="dot" />
      <div class="text">
        <strong>Cremind is too old to upgrade in place.</strong>
        {{ state.error }} See release notes for the legacy export path.
      </div>
      <a
        v-if="state.releaseUrl"
        class="link"
        :href="state.releaseUrl"
        target="_blank"
        rel="noopener"
      >
        Notes
      </a>
      <button
        v-if="updatesHref"
        class="link"
        @click="openUpdates"
      >
        Manage
      </button>
      <button
        class="dismiss"
        title="Dismiss"
        @click="dismissedBanner = true"
      >×</button>
    </div>
  </div>

  <!-- Modal — single instance covering applying / failed / restart_required / done. -->
  <Teleport to="body">
    <div
      v-if="modalOpen"
      class="upgrade-modal-overlay"
      @click.self="closeModalIfTerminal"
    >
      <div class="upgrade-modal" role="dialog" aria-modal="true">
        <header class="upgrade-modal-header">
          <strong>Updating Cremind</strong>
          <button
            class="dismiss"
            :disabled="upgradeInFlight"
            :title="upgradeInFlight ? 'Upgrade in progress — please wait' : 'Close'"
            @click="closeModalIfTerminal"
          >×</button>
        </header>

        <div class="upgrade-modal-phase" :class="state.phase">
          <span v-if="upgradeInFlight" class="spinner" aria-hidden="true" />
          <span v-else-if="state.phase === 'done'">✓</span>
          <span v-else-if="state.phase === 'failed'">✕</span>
          <span v-else-if="state.phase === 'restart_required'">↻</span>
          <span>{{ state.phaseLabel }}</span>
        </div>

        <div v-if="upgradeInFlight" class="upgrade-modal-warning">
          Don't close Cremind while the upgrade is running. If it fails,
          the previous version is restored automatically.
        </div>

        <div v-if="state.phase === 'restart_required'" class="upgrade-modal-warning info">
          Restart Cremind to apply the downloaded update.
        </div>

        <div v-if="state.error" class="upgrade-modal-error">
          {{ state.error }}
        </div>

        <div ref="logEl" class="upgrade-modal-log">
          <div v-if="state.log.length === 0">Waiting for output…</div>
          <div v-for="(line, i) in state.log" :key="i" :class="logLineClass(line)">
            {{ line }}
          </div>
        </div>

        <footer class="upgrade-modal-footer">
          <button
            v-if="state.phase === 'restart_required' || state.phase === 'failed'"
            class="link primary"
            @click="onPrimaryButton"
          >
            {{ primaryButtonLabel }}
          </button>
          <button
            class="link"
            :disabled="upgradeInFlight"
            @click="closeModalIfTerminal"
          >
            {{ state.phase === 'done' ? 'Close' : 'Dismiss' }}
          </button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.update-banner-stack {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 1900;  /* below the titlebar (1999) so it doesn't overlap */
  display: flex;
  flex-direction: column;
  pointer-events: none;
}
.update-banner-stack.has-titlebar {
  /* Sit below the 32px Electron titlebar overlay so the min/max/close
     buttons don't cover the banner. */
  top: 32px;
}
.update-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  font-size: 13px;
  pointer-events: auto;
  border-bottom: 1px solid rgba(0, 0, 0, 0.08);
}
.update-banner.available {
  background: #1f4d8a;
  color: #fff;
}
.update-banner.blocked {
  background: #8b2929;
  color: #fff;
}
.update-banner .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}
.update-banner .text {
  flex: 1;
  line-height: 1.4;
}
.update-banner .link {
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.4);
  color: inherit;
  font-size: 12px;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
  text-decoration: none;
}
.update-banner .link:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.12);
}
.update-banner .link:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.update-banner .dismiss {
  background: transparent;
  border: 0;
  color: inherit;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
}
.update-banner .link.primary {
  background: rgba(255, 255, 255, 0.95);
  color: #1f4d8a;
  border-color: transparent;
  font-weight: 600;
}
.update-banner .link.primary:hover:not(:disabled) {
  background: #fff;
}

/* ── Upgrade modal ──────────────────────────────────────────────────── */
.upgrade-modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}
.upgrade-modal {
  background: #1e1e1e;
  color: #eaeaea;
  border: 1px solid #333;
  border-radius: 6px;
  width: min(720px, 90vw);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
}
.upgrade-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid #333;
}
.upgrade-modal-header .dismiss {
  background: transparent;
  border: 0;
  color: inherit;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
}
.upgrade-modal-header .dismiss:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.upgrade-modal-phase {
  padding: 10px 16px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #bbb;
}
.upgrade-modal-phase.done {
  color: #6ec76e;
}
.upgrade-modal-phase.failed {
  color: #e08080;
}
.upgrade-modal-phase.restart_required {
  color: #f0c46c;
}
.upgrade-modal-warning {
  margin: 0 16px 8px;
  padding: 6px 10px;
  font-size: 12px;
  color: #f0c46c;
  background: rgba(240, 196, 108, 0.08);
  border: 1px solid rgba(240, 196, 108, 0.3);
  border-radius: 4px;
}
.upgrade-modal-warning.info {
  color: #7ec3ff;
  background: rgba(126, 195, 255, 0.08);
  border-color: rgba(126, 195, 255, 0.3);
}
.upgrade-modal-log {
  margin: 0 16px;
  padding: 10px;
  flex: 1;
  overflow-y: auto;
  background: #0e0e0e;
  border: 1px solid #2a2a2a;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-word;
  min-height: 200px;
  max-height: 50vh;
}
.upgrade-modal-log .log-shell {
  color: #98c379;
}
.upgrade-modal-log .log-backend {
  color: #c0c0c0;
}
.upgrade-modal-error {
  margin: 10px 16px 0;
  padding: 8px 10px;
  background: rgba(224, 128, 128, 0.1);
  border: 1px solid rgba(224, 128, 128, 0.4);
  color: #e08080;
  border-radius: 4px;
  font-size: 12px;
}
.upgrade-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid #333;
}
.upgrade-modal-footer .link {
  background: transparent;
  border: 1px solid #555;
  color: inherit;
  font-size: 13px;
  padding: 6px 14px;
  border-radius: 4px;
  cursor: pointer;
}
.upgrade-modal-footer .link:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.06);
}
.upgrade-modal-footer .link:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.upgrade-modal-footer .link.primary {
  background: #1f4d8a;
  border-color: transparent;
  color: #fff;
  font-weight: 600;
}
.upgrade-modal-footer .link.primary:hover:not(:disabled) {
  background: #2c66ad;
}
.spinner {
  width: 12px;
  height: 12px;
  border: 2px solid rgba(255, 255, 255, 0.2);
  border-top-color: #6ec76e;
  border-radius: 50%;
  animation: upgrade-spin 0.9s linear infinite;
  display: inline-block;
}
@keyframes upgrade-spin {
  to { transform: rotate(360deg); }
}
</style>
