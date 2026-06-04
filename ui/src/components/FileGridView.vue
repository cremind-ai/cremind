<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { Icon } from '@iconify/vue';
import { useChatStore } from '../stores/chat';
import { useSettingsStore } from '../stores/settings';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import {
  deleteEntry,
  downloadFile,
  moveEntry,
  parentDir,
  uploadFiles,
  type DirectoryEntry,
} from '../services/filesApi';
import { iconFor } from '../utils/fileIcons';
import { openFileInNewTab } from '../utils/openFile';
import { useCwdNavigation } from '../composables/useCwdNavigation';
import FileContextMenu, {
  type FileContextAction,
  type FileContextMenuItem,
} from './FileContextMenu.vue';

const props = defineProps<{
  entries: DirectoryEntry[];
}>();

const settings = useSettingsStore();
const panel = useTerminalPanelStore();
const chat = useChatStore();
const { navigate } = useCwdNavigation();

const INTERNAL_MIME = 'application/x-cremind-path';

function pathSep(): string {
  return panel.cwd.includes('\\') ? '\\' : '/';
}

function statusMsg(msg: string) {
  panel.lastFileEvent && void panel.lastFileEvent;
  // Best-effort surfaced via console; the file watch will refresh the UI.
  // eslint-disable-next-line no-console
  console.warn('[FileGridView]', msg);
}

// ---- selection / open ----

function onTileClick(entry: DirectoryEntry) {
  panel.setSelectedFile(entry.is_dir ? null : entry.path);
}

async function onTileDblClick(entry: DirectoryEntry) {
  if (entry.is_dir) {
    const r = await navigate(entry.path);
    if (!r.ok) statusMsg(r.error || 'Failed to change directory');
    return;
  }
  try {
    await openFileInNewTab(
      settings.agentUrl,
      settings.authToken,
      entry.path,
      chat.activeConversationId || undefined,
    );
  } catch (e) {
    statusMsg((e as Error)?.message || 'Failed to open file');
  }
}

// ---- context menu ----

interface ActiveMenu {
  x: number;
  y: number;
  entry: DirectoryEntry | null; // null = empty-space click on the grid
}

const activeMenu = ref<ActiveMenu | null>(null);

function menuItemsFor(entry: DirectoryEntry | null): FileContextMenuItem[] {
  if (entry === null) {
    return [
      { action: 'upload', label: 'Upload here…', icon: 'mdi:upload' },
      { action: 'mkdir', label: 'New folder', icon: 'mdi:folder-plus-outline' },
      { action: 'refresh', label: 'Refresh', icon: 'mdi:refresh' },
    ];
  }
  if (entry.is_dir) {
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

function openContextMenu(ev: MouseEvent, entry: DirectoryEntry | null) {
  ev.preventDefault();
  ev.stopPropagation();
  activeMenu.value = { x: ev.clientX, y: ev.clientY, entry };
}

async function handleMenuPick(action: FileContextAction) {
  const menu = activeMenu.value;
  activeMenu.value = null;
  if (!menu) return;
  const entry = menu.entry;
  switch (action) {
    case 'download':
      if (entry && !entry.is_dir) {
        try {
          await downloadFile(
            settings.agentUrl,
            settings.authToken,
            entry.path,
            entry.name,
            chat.activeConversationId || undefined,
          );
        } catch (e) {
          statusMsg((e as Error)?.message || 'Download failed');
        }
      }
      break;
    case 'delete':
      if (entry) {
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
            chat.activeConversationId || undefined,
          );
        } catch (e) {
          statusMsg((e as Error)?.message || 'Delete failed');
        }
      }
      break;
    case 'rename':
      if (entry) startRename(entry);
      break;
    case 'upload':
      triggerUpload(entry && entry.is_dir ? entry.path : panel.cwd);
      break;
    case 'mkdir':
      // Best-effort — prompt is fine for v1.
      {
        const name = window.prompt('New folder name');
        if (!name) return;
        const target = entry && entry.is_dir ? entry.path : panel.cwd;
        const dest = target + pathSep() + name;
        try {
          const { mkdir } = await import('../services/filesApi');
          await mkdir(
            settings.agentUrl,
            settings.authToken,
            dest,
            chat.activeConversationId || undefined,
          );
        } catch (e) {
          statusMsg((e as Error)?.message || 'mkdir failed');
        }
      }
      break;
    case 'refresh':
      // No-op: the file watch SSE handles refresh, and toggling a re-fetch
      // here would require parent coordination. Left as a hook for future
      // explicit refresh.
      break;
  }
}

// ---- inline rename ----

const renamingPath = ref<string | null>(null);
const renameInputRef = ref<HTMLInputElement | null>(null);
const renameBuffer = ref('');

async function startRename(entry: DirectoryEntry) {
  renamingPath.value = entry.path;
  renameBuffer.value = entry.name;
  await nextTick();
  renameInputRef.value?.focus();
  renameInputRef.value?.select();
}

async function commitRename(entry: DirectoryEntry) {
  const newName = renameBuffer.value.trim();
  renamingPath.value = null;
  if (!newName || newName === entry.name) return;
  const dest = parentDir(entry.path) + pathSep() + newName;
  try {
    await moveEntry(
      settings.agentUrl,
      settings.authToken,
      entry.path,
      dest,
      chat.activeConversationId || undefined,
    );
  } catch (e) {
    statusMsg((e as Error)?.message || 'Rename failed');
  }
}

function cancelRename() {
  renamingPath.value = null;
}

// ---- drag and drop ----

interface DragState {
  hoverPath: string | null; // path of folder being hovered as drop target
}
const drag = ref<DragState>({ hoverPath: null });

function onDragStart(ev: DragEvent, entry: DirectoryEntry) {
  if (!ev.dataTransfer) return;
  ev.dataTransfer.setData(INTERNAL_MIME, entry.path);
  ev.dataTransfer.effectAllowed = 'move';
}

function onDragOverFolder(ev: DragEvent, entry: DirectoryEntry) {
  if (!entry.is_dir) return;
  ev.preventDefault();
  ev.stopPropagation();
  if (ev.dataTransfer) {
    ev.dataTransfer.dropEffect =
      ev.dataTransfer.types.includes('Files') ? 'copy' : 'move';
  }
  drag.value.hoverPath = entry.path;
}

function onDragLeaveFolder(_ev: DragEvent, entry: DirectoryEntry) {
  if (drag.value.hoverPath === entry.path) drag.value.hoverPath = null;
}

async function onDropOnFolder(ev: DragEvent, entry: DirectoryEntry) {
  // Always claim the drop so the panel-root handler doesn't re-fire (which
  // would double-upload, leaving "file.txt" + "file (1).txt"). Drops on a
  // file tile are rerouted to the current cwd.
  ev.preventDefault();
  ev.stopPropagation();
  drag.value.hoverPath = null;
  const target = entry.is_dir ? entry.path : panel.cwd;
  await handleDrop(ev, target);
}

async function onDropOnGrid(ev: DragEvent) {
  ev.preventDefault();
  ev.stopPropagation();
  drag.value.hoverPath = null;
  await handleDrop(ev, panel.cwd);
}

function onDragOverGrid(ev: DragEvent) {
  ev.preventDefault();
  ev.stopPropagation();
  if (ev.dataTransfer) {
    ev.dataTransfer.dropEffect =
      ev.dataTransfer.types.includes('Files') ? 'copy' : 'move';
  }
}

async function handleDrop(ev: DragEvent, targetDir: string) {
  if (!ev.dataTransfer) return;
  // External: OS files dropped in.
  if (ev.dataTransfer.files && ev.dataTransfer.files.length > 0) {
    const files = Array.from(ev.dataTransfer.files);
    try {
      await uploadFiles(
        settings.agentUrl,
        settings.authToken,
        targetDir,
        files,
        chat.activeConversationId || undefined,
      );
    } catch (e) {
      statusMsg((e as Error)?.message || 'Upload failed');
    }
    return;
  }
  // Internal: move within the tree.
  const src = ev.dataTransfer.getData(INTERNAL_MIME);
  if (!src) return;
  const sep = pathSep();
  const dest = targetDir.endsWith(sep)
    ? targetDir + basename(src)
    : targetDir + sep + basename(src);
  if (dest === src || dest.startsWith(src + sep)) return; // no-op / illegal
  try {
    await moveEntry(
      settings.agentUrl,
      settings.authToken,
      src,
      dest,
      chat.activeConversationId || undefined,
    );
  } catch (e) {
    statusMsg((e as Error)?.message || 'Move failed');
  }
}

function basename(p: string): string {
  const sepIdx = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return sepIdx === -1 ? p : p.slice(sepIdx + 1);
}

// ---- programmatic upload via hidden file input ----

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
      uploadTargetDir.value || panel.cwd,
      files,
      chat.activeConversationId || undefined,
    );
  } catch (e) {
    statusMsg((e as Error)?.message || 'Upload failed');
  }
}

const sortedEntries = computed(() => props.entries);

watch(() => panel.viewMode, () => {
  // Reset transient state when the user toggles back to list.
  drag.value.hoverPath = null;
  renamingPath.value = null;
  activeMenu.value = null;
});
</script>

<template>
  <div
    class="grid-root"
    @contextmenu.self.prevent="openContextMenu($event, null)"
    @dragover.prevent="onDragOverGrid"
    @drop.prevent="onDropOnGrid"
  >
    <div
      v-for="entry in sortedEntries"
      :key="entry.path"
      class="grid-tile"
      :class="{
        selected: !entry.is_dir && panel.selectedFilePath === entry.path,
        'drop-hover': drag.hoverPath === entry.path,
      }"
      :title="entry.path"
      :draggable="renamingPath !== entry.path"
      @click.stop="onTileClick(entry)"
      @dblclick.stop="onTileDblClick(entry)"
      @contextmenu.stop="openContextMenu($event, entry)"
      @dragstart="onDragStart($event, entry)"
      @dragover="onDragOverFolder($event, entry)"
      @dragleave="onDragLeaveFolder($event, entry)"
      @drop="onDropOnFolder($event, entry)"
    >
      <Icon class="tile-icon" :icon="iconFor(entry, false)" />
      <input
        v-if="renamingPath === entry.path"
        ref="renameInputRef"
        v-model="renameBuffer"
        class="tile-rename"
        @click.stop
        @dblclick.stop
        @keydown.enter.prevent="commitRename(entry)"
        @keydown.escape.prevent="cancelRename"
        @blur="commitRename(entry)"
      />
      <span v-else class="tile-name">{{ entry.name }}</span>
    </div>

    <FileContextMenu
      v-if="activeMenu"
      :x="activeMenu.x"
      :y="activeMenu.y"
      :items="menuItemsFor(activeMenu.entry)"
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
  </div>
</template>

<style scoped>
.grid-root {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
  gap: 4px;
  padding: 8px;
  align-content: start;
  min-height: 100%;
}
.grid-tile {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 8px 4px;
  border-radius: 4px;
  cursor: pointer;
  text-align: center;
  user-select: none;
  outline: 1px solid transparent;
  color: var(--text-primary);
}
.grid-tile:hover {
  background: var(--hover-bg);
}
.grid-tile.selected {
  background: var(--primary-color);
  color: #fff;
}
.grid-tile.drop-hover {
  outline: 1px dashed var(--primary-light);
  background: rgba(59, 130, 246, 0.15);
}
.tile-icon {
  font-size: 36px;
  flex-shrink: 0;
}
.tile-name {
  font-size: 0.74rem;
  line-height: 1.15;
  word-break: break-word;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  max-width: 100%;
}
.tile-rename {
  width: 100%;
  font-size: 0.74rem;
  background: var(--surface-color);
  color: var(--text-primary);
  border: 1px solid var(--primary-color);
  border-radius: 3px;
  padding: 1px 3px;
  text-align: center;
  outline: none;
}
.hidden-file-input {
  display: none;
}
</style>
