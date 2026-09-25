<script setup lang="ts">
/**
 * "Sources (n)" under an answer that cites the user's documents: one row per
 * distinct citation, numbered like the chips in the text, each opening the
 * viewer. Collapsed by default — the chips already carry the numbers — but the
 * header says how many sources could not be verified, so a reader does not
 * have to expand it to learn the answer leans on something shaky.
 */
import { computed, ref } from 'vue';
import { Icon } from '@iconify/vue';
import { citationStatusInfo, type CitationSource } from '../../utils/citations';
import { iconFor } from '../../utils/fileIcons';

const props = defineProps<{ sources: CitationSource[] }>();
const emit = defineEmits<{ (e: 'open', token: string): void }>();

const expanded = ref(false);

const rows = computed(() => props.sources.map(src => {
  const info = citationStatusInfo(src.item);
  const file = src.item?.file;
  return {
    ...src,
    info,
    name: file?.name || file?.rel_path || 'Unresolved source',
    detail: [src.item?.locator_label, file?.rel_path && file.rel_path !== file.name ? file.rel_path : '']
      .filter(Boolean)
      .join(' · '),
    icon: file ? iconFor({ name: file.name || file.rel_path, is_dir: file.kind === 'folder' }) : 'mdi:file-question-outline',
    drive: file?.source === 'drive',
  };
}));

// Only resolved items count: "not checked yet" is not "failed the check".
const unverified = computed(
  () => rows.value.filter(r => r.info.tone === 'warn' || r.info.tone === 'gone' || r.info.quoteNote).length,
);
</script>

<template>
  <div class="doc-sources" :class="{ expanded }">
    <button
      type="button"
      class="doc-sources-toggle"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <Icon :icon="expanded ? 'mdi:chevron-down' : 'mdi:chevron-right'" class="doc-sources-chevron" />
      <Icon icon="mdi:bookmark-multiple-outline" class="doc-sources-icon" />
      <span class="doc-sources-title">Sources ({{ sources.length }})</span>
      <span v-if="unverified" class="doc-sources-warn">
        · {{ unverified }} not verified
      </span>
    </button>
    <ol v-if="expanded" class="doc-sources-list">
      <li v-for="row in rows" :key="row.token">
        <button
          type="button"
          class="doc-source"
          :class="`tone-${row.info.tone}`"
          :title="row.info.note || row.name"
          @click="emit('open', row.token)"
        >
          <span class="doc-source-n" :class="{ mismatch: !!row.info.quoteNote }">{{ row.n }}</span>
          <Icon :icon="row.icon" class="doc-source-icon" />
          <span class="doc-source-text">
            <span class="doc-source-name">{{ row.name }}</span>
            <span v-if="row.detail" class="doc-source-detail">{{ row.detail }}</span>
          </span>
          <Icon v-if="row.drive" icon="mdi:google-drive" class="doc-source-drive" title="Google Drive" />
          <span v-if="row.info.tone !== 'ok' && row.info.tone !== 'pending'" class="doc-source-status">
            {{ row.info.label }}
          </span>
          <span v-else-if="row.info.quoteNote" class="doc-source-status quote">Quote mismatch</span>
        </button>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.doc-sources {
  margin-top: 8px;
  border-top: 1px solid var(--border-color);
  padding-top: 6px;
  font-size: 0.78rem;
}

.doc-sources-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 4px;
  margin-left: -4px;
  border: none;
  border-radius: 6px;
  background: none;
  color: var(--text-secondary);
  font: inherit;
  cursor: pointer;
}
.doc-sources-toggle:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.doc-sources-chevron { font-size: 1rem; }
.doc-sources-icon { font-size: 0.95rem; }
.doc-sources-title { font-weight: 600; }
.doc-sources-warn { color: var(--warning-color); }

.doc-sources-list {
  list-style: none;
  margin: 4px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.doc-source {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 4px 6px;
  border: none;
  border-radius: 6px;
  background: none;
  color: var(--text-primary);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.doc-source:hover { background: var(--surface-hover); }

.doc-source-n {
  flex-shrink: 0;
  min-width: 18px;
  height: 18px;
  padding: 0 4px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 0.68rem;
  font-weight: 600;
  color: var(--primary-color);
  background: color-mix(in srgb, var(--primary-color) 14%, transparent);
}
.tone-warn .doc-source-n {
  color: var(--warning-color);
  background: color-mix(in srgb, var(--warning-color) 16%, transparent);
}
.tone-gone .doc-source-n {
  color: var(--text-tertiary);
  background: color-mix(in srgb, var(--text-tertiary) 14%, transparent);
  text-decoration: line-through;
}
.tone-pending .doc-source-n {
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--text-secondary) 12%, transparent);
}
.doc-source-n.mismatch {
  box-shadow: 0 0 0 1.5px var(--danger-color);
}

.doc-source-icon { flex-shrink: 0; font-size: 1rem; }

.doc-source-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}
.doc-source-name,
.doc-source-detail {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.doc-source-name { font-weight: 500; }
.tone-gone .doc-source-name {
  color: var(--text-tertiary);
  text-decoration: line-through;
}
.doc-source-detail {
  font-size: 0.72rem;
  color: var(--text-tertiary);
}

.doc-source-drive {
  flex-shrink: 0;
  color: var(--text-tertiary);
}

.doc-source-status {
  flex-shrink: 0;
  font-size: 0.7rem;
  padding: 1px 6px;
  border-radius: 8px;
  color: var(--warning-color);
  background: color-mix(in srgb, var(--warning-color) 14%, transparent);
}
.tone-gone .doc-source-status {
  color: var(--text-tertiary);
  background: color-mix(in srgb, var(--text-tertiary) 12%, transparent);
}
.doc-source-status.quote {
  color: var(--danger-color);
  background: color-mix(in srgb, var(--danger-color) 12%, transparent);
}
</style>
