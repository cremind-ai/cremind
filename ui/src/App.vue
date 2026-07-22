<script setup lang="ts">
import { computed, watch, onMounted, onUnmounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useChatStore } from './stores/chat';
import { useSettingsStore } from './stores/settings';
import { useEmbeddingStatusStore } from './stores/embeddingStatus';
import { checkSetupStatus } from './services/configApi';
import { PROFILE_ROUTES, CHAT_ROUTES } from './router/profileRoutes';
import NavRail from './components/NavRail.vue';
import ConversationsPanel from './components/ConversationsPanel.vue';
import UpdateBanner from './components/UpdateBanner.vue';
import FloatingTodoLayer from './components/plan/FloatingTodoLayer.vue';

const route = useRoute();
const router = useRouter();
const chatStore = useChatStore();
const settingsStore = useSettingsStore();
const embeddingStatusStore = useEmbeddingStatusStore();

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
});

onMounted(async () => {
  applyTheme();
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
