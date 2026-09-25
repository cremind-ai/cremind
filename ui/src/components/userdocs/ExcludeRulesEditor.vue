<script setup lang="ts">
/**
 * The profile's exclude rules: what inside the folder the index leaves out
 * entirely (`skip`), or keeps findable by name, date and folder without
 * reading the content (`metadata_only` — for folders of private scans, say).
 *
 * Edits stay local until Save. Saving emits the whole list; the page sends it
 * through the confirm flow, because a new rule that matches files already in
 * the index removes them from it — the server answers with the count first.
 * `.gitignore`-style ignore files inside the folder apply on top of these.
 */
import { computed, ref, watch } from 'vue';
import {
  ElButton, ElInput, ElOption, ElSelect, ElTable, ElTableColumn,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import type { ExcludeMode, ExcludeRule, ExcludeType } from '../../services/userdocsApi';

const props = withDefaults(defineProps<{
  rules: ExcludeRule[];
  disabled?: boolean;
  saving?: boolean;
}>(), { disabled: false, saving: false });

const emit = defineEmits<{ save: [rules: ExcludeRule[]] }>();

interface Row extends ExcludeRule { key: number }

let nextKey = 0;
function toRows(rules: ExcludeRule[]): Row[] {
  return rules.map(r => ({ ...r, key: nextKey++ }));
}

const rows = ref<Row[]>(toRows(props.rules));

/** What would be saved: blank patterns dropped, the same normalisation the
 *  server applies to extensions (no leading dot or star, lower case). */
function cleaned(list: ExcludeRule[]): ExcludeRule[] {
  return list
    .map(r => ({
      pattern: r.type === 'ext'
        ? r.pattern.trim().toLowerCase().replace(/^\*/, '').replace(/^\./, '')
        : r.pattern.trim(),
      type: r.type,
      mode: r.mode,
    }))
    .filter(r => r.pattern);
}

const dirty = computed(() =>
  JSON.stringify(cleaned(rows.value)) !== JSON.stringify(cleaned(props.rules)));

// Saved rules coming back from the server replace the local copy — unless
// there are unsaved edits, which a background refresh must not wipe.
watch(() => props.rules, (next) => {
  if (!dirty.value) rows.value = toRows(next);
});

const TYPE_OPTIONS: { value: ExcludeType; label: string; placeholder: string }[] = [
  { value: 'dir', label: 'Folder', placeholder: 'Archive/Old projects' },
  { value: 'glob', label: 'Pattern', placeholder: '**/node_modules' },
  { value: 'ext', label: 'File type', placeholder: 'mp4' },
];
const MODE_OPTIONS: { value: ExcludeMode; label: string }[] = [
  { value: 'skip', label: 'Leave out' },
  { value: 'metadata_only', label: 'Name & details only' },
];

function placeholderFor(type: ExcludeType): string {
  return TYPE_OPTIONS.find(o => o.value === type)?.placeholder ?? '';
}

function addRule() {
  rows.value = [...rows.value, { pattern: '', type: 'dir', mode: 'skip', key: nextKey++ }];
}

function removeRule(key: number) {
  rows.value = rows.value.filter(r => r.key !== key);
}

function reset() {
  rows.value = toRows(props.rules);
}

function save() {
  if (!dirty.value) return;
  emit('save', cleaned(rows.value));
}
</script>

<template>
  <div class="excludes">
    <ElTable
      v-if="rows.length"
      :data="rows"
      row-key="key"
      size="small"
      class="excludes-table"
    >
      <ElTableColumn label="What" width="130">
        <template #default="{ row }">
          <ElSelect v-model="row.type" size="small" :disabled="disabled">
            <ElOption v-for="o in TYPE_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
          </ElSelect>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Matching">
        <template #default="{ row }">
          <ElInput
            v-model="row.pattern"
            size="small"
            :disabled="disabled"
            :placeholder="placeholderFor(row.type)"
          />
        </template>
      </ElTableColumn>
      <ElTableColumn label="Then" width="190">
        <template #default="{ row }">
          <ElSelect v-model="row.mode" size="small" :disabled="disabled">
            <ElOption v-for="o in MODE_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
          </ElSelect>
        </template>
      </ElTableColumn>
      <ElTableColumn width="48" align="center">
        <template #default="{ row }">
          <button
            type="button"
            class="remove"
            :disabled="disabled"
            title="Remove rule"
            @click="removeRule(row.key)"
          >
            <Icon icon="mdi:trash-can-outline" />
          </button>
        </template>
      </ElTableColumn>
    </ElTable>
    <p v-else class="hint">No rules — everything in the folder is indexed.</p>

    <p class="hint">
      A folder is relative to the indexed folder; a pattern uses <code>*</code> and <code>**</code>;
      a file type is an extension. <strong>Name &amp; details only</strong> keeps files findable by
      name, date and folder without reading what is in them. A rule that removes files already in
      the index asks you first.
    </p>

    <div class="excludes-actions">
      <ElButton size="small" :disabled="disabled" @click="addRule">
        <Icon icon="mdi:plus" class="btn-icon" /> Add rule
      </ElButton>
      <template v-if="dirty">
        <ElButton size="small" type="primary" :loading="saving" :disabled="disabled" @click="save">
          Save rules
        </ElButton>
        <ElButton size="small" :disabled="saving" @click="reset">Cancel</ElButton>
      </template>
    </div>
  </div>
</template>

<style scoped>
.excludes { display: flex; flex-direction: column; gap: 8px; }
.excludes-table { width: 100%; }
.hint { margin: 0; font-size: 0.78rem; color: var(--text-secondary); line-height: 1.45; }
.hint code {
  font-family: var(--font-mono, monospace); font-size: 0.78rem;
  background: var(--bg-color); padding: 0 4px; border-radius: 3px;
}
.hint strong { color: var(--text-primary); font-weight: 600; }
.excludes-actions { display: flex; gap: 8px; }
.excludes-actions :deep(.el-button + .el-button) { margin-left: 0; }
.btn-icon { margin-right: 4px; }
.remove {
  border: none; background: none; cursor: pointer; padding: 4px; line-height: 1;
  color: var(--text-secondary); font-size: 16px; border-radius: 4px;
}
.remove:hover:not(:disabled) { color: var(--danger-color); background: var(--hover-bg); }
.remove:disabled { cursor: not-allowed; opacity: 0.5; }
</style>
