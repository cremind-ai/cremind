<script setup lang="ts">
/**
 * The side rail's theme menu: every theme as a thumbnail, one click to switch,
 * and the way to the Appearance page for fonts, text size and custom colors.
 * The Custom thumbnail appears once this profile has custom colors — until
 * then picking it would only copy the theme already on screen.
 */
import { computed } from 'vue';
import { Icon } from '@iconify/vue';
import { ElMessage } from 'element-plus';
import { useAppearanceStore } from '../../stores/appearance';
import { themeChoices } from '../../appearance/presets';
import ThemeSwatch from './ThemeSwatch.vue';

const emit = defineEmits<{ 'open-settings': [] }>();

const appearance = useAppearanceStore();

const choices = computed(() =>
  themeChoices(appearance.settings).filter(
    c => c.id !== 'custom' || appearance.hasCustomColors || appearance.settings.theme === 'custom',
  ));

async function pick(id: string) {
  try {
    await appearance.chooseTheme(id);
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Could not save the theme');
  }
}
</script>

<template>
  <!-- rail-menu: the rail's menu-row styles (NavRail.vue) for the link below. -->
  <div class="theme-picker rail-menu">
    <div class="theme-picker-title">Theme</div>
    <div class="theme-picker-grid" role="radiogroup" aria-label="Theme">
      <button
        v-for="choice in choices"
        :key="choice.id"
        type="button"
        role="radio"
        class="theme-picker-option"
        :class="{ selected: appearance.settings.theme === choice.id }"
        :aria-checked="appearance.settings.theme === choice.id"
        :title="choice.description"
        @click="pick(choice.id)"
      >
        <span class="theme-picker-thumb">
          <ThemeSwatch :palette="choice.palette" :second="choice.second" />
          <Icon v-if="appearance.settings.theme === choice.id" icon="mdi:check-circle" class="theme-picker-check" />
        </span>
        <span class="theme-picker-label">{{ choice.label }}</span>
      </button>
    </div>
    <div class="rail-menu-divider" />
    <button type="button" class="rail-menu-item theme-picker-more" @click="emit('open-settings')">
      <Icon icon="mdi:tune-variant" class="rail-menu-icon" />
      <span>Fonts, text size &amp; custom colors…</span>
    </button>
  </div>
</template>

<style scoped>
.theme-picker {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.theme-picker-title {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 4px 6px 0;
}

.theme-picker-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  padding: 2px;
}

.theme-picker-option {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
  padding: 4px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  cursor: pointer;
  color: var(--text-secondary);
  font: inherit;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.theme-picker-option:hover {
  background: var(--hover-bg);
}

.theme-picker-option.selected {
  border-color: var(--primary-color);
  color: var(--text-primary);
}

.theme-picker-option:focus-visible {
  outline: 2px solid var(--primary-color);
  outline-offset: 1px;
}

.theme-picker-thumb {
  position: relative;
  display: block;
}

.theme-picker-check {
  position: absolute;
  top: 3px;
  right: 3px;
  font-size: 16px;
  color: var(--primary-color);
  background: var(--surface-color);
  border-radius: 50%;
}

.theme-picker-label {
  font-size: 0.72rem;
  line-height: 1.2;
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.theme-picker-more {
  font: inherit;
}
</style>
