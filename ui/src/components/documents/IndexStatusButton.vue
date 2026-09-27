<script setup lang="ts">
/**
 * A file's index status in the file tree: one small button per file row (list
 * view) or tile (grid view), shown only while "Search my documents" is on.
 *
 * While mounted it registers its path with the index status store, so exactly
 * the files on screen are looked up. Clicking it (or Enter / Space) opens the
 * file's "Indexed content" in its panel. It never opens the file, changes the
 * selection or starts a drag: every pointer event stops here, and `press`
 * tells the row to refuse a drag that begins on the button (a drag starts on
 * the draggable row, not on this child, so the row has to know).
 */
import { computed, inject, onBeforeUnmount, onMounted, watch } from 'vue';
import { Icon } from '@iconify/vue';
import { useIndexStatusStore } from '../../stores/indexStatus';
import { indexStatusAriaLabel } from '../../utils/indexStatus';
import { INDEXED_CONTENT_OPENER } from './indexedContentContext';

const props = withDefaults(defineProps<{
  path: string;
  name: string;
  variant?: 'row' | 'tile';
}>(), { variant: 'row' });

const emit = defineEmits<{ press: [] }>();

const store = useIndexStatusStore();
const opener = inject(INDEXED_CONTENT_OPENER, null);

onMounted(() => store.register(props.path));
watch(() => props.path, (next, prev) => {
  if (prev) store.unregister(prev);
  if (next) store.register(next);
});
onBeforeUnmount(() => store.unregister(props.path));

const view = computed(() => store.statusFor(props.path));
const ariaLabel = computed(() => (view.value ? indexStatusAriaLabel(view.value, props.name) : ''));

function open(ev: MouseEvent) {
  if (!view.value || !opener) return;
  opener.open({ path: props.path, name: props.name, originEl: ev.currentTarget as HTMLElement | null });
}
</script>

<template>
  <button
    v-if="view"
    type="button"
    class="index-status"
    :class="[`tone-${view.tone}`, `variant-${variant}`, `kind-${view.kind}`]"
    :aria-label="ariaLabel"
    :title="view.tooltip"
    draggable="false"
    @click.stop.prevent="open"
    @dblclick.stop.prevent
    @mousedown.stop
    @pointerdown.stop="emit('press')"
    @dragstart.stop.prevent
  >
    <Icon :icon="view.icon" aria-hidden="true" />
  </button>
</template>

<style scoped>
.index-status {
  --tone: var(--text-tertiary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  padding: 1px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--tone);
  cursor: pointer;
  line-height: 1;
}
.index-status:hover {
  background: var(--hover-bg);
  filter: brightness(1.1);
}
.index-status:focus-visible {
  outline: 2px solid var(--primary-color);
  outline-offset: 1px;
}
.tone-ok { --tone: var(--success-color); }
.tone-info { --tone: var(--primary-color); }
.tone-warn { --tone: var(--warning-color); }
.tone-danger { --tone: var(--danger-color); }
.tone-muted { --tone: var(--text-tertiary); }

.variant-row {
  margin-left: auto;
  font-size: 0.95rem;
}
.variant-tile {
  position: absolute;
  top: 3px;
  right: 3px;
  font-size: 0.95rem;
  background: var(--surface-color);
  box-shadow: 0 0 0 1px var(--border-color);
}
.variant-tile:hover {
  background: var(--surface-hover);
}
.kind-indexing :deep(svg) {
  animation: index-status-pulse 1.6s ease-in-out infinite;
}
@keyframes index-status-pulse {
  50% { opacity: 0.45; }
}
@media (prefers-reduced-motion: reduce) {
  .kind-indexing :deep(svg) { animation: none; }
}
</style>
