<script setup lang="ts">
/**
 * The state banner at the top of My Documents: one `state(reason)` of the
 * snapshot, worded by `stateBanner` (utils/userdocsView.ts), with the actions
 * that state allows. The panel only renders and reports which action was
 * clicked — the page owns what each one does.
 *
 * Colours come from the app's own tokens through color-mix rather than
 * ElAlert, whose light-tint variables the theme never redeclares (they would
 * render light-theme tints in dark mode).
 */
import { computed, ref } from 'vue';
import { ElButton, ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { BannerActionId, StateBanner } from '../../utils/userdocsView';
import { copyTextToClipboard } from '../../utils/clipboard';

const props = withDefaults(defineProps<{
  banner: StateBanner | null;
  /** An action is in flight — its buttons are disabled meanwhile. */
  busy?: boolean;
}>(), { busy: false });

const emit = defineEmits<{ action: [id: BannerActionId] }>();

const icon = computed(() => {
  switch (props.banner?.tone) {
    case 'success': return 'mdi:check-circle-outline';
    case 'warning': return 'mdi:alert-outline';
    case 'error': return 'mdi:alert-circle-outline';
    default: return 'mdi:information-outline';
  }
});

const copied = ref(false);
async function copySnippet() {
  if (!props.banner?.snippet) return;
  const ok = await copyTextToClipboard(props.banner.snippet);
  if (ok) {
    copied.value = true;
    setTimeout(() => { copied.value = false; }, 1500);
  } else {
    ElMessage.error('Could not copy — select the line and copy it by hand.');
  }
}
</script>

<template>
  <section
    v-if="banner"
    class="ud-banner"
    :class="`tone-${banner.tone}`"
    role="status"
    aria-live="polite"
  >
    <div class="ud-banner-icon" aria-hidden="true">
      <Icon v-if="banner.busy" icon="mdi:sync" class="spin" />
      <Icon v-else :icon="icon" />
    </div>
    <div class="ud-banner-main">
      <h2 class="ud-banner-title">{{ banner.title }}</h2>
      <p class="ud-banner-body">{{ banner.body }}</p>
      <div v-if="banner.snippet" class="ud-snippet">
        <code>{{ banner.snippet }}</code>
        <button type="button" class="ud-copy" :title="copied ? 'Copied' : 'Copy'" @click="copySnippet">
          <Icon :icon="copied ? 'mdi:check' : 'mdi:content-copy'" />
        </button>
      </div>
      <div v-if="banner.actions.length" class="ud-banner-actions">
        <ElButton
          v-for="action in banner.actions"
          :key="action.id"
          size="small"
          :type="action.primary ? 'primary' : undefined"
          :disabled="busy"
          @click="emit('action', action.id)"
        >
          {{ action.label }}
        </ElButton>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ud-banner {
  --tone: var(--primary-color);
  display: flex;
  gap: 12px;
  padding: 14px 16px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--tone) 40%, var(--border-color));
  background: color-mix(in srgb, var(--tone) 8%, var(--surface-color));
  color: var(--text-primary);
}
.tone-success { --tone: var(--success-color); }
.tone-warning { --tone: var(--warning-color); }
.tone-error { --tone: var(--danger-color); }

.ud-banner-icon { font-size: 22px; color: var(--tone); line-height: 1; padding-top: 1px; flex: none; }
.ud-banner-main { flex: 1; min-width: 0; }
.ud-banner-title { font-size: 0.95rem; font-weight: 600; margin: 0 0 4px; line-height: 1.4; }
.ud-banner-body { font-size: 0.85rem; color: var(--text-secondary); margin: 0; line-height: 1.5; }
.ud-banner-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.ud-banner-actions :deep(.el-button + .el-button) { margin-left: 0; }

.ud-snippet {
  display: flex; align-items: center; gap: 8px; margin-top: 10px;
  padding: 8px 10px; border-radius: 6px;
  background: var(--bg-color); border: 1px solid var(--border-color);
}
.ud-snippet code {
  flex: 1; min-width: 0; overflow-x: auto; white-space: pre;
  font-family: var(--font-mono, monospace); font-size: 0.8rem; color: var(--text-primary);
}
.ud-copy {
  flex: none; border: none; background: none; cursor: pointer; padding: 2px;
  color: var(--text-secondary); font-size: 16px; line-height: 1;
}
.ud-copy:hover { color: var(--primary-color); }

.spin { animation: spin 1.1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
