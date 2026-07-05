<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import {
  listDirectory,
  parentDir,
  deleteEntry,
  downloadFile,
  mkdir as mkdirApi,
  moveEntry,
  uploadFiles,
  DirectoryAccessError,
  type DirectoryEntry,
} from '../services/filesApi';
import { iconFor } from '../utils/fileIcons';
import { openFileInNewTab } from '../utils/openFile';
import FileContextMenu, {
  type FileContextAction,
  type FileContextMenuItem,
} from './FileContextMenu.vue';

const props = defineProps<{
  entry: DirectoryEntry;
  depth: number;
}>();

const settings = useSettingsStore();
const panel = useTerminalPanelStore();

const expanded = ref(false);
const loading = ref(false);
const errorMessage = ref<string | null>(null);
const children = ref<DirectoryEntry[]>([]);
const truncated = ref(false);
let abortCtl: AbortController | null = null;

async function loadChildren() {
  if (!props.entry.is_dir) return;
  abortCtl?.abort();
  abortCtl = new AbortController();
  loading.value = true;
  errorMessage.value = null;
  try {
    const data = await listDirectory(
      settings.agentUrl,
      settings.authToken,
      props.entry.path,
      panel.showHiddenFiles,
      abortCtl.signal,
      panel.scopeConversationId || undefined,
    );
    children.value = data.entries;
    truncated.value = data.truncated;
  } catch (e: unknown) {
    if ((e as Error)?.name === 'AbortError') return;
    if (e instanceof DirectoryAccessError) {
      errorMessage.value = e.message;
    } else {
      errorMessage.value = 'Failed to load';
    }
  } finally {
    loading.value = false;
  }
}

async function toggleExpand() {
  if (!props.entry.is_dir) return;
  expanded.value = !expanded.value;
  if (expanded.value && children.value.length === 0 && !errorMessage.value) {
    await loadChildren();
  }
}

// Re-fetch children whenever the show-hidden toggle flips while expanded.
watch(
  () => panel.showHiddenFiles,
  () => {
    if (expanded.value && props.entry.is_dir) {
      loadChildren();
    }
  },
);

// React to filesystem-watch events streamed from the backend. If an event
// lands inside this node's directory and the node is expanded, refetch
// children (debounced so a burst of events triggers one reload).
let childRefetchTimer: ReturnType<typeof setTimeout> | null = null;
function scheduleChildRefetch() {
  if (childRefetchTimer) return;
  childRefetchTimer = setTimeout(() => {
    childRefetchTimer = null;
    if (expanded.value && props.entry.is_dir) loadChildren();
  }, 200);
}

watch(
  () => panel.lastFileEvent,
  (ev) => {
    if (!ev || !props.entry.is_dir || !expanded.value) return;
    const candidates = [parentDir(ev.path)];
    if (ev.type === 'moved' && ev.dest_path) candidates.push(parentDir(ev.dest_path));
    if (candidates.includes(props.entry.path)) scheduleChildRefetch();
  },
);

function handleFileClick() {
  if (props.entry.is_dir) return;
  panel.setSelectedFile(props.entry.path);
}

function handleFileDoubleClick() {
  if (props.entry.is_dir) return;
  openFileInNewTab(
    settings.agentUrl,
    settings.authToken,
    props.entry.path,
    panel.scopeConversationId || undefined,
  ).catch(() => {
    /* surfaced via console; tree row stays put */
  });
}

const INTERNAL_MIME = 'application/x-cremind-path';

function pathSep(): string {
  return props.entry.path.includes('\\') ? '\\' : '/';
}

function basename(p: string): string {
  const sepIdx = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return sepIdx === -1 ? p : p.slice(sepIdx + 1);
}

// ---- context menu ----

interface ActiveMenu {
  x: number;
  y: number;
}
const activeMenu = ref<ActiveMenu | null>(null);

function menuItems(): FileContextMenuItem[] {
  if (props.entry.is_dir) {
    return [
      { action: 'upload', label: 'Upload here…', icon: 'mdi:upload' },
      { action: 'mkdir', label: 'New folder', icon: 'mdi:folder-plus-outline' },
      { action: 'rename', label: 'Rename', icon: 'mdi:rename-outline' },
      { action: 'delete', label: 'Delete', icon: 'mdi:trash-can-outline', danger: true },
    ];
  }
  return [
    { action: 'download', label: 'Download', icon: 'mdi:download' },
    { action: 'rename', label: 'Rename', icon: 'mdi:rename-outline' },
    { action: 'delete', label: 'Delete', icon: 'mdi:trash-can-outline', danger: true },
  ];
}

function openContextMenu(ev: MouseEvent) {
  ev.preventDefault();
  ev.stopPropagation();
  activeMenu.value = { x: ev.clientX, y: ev.clientY };
}

async function handleMenuPick(action: FileContextAction) {
  activeMenu.value = null;
  const entry = props.entry;
  switch (action) {
    case 'download':
      try {
        await downloadFile(
          settings.agentUrl,
          settings.authToken,
          entry.path,
          entry.name,
          panel.scopeConversationId || undefined,
        );
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn('Download failed', e);
      }
      break;
    case 'delete': {
      const ok = window.confirm(
        `Delete ${entry.is_dir ? 'folder' : 'file'} "${entry.name}"?` +
          (entry.is_dir ? ' This removes all of its contents.' : ''),
      );
      if (!ok) return;
      try {
        await deleteEntry(
          settings.agentUrl,
          settings.authToken,
          entry.path,
          panel.scopeConversationId || undefined,
        );
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn('Delete failed', e);
      }
      break;
    }
    case 'rename':
      startRename();
      break;
    case 'upload':
      if (entry.is_dir) triggerUpload(entry.path);
      break;
    case 'mkdir': {
      if (!entry.is_dir) return;
      const name = window.prompt('New folder name');
      if (!name) return;
      const dest = entry.path + pathSep() + name;
      try {
        await mkdirApi(
          settings.agentUrl,
          settings.authToken,
          dest,
          panel.scopeConversationId || undefined,
        );
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn('mkdir failed', e);
      }
      break;
    }
    case 'refresh':
      break;
  }
}

// ---- inline rename ----

const renaming = ref(false);
const renameInputRef = ref<HTMLInputElement | null>(null);
const renameBuffer = ref('');

async function startRename() {
  renaming.value = true;
  renameBuffer.value = props.entry.name;
  await nextTick();
  renameInputRef.value?.focus();
  renameInputRef.value?.select();
}

async function commitRename() {
  if (!renaming.value) return;
  const newName = renameBuffer.value.trim();
  renaming.value = false;
  if (!newName || newName === props.entry.name) return;
  const dest = parentDir(props.entry.path) + pathSep() + newName;
  try {
    await moveEntry(
      settings.agentUrl,
      settings.authToken,
      props.entry.path,
      dest,
      panel.scopeConversationId || undefined,
    );
  } catch (e) {
    // eslint-disable-next-line no-console
    console.warn('Rename failed', e);
  }
}

function cancelRename() {
  renaming.value = false;
}

// ---- drag and drop ----

const dropHover = ref(false);

function onDragStart(ev: DragEvent) {
  if (!ev.dataTransfer) return;
  ev.dataTransfer.setData(INTERNAL_MIME, props.entry.path);
  ev.dataTransfer.effectAllowed = 'move';
}

function onDragOver(ev: DragEvent) {
  if (!props.entry.is_dir) return;
  ev.preventDefault();
  ev.stopPropagation();
  if (ev.dataTransfer) {
    ev.dataTransfer.dropEffect =
      ev.dataTransfer.types.includes('Files') ? 'copy' : 'move';
  }
  dropHover.value = true;
}

function onDragLeave() {
  dropHover.value = false;
}

async function onDrop(ev: DragEvent) {
  if (!props.entry.is_dir) return;
  ev.preventDefault();
  ev.stopPropagation();
  dropHover.value = false;
  if (!ev.dataTransfer) return;
  const files = ev.dataTransfer.files;
  if (files && files.length > 0) {
    try {
      await uploadFiles(
        settings.agentUrl,
        settings.authToken,
        props.entry.path,
        Array.from(files),
        panel.scopeConversationId || undefined,
      );
      if (!expanded.value) await toggleExpand();
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn('Upload failed', e);
    }
    return;
  }
  const src = ev.dataTransfer.getData(INTERNAL_MIME);
  if (!src) return;
  const sep = pathSep();
  const dest = props.entry.path + sep + basename(src);
  if (dest === src || dest.startsWith(src + sep)) return;
  try {
    await moveEntry(
      settings.agentUrl,
      settings.authToken,
      src,
      dest,
      panel.scopeConversationId || undefined,
    );
  } catch (e) {
    // eslint-disable-next-line no-console
    console.warn('Move failed', e);
  }
}

// ---- programmatic upload ----

const fileInputRef = ref<HTMLInputElement | null>(null);
const uploadTargetDir = ref<string>('');

function triggerUpload(targetDir: string) {
  uploadTargetDir.value = targetDir;
  fileInputRef.value?.click();
}

async function onFileInputChange(ev: Event) {
  const input = ev.target as HTMLInputElement;
  const files = input.files ? Array.from(input.files) : [];
  input.value = '';
  if (!files.length) return;
  try {
    await uploadFiles(
      settings.agentUrl,
      settings.authToken,
      uploadTargetDir.value || props.entry.path,
      files,
      panel.scopeConversationId || undefined,
    );
    if (!expanded.value && props.entry.is_dir) await toggleExpand();
  } catch (e) {
    // eslint-disable-next-line no-console
    console.warn('Upload failed', e);
  }
}
</script>

<template>
  <li class="tree-node">
    <div
      class="tree-row"
      :class="{
        selected: !entry.is_dir && panel.selectedFilePath === entry.path,
        'drop-hover': dropHover,
      }"
      :style="{ paddingLeft: 4 + depth * 12 + 'px' }"
      :title="entry.path"
      :draggable="!renaming"
      @click="entry.is_dir ? toggleExpand() : handleFileClick()"
      @dblclick="handleFileDoubleClick"
      @contextmenu="openContextMenu"
      @dragstart="onDragStart"
      @dragover="onDragOver"
      @dragleave="onDragLeave"
      @drop="onDrop"
    >
      <span v-if="entry.is_dir" class="chevron" :class="{ expanded }">
        <Icon icon="mdi:chevron-right" />
      </span>
      <span v-else class="chevron-spacer" />
      <Icon class="file-icon" :icon="iconFor(entry, expanded)" />
      <input
        v-if="renaming"
        ref="renameInputRef"
        v-model="renameBuffer"
        class="rename-input"
        @click.stop
        @dblclick.stop
        @keydown.enter.prevent="commitRename"
        @keydown.escape.prevent="cancelRename"
        @blur="commitRename"
      />
      <span v-else class="name">{{ entry.name }}</span>
    </div>
    <FileContextMenu
      v-if="activeMenu"
      :x="activeMenu.x"
      :y="activeMenu.y"
      :items="menuItems()"
      @pick="handleMenuPick"
      @close="activeMenu = null"
    />
    <input
      ref="fileInputRef"
      type="file"
      multiple
      class="hidden-file-input"
      @change="onFileInputChange"
    />
    <ul v-if="expanded && entry.is_dir" class="tree-children">
      <li v-if="loading" class="tree-info" :style="{ paddingLeft: 4 + (depth + 1) * 12 + 'px' }">
        <Icon icon="mdi:loading" class="spinner" />
        <span>Loading…</span>
      </li>
      <li
        v-else-if="errorMessage"
        class="tree-info error"
        :style="{ paddingLeft: 4 + (depth + 1) * 12 + 'px' }"
      >
        {{ errorMessage }}
      </li>
      <template v-else>
        <FileTreeNode
          v-for="child in children"
          :key="child.path"
          :entry="child"
          :depth="depth + 1"
        />
        <li
          v-if="truncated"
          class="tree-info"
          :style="{ paddingLeft: 4 + (depth + 1) * 12 + 'px' }"
        >
          (truncated — &gt;2000 entries)
        </li>
      </template>
    </ul>
  </li>
</template>

<style scoped>
.tree-node {
  list-style: none;
  margin: 0;
  padding: 0;
}
.tree-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px 2px 4px;
  font-size: 0.82rem;
  color: var(--text-primary);
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
}
.tree-row:hover {
  background: var(--hover-bg);
}
.tree-row.selected {
  background: var(--primary-color);
  color: #fff;
}
.tree-row.drop-hover {
  outline: 1px dashed var(--primary-light);
  background: rgba(59, 130, 246, 0.18);
}
.rename-input {
  flex: 1 1 auto;
  min-width: 0;
  font: inherit;
  font-size: 0.82rem;
  background: var(--surface-color);
  color: var(--text-primary);
  border: 1px solid var(--primary-color);
  border-radius: 3px;
  padding: 0 4px;
  outline: none;
}
.hidden-file-input {
  display: none;
}
.chevron {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  color: var(--text-secondary);
  transition: transform 0.1s ease;
  flex-shrink: 0;
}
.chevron.expanded {
  transform: rotate(90deg);
}
.chevron-spacer {
  display: inline-block;
  width: 14px;
  flex-shrink: 0;
}
.file-icon {
  font-size: 1.05rem;
  flex-shrink: 0;
}
.name {
  overflow: hidden;
  text-overflow: ellipsis;
}
.tree-children {
  list-style: none;
  margin: 0;
  padding: 0;
}
.tree-info {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 6px;
  font-size: 0.78rem;
  font-style: italic;
  color: var(--text-secondary);
}
.tree-info.error {
  color: var(--danger-color);
}
.spinner {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
