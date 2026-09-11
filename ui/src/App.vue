<script setup lang="ts">
import { computed, watch, onMounted, onUnmounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useChatStore } from './stores/chat';
import { useGroupChatStore } from './stores/groupChat';
import { useSettingsStore } from './stores/settings';
import { useEmbeddingStatusStore } from './stores/embeddingStatus';
import { checkSetupStatus } from './services/configApi';
import { PROFILE_ROUTES, CHAT_ROUTES } from './router/profileRoutes';
import NavRail from './components/NavRail.vue';
import ConversationsPanel from './components/ConversationsPanel.vue';
import UpdateBanner from './components/UpdateBanner.vue';
import FloatingTodoLayer from './components/plan/FloatingTodoLayer.vue';
import HttpsRecoveryHelp from './components/shared/HttpsRecoveryHelp.vue';
import {
  dismissHttpsOverlay,
  httpsTransitionState,
  installHttpsTransitionCoordinator,
  restoreHttpsOverlay,
  retryHttpsTransition,
} from './services/httpsTransition';
import {
  beginMigrationGate,
  installMigrationReadinessResponder,
  migrationReadiness,
  waitForMigrationReady,
} from './services/migrationReadiness';

// Keep these refs top-level so Vue unwraps them in the template. Refs reached
// through a plain object's properties are otherwise exposed as Ref objects.
const httpsPhase = httpsTransitionState.phase;
const httpsError = httpsTransitionState.error;
const httpsTransition = httpsTransitionState.transition;
const httpsOverlayDismissed = httpsTransitionState.overlayDismissed;
const httpsReason = httpsTransitionState.reason;
const httpsReadiness = httpsTransitionState.readiness;
// Carries this tab's one-use handoff ticket while it has one, so the explicit
// link lands on the same page, signed in — not on a bare login screen.
const httpsRecoveryUrl = httpsTransitionState.recoveryUrl;
const httpsInstallMode = httpsTransitionState.installMode;
const httpsPortForward = httpsTransitionState.portForward;
const migrationInProgress = migrationReadiness.migrating;
// Pages that explain the switch themselves (Settings → HTTPS, setup).
const httpsOverlayExcluded = computed(() => [
  'security-settings', 'setup', 'setup-profile',
].includes(route.name as string));
// Either blocking card is showing something the user has put away. The chip
// that replaces them is what keeps dismissal from ever being a one-way door.
const httpsOverlayPending = computed(() =>
  (['waiting', 'moving', 'attention'].includes(httpsPhase.value)
    || (migrationInProgress.value && httpsPhase.value === 'idle'))
  && !httpsOverlayExcluded.value);
// Only the admin profile can reach the HTTPS settings page (the router bounces
// everyone else), so for other profiles the chip states the situation without
// pretending to offer an action.
const httpsPendingActionable = computed(() => currentProfile.value === 'admin');
function openHttpsSettings(): void {
  if (!currentProfile.value) return;
  void router.push({ name: 'security-settings', params: { profile: currentProfile.value } });
}
// The CA download stays on this (plaintext) origin: the recovery listener
// keeps serving /ca.pem after it stops serving the application.
const httpsCaUrl = computed(() => {
  const source = httpsTransition.value?.source_origin ?? window.location.origin;
  return `${source.replace(/\/$/, '')}/ca.pem`;
});
const httpsKubernetes = computed(() =>
  httpsInstallMode.value ? httpsInstallMode.value === 'kubernetes' : null);
const httpsRecoveryMessage = computed(() =>
  httpsReadiness.value?.responded ? httpsReadiness.value.message : null);
// The recovery steps (trust the CA, reconnect the port-forward, open the secure
// address) only mean something once the switch has activated. Before that no
// HTTPS listener exists to trust, tunnel to or open, even though a recovery
// link can already be composed for a prepared switch.
const httpsRecoverable = computed(() =>
  ['activating', 'active'].includes(httpsTransition.value?.phase ?? ''));

const route = useRoute();
const router = useRouter();
const chatStore = useChatStore();
const groupChatStore = useGroupChatStore();
const settingsStore = useSettingsStore();
const embeddingStatusStore = useEmbeddingStatusStore();
let stopHttpsCoordinator: (() => void) | null = null;
let stopElectronMigrationGuard: (() => void) | null = null;
let stopElectronMigrationRelease: (() => void) | null = null;
let releaseElectronMigrationGate: (() => void) | null = null;
let stopMigrationResponder: (() => void) | null = null;

// Detect if running in Electron
const isElectron = computed(() => {
  return typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__;
});

// Get current profile from route
const currentProfile = computed(() => {
  return (route.params.profile as string) || '';
});

// The nav rail is persistent across every profile-scoped page so navigation
// is always one click away. The conversations panel only renders on the chat
// pages (and only when the user hasn't collapsed it).
const showRail = computed(() => {
  const name = route.name as string;
  return !!name && PROFILE_ROUTES.has(name) && !!currentProfile.value;
});

const showConversations = computed(() => {
  return (route.name === 'chat' || route.name === 'conversation') && !!currentProfile.value;
});


// Global vector-embedding busy gate. While a reload/rebuild is in
// flight, the agent refuses to run and embedding-dependent features
// degrade — surface that with a full-screen overlay so the user
// understands the app is briefly unavailable rather than broken.
//
// State is sourced from the shared SSE stream (see
// `services/embeddingStateStream.ts` and `stores/embeddingStatus.ts`).
// We don't poll — the backend pushes a state frame on every transition.

// Don't show the overlay on the EmbeddingSettings page itself (that
// page has its own inline progress display) or in the setup wizard
// (which has its own gating UI on the final step).
const embeddingBusy = computed(
  () => embeddingStatusStore.isBusy
    && route.name !== 'embedding-settings'
    && route.name !== 'setup'
    && route.name !== 'setup-profile',
);

const phaseLabel = computed(() => {
  const p = embeddingStatusStore.phase;
  if (!p) return '';
  return ({
    loading_model: 'Loading embedding model…',
    connecting_store: 'Connecting to vector store…',
    preparing_rebuild: 'Preparing rebuild…',
    rebuilding_places: 'Rebuilding Google Places type embeddings…',
    rebuilding_tools: 'Rebuilding tool & skill embeddings…',
    rebuilding_docs: 'Rebuilding documentation embeddings…',
  } as Record<string, string>)[p] ?? p;
});

// Apply theme to document
const applyTheme = () => {
  document.documentElement.setAttribute('data-theme', settingsStore.theme);
};

watch(() => settingsStore.theme, applyTheme);

watch(
  () => [settingsStore.agentUrl, settingsStore.authToken] as const,
  ([agentUrl, token]) => {
    stopHttpsCoordinator?.();
    stopHttpsCoordinator = null;
    // Every renderer registers with the backend's pre-activation barrier.
    // Electron still delegates the private, per-window handoff and process
    // restart to its main process.
    if (agentUrl) {
      stopHttpsCoordinator = installHttpsTransitionCoordinator(agentUrl, token ?? '');
    }
  },
  { immediate: true },
);

// Async post-navigation handling: server-side profile validation + chat
// reset/connect. Synchronous token activation and login redirect for missing
// tokens are handled by the router beforeEach guard, so views always see a
// valid token by the time their onMounted fires.
async function handleProfileNavigation(
  profileName: string,
  previousProfile: string,
  routeName: string,
) {
  if (!profileName) return;

  // Verify profile still exists on server
  try {
    const status = await checkSetupStatus(settingsStore.agentUrl, profileName);
    if (status.profile_exists === false) {
      router.replace(`/login/${profileName}`);
      return;
    }
  } catch {
    // Server unreachable — proceed with cached token
  }

  // Only chat-rendering routes need the long-lived ``profile-events`` SSE.
  // Opening it on every profile route (Events, Settings, Processes, …) piled
  // onto each page's own admin streams and saturated the browser's per-origin
  // HTTP/1.1 connection cap, stalling later REST requests. Gate the chat
  // connection lifecycle to CHAT_ROUTES; non-chat pages that read the chat
  // store (e.g. SkillEventsPage's "Simulate") open their per-conversation
  // stream on demand via trackConversation, independent of connect().
  const onChatRoute = CHAT_ROUTES.has(routeName);
  if (previousProfile && previousProfile !== profileName) {
    // Group rooms and their per-group SSE are scoped to the previous profile's
    // token — drop them alongside the chat state, whatever route we land on.
    groupChatStore.resetForProfileSwitch();
    // Reset chat state when switching to a different profile.
    if (onChatRoute) {
      await chatStore.resetForProfileSwitch();
    } else if (chatStore.isConnected) {
      // Leaving a chat page's profile for a non-chat page of another
      // profile: drop the stale connection rather than reopening it.
      chatStore.disconnect();
    }
  } else if (onChatRoute && !chatStore.isConnected) {
    try {
      await chatStore.connect();
    } catch (e) {
      // Connection failure handled by chat store
    }
  }
}

watch(
  () => [route.name, route.params.profile],
  ([routeName, profile], [, oldProfile]) => {
    if (routeName && PROFILE_ROUTES.has(routeName as string) && profile) {
      handleProfileNavigation(profile as string, (oldProfile as string) || '', routeName as string);
    }
  },
);

onUnmounted(() => {
  embeddingStatusStore.disconnect();
  stopHttpsCoordinator?.();
  stopElectronMigrationGuard?.();
  stopElectronMigrationRelease?.();
  releaseElectronMigrationGate?.();
  stopMigrationResponder?.();
});

onMounted(async () => {
  applyTheme();
  stopMigrationResponder = installMigrationReadinessResponder();
  stopElectronMigrationRelease = window.cremind?.server?.onMigrationReleased?.(
    () => {
      releaseElectronMigrationGate?.();
      releaseElectronMigrationGate = null;
    },
  ) ?? null;
  stopElectronMigrationGuard = window.cremind?.server?.onBeforeMigration?.(
    async () => {
      if (!releaseElectronMigrationGate) {
        releaseElectronMigrationGate = beginMigrationGate();
      }
      try {
        await waitForMigrationReady(5 * 60_000);
        return settingsStore.profileId || undefined;
      } catch (error) {
        releaseElectronMigrationGate();
        releaseElectronMigrationGate = null;
        throw error;
      }
    },
  ) ?? null;
  // Subscribe once to the embedding-state SSE stream. Per-page views
  // read from this store reactively instead of opening their own
  // streams, and the underlying connection is shared across browser
  // tabs by `createSharedStream`.
  embeddingStatusStore.connect(settingsStore.agentUrl);

  // Handle OAuth callback redirect
  const params = new URLSearchParams(window.location.search);
  if (params.get('agents') === 'open') {
    params.delete('agents');
    const clean = params.toString();
    const newUrl = window.location.pathname + (clean ? '?' + clean : '');
    window.history.replaceState({}, '', newUrl);

    if (window.opener) {
      window.opener.postMessage({ type: 'a2a-auth-complete' }, window.location.origin);
      window.close();
      return;
    }
  }

  const routeName = route.name as string;
  const profile = route.params.profile as string;
  if (routeName && PROFILE_ROUTES.has(routeName) && profile) {
    await handleProfileNavigation(profile, '', routeName);
  }
});

const handleNewChat = () => {
  chatStore.clearConversation();
  if (currentProfile.value) {
    router.push({ name: 'chat', params: { profile: currentProfile.value } });
  }
};

const handleLogout = () => {
  const profile = currentProfile.value;
  if (profile) {
    settingsStore.removeTokenForProfile(profile);
  }
  // Disconnect chat if connected
  if (chatStore.isConnected) {
    chatStore.disconnect();
  }
  // Clear active session
  settingsStore.authToken = '';
  settingsStore.profileId = '';
  router.push('/');
};
</script>

<template>
  <!-- Titlebar for dragging (Electron only) -->
  <div v-if="isElectron" class="titlebar"></div>

  <!-- Backend + Electron-app update notifications. -->
  <UpdateBanner />

  <!-- Main App Layout -->
  <div class="app-layout" :class="{ 'has-titlebar': isElectron }">
    <!-- Persistent icon rail (all profile pages) -->
    <NavRail v-if="showRail" @logout="handleLogout" />

    <!-- Conversation history panel (chat pages, unless collapsed) -->
    <ConversationsPanel
      v-if="showConversations && !settingsStore.conversationsPanelCollapsed"
      @newChat="handleNewChat"
    />

    <!-- Main Content Area -->
    <main class="main-content">
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </main>

    <!-- Floating multi-window todo panels (plan mode + event runs). Always
         mounted; it self-scopes to the viewed conversation / open run drawer. -->
    <FloatingTodoLayer />

    <!-- Vector embedding busy gate -->
    <div v-if="embeddingBusy" class="embedding-overlay">
      <div class="embedding-overlay-card">
        <div class="spinner"></div>
        <h2>Updating Vector Embedding</h2>
        <p class="phase-line">{{ phaseLabel || 'Working on it…' }}</p>
        <p class="hint">
          The agent is briefly unavailable while embeddings are reloaded
          and rebuilt. Please wait — this can take up to a minute on
          first run.
        </p>
      </div>
    </div>

    <div
      v-if="(httpsPhase === 'waiting'
        || httpsPhase === 'moving'
        || httpsPhase === 'attention') && !httpsOverlayDismissed"
      v-show="!httpsOverlayExcluded"
      class="embedding-overlay https-transition-overlay"
    >
      <div class="embedding-overlay-card" :class="{ 'https-attention-card': httpsPhase === 'attention' }">
        <div v-if="httpsPhase !== 'attention'" class="spinner"></div>
        <h2>{{ httpsPhase === 'attention'
          ? 'HTTPS needs your attention'
          : 'Switching this tab to HTTPS' }}</h2>
        <p class="phase-line">
          <template v-if="httpsPhase === 'attention'">
            {{ httpsError || 'The secure address could not be verified.' }}
          </template>
          <template v-else>
            Waiting for the secure server, then this tab will reopen at the same page.
          </template>
        </p>
        <!-- After 45 seconds: what to fix on this side (or, when the secure
             server answered, what it said), a retry, and the explicit link —
             which carries this tab's handoff ticket and opens in this tab. -->
        <HttpsRecoveryHelp
          v-if="httpsPhase === 'attention' && httpsRecoveryUrl && httpsRecoverable"
          :https-url="httpsRecoveryUrl"
          :ca-url="httpsCaUrl"
          :show-trust="httpsTransition?.certificate_kind === 'local'"
          :port-forward="httpsPortForward"
          :kubernetes="httpsKubernetes"
          :reason="httpsReason"
          :message="httpsRecoveryMessage"
          @retry="retryHttpsTransition()"
        />
        <p v-else class="hint">
          If your browser shows a certificate warning, trust the Cremind CA on this
          device, or open the secure address and continue past the warning. On
          Kubernetes, rerun the port-forward command after the rollout.
        </p>
        <div class="transition-actions">
          <button class="transition-dismiss" @click="dismissHttpsOverlay()">
            Keep using this page
          </button>
        </div>
        <!-- The switch is not cancelled by dismissing it, so say so rather
             than letting the move arrive as a surprise mid-edit. -->
        <p class="hint">
          Your data is untouched on the server — this only changes the address the
          browser uses. This tab still moves to HTTPS on its own once the secure
          address answers, so finish anything you are in the middle of.
        </p>
      </div>
    </div>

    <div
      v-if="migrationInProgress && httpsPhase === 'idle' && !httpsOverlayDismissed
        && !httpsOverlayExcluded"
      class="embedding-overlay https-transition-overlay"
    >
      <div class="embedding-overlay-card">
        <div class="spinner"></div>
        <h2>Preparing tabs for HTTPS</h2>
        <p class="phase-line">
          Finishing active uploads and saving each tab's draft before the server restarts.
        </p>
        <div class="transition-actions">
          <button class="transition-dismiss" @click="dismissHttpsOverlay()">
            Keep using this page
          </button>
        </div>
      </div>
    </div>

    <!-- Nothing the user put away is ever gone: this brings it back. -->
    <button
      v-if="httpsOverlayDismissed && httpsOverlayPending"
      class="https-transition-chip"
      @click="restoreHttpsOverlay()"
    >
      {{ httpsPhase === 'attention'
        ? 'HTTPS switch needs attention'
        : 'HTTPS switch pending' }}
    </button>

    <!-- A switch waiting on a deployment change blocks nothing: this says it is
         still open, and takes the admin to the steps or the way out. -->
    <component
      :is="httpsPendingActionable ? 'button' : 'div'"
      v-if="httpsPhase === 'pending' && !httpsOverlayExcluded"
      class="https-transition-chip https-pending-chip"
      :title="httpsPendingActionable
        ? 'Open HTTPS settings to see the remaining steps, or cancel the switch'
        : 'An administrator has to apply the deployment change, or cancel it'"
      @click="httpsPendingActionable && openHttpsSettings()"
    >
      {{ httpsError ? 'HTTPS switch needs attention' : 'HTTPS switch waiting for the deployment change' }}
    </component>

    <!-- Saving this tab's handoff before the move. Brief and non-blocking:
         the page keeps working, and the blocking card follows only once the
         server is actually switching. -->
    <div
      v-if="httpsPhase === 'preparing' && !httpsOverlayExcluded"
      class="https-transition-chip https-pending-chip"
      role="status"
    >
      <span class="chip-spinner" aria-hidden="true"></span>
      Preparing this tab for HTTPS…
    </div>
  </div>

</template>

<style>
.titlebar {
  height: 32px;
  width: 100%;
  position: fixed;
  top: 0;
  left: 0;
  z-index: 1999;
  -webkit-app-region: drag;
  pointer-events: auto;
  background: var(--surface-color);
  border-bottom: 1px solid var(--border-color);
}

.app-layout {
  display: flex;
  width: 100%;
  height: 100vh;
  overflow: hidden;
}

.app-layout.has-titlebar {
  padding-top: 32px;
  box-sizing: border-box;
}

.main-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-color);
}

.embedding-overlay {
  position: fixed;
  inset: 0;
  z-index: 3000;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: all;
}

.embedding-overlay-card {
  max-width: 420px;
  background: var(--surface-color);
  color: var(--text-primary);
  border-radius: 12px;
  padding: 28px 32px;
  text-align: center;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
}

.embedding-overlay-card h2 {
  font-size: 1.15rem;
  font-weight: 600;
  margin: 12px 0 4px 0;
}

.embedding-overlay-card .phase-line {
  color: var(--text-secondary);
  font-size: 0.9rem;
  margin: 0 0 12px 0;
}

.embedding-overlay-card .hint {
  font-size: 0.78rem;
  color: var(--text-secondary);
  line-height: 1.55;
  margin: 0;
}
.transition-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 16px;
}

.transition-dismiss {
  border: 1px solid var(--border-color);
  background: transparent;
  color: var(--text-primary);
  border-radius: 6px;
  padding: 8px 16px;
  font-weight: 600;
  cursor: pointer;
}
.transition-dismiss:hover { border-color: var(--primary-color); color: var(--primary-color); }

/* Deliberately unobtrusive but always present while a switch is pending, so
   the explanation is one click away rather than dismissed for good. */
.https-transition-chip {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 2000;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--border-color);
  border-radius: 999px;
  padding: 7px 14px;
  background: var(--surface-color);
  color: var(--text-secondary);
  font-size: 0.8rem;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 2px 10px rgb(0 0 0 / 18%);
}
.https-transition-chip:hover { color: var(--primary-color); border-color: var(--primary-color); }
/* Not an alert: nothing is broken and nothing is blocked while it shows. */
.https-pending-chip { font-weight: 500; cursor: default; }
button.https-pending-chip { cursor: pointer; }
.chip-spinner {
  width: 10px;
  height: 10px;
  border: 2px solid var(--border-color);
  border-top-color: var(--primary-color);
  border-radius: 50%;
  animation: spin 0.9s linear infinite;
}
/* Room for the recovery steps without turning the card into a page. */
.embedding-overlay-card.https-attention-card {
  max-width: 560px;
  max-height: calc(100vh - 48px);
  overflow-y: auto;
}

.spinner {
  width: 32px;
  height: 32px;
  margin: 0 auto;
  border: 3px solid rgba(0, 0, 0, 0.1);
  border-top-color: var(--primary-color, #4f8cff);
  border-radius: 50%;
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
