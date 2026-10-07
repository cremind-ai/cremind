<script setup lang="ts">
/**
 * A thumbnail of a theme: a miniature chat — side rail, an agent message, a
 * reply of yours in the accent color — painted in the theme's own colors
 * rather than the ones on screen. With `second`, the right half shows that
 * palette instead (Match system: light on the left, dark on the right).
 */
import { computed } from 'vue';
import type { Palette } from '../../appearance/presets';
import { themeTokens } from '../../appearance/tokens';

const props = defineProps<{
  palette: Palette;
  second?: Palette | null;
}>();

function look(p: Palette): Record<string, string> {
  const t = themeTokens(p);
  return {
    '--sw-bg': p.background,
    '--sw-surface': p.surface,
    '--sw-border': t['--border-color'],
    '--sw-text': p.text,
    '--sw-muted': t['--text-tertiary'],
    '--sw-accent': p.accent,
    '--sw-on-accent': t['--on-primary'],
  };
}

const first = computed(() => look(props.palette));
const other = computed(() => (props.second ? look(props.second) : null));
</script>

<template>
  <span class="swatch" aria-hidden="true">
    <span v-for="(style, i) in (other ? [first, other] : [first])" :key="i" class="mini" :class="{ second: i === 1 }" :style="style">
      <span class="mini-rail"><span class="mini-dot" /></span>
      <span class="mini-main">
        <span class="mini-agent">
          <span class="mini-line" />
          <span class="mini-line short" />
        </span>
        <span class="mini-user"><span class="mini-line on-accent" /></span>
      </span>
    </span>
  </span>
</template>

<style scoped>
.swatch {
  position: relative;
  display: block;
  width: 100%;
  aspect-ratio: 16 / 10;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--border-color);
  box-sizing: border-box;
}

.mini {
  position: absolute;
  inset: 0;
  display: flex;
  background: var(--sw-bg);
}

/* The second palette takes the right half, cut on a slant. */
.mini.second {
  clip-path: polygon(58% 0, 100% 0, 100% 100%, 42% 100%);
}

.mini-rail {
  width: 16%;
  background: var(--sw-surface);
  border-right: 1px solid var(--sw-border);
  display: flex;
  justify-content: center;
  padding-top: 14%;
  box-sizing: border-box;
}

.mini-dot {
  width: 42%;
  aspect-ratio: 1;
  border-radius: 30%;
  background: var(--sw-accent);
  height: fit-content;
}

.mini-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 10%;
  padding: 0 9%;
}

.mini-agent {
  align-self: flex-start;
  width: 72%;
  padding: 7% 8%;
  border-radius: 6px 6px 6px 2px;
  background: var(--sw-surface);
  border: 1px solid var(--sw-border);
  display: flex;
  flex-direction: column;
  gap: 5px;
  box-sizing: border-box;
}

.mini-user {
  align-self: flex-end;
  width: 46%;
  padding: 6% 8%;
  border-radius: 6px 6px 2px 6px;
  background: var(--sw-accent);
  box-sizing: border-box;
  display: flex;
}

.mini-line {
  display: block;
  height: 4px;
  width: 100%;
  border-radius: 2px;
  background: var(--sw-text);
  opacity: 0.75;
}

.mini-line.short {
  width: 60%;
  background: var(--sw-muted);
}

.mini-line.on-accent {
  background: var(--sw-on-accent);
  opacity: 0.9;
}
</style>
