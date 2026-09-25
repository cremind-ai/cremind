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
  <div class="ud-sources" :class="{ expanded }">
    <button
      type="button"
      class="ud-sources-toggle"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <Icon :icon="expanded ? 'mdi:chevron-down' : 'mdi:chevron-right'" class="ud-sources-chevron" />
      <Icon icon="mdi:bookmark-multiple-outline" class="ud-sources-icon" />
      <span class="ud-sources-title">Sources ({{ sources.length }})</span>
      <span v-if="unverified" class="ud-sources-warn">
        · {{ unverified }} not verified
      </span>
    </button>
    <ol v-if="expanded" class="ud-sources-list">
      <li v-for="row in rows" :key="row.token">
        <button
          type="button"
          class="ud-source"
          :class="`tone-${row.info.tone}`"
          :title="row.info.note || row.name"
          @click="emit('open', row.token)"
        >
          <span class="ud-source-n" :class="{ mismatch: !!row.info.quoteNote }">{{ row.n }}</span>
          <Icon :icon="row.icon" class="ud-source-icon" />
          <span class="ud-source-text">
            <span class="ud-source-name">{{ row.name }}</span>
            <span v-if="row.detail" class="ud-source-detail">{{ row.detail }}</span>
          </span>
          <Icon v-if="row.drive" icon="mdi:google-drive" class="ud-source-drive" title="Google Drive" />
          <span v-if="row.info.tone !== 'ok' && row.info.tone !== 'pending'" class="ud-source-status">
            {{ row.info.label }}
          </span>
          <span v-else-if="row.info.quoteNote" class="ud-source-status quote">Quote mismatch</span>
        </button>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.ud-sources {
  margin-top: 8px;
  border-top: 1px solid var(--border-color);
  padding-top: 6px;
  font-size: 0.78rem;
}

.ud-sources-toggle {
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
.ud-sources-toggle:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.ud-sources-chevron { font-size: 1rem; }
.ud-sources-icon { font-size: 0.95rem; }
.ud-sources-title { font-weight: 600; }
.ud-sources-warn { color: var(--warning-color); }

.ud-sources-list {
  list-style: none;
  margin: 4px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ud-source {
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
.ud-source:hover { background: var(--surface-hover); }

.ud-source-n {
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
.tone-warn .ud-source-n {
  color: var(--warning-color);
  background: color-mix(in srgb, var(--warning-color) 16%, transparent);
}
.tone-gone .ud-source-n {
  color: var(--text-tertiary);
  background: color-mix(in srgb, var(--text-tertiary) 14%, transparent);
  text-decoration: line-through;
}
.tone-pending .ud-source-n {
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--text-secondary) 12%, transparent);
}
.ud-source-n.mismatch {
  box-shadow: 0 0 0 1.5px var(--danger-color);
}

.ud-source-icon { flex-shrink: 0; font-size: 1rem; }

.ud-source-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}
.ud-source-name,
.ud-source-detail {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ud-source-name { font-weight: 500; }
.tone-gone .ud-source-name {
  color: var(--text-tertiary);
  text-decoration: line-through;
}
.ud-source-detail {
  font-size: 0.72rem;
  color: var(--text-tertiary);
}

.ud-source-drive {
  flex-shrink: 0;
  color: var(--text-tertiary);
}

.ud-source-status {
  flex-shrink: 0;
  font-size: 0.7rem;
  padding: 1px 6px;
  border-radius: 8px;
  color: var(--warning-color);
  background: color-mix(in srgb, var(--warning-color) 14%, transparent);
}
.tone-gone .ud-source-status {
  color: var(--text-tertiary);
  background: color-mix(in srgb, var(--text-tertiary) 12%, transparent);
}
.ud-source-status.quote {
  color: var(--danger-color);
  background: color-mix(in srgb, var(--danger-color) 12%, transparent);
}
</style>
