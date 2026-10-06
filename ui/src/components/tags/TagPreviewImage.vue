<script setup lang="ts">
/**
 * One stored screen preview of a tag. The companion renders one PNG per
 * revision, pixel for pixel: 1-bit for black/white panels, a white/black/red
 * palette for three-colour panels, in the orientation the tag is read. The
 * endpoint needs the Bearer header, so the PNG is fetched as a blob and shown
 * through an object URL, revoked when the revision changes or the component
 * unmounts. `revision` is what the overview says is stored: null means nothing
 * has been sent yet, and no request is made. `epoch` is part of the key too: a
 * change of owner drops the previews and restarts the revisions, so a new
 * owner's "revision 1" is never the old owner's image.
 *
 * The PNG is drawn at a whole number of screen pixels per tag pixel
 * (`previewZoom`): the screens use 12 px text with 1 px stems that any
 * fractional scale breaks. Below 1× the frame scrolls instead — unless
 * `shrink` (a thumbnail), which scales down smoothly. Clicking the image opens
 * it at the largest whole zoom the window holds.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElDialog } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../stores/tags';
import { logicalSize, previewZoom, steadyZoom, type PreviewZoom } from '../../utils/tagsFormat';

const props = withDefaults(defineProps<{
  deviceId: string;
  kind: 'desired' | 'displayed';
  revision: number | null | undefined;
  epoch?: number;
  label: string;
  /** The panel's native size and quarter turns: the preview's size before the PNG loads. */
  width?: number | null;
  height?: number | null;
  rotation?: number | null;
  /** The tag's name, for the enlarged view's title. */
  name?: string;
  /** The tallest it is drawn, in CSS pixels; a screen taller than this at 1× is still drawn at 1×. */
  maxHeight?: number;
  /** A thumbnail: scale down to fit (smoothly) instead of scrolling below 1×. */
  shrink?: boolean;
  /** A small preview in a list (Settings → Tags): no revision number. */
  hideRevision?: boolean;
}>(), { maxHeight: 360 });

const store = useTagsStore();
const src = ref<string | null>(null);
const state = ref<'idle' | 'loading' | 'none' | 'failed' | 'ready'>('idle');
/** The revision the server says the loaded PNG is (X-Tag-Revision). */
const shownRevision = ref<number | null>(null);
/** The loaded PNG's size in pixels. */
const natural = ref<{ w: number; h: number } | null>(null);
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
    shownRevision.value = null;
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
    shownRevision.value = preview.revision ?? props.revision ?? null;
    state.value = 'ready';
  } catch {
    if (gen !== generation) return;
    release();
    src.value = null;
    state.value = 'failed';
  }
}

watch(() => [props.deviceId, props.kind, props.revision, props.epoch], (next, prev) => {
  // A new owner (epoch) never keeps the previous owner's image up while loading.
  if (prev && next[3] !== prev[3]) { release(); src.value = null; natural.value = null; }
  void load();
}, { immediate: true });

const shown = computed(() => shownRevision.value ?? props.revision ?? null);
const alt = computed(() => (props.hideRevision ? props.label : `${props.label} (revision ${shown.value})`));

// ── size: a whole zoom, from the frame's width and the screen's pixel ratio ──

const frame = ref<HTMLElement | null>(null);
/** The frame's inner width in CSS pixels (0 until measured, and while hidden). */
const boxWidth = ref(0);
const dpr = ref(screenRatio());
/** The PNG's size; until it loads, the panel's as the tag is read. */
const size = computed(() => {
  if (natural.value) return natural.value;
  const s = logicalSize({ width: props.width, height: props.height, rotation: props.rotation });
  return s ? { w: s.width, h: s.height } : null;
});
/** How the PNG is drawn: null until the frame is measured and a size is known. */
const fit = ref<PreviewZoom | null>(null);
/** The image and screen `fit` was worked out for: its zoom is held only for those. */
let fitFor = '';

function screenRatio(): number {
  return window.devicePixelRatio > 0 ? window.devicePixelRatio : 1;
}

/** 1×: one screen pixel per tag pixel, however little room there is. */
function oneToOne(n: { w: number; h: number }): PreviewZoom {
  return { cssWidth: n.w / dpr.value, cssHeight: n.h / dpr.value, zoom: 1, pixelated: true };
}

function relayout() {
  const n = size.value;
  if (!n || boxWidth.value <= 0) {
    fit.value = null;
    fitFor = '';
    return;
  }
  const key = `${n.w}x${n.h}@${dpr.value}`;
  const held = key === fitFor && fit.value?.pixelated ? fit.value.zoom : null;
  const next = steadyZoom(held, n, { w: boxWidth.value, h: props.maxHeight }, dpr.value);
  fit.value = next.pixelated || props.shrink ? next : oneToOne(n);
  fitFor = key;
}
watch([size, boxWidth, dpr, () => props.maxHeight, () => props.shrink], relayout, { immediate: true });

function onImageLoad(e: Event) {
  const img = e.target as HTMLImageElement;
  const w = img.naturalWidth;
  const h = img.naturalHeight;
  if (w && h && (natural.value?.w !== w || natural.value?.h !== h)) natural.value = { w, h };
}

let observer: ResizeObserver | null = null;
let resizeFrame = 0;
let ratioQuery: MediaQueryList | null = null;

/** Browser zoom, or the window moved to another screen: re-arm for the new ratio. */
function watchRatio() {
  ratioQuery?.removeEventListener('change', watchRatio);
  dpr.value = screenRatio();
  ratioQuery = window.matchMedia(`(resolution: ${dpr.value}dppx)`);
  ratioQuery.addEventListener('change', watchRatio);
}

onMounted(() => {
  watchRatio();
  const el = frame.value;
  if (!el) return;
  boxWidth.value = el.clientWidth;
  observer = new ResizeObserver((entries) => {
    const width = entries[entries.length - 1]?.contentRect.width ?? 0;
    // On the next frame: resizing the image inside this callback would resize
    // the frame within the same observation (a ResizeObserver loop).
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => { boxWidth.value = width; });
  });
  observer.observe(el);
});

const px = (n: number) => `${n}px`;
const imgStyle = computed(() => (fit.value
  ? { width: px(fit.value.cssWidth), height: px(fit.value.cssHeight) }
  : { width: '100%', height: 'auto' }));
/** Before the PNG loads: hold the room it will take (up to `maxHeight`), or a
 *  4:3 box while the size is unknown. */
const frameStyle = computed(() => (fit.value || src.value ? {} : { aspectRatio: '4 / 3', maxHeight: px(props.maxHeight) }));
const emptyStyle = computed(() => (fit.value ? { minHeight: px(Math.min(fit.value.cssHeight, props.maxHeight)) } : {}));
const zoomHint = computed(() => (fit.value?.zoom === 1
  ? 'Pixel for pixel: one screen pixel per pixel of the tag'
  : `Each pixel of the tag drawn as ${fit.value?.zoom}×${fit.value?.zoom} screen pixels`));

// ── enlarged: the largest whole zoom the window holds ──

const enlarged = ref(false);
/** The dialog is mounted on the first click, not by every preview on the page. */
const dialogMounted = ref(false);
const large = ref<PreviewZoom | null>(null);
/** Room the dialog's padding and title, the stage's padding and border (26 px
 *  each way, see .tag-preview-stage) and the caption take around the image, in
 *  CSS pixels, with a margin to the window's edges. */
const DIALOG_CHROME = { w: 96, h: 184 };

function measureLarge() {
  const n = size.value;
  if (!n) {
    large.value = null;
    return;
  }
  const box = { w: window.innerWidth - DIALOG_CHROME.w, h: window.innerHeight - DIALOG_CHROME.h };
  const next = previewZoom(n, box, dpr.value, Infinity);
  large.value = next.pixelated ? next : oneToOne(n);
}

function enlarge() {
  dialogMounted.value = true;
  enlarged.value = true;
}

watch(enlarged, (open) => {
  if (open) {
    measureLarge();
    window.addEventListener('resize', measureLarge);
  } else {
    window.removeEventListener('resize', measureLarge);
  }
});
watch([size, dpr], () => { if (enlarged.value) measureLarge(); });
watch(src, (value) => { if (!value) enlarged.value = false; });

const largeStyle = computed(() => (large.value
  ? { width: px(large.value.cssWidth), height: px(large.value.cssHeight) }
  : {}));
const dialogTitle = computed(() => (props.name ? `${props.name} — ${props.label}` : props.label));

onBeforeUnmount(() => {
  generation += 1;
  release();
  observer?.disconnect();
  cancelAnimationFrame(resizeFrame);
  ratioQuery?.removeEventListener('change', watchRatio);
  window.removeEventListener('resize', measureLarge);
});
</script>

<template>
  <figure class="tag-preview">
    <figcaption class="tag-preview-caption">
      <span class="tag-preview-label">{{ label }}</span>
      <span class="tag-preview-info">
        <span v-if="!hideRevision && shown != null" class="tag-preview-rev">rev {{ shown }}</span>
        <span v-if="src && fit?.pixelated" class="tag-preview-zoom" :title="zoomHint">{{ fit.zoom }}×</span>
      </span>
    </figcaption>
    <div ref="frame" class="tag-preview-frame" :style="frameStyle">
      <button v-if="src" type="button" class="tag-preview-open" title="View larger" :aria-label="`${alt}: view larger`" @click="enlarge">
        <img :src="src" :alt="alt" class="tag-preview-img" :class="{ pixelated: fit?.pixelated }" :style="imgStyle" @load="onImageLoad" />
      </button>
      <div v-else class="tag-preview-empty" :style="emptyStyle">
        <Icon
          :icon="state === 'failed' ? 'mdi:image-broken-variant' : state === 'loading' ? 'mdi:loading' : 'mdi:image-off-outline'"
          :class="{ spin: state === 'loading' }"
        />
        <span>{{ state === 'failed' ? 'Preview unavailable' : state === 'loading' ? 'Loading…' : 'No preview yet' }}</span>
      </div>
    </div>
    <ElDialog v-if="dialogMounted" v-model="enlarged" :title="dialogTitle" width="fit-content" align-center append-to-body>
      <div v-if="src" class="tag-preview-stage">
        <img :src="src" :alt="alt" class="tag-preview-img" :class="{ pixelated: large?.pixelated }" :style="largeStyle" />
      </div>
      <p v-if="size" class="tag-preview-meta">
        <template v-if="!hideRevision && shown != null">Revision {{ shown }} · </template>{{ size.w }}×{{ size.h }} pixels<template v-if="large"> · shown at {{ large.zoom }}×</template>
      </p>
    </ElDialog>
  </figure>
</template>

<style scoped>
.tag-preview { margin: 0; min-width: 0; flex: 1 1 0; }
.tag-preview-caption {
  display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
  margin-bottom: 6px; font-size: 0.78rem; color: var(--text-secondary);
}
.tag-preview-label { font-weight: 600; }
.tag-preview-info { display: flex; gap: 8px; }
.tag-preview-rev, .tag-preview-zoom { color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.tag-preview-zoom { cursor: help; }
/* Never wider than its column: a screen that does not fit at 1× scrolls inside. */
.tag-preview-frame {
  border: 1px solid var(--border-color); border-radius: 8px;
  background: var(--hover-bg);
  display: flex; overflow: auto;
}
/* Auto margins centre it, and unlike centring the frame's content they leave
   an overflowing image scrollable from its left edge. */
.tag-preview-open {
  margin: auto; padding: 0; border: 0; background: none;
  display: block; line-height: 0; cursor: zoom-in;
}
.tag-preview-open:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
/* The PNG is the e-paper screen itself: keep it on white paper, at its own size. */
.tag-preview-img { display: block; max-width: none; background: #fff; }
.tag-preview-img.pixelated { image-rendering: pixelated; }
.tag-preview-empty {
  flex: 1; box-sizing: border-box;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px;
  color: var(--text-tertiary); font-size: 0.8rem; padding: 12px; text-align: center;
}
.tag-preview-empty :deep(svg) { font-size: 1.5rem; }
.spin { animation: tag-preview-spin 1s linear infinite; }
@keyframes tag-preview-spin { to { transform: rotate(360deg); } }
/* The image is sized to fit (DIALOG_CHROME); these limits only matter when even
   1× does not fit the window, and the stage scrolls. */
.tag-preview-stage {
  display: flex; overflow: auto; box-sizing: border-box;
  max-width: calc(100vw - 70px); max-height: calc(100vh - 158px);
  padding: 12px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--hover-bg);
}
.tag-preview-stage .tag-preview-img { margin: auto; }
.tag-preview-meta {
  margin: 10px 0 0; font-size: 0.8rem; color: var(--text-secondary); font-variant-numeric: tabular-nums;
}
</style>
