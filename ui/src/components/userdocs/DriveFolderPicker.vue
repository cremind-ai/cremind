<script setup lang="ts">
/**
 * Choose the Google Drive folders Drive search indexes (`include_folders`).
 * A chosen folder brings everything inside it, subfolders included.
 *
 * The dialog browses the linked account one level at a time through
 * `GET /api/userdocs/drive/folders` — the same `DriveClient` the sync uses, so
 * what is listed here is what the sync can reach. A whole-Drive account starts
 * at My Drive and must keep at least one folder; a per-file account starts at
 * the folders it was granted, and choosing none means "everything granted".
 * A folder the browser can't reach from the top (shared from elsewhere) can be
 * added by pasting its Drive link; it is looked up once to check access and
 * learn its name.
 *
 * Saving emits the choice. The Drive section sends it, and the page's confirm
 * flow asks first when it narrows what is indexed (the Drive files outside
 * the new folders leave the index).
 */
import { computed, ref, watch } from 'vue';
import { ElButton, ElCheckbox, ElDialog, ElInput } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../../stores/settings';
import {
  listDriveFolders,
  UserDocsApiError,
  type DriveFolder,
} from '../../services/userdocsApi';
import {
  coveringFolder,
  crumbsTo,
  driveRootCrumb,
  droppedFolders,
  enterFolder,
  folderFallbackName,
  parseDriveFolderRef,
  sameFolderSet,
  toggleFolder,
  type DriveCrumb,
} from '../../utils/userdocsView';

const props = withDefaults(defineProps<{
  modelValue: boolean;
  /** The account sees the whole Drive: at least one folder is required. */
  wholeDrive: boolean;
  /** The saved folders (with names where known). */
  selected: DriveFolder[];
  saving?: boolean;
  confirmLabel?: string;
}>(), { saving: false, confirmLabel: 'Save folders' });

const emit = defineEmits<{
  'update:modelValue': [open: boolean];
  save: [folders: DriveFolder[]];
}>();

const settingsStore = useSettingsStore();

const crumbs = ref<DriveCrumb[]>([driveRootCrumb(props.wholeDrive)]);
const listing = ref<DriveFolder[]>([]);
const loading = ref(false);
const listError = ref('');
const chosen = ref<DriveFolder[]>([]);
const pasteText = ref('');
const pasteError = ref('');
const pasting = ref(false);
let loadSeq = 0;

const here = computed(() => crumbs.value[crumbs.value.length - 1]);
const covering = computed(() => coveringFolder(crumbs.value, chosen.value));
const chosenIds = computed(() => chosen.value.map(f => f.id));
const savedIds = computed(() => props.selected.map(f => f.id));

const changed = computed(() => !sameFolderSet(chosenIds.value, savedIds.value));
const missingRequired = computed(() => props.wholeDrive && chosen.value.length === 0);
const canSave = computed(() => changed.value && !missingRequired.value && !props.saving);
/** Fewer files will be indexed — the server confirms how many leave. */
const narrows = computed(() =>
  droppedFolders(savedIds.value, chosenIds.value).length > 0
  || (!props.wholeDrive && savedIds.value.length === 0 && chosen.value.length > 0));

function describe(e: unknown): string {
  if (e instanceof UserDocsApiError) {
    if (e.code === 'DriveNotLinked') return 'Google Drive is not linked for this profile. Link it in Settings → GSuite.';
    if (e.code === 'DriveUnreachable') return "Google Drive didn't answer. Try again in a moment.";
  }
  return e instanceof Error ? e.message : 'Could not list the folders.';
}

async function load(parent: string | null) {
  const seq = ++loadSeq;
  loading.value = true;
  listError.value = '';
  try {
    const res = await listDriveFolders(settingsStore.agentUrl, settingsStore.authToken, parent);
    if (seq !== loadSeq) return;
    listing.value = Array.isArray(res.folders) ? res.folders : [];
  } catch (e) {
    if (seq !== loadSeq) return;
    listing.value = [];
    listError.value = describe(e);
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

// Every opening starts from the saved choice and the top of the Drive.
watch(() => props.modelValue, (open) => {
  if (!open) return;
  chosen.value = props.selected.map(f => ({ ...f }));
  crumbs.value = [driveRootCrumb(props.wholeDrive)];
  pasteText.value = '';
  pasteError.value = '';
  void load(null);
}, { immediate: true });

function openFolder(folder: DriveFolder) {
  crumbs.value = enterFolder(crumbs.value, folder);
  void load(folder.id);
}

function goTo(index: number) {
  crumbs.value = crumbsTo(crumbs.value, index);
  void load(here.value.id);
}

function isChosen(id: string): boolean {
  return chosenIds.value.includes(id);
}

function toggle(folder: DriveFolder) {
  chosen.value = toggleFolder(chosen.value, folder);
}

// ── paste a folder link ───────────────────────────────────────────────────

async function addPasted() {
  pasteError.value = '';
  const id = parseDriveFolderRef(pasteText.value);
  if (!id) {
    pasteError.value = 'Paste a Google Drive folder link (drive.google.com/drive/folders/…) or a folder id.';
    return;
  }
  if (isChosen(id)) {
    pasteText.value = '';
    return;
  }
  pasting.value = true;
  try {
    // Listing it checks the linked account can open it, and names it.
    const res = await listDriveFolders(settingsStore.agentUrl, settingsStore.authToken, id);
    const name = res.parent?.id === id && res.parent.name ? res.parent.name : folderFallbackName(id);
    chosen.value = [...chosen.value, { id, name }];
    pasteText.value = '';
  } catch (e) {
    pasteError.value = e instanceof UserDocsApiError && e.code !== 'DriveUnreachable' && e.code !== 'DriveNotLinked'
      ? "The linked Google account can't open that folder."
      : describe(e);
  } finally {
    pasting.value = false;
  }
}

function close() {
  if (!props.saving) emit('update:modelValue', false);
}

function save() {
  if (!canSave.value) return;
  emit('save', chosen.value.map(f => ({ ...f })));
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    title="Choose Google Drive folders"
    width="580px"
    :close-on-click-modal="!saving"
    :close-on-press-escape="!saving"
    :show-close="!saving"
    append-to-body
    @update:model-value="(v: boolean) => { if (!v) close(); }"
  >
    <p class="dfp-intro">
      <template v-if="wholeDrive">
        This Google account can see your whole Drive, so Cremind indexes only the folders you choose
        here — with everything inside them. Choose at least one.
      </template>
      <template v-else>
        Cremind indexes the files you granted it and the files it created. Choose folders to index
        only what is inside them, or none to keep indexing everything granted.
      </template>
    </p>

    <div class="dfp-chosen" :class="{ empty: !chosen.length }">
      <span class="dfp-chosen-label">Chosen:</span>
      <span v-if="!chosen.length" class="dfp-none">
        {{ wholeDrive ? 'none yet' : 'none — everything granted is indexed' }}
      </span>
      <span v-for="f in chosen" :key="f.id" class="dfp-chip" :title="f.id">
        <Icon icon="mdi:folder-google-drive" />
        <span class="dfp-chip-name">{{ f.name }}</span>
        <button type="button" class="dfp-chip-x" :disabled="saving" :title="`Remove ${f.name}`" @click="toggle(f)">
          <Icon icon="mdi:close" />
        </button>
      </span>
    </div>

    <nav class="dfp-crumbs" aria-label="Folder path">
      <template v-for="(c, i) in crumbs" :key="`${i}:${c.id ?? 'top'}`">
        <Icon v-if="i > 0" icon="mdi:chevron-right" class="dfp-crumb-sep" />
        <button
          type="button"
          class="dfp-crumb"
          :class="{ current: i === crumbs.length - 1 }"
          :disabled="i === crumbs.length - 1 || loading"
          @click="goTo(i)"
        >
          {{ c.name }}
        </button>
      </template>
    </nav>

    <p v-if="covering" class="dfp-covered">
      <Icon icon="mdi:check-circle-outline" />
      Everything here is already included with “{{ covering.name }}”.
    </p>
    <p v-if="listError" class="dfp-error"><Icon icon="mdi:alert-circle-outline" /> {{ listError }}</p>

    <div class="dfp-list" :class="{ loading }">
      <div v-for="folder in listing" :key="folder.id" class="dfp-row">
        <ElCheckbox
          :model-value="!!covering || isChosen(folder.id)"
          :disabled="!!covering || saving"
          :aria-label="`Index ${folder.name}`"
          @update:model-value="toggle(folder)"
        />
        <button type="button" class="dfp-open" :disabled="loading" @click="openFolder(folder)">
          <Icon icon="mdi:folder-google-drive" class="dfp-icon" />
          <span class="dfp-name">{{ folder.name }}</span>
          <Icon icon="mdi:chevron-right" class="dfp-chevron" />
        </button>
      </div>
      <p v-if="!loading && !listError && !listing.length" class="dfp-empty">
        <template v-if="here.id">No subfolders.</template>
        <template v-else-if="wholeDrive">No folders in My Drive.</template>
        <template v-else>
          No folders were granted to Cremind. Grant folders in Settings → GSuite → Drive access, or
          paste a folder link below.
        </template>
      </p>
      <p v-if="loading && !listing.length" class="dfp-empty">
        <Icon icon="mdi:loading" class="spin" /> Loading…
      </p>
    </div>

    <div class="dfp-paste">
      <ElInput
        v-model="pasteText"
        size="small"
        clearable
        :disabled="saving || pasting"
        placeholder="Or paste a Drive folder link"
        @keyup.enter="addPasted"
      />
      <ElButton size="small" :loading="pasting" :disabled="saving || !pasteText.trim()" @click="addPasted">
        Add
      </ElButton>
    </div>
    <p v-if="pasteError" class="dfp-error"><Icon icon="mdi:alert-circle-outline" /> {{ pasteError }}</p>

    <p v-if="narrows && changed" class="dfp-hint">
      <Icon icon="mdi:information-outline" />
      Drive files outside the chosen folders leave the index — you are asked to confirm how many first.
    </p>
    <p v-else-if="missingRequired" class="dfp-hint">
      <Icon icon="mdi:information-outline" />
      Choose at least one folder.
    </p>

    <template #footer>
      <ElButton :disabled="saving" @click="close">Cancel</ElButton>
      <ElButton type="primary" :loading="saving" :disabled="!canSave" @click="save">
        {{ confirmLabel }}
      </ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.dfp-intro { margin: 0 0 12px; font-size: 0.85rem; color: var(--text-secondary); line-height: 1.5; }

.dfp-chosen {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 10px;
  padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border-color); background: var(--bg-color);
}
.dfp-chosen-label { font-size: 0.8rem; color: var(--text-secondary); }
.dfp-none { font-size: 0.8rem; color: var(--text-tertiary); }
.dfp-chip {
  display: inline-flex; align-items: center; gap: 4px; max-width: 100%;
  padding: 2px 4px 2px 8px; border-radius: 999px; font-size: 0.78rem; color: var(--text-primary);
  border: 1px solid color-mix(in srgb, var(--primary-color) 40%, var(--border-color));
  background: color-mix(in srgb, var(--primary-color) 10%, var(--surface-color));
}
.dfp-chip :deep(svg) { flex: none; color: var(--primary-color); }
.dfp-chip-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dfp-chip-x {
  display: inline-flex; border: none; background: none; cursor: pointer; padding: 2px;
  border-radius: 50%; color: var(--text-secondary); line-height: 1;
}
.dfp-chip-x :deep(svg) { color: inherit; }
.dfp-chip-x:hover:not(:disabled) { color: var(--danger-color); background: var(--hover-bg); }
.dfp-chip-x:disabled { cursor: not-allowed; opacity: 0.5; }

.dfp-crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 2px; margin-bottom: 6px; }
.dfp-crumb {
  border: none; background: none; cursor: pointer; padding: 2px 4px; border-radius: 4px;
  font-size: 0.82rem; color: var(--primary-color);
}
.dfp-crumb:hover:not(:disabled) { background: var(--hover-bg); }
.dfp-crumb.current { color: var(--text-primary); font-weight: 600; cursor: default; }
.dfp-crumb:disabled:not(.current) { opacity: 0.6; cursor: default; }
.dfp-crumb-sep { color: var(--text-tertiary); font-size: 14px; }

.dfp-covered, .dfp-error, .dfp-hint {
  display: flex; align-items: flex-start; gap: 6px; margin: 0 0 8px;
  font-size: 0.8rem; line-height: 1.45; color: var(--text-secondary);
}
.dfp-covered :deep(svg), .dfp-error :deep(svg), .dfp-hint :deep(svg) { flex: none; margin-top: 2px; }
.dfp-covered :deep(svg) { color: var(--success-color); }
.dfp-error { color: var(--text-primary); }
.dfp-error :deep(svg) { color: var(--danger-color); }
.dfp-hint { margin: 8px 0 0; }
.dfp-hint :deep(svg) { color: var(--primary-color); }

.dfp-list {
  max-height: 300px; overflow-y: auto; border: 1px solid var(--border-color);
  border-radius: 8px; padding: 4px; background: var(--bg-color);
}
.dfp-list.loading { opacity: 0.6; }
.dfp-row { display: flex; align-items: center; gap: 4px; padding-left: 6px; border-radius: 6px; }
.dfp-row:hover { background: var(--hover-bg); }
.dfp-open {
  flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; padding: 6px 8px;
  border: none; background: none; cursor: pointer; text-align: left;
  color: var(--text-primary); font-size: 0.85rem;
}
.dfp-open:disabled { cursor: default; }
.dfp-icon { color: var(--primary-color); flex: none; }
.dfp-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dfp-chevron { margin-left: auto; color: var(--text-tertiary); flex: none; }
.dfp-empty {
  display: flex; align-items: center; gap: 6px; margin: 8px;
  font-size: 0.82rem; color: var(--text-secondary); line-height: 1.45;
}

.dfp-paste { display: flex; gap: 8px; margin-top: 10px; }
.dfp-paste :deep(.el-input) { flex: 1; }

.spin { animation: spin 1.1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
