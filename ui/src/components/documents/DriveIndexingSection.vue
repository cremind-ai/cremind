<script setup lang="ts">
/**
 * Google Drive as a second source for My Documents: the linked account, the
 * switch, the folders to index, and how the Drive sync is doing.
 *
 * What gets indexed is what the linked Google account lets Cremind see. A
 * per-file account (the `drive.file` scope) covers the files it was granted
 * and the files Cremind created; folders optionally narrow that. A whole-Drive
 * account must choose folders first, so switching Drive on opens the folder
 * picker before anything is sent.
 *
 * Drive has its own state beside the local folder's (`snapshot.drive`), so its
 * holds and its held mass removal are shown here, worded by `driveBanner`.
 * Turning Drive off always removes its index; the page sends every change
 * through its confirm flow (`save`), which shows the plan before anything goes.
 * Sync and the deletion answers are emitted for the page to run, like every
 * other control action.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElButton, ElDialog, ElSwitch } from 'element-plus';
import { Icon } from '@iconify/vue';
import type {
  DriveFolder,
  DocumentsDriveLink,
  DocumentsSnapshot,
  DocumentsSource,
} from '../../services/documentsApi';
import {
  driveAccessText,
  driveBanner,
  driveCountsText,
  driveEnableBlocker,
  driveStatus,
  formatAgo,
  formatCount,
  namedFolders,
  type BannerActionId,
} from '../../utils/documentsView';
import DocumentsStatePanel from './DocumentsStatePanel.vue';
import DriveFolderPicker from './DriveFolderPicker.vue';

/** One Drive settings change; the page turns it into a `kind: 'drive'` PUT. */
interface DriveChange {
  enabled?: boolean;
  include_folders?: string[];
}

const props = withDefaults(defineProps<{
  /** The saved Drive source (`settings.drive`). */
  source: DocumentsSource | null;
  link: DocumentsDriveLink | null;
  snapshot: DocumentsSnapshot | null;
  /** Vector Embedding is on and the admin allows the feature. */
  canEnable?: boolean;
  /** Save a change through the page's confirm flow; true once applied. */
  save: (change: DriveChange) => Promise<boolean>;
  /** A control action is in flight. */
  busy?: boolean;
}>(), { canEnable: true, busy: false });

const emit = defineEmits<{
  sync: [full: boolean];
  'confirm-deletions': [];
  'reject-deletions': [];
  'open-gsuite': [];
}>();

const saving = ref(false);

const enabled = computed(() => !!props.source?.enabled);
const linked = computed(() => !!props.link?.linked);
// The live snapshot's view; before the first frame, the one the settings
// read came with.
const view = computed(() => props.snapshot?.drive ?? props.link?.index ?? null);
const wholeDrive = computed(() => !!(props.link?.whole_drive ?? view.value?.whole_drive));
const savedIds = computed(() => props.source?.options.include_folders ?? []);

/** Names the picker learned, for folders the server reports by id only. */
const knownNames = ref<Record<string, string>>({});
const folders = computed(() => namedFolders(savedIds.value, view.value?.include_folders, knownNames.value));

const banner = computed(() => (view.value
  ? driveBanner({ ...(props.snapshot ?? { v: 1, enabled: true, state: 'unknown', reason: null }), drive: view.value })
  : null));
const status = computed(() => driveStatus(view.value));
const countsText = computed(() => driveCountsText(view.value));
const blocker = computed(() => driveEnableBlocker(props.link, savedIds.value));

const statusIcon = computed(() => {
  switch (status.value?.tone) {
    case 'success': return 'mdi:check-circle-outline';
    case 'warning': return 'mdi:alert-outline';
    case 'error': return 'mdi:alert-circle-outline';
    default: return 'mdi:information-outline';
  }
});

/** The index was built from another account than the one linked now; the
 *  next sync notices and re-indexes. */
const accountChanged = computed(() => {
  const indexed = view.value?.account_email || props.link?.identity_email;
  const current = props.link?.email;
  return !!(enabled.value && indexed && current && indexed.toLowerCase() !== current.toLowerCase());
});

// "12 min ago" has to move on while the page stays open.
const now = ref(Date.now());
let clock: ReturnType<typeof setInterval> | null = null;
onMounted(() => { clock = setInterval(() => { now.value = Date.now(); }, 30_000); });
onBeforeUnmount(() => { if (clock !== null) clearInterval(clock); });
const lastSync = computed(() => formatAgo(view.value?.last_sync_at, now.value));
const lastFull = computed(() => formatAgo(view.value?.last_full_at, now.value));

async function apply(change: DriveChange): Promise<boolean> {
  if (saving.value) return false;
  saving.value = true;
  try {
    return await props.save(change);
  } finally {
    saving.value = false;
  }
}

// ── on / off ──────────────────────────────────────────────────────────────

const switchDisabled = computed(() =>
  saving.value || (!enabled.value && (!linked.value || !props.canEnable)));

function onToggle(next: string | number | boolean) {
  if (!next) {
    void apply({ enabled: false });
    return;
  }
  if (blocker.value === 'not_linked') return;
  if (blocker.value === 'folders_required') {
    openPicker('enable');
    return;
  }
  void apply({ enabled: true });
}

// ── folders ───────────────────────────────────────────────────────────────

const pickerOpen = ref(false);
const pickerMode = ref<'enable' | 'edit'>('edit');

/**
 * Open the folder picker. In `enable` mode saving it also turns Drive on (the
 * switch, or the page after the server answered DriveFoldersRequired); in
 * `edit` mode it only saves the folders, whether Drive is on or not.
 */
function openPicker(mode: 'enable' | 'edit' = 'edit') {
  pickerMode.value = mode;
  pickerOpen.value = true;
}

async function onPickerSave(chosen: DriveFolder[]) {
  const names = { ...knownNames.value };
  for (const f of chosen) names[f.id] = f.name;
  knownNames.value = names;
  const ids = chosen.map(f => f.id);
  const ok = await apply(pickerMode.value === 'enable' && !enabled.value
    ? { enabled: true, include_folders: ids }
    : { include_folders: ids });
  if (ok) pickerOpen.value = false;
}

function clearFolders() {
  void apply({ include_folders: [] });
}

// ── the held mass removal ─────────────────────────────────────────────────

const deletionsOpen = ref(false);
const massDelete = computed(() => {
  const conf = view.value?.confirmation;
  return conf?.kind === 'mass_delete' ? conf : null;
});
// Opens by itself once per held removal; the banner's Review reopens it.
const seen = new Set<string>();
watch(massDelete, (conf) => {
  if (!conf) {
    deletionsOpen.value = false;
    return;
  }
  const key = `${conf.missing}/${conf.total}`;
  if (seen.has(key)) return;
  seen.add(key);
  deletionsOpen.value = true;
}, { immediate: true });

function onBannerAction(id: BannerActionId) {
  if (id === 'relink_google') emit('open-gsuite');
  else if (id === 'review_deletions') deletionsOpen.value = true;
}

defineExpose({ openPicker });
</script>

<template>
  <div class="drive">
    <div class="drive-head">
      <div class="drive-head-main">
        <h2 class="drive-title"><Icon icon="mdi:google-drive" class="drive-title-icon" /> Google Drive</h2>
        <p class="drive-muted">
          Let the agent search your Google Drive files too. Cremind only reads them — nothing in your
          Drive is ever changed.
        </p>
      </div>
      <ElSwitch
        :model-value="enabled"
        :loading="saving"
        :disabled="switchDisabled"
        aria-label="Search my Google Drive"
        @update:model-value="onToggle"
      />
    </div>

    <!-- The linked account -->
    <div v-if="!linked" class="drive-row">
      <Icon icon="mdi:link-off" class="drive-row-icon" />
      <div class="drive-row-main">
        <strong>Google Drive isn't linked</strong>
        <p>
          Link your Google account in Settings → GSuite (or ask the agent to link the gdrive skill),
          then turn Drive search on here.
        </p>
        <ElButton size="small" @click="emit('open-gsuite')">
          <Icon icon="mdi:open-in-new" class="btn-icon" /> GSuite settings
        </ElButton>
      </div>
    </div>
    <div v-else class="drive-row">
      <Icon icon="mdi:account-circle-outline" class="drive-row-icon" />
      <div class="drive-row-main">
        <strong>{{ link?.email || 'Linked' }}</strong>
        <p>{{ driveAccessText(link) }}</p>
        <p v-if="link?.scopes_stale" class="drive-attn">
          <Icon icon="mdi:alert-outline" />
          <span>
            Google access for Drive is out of date.
            <a href="#" @click.prevent="emit('open-gsuite')">Re-link in Settings → GSuite</a>.
          </span>
        </p>
        <p v-if="accountChanged" class="drive-attn">
          <Icon icon="mdi:account-switch-outline" />
          <span>
            The Drive index was built from {{ view?.account_email || link?.identity_email }}. It is
            rebuilt for {{ link?.email }} on the next sync.
          </span>
        </p>
      </div>
    </div>

    <DocumentsStatePanel v-if="banner" :banner="banner" :busy="busy || saving" @action="onBannerAction" />

    <!-- Folders -->
    <div v-if="linked || folders.length" class="drive-block">
      <div class="drive-block-head">
        <strong>{{ wholeDrive ? 'Folders to index' : 'Folders' }}</strong>
        <span v-if="wholeDrive" class="drive-tag">Required</span>
      </div>
      <p class="drive-hint">
        <template v-if="wholeDrive">
          This account can see your whole Drive, so only the folders you choose are indexed — with
          everything inside them.
        </template>
        <template v-else-if="folders.length">
          Only the granted files inside these folders are indexed.
        </template>
        <template v-else>
          Every file you granted Cremind is indexed. Choose folders to index only what is inside them.
        </template>
      </p>
      <ul v-if="folders.length" class="drive-folders">
        <li v-for="f in folders" :key="f.id" :title="f.id">
          <Icon icon="mdi:folder-google-drive" /> <span>{{ f.name }}</span>
        </li>
      </ul>
      <div class="drive-actions">
        <ElButton size="small" :disabled="!linked || saving" @click="openPicker()">
          <Icon icon="mdi:folder-edit-outline" class="btn-icon" />
          {{ folders.length ? 'Change folders…' : wholeDrive ? 'Choose folders…' : 'Limit to folders…' }}
        </ElButton>
        <ElButton v-if="!wholeDrive && folders.length" size="small" text :disabled="saving" @click="clearFolders">
          Index everything granted
        </ElButton>
      </div>
    </div>

    <!-- Sync -->
    <div v-if="enabled && status" class="drive-block">
      <div class="drive-status">
        <span class="drive-chip" :class="`tone-${status.tone}`">
          <Icon v-if="status.busy" icon="mdi:sync" class="spin" />
          <Icon v-else :icon="statusIcon" />
          {{ status.label }}
        </span>
        <span class="drive-hint">{{ countsText }}</span>
      </div>
      <p class="drive-hint">
        Checked for changes {{ lastSync || 'not yet' }}<template v-if="lastFull"> · every file checked {{ lastFull }}</template>.
        Drive is checked every few minutes.
      </p>
      <div class="drive-actions">
        <ElButton size="small" :disabled="busy || saving" @click="emit('sync', false)">
          <Icon icon="mdi:sync" class="btn-icon" /> Sync now
        </ElButton>
        <ElButton
          size="small"
          :disabled="busy || saving"
          title="List every Drive file again, not just the recent changes"
          @click="emit('sync', true)"
        >
          <Icon icon="mdi:folder-search-outline" class="btn-icon" /> Check every file
        </ElButton>
      </div>
    </div>

    <p v-if="!enabled && linked && !canEnable" class="drive-hint">
      Drive search can't be turned on while document search is unavailable on this server.
    </p>

    <DriveFolderPicker
      v-model="pickerOpen"
      :whole-drive="wholeDrive"
      :selected="folders"
      :saving="saving"
      :confirm-label="pickerMode === 'enable' && !enabled ? 'Turn on Drive search' : 'Save folders'"
      @save="onPickerSave"
    />

    <ElDialog
      v-model="deletionsOpen"
      title="Files disappeared from Google Drive"
      width="520px"
      :close-on-click-modal="!busy"
      :close-on-press-escape="!busy"
      :show-close="!busy"
      append-to-body
    >
      <div class="del-summary">
        <Icon icon="mdi:folder-alert-outline" class="del-icon" />
        <p>
          <strong>{{ formatCount(massDelete?.missing ?? 0) }} of {{ formatCount(massDelete?.total ?? 0) }} Drive files</strong>
          vanished from what Cremind can see in your Drive at the same time. Nothing has been removed
          from the index yet.
        </p>
      </div>
      <ul class="del-options">
        <li>
          <strong>Keep them</strong> if a folder was unshared or moved by mistake, or access to it is
          being restored. Cremind checks them again on the next full sync.
        </li>
        <li>
          <strong>Remove them</strong> if they were really deleted or should no longer be searched.
          Their entries leave the index; nothing in your Drive is touched.
        </li>
      </ul>
      <template #footer>
        <ElButton :disabled="busy" @click="emit('reject-deletions')">Keep them</ElButton>
        <ElButton class="doc-danger" :loading="busy" @click="emit('confirm-deletions')">Remove them</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.drive { display: flex; flex-direction: column; gap: 12px; }
.drive-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.drive-head-main { min-width: 0; }
.drive-title {
  display: flex; align-items: center; gap: 6px;
  font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 6px;
}
.drive-title-icon { color: var(--primary-color); }
.drive-muted { margin: 0; font-size: 0.85rem; color: var(--text-secondary); line-height: 1.5; }
.btn-icon { margin-right: 4px; }

.drive-row { display: flex; gap: 10px; align-items: flex-start; }
.drive-row-icon { font-size: 22px; color: var(--text-secondary); flex: none; margin-top: 1px; }
.drive-row-main { flex: 1; min-width: 0; }
.drive-row-main strong { font-size: 0.875rem; color: var(--text-primary); }
.drive-row-main p { margin: 2px 0 8px; font-size: 0.82rem; color: var(--text-secondary); line-height: 1.45; }
.drive-row-main p:last-child { margin-bottom: 0; }
.drive-attn { display: flex; gap: 6px; align-items: flex-start; }
.drive-attn :deep(svg) { flex: none; margin-top: 2px; color: var(--warning-color); }
.drive-attn a { color: var(--primary-color); }

.drive-block {
  display: flex; flex-direction: column; gap: 8px; padding-top: 12px;
  border-top: 1px solid var(--border-color);
}
.drive-block-head { display: flex; align-items: center; gap: 8px; }
.drive-block-head strong { font-size: 0.9rem; color: var(--text-primary); font-weight: 600; }
.drive-tag {
  font-size: 0.7rem; padding: 1px 8px; border-radius: 999px; color: var(--text-primary);
  border: 1px solid color-mix(in srgb, var(--warning-color) 45%, var(--border-color));
  background: color-mix(in srgb, var(--warning-color) 10%, var(--surface-color));
}
.drive-hint { margin: 0; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45; }
.drive-folders { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }
.drive-folders li {
  display: inline-flex; align-items: center; gap: 5px; max-width: 100%;
  padding: 3px 10px; border-radius: 999px; font-size: 0.8rem; color: var(--text-primary);
  border: 1px solid var(--border-color); background: var(--bg-color);
}
.drive-folders li :deep(svg) { flex: none; color: var(--primary-color); }
.drive-folders li span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.drive-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.drive-actions :deep(.el-button + .el-button) { margin-left: 0; }

.drive-status { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.drive-chip {
  --tone: var(--primary-color);
  display: inline-flex; align-items: center; gap: 5px; padding: 2px 10px; border-radius: 999px;
  font-size: 0.8rem; color: var(--text-primary);
  border: 1px solid color-mix(in srgb, var(--tone) 40%, var(--border-color));
  background: color-mix(in srgb, var(--tone) 10%, var(--surface-color));
}
.drive-chip :deep(svg) { color: var(--tone); }
.tone-success { --tone: var(--success-color); }
.tone-warning { --tone: var(--warning-color); }
.tone-error { --tone: var(--danger-color); }

.del-summary { display: flex; gap: 10px; align-items: flex-start; }
.del-summary p { margin: 0; font-size: 0.875rem; line-height: 1.5; color: var(--text-primary); }
.del-icon { font-size: 24px; color: var(--warning-color); flex: none; }
.del-options {
  margin: 14px 0 0; padding-left: 18px; font-size: 0.85rem; line-height: 1.5;
  color: var(--text-secondary); display: flex; flex-direction: column; gap: 6px;
}
.del-options strong { color: var(--text-primary); }

/* See ConfirmPlanDialog: the danger token, not ElButton type="danger". */
.doc-danger {
  --el-button-text-color: #fff;
  --el-button-bg-color: var(--danger-color);
  --el-button-border-color: var(--danger-color);
  --el-button-hover-text-color: #fff;
  --el-button-hover-bg-color: color-mix(in srgb, var(--danger-color) 85%, #fff);
  --el-button-hover-border-color: color-mix(in srgb, var(--danger-color) 85%, #fff);
  --el-button-active-text-color: #fff;
  --el-button-active-bg-color: color-mix(in srgb, var(--danger-color) 85%, #000);
  --el-button-active-border-color: color-mix(in srgb, var(--danger-color) 85%, #000);
}

.spin { animation: spin 1.1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
