<script setup lang="ts">
/**
 * One stored screen preview of a tag (the companion renders a 1-bit PNG per
 * revision). The endpoint needs the Bearer header, so the PNG is fetched as a
 * blob and shown through an object URL, revoked when the revision changes or
 * the component unmounts. `revision` is what the overview says is stored:
 * null means nothing has been sent yet, and no request is made.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../stores/tags';

const props = defineProps<{
  deviceId: string;
  kind: 'desired' | 'displayed';
  revision: number | null | undefined;
  label: string;
  width?: number | null;
  height?: number | null;
}>();

const store = useTagsStore();
const src = ref<string | null>(null);
const state = ref<'idle' | 'loading' | 'none' | 'failed' | 'ready'>('idle');
let owned: string | null = null;
let generation = 0;

function release() {
  if (owned) URL.revokeObjectURL(owned);
  owned = null;
}

async function load() {
  const gen = ++generation;
  if (props.revision == null) {
    release();
    src.value = null;
    state.value = 'none';
    return;
  }
  // Keep the old image up while the new revision loads, so the card does not blink.
  if (!src.value) state.value = 'loading';
  try {
    const preview = await store.preview(props.deviceId, props.kind);
    if (gen !== generation) return;
    release();
    if (!preview) {
      src.value = null;
      state.value = 'none';
      return;
    }
    owned = URL.createObjectURL(preview.blob);
    src.value = owned;
    state.value = 'ready';
  } catch {
    if (gen !== generation) return;
    release();
    src.value = null;
    state.value = 'failed';
  }
}

watch(() => [props.deviceId, props.kind, props.revision], load, { immediate: true });
onBeforeUnmount(() => { generation += 1; release(); });

const aspect = computed(() =>
  props.width && props.height ? `${props.width} / ${props.height}` : '4 / 3');
</script>

<template>
  <figure class="tag-preview">
    <figcaption class="tag-preview-caption">
      <span class="tag-preview-label">{{ label }}</span>
      <span v-if="revision != null" class="tag-preview-rev">rev {{ revision }}</span>
    </figcaption>
    <div class="tag-preview-frame" :style="{ aspectRatio: aspect }">
      <img v-if="src" :src="src" :alt="`${label} (revision ${revision})`" class="tag-preview-img" />
      <div v-else class="tag-preview-empty">
        <Icon
          :icon="state === 'failed' ? 'mdi:image-broken-variant' : state === 'loading' ? 'mdi:loading' : 'mdi:image-off-outline'"
          :class="{ spin: state === 'loading' }"
        />
        <span>{{ state === 'failed' ? 'Preview unavailable' : state === 'loading' ? 'Loading…' : 'No preview yet' }}</span>
      </div>
    </div>
  </figure>
</template>

<style scoped>
.tag-preview { margin: 0; min-width: 0; flex: 1 1 0; }
.tag-preview-caption {
  display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
  margin-bottom: 6px; font-size: 0.78rem; color: var(--text-secondary);
}
.tag-preview-label { font-weight: 600; }
.tag-preview-rev { color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.tag-preview-frame {
  width: 100%; max-height: 240px;
  border: 1px solid var(--border-color); border-radius: 8px;
  background: var(--hover-bg);
  display: flex; align-items: center; justify-content: center;
  overflow: hidden;
}
/* The PNG is the e-paper screen itself: keep it on white paper and crisp. */
.tag-preview-img {
  width: 100%; height: 100%; object-fit: contain;
  background: #fff; image-rendering: pixelated;
}
.tag-preview-empty {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  color: var(--text-tertiary); font-size: 0.8rem; padding: 12px; text-align: center;
}
.tag-preview-empty :deep(svg) { font-size: 1.5rem; }
.spin { animation: tag-preview-spin 1s linear infinite; }
@keyframes tag-preview-spin { to { transform: rotate(360deg); } }
</style>
