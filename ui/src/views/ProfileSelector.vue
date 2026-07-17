<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { ElButton, ElInput, ElSelect, ElOption, ElDivider } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { checkSetupStatus, listPublicProfileNames, resetOrphanedSetup } from '../services/configApi';
import { fetchMe } from '../services/agentApi';
import { safeRedirectTarget } from '../utils/loginRedirect';

// During a backend restart (typical immediately post-upgrade) the
// /api/config/setup-status endpoint can briefly return
// has_profiles=false before storage fully reloads. Acting on that
// reading wipes every stored token. Retry until we get a stable
// answer.
const SETUP_STATUS_MAX_WAIT_MS = 10000;

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

const router = useRouter();
const route = useRoute();
const settingsStore = useSettingsStore();

// Tray / jumplist / dock entries can deep-link to a profile-scoped
// route via ``?cremind_window=<target>``. When no profile is logged in
// the router falls through to this selector; once the user picks one
// we honor the hint instead of dumping them on chat.
function destinationFor(profile: string): string {
  // A global 401 (expired token) redirects here as ``/?redirect=<prev page>``.
  // Honor it — validated to the same profile — before the window hint below.
  const redirect = safeRedirectTarget(route.query.redirect, profile);
  if (redirect) return redirect;
  const hint = route.query.cremind_window;
  if (hint === 'settings') return `/${profile}/settings`;
  if (hint === 'processes') return `/${profile}/processes`;
  if (hint === 'events') return `/${profile}/events`;
  if (hint === 'channels') return `/${profile}/channels`;
  return `/${profile}`;
}

const loggedInProfiles = ref<string[]>([]);
const checking = ref(true);

// Login form state
const loginProfileName = ref('');
const loginToken = ref('');
const loginLoading = ref(false);
const loginError = ref('');

// Names served by the public /api/profiles/names endpoint, feeding the
// login dropdown. Best-effort: on any failure the dropdown stays empty
// and `allow-create` lets the user type the name by hand, which is the
// pre-dropdown behavior.
const availableProfiles = ref<string[]>([]);

// Returns true only when the server gave a definitive answer, so callers can
// safely reconcile the saved-profile cache against it (a failed fetch must
// never be treated as "no profiles exist").
async function loadAvailableProfiles(): Promise<boolean> {
  try {
    const { profiles } = await listPublicProfileNames(settingsStore.agentUrl);
    availableProfiles.value = profiles;
    return true;
  } catch {
    availableProfiles.value = [];
    return false;
  }
}

onMounted(async () => {
  // Kick off the authoritative profile-name fetch in parallel with the
  // setup-status probe. It feeds the login dropdown and, below, prunes
  // deleted profiles from the saved-profile cards.
  const profileNamesReady = loadAvailableProfiles();

  const status = await readStableSetupStatus();

  if (!status) {
    // Backend never gave a stable reading within the budget — fall back
    // to whatever we have locally rather than destroying state.
    loggedInProfiles.value = settingsStore.getLoggedInProfiles();
    checking.value = false;
    return;
  }

  if (!status.setup_complete) {
    router.replace('/setup');
    return;
  }

  // Detect orphaned setup: setup_complete=true but no profiles in DB.
  // Only acted on after readStableSetupStatus confirmed two consecutive
  // readings — a single transient post-restart reading no longer wipes
  // tokens.
  if (status.has_profiles === false) {
    try {
      await resetOrphanedSetup(settingsStore.agentUrl);
    } catch {
      // If reset fails, still redirect — SetupWizard will re-check
    }
    for (const p of settingsStore.getLoggedInProfiles()) {
      settingsStore.removeTokenForProfile(p);
    }
    router.replace('/setup');
    return;
  }

  // Authenticated per-profile probe. Only prune on a definitive 401/403
  // — never on network errors, 5xx, or timeouts, which can happen
  // briefly during a backend restart.
  const base = resolveBaseUrl(settingsStore.agentUrl);
  const localProfiles = settingsStore.getLoggedInProfiles();
  await Promise.all(localProfiles.map(async (lp) => {
    const token = settingsStore.getTokenForProfile(lp);
    if (!token) return;
    try {
      const res = await fetch(`${base}/api/me`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 401 || res.status === 403) {
        settingsStore.removeTokenForProfile(lp);
      }
    } catch {
      // Network failure — keep the token.
    }
  }));

  // Reconcile the saved-profile cache against the authoritative live list:
  // a profile deleted on the server (via Settings, the CLI, or another
  // browser) leaves a still-valid JWT behind, so the /api/me probe above
  // can't detect it — it must be pruned by membership in the live list.
  // Only prune when we got a definitive list; a failed fetch keeps the cards.
  const listOk = await profileNamesReady;
  if (listOk) {
    const live = new Set(availableProfiles.value);
    for (const p of settingsStore.getLoggedInProfiles()) {
      if (!live.has(p)) settingsStore.removeTokenForProfile(p);
    }
  }

  loggedInProfiles.value = settingsStore.getLoggedInProfiles();
  checking.value = false;
});

async function readStableSetupStatus() {
  const start = Date.now();
  let delay = 200;
  while (Date.now() - start < SETUP_STATUS_MAX_WAIT_MS) {
    let candidate: Awaited<ReturnType<typeof checkSetupStatus>> | null = null;
    try {
      candidate = await checkSetupStatus(settingsStore.agentUrl);
    } catch {
      // transient — retry
    }
    if (candidate) {
      const looksOrphaned = candidate.setup_complete && candidate.has_profiles === false;
      if (!looksOrphaned) return candidate;
      // Plausibly partial backend state — confirm with one more read
      // before believing it.
      let confirm: typeof candidate | null = null;
      try {
        await new Promise((r) => setTimeout(r, 250));
        confirm = await checkSetupStatus(settingsStore.agentUrl);
      } catch {
        // transient — retry the outer loop
      }
      if (confirm) {
        return confirm;
      }
    }
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(Math.floor(delay * 1.5), 1500);
  }
  return null;
}

function selectProfile(profile: string) {
  router.push(destinationFor(profile));
}

async function handleLogin() {
  const profile = loginProfileName.value.trim();
  const token = loginToken.value.trim();
  if (!profile || !token) return;

  loginLoading.value = true;
  loginError.value = '';
  try {
    // Verify profile exists
    const status = await checkSetupStatus(settingsStore.agentUrl, profile);
    if (status.profile_exists === false) {
      loginError.value = `Profile '${profile}' does not exist`;
      return;
    }

    // Verify token
    const me = await fetchMe(settingsStore.agentUrl, token);
    if (me.profile !== profile) {
      loginError.value = `This token belongs to profile '${me.profile}', not '${profile}'`;
      return;
    }

    // Save and navigate
    settingsStore.setTokenForProfile(profile, token);
    settingsStore.activateProfile(profile);
    router.push(destinationFor(profile));
  } catch {
    loginError.value = 'Invalid or expired token';
  } finally {
    loginLoading.value = false;
  }
}

</script>

<template>
  <div class="profile-selector" v-if="!checking">
    <div class="selector-container">
      <div class="selector-header">
        <h1 class="selector-title">Cremind</h1>
        <p class="selector-subtitle">Select a profile or log in</p>
      </div>

      <!-- Saved profiles -->
      <div v-if="loggedInProfiles.length > 0" class="profiles-section">
        <div class="profiles-grid">
          <div
            v-for="profile in loggedInProfiles"
            :key="profile"
            class="profile-card"
            @click="selectProfile(profile)"
          >
            <div class="profile-avatar">
              <Icon icon="mdi:account-circle" />
            </div>
            <span class="profile-name">{{ profile }}</span>
            <Icon icon="mdi:chevron-right" class="profile-chevron" />
          </div>
        </div>
        <ElDivider content-position="left">or log in to another profile</ElDivider>
      </div>

      <!-- Login form -->
      <div class="login-section">
        <div class="login-field">
          <label class="field-label">Profile name</label>
          <ElSelect
            v-model="loginProfileName"
            filterable
            allow-create
            default-first-option
            :placeholder="availableProfiles.length > 0 ? 'Select a profile' : 'e.g. admin, lee'"
            class="profile-select"
            @keyup.enter="handleLogin"
          >
            <ElOption
              v-for="profile in availableProfiles"
              :key="profile"
              :label="profile"
              :value="profile"
            />
          </ElSelect>
        </div>

        <div class="login-field">
          <label class="field-label">Token</label>
          <ElInput
            v-model="loginToken"
            type="textarea"
            :rows="2"
            placeholder="Paste your JWT token"
            class="token-input"
          />
        </div>

        <p v-if="loginError" class="login-error">{{ loginError }}</p>

        <div class="login-actions">
          <ElButton
            type="primary"
            :loading="loginLoading"
            @click="handleLogin"
            :disabled="!loginProfileName.trim() || !loginToken.trim()"
          >
            <Icon icon="mdi:login" /> Login
          </ElButton>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.profile-selector {
  width: 100%;
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-color);
  padding: 24px;
  box-sizing: border-box;
}

.selector-container {
  width: 100%;
  max-width: 420px;
  background: var(--surface-color);
  border-radius: 12px;
  border: 1px solid var(--border-color);
  padding: 32px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
}

.selector-header {
  text-align: center;
  margin-bottom: 24px;
}

.selector-title {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0 0 4px 0;
}

.selector-subtitle {
  font-size: 0.9rem;
  color: var(--text-secondary);
  margin: 0;
}

.profiles-section {
  margin-bottom: 0;
}

.profiles-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.profile-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease;
  background: var(--bg-color);
}

.profile-card:hover {
  border-color: var(--primary-color);
  background: var(--hover-bg);
}

.profile-avatar {
  font-size: 28px;
  color: var(--primary-color);
  display: flex;
  align-items: center;
}

.profile-name {
  flex: 1;
  font-size: 1rem;
  font-weight: 600;
  color: var(--text-primary);
}

.profile-chevron {
  font-size: 18px;
  color: var(--text-tertiary);
}

.login-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.login-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field-label {
  font-size: 0.8rem;
  font-weight: 500;
  color: var(--text-secondary);
}

.profile-select {
  width: 100%;
}

.token-input :deep(textarea) {
  font-family: monospace;
  font-size: 0.8rem;
}

.login-error {
  color: var(--el-color-danger);
  font-size: 0.8rem;
  margin: 0;
}

.login-actions {
  display: flex;
  gap: 8px;
}
</style>
