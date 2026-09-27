<script setup lang="ts">
/**
 * The one place a settings page saves: a bar at the end of the page that stays
 * pinned to the bottom of the view while the page scrolls (an affix), so Save
 * is always in sight wherever the edit was made. Quiet while nothing has
 * changed; once anything has, it says so, lights up and offers Discard next to
 * Save — and leaving the page asks first (useLeaveGuard).
 *
 * On/off switches that start or stop something (enable a tool, connect a
 * channel) still act the moment they are flipped; the bar is for the fields
 * around them.
 *
 * Place it as the last child of the page's content column, inside the
 * element that scrolls: `position: sticky` needs the scrolling ancestor.
 */
import { ElButton } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useLeaveGuard } from '../../composables/useUnsavedChanges';

const props = withDefaults(defineProps<{
  dirty: boolean;
  saving?: boolean;
  /** Save cannot run right now — `hint` says why. */
  disabled?: boolean;
  /** Replaces the usual "unsaved changes" text, e.g. to say why Save is off. */
  hint?: string;
  saveLabel?: string;
  /** Save stays available with nothing changed — to retry a failed apply. */
  saveWhenClean?: boolean;
}>(), {
  saving: false,
  disabled: false,
  hint: '',
  saveLabel: 'Save changes',
  saveWhenClean: false,
});

const emit = defineEmits<{ (e: 'save'): void; (e: 'discard'): void }>();

useLeaveGuard(() => props.dirty);
</script>

<template>
  <div class="save-bar" :class="{ dirty }" role="region" aria-label="Save changes">
    <div class="save-bar-status" aria-live="polite">
      <span v-if="dirty" class="save-bar-dot" aria-hidden="true" />
      <Icon v-else icon="mdi:check-circle-outline" class="save-bar-clean" aria-hidden="true" />
      <span class="save-bar-text">{{ hint || (dirty ? 'You have unsaved changes' : 'No unsaved changes') }}</span>
    </div>
    <div class="save-bar-actions">
      <ElButton v-if="dirty" :disabled="saving" @click="emit('discard')">Discard</ElButton>
      <ElButton
        type="primary"
        :disabled="(!dirty && !saveWhenClean) || disabled"
        :loading="saving"
        @click="emit('save')"
      >
        <Icon v-if="!saving" icon="mdi:content-save-outline" class="save-bar-icon" />
        {{ saveLabel }}
      </ElButton>
    </div>
  </div>
</template>

<style scoped>
.save-bar {
  position: sticky;
  bottom: 12px;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-top: 24px;
  padding: 10px 12px 10px 16px;
  border: 1px solid var(--border-color);
  border-radius: 12px;
  background: var(--surface-color);
  color: var(--text-secondary);
  font-size: 0.875rem;
  box-shadow: 0 4px 18px rgba(15, 23, 42, 0.1);
  transition: border-color 0.2s ease, background-color 0.2s ease, box-shadow 0.2s ease;
}
.save-bar.dirty {
  border-color: var(--primary-color);
  background: color-mix(in srgb, var(--primary-color) 8%, var(--surface-color));
  color: var(--text-primary);
  box-shadow: 0 6px 24px color-mix(in srgb, var(--primary-color) 28%, transparent);
}

.save-bar-status {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.save-bar-text { font-weight: 500; }
.save-bar-clean {
  flex-shrink: 0;
  font-size: 1.05rem;
  color: var(--text-tertiary);
}
.save-bar-dot {
  flex-shrink: 0;
  width: 9px;
  height: 9px;
  margin: 0 3px;
  border-radius: 50%;
  background: var(--primary-color);
  animation: save-bar-pulse 1.8s ease-out infinite;
}
@keyframes save-bar-pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--primary-color) 60%, transparent); }
  70% { box-shadow: 0 0 0 8px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}
@media (prefers-reduced-motion: reduce) {
  .save-bar-dot { animation: none; }
}

.save-bar-actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
}
.save-bar-actions .el-button + .el-button { margin-left: 0; }
.save-bar-icon { margin-right: 6px; font-size: 1rem; }
</style>
