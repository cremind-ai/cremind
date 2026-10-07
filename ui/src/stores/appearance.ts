/**
 * The signed-in profile's appearance: color theme, font and text size — and,
 * riding along because it is read and saved the same way, whether a reply's
 * Thinking Process opens by itself (set from the row's Auto-open switch).
 *
 * The settings are the ``appearance.*`` (and ``chat.thinking_process``) keys of
 * the profile's config on the server, so they follow the profile to every
 * device (and `cremind config set appearance.theme dark` works). This store
 * paints them at once — from a
 * local copy before the server answers, so a dark theme never flashes white —
 * saves edits after a short pause (a color being dragged is one save, not
 * fifty), and re-reads the server on every settings-state ping, which a change
 * from the CLI, another tab or another device sends.
 */

import { defineStore } from 'pinia';
import { computed, ref, watch } from 'vue';
import { useSettingsStore } from './settings';
import { getUserConfig, resetUserConfigKey, updateUserConfig } from '../services/configApi';
import { subscribeSettingsState, type ProfileEventsSubHandle } from '../services/profileEventsStream';
import {
  APPEARANCE_KEYS,
  DEFAULT_APPEARANCE,
  appearanceFromConfig,
  appearanceToConfig,
  resolvePalette,
  type AppearanceSettings,
} from '../appearance/presets';
import { computeAppearance } from '../appearance/tokens';
import {
  LAST_KEY,
  applyAppearance,
  cacheAppearance,
  profileKey,
  readCachedAppearance,
} from '../appearance/apply';

/** How long an edit waits for the next one before it is saved. */
const SAVE_DELAY_MS = 350;
/** Pings arrive in bursts (a save wakes every open tab): one read per burst. */
const REFETCH_DELAY_MS = 150;

// Before this store, the only setting was a light/dark switch kept per browser
// under `theme`. It is snapshotted once, before this store first writes that
// key itself, and each profile that has never chosen a theme inherits it once.
const LEGACY_SNAPSHOT_KEY = 'appearance_legacy_theme';
const migratedKey = (profile: string) => `appearance_migrated_${profile}`;

function storageGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* blocked storage: a migration may run twice, which is harmless */
  }
}

function legacyTheme(): string {
  let snapshot = storageGet(LEGACY_SNAPSHOT_KEY);
  if (snapshot === null) {
    snapshot = storageGet('theme') === 'dark' ? 'dark' : 'light';
    storageSet(LEGACY_SNAPSHOT_KEY, snapshot);
  }
  return snapshot;
}

/** The profile in this tab's URL (`#/<profile>/…`, `#/login/<profile>`). */
function profileFromLocation(): string {
  try {
    const m = /^#\/(?:login\/)?([^/?#]+)/.exec(window.location.hash);
    return m ? decodeURIComponent(m[1]) : '';
  } catch {
    return '';
  }
}

function systemPrefersDark(): boolean {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  } catch {
    return false;
  }
}

function initialSettings(): AppearanceSettings {
  const cached = readCachedAppearance(profileKey(profileFromLocation()))
    ?? readCachedAppearance(LAST_KEY);
  if (cached) return cached.settings;
  // A first load after upgrading: what the old switch showed, until the
  // server's answer (and the migration below) settles it.
  return legacyTheme() === 'dark' ? { ...DEFAULT_APPEARANCE, theme: 'dark' } : { ...DEFAULT_APPEARANCE };
}

export const useAppearanceStore = defineStore('appearance', () => {
  const settingsStore = useSettingsStore();
  legacyTheme();

  const settings = ref<AppearanceSettings>(initialSettings());
  const systemDark = ref(systemPrefersDark());
  /** The profile whose server values `settings` holds ('' before the first read). */
  const loadedProfile = ref('');
  /** Keys this profile has set (the rest are the defaults). */
  const overridden = ref<Set<string>>(new Set());
  /** Bumped on every repaint, for code that reads the CSS variables back. */
  const revision = ref(0);
  const saving = ref(false);

  const applied = computed(() => computeAppearance(settings.value, systemDark.value));
  const mode = computed(() => applied.value.mode);
  const palette = computed(() => resolvePalette(settings.value, systemDark.value));

  watch(applied, (a) => {
    applyAppearance(a);
    cacheAppearance(loadedProfile.value, settings.value, a);
    revision.value++;
  }, { immediate: true });

  // ── saving ──

  let pending: Partial<AppearanceSettings> = {};
  /** The sign-in the pending edits were made under: a batch is saved for the
   *  profile it was made in, even if the tab switched profile before it went. */
  let pendingToken = '';
  let pendingAgentUrl = '';
  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  let saveWaiters: { resolve: () => void; reject: (e: unknown) => void }[] = [];
  /** Counts local edits, so a server read that started before one is dropped. */
  let editSeq = 0;
  let inFlight: Promise<void> | null = null;

  async function flush(): Promise<void> {
    saveTimer = null;
    const patch = pending;
    pending = {};
    const waiters = saveWaiters;
    saveWaiters = [];
    const agentUrl = pendingAgentUrl;
    const token = pendingToken;
    if (!token || !Object.keys(patch).length) {
      waiters.forEach(w => w.resolve());
      return;
    }
    saving.value = true;
    const run = (async () => {
      try {
        await updateUserConfig(agentUrl, token, appearanceToConfig(patch));
        for (const field of Object.keys(patch)) {
          overridden.value.add(APPEARANCE_KEYS[field as keyof AppearanceSettings]);
        }
        waiters.forEach(w => w.resolve());
      } catch (e) {
        waiters.forEach(w => w.reject(e));
        // What the server holds is what this profile has: show that again.
        scheduleRefetch(0);
      } finally {
        saving.value = false;
        inFlight = null;
      }
    })();
    inFlight = run;
    await run;
  }

  /**
   * Change some settings: painted now, saved after a short pause. Resolves
   * once saved (rejects with the server's complaint). Signed out, the change
   * only paints.
   */
  function update(patch: Partial<AppearanceSettings>): Promise<void> {
    settings.value = { ...settings.value, ...patch };
    editSeq++;
    if (Object.keys(pending).length && settingsStore.authToken !== pendingToken) {
      // Another sign-in since the last edit: send that profile's batch now.
      if (saveTimer) clearTimeout(saveTimer);
      void flush();
    }
    pendingToken = settingsStore.authToken;
    pendingAgentUrl = settingsStore.agentUrl;
    pending = { ...pending, ...patch };
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { void flush(); }, SAVE_DELAY_MS);
    return new Promise((resolve, reject) => saveWaiters.push({ resolve, reject }));
  }

  /** Pick a theme. The first switch to "custom" starts from the theme on
   *  screen rather than from the default custom colors. */
  function chooseTheme(id: string): Promise<void> {
    if (id === 'custom' && !hasCustomColors.value) {
      const p = palette.value;
      return update({
        theme: 'custom',
        customAccent: p.accent,
        customBackground: p.background,
        customSurface: p.surface,
        customText: p.text,
      });
    }
    return update({ theme: id });
  }

  const hasCustomColors = computed(() =>
    ['customAccent', 'customBackground', 'customSurface', 'customText'].some(
      field => overridden.value.has(APPEARANCE_KEYS[field as keyof AppearanceSettings]),
    ));

  /** The appearance back to its defaults (its overrides are removed). The
   *  Thinking Process mode is not appearance, so it stays as it is — a change
   *  to it still waiting to be saved included. */
  async function resetAll(): Promise<void> {
    const agentUrl = settingsStore.agentUrl;
    const token = settingsStore.authToken;
    const { thinkingProcess } = pending;
    if (thinkingProcess === undefined) {
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = null;
      pending = {};
      saveWaiters.forEach(w => w.resolve());
      saveWaiters = [];
    } else {
      pending = { thinkingProcess };
    }
    settings.value = { ...DEFAULT_APPEARANCE, thinkingProcess: settings.value.thinkingProcess };
    editSeq++;
    if (!token) return;
    const keys = Object.values(APPEARANCE_KEYS).filter(key => key.startsWith('appearance.'));
    await Promise.all(keys.map(key => resetUserConfigKey(agentUrl, token, key)));
    for (const key of keys) overridden.value.delete(key);
  }

  // ── reading the server ──

  async function refetch(): Promise<void> {
    const agentUrl = settingsStore.agentUrl;
    const token = settingsStore.authToken;
    const profile = settingsStore.profileId;
    if (!token || !agentUrl) return;
    const seq = editSeq;
    let config;
    try {
      config = await getUserConfig(agentUrl, token);
    } catch {
      return; // offline or signed out: keep what is painted
    }
    // Answers for a profile or an edit that is no longer current are stale.
    if (settingsStore.authToken !== token || settingsStore.agentUrl !== agentUrl) return;
    if (seq !== editSeq || saveTimer || inFlight) return;
    const owner = config.profile || profile;
    const values = config.values ?? {};
    overridden.value = new Set(
      Object.values(APPEARANCE_KEYS).filter(key => values[key] !== null && values[key] !== undefined),
    );
    loadedProfile.value = owner;
    settings.value = appearanceFromConfig(values, config.defaults ?? {});
    if (owner && storageGet(migratedKey(owner)) === null) {
      storageSet(migratedKey(owner), '1');
      if (legacyTheme() === 'dark' && !overridden.value.has(APPEARANCE_KEYS.theme)) {
        void update({ theme: 'dark' }).catch(() => {});
      }
    }
  }

  let refetchTimer: ReturnType<typeof setTimeout> | null = null;
  function scheduleRefetch(delay = REFETCH_DELAY_MS): void {
    if (refetchTimer) clearTimeout(refetchTimer);
    refetchTimer = setTimeout(() => {
      refetchTimer = null;
      void refetch();
    }, delay);
  }

  // ── lifecycle ──

  let started = false;
  let subscription: ProfileEventsSubHandle | null = null;

  /** Follow the device's light/dark setting and the signed-in profile. Call once. */
  function start(): void {
    if (started) return;
    started = true;
    try {
      const media = window.matchMedia('(prefers-color-scheme: dark)');
      media.addEventListener('change', (e) => { systemDark.value = e.matches; });
    } catch {
      /* no matchMedia: "system" stays light */
    }
    watch(
      () => [settingsStore.agentUrl, settingsStore.authToken, settingsStore.profileId] as const,
      ([agentUrl, token, profile], previous) => {
        if (previous && profile !== previous[2]) {
          // Another profile in this tab: its own look at once, if this
          // browser has shown it before; the server's answer follows.
          const cached = profile ? readCachedAppearance(profileKey(profile)) : null;
          if (cached) settings.value = cached.settings;
          loadedProfile.value = '';
          overridden.value = new Set();
        }
        subscription?.close();
        subscription = null;
        if (!agentUrl || !token) return;
        // The first ping arrives as the stream connects: that is the first read.
        subscription = subscribeSettingsState(agentUrl, token, () => scheduleRefetch());
        scheduleRefetch();
      },
      { immediate: true },
    );
  }

  return {
    settings,
    systemDark,
    applied,
    mode,
    palette,
    revision,
    saving,
    loadedProfile,
    hasCustomColors,
    start,
    update,
    chooseTheme,
    resetAll,
    refetch,
  };
});
