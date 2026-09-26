<script setup lang="ts">
/**
 * The NavRail's Documentation search indicator. Hidden unless there is
 * something to see: a spinning sync icon while indexing is under way, an
 * alert when the user has to act (a held deletion, an unavailable folder, a
 * full disk, files that failed). The tooltip says what, and a click opens
 * Settings → My Documents.
 *
 * Deliberately a chip and never the App.vue full-screen embedding overlay:
 * indexing runs in the background and blocks nothing — chat included.
 */
import { computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElTooltip } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useDocumentsStore } from '../../stores/documents';
import { chipTooltip } from '../../utils/documentsView';

const route = useRoute();
const router = useRouter();
const store = useDocumentsStore();

const visible = computed(() => store.isActive || store.needsAttention);
// Failures during a run tint the spinner rather than replacing it: work is
// still under way, and that is the more useful thing to show.
const mode = computed(() => (store.isActive ? 'active' : 'attention'));
const tooltip = computed(() => chipTooltip(store.snapshot));
const onPage = computed(() => route.name === 'documents-settings');

function open() {
  const profile = route.params.profile as string | undefined;
  if (!profile) return;
  void router.push({ name: 'documents-settings', params: { profile } });
}
</script>

<template>
  <ElTooltip v-if="visible" :content="tooltip" placement="right" :show-after="200">
    <button
      type="button"
      class="rail-item doc-chip"
      :class="[`doc-${mode}`, { active: onPage, 'doc-has-failed': store.isActive && store.needsAttention }]"
      :aria-label="tooltip"
      @click="open"
    >
      <Icon v-if="mode === 'active'" icon="mdi:sync" class="rail-icon doc-spin" />
      <Icon v-else icon="mdi:sync-alert" class="rail-icon" />
    </button>
  </ElTooltip>
</template>

<style scoped>
/* Same box as NavRail's .rail-item (scoped there, so restated here). */
.rail-item {
  position: relative;
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  cursor: pointer;
  border-radius: 8px;
  padding: 0;
  transition: background 0.15s ease, color 0.15s ease;
}
.rail-item:hover { background: var(--hover-bg); }
.rail-item.active { background: var(--surface-hover); }
.rail-icon { font-size: 20px; }

.doc-active { color: var(--primary-color); }
.doc-attention { color: var(--warning-color); }
.doc-has-failed { color: var(--warning-color); }

.doc-spin { animation: doc-spin 1.4s linear infinite; }
@keyframes doc-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .doc-spin { animation: none; } }
</style>
