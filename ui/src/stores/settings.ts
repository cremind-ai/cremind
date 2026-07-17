import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import { fetchMe } from '../services/agentApi';
import { getAgentUrl, setAgentUrl as persistAgentUrl } from '../services/runtimeConfig';
import { type ChatMode, isChatMode, DEFAULT_CHAT_MODE } from '../constants/chatModes';

export type ProfileValue = string | boolean | number | Record<string, unknown>;

// ── Per-profile token helpers ──

function _getLoggedInProfiles(): string[] {
  try {
    const raw = localStorage.getItem('logged_in_profiles');
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function _saveLoggedInProfiles(profiles: string[]) {
  localStorage.setItem('logged_in_profiles', JSON.stringify(profiles));
}

function _migrateOldToken() {
  // Migrate from old single-token format to per-profile format
  const oldToken = localStorage.getItem('agent_auth_token');
  const oldProfileId = localStorage.getItem('profile_id');
  if (oldToken && oldProfileId) {
    const key = `agent_token_${oldProfileId}`;
    if (!localStorage.getItem(key)) {
      localStorage.setItem(key, oldToken);
      const profiles = _getLoggedInProfiles();
      if (!profiles.includes(oldProfileId)) {
        profiles.push(oldProfileId);
        _saveLoggedInProfiles(profiles);
      }
    }
    localStorage.removeItem('agent_auth_token');
  }
  // Earlier builds stored the agent URL in localStorage. The runtime config
  // (window.cremind.config) is now authoritative, so drop any stale value
  // here on first load to avoid confusion.
  localStorage.removeItem('agent_url');
}

// Run migration on module load
_migrateOldToken();

export const useSettingsStore = defineStore('settings', () => {
  // Auto-connect to agent on app load
  const autoConnect = ref(localStorage.getItem('auto_connect') === 'true');

  // Agent URL — runtime config (Electron) or build-time fallback (web).
  // ``setAgentUrl`` writes through to the persisted Electron config when
  // the bridge is available, so the value survives renderer restarts.
  const agentUrl = ref(getAgentUrl());

  async function setAgentUrlAction(url: string) {
    agentUrl.value = url;
    await persistAgentUrl(url);
  }

  // UI theme
  const theme = ref<'light' | 'dark'>(
    (localStorage.getItem('theme') as 'light' | 'dark') || 'light'
  );

  // Sidebar collapsed state
  const sidebarCollapsed = ref(localStorage.getItem('sidebar_collapsed') === 'true');

  // Usage chip: open its explanation popover on hover (default) vs. on click.
  // Default true unless explicitly turned off.
  const usageChipHover = ref(localStorage.getItem('usage_chip_hover') !== 'false');

  // Events page view: the classic tables ('events') vs. the Tasks Kanban board
  // ('tasks'). Presentation-only, so global (not per-profile) like the sidebar
  // state. Validated on read so a stray key can never blank the page.
  const eventsViewMode = ref<'events' | 'tasks'>(
    localStorage.getItem('events_view_mode') === 'tasks' ? 'tasks' : 'events',
  );

  // Active profile ID (the currently-used profile for this tab session)
  const profileId = ref(localStorage.getItem('profile_id') || '');

  // Active authentication token (for the current profile session)
  const authToken = ref('');

  // Working directory from backend (for resolving absolute file paths to API URLs)
  const workingDir = ref('');

  // Chat mode (per-profile, default: reasoning). Replaces the old reasoning
  // toggle; the mode is remembered across sessions and sticky until changed.
  const chatMode = ref<ChatMode>(DEFAULT_CHAT_MODE);

  // Detect if running in Electron
  const isElectron = computed(() => {
    return typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__;
  });

  // ── Desktop-app update preferences ──
  //
  // The release channel is fixed at install time (see app/upgrade/channel.py)
  // and is intentionally not exposed for runtime mutation — switching
  // channels is a deliberate reinstall. We only expose the ``autoUpdate``
  // toggle here. Source of truth is the Electron config (cremind-config.json),
  // read via the preload bridge.
  const bridgeConfig = (typeof window !== 'undefined' ? window.cremind?.config : undefined) ?? null;
  const autoUpdate = ref<boolean>(bridgeConfig?.autoUpdate ?? true);

  async function setAutoUpdate(value: boolean) {
    autoUpdate.value = value;
    if (typeof window !== 'undefined' && window.cremind) {
      await window.cremind.setConfig({ autoUpdate: value });
    }
  }

  // ── Per-profile token management ──

  function getTokenForProfile(profileName: string): string {
    return localStorage.getItem(`agent_token_${profileName}`) || '';
  }

  function setTokenForProfile(profileName: string, token: string) {
    localStorage.setItem(`agent_token_${profileName}`, token);
    const profiles = _getLoggedInProfiles();
    if (!profiles.includes(profileName)) {
      profiles.push(profileName);
      _saveLoggedInProfiles(profiles);
    }
  }

  function getChatMode(profileName: string): ChatMode {
    const val = localStorage.getItem(`chat_mode_${profileName}`);
    if (isChatMode(val)) return val;
    // Migrate from the legacy per-profile reasoning toggle: off → instant.
    const legacy = localStorage.getItem(`reasoning_enabled_${profileName}`);
    return legacy === 'false' ? 'instant' : DEFAULT_CHAT_MODE;
  }

  function setChatMode(profileName: string, mode: ChatMode) {
    chatMode.value = mode;
    localStorage.setItem(`chat_mode_${profileName}`, mode);
    // Keep the legacy key coherent (mode !== instant ⇒ reasoning on) so a
    // rollback to an older build still reflects the user's thinking preference.
    localStorage.setItem(`reasoning_enabled_${profileName}`, String(mode !== 'instant'));
  }

  function removeTokenForProfile(profileName: string) {
    localStorage.removeItem(`agent_token_${profileName}`);
    const profiles = _getLoggedInProfiles().filter(p => p !== profileName);
    _saveLoggedInProfiles(profiles);
  }

  function getLoggedInProfiles(): string[] {
    return _getLoggedInProfiles();
  }

  /**
   * Activate a profile for the current session.
   * Loads the stored token for this profile and sets it as active.
   * Returns true if the profile has a stored token.
   */
  function activateProfile(profileName: string): boolean {
    const token = getTokenForProfile(profileName);
    if (!token) return false;

    authToken.value = token;
    profileId.value = profileName;
    chatMode.value = getChatMode(profileName);

    fetchMe(agentUrl.value, token)
      .then((me) => {
        if (me.working_dir) {
          workingDir.value = me.working_dir;
        }
      })
      .catch(() => {});
    return true;
  }

  // ── Legacy methods ──

  function setAutoConnect(value: boolean) {
    autoConnect.value = value;
    localStorage.setItem('auto_connect', String(value));
  }

  function setTheme(newTheme: 'light' | 'dark') {
    theme.value = newTheme;
    localStorage.setItem('theme', newTheme);
  }

  function setSidebarCollapsed(value: boolean) {
    sidebarCollapsed.value = value;
    localStorage.setItem('sidebar_collapsed', String(value));
  }

  function setUsageChipHover(value: boolean) {
    usageChipHover.value = value;
    localStorage.setItem('usage_chip_hover', String(value));
  }

  function setEventsViewMode(mode: 'events' | 'tasks') {
    eventsViewMode.value = mode;
    localStorage.setItem('events_view_mode', mode);
  }

  function setProfileId(id: string) {
    profileId.value = id;
  }

  function setAuthToken(token: string) {
    authToken.value = token;
    if (token) {
      // Fetch profile from backend and update profileId + per-profile storage
      fetchMe(agentUrl.value, token)
        .then((me) => {
          if (me.profile) {
            setProfileId(me.profile);
            setTokenForProfile(me.profile, token);
          }
          if (me.working_dir) {
            workingDir.value = me.working_dir;
          }
        })
        .catch(() => {
          // Token may be invalid or server unreachable — ignore silently
        });
    } else {
      setProfileId('');
    }
  }

  return {
    autoConnect,
    agentUrl,
    setAgentUrl: setAgentUrlAction,
    theme,
    sidebarCollapsed,
    usageChipHover,
    eventsViewMode,
    profileId,
    authToken,
    workingDir,
    isElectron,
    setAutoConnect,
    setTheme,
    setSidebarCollapsed,
    setUsageChipHover,
    setEventsViewMode,
    setProfileId,
    setAuthToken,
    // Per-profile token management
    getTokenForProfile,
    setTokenForProfile,
    removeTokenForProfile,
    getLoggedInProfiles,
    activateProfile,
    // Chat mode (plan / reasoning / instant)
    chatMode,
    getChatMode,
    setChatMode,
    // Desktop-app update preferences
    autoUpdate,
    setAutoUpdate,
  };
});
