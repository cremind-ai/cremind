<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { Icon } from '@iconify/vue';
import { useChatStore } from '../stores/chat';
import { useSettingsStore } from '../stores/settings';
import { useTerminalPanelStore } from '../stores/terminalPanel';
import {
  listDirectory,
  type DirectoryEntry,
} from '../services/filesApi';
import { useCwdNavigation } from '../composables/useCwdNavigation';

interface Segment {
  label: string;
  path: string;
}

const panel = useTerminalPanelStore();
const settings = useSettingsStore();
const chat = useChatStore();
const { navigate } = useCwdNavigation();

function detectSep(p: string): string {
  return p && p.includes('\\') ? '\\' : '/';
}

// ── Breadcrumb segments (display) ─────────────────────────────────

const segments = computed<Segment[]>(() => {
  const cwd = panel.cwd || '';
  if (!cwd) return [];
  const sep = detectSep(cwd);
  const parts = cwd.split(/[\\/]/g);
  const out: Segment[] = [];
  let acc = '';
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    if (i === 0) {
      if (part === '' && sep === '/') {
        acc = '/';
        out.push({ label: '/', path: '/' });
      } else if (part) {
        acc = part + sep;
        out.push({ label: part, path: acc });
      }
      continue;
    }
    if (!part) continue;
    acc = acc.endsWith(sep) ? acc + part : acc + sep + part;
    out.push({ label: part, path: acc });
  }
  return out;
});

const upPath = computed<string | null>(() => {
  const segs = segments.value;
  if (segs.length <= 1) return null;
  return segs[segs.length - 2].path;
});

// ── Edit mode ─────────────────────────────────────────────────────

const editing = ref(false);
const editValue = ref('');
const editInputRef = ref<HTMLInputElement | null>(null);

async function startEdit() {
  if (editing.value) return;
  editValue.value = panel.cwd || '';
  editing.value = true;
  await refreshSuggestions();
  await nextTick();
  editInputRef.value?.focus();
  editInputRef.value?.select();
}

function cancelEdit() {
  editing.value = false;
  closeDropdown();
}

async function commitEdit() {
  const target = editValue.value.trim();
  editing.value = false;
  closeDropdown();
  if (!target) return;
  const r = await navigate(target);
  if (!r.ok) showError(r.error || 'Failed to change directory');
}

// ── Suggestions / dropdown ────────────────────────────────────────

const dropdownOpen = ref(false); // user-toggled "browse children" dropdown
const dropdownEntries = ref<DirectoryEntry[]>([]);
const dropdownLoading = ref(false);
const dropdownError = ref<string | null>(null);
const dropdownIndex = ref(-1);
let dropdownAbortCtl: AbortController | null = null;

const dropdownVisible = computed(
  () => editing.value || dropdownOpen.value,
);

// In edit mode, parse the typed text into <parent dir> + <fragment>. The
// dropdown lists children of the parent, narrowed by the fragment — so
// the user can paste a partial path and finish via autocomplete.
const editParentAndFragment = computed<{ parent: string; fragment: string }>(() => {
  const v = editValue.value;
  if (!v) return { parent: panel.cwd, fragment: '' };
  const idx = Math.max(v.lastIndexOf('/'), v.lastIndexOf('\\'));
  if (idx === -1) return { parent: panel.cwd, fragment: v };
  let parent = v.slice(0, idx);
  if (!parent) parent = '/'; // POSIX root slash
  return { parent, fragment: v.slice(idx + 1) };
});

const filteredEntries = computed<DirectoryEntry[]>(() => {
  const dirs = dropdownEntries.value.filter(e => e.is_dir);
  if (!editing.value) return dirs;
  const q = editParentAndFragment.value.fragment.toLowerCase();
  if (!q) return dirs;
  return dirs.filter(e => e.name.toLowerCase().includes(q));
});

async function refreshSuggestions() {
  const listPath = editing.value
    ? editParentAndFragment.value.parent || panel.cwd
    : panel.cwd;
  if (!listPath) {
    dropdownEntries.value = [];
    return;
  }
  dropdownAbortCtl?.abort();
  dropdownAbortCtl = new AbortController();
  dropdownLoading.value = true;
  dropdownError.value = null;
  try {
    const data = await listDirectory(
      settings.agentUrl,
      settings.authToken,
      listPath,
      panel.showHiddenFiles,
      dropdownAbortCtl.signal,
      chat.activeConversationId || undefined,
    );
    dropdownEntries.value = data.entries;
    dropdownIndex.value = -1;
  } catch (e: unknown) {
    if ((e as Error)?.name === 'AbortError') return;
    dropdownEntries.value = [];
    dropdownError.value = (e as Error)?.message || 'Failed to load';
  } finally {
    dropdownLoading.value = false;
  }
}

function toggleChildrenDropdown() {
  if (dropdownOpen.value) {
    closeDropdown();
    return;
  }
  dropdownOpen.value = true;
  refreshSuggestions();
}

function closeDropdown() {
  dropdownOpen.value = false;
  dropdownAbortCtl?.abort();
}

// Re-fetch suggestions when the typed parent crosses a separator.
let lastListedParent = '';
watch(editValue, () => {
  if (!editing.value) return;
  const parent = editParentAndFragment.value.parent;
  if (parent !== lastListedParent) {
    lastListedParent = parent;
    refreshSuggestions();
  }
});

// ── Picking entries ──────────────────────────────────────────────

async function pickSegment(seg: Segment) {
  closeDropdown();
  const r = await navigate(seg.path);
  if (!r.ok) showError(r.error || 'Failed to change directory');
}

async function pickEntry(entry: DirectoryEntry) {
  if (editing.value) {
    editValue.value = entry.path;
    editing.value = false;
    closeDropdown();
    const r = await navigate(entry.path);
    if (!r.ok) showError(r.error || 'Failed to change directory');
    return;
  }
  closeDropdown();
  const r = await navigate(entry.path);
  if (!r.ok) showError(r.error || 'Failed to change directory');
}

async function goUp() {
  if (!upPath.value) return;
  closeDropdown();
  const r = await navigate(upPath.value);
  if (!r.ok) showError(r.error || 'Failed to change directory');
}

// ── Keyboard nav (edit mode) ─────────────────────────────────────

function onInputKey(ev: KeyboardEvent) {
  const list = filteredEntries.value;
  if (ev.key === 'Enter') {
    ev.preventDefault();
    if (dropdownIndex.value >= 0 && list[dropdownIndex.value]) {
      pickEntry(list[dropdownIndex.value]);
    } else {
      commitEdit();
    }
  } else if (ev.key === 'Escape') {
    ev.preventDefault();
    cancelEdit();
  } else if (ev.key === 'ArrowDown') {
    ev.preventDefault();
    if (list.length === 0) return;
    dropdownIndex.value = (dropdownIndex.value + 1) % list.length;
  } else if (ev.key === 'ArrowUp') {
    ev.preventDefault();
    if (list.length === 0) return;
    dropdownIndex.value =
      (dropdownIndex.value - 1 + list.length) % list.length;
  } else if (ev.key === 'Tab') {
    const idx = dropdownIndex.value >= 0 ? dropdownIndex.value : 0;
    const entry = list[idx];
    if (entry) {
      ev.preventDefault();
      // Replace the trailing fragment with the selected entry's path so
      // the user can keep tabbing deeper.
      const sep = detectSep(entry.path);
      editValue.value = entry.path + (entry.is_dir ? sep : '');
    }
  }
}

// ── Toast ────────────────────────────────────────────────────────

const errorToast = ref<string | null>(null);
let errorToastTimer: ReturnType<typeof setTimeout> | null = null;

function showError(msg: string) {
  errorToast.value = msg;
  if (errorToastTimer) clearTimeout(errorToastTimer);
  errorToastTimer = setTimeout(() => {
    errorToast.value = null;
    errorToastTimer = null;
  }, 4000);
}

// ── Outside click / global key ───────────────────────────────────

function onWindowMouseDown(ev: MouseEvent) {
  if (!editing.value && !dropdownOpen.value) return;
  const root = (ev.target as HTMLElement)?.closest('.cwd-breadcrumb-root');
  if (root) return;
  if (editing.value) cancelEdit();
  if (dropdownOpen.value) closeDropdown();
}

watch([editing, dropdownOpen], ([e, d]) => {
  if (e || d) {
    window.addEventListener('mousedown', onWindowMouseDown, true);
  } else {
    window.removeEventListener('mousedown', onWindowMouseDown, true);
  }
});

onBeforeUnmount(() => {
  window.removeEventListener('mousedown', onWindowMouseDown, true);
  dropdownAbortCtl?.abort();
  if (errorToastTimer) clearTimeout(errorToastTimer);
});
</script>

<template>
  <div class="cwd-breadcrumb-root">
    <button
      class="bc-icon-btn"
      title="Go up"
      :disabled="!upPath"
      @click="goUp"
    >
      <Icon icon="mdi:arrow-up" />
    </button>

    <div
      class="bc-bar"
      :class="{ editing }"
      :title="editing ? '' : 'Click to type or paste a path'"
      @click="!editing && startEdit()"
    >
      <Icon
        v-if="!editing"
        icon="mdi:folder-outline"
        class="bc-folder-icon"
      />
      <template v-if="!editing">
        <template v-if="segments.length === 0">
          <span class="bc-empty">—</span>
        </template>
        <template v-else>
          <template v-for="(seg, idx) in segments" :key="seg.path">
            <button
              class="bc-segment"
              :class="{ last: idx === segments.length - 1 }"
              :title="seg.path"
              @click.stop="pickSegment(seg)"
            >{{ seg.label }}</button>
            <span v-if="idx < segments.length - 1" class="bc-sep">›</span>
          </template>
        </template>
      </template>
      <input
        v-else
        ref="editInputRef"
        v-model="editValue"
        class="bc-input"
        spellcheck="false"
        autocapitalize="off"
        autocomplete="off"
        placeholder="Type or paste a directory path…"
        @keydown="onInputKey"
        @click.stop
      />
    </div>

    <button
      v-if="!editing"
      class="bc-icon-btn"
      title="Browse subdirectories"
      :class="{ active: dropdownOpen }"
      @click.stop="toggleChildrenDropdown"
    >
      <Icon icon="mdi:menu-down" />
    </button>
    <button
      v-else
      class="bc-icon-btn"
      title="Cancel (Esc)"
      @click.stop="cancelEdit"
    >
      <Icon icon="mdi:close" />
    </button>

    <div
      v-if="dropdownVisible"
      class="bc-popover"
      @mousedown.stop
      @click.stop
    >
      <div v-if="dropdownLoading" class="bc-status">
        <Icon icon="mdi:loading" class="spin" /> Loading…
      </div>
      <div v-else-if="dropdownError" class="bc-status error">
        {{ dropdownError }}
      </div>
      <div
        v-else-if="filteredEntries.length === 0"
        class="bc-status"
      >No subdirectories</div>
      <button
        v-for="(entry, idx) in filteredEntries"
        :key="entry.path"
        class="bc-entry"
        :class="{ selected: idx === dropdownIndex }"
        :title="entry.path"
        @click="pickEntry(entry)"
        @mouseenter="dropdownIndex = idx"
      >
        <Icon icon="vscode-icons:default-folder" class="bc-entry-icon" />
        <span class="bc-entry-name">{{ entry.name }}</span>
      </button>
    </div>

    <div v-if="errorToast" class="bc-toast">{{ errorToast }}</div>
  </div>
</template>

<style scoped>
.cwd-breadcrumb-root {
  position: relative;
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}
.bc-icon-btn {
  background: transparent;
  border: none;
  color: var(--text-secondary);
  padding: 3px 4px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  border-radius: 3px;
  font-size: 0.95rem;
  flex-shrink: 0;
}
.bc-icon-btn:hover:not(:disabled) {
  color: var(--text-primary);
  background: var(--hover-bg);
}
.bc-icon-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.bc-icon-btn.active {
  color: var(--text-primary);
  background: var(--hover-bg);
}

.bc-bar {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  gap: 0;
  min-width: 0;
  height: 24px;
  padding: 0 6px;
  background: var(--bg-color);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  cursor: text;
  overflow: hidden;
  font-family: Consolas, Monaco, monospace;
  font-size: 0.78rem;
}
.bc-bar:hover:not(.editing) {
  border-color: var(--border-hover);
  background: var(--surface-color);
}
.bc-bar.editing {
  border-color: var(--primary-color);
  background: var(--bg-color);
  cursor: text;
}
.bc-folder-icon {
  font-size: 0.95rem;
  color: var(--text-tertiary);
  flex-shrink: 0;
  margin-right: 4px;
}
.bc-empty {
  color: var(--text-tertiary);
  font-style: italic;
}
.bc-segment {
  background: transparent;
  border: none;
  color: var(--text-secondary);
  font-family: inherit;
  font-size: inherit;
  padding: 1px 4px;
  cursor: pointer;
  border-radius: 2px;
  flex-shrink: 0;
  white-space: nowrap;
}
.bc-segment:hover {
  background: var(--hover-bg);
  color: var(--text-primary);
}
.bc-segment.last {
  color: var(--text-primary);
  font-weight: 500;
}
.bc-sep {
  color: var(--text-tertiary);
  flex-shrink: 0;
  user-select: none;
  padding: 0 1px;
}
.bc-input {
  flex: 1 1 auto;
  min-width: 0;
  background: transparent;
  border: none;
  outline: none;
  color: var(--text-primary);
  font-family: inherit;
  font-size: inherit;
  padding: 0;
}
.bc-input::placeholder {
  color: var(--text-tertiary);
}

.bc-popover {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  margin-top: 4px;
  z-index: 50;
  max-height: 320px;
  display: flex;
  flex-direction: column;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
  overflow: auto;
  padding: 4px 0;
}
[data-theme="dark"] .bc-popover {
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
}
.bc-status {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font-size: 0.78rem;
  color: var(--text-secondary);
  font-style: italic;
}
.bc-status.error {
  color: var(--danger-color);
}
.bc-entry {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 4px 10px;
  background: transparent;
  border: none;
  color: var(--text-primary);
  font-size: 0.82rem;
  text-align: left;
  cursor: pointer;
  font-family: inherit;
}
.bc-entry:hover,
.bc-entry.selected {
  background: var(--hover-bg);
}
.bc-entry-icon {
  font-size: 1rem;
  flex-shrink: 0;
}
.bc-entry-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.spin {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
.bc-toast {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 4px;
  z-index: 60;
  padding: 6px 10px;
  background: var(--danger-color);
  color: #fff;
  border-radius: 4px;
  font-size: 0.78rem;
  font-family: system-ui, sans-serif;
  max-width: 280px;
}
</style>
