<script setup lang="ts">
import { computed } from 'vue';
import { Icon } from '@iconify/vue';
import { useCopyToClipboard } from '../../composables/useCopyToClipboard';

// Shows a truncated id with a copy button that copies the full UUID. Used
// across the Events page so users can hand an id to the agent ("what is event
// id <id>?"). `kind` distinguishes an "Event" (the automation definition /
// subscription) from a "Run" (one firing of it) — the two carry different ids
// and are acted on by different CLI commands, so the label is required. Root
// and button both stop click propagation so the chip is safe inside
// click-to-open table rows and board cards.
const props = withDefaults(
  defineProps<{ id: string; kind: 'event' | 'run'; size?: 'sm' | 'xs' }>(),
  { size: 'sm' },
);

const { copy, isCopied } = useCopyToClipboard();
const shortId = computed(() => props.id.slice(0, 8));
const kindLabel = computed(() => (props.kind === 'run' ? 'Run' : 'Event'));
</script>

<template>
  <span class="id-chip" :class="`size-${size}`" @click.stop>
    <span class="id-kind">{{ kindLabel }}</span>
    <code class="id-text" :title="id">{{ shortId }}</code>
    <button
      type="button"
      class="copy-icon-btn"
      :class="{ copied: isCopied() }"
      :title="isCopied() ? 'Copied!' : `Copy ${kind} id ${id}`"
      :aria-label="`Copy ${kind} id ${id}`"
      @click.stop="copy(id)"
    >
      <Icon :icon="isCopied() ? 'mdi:check' : 'mdi:content-copy'" />
    </button>
  </span>
</template>

<style scoped>
.id-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  white-space: nowrap;
  vertical-align: middle;
}
.id-kind {
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 3px;
  padding: 0 3px;
  line-height: 1.4;
}
.size-sm .id-kind { font-size: 0.625rem; }
.size-xs .id-kind { font-size: 0.5625rem; }
.id-text {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  color: var(--text-tertiary);
  background: none;
  letter-spacing: 0.02em;
}
.size-sm .id-text { font-size: 0.75rem; }
.size-xs .id-text { font-size: 0.6875rem; }

.copy-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  cursor: pointer;
  padding: 2px;
  border-radius: 4px;
  color: var(--text-secondary);
  transition: color 0.15s ease;
}
.size-sm .copy-icon-btn { font-size: 0.85rem; }
.size-xs .copy-icon-btn { font-size: 0.75rem; }
.copy-icon-btn:hover { color: var(--primary-color); }
.copy-icon-btn.copied { color: var(--success-color, #67c23a); }
</style>
