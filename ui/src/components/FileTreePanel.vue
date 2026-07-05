<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import {
  listDirectory,
  getInitialCwd,
  watchDirectory,
  parentDir,
  uploadFiles,
  DirectoryAccessError,
  type DirectoryEntry,
  type FileWatchHandle,
} from '../services/filesApi';
import FileTreeNode from './FileTreeNode.vue';
import FileGridView from './FileGridView.vue';
import CwdBreadcrumb from './CwdBreadcrumb.vue';

const settings = useSettingsStore();
const panel = useTerminalPanelStore();

const rootEntries = ref<DirectoryEntry[]>([]);
const loading = ref(false);
const errorMessage = ref<string | null>(null);
const truncated = ref(false);
let abortCtl: AbortController | null = null;

// The last directory that listed cleanly with no active conversation — the
// fallback we roll back to when a subsequent no-conversation navigation lands
// on an unlistable path (see loadRoot's catch).
const lastGoodCwd = ref('');

// Transient toast shown when we recover from a failed navigation, so the user
// learns why the cwd didn't change without a persistent error screen.
const treeToast = ref<string | null>(null);
let treeToastTimer: ReturnType<typeof setTimeout> | null = null;
function showTreeToast(msg: string) {
  treeToast.value = msg;
  if (treeToastTimer) clearTimeout(treeToastTimer);
  treeToastTimer = setTimeout(() => {
    treeToast.value = null;
    treeToastTimer = null;
  }, 4000);
}

async function loadRoot() {
  if (!panel.cwd) return;
  abortCtl?.abort();
  abortCtl = new AbortController();
  loading.value = true;
  errorMessage.value = null;
  try {
    const data = await listDirectory(
      settings.agentUrl,
      settings.authToken,
      panel.cwd,
      panel.showHiddenFiles,
      abortCtl.signal,
      panel.scopeConversationId || undefined,
    );
    rootEntries.value = data.entries;
    truncated.value = data.truncated;
    // Only no-conversation cwds are safe rollback targets — conversation cwds
    // are widened server-side via the override and may sit outside the static
    // allowlist.
    if (!panel.scopeConversationId) lastGoodCwd.value = panel.cwd;
  } catch (e: unknown) {
    if ((e as Error)?.name === 'AbortError') return;
    // Without a conversation, a 403/404 means the target resolves outside the
    // allowlist (e.g. a symlink/junction pointing out of the working tree) or
    // has vanished — there's no override to widen the allowlist and no async
    // race. Roll the cwd back to the last good directory and toast, instead of
    // stranding the tree on an error screen. (With a conversation, a transient
    // 403 is the override-not-yet-set race, so we must NOT roll back.)
    if (
      e instanceof DirectoryAccessError &&
      (e.status === 403 || e.status === 404) &&
      !panel.scopeConversationId &&
      lastGoodCwd.value &&
      lastGoodCwd.value !== panel.cwd
    ) {
      panel.setUserDefaultCwd(lastGoodCwd.value);
      showTreeToast("That folder isn't accessible");
      return; // the cwd change re-triggers loadRoot for the restored directory
    }
    if (e instanceof DirectoryAccessError) {
      if (e.status === 403) {
        errorMessage.value = 'Path is outside accessible bases';
      } else if (e.status === 404) {
        errorMessage.value = 'Directory not found';
      } else {
        errorMessage.value = e.message;
      }
    } else {
      errorMessage.value = 'Failed to load directory';
    }
    rootEntries.value = [];
    truncated.value = false;
  } finally {
    loading.value = false;
  }
}

let watchHandle: FileWatchHandle | null = null;
let rootRefetchTimer: ReturnType<typeof setTimeout> | null = null;

function scheduleRootRefetch() {
  if (rootRefetchTimer) return;
  rootRefetchTimer = setTimeout(() => {
    rootRefetchTimer = null;
    loadRoot();
  }, 200);
}

function openWatch() {
  closeWatch();
  if (!panel.cwd || !settings.authToken) return;
  watchHandle = watchDirectory(
    settings.agentUrl,
    settings.authToken,
    panel.cwd,
    (ev) => {
      if (ev.type === 'ready') return;
      // Broadcast to subtree nodes; they decide whether to act.
      panel.pushFileEvent(ev);
      // Root-level relevance: event's parent matches the watched cwd.
      const parents: string[] = [parentDir(ev.path)];
      if (ev.type === 'moved' && ev.dest_path) parents.push(parentDir(ev.dest_path));
      if (parents.includes(panel.cwd)) scheduleRootRefetch();
    },
    undefined,
    panel.scopeConversationId || undefined,
  );
}

function closeWatch() {
  watchHandle?.close();
  watchHandle = null;
  if (rootRefetchTimer) {
    clearTimeout(rootRefetchTimer);
    rootRefetchTimer = null;
  }
}

// External (OS) drag-and-drop upload onto the panel root → upload to the
// current cwd. Internal drags carry the ``application/x-cremind-path`` MIME
// and are handled lower in the tree by the individual node/tile components.
const dragHover = ref(false);

function onPanelDragOver(ev: DragEvent) {
  if (!ev.dataTransfer) return;
  // Only show the drop affordance for OS file drags.
  if (!ev.dataTransfer.types.includes('Files')) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect = 'copy';
  dragHover.value = true;
}

function onPanelDragLeave(ev: DragEvent) {
  // ``dragleave`` fires for every child enter; only clear when we're leaving
  // the panel root for real (relatedTarget outside).
  const related = ev.relatedTarget as Node | null;
  const root = ev.currentTarget as HTMLElement;
  if (!related || !root.contains(related)) dragHover.value = false;
}

async function onPanelDrop(ev: DragEvent) {
  dragHover.value = false;
  if (!ev.dataTransfer) return;
  const files = ev.dataTransfer.files;
  if (!files || files.length === 0) return; // internal drops handled elsewhere
  ev.preventDefault();
  try {
    await uploadFiles(
      settings.agentUrl,
      settings.authToken,
      panel.cwd,
      Array.from(files),
      panel.scopeConversationId || undefined,
    );
  } catch (e) {
    // The watch SSE will refresh on success; surface only the failure.
    // eslint-disable-next-line no-console
    console.warn('Upload failed', e);
  }
}

onMounted(async () => {
  // Seed the fallback cwd used when no conversation is active. Per-
  // conversation cwds arrive through the chat SSE stream's ``ready`` and
  // ``cwd`` events and are written into the panel store from there.
  if (!panel.userDefaultCwd) {
    try {
      const cwd = await getInitialCwd(settings.agentUrl, settings.authToken);
      panel.setUserDefaultCwd(cwd);
      // Pin the working-dir root too — it's an allowed read base and the
      // floor for no-conversation breadcrumb navigation.
      panel.setUserWorkingRoot(cwd);
    } catch {
      /* fall through — a ready event will populate eventually */
    }
  }
});

watch(
  // Re-fetch when the cwd changes OR the scope conversation changes (e.g. the
  // event-run drawer focuses a run whose cwd equals the chat's — same path,
  // different backend read-allowlist scope).
  () => [panel.cwd, panel.scopeConversationId] as const,
  () => {
    loadRoot();
    openWatch();
  },
  { immediate: true },
);
watch(() => panel.showHiddenFiles, loadRoot);

onBeforeUnmount(() => {
  closeWatch();
  if (treeToastTimer) clearTimeout(treeToastTimer);
});
</script>

<template>
  <div
    class="file-tree-panel"
    :class="{ 'drag-hover': dragHover }"
    @dragover="onPanelDragOver"
    @dragleave="onPanelDragLeave"
    @drop="onPanelDrop"
  >
    <div class="tree-header">
      <CwdBreadcrumb />
      <button
        class="tree-action"
        :title="panel.viewMode === 'icon' ? 'Switch to list view' : 'Switch to icon view'"
        @click="panel.toggleViewMode()"
      >
        <Icon :icon="panel.viewMode === 'icon' ? 'mdi:view-list' : 'mdi:view-grid'" />
      </button>
      <button
        class="tree-action"
        :title="panel.showHiddenFiles ? 'Hide hidden files' : 'Show hidden files'"
        @click="panel.toggleHidden()"
      >
        <Icon :icon="panel.showHiddenFiles ? 'mdi:eye-outline' : 'mdi:eye-off-outline'" />
      </button>
    </div>
    <div class="tree-body">
      <div v-if="loading && rootEntries.length === 0" class="tree-status">
        <Icon icon="mdi:loading" class="spinner" />
        <span>Loading…</span>
      </div>
      <div v-else-if="errorMessage" class="tree-status error">
        <Icon icon="mdi:alert-circle-outline" />
        <span>{{ errorMessage }}</span>
      </div>
      <template v-else-if="panel.viewMode === 'icon'">
        <FileGridView :entries="rootEntries" />
        <div v-if="truncated" class="tree-status truncated">
          Directory truncated at 2000 entries — use the terminal to browse.
        </div>
      </template>
      <ul v-else class="tree-root">
        <FileTreeNode
          v-for="entry in rootEntries"
          :key="entry.path"
          :entry="entry"
          :depth="0"
        />
        <li v-if="truncated" class="tree-status truncated">
          Directory truncated at 2000 entries — use the terminal to browse.
        </li>
      </ul>
    </div>
    <div v-if="treeToast" class="tree-toast">{{ treeToast }}</div>
  </div>
</template>

<style scoped>
.file-tree-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--bg-color);
  color: var(--text-primary);
  overflow: hidden;
  position: relative;
}
.file-tree-panel.drag-hover::after {
  content: 'Drop to upload here';
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(59, 130, 246, 0.12);
  outline: 2px dashed var(--primary-light);
  outline-offset: -8px;
  color: var(--primary-color);
  font-size: 0.9rem;
  pointer-events: none;
  z-index: 5;
}
.tree-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  background: var(--surface-color);
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
  font-size: 0.78rem;
  color: var(--text-secondary);
}
.tree-header-icon {
  font-size: 1rem;
  flex-shrink: 0;
}
.tree-cwd {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: Consolas, Monaco, monospace;
  color: var(--text-primary);
}
.tree-action {
  background: transparent;
  border: none;
  color: var(--text-secondary);
  padding: 2px 4px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  border-radius: 3px;
  font-size: 0.95rem;
  flex-shrink: 0;
}
.tree-action:hover {
  color: var(--text-primary);
  background: var(--hover-bg);
}
.tree-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 4px 0;
}
.tree-root {
  list-style: none;
  margin: 0;
  padding: 0;
}
.tree-status {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font-size: 0.8rem;
  color: var(--text-secondary);
}
.tree-status.error {
  color: var(--danger-color);
}
.tree-status.truncated {
  font-style: italic;
}
.spinner {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
.tree-toast {
  position: absolute;
  left: 50%;
  bottom: 12px;
  transform: translateX(-50%);
  z-index: 10;
  padding: 6px 12px;
  max-width: 90%;
  background: var(--danger-color);
  color: #fff;
  border-radius: 4px;
  font-size: 0.78rem;
  font-family: system-ui, sans-serif;
  text-align: center;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
  pointer-events: none;
}
</style>
