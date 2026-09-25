<script setup lang="ts">
/**
 * Settings → My Documents: User Document Search for this profile.
 *
 * The profile turns the feature on, picks the folder the agent may search,
 * says what to leave out, and watches the index being built and kept current.
 * Everything here is the caller's own — the server scopes every route to the
 * token's profile, so this page can neither see nor change another profile's
 * folder or index.
 *
 * What the page shows is driven by the live snapshot in the userDocs store,
 * which streams while this page is mounted (`attachPage`). The page adds the
 * saved settings (`GET /settings`) and runs every change:
 *
 * - settings saves and `rebuild` go through the confirm round trip: a change
 *   that would remove indexed content comes back as a plan, shown in
 *   ConfirmPlanDialog, and is repeated with the server's token on confirm;
 * - the engine's own confirmations (first sync, a held mass deletion, a moved
 *   working directory) arrive in the snapshot and open their dialogs once.
 *
 * An admin-disabled gate replaces the page with an explanation (admins get a
 * link to the gate); an engine that failed to start (503) replaces only the
 * live panels, since settings can still be saved.
 *
 * Google Drive is a second source with its own switch (DriveIndexingSection).
 * A profile may use either or both, so "on" for the live panels means the
 * local folder or Drive; the per-profile options (captions, identity, where
 * the agent may use it) stay on the local source's row either way.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton, ElCheckbox, ElDialog, ElMessage, ElMessageBox, ElRadio, ElRadioGroup, ElSwitch,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { useUserDocsStore } from '../stores/userDocs';
import {
  getUserDocsSettings,
  getUserDocsStorage,
  listUserDocsActivity,
  runUserDocsControl,
  saveUserDocsSettings,
  UserDocsApiError,
  type ChangePlan,
  type ConfirmOutcome,
  type ExcludeRule,
  type RootMode,
  type UserDocsControlAction,
  type UserDocsControlRequest,
  type UserDocsControlResult,
  type UserDocsOptions,
  type UserDocsSettings,
  type UserDocsSettingsPatch,
  type UserDocsSettingsSaved,
  type UserDocsSourceKind,
  type UserDocsStorageInfo,
} from '../services/userdocsApi';
import { dockerWarning, formatBytes, formatCount, stateBanner, type BannerActionId } from '../utils/userdocsView';
import DriveIndexingSection from '../components/userdocs/DriveIndexingSection.vue';
import UserDocsStatePanel from '../components/userdocs/UserDocsStatePanel.vue';
import RootFolderPicker from '../components/userdocs/RootFolderPicker.vue';
import ExcludeRulesEditor from '../components/userdocs/ExcludeRulesEditor.vue';
import StorageUsageBar from '../components/userdocs/StorageUsageBar.vue';
import SyncActivityPanel from '../components/userdocs/SyncActivityPanel.vue';
import FileStatusTable from '../components/userdocs/FileStatusTable.vue';
import FirstSyncEstimateDialog from '../components/userdocs/FirstSyncEstimateDialog.vue';
import ConfirmPlanDialog from '../components/userdocs/ConfirmPlanDialog.vue';
import DeletionsConfirmDialog from '../components/userdocs/DeletionsConfirmDialog.vue';
import CaptioningSection from '../components/userdocs/CaptioningSection.vue';
import IdentitySection from '../components/userdocs/IdentitySection.vue';
import ChannelAccessSection from '../components/userdocs/ChannelAccessSection.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();
const store = useUserDocsStore();

const settings = ref<UserDocsSettings | null>(null);
const loading = ref(true);
const loadError = ref('');
/** The engine failed to start on this server (503 from an engine route). */
const engineDown = ref(false);
const storageDetail = ref<UserDocsStorageInfo | null>(null);
/** When the kept index was last updated — for the embedding-off banner. */
const asOf = ref<number | null>(null);

/** One action at a time: every button that sends something disables meanwhile. */
const acting = ref(false);
const savingRoot = ref(false);
const savingExcludes = ref(false);
const savingOptions = ref(false);
const togglingEnabled = ref(false);

const snapshot = computed(() => store.snapshot);
const local = computed(() => settings.value?.local ?? null);
const drive = computed(() => settings.value?.drive ?? null);
const policy = computed(() => settings.value?.policy_view ?? null);
const localEnabled = computed(() => !!local.value?.enabled);
const driveEnabled = computed(() => !!drive.value?.enabled);
/** Either source is on: the live panels (sync, storage, files) apply. */
const enabled = computed(() => localEnabled.value || driveEnabled.value);
const isAdmin = computed(() => !!policy.value?.is_admin);

const banner = computed(() => {
  const snap = snapshot.value;
  if (!snap) return null;
  return stateBanner(snap, settings.value, {
    asOf: asOf.value,
    keptIndexBytes: snap.state === 'disabled' ? storageDetail.value?.total_bytes ?? null : null,
    driveEnabled: driveEnabled.value,
  });
});

/** The documents folder dies with the container (an install from before the
 *  bind mount). Shown under the state banner; it blocks nothing. */
const containerWarning = computed(() => dockerWarning(snapshot.value));

const pausedByUser = computed(() =>
  snapshot.value?.state === 'paused' && snapshot.value.reason === 'user');

const captionCap = computed(() =>
  local.value?.options.caption.daily_cap ?? policy.value?.vision_daily_cap_default ?? null);

/** Images waiting for a description, from the live snapshot. */
const visionWaiting = computed(() => {
  const w = (snapshot.value?.vision as { waiting?: number } | undefined)?.waiting;
  return typeof w === 'number' ? w : null;
});

// ── loading ───────────────────────────────────────────────────────────────

let detachPage: (() => void) | null = null;

async function loadSettings() {
  try {
    const s = await getUserDocsSettings(settingsStore.agentUrl, settingsStore.authToken);
    applySettings(s);
    loadError.value = '';
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Could not load your document search settings.';
  } finally {
    loading.value = false;
  }
}

function applySettings(s: UserDocsSettings) {
  settings.value = s;
  store.setSettings(s);
}

/** Storage numbers for the bar's split, and the kept index's size while the
 *  feature is off. Best effort: before the first index exists the engine
 *  answers NotEnabled, which just means there is nothing to show. */
async function loadStorage() {
  try {
    storageDetail.value = await getUserDocsStorage(settingsStore.agentUrl, settingsStore.authToken);
  } catch (e) {
    storageDetail.value = null;
    if (e instanceof UserDocsApiError && e.code === 'EngineNotRunning') engineDown.value = true;
  }
}

onMounted(async () => {
  detachPage = store.attachPage();
  await loadSettings();
  if (settings.value?.policy_view.allowed) void loadStorage();
});

onBeforeUnmount(() => {
  detachPage?.();
  detachPage = null;
});

// "Keyword search over the index as of <time>": the newest activity entry is
// the last time the index changed.
watch(
  () => (snapshot.value?.state === 'suspended' && snapshot.value.reason === 'embedding_off'),
  async (frozen) => {
    if (!frozen || asOf.value !== null) return;
    try {
      const res = await listUserDocsActivity(settingsStore.agentUrl, settingsStore.authToken, { limit: 1 });
      asOf.value = res.events[0]?.ts ?? null;
    } catch {
      // The banner falls back to "as of when it was turned off".
    }
  },
  { immediate: true },
);

// ── errors ────────────────────────────────────────────────────────────────

function describeError(e: unknown): string {
  if (e instanceof UserDocsApiError) {
    switch (e.code) {
      case 'EngineNotRunning':
        engineDown.value = true;
        return 'The document indexing engine is not running on this server.';
      case 'FeatureDisabledByAdmin':
        void loadSettings();
        return 'An administrator has not allowed User Document Search on this server.';
      case 'EmbeddingDisabled':
        void loadSettings();
        return 'Vector Embedding is off, so document search cannot be turned on right now.';
      case 'NotEnabled':
        return 'Turn document search on first.';
      case 'DriveNotLinked':
        // The link state shown is stale; show it as it is now.
        void loadSettings();
        return e.message;
      case 'DriveFoldersRequired':
        // A whole-Drive account names folders first: open the picker, and
        // saving it turns Drive on.
        driveSection.value?.openPicker('enable');
        return e.message;
      case 'VisionModelChanged':
      case 'VisionNotConfigured':
        // Show the model as it is now before asking again.
        void loadSettings();
        return e.message;
      default:
        return e.message;
    }
  }
  return e instanceof Error ? e.message : String(e);
}

// ── the confirm round trip ────────────────────────────────────────────────

type PendingConfirm<T> = Extract<ConfirmOutcome<T>, { kind: 'confirm' }>;

const planOpen = ref(false);
const planBusy = ref(false);
const planChanged = ref(false);
const planValue = ref<ChangePlan | null>(null);
const planMessage = ref('');
const planTitle = ref('');
const planConfirmLabel = ref('Confirm');
let planConfirm: (() => Promise<void>) | null = null;
let planCancel: (() => void) | null = null;

/**
 * Resolve an outcome to its result, asking the user first when the server
 * wants a confirmation. Resolves to null when the user cancels (or the
 * confirmed request fails — the error is shown).
 */
function resolveConfirm<T>(
  outcome: ConfirmOutcome<T>,
  title: string,
  confirmLabel: string,
): Promise<T | null> {
  if (outcome.kind === 'done') return Promise.resolve(outcome.result);
  return new Promise<T | null>((resolve) => {
    let current: PendingConfirm<T> = outcome;
    const finish = (result: T | null) => {
      planOpen.value = false;
      planConfirm = null;
      planCancel = null;
      resolve(result);
    };
    planValue.value = current.plan;
    planMessage.value = current.message;
    planTitle.value = title;
    planConfirmLabel.value = confirmLabel;
    planChanged.value = false;
    planBusy.value = false;
    planOpen.value = true;
    planConfirm = async () => {
      planBusy.value = true;
      try {
        const next = await current.confirm();
        if (next.kind === 'done') {
          finish(next.result);
        } else {
          // The change moved under the dialog; show the new plan and ask again.
          current = next;
          planValue.value = next.plan;
          planMessage.value = next.message;
          planChanged.value = true;
        }
      } catch (e) {
        ElMessage.error(describeError(e));
        finish(null);
      } finally {
        planBusy.value = false;
      }
    };
    planCancel = () => finish(null);
  });
}

function onPlanOpenChange(open: boolean) {
  if (!open && !planBusy.value) planCancel?.();
}

// ── settings saves ────────────────────────────────────────────────────────

/** Save part of one source's settings; true when it was applied. */
async function saveSource(
  kind: UserDocsSourceKind,
  patch: Omit<UserDocsSettingsPatch, 'kind'>,
  confirmTitle: string,
  confirmLabel: string,
): Promise<boolean> {
  try {
    const outcome = await saveUserDocsSettings(settingsStore.agentUrl, settingsStore.authToken, {
      kind, ...patch,
    });
    const saved: UserDocsSettingsSaved | null = await resolveConfirm(outcome, confirmTitle, confirmLabel);
    if (!saved) return false;
    applySettings(saved.settings);
    store.applySnapshot(saved.snapshot);
    return true;
  } catch (e) {
    ElMessage.error(describeError(e));
    return false;
  }
}

function saveLocal(
  patch: Omit<UserDocsSettingsPatch, 'kind'>,
  confirmTitle: string,
  confirmLabel: string,
): Promise<boolean> {
  return saveSource('local', patch, confirmTitle, confirmLabel);
}

function saveDrive(
  patch: Omit<UserDocsSettingsPatch, 'kind'>,
  confirmTitle: string,
  confirmLabel: string,
): Promise<boolean> {
  return saveSource('drive', patch, confirmTitle, confirmLabel);
}

async function onToggleEnabled(next: string | number | boolean) {
  if (togglingEnabled.value) return;
  if (!next) {
    disableDeleteIndex.value = false;
    disableOpen.value = true;
    return;
  }
  togglingEnabled.value = true;
  try {
    if (await saveLocal({ enabled: true }, 'Turn on document search?', 'Turn on')) {
      ElMessage.success('Document search is on. Cremind is looking through your folder.');
      void loadStorage();
    }
  } finally {
    togglingEnabled.value = false;
  }
}

// Turning it off asks what happens to the index: kept (re-enabling only
// catches up with changes) or deleted (through the confirm flow, which shows
// what would go).
const disableOpen = ref(false);
const disableDeleteIndex = ref(false);

async function confirmDisable() {
  togglingEnabled.value = true;
  try {
    const deleting = disableDeleteIndex.value;
    const ok = await saveLocal(
      deleting ? { enabled: false, delete_index: true } : { enabled: false },
      'Delete your document index?',
      'Delete index',
    );
    if (ok) {
      disableOpen.value = false;
      ElMessage.success(deleting ? 'Document search is off and its index is being deleted.' : 'Document search is off. Your index is kept.');
      void loadStorage();
    }
  } finally {
    togglingEnabled.value = false;
  }
}

async function onSaveRoot(choice: { root_mode: RootMode; root_path: string | null }) {
  savingRoot.value = true;
  try {
    const ok = await saveLocal(
      choice.root_mode === 'inherit' ? { root_mode: 'inherit' } : choice,
      'Change the indexed folder?',
      'Change folder',
    );
    if (ok) ElMessage.success('Folder saved.');
  } finally {
    savingRoot.value = false;
  }
}

async function onSaveExcludes(rules: ExcludeRule[]) {
  savingExcludes.value = true;
  try {
    const ok = await saveLocal({ excludes: rules }, 'Apply these exclusions?', 'Apply');
    if (ok) ElMessage.success('Exclusions saved.');
  } finally {
    savingExcludes.value = false;
  }
}

// Captions, identity and channel access are plain option saves: none of them
// removes indexed content, so none needs a confirmation.
async function saveOptions(options: UserDocsSettingsPatch['options'], done: string) {
  savingOptions.value = true;
  try {
    if (await saveLocal({ options }, 'Save?', 'Save')) ElMessage.success(done);
  } finally {
    savingOptions.value = false;
  }
}

function onSaveCaption(patch: Partial<UserDocsOptions['caption']>) {
  const msg = patch.enabled === undefined
    ? 'Limits saved.'
    : patch.enabled ? 'Image descriptions are on.' : 'Image descriptions are off. Existing descriptions stay searchable.';
  void saveOptions({ caption: patch }, msg);
}

function onSaveIdentity(identity: UserDocsOptions['identity']) {
  void saveOptions({ identity }, 'Saved.');
}

function onSaveAllowIn(patch: Partial<UserDocsOptions['allow_in']>) {
  void saveOptions({ allow_in: patch }, 'Saved. It applies from the next message.');
}

async function onConsentVision(model: string) {
  try {
    await ElMessageBox.confirm(
      `Your photos and scanned pages will be sent to ${model} to describe what they show. Descriptions `
        + 'are stored in your index; a copy of the same image is never sent twice. You can stop at any time.',
      'Allow describing your images?',
      { confirmButtonText: `Allow ${model}`, cancelButtonText: 'Cancel' },
    );
  } catch {
    return;
  }
  if (await control('consent_vision', { model })) {
    ElMessage.success('Allowed. Waiting images are being described.');
    await loadSettings();
  }
}

async function onRevokeVision() {
  if (await control('revoke_vision_consent')) {
    ElMessage.success('Stopped. Existing descriptions stay searchable.');
    await loadSettings();
  }
}

function goToLlmSettings() {
  void router.push({ name: 'llm-settings', params: { profile: props.profile } });
}

// ── Google Drive ──────────────────────────────────────────────────────────

const driveSection = ref<InstanceType<typeof DriveIndexingSection> | null>(null);

/**
 * One Drive change through the confirm flow. Turning Drive off always
 * removes its index, and narrowing its folders removes the files outside
 * them, so the server answers both with a plan first; turning it on for a
 * whole-Drive account without folders is refused (DriveFoldersRequired).
 */
async function applyDrive(change: { enabled?: boolean; include_folders?: string[] }): Promise<boolean> {
  const patch: Omit<UserDocsSettingsPatch, 'kind'> = {};
  if (change.enabled !== undefined) patch.enabled = change.enabled;
  if (change.include_folders !== undefined) patch.options = { include_folders: change.include_folders };
  const turningOff = change.enabled === false;
  const turningOn = change.enabled === true;
  const ok = await saveDrive(
    patch,
    turningOff ? 'Turn off Google Drive search?' : turningOn ? 'Turn on Google Drive search?' : 'Change the Drive folders?',
    turningOff ? 'Turn off and remove' : turningOn ? 'Turn on' : 'Change folders',
  );
  if (ok) {
    ElMessage.success(turningOff
      ? 'Google Drive search is off. Its index is being removed.'
      : turningOn
        ? 'Google Drive search is on. Cremind is listing your Drive.'
        : 'Drive folders saved.');
    fileTableKey.value += 1;
  }
  return ok;
}

async function syncDrive(full: boolean) {
  if (await control(full ? 'rescan' : 'sync_now', { source: 'drive' })) {
    ElMessage.success(full ? 'Checking every Drive file.' : 'Checking Google Drive for changes.');
  }
}

async function removeDriveDeletions() {
  if (await control('confirm_deletions', { source: 'drive' })) {
    ElMessage.success('The vanished Drive files were removed from the index.');
    fileTableKey.value += 1;
  }
}

async function keepDriveDeletions() {
  if (await control('reject_deletions', { source: 'drive' })) {
    ElMessage.success('Kept. Cremind checks them again on the next full sync.');
  }
}

function goToGsuiteSettings() {
  void router.push({ name: 'gsuite-settings', params: { profile: props.profile } });
}

// ── engine actions ────────────────────────────────────────────────────────

const fileTableKey = ref(0);
const fileTable = ref<InstanceType<typeof FileStatusTable> | null>(null);

/** Run one control action; the returned snapshot is folded into the store. */
async function control(
  action: UserDocsControlAction,
  extra: Omit<UserDocsControlRequest, 'action'> = {},
  confirmText: { title: string; label: string } | null = null,
): Promise<UserDocsControlResult | null> {
  if (acting.value) return null;
  acting.value = true;
  try {
    const outcome = await runUserDocsControl(settingsStore.agentUrl, settingsStore.authToken, {
      action, ...extra,
    });
    const res = await resolveConfirm(outcome, confirmText?.title ?? 'Confirm', confirmText?.label ?? 'Confirm');
    if (res) {
      engineDown.value = false;
      store.applySnapshot(res.snapshot);
    }
    return res;
  } catch (e) {
    ElMessage.error(describeError(e));
    return null;
  } finally {
    acting.value = false;
  }
}

async function retry(targets: string[]) {
  const res = await control('retry_failed', { targets });
  if (res) {
    ElMessage.success(`Retrying ${formatCount(res.files ?? targets.length)} file${(res.files ?? targets.length) === 1 ? '' : 's'}.`);
    fileTableKey.value += 1;
  }
}

async function retryAll() {
  const res = await control('retry_failed');
  if (res) {
    ElMessage.success(res.files ? `Retrying ${formatCount(res.files)} files.` : 'No failed files to retry.');
    fileTableKey.value += 1;
  }
}

async function reindex(targets: string[], source?: UserDocsSourceKind) {
  const res = await control('reindex', source === 'drive' ? { targets, source } : { targets });
  if (res) {
    ElMessage.success(`Reindexing ${formatCount(res.files ?? targets.length)} file${(res.files ?? targets.length) === 1 ? '' : 's'}.`);
    fileTableKey.value += 1;
  }
}

// The file list is not refetched per frame (a first sync finishes dozens of
// files a second); it catches up whenever the sync changes state — scan to
// indexing, indexing to idle, a confirmation answered.
watch(() => snapshot.value?.state, (now, prev) => {
  if (prev && now && now !== prev && enabled.value) fileTableKey.value += 1;
});

async function rescan() {
  if (await control('rescan')) ElMessage.success('Checking your folder for changes.');
}

async function togglePause() {
  if (pausedByUser.value) {
    if (await control('resume')) ElMessage.success('Syncing resumed.');
  } else if (await control('pause')) {
    ElMessage.success('Syncing paused. Search keeps working.');
  }
}

const rebuildOpen = ref(false);
const rebuildReextract = ref(false);

async function confirmRebuild() {
  rebuildOpen.value = false;
  const res = await control(
    'rebuild',
    { reextract: rebuildReextract.value },
    { title: 'Rebuild the index?', label: 'Rebuild' },
  );
  if (res) {
    ElMessage.success('Rebuilding the index in the background. Search keeps working.');
    fileTableKey.value += 1;
  }
}

async function confirmRootChange() {
  const conf = snapshot.value?.confirmation;
  const detail = snapshot.value?.detail ?? {};
  const from = conf?.kind === 'root_change' ? conf.from : detail.from;
  const to = conf?.kind === 'root_change' ? conf.to : detail.to;
  const files = conf?.kind === 'root_change' ? conf.files : 0;
  try {
    await ElMessageBox.confirm(
      `Index ${to ?? 'the new working directory'} instead of ${from ?? 'the previous folder'}? `
        + (files ? `Of the ${formatCount(files)} indexed files, those inside the new folder keep their index; the rest are removed from it. ` : '')
        + 'Your files themselves are not touched.',
      'Use the new folder?',
      { confirmButtonText: 'Use the new folder', cancelButtonText: 'Not now' },
    );
  } catch {
    return;
  }
  if (await control('confirm_root_change')) ElMessage.success('Now indexing the new folder.');
}

// ── the engine's confirmations ────────────────────────────────────────────

const firstSyncOpen = ref(false);
const deletionsOpen = ref(false);
// Each confirmation opens its dialog once by itself; after that the banner's
// button reopens it.
const seenConfirmations = new Set<string>();

const firstSyncEstimate = computed(() => {
  const conf = snapshot.value?.confirmation;
  return conf?.kind === 'first_sync' ? conf.estimate : null;
});
const massDelete = computed(() => {
  const conf = snapshot.value?.confirmation;
  return conf?.kind === 'mass_delete' ? conf : null;
});

watch(() => snapshot.value?.confirmation, (conf) => {
  if (!conf) {
    firstSyncOpen.value = false;
    deletionsOpen.value = false;
    return;
  }
  const key = conf.kind === 'first_sync'
    ? `first_sync:${conf.estimate?.finished_at ?? conf.estimate?.root ?? ''}`
    : conf.kind === 'mass_delete'
      ? `mass_delete:${conf.missing}/${conf.total}`
      : `root_change:${conf.to ?? ''}`;
  if (seenConfirmations.has(key)) return;
  seenConfirmations.add(key);
  if (conf.kind === 'first_sync') firstSyncOpen.value = true;
  else if (conf.kind === 'mass_delete') deletionsOpen.value = true;
}, { immediate: true });

async function startFirstSync() {
  if (await control('start')) {
    firstSyncOpen.value = false;
    ElMessage.success('First sync started. Search works as soon as the first files are in.');
  }
}

function adjustFromEstimate() {
  firstSyncOpen.value = false;
  scrollTo(excludesSection.value);
}

async function turnOffFromEstimate() {
  togglingEnabled.value = true;
  try {
    if (await saveLocal({ enabled: false }, 'Turn off document search?', 'Turn off')) {
      firstSyncOpen.value = false;
    }
  } finally {
    togglingEnabled.value = false;
  }
}

async function removeDeletions() {
  const res = await control('confirm_deletions');
  if (res) {
    deletionsOpen.value = false;
    ElMessage.success('The vanished files were removed from the index.');
    fileTableKey.value += 1;
  }
}

async function keepDeletions() {
  const res = await control('reject_deletions');
  if (res) {
    deletionsOpen.value = false;
    ElMessage.success('Kept. They stay hidden and come back if the files return.');
  }
}

// ── banner actions ────────────────────────────────────────────────────────

const folderSection = ref<HTMLElement | null>(null);
const excludesSection = ref<HTMLElement | null>(null);

function scrollTo(el: HTMLElement | null) {
  el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function goToEmbeddingSettings() {
  void router.push({ name: 'embedding-settings', params: { profile: props.profile } });
}

function onBannerAction(id: BannerActionId) {
  switch (id) {
    case 'enable': void onToggleEnabled(true); break;
    case 'open_admin': goToEmbeddingSettings(); break;
    case 'choose_folder': scrollTo(folderSection.value); break;
    case 'adjust_excludes': scrollTo(excludesSection.value); break;
    case 'rescan': void rescan(); break;
    case 'confirm_root_change': void confirmRootChange(); break;
    case 'review_first_sync': firstSyncOpen.value = true; break;
    case 'review_deletions': deletionsOpen.value = true; break;
    case 'resume':
    case 'pause': void togglePause(); break;
    case 'retry_failed': void retryAll(); break;
    case 'relink_google': goToGsuiteSettings(); break;
  }
}

function showFailed() {
  fileTable.value?.showStatus('error');
}

function onEngineDown() {
  engineDown.value = true;
}

async function retryEngine() {
  engineDown.value = false;
  await store.refresh();
  void loadStorage();
}

function goBack() {
  void router.push(`/${props.profile}/settings`);
}
</script>

<template>
  <div class="ud-page">
    <div class="ud-container">
      <div class="ud-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" /> Back to Settings
        </button>
        <h1 class="ud-title">My Documents</h1>
        <p class="ud-subtitle">
          Let the agent search your own files and answer with citations you can open. Cremind keeps
          a private index of the folder you choose (and of your Google Drive, if you turn it on) and
          updates it as files change — only this profile can search it, and your files are never
          modified.
        </p>
      </div>

      <div v-if="loading" class="ud-muted">Loading…</div>

      <section v-else-if="loadError" class="ud-card ud-problem">
        <Icon icon="mdi:alert-circle-outline" class="ud-problem-icon" />
        <div>
          <h2>Couldn't load your document search settings</h2>
          <p>{{ loadError }}</p>
          <ElButton size="small" @click="loadSettings">Try again</ElButton>
        </div>
      </section>

      <template v-else-if="settings && local && policy">
        <!-- The admin has not allowed the feature: nothing here can be used. -->
        <section v-if="!policy.allowed" class="ud-card ud-problem">
          <Icon icon="mdi:shield-lock-outline" class="ud-problem-icon" />
          <div>
            <h2>Not available on this server</h2>
            <p v-if="isAdmin">
              User Document Search is turned off for every profile. Allow it on the Vector
              Embedding page, then come back here to choose a folder.
            </p>
            <p v-else>
              An administrator has to allow User Document Search before you can use it. Ask your
              administrator if you need it.
            </p>
            <p v-if="enabled">
              Your index is kept, and syncing picks up where it left off once it is allowed again.
            </p>
            <ElButton v-if="isAdmin" size="small" type="primary" @click="goToEmbeddingSettings">
              Open Vector Embedding settings
            </ElButton>
          </div>
        </section>

        <template v-else>
          <UserDocsStatePanel :banner="banner" :busy="acting || togglingEnabled" @action="onBannerAction" />
          <UserDocsStatePanel v-if="containerWarning" :banner="containerWarning" />

          <!-- On / off -->
          <section class="ud-card">
            <div class="ud-toggle-row">
              <div>
                <h2>Search my documents</h2>
                <p class="ud-muted">
                  The agent can find, read and cite files from your folder — in the web app and the CLI.
                </p>
              </div>
              <ElSwitch
                :model-value="localEnabled"
                :loading="togglingEnabled"
                :disabled="!localEnabled && !policy.effective"
                aria-label="Search my documents"
                @update:model-value="onToggleEnabled"
              />
            </div>
            <p v-if="!localEnabled && policy.reason === 'embedding_disabled'" class="ud-note">
              <Icon icon="mdi:information-outline" />
              <span>
                Vector Embedding is off on this server, so document search can't be turned on right now.
                <template v-if="isAdmin">
                  <a href="#" @click.prevent="goToEmbeddingSettings">Turn Vector Embedding on</a>.
                </template>
                <template v-else>Ask your administrator.</template>
              </span>
            </p>
          </section>

          <!-- Folder -->
          <section ref="folderSection" class="ud-card">
            <h2>Folder</h2>
            <p class="ud-muted">
              Everything inside it is indexed, subfolders included, except what you exclude below.
              <template v-if="!localEnabled">Choose it before turning search on, if you like.</template>
            </p>
            <RootFolderPicker
              :source="local"
              :policy="policy"
              :saving="savingRoot"
              :disabled="savingRoot"
              @save="onSaveRoot"
            />
          </section>

          <!-- Exclusions -->
          <section ref="excludesSection" class="ud-card">
            <h2>Exclusions</h2>
            <ExcludeRulesEditor
              :rules="local.excludes"
              :saving="savingExcludes"
              :disabled="savingExcludes"
              @save="onSaveExcludes"
            />
          </section>

          <!-- Google Drive: a second source, on its own switch -->
          <section class="ud-card">
            <DriveIndexingSection
              ref="driveSection"
              :source="drive"
              :link="settings.drive_link"
              :snapshot="snapshot"
              :can-enable="policy.effective"
              :save="applyDrive"
              :busy="acting"
              @sync="syncDrive"
              @confirm-deletions="removeDriveDeletions"
              @reject-deletions="keepDriveDeletions"
              @open-gsuite="goToGsuiteSettings"
            />
          </section>

          <!-- Photos and scans: the Specialized Vision Model -->
          <section class="ud-card">
            <h2>Photos and scanned pages</h2>
            <CaptioningSection
              :caption="local.options.caption"
              :vision="settings.vision"
              :default-cap="policy.vision_daily_cap_default"
              :waiting="visionWaiting"
              :saving="savingOptions"
              :busy="acting"
              @save="onSaveCaption"
              @consent="onConsentVision"
              @revoke="onRevokeVision"
              @open-llm="goToLlmSettings"
            />
          </section>

          <!-- Who "me" is -->
          <section class="ud-card">
            <h2>You</h2>
            <p class="ud-muted">So "the report I wrote" and "photos I took" find your own files first.</p>
            <IdentitySection
              :identity="local.options.identity"
              :saving="savingOptions"
              @save="onSaveIdentity"
            />
          </section>

          <!-- Where the agent may use it -->
          <section class="ud-card">
            <h2>Where the agent may use your documents</h2>
            <ChannelAccessSection
              :allow-in="local.options.allow_in"
              :saving="savingOptions"
              @save="onSaveAllowIn"
            />
          </section>

          <template v-if="enabled">
            <section v-if="engineDown" class="ud-card ud-problem">
              <Icon icon="mdi:cog-sync-outline" class="ud-problem-icon" />
              <div>
                <h2>The indexing engine isn't running</h2>
                <p>
                  It did not start with the server, so nothing is being indexed and progress can't be
                  shown. Your settings above can still be changed.
                  <template v-if="isAdmin">Check the server log (logs/app.log) and restart Cremind.</template>
                  <template v-else>Let your administrator know.</template>
                </p>
                <ElButton size="small" @click="retryEngine">Check again</ElButton>
              </div>
            </section>

            <template v-else>
              <!-- Progress and actions -->
              <section class="ud-card">
                <div class="ud-card-head">
                  <h2>Sync</h2>
                  <div class="ud-actions">
                    <ElButton size="small" :disabled="acting" @click="togglePause">
                      <Icon :icon="pausedByUser ? 'mdi:play' : 'mdi:pause'" class="btn-icon" />
                      {{ pausedByUser ? 'Resume' : 'Pause' }}
                    </ElButton>
                    <ElButton v-if="localEnabled" size="small" :disabled="acting" @click="rescan">
                      <Icon icon="mdi:refresh" class="btn-icon" /> Rescan
                    </ElButton>
                    <ElButton size="small" :disabled="acting" @click="rebuildReextract = false; rebuildOpen = true">
                      <Icon icon="mdi:database-refresh-outline" class="btn-icon" /> Rebuild…
                    </ElButton>
                  </div>
                </div>
                <SyncActivityPanel
                  :snapshot="snapshot"
                  :busy="acting"
                  @retry="retry"
                  @retry-all="retryAll"
                  @show-failed="showFailed"
                  @engine-down="onEngineDown"
                />
              </section>

              <section class="ud-card">
                <h2>Storage</h2>
                <StorageUsageBar :storage="snapshot?.storage" :detail="storageDetail" />
              </section>

              <section class="ud-card">
                <h2>Indexed files</h2>
                <FileStatusTable
                  ref="fileTable"
                  :refresh-key="fileTableKey"
                  :busy="acting"
                  :show-source="driveEnabled"
                  @reindex="reindex"
                  @engine-down="onEngineDown"
                />
              </section>
            </template>
          </template>
        </template>
      </template>
    </div>

    <!-- Turn off: keep or delete the index -->
    <ElDialog v-model="disableOpen" title="Turn off document search?" width="480px" append-to-body>
      <p v-if="driveEnabled" class="ud-dialog-text">
        This turns off your folder only. Google Drive search stays on, with its own index.
      </p>
      <ElRadioGroup v-model="disableDeleteIndex" class="ud-disable-options">
        <ElRadio :value="false">
          <strong>Keep the index</strong>
          <span class="ud-radio-sub">
            The agent stops using it. Turning search back on only catches up with what changed.
            <template v-if="storageDetail?.total_bytes">It takes {{ formatBytes(storageDetail.total_bytes) }}.</template>
          </span>
        </ElRadio>
        <ElRadio :value="true">
          <strong>Delete the index</strong>
          <span class="ud-radio-sub">
            Frees the space. Turning search back on reads every file again. Your files are not touched.
          </span>
        </ElRadio>
      </ElRadioGroup>
      <template #footer>
        <ElButton :disabled="togglingEnabled" @click="disableOpen = false">Cancel</ElButton>
        <ElButton type="primary" :loading="togglingEnabled" @click="confirmDisable">Turn off</ElButton>
      </template>
    </ElDialog>

    <!-- Rebuild -->
    <ElDialog v-model="rebuildOpen" title="Rebuild the index" width="480px" append-to-body>
      <p class="ud-dialog-text">
        Re-embeds every indexed passage from the text already stored — use it if search results look
        wrong. It runs in the background and search keeps working.
      </p>
      <ElCheckbox v-model="rebuildReextract">
        Also read every file again (slower — for files that were read incorrectly)
      </ElCheckbox>
      <template #footer>
        <ElButton @click="rebuildOpen = false">Cancel</ElButton>
        <ElButton type="primary" @click="confirmRebuild">Continue</ElButton>
      </template>
    </ElDialog>

    <ConfirmPlanDialog
      :model-value="planOpen"
      :plan="planValue"
      :title="planTitle"
      :message="planMessage"
      :confirm-label="planConfirmLabel"
      :busy="planBusy"
      :changed="planChanged"
      @update:model-value="onPlanOpenChange"
      @confirm="planConfirm?.()"
    />

    <FirstSyncEstimateDialog
      v-model="firstSyncOpen"
      :estimate="firstSyncEstimate"
      :storage="snapshot?.storage"
      :caption-cap="captionCap"
      :caption-enabled="local?.options.caption.enabled ?? true"
      :busy="acting || togglingEnabled"
      @start="startFirstSync"
      @adjust="adjustFromEstimate"
      @turn-off="turnOffFromEstimate"
    />

    <DeletionsConfirmDialog
      v-model="deletionsOpen"
      :missing="massDelete?.missing ?? 0"
      :total="massDelete?.total ?? 0"
      :root="snapshot?.sources?.local?.root ?? local?.root_path ?? null"
      :busy="acting"
      @remove="removeDeletions"
      @keep="keepDeletions"
    />
  </div>
</template>

<style scoped>
.ud-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.ud-container { max-width: 860px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }
.ud-header { margin-bottom: 10px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none; border: none;
  color: var(--text-secondary); cursor: pointer; font-size: 0.875rem;
  padding: 4px 0; margin-bottom: 16px;
}
.back-btn:hover { color: var(--primary-color); }
.ud-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px; }
.ud-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; max-width: 680px; line-height: 1.5; }

.ud-card {
  background: var(--surface-color); border: 1px solid var(--border-color);
  border-radius: 10px; padding: 16px 18px;
}
.ud-card h2 { font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 6px; }
.ud-card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.ud-card-head h2 { margin: 0; }
.ud-muted { margin: 0 0 12px; font-size: 0.85rem; color: var(--text-secondary); line-height: 1.5; }
.ud-toggle-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.ud-toggle-row .ud-muted { margin: 0; }
.ud-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.ud-actions :deep(.el-button + .el-button) { margin-left: 0; }
.btn-icon { margin-right: 4px; }

.ud-note {
  display: flex; gap: 8px; align-items: flex-start; margin: 12px 0 0;
  font-size: 0.82rem; color: var(--text-secondary); line-height: 1.45;
}
.ud-note :deep(svg) { flex: none; margin-top: 2px; color: var(--warning-color); }
.ud-note a { color: var(--primary-color); }

.ud-problem { display: flex; gap: 14px; align-items: flex-start; }
.ud-problem-icon { font-size: 26px; color: var(--warning-color); flex: none; margin-top: 2px; }
.ud-problem p { margin: 0 0 10px; font-size: 0.875rem; color: var(--text-secondary); line-height: 1.5; }

.ud-disable-options { display: flex; flex-direction: column; align-items: stretch; gap: 12px; }
.ud-disable-options :deep(.el-radio) {
  height: auto; align-items: flex-start; white-space: normal; margin-right: 0;
}
.ud-disable-options :deep(.el-radio__label) { display: flex; flex-direction: column; gap: 2px; }
.ud-radio-sub { font-weight: 400; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45; }
.ud-dialog-text { margin: 0 0 12px; font-size: 0.875rem; color: var(--text-secondary); line-height: 1.5; }
</style>
