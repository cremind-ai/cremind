<script setup lang="ts">
/**
 * How much room the document index takes, against the limit that actually
 * binds it, plus the free disk underneath.
 *
 * The binding limit is the profile's own budget when the admin set one, else
 * the shared budget — measured against every profile's indexes together,
 * because that total is what the storage governor pauses on. The bar carries a
 * mark at 85%, where the governor starts asking before big syncs. Numbers come
 * from the snapshot (refreshed by the governor every minute while indexing);
 * `detail` (GET /storage) adds the index-file / vector split when loaded.
 */
import { computed } from 'vue';
import type { UserDocsStorageInfo, UserDocsStorageSnapshot } from '../../services/userdocsApi';
import { formatBytes } from '../../utils/userdocsView';

const props = withDefaults(defineProps<{
  storage: UserDocsStorageSnapshot | null | undefined;
  detail?: UserDocsStorageInfo | null;
}>(), { detail: null });

const WARN_RATIO = 0.85;

const LEVELS: Record<string, { label: string; tone: string }> = {
  ok: { label: 'OK', tone: 'success' },
  warn: { label: 'Getting full', tone: 'warning' },
  budget: { label: 'Budget full', tone: 'warning' },
  disk_low: { label: 'Disk low', tone: 'warning' },
  disk_critical: { label: 'Disk critical', tone: 'error' },
};

const level = computed(() => props.storage?.level ?? props.detail?.level ?? null);
const levelInfo = computed(() => (level.value ? LEVELS[level.value] ?? null : null));

const mine = computed(() => props.storage?.used_bytes ?? props.detail?.total_bytes ?? 0);

const bar = computed(() => {
  const s = props.storage ?? {};
  const profileBudget = s.profile_budget_bytes ?? props.detail?.profile_budget_bytes ?? null;
  if (profileBudget) {
    return { used: mine.value, limit: profileBudget, label: 'Your index' };
  }
  const budget = s.budget_bytes ?? props.detail?.budget_bytes ?? null;
  if (budget) {
    return { used: s.global_bytes ?? mine.value, limit: budget, label: "All profiles' indexes" };
  }
  return null;
});

const pct = computed(() => {
  if (!bar.value || bar.value.limit <= 0) return 0;
  return Math.min(100, (100 * bar.value.used) / bar.value.limit);
});

const freeLine = computed(() => {
  const free = props.storage?.free_bytes ?? props.detail?.free_bytes ?? null;
  if (free == null) return '';
  const total = props.storage?.total_bytes ?? props.detail?.disk_total_bytes ?? null;
  return total ? `${formatBytes(free)} free of ${formatBytes(total)} on disk` : `${formatBytes(free)} free on disk`;
});

const methodNote = computed(() => {
  switch (props.storage?.method ?? props.detail?.method) {
    case 'admin': return 'Capacity set by your administrator.';
    case 'env': return 'Capacity from the deployment (volume sizes).';
    default: return '';
  }
});
</script>

<template>
  <div class="storage">
    <div class="storage-head">
      <span class="storage-title">
        {{ formatBytes(mine) }} used by your index
        <span v-if="detail" class="sub">
          ({{ formatBytes(detail.index_bytes) }} text and search data,
          ~{{ formatBytes(detail.vector_bytes_est) }} vectors)
        </span>
      </span>
      <span v-if="levelInfo" class="level" :class="`tone-${levelInfo.tone}`">{{ levelInfo.label }}</span>
    </div>

    <template v-if="bar">
      <div
        class="bar"
        role="meter"
        :aria-valuenow="Math.round(pct)"
        aria-valuemin="0"
        aria-valuemax="100"
        :aria-label="`${bar.label}: ${Math.round(pct)}% of the storage budget`"
      >
        <div class="bar-fill" :class="levelInfo ? `tone-${levelInfo.tone}` : ''" :style="{ width: `${pct}%` }" />
        <div class="bar-mark" :style="{ left: `${WARN_RATIO * 100}%` }" title="85%: large syncs ask first" />
      </div>
      <div class="storage-foot">
        <span>{{ bar.label }}: {{ formatBytes(bar.used) }} of {{ formatBytes(bar.limit) }}</span>
        <span v-if="freeLine">{{ freeLine }}</span>
      </div>
    </template>
    <!-- The budget always exists (the admin minimum is 256 MB); until the
         governor's first measurement it is simply not known yet. -->
    <div v-else class="storage-foot">
      <span>Measuring storage — this updates within a few minutes.</span>
      <span v-if="freeLine">{{ freeLine }}</span>
    </div>
    <p v-if="storage?.message && level && level !== 'ok'" class="storage-msg">{{ storage.message }}</p>
    <p v-if="methodNote" class="storage-method">{{ methodNote }}</p>
  </div>
</template>

<style scoped>
.storage { display: flex; flex-direction: column; gap: 8px; }
.storage-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.storage-title { font-size: 0.875rem; color: var(--text-primary); }
.sub { color: var(--text-secondary); font-size: 0.8rem; }

.level {
  --tone: var(--success-color);
  flex: none; font-size: 0.72rem; font-weight: 600; padding: 2px 9px; border-radius: 999px;
  color: var(--tone);
  background: color-mix(in srgb, var(--tone) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--tone) 35%, transparent);
}
.tone-success { --tone: var(--success-color); }
.tone-warning { --tone: var(--warning-color); }
.tone-error { --tone: var(--danger-color); }

.bar {
  position: relative; height: 10px; border-radius: 999px; overflow: visible;
  background: var(--hover-bg); border: 1px solid var(--border-color);
}
.bar-fill {
  --tone: var(--primary-color);
  height: 100%; border-radius: 999px; background: var(--tone);
  transition: width 0.4s ease;
}
.bar-fill.tone-success { --tone: var(--primary-color); }
.bar-mark {
  position: absolute; top: -3px; bottom: -3px; width: 2px; border-radius: 1px;
  background: color-mix(in srgb, var(--warning-color) 70%, transparent);
}
.storage-foot {
  display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  font-size: 0.8rem; color: var(--text-secondary);
}
.storage-msg, .storage-method { margin: 0; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45; }
</style>
