<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElSteps, ElStep, ElButton, ElMessage,
  ElForm, ElFormItem, ElInput, ElRadio, ElRadioGroup, ElAlert, ElTag,
  ElSelect, ElOption,
} from 'element-plus';
import { useSettingsStore } from '../stores/settings';
import { useConfigStore } from '../stores/config';
import { useChatStore } from '../stores/chat';
import StepServerConfig from '../components/setup/StepServerConfig.vue';
import StepEmbeddingConfig, { type EmbeddingConfigPayload } from '../components/setup/StepEmbeddingConfig.vue';
import StepLLMConfig from '../components/setup/StepLLMConfig.vue';
import StepToolConfig from '../components/setup/StepToolConfig.vue';
import StepMemoryConfig from '../components/setup/StepMemoryConfig.vue';
import StepChannelsConfig from '../components/setup/StepChannelsConfig.vue';
import StepProfileCreate from '../components/setup/StepProfileCreate.vue';
import RestoreFromBackupCard from '../components/setup/RestoreFromBackupCard.vue';
import ChannelPairingDialog from '../components/channels/ChannelPairingDialog.vue';
import { storeToRefs } from 'pinia';
import {
  completeSetup,
  checkSetupStatus,
  fetchServiceCapabilities,
  fetchInstallSecrets,
  type ServiceCapabilitiesResponse,
  type InstallSecrets,
} from '../services/configApi';
import {
  buildExport,
  exportMimeType,
  type ConfigExportSnapshot,
  type ExportFormat,
} from '../utils/configExport';
import {
  openSetupProgressStream,
  type SetupLogEntry,
  type SetupProgressStreamHandle,
} from '../services/setupProgressStream';
import {
  fetchSetupProfiles,
  type SetupProfile,
} from '../services/setupProfilesApi';
import {
  fetchInstallCatalog,
  getBundledInstallCatalog,
  recommendInstallMode,
  type InstallCatalog,
  type DeploymentAdvancedField,
} from '../services/installCatalogApi';
import {
  fetchChannelCatalogPublic,
  type ChannelCatalogEntry,
  type ChannelRow,
  type CreateChannelPayload,
} from '../services/channelApi';
import { useEmbeddingStatusStore } from '../stores/embeddingStatus';

const props = defineProps<{
  profile?: string;
}>();

const router = useRouter();
const settingsStore = useSettingsStore();
const configStore = useConfigStore();
const chatStore = useChatStore();

const currentStep = ref(0);
const submitting = ref(false);
// Toggles the welcome pane between "configure a fresh install" and the
// restore-from-backup card. Setup-mode restore is a self-contained flow that
// boots the restored system and routes to the profile selector on success.
const showRestoreCard = ref(false);
// True when the backend's /api/config/setup response flagged that one or
// more newly-installed features (e.g. embedding.me5 / sentence-transformers
// + torch) cannot be activated in-process and require restarting the
// server before they take effect.
const restartRequired = ref(false);

// Live progress feed for the Complete Setup phase. Each entry is one
// frame from the /api/config/setup/stream SSE channel; the panel below
// the action bar renders them as a scrolling log while ``submitting``.
const setupLog = ref<SetupLogEntry[]>([]);
const setupLogEl = ref<HTMLPreElement | null>(null);
let setupProgressHandle: SetupProgressStreamHandle | null = null;

watch(
  () => setupLog.value.length,
  async () => {
    await nextTick();
    const el = setupLogEl.value;
    if (el) el.scrollTop = el.scrollHeight;
  },
);

// ── First-run installer phase (Electron only) ────────────────────────────
//
// When the renderer is hosted inside the Electron app and the persisted
// runtime config has no ``agentUrl``, this view *also* hosts the four
// installer steps (Welcome / Deployment / Mode / Install) at the start
// of the wizard, so the user moves through a single continuous flow:
//
//   Welcome → Deployment → Mode → Install → Server → … → Complete
//
// The web build (``__IS_ELECTRON__ === false``) and Electron launches
// where the install has already happened skip the prefix entirely. In
// both of those cases ``includeInstallerSteps`` is ``false`` and this
// file behaves identically to the curl-installed Setup Wizard.

const isElectron = computed(
  () => typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__,
);
const installerBridge = computed(() => window.cremind?.installer);

type InstallEnv = {
  os: 'linux' | 'macos' | 'windows' | 'unknown';
  arch: string;
  hasDocker: boolean;
  hasPython: boolean;
  pythonVersion: string;
  channel: 'production' | 'test' | 'dev';
};

const installEnv = ref<InstallEnv | null>(null);
const detectingInstallEnv = ref(false);
const installDeployment = ref<'local' | 'server' | 'custom'>('local');
const installAppHost = ref('');
const installMode = ref<'docker' | 'native'>('native');
const installLog = ref<Array<{ stream: string; line: string }>>([]);
const installing = ref(false);
const installDone = ref(false);
const installFailed = ref(false);
const installError = ref('');

// Install catalog — drives the radio labels/descriptions for
// deployment, install mode, and the advanced fields shown when the
// user picks ``custom``. We seed it with the JSON snapshot bundled
// into the SPA at build time so the Electron installer stage (which
// runs before any backend exists) has the data immediately, then
// ``loadInstallCatalog`` optionally refreshes it from the live API.
const installCatalog = ref<InstallCatalog | null>(getBundledInstallCatalog());
// Seed the custom-deployment field values with each field's default
// so the form mounts ready-to-submit. The user can still edit any
// field, and an empty field falls back to the catalog default when the
// installer renders the .env.
const installCustomValues = ref<Record<string, string>>(
  Object.fromEntries(
    (installCatalog.value?.deployments?.custom?.advanced_fields ?? []).map(
      (f) => [f.key, f.default ?? ''],
    ),
  ),
);

const installCatalogDeploymentEntries = computed(() => {
  const cat = installCatalog.value;
  if (!cat) return [] as Array<[string, InstallCatalog['deployments'][string]]>;
  return Object.entries(cat.deployments).sort(
    (a, b) => (a[1].order ?? 999) - (b[1].order ?? 999),
  );
});

const installCatalogModeEntries = computed(() => {
  const cat = installCatalog.value;
  if (!cat) return [] as Array<[string, InstallCatalog['modes'][string]]>;
  return Object.entries(cat.modes).sort(
    (a, b) => (a[1].order ?? 999) - (b[1].order ?? 999),
  );
});

const installCatalogCustomFields = computed<DeploymentAdvancedField[]>(() => {
  const cat = installCatalog.value;
  if (!cat) return [];
  return cat.deployments?.custom?.advanced_fields ?? [];
});

// Recommended install mode — derived from the catalog so install.sh,
// install.ps1, and the Setup Wizard never disagree. ``null`` until
// the Electron bridge has reported ``hasDocker``; the install-mode
// step's "recommended" badge hides until this resolves.
const recommendedInstallMode = computed<string | null>(() =>
  recommendInstallMode(installCatalog.value, {
    hasDocker: installEnv.value?.hasDocker ?? false,
  }),
);

// Two visible stages plus the interstitial:
//   'installer'   — Electron-only: Welcome / Deployment / Mode / Install
//   'transition' — install finished; the user clicks Continue to start
//                   the Setup Wizard. Always rendered between the two
//                   stages so the boundary is unambiguous.
//   'setup'       — the existing Setup Wizard (Server / Embedding / …)
//
// Web users (``__IS_ELECTRON__ === false``) and Electron users with a
// prior install start at ``'setup'`` and behave exactly like the
// pre-merge curl-installed flow.
const currentStage = ref<'installer' | 'transition' | 'setup'>(
  isElectron.value && !settingsStore.agentUrl ? 'installer' : 'setup',
);

const installDockerDisabled = computed(() => {
  if (!installEnv.value) return false;
  return !installEnv.value.hasDocker;
});

function installerPushLog(entry: { stream: string; line: string }) {
  for (const piece of entry.line.split(/\r?\n/)) {
    if (piece) installLog.value.push({ stream: entry.stream, line: piece });
  }
  queueMicrotask(() => {
    const el = document.querySelector<HTMLElement>('.installer-log-pane');
    if (el) el.scrollTop = el.scrollHeight;
  });
}

function onInstallerDone(result: { exitCode: number; error?: string }) {
  installing.value = false;
  if (result.exitCode === 0) {
    installDone.value = true;
    // Slide into the transition stage. ``continueToSetupWizard`` is
    // what eventually spawns the backend (creating the SQLite DB at
    // that point), sets agentUrl, and opens the embedding SSE stream
    // — we deliberately do none of that here so the install step
    // leaves no server / no DB behind until the user explicitly
    // continues.
    setTimeout(() => {
      currentStage.value = 'transition';
    }, 600);
  } else {
    installFailed.value = true;
    installError.value =
      result.error || `Installer exited with code ${result.exitCode}.`;
  }
}

async function continueToSetupWizard() {
  // Now that the user has acknowledged the install, spin up the
  // backend (the install script no longer does this in Electron mode
  // — that's how we keep the SQLite DB from being created at install
  // time) and then run the setup-status / preset / catalog fetches
  // before mounting the Setup Wizard. ``checkingStatus`` doubles as
  // the button's loading state.
  checkingStatus.value = true;
  try {
    const bridge = window.cremind?.server;
    if (bridge) {
      const result = await bridge.start();
      if (!result.ok) {
        installFailed.value = true;
        installError.value =
          result.error || 'Failed to start the Cremind backend.';
        // Drop back to the install-run step so the user sees the error
        // alongside the install log.
        currentStage.value = 'installer';
        currentStep.value = installerSteps.length - 1;
        return;
      }
    }
    // Sync the agentUrl now that the backend is up. ``setAgentUrl``
    // persists through to runtimeConfig (main-process JSON file via
    // the preload bridge), so the value survives the cross-origin
    // pivot below.
    if (!settingsStore.agentUrl) {
      await settingsStore.setAgentUrl('http://localhost:1515');
    }
    // Pivot off ``file://`` onto the wheel-served SPA origin before
    // the user touches any setup form. The auth token created in
    // ``handleFinish`` lands in ``localStorage`` on this origin — the
    // same origin every subsequent launch's pivot
    // (``pivotFileWindowsToBackend`` in electron/main.ts) loads — so
    // the user never has to re-enter their token after restart.
    //
    // Path B in ``onMounted`` re-runs ``runPostInstallSetupChecks``
    // against the now-healthy backend on the new origin, so we don't
    // need to call it here. ``embeddingStatusStore.connect`` likewise
    // re-fires from the module-load belt-and-suspenders below (and
    // from App.vue's onMounted) once the SPA boots on the new origin.
    //
    // Install stage stays on the asar — only the *setup* stage moves
    // to http. The user has not entered any wizard data at this seam.
    if (window.location.protocol === 'file:') {
      window.location.replace('http://127.0.0.1:1515/#/setup');
      return;
    }
    embeddingStatusStore.connect(settingsStore.agentUrl);
    await runPostInstallSetupChecks();
  } finally {
    checkingStatus.value = false;
  }
  currentStage.value = 'setup';
  currentStep.value = 0;
}

async function startInstallerRun() {
  const bridge = installerBridge.value;
  if (!bridge) {
    installFailed.value = true;
    installError.value = 'The installer is only available in the Electron app.';
    return;
  }
  installing.value = true;
  installDone.value = false;
  installFailed.value = false;
  installError.value = '';
  installLog.value = [];
  try {
    // For Custom deployment, forward the four advanced-field values so
    // the install script skips its interactive prompts. The script
    // accepts each as a CLI flag (--listen-host / --public-url / etc.)
    // mapped from the install bridge's `customFields` keys.
    const customFields = installDeployment.value === 'custom'
      ? {
          listen_host:     installCustomValues.value.listen_host?.trim() || undefined,
          public_url:      installCustomValues.value.public_url?.trim() || undefined,
          allowed_origins: installCustomValues.value.allowed_origins?.trim() || undefined,
          wizard_preset:   installCustomValues.value.wizard_preset?.trim() || undefined,
        }
      : undefined;
    await bridge.run({
      deployment: installDeployment.value,
      appHost:
        installDeployment.value === 'server' ? installAppHost.value.trim() : undefined,
      mode: installMode.value,
      customFields,
      // Pass the explicit spec only when the user picked "Specific";
      // omitting it lets the install script resolve the channel default
      // (production = Electron build version; test = highest matching
      // ``rcN``).
      version: resolveVersionPayload(),
    });
    // The actual success/failure transition is driven by the
    // ``cremind:installer:done`` event handler (``onInstallerDone``); the
    // ``run`` promise just resolves once the child exits.
  } catch (err) {
    installing.value = false;
    installFailed.value = true;
    installError.value = String(err);
  }
}

// Read embedding lifecycle state from the SSE-driven Pinia store. The
// wizard runs pre-token, but the embedding stream is unauthenticated
// for exactly this case. App.vue's onMounted has already opened the
// shared connection by the time this view is rendered (the wizard is
// rendered as a child route).
const embeddingStatusStore = useEmbeddingStatusStore();
const { status: embeddingStatus, error: embeddingError } = storeToRefs(embeddingStatusStore);

// Belt-and-suspenders: in case the user navigates straight to /setup
// before App.vue's onMounted fires (rare but possible in dev), make
// sure the stream is connected. Skip if agentUrl is still unset —
// that's the Electron first-run case where the backend isn't up yet;
// ``onInstallerDone`` re-runs this once the script has reported success.
if (settingsStore.agentUrl) {
  embeddingStatusStore.connect(settingsStore.agentUrl);
}

// Determine if this is the first setup or a profile-specific setup
const profileName = computed(() => props.profile || 'admin');
const isFirstSetup = ref(true);
const generatedToken = ref('');
const tokenExpiresAt = ref<string>('');
// True once the user has triggered at least one config download on the
// final step. ``handleFinish`` (Start Using Cremind) refuses to proceed
// until this is set — the export bundles the only copy of the JWT plus
// every connection string the user might need later for recovery.
const configSaved = ref(false);
const installSecrets = ref<InstallSecrets | null>(null);
const checkingStatus = ref(true);

// Web users (test/production docker installs opened in a browser) never
// walk through the Electron-only Deployment/Mode steps, so ``installMode``
// and ``installDeployment`` stay at their declared defaults ('native' /
// 'local'). Sync them from the backend's authoritative install-secrets
// response so the downloadable config export reflects the real
// deployment instead of the unmutated form defaults.
function reconcileInstallStateFromSecrets(secrets: InstallSecrets | null) {
  if (!secrets || !secrets.available) return;
  const mode = (secrets.install_mode === 'docker' || secrets.install_mode === 'native')
    ? secrets.install_mode
    : secrets.deployment;
  if (mode === 'docker' || mode === 'native') installMode.value = mode;
  // Only sync the deployment type when the ref is still its untouched
  // default — protects the Electron installer-stage selection from being
  // overwritten when it picked 'custom' or 'server'.
  const env = secrets.setup_wizard_env;
  if (
    installDeployment.value === 'local'
    && (env === 'local' || env === 'server' || env === 'custom')
  ) {
    installDeployment.value = env;
  }
}

// Collected config from each step.
// ``serverConfig`` may carry a nested ``postgres`` object when the admin
// picks PostgreSQL during first setup; everything else is a flat string map.
const serverConfig = ref<Record<string, any>>({});
const embeddingConfig = ref<EmbeddingConfigPayload | null>(null);
const llmConfig = ref<Record<string, string>>({});
const toolConfigs = ref<Record<string, Record<string, string>>>({});
const agentConfigs = ref<Record<string, Record<string, string>>>({});
const channelConfigs = ref<CreateChannelPayload[]>([]);
// Memory opt-in (per-profile). Off by default; persisted as the
// ``memory.enabled`` user_config key in the setup payload.
const memoryEnabled = ref(false);

// Catalog of supported channel types — fetched pre-auth so the post-token
// pairing rows can look up ``setup_kind`` on each created channel.
const channelsCatalog = ref<Record<string, ChannelCatalogEntry>>({});

// Server-side outcome of channel creation, populated after ``handleCompleteSetup``
// succeeds. Used both for surfacing per-channel errors and for driving
// the in-wizard pairing prompts.
const createdChannels = ref<ChannelRow[]>([]);
const channelErrors = ref<Array<{ channel_type: string | null; error: string }>>([]);

// Pairing-dialog state for the post-token pass.
const pairingChannelId = ref<string>('');
const pairingChannelLabel = ref<string>('');
const pairingOpen = ref(false);

// Active environment preset. Determined by ``SETUP_WIZARD_ENV`` in the
// project ``.env`` and reported back by /api/config/setup-profiles. We
// only use it to pre-fill the wizard forms — every step still mounts its
// usual component, every field stays editable. ``null`` means the env
// var is unset and the wizard runs from its built-in component-level
// fallbacks (today's behaviour).
const activeProfile = ref<SetupProfile | null>(null);

// Per-service deployment capabilities from
// /api/services/capabilities. Drives the Deployment-mode radio in the
// Server / Embedding steps. ``null`` while still loading — the steps
// render their forms without the radio until this populates, which is
// the same fallback they used to show with no install locks.
const serviceCapabilities = ref<ServiceCapabilitiesResponse | null>(null);

// First-run installer steps (Electron only, stage = 'installer').
//
// ``install-version`` sits between ``install-mode`` and ``install-run``
// so the user picks the exact cremind package version (or accepts the
// channel default) before the install kicks off. On the production
// channel only one version is valid (== the Electron build), so the
// step is informational; on test, it shows the dropdown of
// ``<electron>rcN`` candidates.
const installerSteps = [
  { key: 'install-welcome', title: 'Welcome', description: 'Get Cremind running' },
  { key: 'install-deployment', title: 'Deployment', description: 'How Cremind is reached' },
  { key: 'install-mode', title: 'Mode', description: 'Docker or native' },
  { key: 'install-version', title: 'Version', description: 'Pick a release' },
  { key: 'install-run', title: 'Install', description: 'Bootstrap the backend' },
] as const;

// ── Version-selection state (install-version step) ──────────────────────
//
// ``versionMode`` is the "Latest / Specific" radio. ``versionSpec`` is
// the user-entered or list-picked version when ``specific``. On the
// production channel the radio is read-only (only one allowed version)
// and ``versionSpec`` always equals the Electron build version.
const versionMode = ref<'latest' | 'specific'>('latest');
const versionSpec = ref<string>('');
const availableVersions = ref<CremindInstallVersions | null>(null);
const versionsLoading = ref(false);
const versionsError = ref<string>('');
const versionInputError = ref<string>('');

function validateVersionSpec(): boolean {
  versionInputError.value = '';
  const channel = availableVersions.value?.channel ?? 'production';
  const electron = availableVersions.value?.electronVersion ?? '';
  const spec = versionSpec.value.trim();
  if (!spec) {
    versionInputError.value = 'Enter a version.';
    return false;
  }
  if (channel === 'production') {
    if (spec !== electron) {
      versionInputError.value =
        `Invalid version: this Electron app supports cremind v${electron} only. ` +
        `Use the in-app update flow to install a different version.`;
      return false;
    }
    return true;
  }
  if (channel === 'test') {
    // No line lock on test — any published RC of any line is installable
    // (incl. the per-PR ``.devM`` form). Validate shape only, then require
    // membership in the fetched list when we have one.
    const okShape = /^\d+\.\d+\.\d+rc\d+(\.dev\d+)?$/.test(spec);
    if (!okShape) {
      versionInputError.value =
        `Invalid version: ${spec} does not look like a test prerelease ` +
        `(expected X.Y.ZrcN[.devM]).`;
      return false;
    }
    // If a list was fetched, require membership; if the list is empty
    // (network blip / no releases yet), accept any shape-valid value
    // so the user isn't blocked by a transient API failure.
    const list = availableVersions.value?.versions ?? [];
    if (list.length > 0 && !list.includes(spec)) {
      versionInputError.value =
        `Invalid version: ${spec} is not a published test release. ` +
        `Pick one from the list.`;
      return false;
    }
    return true;
  }
  return true;
}

// Returns the version string to forward to the installer, or undefined
// to let the script resolve the channel default. Called from
// ``startInstallerRun``.
function resolveVersionPayload(): string | undefined {
  if (versionMode.value === 'latest') return undefined;
  return versionSpec.value.trim() || undefined;
}

async function loadInstallVersions() {
  const bridge = installerBridge.value;
  if (!bridge) return;
  versionsLoading.value = true;
  versionsError.value = '';
  try {
    const result = await bridge.listVersions();
    availableVersions.value = result;
    // Seed the input with the latest entry (or the Electron version for
    // production, where the list is exactly one). The "Latest" radio is
    // still the default; the seed makes "Specific" feel populated when
    // the user toggles into it.
    versionSpec.value = result.latest ?? result.electronVersion;
  } catch (err) {
    versionsError.value = String(err);
    availableVersions.value = null;
  } finally {
    versionsLoading.value = false;
  }
}

// Setup-wizard core steps. ``isFirstSetup=true`` adds the Server +
// Embedding steps that the admin uses to wire up storage and embeddings;
// ``isFirstSetup=false`` is the per-profile flow used for additional
// profiles after admin setup is done.
const setupSteps = computed(() => {
  if (isFirstSetup.value) {
    return [
      { key: 'server', title: 'Server', description: 'General settings' },
      { key: 'embedding', title: 'Vector Embedding', description: 'Optional — semantic search' },
      { key: 'llm', title: 'LLM Providers', description: 'Configure AI models' },
      { key: 'tools', title: 'Tools', description: 'Configure built-in tools' },
      { key: 'memory', title: 'Memory', description: 'Optional — learns habits & facts' },
      { key: 'channels', title: 'Channels', description: 'Connect messaging apps (optional)' },
      { key: 'complete', title: 'Complete', description: 'Generate token' },
    ];
  }
  return [
    { key: 'llm', title: 'LLM Providers', description: 'Configure AI models' },
    { key: 'tools', title: 'Tools', description: 'Configure built-in tools' },
    { key: 'memory', title: 'Memory', description: 'Optional — learns habits & facts' },
    { key: 'channels', title: 'Channels', description: 'Connect messaging apps (optional)' },
    { key: 'complete', title: 'Complete', description: 'Create profile' },
  ];
});

// The set of steps shown for the current stage. The transition stage
// shows no step bar at all (we render an interstitial card instead).
const steps = computed(() => {
  if (currentStage.value === 'installer') return [...installerSteps];
  if (currentStage.value === 'setup') return setupSteps.value;
  return [];
});

function applyProfileDefaults(profile: SetupProfile) {
  activeProfile.value = profile;
  // Seed the step refs so each form mounts with the preset already loaded.
  // Deep-clone so the user editing fields doesn't mutate the cached preset.
  serverConfig.value = JSON.parse(JSON.stringify(profile.server_config ?? {}));
  embeddingConfig.value = profile.embedding_config
    ? (JSON.parse(JSON.stringify(profile.embedding_config)) as EmbeddingConfigPayload)
    : null;
}

// The key of the currently active step
const currentStepKey = computed(() => {
  return steps.value[currentStep.value]?.key || '';
});

// Whether we're on the final step
const isLastStep = computed(() => currentStep.value === steps.value.length - 1);

async function detectInstallEnvironment() {
  const bridge = installerBridge.value;
  if (!bridge) return;
  detectingInstallEnv.value = true;
  try {
    installEnv.value = await bridge.detect();
    // Seed installMode from the catalog-driven recommendation. Falls
    // back to 'native' when no mode's requirements are satisfied
    // (theoretical, since native declares no requires).
    installMode.value = (recommendedInstallMode.value as 'docker' | 'native') ?? 'native';
  } catch (err) {
    installError.value = `Detection failed: ${err}`;
  } finally {
    detectingInstallEnv.value = false;
  }
  // Catalog fetch is independent of the bridge detection — it hits the
  // backend's pre-auth endpoint, which is alive whenever the bundled
  // Vite dev server can proxy to it. The installer stage in Electron
  // runs BEFORE the backend exists; the catalog is fetched from the
  // local Vite dev proxy in dev, and falls back to the hardcoded
  // labels rendered inline below when the backend is unreachable.
  void loadInstallCatalog();
}

async function loadInstallCatalog() {
  // The component already mounted with the bundled catalog seeded in
  // ``installCatalog.value``; this refresh swaps in the live API
  // response when available so the UI picks up any server-side edits
  // to ``install_catalog.toml`` without rebuilding the SPA. Failures
  // are silent — the bundled copy is identical for the same release.
  try {
    const url = settingsStore.agentUrl || 'http://localhost:1515';
    const resp = await fetchInstallCatalog(url);
    installCatalog.value = resp.catalog;
    // Re-seed any custom field that the user hasn't touched yet so a
    // changed default in the live catalog flows through. Fields the
    // user has typed into are left alone.
    for (const f of resp.catalog.deployments?.custom?.advanced_fields ?? []) {
      const current = installCustomValues.value[f.key];
      if (current === undefined || current === '') {
        installCustomValues.value[f.key] = f.default ?? '';
      }
    }
  } catch {
    // Endpoint unreachable (the typical installer-stage case before
    // ``cremind serve`` starts) — keep the bundled catalog already in
    // ``installCatalog.value``.
  }
}

async function runPostInstallSetupChecks() {
  // Mirrors the script-install path: hit the running backend for setup
  // status + preset + channel catalog, populate ``isFirstSetup`` and the
  // pre-fill values, redirect if setup is already complete.
  try {
    const status = await checkSetupStatus(settingsStore.agentUrl, profileName.value);
    isFirstSetup.value = !status.setup_complete;
    try {
      serviceCapabilities.value = await fetchServiceCapabilities(settingsStore.agentUrl);
    } catch {
      // Capability fetch is best-effort — when it fails the wizard
      // continues without per-service deployment radios. Falls back
      // to External-only forms.
      serviceCapabilities.value = null;
    }
    // Load the install catalog so DeploymentModeRadio can read the
    // service-mode labels/descriptions from the same TOML as install.sh
    // and install.ps1. Best-effort: missing-on-the-server falls back to
    // the component's hardcoded labels.
    await loadInstallCatalog();

    if (isFirstSetup.value) {
      for (const p of settingsStore.getLoggedInProfiles()) {
        settingsStore.removeTokenForProfile(p);
      }
      localStorage.removeItem('cremind.notifications.v1');
    }

    if (status.setup_complete && !props.profile) {
      router.replace('/login/admin');
      return;
    }
    if (status.profile_exists && props.profile) {
      router.replace(`/login/${props.profile}`);
      return;
    }

    if (isFirstSetup.value) {
      try {
        const resp = await fetchSetupProfiles(settingsStore.agentUrl);
        if (resp.selected) {
          const preset = (resp.profiles ?? []).find((p) => p.id === resp.selected);
          if (preset) applyProfileDefaults(preset);
        }
      } catch {
        // Server unreachable or endpoint missing — leave the wizard
        // unconfigured; component-level fallbacks take over.
      }
    }

    try {
      channelsCatalog.value = await fetchChannelCatalogPublic(settingsStore.agentUrl);
    } catch {
      // Silent fallback — pairing rows simply won't render.
    }
  } catch {
    // Server unreachable - assume first setup
    isFirstSetup.value = true;
  }
}

onMounted(async () => {
  // Path A: Electron + first run. Host the installer stage here. The
  // backend isn't running yet, so skip ``checkSetupStatus``; we run it
  // lazily when ``continueToSetupWizard`` is clicked.
  if (currentStage.value === 'installer') {
    isFirstSetup.value = true;  // genuine first-run
    checkingStatus.value = false;
    const bridge = installerBridge.value;
    if (bridge) {
      bridge.onLog(installerPushLog);
      bridge.onDone(onInstallerDone);
      void detectInstallEnvironment();
    }
    return;
  }

  // Path B: web build, or Electron with a prior install. No installer
  // stage — run the existing setup-status flow against the already-up
  // backend.
  checkingStatus.value = true;
  await runPostInstallSetupChecks();
  checkingStatus.value = false;
});

onUnmounted(() => {
  const bridge = installerBridge.value;
  if (bridge) {
    bridge.offLog(installerPushLog);
    bridge.offDone(onInstallerDone);
  }
});

function handleServerConfigUpdate(config: Record<string, any>) {
  serverConfig.value = config;
}

function handleEmbeddingConfigUpdate(config: EmbeddingConfigPayload) {
  embeddingConfig.value = config;
}

function handleLLMConfigUpdate(config: Record<string, string>) {
  llmConfig.value = config;
}

function handleToolConfigsUpdate(configs: Record<string, Record<string, string>>) {
  toolConfigs.value = configs;
}

function handleAgentConfigsUpdate(configs: Record<string, Record<string, string>>) {
  agentConfigs.value = configs;
}

function handleChannelConfigsUpdate(configs: CreateChannelPayload[]) {
  channelConfigs.value = configs;
}

function modeNeedsPairing(channelType: string, modeId: string): boolean {
  const entry = channelsCatalog.value[channelType];
  if (!entry) return false;
  const mode = entry.modes.find((m) => m.id === modeId);
  return Boolean(mode && mode.setup_kind);
}

const channelsAwaitingPairing = computed(() =>
  createdChannels.value.filter((c) => modeNeedsPairing(c.channel_type, c.mode)),
);

function openPairingFor(channel: ChannelRow) {
  pairingChannelId.value = channel.id;
  pairingChannelLabel.value =
    channelsCatalog.value[channel.channel_type]?.display_name || channel.channel_type;
  pairingOpen.value = true;
}

// Per-step gates. Disabling Next is friendlier than silently advancing
// into an invalid state.
const canAdvanceFromInstallerDeployment = computed(() => {
  if (installDeployment.value === 'local') return true;
  if (installDeployment.value === 'server') {
    return /^[A-Za-z0-9.:-]+$/.test(installAppHost.value.trim());
  }
  // Custom: require listen_host + public_url so the rendered .env at
  // least has the two values the backend can't guess. Allowed origins
  // and wizard preset have catalog defaults that fill in for blanks.
  if (installDeployment.value === 'custom') {
    return Boolean(
      installCustomValues.value.listen_host?.trim()
      && installCustomValues.value.public_url?.trim(),
    );
  }
  return false;
});
const canAdvanceFromInstallerMode = computed(() => {
  if (installMode.value === 'docker' && installDockerDisabled.value) return false;
  return true;
});
// Block Next on the version step until either ``latest`` is selected
// (the default — always valid) or the user-entered ``specific`` value
// passes channel + Electron-line validation.
const canAdvanceFromInstallerVersion = computed(() => {
  if (versionMode.value === 'latest') return true;
  if (!availableVersions.value) return false;
  return validateVersionSpec();
});

function handleNext() {
  const key = currentStepKey.value;

  // Installer phase gates / actions.
  if (key === 'install-welcome') {
    if (detectingInstallEnv.value || !installEnv.value) return;
    currentStep.value++;
    return;
  }
  if (key === 'install-deployment') {
    if (!canAdvanceFromInstallerDeployment.value) return;
    currentStep.value++;
    return;
  }
  if (key === 'install-mode') {
    if (!canAdvanceFromInstallerMode.value) return;
    // Advance into the Version step; load the available-versions list
    // lazily so the GitHub round-trip happens off the welcome screen.
    currentStep.value++;
    void loadInstallVersions();
    return;
  }
  if (key === 'install-version') {
    if (!canAdvanceFromInstallerVersion.value) return;
    // Advance into the Install step and trigger the script there.
    currentStep.value++;
    void startInstallerRun();
    return;
  }
  if (key === 'install-run') {
    // No manual advance — onInstallerDone schedules the transition once
    // the script reports success.
    return;
  }

  // Existing setup-step gates.
  if (key === 'embedding' && embeddingConfig.value?.enabled) {
    if (embeddingConfig.value.provider === 'gemma' && !embeddingConfig.value.hf_token.trim()) {
      ElMessage.error('HF_TOKEN is required when the embedding provider is Gemma.');
      return;
    }
  }
  if (currentStep.value < steps.value.length - 1) {
    currentStep.value++;
  }
}

function handlePrev() {
  // No going back from the install-run step once the script has started:
  // the venv / db / .env writes are already in flight.
  if (currentStepKey.value === 'install-run' && (installing.value || installDone.value)) {
    return;
  }
  if (currentStep.value > 0) {
    currentStep.value--;
  }
}

function retryInstallerRun() {
  installFailed.value = false;
  installError.value = '';
  installLog.value = [];
  void startInstallerRun();
}

async function handleCompleteSetup() {
  submitting.value = true;
  setupLog.value = [];

  // Subscribe to the per-phase progress stream BEFORE firing the POST so
  // we don't miss the first few events (feature install / db validation
  // happen within milliseconds of the request hitting the handler).
  setupProgressHandle = openSetupProgressStream(
    settingsStore.agentUrl,
    (entry) => {
      setupLog.value = [...setupLog.value, entry];
    },
  );

  try {
    const config: Record<string, unknown> = { profile: profileName.value };

    if (isFirstSetup.value) {
      config.server_config = serverConfig.value;
      if (embeddingConfig.value) {
        config.embedding_config = embeddingConfig.value;
      }
    }
    // Always send LLM and tool configs (both first setup and profile setup)
    config.llm_config = llmConfig.value;
    config.tool_configs = toolConfigs.value;
    // Per-profile general settings; the Memory opt-in is the only one collected
    // in the wizard today. Backend validates against CONFIG_SCHEMA.
    config.user_config = { 'memory.enabled': String(memoryEnabled.value) };
    if (Object.keys(agentConfigs.value).length > 0) {
      config.agent_configs = agentConfigs.value;
    }
    if (channelConfigs.value.length > 0) {
      config.channel_configs = channelConfigs.value;
    }

    const result = await completeSetup(settingsStore.agentUrl, config as any);
    generatedToken.value = result.token;
    tokenExpiresAt.value = (result as any).expires_at || '';
    restartRequired.value = Boolean((result as any).restart_required);
    createdChannels.value = (result as any).channels || [];
    channelErrors.value = (result as any).channel_errors || [];

    // Fire-and-forget: the downloadable config bundle wants Docker
    // install-time secrets (VNC password, docker-generated PG creds)
    // that don't live in wizard state. A failure here just means the
    // export omits the Docker section — never block setup completion
    // on it.
    fetchInstallSecrets(settingsStore.agentUrl, generatedToken.value)
      .then((secrets) => {
        installSecrets.value = secrets;
        reconcileInstallStateFromSecrets(secrets);
      })
      .catch(() => { installSecrets.value = null; });

    // Activate the token immediately so the in-wizard pairing dialog
    // (ChannelPairingDialog) can authenticate against the SSE stream.
    // Final navigation + chat-state reset still waits for the user to
    // click "Start Using Cremind" in handleFinish().
    if (channelsAwaitingPairing.value.length > 0) {
      settingsStore.setTokenForProfile(profileName.value, generatedToken.value);
      settingsStore.activateProfile(profileName.value);
    }

    // The backend kicks off model load + cache rebuild in the
    // background when first-setup completes with embedding enabled.
    // We don't need to poll — the embedding-status SSE store is
    // already subscribed and will receive each phase transition
    // automatically. Status-driven computeds below gate the
    // "Start Using Cremind" button.

    if (channelErrors.value.length > 0) {
      ElMessage.warning(
        `Setup completed, but ${channelErrors.value.length} channel(s) failed to register.`,
      );
    } else {
      ElMessage.success('Setup completed! Copy your token below.');
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Setup failed');
  } finally {
    submitting.value = false;
    if (setupProgressHandle) {
      setupProgressHandle.close();
      setupProgressHandle = null;
    }
  }
}

const latestSetupLogMessage = computed(() => {
  if (setupLog.value.length === 0) return '';
  return setupLog.value[setupLog.value.length - 1].message;
});

async function handleFinish() {
  if (!generatedToken.value) {
    ElMessage.warning('Please complete setup first.');
    return;
  }
  if (!configSaved.value) {
    ElMessage.warning('Save the configuration file first — it holds your JWT and connection details.');
    return;
  }
  // Setup has completed successfully (we have a token). Always allow the
  // user to proceed to the main chat view; no restart-wait gate. New
  // feature extras (postgres, llm.*, …) are loaded by the deferred-boot
  // path on first use, and any embedding-load failure can be diagnosed
  // from Settings → Vector Embedding later.
  if (embeddingFailed.value) {
    ElMessage.error(
      `Vector embedding failed to initialize: ${embeddingError.value ?? 'unknown error'}. ` +
      'You can still proceed, but Automatic Skill Mode and other embedding-dependent features will be unavailable.'
    );
  }

  // Save token per-profile and activate, then redirect to chat
  settingsStore.setTokenForProfile(profileName.value, generatedToken.value);
  settingsStore.activateProfile(profileName.value);
  configStore.setupComplete = true;
  // Drop any in-memory chat state from the pre-wipe session before entering
  // the chat view; otherwise the conversations list / messages survive and
  // their detail views 404 against the freshly empty DB.
  await chatStore.resetForProfileSwitch();
  router.push(`/${profileName.value}`);
}

const embeddingReady = computed(() => {
  if (embeddingStatus.value === 'ready') return true;
  // 'disabled' only counts as ready if the user actually chose to leave
  // embedding off. After a restart-required install the runtime is also
  // 'disabled' (the boot path skipped the model load because SQLite was
  // still empty when boot ran) — but the user did ask for embedding, so
  // we must wait until the post-restart boot flips status to 'ready'.
  if (embeddingStatus.value === 'disabled') {
    return !(embeddingConfig.value?.enabled && restartRequired.value);
  }
  return false;
});

const embeddingBlocking = computed(() => {
  if (!isFirstSetup.value) return false;
  if (!embeddingConfig.value?.enabled) return false;
  // A 'failed' status is terminal: the boot tried to load embedding and
  // didn't make it. Let the user proceed (handleFinish surfaces the error
  // and runs in degraded mode); don't trap them on the restart-wait gate.
  if (
    restartRequired.value
    && embeddingStatus.value !== 'ready'
    && embeddingStatus.value !== 'failed'
  ) return true;
  return embeddingStatus.value === 'initializing'
    || embeddingStatus.value === 'rebuilding';
});

const embeddingFailed = computed(() => embeddingStatus.value === 'failed');

function buildConfigSnapshot(): ConfigExportSnapshot {
  const server = serverConfig.value as Record<string, any>;
  const dbProvider: 'postgres' | 'sqlite' = server.db_provider === 'postgres'
    ? 'postgres'
    : 'sqlite';

  let postgres: ConfigExportSnapshot['database']['postgres'] | undefined;
  if (dbProvider === 'postgres') {
    const pg = (server.postgres ?? {}) as Record<string, any>;
    const secrets = installSecrets.value;
    postgres = {
      host: String(pg.host ?? secrets?.pg_host ?? 'localhost'),
      port: Number(pg.port ?? secrets?.pg_port ?? 5432),
      database: String(pg.database ?? secrets?.pg_database ?? 'cremind'),
      user: String(pg.user ?? secrets?.pg_user ?? 'cremind'),
      password: String(pg.password ?? secrets?.pg_password ?? ''),
      sslmode: pg.sslmode ? String(pg.sslmode)
        : (secrets?.pg_sslmode ?? undefined),
    };
  }

  const workingDir = String(server.user_working_dir ?? '~/Documents');
  const systemDir = String(server.system_dir ?? '~/.cremind');
  const sqlitePath = `${systemDir.replace(/[\\/]+$/, '')}/storage/cremind.db`;

  // Vector store info lives in embeddingConfig (the wizard's local ref
  // submitted to /api/config/setup) — NOT serverConfig. The flat
  // ``vectorstore.*`` keys only land in the SQLite server_config table
  // after persist_embedding_config runs, and the wizard never refetches.
  const vs = embeddingConfig.value?.vectorstore;
  const vectorProvider: 'qdrant' | 'chroma' | 'none' = (
    embeddingConfig.value?.enabled
    && (vs?.provider === 'qdrant' || vs?.provider === 'chroma')
  ) ? vs!.provider : 'none';

  let qdrant: ConfigExportSnapshot['vectorStore']['qdrant'] | undefined;
  if (vectorProvider === 'qdrant' && vs) {
    qdrant = {
      host: String(vs.qdrant.host ?? 'localhost'),
      port: Number(vs.qdrant.port ?? 6333),
      api_key: vs.qdrant.api_key ? String(vs.qdrant.api_key) : undefined,
      https: Boolean(vs.qdrant.https),
    };
  }

  let chroma: ConfigExportSnapshot['vectorStore']['chroma'] | undefined;
  if (vectorProvider === 'chroma' && vs) {
    // Same persist-vs-http translation as
    // app/lib/embedding_lifecycle.py:120 (Native → persistent file;
    // Docker / External → http endpoint).
    const mode: 'http' | 'persistent' =
      vs.chroma.deployment_mode === 'native' ? 'persistent' : 'http';
    chroma = {
      mode,
      host: vs.chroma.host || undefined,
      port: vs.chroma.port ?? undefined,
      ssl: vs.chroma.ssl ?? undefined,
      api_key: vs.chroma.api_key ? String(vs.chroma.api_key) : undefined,
      persist_path: vs.chroma.persist_path
        ? String(vs.chroma.persist_path) : undefined,
    };
  }

  const includeVnc = installMode.value === 'docker'
    && Boolean(installSecrets.value?.vnc_password);

  let vnc: ConfigExportSnapshot['vnc'] | undefined;
  if (includeVnc) {
    const secrets = installSecrets.value!;
    // Derive the host from APP_URL so server / custom deployments show a
    // host the user can actually reach. APP_URL is provided by the
    // install-secrets endpoint from the cremind container's env. Falls
    // back to localhost for local installs (APP_URL absent or missing
    // host portion).
    let host = 'localhost';
    if (secrets.app_url) {
      const m = secrets.app_url.match(/^[a-z]+:\/\/([^/:]+)/i);
      if (m && m[1]) host = m[1];
    }
    // Kubernetes runs a single pod behind an nginx proxy that fronts the
    // SPA, API and noVNC on ONE port — there are no separately-exposed
    // 6080/5900 ports. The agent desktop lives at the app origin under
    // /vnc/vnc.html (e.g. http://localhost:1515/vnc/vnc.html for the
    // documented `port-forward svc/cremind 1515:80`, or https://<host>/...
    // behind an Ingress). See helm/cremind/templates/proxy-configmap.yaml.
    if (secrets.install_mode === 'kubernetes') {
      let origin = 'http://localhost:1515';
      if (secrets.app_url) {
        try { origin = new URL(secrets.app_url).origin; } catch { /* keep default */ }
      }
      vnc = {
        password: secrets.vnc_password as string,
        host,
        novnc_url: `${origin}/vnc/vnc.html`,
        resolution: secrets.resolution || undefined,
        environment: 'kubernetes',
      };
    } else {
      const novncPort = secrets.novnc_port ?? 6080;
      const vncPort = secrets.vnc_port ?? 5900;
      vnc = {
        password: secrets.vnc_password as string,
        host,
        novnc_port: novncPort,
        vnc_port: vncPort,
        novnc_url: `http://${host}:${novncPort}/vnc.html`,
        vnc_endpoint: `${host}:${vncPort}`,
        resolution: secrets.resolution || undefined,
        environment: 'docker',
      };
    }
  }

  const customFields = installDeployment.value === 'custom'
    ? { ...installCustomValues.value } : undefined;

  return {
    profile: profileName.value,
    token: generatedToken.value,
    tokenExpiresAt: tokenExpiresAt.value,
    agentUrl: settingsStore.agentUrl,
    generatedAt: new Date().toISOString(),
    deployment: {
      type: installDeployment.value,
      mode: installMode.value,
      customFields,
    },
    vnc,
    workingDir,
    systemDir,
    database: {
      provider: dbProvider,
      postgres,
      sqlite: dbProvider === 'sqlite' ? { path: sqlitePath } : undefined,
    },
    vectorStore: {
      provider: vectorProvider,
      qdrant,
      chroma,
    },
    embedding: {
      enabled: Boolean(embeddingConfig.value?.enabled),
      provider: embeddingConfig.value?.provider,
    },
    channels: createdChannels.value.map((ch) => ({
      type: String((ch as any).channel_type ?? ''),
      mode: String((ch as any).mode ?? ''),
      id: String((ch as any).id ?? ''),
    })),
  };
}

async function downloadConfigFile(format: ExportFormat) {
  if (!generatedToken.value) {
    ElMessage.warning('Setup is not complete yet.');
    return;
  }
  // handleCompleteSetup kicks off the install-secrets fetch fire-and-forget
  // as a pre-warmer. If the user clicks Download before that resolves,
  // installSecrets.value is still null — wait for the fetch here so the
  // exported file contains VNC password and Docker-provisioned Postgres
  // credentials instead of silently omitting them.
  if (!installSecrets.value) {
    try {
      installSecrets.value = await fetchInstallSecrets(
        settingsStore.agentUrl, generatedToken.value,
      );
      reconcileInstallStateFromSecrets(installSecrets.value);
    } catch {
      installSecrets.value = null;
    }
  }
  const snapshot = buildConfigSnapshot();
  const content = buildExport(format, snapshot);
  const mime = exportMimeType(format);
  const filename = `cremind-${profileName.value}-config.${format}`;
  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  // Recovering credentials, paths, and the JWT token without the export
  // is painful, so we gate "Start Using Cremind" on this flag — the user
  // must trigger at least one download before they can leave the wizard.
  configSaved.value = true;
}
</script>

<template>
  <div class="setup-wizard">
    <div v-if="checkingStatus" class="loading-state">Checking setup status...</div>
    <div v-else class="setup-container">
      <div class="setup-header">
        <h1 class="setup-title">
          <template v-if="currentStage === 'installer'">
            Cremind — first-run installer
          </template>
          <template v-else-if="currentStage === 'transition'">
            Installation complete
          </template>
          <template v-else>
            {{ isFirstSetup ? 'Welcome to Cremind' : `Setup Profile: ${profileName}` }}
          </template>
        </h1>
        <p class="setup-subtitle">
          <template v-if="currentStage === 'installer'">
            Set up the backend, then continue to the Setup Wizard.
          </template>
          <template v-else-if="currentStage === 'transition'">
            The backend is running. Continue to configure your LLM
            providers, tools, and first profile.
          </template>
          <template v-else>
            {{ isFirstSetup ? "Let's set up your personal assistant" : 'Configure your new profile' }}
          </template>
        </p>
        <div
          v-if="activeProfile && currentStage === 'setup'"
          class="setup-profile-badge"
        >
          Pre-filled from <strong>{{ activeProfile.label }}</strong> preset (SETUP_WIZARD_ENV)
        </div>
      </div>

      <ElSteps
        v-if="currentStage !== 'transition'"
        :active="currentStep"
        finish-status="success"
        align-center
        class="setup-steps"
      >
        <ElStep v-for="step in steps" :key="step.key" :title="step.title" :description="step.description" />
      </ElSteps>

      <!-- Interstitial between the two stages. The user clicks Continue
           to enter the Setup Wizard; making this an explicit step keeps
           the boundary unambiguous and gives the user a chance to re-read
           the install log before moving on. -->
      <div v-if="currentStage === 'transition'" class="stage-transition">
        <div class="stage-transition-icon"></div>
        <h2>First-run installer finished.</h2>
        <p>
          The Cremind backend is up and running. Next: configure your LLM
          providers, built-in tools, channels, and create the first
          profile in the Setup Wizard.
        </p>
        <details class="stage-transition-log">
          <summary>View install log</summary>
          <pre class="installer-log-pane installer-log-pane--inline">
            <span
              v-for="(entry, i) in installLog" :key="i"
              :class="['installer-log-line', `installer-log-${entry.stream}`]"
            >{{ entry.line }}</span>
          </pre>
        </details>
        <ElButton
          type="primary"
          size="large"
          :loading="checkingStatus"
          @click="continueToSetupWizard"
        >
          {{ checkingStatus ? 'Loading setup wizard…' : 'Continue to Setup Wizard →' }}
        </ElButton>
      </div>

      <div class="step-content">
        <!-- ── Installer phase (Electron + first-run only) ──────── -->
        <div v-if="currentStepKey === 'install-welcome' && showRestoreCard" class="installer-pane">
          <RestoreFromBackupCard @cancel="showRestoreCard = false" />
        </div>

        <div v-else-if="currentStepKey === 'install-welcome'" class="installer-pane">
          <h2>Let's get Cremind running.</h2>
          <p v-if="installEnv?.channel === 'dev'">
            Developer install. We'll reuse the local checkout's
            <code>.venv</code> instead of downloading from PyPI. After a
            few quick questions we'll start the backend, then continue to
            the setup wizard — all inside this window.
          </p>
          <p v-else>
            This installer runs the same script as
            <code>curl …/install.sh</code>. We'll ask three quick questions
            (deployment, host, and run mode), then install Cremind and
            continue to the setup wizard for the first profile.
          </p>
          <p v-if="installEnv" class="installer-env-tags">
            <ElTag :type="installEnv.hasDocker ? 'success' : 'info'">
              Docker {{ installEnv.hasDocker ? 'detected' : 'not available' }}
            </ElTag>
            <ElTag :type="installEnv.hasPython ? 'success' : 'info'">
              Python {{ installEnv.hasPython ? installEnv.pythonVersion : 'not on PATH' }}
            </ElTag>
            <ElTag>{{ installEnv.os }} / {{ installEnv.arch }}</ElTag>
          </p>
          <p v-else-if="detectingInstallEnv">Detecting your environment…</p>
          <p class="restore-link">
            Already have a Cremind backup?
            <a @click="showRestoreCard = true">Restore it instead</a>
          </p>
        </div>

        <div v-else-if="currentStepKey === 'install-deployment'" class="installer-pane">
          <h2>How will you reach Cremind?</h2>
          <ElForm label-position="top">
            <ElFormItem>
              <!-- Render from the install catalog when it's loaded so the
                   labels match install.sh / install.ps1; fall back to a
                   small hardcoded list when the backend is unreachable
                   (the very-first-run case before any backend exists). -->
              <ElRadioGroup v-model="installDeployment">
                <template v-if="installCatalogDeploymentEntries.length > 0">
                  <ElRadio
                    v-for="[id, entry] in installCatalogDeploymentEntries"
                    :key="id"
                    :value="id"
                  >
                    <strong>{{ entry.label }}</strong> — {{ entry.description }}
                  </ElRadio>
                </template>
                <template v-else>
                  <ElRadio value="local">
                    <strong>Local</strong> — bind to 127.0.0.1, only this machine
                  </ElRadio>
                  <ElRadio value="server">
                    <strong>Server</strong> — bind to all interfaces, reachable from other devices
                  </ElRadio>
                  <ElRadio value="custom">
                    <strong>Custom (advanced)</strong> — pick host, URL, and CORS yourself
                  </ElRadio>
                </template>
              </ElRadioGroup>
            </ElFormItem>
            <ElFormItem v-if="installDeployment === 'server'" label="Public IP or domain">
              <ElInput v-model="installAppHost" placeholder="e.g. 100.120.175.90 or cremind.example.com" />
              <p class="installer-hint">
                Used in the public URL the agent advertises and in the
                list of origins allowed to talk to the API. Letters,
                digits, dot, colon, and hyphen only.
              </p>
            </ElFormItem>
            <!-- Custom-deployment advanced fields. Each one renders with
                 the prompt + hint from the catalog so the wording stays
                 in lock-step with install.sh / install.ps1. -->
            <template v-if="installDeployment === 'custom'">
              <ElFormItem
                v-for="field in installCatalogCustomFields"
                :key="field.key"
                :label="field.prompt"
              >
                <ElInput
                  v-if="!field.choices || field.choices.length === 0"
                  v-model="installCustomValues[field.key]"
                  :placeholder="field.default"
                />
                <ElRadioGroup
                  v-else
                  v-model="installCustomValues[field.key]"
                >
                  <ElRadio v-for="c in field.choices" :key="c" :value="c">
                    {{ c }}
                  </ElRadio>
                </ElRadioGroup>
                <p class="installer-hint">{{ field.hint }}</p>
              </ElFormItem>
            </template>
          </ElForm>
        </div>

        <div v-else-if="currentStepKey === 'install-mode'" class="installer-pane">
          <h2>How should Cremind run?</h2>
          <ElForm label-position="top">
            <ElFormItem>
              <ElRadioGroup v-model="installMode">
                <template v-if="installCatalogModeEntries.length > 0">
                  <ElRadio
                    v-for="[id, entry] in installCatalogModeEntries"
                    :key="id"
                    :value="id"
                    :disabled="id === 'docker' && installDockerDisabled"
                  >
                    <strong>{{ entry.label }}</strong> — {{ entry.description }}
                    <span
                      v-if="recommendedInstallMode === id"
                      class="installer-badge"
                    >recommended</span>
                    <span class="installer-hint">
                      {{ entry.hint }}
                      <template v-if="id === 'native' && installEnv && !installEnv.hasPython">
                        <br>Python 3.13 isn't on PATH; the installer will fetch an isolated copy via uv (~80 MB).
                      </template>
                    </span>
                  </ElRadio>
                </template>
                <!-- Backend unreachable: render the same two modes
                     hardcoded so the installer stage works before any
                     backend exists. -->
                <template v-else>
                  <ElRadio value="docker" :disabled="installDockerDisabled">
                    <strong>Docker</strong> — sandboxed VNC desktop with a bundled storage stack
                  </ElRadio>
                  <ElRadio value="native">
                    <strong>Native</strong> — Python venv at <code>~/.cremind/venv</code> with embedded storage
                  </ElRadio>
                </template>
              </ElRadioGroup>
            </ElFormItem>
          </ElForm>
        </div>

        <div v-else-if="currentStepKey === 'install-version'" class="installer-pane">
          <h2>Pick a release</h2>
          <p v-if="availableVersions" class="installer-hint">
            This Electron app is <strong>v{{ availableVersions.electronVersion }}</strong>
            on the <strong>{{ availableVersions.channel }}</strong> channel.
          </p>
          <ElAlert
            v-if="versionsError"
            type="warning" show-icon :closable="false"
            title="Couldn't fetch the version list from GitHub."
            :description="versionsError + ' — you can still continue with the channel default.'"
            style="margin-bottom: 12px;"
          />
          <ElForm label-position="top">
            <ElFormItem>
              <ElRadioGroup v-model="versionMode">
                <ElRadio value="latest">
                  <strong>Latest version (default)</strong>
                  <span class="installer-hint">
                    <template v-if="availableVersions?.channel === 'production'">
                      Installs cremind v{{ availableVersions.electronVersion }} — the
                      version this Electron app was built against.
                    </template>
                    <template v-else-if="availableVersions?.channel === 'test' && availableVersions?.latest">
                      Installs cremind {{ availableVersions.latest }} — the highest
                      <code>{{ availableVersions.electronVersion }}rcN</code> prerelease.
                    </template>
                    <template v-else-if="availableVersions?.channel === 'dev'">
                      Editable install from the local checkout (no PyPI fetch).
                    </template>
                    <template v-else>
                      The installer resolves the channel default.
                    </template>
                  </span>
                </ElRadio>
                <ElRadio
                  value="specific"
                  :disabled="!availableVersions || availableVersions.channel === 'dev' || availableVersions.channel === 'production'"
                >
                  <strong>Specific version</strong>
                  <span class="installer-hint">
                    <template v-if="availableVersions?.channel === 'production'">
                      Production requires an exact match to this Electron build — use
                      the in-app update flow to install a different version.
                    </template>
                    <template v-else-if="availableVersions?.channel === 'dev'">
                      Not applicable on the dev channel.
                    </template>
                    <template v-else>
                      Pick a published prerelease from the list, or type one.
                    </template>
                  </span>
                </ElRadio>
              </ElRadioGroup>
            </ElFormItem>

            <ElFormItem
              v-if="versionMode === 'specific' && availableVersions?.channel === 'test'"
              label="Test release"
              :error="versionInputError"
            >
              <ElSelect
                v-if="availableVersions.versions.length > 0"
                v-model="versionSpec"
                placeholder="Select a published prerelease"
                style="width: 100%; margin-bottom: 8px;"
                @change="validateVersionSpec"
              >
                <ElOption
                  v-for="v in [...availableVersions.versions].reverse()"
                  :key="v"
                  :value="v"
                  :label="v"
                />
              </ElSelect>
              <ElInput
                v-model="versionSpec"
                placeholder="e.g. 0.2.1rc3"
                @blur="validateVersionSpec"
                @input="validateVersionSpec"
              />
            </ElFormItem>
          </ElForm>
        </div>

        <div v-else-if="currentStepKey === 'install-run'" class="installer-pane">
          <h2 v-if="installing">Installing…</h2>
          <h2 v-else-if="installDone">Installed.</h2>
          <h2 v-else-if="installFailed">Install failed.</h2>
          <h2 v-else>Preparing installer…</h2>
          <ElAlert
            v-if="installFailed"
            type="error" show-icon :closable="false"
            :title="installError || 'The installer reported an error.'"
            description="Scroll the log below for details. You can retry once the issue is resolved."
            style="margin-bottom: 12px;"
          />
          <pre class="installer-log-pane">
            <span
              v-for="(entry, i) in installLog" :key="i"
              :class="['installer-log-line', `installer-log-${entry.stream}`]"
            >{{ entry.line }}</span>
          </pre>
        </div>

        <!-- ── Existing setup steps ───────────────────────────── -->
        <StepServerConfig
          v-else-if="currentStepKey === 'server'"
          :config="serverConfig"
          :first-setup="isFirstSetup"
          :service-capabilities="serviceCapabilities"
          :install-catalog="installCatalog"
          @update="handleServerConfigUpdate"
        />
        <StepEmbeddingConfig
          v-else-if="currentStepKey === 'embedding'"
          :config="embeddingConfig ?? {}"
          :service-capabilities="serviceCapabilities"
          :install-catalog="installCatalog"
          @update="handleEmbeddingConfigUpdate"
        />
        <StepLLMConfig
          v-else-if="currentStepKey === 'llm'"
          :agent-url="settingsStore.agentUrl"
          :config="llmConfig"
          @update="handleLLMConfigUpdate"
        />
        <StepToolConfig
          v-else-if="currentStepKey === 'tools'"
          :agent-url="settingsStore.agentUrl"
          :configs="toolConfigs"
          :profile="profileName"
          :is-first-setup="isFirstSetup"
          @update="handleToolConfigsUpdate"
          @update:agent-configs="handleAgentConfigsUpdate"
        />
        <StepMemoryConfig
          v-else-if="currentStepKey === 'memory'"
          :enabled="memoryEnabled"
          @update="memoryEnabled = $event"
        />
        <StepChannelsConfig
          v-else-if="currentStepKey === 'channels'"
          :agent-url="settingsStore.agentUrl"
          :configs="channelConfigs"
          @update="handleChannelConfigsUpdate"
        />
        <StepProfileCreate
          v-else-if="currentStepKey === 'complete'"
          :token="generatedToken"
          :submitting="submitting"
          :profile-name="profileName"
          :config-saved="configSaved"
          @generate="handleCompleteSetup"
          @download="downloadConfigFile"
        />

        <div
          v-if="generatedToken && channelErrors.length > 0"
          class="channel-errors-box"
        >
          <strong>{{ channelErrors.length }} channel(s) failed to register</strong>
          <ul>
            <li v-for="(err, i) in channelErrors" :key="i">
              {{ err.channel_type || 'unknown' }}: {{ err.error }}
            </li>
          </ul>
          <p class="hint">
            Setup completed; you can re-add these from
            <strong>Settings → Channels</strong> after entering Cremind.
          </p>
        </div>

        <div
          v-if="generatedToken && channelsAwaitingPairing.length > 0"
          class="pairing-box"
        >
          <strong>Pair your channels</strong>
          <p>
            The channels below need an interactive pairing step (QR scan or
            verification code) before they can receive messages. You can
            pair them now or skip and finish from
            <strong>Settings → Channels</strong> later.
          </p>
          <div
            v-for="ch in channelsAwaitingPairing"
            :key="ch.id"
            class="pairing-row"
          >
            <span class="pairing-name">
              {{ channelsCatalog[ch.channel_type]?.display_name || ch.channel_type }}
              <small>({{ ch.mode }})</small>
            </span>
            <ElButton size="small" @click="openPairingFor(ch)">Pair now</ElButton>
          </div>
        </div>

        <div
          v-if="submitting || setupLog.length > 0"
          class="setup-log-box"
        >
          <div class="setup-log-header">
            <strong>{{ submitting ? 'Setup in progress…' : 'Setup log' }}</strong>
            <span v-if="submitting && latestSetupLogMessage" class="setup-log-latest">
              {{ latestSetupLogMessage }}
            </span>
          </div>
          <pre v-if="setupLog.length > 0" ref="setupLogEl" class="setup-log-stream"><span
              v-for="(entry, i) in setupLog"
              :key="i"
              :class="['setup-log-line', `is-${entry.level}`]"
            >[{{ entry.step }}] {{ entry.message }}
</span></pre>
        </div>

        <div
          v-if="generatedToken && embeddingConfig?.enabled && !embeddingReady"
          class="embedding-status-box"
          :class="{ 'is-failed': embeddingFailed }"
        >
          <template v-if="restartRequired && embeddingStatus !== 'ready'">
            <ElAlert
              type="warning"
              :closable="false"
              show-icon
              title="Server restart required"
            >
              <p>
                Cremind just installed new dependencies for Vector Embedding
                (sentence-transformers / torch). These can't be re-imported
                into the running process safely, so the embedding subsystem
                is deferred until the next boot.
              </p>
              <p>
                Stop and rerun <code>cremind serve</code> (or quit and reopen
                the Cremind desktop app). This page will unlock automatically
                once the server reports embedding ready.
              </p>
            </ElAlert>
          </template>
          <template v-else-if="embeddingBlocking">
            <strong>Loading vector embedding model…</strong>
            <p>
              The embedding model is downloading and loading into memory.
              This can take a minute on first run.
              The Start button will activate when it's ready.
            </p>
          </template>
          <template v-else-if="embeddingFailed">
            <strong>Vector embedding failed to initialize</strong>
            <p>{{ embeddingError ?? 'Unknown error.' }}</p>
            <p class="hint">
              You can still proceed — Automatic Skill Mode and other
              embedding-dependent features will be unavailable until you
              fix the backing service or disable Vector Embedding from Settings.
            </p>
          </template>
        </div>
      </div>

      <div v-if="currentStage !== 'transition'" class="step-actions">
        <!-- Previous: hidden during the install-run step and after the
             token has been generated. -->
        <ElButton
          v-if="currentStep > 0 && !generatedToken
                && !(currentStepKey === 'install-run' && (installing || installDone))"
          @click="handlePrev"
        >
          Previous
        </ElButton>
        <div class="spacer"></div>

        <!-- Installer-phase action buttons (replace the generic Next on
             the install steps) -->
        <template v-if="currentStepKey === 'install-welcome'">
          <ElButton
            type="primary"
            :disabled="detectingInstallEnv || !installEnv"
            @click="handleNext"
          >
            Get started
          </ElButton>
        </template>
        <template v-else-if="currentStepKey === 'install-deployment'">
          <ElButton
            type="primary"
            :disabled="!canAdvanceFromInstallerDeployment"
            @click="handleNext"
          >
            Next
          </ElButton>
        </template>
        <template v-else-if="currentStepKey === 'install-mode'">
          <ElButton
            type="primary"
            :disabled="!canAdvanceFromInstallerMode"
            @click="handleNext"
          >
            Next
          </ElButton>
        </template>
        <template v-else-if="currentStepKey === 'install-version'">
          <ElButton
            type="primary"
            :loading="versionsLoading"
            :disabled="!canAdvanceFromInstallerVersion"
            @click="handleNext"
          >
            Install
          </ElButton>
        </template>
        <template v-else-if="currentStepKey === 'install-run'">
          <ElButton
            v-if="installFailed"
            type="primary"
            @click="retryInstallerRun"
          >
            Try again
          </ElButton>
          <!-- During install and on success: no manual action; the
               install-done handler advances the step on its own. -->
        </template>

        <!-- Setup-phase action buttons (unchanged from the original
             single-flow wizard) -->
        <template v-else>
          <ElButton
            v-if="!isLastStep"
            type="primary"
            @click="handleNext"
          >
            Next
          </ElButton>
          <ElButton
            v-else-if="!generatedToken"
            type="primary"
            :loading="submitting"
            @click="handleCompleteSetup"
          >
            {{ isFirstSetup ? 'Complete Setup' : `Create Profile "${profileName}"` }}
          </ElButton>
          <ElButton
            v-else
            type="success"
            :disabled="!configSaved"
            :title="configSaved ? '' : 'Save the configuration first — it contains your JWT and connection details.'"
            @click="handleFinish"
          >
            Start Using Cremind
          </ElButton>
        </template>
      </div>
    </div>

    <ChannelPairingDialog
      v-model="pairingOpen"
      :channel-id="pairingChannelId"
      :channel-label="pairingChannelLabel"
    />
  </div>
</template>

<style scoped>
.setup-wizard {
  width: 100%;
  height: 100vh;
  display: flex;
  align-items: safe center;
  justify-content: safe center;
  background: var(--bg-color);
  overflow-y: auto;
  padding: 24px;
  box-sizing: border-box;
}

.loading-state { color: var(--text-secondary); font-size: 0.95rem; }

.setup-container {
  width: 100%;
  max-width: 720px;
  background: var(--surface-color);
  border-radius: 12px;
  border: 1px solid var(--border-color);
  padding: 32px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
}

.setup-header { text-align: center; margin-bottom: 32px; }
.setup-title { font-size: 1.75rem; font-weight: 700; color: var(--text-primary); margin: 0 0 8px 0; }
.setup-subtitle { font-size: 0.95rem; color: var(--text-secondary); margin: 0; }
.setup-profile-badge {
  margin-top: 12px;
  display: inline-block;
  padding: 4px 10px;
  background: var(--hover-bg);
  border-radius: 999px;
  font-size: 0.78rem;
  color: var(--text-secondary);
}
.setup-profile-badge strong { color: var(--text-primary); }
.setup-steps { margin-bottom: 32px; }
.step-content { min-height: 300px; margin-bottom: 24px; }
.step-actions {
  display: flex; align-items: center; padding-top: 16px;
  border-top: 1px solid var(--border-color);
}
.spacer { flex: 1; }

/* ── Stage transition (between installer + setup wizard) ───────────── */
.stage-transition {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 36px 16px 8px;
  text-align: center;
}
.stage-transition h2 {
  font-size: 1.2rem;
  color: var(--text-primary);
  margin: 0;
}
.stage-transition p {
  margin: 0;
  max-width: 480px;
  color: var(--text-secondary);
  line-height: 1.6;
}
.stage-transition-icon {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: var(--el-color-success-light-9, rgba(103, 194, 58, 0.15));
  position: relative;
}
.stage-transition-icon::after {
  content: '';
  position: absolute;
  top: 18px;
  left: 16px;
  width: 22px;
  height: 11px;
  border-left: 3px solid var(--el-color-success, #67c23a);
  border-bottom: 3px solid var(--el-color-success, #67c23a);
  transform: rotate(-45deg);
}
.stage-transition-log {
  width: 100%;
  max-width: 600px;
  text-align: left;
  margin-top: 4px;
}
.stage-transition-log summary {
  cursor: pointer;
  font-size: 0.85rem;
  color: var(--text-secondary);
  user-select: none;
  margin-bottom: 8px;
}
.stage-transition-log .installer-log-pane--inline {
  height: 200px;
}

/* ── Installer-phase styles (Electron + first-run only) ─────────────── */
.installer-pane h2 {
  font-size: 1.05rem;
  margin: 0 0 12px 0;
  color: var(--text-primary);
}
.installer-pane code {
  background: rgba(127, 127, 127, 0.15);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 0.92em;
}
.installer-pane .installer-hint {
  display: block;
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.45;
  color: var(--text-secondary);
}
.installer-pane .installer-badge {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  font-size: 11px;
  border-radius: 9px;
  background: var(--el-color-success-light-9);
  color: var(--el-color-success);
}
.installer-pane .installer-env-tags {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.installer-pane .restore-link {
  margin-top: 20px;
  font-size: 0.875rem;
  color: var(--text-secondary);
}
.installer-pane .restore-link a {
  color: var(--primary-color);
  cursor: pointer;
  text-decoration: underline;
}
/* Log-pane styling is intentionally not scoped to ``.installer-pane`` —
   the same markup is rendered inside the ``.stage-transition`` block as
   a collapsible "View install log" details element. Scoping it would
   make that second render site lose the dark background and, more
   visibly, the ``display: block`` on log lines (so every entry collapses
   onto a single inline line). */
.installer-log-pane {
  background: #1e1e1e;
  color: #ddd;
  padding: 12px;
  border-radius: 6px;
  height: 280px;
  overflow-y: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  margin: 0;
}
.installer-log-line { display: block; }
.installer-log-stderr { color: #ff8a8a; }
.installer-log-info { color: #80b9ff; }

/* Element Plus radios default to inline-flex laid out horizontally and
   force their label to a single line — multi-line slot content (the
   <strong> + description + hint blocks on the Mode page) collides with
   that layout. Stack them vertically and let labels wrap. */
.installer-pane :deep(.el-radio-group) {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  width: 100%;
  gap: 12px;
}
.installer-pane :deep(.el-radio) {
  display: flex;
  align-items: flex-start;
  width: 100%;
  height: auto;
  margin-right: 0;
  white-space: normal;
}
.installer-pane :deep(.el-radio__input) { margin-top: 3px; }
.installer-pane :deep(.el-radio__label) {
  white-space: normal;
  line-height: 1.5;
}

.setup-log-box {
  margin-top: 16px;
  padding: 12px 16px;
  background: var(--hover-bg);
  border-radius: 8px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  line-height: 1.5;
}

.setup-log-header {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: 8px;
}

.setup-log-header strong {
  color: var(--text-primary);
}

.setup-log-latest {
  font-size: 0.8rem;
  opacity: 0.85;
}

.setup-log-stream {
  margin: 0;
  padding: 8px 10px;
  background: rgba(0, 0, 0, 0.04);
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.75rem;
  line-height: 1.45;
  max-height: 240px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.setup-log-line { display: block; }
.setup-log-line.is-info { color: var(--text-secondary); }
.setup-log-line.is-success { color: #1f8a4a; }
.setup-log-line.is-warning { color: #b48800; }
.setup-log-line.is-error { color: #b03030; }

.embedding-status-box {
  margin-top: 16px;
  padding: 12px 16px;
  background: var(--hover-bg);
  border-radius: 8px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  line-height: 1.5;
}

.embedding-status-box strong {
  display: block;
  color: var(--text-primary);
  margin-bottom: 4px;
}

.embedding-status-box p {
  margin: 4px 0 0 0;
}

.embedding-status-box .hint {
  font-size: 0.78rem;
  opacity: 0.85;
  margin-top: 6px;
}

.embedding-status-box.is-failed {
  border: 1px solid #f5a3a3;
  background: #fff4f4;
  color: #b03030;
}

.embedding-status-box.is-failed strong { color: #b03030; }

.channel-errors-box {
  margin-top: 16px;
  padding: 12px 16px;
  background: #fff4f4;
  border: 1px solid #f5a3a3;
  border-radius: 8px;
  font-size: 0.85rem;
  color: #b03030;
  line-height: 1.5;
}
.channel-errors-box strong { display: block; margin-bottom: 6px; }
.channel-errors-box ul { margin: 4px 0 6px 0; padding-left: 20px; }
.channel-errors-box .hint { margin-top: 6px; font-size: 0.78rem; color: var(--text-secondary); }

.pairing-box {
  margin-top: 16px;
  padding: 12px 16px;
  background: var(--hover-bg);
  border-radius: 8px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  line-height: 1.5;
}
.pairing-box strong { display: block; color: var(--text-primary); margin-bottom: 4px; }
.pairing-box p { margin: 4px 0 8px 0; }
.pairing-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 0;
  border-top: 1px solid var(--border-color);
}
.pairing-row:first-of-type { border-top: none; }
.pairing-name { color: var(--text-primary); font-weight: 500; }
.pairing-name small { color: var(--text-secondary); margin-left: 6px; font-weight: 400; }
</style>
