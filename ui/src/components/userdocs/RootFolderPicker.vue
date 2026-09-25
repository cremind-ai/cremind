<script setup lang="ts">
/**
 * Which folder this profile indexes: the server's working directory
 * ("inherit" — it follows the working directory if an admin moves it), or a
 * folder of the user's choosing.
 *
 * Every candidate is checked live against `POST /validate-root` (debounced
 * 300 ms) — the same checks the save runs, so Save is only offered for a
 * folder the server will accept: it exists and is readable, it is not inside
 * Cremind's system folder (credentials, every profile's data) or an OS
 * location, and for non-admin profiles it sits inside the working directory.
 * Browse lists directories only, through `GET /browse`, which applies the same
 * limits on the server side.
 *
 * Saving emits the choice; the page sends it through the confirm flow, since
 * moving the folder can drop indexed files that are not inside the new one.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import {
  ElButton, ElCheckbox, ElDialog, ElInput, ElRadio, ElRadioGroup,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../../stores/settings';
import {
  browseUserDocsFolders,
  validateUserDocsRoot,
  type BrowseResult,
  type RootCheck,
  type RootMode,
  type UserDocsPolicyView,
  type UserDocsSource,
} from '../../services/userdocsApi';

const props = withDefaults(defineProps<{
  source: UserDocsSource;
  policy: UserDocsPolicyView;
  disabled?: boolean;
  saving?: boolean;
}>(), { disabled: false, saving: false });

const emit = defineEmits<{ save: [choice: { root_mode: RootMode; root_path: string | null }] }>();

const settingsStore = useSettingsStore();

const mode = ref<RootMode>(props.source.root_mode);
const path = ref(props.source.root_mode === 'custom' ? props.source.root_path ?? '' : '');

const dirty = computed(() => {
  if (mode.value !== props.source.root_mode) return true;
  return mode.value === 'custom' && path.value.trim() !== (props.source.root_path ?? '');
});

// A new source from the server (after any save) resets the form, unless the
// user is in the middle of an edit of their own and the folder did not move.
watch(() => props.source, (next, prev) => {
  if (dirty.value && prev && prev.updated_at === next.updated_at) return;
  mode.value = next.root_mode;
  path.value = next.root_mode === 'custom' ? next.root_path ?? '' : '';
});

// ── live validation ───────────────────────────────────────────────────────

const check = ref<RootCheck | null>(null);
const validating = ref(false);
let validateTimer: ReturnType<typeof setTimeout> | null = null;
let validateSeq = 0;

function scheduleValidate() {
  if (validateTimer !== null) clearTimeout(validateTimer);
  const candidate = mode.value === 'inherit' ? null : path.value.trim();
  if (candidate === '') {
    check.value = null;
    validating.value = false;
    return;
  }
  validating.value = true;
  validateTimer = setTimeout(async () => {
    validateTimer = null;
    const seq = ++validateSeq;
    try {
      const result = await validateUserDocsRoot(settingsStore.agentUrl, settingsStore.authToken, candidate);
      if (seq === validateSeq) check.value = result;
    } catch (e) {
      if (seq === validateSeq) {
        check.value = {
          ok: false, path: candidate, code: 'request_failed',
          message: e instanceof Error ? e.message : 'Could not check the folder.',
          locked_excludes: [],
        };
      }
    } finally {
      if (seq === validateSeq) validating.value = false;
    }
  }, 300);
}

watch([mode, path], scheduleValidate, { immediate: true });
onBeforeUnmount(() => { if (validateTimer !== null) clearTimeout(validateTimer); });

const canSave = computed(() =>
  dirty.value && !validating.value && !!check.value?.ok && !props.disabled);

function save() {
  if (!canSave.value || !check.value) return;
  emit('save', {
    root_mode: mode.value,
    root_path: mode.value === 'custom' ? check.value.path ?? path.value.trim() : null,
  });
}

function reset() {
  mode.value = props.source.root_mode;
  path.value = props.source.root_mode === 'custom' ? props.source.root_path ?? '' : '';
}

const constraintHint = computed(() =>
  props.policy.root_constraint === 'working_dir'
    ? `Choose a folder inside the working directory (${props.policy.working_dir}).`
    : 'Any folder this server can read, except Cremind\'s own system folder and operating-system locations.');

// ── browse dialog ─────────────────────────────────────────────────────────

const browseOpen = ref(false);
const browse = ref<BrowseResult | null>(null);
const browseLoading = ref(false);
const browseError = ref('');
const showHidden = ref(false);

async function browseTo(target: string | null) {
  browseLoading.value = true;
  browseError.value = '';
  try {
    browse.value = await browseUserDocsFolders(
      settingsStore.agentUrl, settingsStore.authToken, target, showHidden.value,
    );
  } catch (e) {
    browseError.value = e instanceof Error ? e.message : 'Could not list that folder.';
  } finally {
    browseLoading.value = false;
  }
}

function openBrowse() {
  browseOpen.value = true;
  const start = mode.value === 'custom' && path.value.trim()
    ? path.value.trim()
    : props.source.root_path || null;
  void browseTo(start);
}

watch(showHidden, () => { if (browseOpen.value) void browseTo(browse.value?.path ?? null); });

function chooseBrowsed() {
  if (!browse.value) return;
  mode.value = 'custom';
  path.value = browse.value.path;
  browseOpen.value = false;
}
</script>

<template>
  <div class="root-picker">
    <ElRadioGroup v-model="mode" :disabled="disabled" class="root-modes">
      <ElRadio value="inherit">
        Use the working directory
        <span class="mono">{{ policy.working_dir }}</span>
      </ElRadio>
      <ElRadio value="custom">Choose a folder</ElRadio>
    </ElRadioGroup>

    <div v-if="mode === 'custom'" class="root-custom">
      <ElInput
        v-model="path"
        :disabled="disabled"
        placeholder="/path/to/your/documents"
        clearable
      />
      <ElButton :disabled="disabled" @click="openBrowse">
        <Icon icon="mdi:folder-open-outline" class="btn-icon" /> Browse…
      </ElButton>
    </div>
    <p class="hint">{{ constraintHint }}</p>

    <p v-if="validating" class="check pending">
      <Icon icon="mdi:loading" class="spin" /> Checking the folder…
    </p>
    <p v-else-if="check && check.ok" class="check ok">
      <Icon icon="mdi:check-circle-outline" />
      <span><span class="mono">{{ check.path }}</span> can be indexed.</span>
    </p>
    <p v-else-if="check && !check.ok" class="check bad">
      <Icon icon="mdi:alert-circle-outline" />
      <span>{{ check.message || 'This folder cannot be indexed.' }}</span>
    </p>
    <p v-if="check?.ok && check.locked_excludes.length" class="hint">
      Always skipped inside it:
      <span v-for="p in check.locked_excludes" :key="p" class="mono">{{ p }}</span>
      (Cremind's own data).
    </p>

    <div v-if="dirty" class="root-actions">
      <ElButton type="primary" :loading="saving" :disabled="!canSave" @click="save">Save folder</ElButton>
      <ElButton :disabled="saving" @click="reset">Cancel</ElButton>
    </div>

    <ElDialog v-model="browseOpen" title="Choose a folder" width="560px" append-to-body>
      <div class="browse-bar">
        <ElButton
          size="small"
          :disabled="!browse?.parent || browseLoading"
          title="Up one level"
          @click="browseTo(browse?.parent ?? null)"
        >
          <Icon icon="mdi:arrow-up" />
        </ElButton>
        <span class="mono browse-path">{{ browse?.path || '…' }}</span>
      </div>
      <div v-if="browse?.roots?.length" class="browse-roots">
        <button
          v-for="r in browse.roots"
          :key="r"
          type="button"
          class="browse-root"
          @click="browseTo(r)"
        >
          <Icon icon="mdi:folder-outline" /> {{ r }}
        </button>
      </div>
      <p v-if="browseError" class="check bad"><Icon icon="mdi:alert-circle-outline" /> {{ browseError }}</p>
      <div class="browse-list" :class="{ loading: browseLoading }">
        <button
          v-for="entry in browse?.entries ?? []"
          :key="entry.path"
          type="button"
          class="browse-entry"
          @click="browseTo(entry.path)"
        >
          <Icon icon="mdi:folder-outline" class="entry-icon" />
          <span>{{ entry.name }}</span>
          <Icon icon="mdi:chevron-right" class="entry-chevron" />
        </button>
        <p v-if="browse && !browse.entries.length && !browseLoading" class="browse-empty">No subfolders.</p>
        <p v-if="browse?.truncated" class="hint">Only the first 2,000 subfolders are shown.</p>
      </div>
      <template #footer>
        <div class="browse-footer">
          <ElCheckbox v-model="showHidden">Show hidden folders</ElCheckbox>
          <span class="grow" />
          <ElButton @click="browseOpen = false">Cancel</ElButton>
          <ElButton type="primary" :disabled="!browse || browseLoading" @click="chooseBrowsed">
            Use this folder
          </ElButton>
        </div>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.root-picker { display: flex; flex-direction: column; gap: 8px; }
.root-modes { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }
.root-modes :deep(.el-radio) { height: auto; min-height: 28px; white-space: normal; margin-right: 0; }
.root-custom { display: flex; gap: 8px; }
.root-custom :deep(.el-input) { flex: 1; }
.btn-icon { margin-right: 4px; }
.mono {
  font-family: var(--font-mono, monospace); font-size: 0.8rem; color: var(--text-secondary);
  word-break: break-all; margin-left: 4px;
}
.hint { margin: 0; font-size: 0.78rem; color: var(--text-secondary); line-height: 1.45; }
.check { display: flex; align-items: flex-start; gap: 6px; margin: 0; font-size: 0.82rem; line-height: 1.45; }
.check :deep(svg) { flex: none; margin-top: 2px; }
.check.ok { color: var(--text-primary); }
.check.ok :deep(svg) { color: var(--success-color); }
.check.bad { color: var(--text-primary); }
.check.bad :deep(svg) { color: var(--danger-color); }
.check.pending { color: var(--text-secondary); }
.check .mono { margin-left: 0; }
.root-actions { display: flex; gap: 8px; margin-top: 4px; }
.root-actions :deep(.el-button + .el-button) { margin-left: 0; }

.browse-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.browse-path { margin-left: 0; color: var(--text-primary); }
.browse-roots { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.browse-root {
  display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 6px;
  border: 1px solid var(--border-color); background: var(--bg-color); color: var(--text-secondary);
  font-size: 0.75rem; cursor: pointer; max-width: 100%; overflow: hidden; text-overflow: ellipsis;
}
.browse-root:hover { color: var(--primary-color); border-color: var(--primary-color); }
.browse-list {
  max-height: 320px; overflow-y: auto; border: 1px solid var(--border-color);
  border-radius: 8px; padding: 4px; background: var(--bg-color);
}
.browse-list.loading { opacity: 0.6; }
.browse-entry {
  display: flex; align-items: center; gap: 8px; width: 100%; padding: 6px 8px;
  border: none; background: none; cursor: pointer; border-radius: 6px; text-align: left;
  color: var(--text-primary); font-size: 0.85rem;
}
.browse-entry:hover { background: var(--hover-bg); }
.entry-icon { color: var(--primary-color); flex: none; }
.entry-chevron { margin-left: auto; color: var(--text-tertiary); flex: none; }
.browse-empty { margin: 8px; font-size: 0.82rem; color: var(--text-secondary); }
.browse-footer { display: flex; align-items: center; gap: 8px; }
.browse-footer .grow { flex: 1; }
.browse-footer :deep(.el-button + .el-button) { margin-left: 0; }

.spin { animation: spin 1.1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
