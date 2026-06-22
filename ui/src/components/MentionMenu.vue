<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { Icon } from '@iconify/vue';

export interface MentionItem {
  name: string;
  description?: string;
  // System variables carry a resolved value (may be null when unset, e.g. no
  // profile). Profiles omit the key entirely — `undefined` means "no value
  // concept", which suppresses the value row in the detail panel.
  value?: string | null;
  // `true` for secret variables (CREMIND_TOKEN): the value renders masked until
  // the user clicks the reveal toggle.
  secret?: boolean;
}

const props = defineProps<{
  visible: boolean;
  items: MentionItem[];
  top: number;
  left: number;
  activeIndex: number;
  prefix: '$' | '@';
}>();

const emit = defineEmits<{
  select: [item: MentionItem];
  'update:activeIndex': [index: number];
}>();

// The popup grows rightward from `left` and is now wider (list + detail
// column), so it can run off the right edge when `$` is typed deep into a line.
// Clamp `left` against the measured popup width after each render.
const VIEWPORT_MARGIN = 8;
const popupRef = ref<HTMLElement | null>(null);
const clampedLeft = ref(props.left);

const clampPosition = () => {
  const el = popupRef.value;
  if (!el) { clampedLeft.value = props.left; return; }
  const max = window.innerWidth - el.offsetWidth - VIEWPORT_MARGIN;
  clampedLeft.value = Math.max(VIEWPORT_MARGIN, Math.min(props.left, max));
};

watch(
  [() => props.visible, () => props.left, () => props.items.length],
  () => { nextTick(clampPosition); },
  { immediate: true },
);

const style = computed(() => ({
  top: `${props.top}px`,
  left: `${clampedLeft.value}px`,
}));

const activeItem = computed<MentionItem | null>(
  () => props.items[props.activeIndex] ?? null,
);

// Only show the detail aside when there's something beyond the name to show —
// system vars (which always carry a `value` key, even if null) and any item
// with a description. Profiles (`@`, no value/description) keep the original
// single-column menu.
const showDetail = computed(() => {
  const item = activeItem.value;
  return !!item && (!!item.description || item.value !== undefined);
});

// Reveal state for the active secret value. Reset whenever the active item
// changes (hover/keyboard moves away) or the menu closes/reopens, so a secret
// never stays revealed across items or re-opens.
const revealed = ref(false);
watch(() => props.activeIndex, () => { revealed.value = false; });
watch(() => props.visible, () => { revealed.value = false; });
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible && items.length > 0"
      ref="popupRef"
      class="mention-popup"
      :style="style"
      @mousedown.prevent
    >
      <ul class="mention-menu" role="listbox">
        <li
          v-for="(item, idx) in items"
          :key="item.name"
          :class="['mention-item', { active: idx === activeIndex }]"
          role="option"
          :aria-selected="idx === activeIndex"
          @mouseenter="emit('update:activeIndex', idx)"
          @click="emit('select', item)"
        >
          <span class="mention-name">{{ prefix }}{{ item.name }}</span>
          <span v-if="item.description" class="mention-desc">{{ item.description }}</span>
        </li>
      </ul>
      <aside v-if="showDetail && activeItem" class="mention-detail">
        <div class="detail-name">{{ prefix }}{{ activeItem.name }}</div>
        <p v-if="activeItem.description" class="detail-desc">{{ activeItem.description }}</p>
        <div v-if="activeItem.value !== undefined" class="detail-value-row">
          <span v-if="activeItem.value == null" class="detail-value detail-value--unset">(not set)</span>
          <template v-else-if="activeItem.secret">
            <span class="detail-value">{{ revealed ? activeItem.value : '••••••••' }}</span>
            <button
              type="button"
              class="reveal-btn"
              :title="revealed ? 'Hide value' : 'Show value'"
              :aria-label="revealed ? 'Hide value' : 'Show value'"
              @mousedown.prevent
              @click.stop="revealed = !revealed"
            >
              <Icon :icon="revealed ? 'mdi:eye-off-outline' : 'mdi:eye-outline'" />
            </button>
          </template>
          <span v-else class="detail-value">{{ activeItem.value }}</span>
        </div>
      </aside>
    </div>
  </Teleport>
</template>

<style scoped>
.mention-popup {
  position: fixed;
  z-index: 3000;
  display: flex;
  align-items: stretch;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  font-size: 13px;
  /* Anchor above the caret (top/left point at the trigger glyph). */
  transform: translateY(-100%);
  overflow: hidden;
}

.mention-menu {
  margin: 0;
  padding: 4px 0;
  list-style: none;
  min-width: 220px;
  max-width: 360px;
  max-height: 240px;
  overflow-y: auto;
  flex-shrink: 0;
}

.mention-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 10px;
  cursor: pointer;
  color: var(--text-primary);
}

.mention-item:hover,
.mention-item.active {
  background: rgba(37, 99, 235, 0.1);
}

.mention-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
  color: var(--primary-color);
}

.mention-desc {
  font-size: 11.5px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Detail aside — full (untruncated) description + current value for the active
   item. */
.mention-detail {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 240px;
  padding: 8px 10px;
  border-left: 1px solid var(--border-color);
  background: var(--surface-color);
  overflow-y: auto;
  max-height: 240px;
}

.detail-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
  color: var(--primary-color);
}

.detail-desc {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--text-secondary);
  white-space: normal;
  word-break: break-word;
}

.detail-value-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.detail-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px;
  color: var(--text-primary);
  word-break: break-all;
  overflow-wrap: anywhere;
}

.detail-value--unset {
  color: var(--text-tertiary);
  font-style: italic;
}

.reveal-btn {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  color: var(--text-tertiary);
  cursor: pointer;
  font-size: 15px;
  transition: all 0.2s ease;
}

.reveal-btn:hover {
  border-color: var(--primary-color);
  color: var(--primary-color);
}
</style>
