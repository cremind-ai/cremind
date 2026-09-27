<script setup lang="ts">
/**
 * "Sources (n)" under an answer that cites the user's documents: one entry per
 * cited file, holding every citation of it — numbered like the chips in the
 * text, each opening the viewer at its own passage — so three passages of one
 * contract read as one document, not three. Collapsed by default — the chips
 * already carry the numbers — but the header says how many citations could not
 * be verified, so a reader does not have to expand it to learn the answer
 * leans on something shaky.
 */
import { computed, ref } from 'vue';
import { Icon } from '@iconify/vue';
import {
  citationStatusInfo,
  groupCitationSources,
  type CitationSource,
  type CitationStatusInfo,
} from '../../utils/citations';
import { iconFor } from '../../utils/fileIcons';

const props = defineProps<{ sources: CitationSource[] }>();
const emit = defineEmits<{ (e: 'open', token: string): void }>();

const expanded = ref(false);

interface Flag {
  text: string;
  kind: 'warn' | 'gone' | 'quote';
  note: string;
}

/** What a citation's status adds to the list: a status that is not plainly
 *  fine, else a quote that does not match its source, else nothing. */
function flagOf(info: CitationStatusInfo): Flag | null {
  if (info.tone === 'warn' || info.tone === 'gone') return { text: info.label, kind: info.tone, note: info.note };
  return info.quoteNote ? { text: 'Quote mismatch', kind: 'quote', note: info.quoteNote } : null;
}

function folderOf(relPath: string): string {
  const rel = relPath.replace(/\/+$/, '');
  return rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
}

const entries = computed(() => groupCitationSources(props.sources).map(group => {
  const cites = group.sources.map(src => {
    const info = citationStatusInfo(src.item);
    return {
      token: src.token,
      n: src.n,
      info,
      label: src.item?.locator_label || '',
      flag: flagOf(info),
      title: [info.note, info.quoteNote].filter(Boolean).join(' ') || undefined,
    };
  });
  // A flag every citation shares — the file was removed, or it is cited once —
  // is said once, on the file; flags that differ stay on their citations.
  const first = cites[0].flag;
  const shared = cites.every(c => c.flag?.text === first?.text) ? first : null;
  const file = group.file;
  const relPath = file?.rel_path || '';
  return {
    key: group.citeId,
    firstToken: cites[0].token,
    cites: shared ? cites.map(c => ({ ...c, flag: null })) : cites,
    flag: shared,
    gone: cites.every(c => c.info.tone === 'gone'),
    name: file?.name || relPath || 'Unresolved source',
    folder: folderOf(relPath),
    relPath,
    icon: file ? iconFor({ name: file.name || relPath, is_dir: file.kind === 'folder' }) : 'mdi:file-question-outline',
    drive: file?.source === 'drive',
  };
}));

// Only resolved items count: "not checked yet" is not "failed the check".
const unverified = computed(() => props.sources.filter(src => {
  const info = citationStatusInfo(src.item);
  return info.tone === 'warn' || info.tone === 'gone' || !!info.quoteNote;
}).length);
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
      <span class="doc-sources-title">Sources ({{ entries.length }})</span>
      <span v-if="sources.length > entries.length">· {{ sources.length }} citations</span>
      <span v-if="unverified" class="doc-sources-warn">
        · {{ unverified }} not verified
      </span>
    </button>
    <ul v-if="expanded" class="doc-sources-list">
      <li v-for="entry in entries" :key="entry.key" class="doc-source" :class="{ gone: entry.gone }">
        <!-- The file opens at its first citation; each number opens its own. -->
        <button
          type="button"
          class="doc-source-head"
          :title="entry.relPath || entry.name"
          @click="emit('open', entry.firstToken)"
        >
          <Icon :icon="entry.icon" class="doc-source-icon" />
          <span class="doc-source-text">
            <span class="doc-source-name">{{ entry.name }}</span>
            <span v-if="entry.folder" class="doc-source-folder">{{ entry.folder }}</span>
          </span>
          <Icon v-if="entry.drive" icon="mdi:google-drive" class="doc-source-drive" title="Google Drive" />
          <span v-if="entry.flag" class="doc-source-status" :class="entry.flag.kind" :title="entry.flag.note">
            {{ entry.flag.text }}
          </span>
        </button>
        <div class="doc-source-cites">
          <button
            v-for="cite in entry.cites"
            :key="cite.token"
            type="button"
            class="doc-source-cite"
            :class="[`tone-${cite.info.tone}`, { bare: !cite.label && !cite.flag }]"
            :title="cite.title"
            :aria-label="[`Source ${cite.n}`, cite.label, cite.flag?.text].filter(Boolean).join(', ')"
            @click="emit('open', cite.token)"
          >
            <span class="doc-source-n" :class="{ mismatch: !!cite.info.quoteNote }">{{ cite.n }}</span>
            <span v-if="cite.label" class="doc-source-loc">{{ cite.label }}</span>
            <span v-if="cite.flag" class="doc-source-flag" :class="cite.flag.kind">{{ cite.flag.text }}</span>
          </button>
        </div>
      </li>
    </ul>
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
  gap: 4px;
}

.doc-source {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.doc-source-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 3px 6px;
  border: none;
  border-radius: 6px;
  background: none;
  color: var(--text-primary);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.doc-source-head:hover { background: var(--surface-hover); }

.doc-source-icon { flex-shrink: 0; font-size: 1rem; }

/* The name keeps its room; the folder after it takes what is left. */
.doc-source-text {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.doc-source-name,
.doc-source-folder {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.doc-source-name { flex: 0 1 auto; font-weight: 500; }
.doc-source-folder {
  flex: 1 1 0;
  font-size: 0.72rem;
  color: var(--text-tertiary);
}
.doc-source.gone .doc-source-name {
  color: var(--text-tertiary);
  text-decoration: line-through;
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
.doc-source-status.gone {
  color: var(--text-tertiary);
  background: color-mix(in srgb, var(--text-tertiary) 12%, transparent);
}
.doc-source-status.quote {
  color: var(--danger-color);
  background: color-mix(in srgb, var(--danger-color) 12%, transparent);
}

/* The file's citations, under its name: the head's padding, icon and gap. */
.doc-source-cites {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding-left: calc(6px + 1rem + 8px);
}

.doc-source-cite {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 100%;
  padding: 1px 8px 1px 1px;
  border: 1px solid var(--border-color);
  border-radius: 11px;
  background: none;
  color: var(--text-secondary);
  font: inherit;
  font-size: 0.72rem;
  cursor: pointer;
}
.doc-source-cite.bare { padding-right: 1px; }
.doc-source-cite:hover,
.doc-source-cite:focus-visible {
  outline: none;
  border-color: color-mix(in srgb, var(--primary-color) 55%, transparent);
  background: var(--surface-hover);
  color: var(--text-primary);
}

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

.doc-source-loc {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tone-gone .doc-source-loc { text-decoration: line-through; }

.doc-source-flag { flex-shrink: 0; font-weight: 500; }
.doc-source-flag.warn { color: var(--warning-color); }
.doc-source-flag.gone { color: var(--text-tertiary); }
.doc-source-flag.quote { color: var(--danger-color); }
</style>
