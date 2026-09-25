<script setup lang="ts">
/**
 * The first sync of a large folder waits for the user
 * (`awaiting_confirmation(first_sync)`): before anything is read, a stat-only
 * walk counts what the sync would cost, and this dialog shows it — files by
 * type, photos to describe, how big the index would get against the storage
 * budget and the free disk, and roughly how long it takes.
 *
 * Start confirms (`control start`); Adjust exclusions sends the user to the
 * rules first; Cancel turns the feature back off (nothing was indexed, so
 * there is nothing to keep).
 */
import { computed } from 'vue';
import { ElButton, ElDialog } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { UserDocsEstimate, UserDocsStorageSnapshot } from '../../services/userdocsApi';
import { formatBytes, formatCount, formatDuration } from '../../utils/userdocsView';

const props = withDefaults(defineProps<{
  modelValue: boolean;
  estimate: UserDocsEstimate | null;
  storage?: UserDocsStorageSnapshot | null;
  /** Photos described per day (the profile's cap, else the admin default). */
  captionCap?: number | null;
  captionEnabled?: boolean;
  busy?: boolean;
}>(), { storage: null, captionCap: null, captionEnabled: true, busy: false });

const emit = defineEmits<{
  'update:modelValue': [open: boolean];
  start: [];
  adjust: [];
  'turn-off': [];
}>();

const KIND_LABELS: Record<string, string> = {
  text: 'Text', markdown: 'Markdown', code: 'Code', csv: 'CSV', json: 'JSON', xml: 'XML',
  html: 'Web pages', pdf: 'PDF', docx: 'Word', doc: 'Word (old format)', xlsx: 'Excel',
  xls: 'Excel (old format)', pptx: 'PowerPoint', ppt: 'PowerPoint (old format)', rtf: 'RTF',
  odt: 'OpenDocument text', ods: 'OpenDocument sheets', odp: 'OpenDocument slides',
  epub: 'E-books', eml: 'Emails', msg: 'Outlook messages', image: 'Images', audio: 'Audio',
  video: 'Video', archive: 'Archives', executable: 'Programs', database: 'Databases',
  font: 'Fonts', bundle: 'App bundles', encrypted: 'Encrypted documents', other: 'Other',
};

const byKind = computed(() =>
  Object.entries(props.estimate?.by_kind ?? {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([kind, n]) => ({ kind, label: KIND_LABELS[kind] ?? kind, n })));

const images = computed(() => props.estimate?.images_to_caption ?? 0);
const captionDays = computed(() => {
  const cap = props.captionCap ?? 0;
  return cap > 0 && images.value > 0 ? Math.ceil(images.value / cap) : null;
});

/** The budget that binds this profile: its own, else the shared one (what is
 *  left of it after every profile's indexes). */
const budgetLeft = computed(() => {
  const s = props.storage;
  if (!s) return null;
  if (s.profile_budget_bytes) return Math.max(0, s.profile_budget_bytes - (s.used_bytes ?? 0));
  if (s.budget_bytes) return Math.max(0, s.budget_bytes - (s.global_bytes ?? s.used_bytes ?? 0));
  return null;
});
const indexBytes = computed(() => props.estimate?.index_bytes ?? 0);
const overBudget = computed(() => budgetLeft.value != null && indexBytes.value > budgetLeft.value);
const overDisk = computed(() =>
  props.storage?.free_bytes != null && indexBytes.value > props.storage.free_bytes);
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    title="Start the first sync?"
    width="600px"
    :close-on-click-modal="!busy"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    append-to-body
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <template v-if="estimate && estimate.state === 'done'">
      <p class="est-lead">
        <strong>{{ formatCount(estimate.files) }} files</strong>
        ({{ formatBytes(estimate.bytes) }}) in
        <code>{{ estimate.root }}</code>.
        Nothing has been read yet.
      </p>

      <div class="est-grid">
        <section class="est-card">
          <h4>By type</h4>
          <ul class="est-kinds">
            <li v-for="row in byKind.slice(0, 10)" :key="row.kind">
              <span>{{ row.label }}</span><span class="num">{{ formatCount(row.n) }}</span>
            </li>
            <li v-if="byKind.length > 10" class="muted">
              <span>{{ byKind.length - 10 }} more types</span>
              <span class="num">{{ formatCount(byKind.slice(10).reduce((a, r) => a + r.n, 0)) }}</span>
            </li>
          </ul>
        </section>

        <section class="est-card">
          <h4>What it takes</h4>
          <dl class="est-facts">
            <dt><Icon icon="mdi:harddisk" /> Index size</dt>
            <dd :class="{ warn: overBudget || overDisk }">
              about {{ formatBytes(indexBytes) }}
              <span v-if="budgetLeft != null" class="sub">
                of {{ formatBytes(budgetLeft) }} left in the budget
              </span>
              <span v-if="storage?.free_bytes != null" class="sub">
                · {{ formatBytes(storage.free_bytes) }} free on disk
              </span>
            </dd>
            <dt><Icon icon="mdi:timer-sand" /> Time</dt>
            <dd>{{ formatDuration(estimate.seconds) }}, in the background</dd>
            <template v-if="images > 0">
              <dt><Icon icon="mdi:image-outline" /> Photos to describe</dt>
              <dd>
                {{ formatCount(images) }}
                <span v-if="!captionEnabled" class="sub">— describing photos is off; they are found by name, date and folder</span>
                <span v-else-if="captionDays && captionDays > 1" class="sub">
                  — about {{ captionDays }} days at {{ formatCount(captionCap) }} a day; until then they are
                  found by name, date and folder
                </span>
              </dd>
            </template>
            <template v-if="(estimate.placeholders ?? 0) > 0">
              <dt><Icon icon="mdi:cloud-off-outline" /> Cloud-only files</dt>
              <dd>
                {{ formatCount(estimate.placeholders) }}
                <span class="sub">— not downloaded to this computer, so indexed by name only</span>
              </dd>
            </template>
          </dl>
        </section>
      </div>

      <p v-if="overBudget || overDisk" class="est-warn">
        <Icon icon="mdi:alert-outline" />
        <span v-if="overDisk">
          The index would not fit on the disk. Exclude folders you don't need searched before starting.
        </span>
        <span v-else>
          This is more than the storage budget has left. Indexing stops at the budget — exclude
          folders you don't need searched, or ask your administrator for a bigger budget.
        </span>
      </p>
      <p class="est-note">
        Search works as soon as the first files are indexed; you can pause at any time.
      </p>
    </template>
    <p v-else-if="estimate && estimate.state === 'error'" class="est-warn">
      <Icon icon="mdi:alert-circle-outline" />
      <span>The estimate failed: {{ estimate.error || 'unknown error' }}</span>
    </p>
    <p v-else class="est-note">Counting files…</p>

    <template #footer>
      <div class="est-footer">
        <ElButton :disabled="busy" @click="emit('turn-off')">Cancel and turn off</ElButton>
        <span class="grow" />
        <ElButton :disabled="busy" @click="emit('adjust')">Adjust exclusions</ElButton>
        <ElButton
          type="primary"
          :loading="busy"
          :disabled="!estimate || estimate.state !== 'done'"
          @click="emit('start')"
        >
          Start sync
        </ElButton>
      </div>
    </template>
  </ElDialog>
</template>

<style scoped>
.est-lead { margin: 0 0 14px; font-size: 0.9rem; color: var(--text-primary); line-height: 1.5; }
.est-lead code {
  font-family: var(--font-mono, monospace); font-size: 0.8rem;
  background: var(--bg-color); padding: 1px 5px; border-radius: 4px; word-break: break-all;
}
.est-grid { display: grid; grid-template-columns: 1fr 1.4fr; gap: 12px; }
@media (max-width: 640px) { .est-grid { grid-template-columns: 1fr; } }
.est-card {
  border: 1px solid var(--border-color); border-radius: 8px; padding: 10px 12px;
  background: var(--bg-color);
}
.est-card h4 {
  margin: 0 0 8px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--text-tertiary);
}
.est-kinds { list-style: none; margin: 0; padding: 0; font-size: 0.85rem; }
.est-kinds li { display: flex; justify-content: space-between; gap: 8px; padding: 2px 0; color: var(--text-primary); }
.est-kinds li.muted { color: var(--text-secondary); }
.num { font-variant-numeric: tabular-nums; }
.est-facts { margin: 0; font-size: 0.85rem; }
.est-facts dt {
  display: flex; align-items: center; gap: 6px; color: var(--text-secondary);
  font-size: 0.78rem; margin-top: 8px;
}
.est-facts dt:first-child { margin-top: 0; }
.est-facts dd { margin: 2px 0 0 22px; color: var(--text-primary); line-height: 1.45; }
.est-facts dd.warn { color: var(--warning-color); }
.sub { color: var(--text-secondary); }
.est-warn {
  display: flex; gap: 8px; align-items: flex-start; margin: 14px 0 0;
  padding: 8px 10px; border-radius: 6px; font-size: 0.85rem; line-height: 1.45;
  background: color-mix(in srgb, var(--warning-color) 10%, var(--surface-color));
  border: 1px solid color-mix(in srgb, var(--warning-color) 40%, var(--border-color));
  color: var(--text-primary);
}
.est-warn :deep(svg) { color: var(--warning-color); flex: none; margin-top: 2px; }
.est-note { margin: 12px 0 0; font-size: 0.8rem; color: var(--text-secondary); }
.est-footer { display: flex; align-items: center; gap: 8px; }
.est-footer .grow { flex: 1; }
.est-footer :deep(.el-button + .el-button) { margin-left: 0; }
</style>
