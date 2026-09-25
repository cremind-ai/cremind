<script setup lang="ts">
/**
 * Image descriptions: photos and scanned PDF pages are described by the
 * profile's Specialized Vision Model (Settings → LLM Providers) so they can be
 * found by what they show. Never the main model — without a vision model,
 * images stay findable by name, folder, date and camera only.
 *
 * Nothing is sent before the user allows it, and the permission names one
 * model: when the vision model changes, the section asks again. The consent
 * button sends the "provider/model" it showed; the server refuses if that is
 * no longer the model (VisionModelChanged), so a switch between reading and
 * clicking can't slip through.
 *
 * The limits (daily cap, smallest image) are edited locally and saved
 * together; turning descriptions on or off saves at once.
 */
import { computed, ref, watch } from 'vue';
import { ElButton, ElInputNumber, ElSwitch } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { DocumentsOptions, DocumentsVisionView } from '../../services/documentsApi';
import { formatCount, visionQuota, visionStatus } from '../../utils/documentsView';

type CaptionOptions = DocumentsOptions['caption'];

const props = withDefaults(defineProps<{
  caption: CaptionOptions;
  vision: DocumentsVisionView | null | undefined;
  /** The admin's default daily cap, used while the profile has none. */
  defaultCap: number;
  /** Images waiting for a description (from the live snapshot). */
  waiting?: number | null;
  saving?: boolean;
  busy?: boolean;
}>(), { waiting: null, saving: false, busy: false });

const emit = defineEmits<{
  save: [patch: Partial<CaptionOptions>];
  consent: [model: string];
  revoke: [];
  'open-llm': [];
}>();

const status = computed(() => visionStatus(props.vision, props.caption.enabled));
const quota = computed(() => visionQuota(props.vision?.quota));
const shownModel = computed(() =>
  (props.vision?.provider && props.vision?.model ? `${props.vision.provider}/${props.vision.model}` : ''));

const icon = computed(() => {
  switch (status.value.tone) {
    case 'success': return 'mdi:image-check-outline';
    case 'warning': return 'mdi:image-off-outline';
    default: return 'mdi:image-search-outline';
  }
});

// ── limits, edited locally ────────────────────────────────────────────────

const useDefaultCap = ref(props.caption.daily_cap == null);
const cap = ref<number>(props.caption.daily_cap ?? props.defaultCap);
const minPx = ref<number>(props.caption.min_px);
const minKb = ref<number>(props.caption.min_kb);

const dirty = computed(() =>
  (useDefaultCap.value ? null : cap.value) !== props.caption.daily_cap
  || minPx.value !== props.caption.min_px
  || minKb.value !== props.caption.min_kb);

function reset() {
  useDefaultCap.value = props.caption.daily_cap == null;
  cap.value = props.caption.daily_cap ?? props.defaultCap;
  minPx.value = props.caption.min_px;
  minKb.value = props.caption.min_kb;
}

// Saved values coming back replace the local copy unless there are edits.
watch(() => props.caption, () => { if (!dirty.value) reset(); }, { deep: true });

function save() {
  if (!dirty.value) return;
  emit('save', {
    daily_cap: useDefaultCap.value ? null : Math.max(0, Math.round(cap.value ?? 0)),
    min_px: Math.round(minPx.value ?? 256),
    min_kb: Math.round(minKb.value ?? 20),
  });
}

function toggle(next: string | number | boolean) {
  emit('save', { enabled: !!next });
}
</script>

<template>
  <div class="cap">
    <div class="cap-toggle">
      <div>
        <strong>Describe photos and scanned pages</strong>
        <p class="hint">
          Lets the agent find a photo by what it shows ("two puppies at the park") and read scanned PDFs.
        </p>
      </div>
      <ElSwitch
        :model-value="caption.enabled"
        :disabled="saving || busy"
        aria-label="Describe photos and scanned pages"
        @update:model-value="toggle"
      />
    </div>

    <div class="cap-status" :class="`tone-${status.tone}`" role="status">
      <Icon :icon="icon" class="cap-status-icon" aria-hidden="true" />
      <div class="cap-status-main">
        <strong>{{ status.title }}</strong>
        <p>{{ status.detail }}</p>
        <p v-if="caption.enabled && waiting" class="cap-waiting">
          {{ formatCount(waiting) }} image{{ waiting === 1 ? '' : 's' }} waiting for a description.
        </p>
        <div v-if="status.action || (vision?.consent && caption.enabled)" class="cap-actions">
          <ElButton
            v-if="status.action === 'consent' && shownModel"
            size="small"
            type="primary"
            :disabled="busy"
            @click="emit('consent', shownModel)"
          >
            Allow {{ shownModel }}
          </ElButton>
          <ElButton v-if="status.action" size="small" :disabled="busy" @click="emit('open-llm')">
            <Icon icon="mdi:open-in-new" class="btn-icon" /> LLM Providers
          </ElButton>
          <ElButton
            v-if="vision?.consent && caption.enabled"
            size="small"
            :disabled="busy"
            @click="emit('revoke')"
          >
            Stop sending images
          </ElButton>
        </div>
      </div>
    </div>

    <div v-if="caption.enabled && quota" class="cap-quota">
      <div class="cap-quota-head">
        <span>Images described</span>
        <span>{{ quota.label }}<template v-if="vision?.quota?.ocr_pages"> · including {{ formatCount(vision.quota.ocr_pages) }} scanned page{{ vision.quota.ocr_pages === 1 ? '' : 's' }}</template></span>
      </div>
      <div
        class="bar"
        role="meter"
        :aria-valuenow="Math.round(quota.fraction * 100)"
        aria-valuemin="0"
        aria-valuemax="100"
        aria-label="Today's image descriptions against the daily limit"
      >
        <div class="bar-fill" :class="{ full: quota.fraction >= 1 }" :style="{ width: `${quota.fraction * 100}%` }" />
      </div>
      <p class="hint">
        Past the limit, images wait and are described on the following days, newest first. The limit
        resets at midnight in your time zone.
      </p>
    </div>

    <div v-if="caption.enabled" class="cap-limits">
      <label class="cap-field">
        <span>Daily limit</span>
        <div class="cap-field-row">
          <ElInputNumber
            v-model="cap"
            :min="0"
            :max="1000000"
            :step="100"
            size="small"
            controls-position="right"
            :disabled="useDefaultCap || saving"
          />
          <label class="cap-default">
            <input v-model="useDefaultCap" type="checkbox" :disabled="saving">
            Server default ({{ formatCount(defaultCap) }})
          </label>
        </div>
      </label>
      <label class="cap-field">
        <span>Skip images smaller than</span>
        <div class="cap-field-row">
          <ElInputNumber v-model="minPx" :min="16" :max="4096" :step="32" size="small" controls-position="right" :disabled="saving" />
          <span class="unit">px</span>
          <ElInputNumber v-model="minKb" :min="0" :max="10240" :step="5" size="small" controls-position="right" :disabled="saving" />
          <span class="unit">KB</span>
        </div>
      </label>
      <p class="hint">Icons and thumbnails below these sizes are never sent.</p>
      <div v-if="dirty" class="cap-save">
        <ElButton size="small" type="primary" :loading="saving" @click="save">Save limits</ElButton>
        <ElButton size="small" :disabled="saving" @click="reset">Cancel</ElButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cap { display: flex; flex-direction: column; gap: 12px; }
.cap-toggle { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.cap-toggle strong { font-size: 0.9rem; color: var(--text-primary); font-weight: 600; }
.hint { margin: 2px 0 0; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45; }

.cap-status {
  --tone: var(--primary-color);
  display: flex; gap: 10px; padding: 10px 12px; border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--tone) 35%, var(--border-color));
  background: color-mix(in srgb, var(--tone) 7%, var(--surface-color));
}
.tone-success { --tone: var(--success-color); }
.tone-warning { --tone: var(--warning-color); }
.tone-error { --tone: var(--danger-color); }
.cap-status-icon { font-size: 20px; color: var(--tone); flex: none; margin-top: 1px; }
.cap-status-main { flex: 1; min-width: 0; }
.cap-status-main strong { font-size: 0.875rem; color: var(--text-primary); }
.cap-status-main p { margin: 2px 0 0; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45; }
.cap-waiting { color: var(--text-primary) !important; }
.cap-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.cap-actions :deep(.el-button + .el-button) { margin-left: 0; }
.btn-icon { margin-right: 4px; }

.cap-quota { display: flex; flex-direction: column; gap: 6px; }
.cap-quota-head {
  display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  font-size: 0.8rem; color: var(--text-secondary);
}
.bar { height: 8px; border-radius: 999px; background: var(--hover-bg); border: 1px solid var(--border-color); }
.bar-fill { height: 100%; border-radius: 999px; background: var(--primary-color); transition: width 0.4s ease; }
.bar-fill.full { background: var(--warning-color); }

.cap-limits { display: flex; flex-direction: column; gap: 8px; }
.cap-field { display: flex; flex-direction: column; gap: 4px; font-size: 0.8rem; color: var(--text-secondary); }
.cap-field-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.cap-default { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); cursor: pointer; }
.unit { color: var(--text-secondary); }
.cap-save { display: flex; gap: 8px; }
.cap-save :deep(.el-button + .el-button) { margin-left: 0; }
</style>
