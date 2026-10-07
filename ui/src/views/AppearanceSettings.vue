<script setup lang="ts">
/**
 * Settings → Appearance: the color theme (a preset, the device's light/dark,
 * or custom colors), and the font and text size. Every change shows at once —
 * the page itself is the preview — and is saved for the profile, so there is
 * no save bar (stores/appearance.ts). Whether a reply's Thinking Process opens
 * by itself is not here: it is the Auto-open switch on the row itself.
 */
import { computed, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import { ElButton, ElInput, ElMessage, ElMessageBox, ElOption, ElSelect, ElSlider } from 'element-plus';
import { useAppearanceStore } from '../stores/appearance';
import {
  FONT_PRESETS,
  FONT_SIZE_MAX,
  FONT_SIZE_MIN,
  FONT_SIZE_STEP,
  THEME_PRESETS,
  customPalette,
  isFontFamily,
  themeChoices,
  themePreset,
  type AppearanceSettings,
} from '../appearance/presets';
import { contrast, parseHex, toHex } from '../appearance/color';
import ThemeSwatch from '../components/appearance/ThemeSwatch.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const appearance = useAppearanceStore();

const s = computed(() => appearance.settings);

function save(patch: Partial<AppearanceSettings>) {
  appearance.update(patch).catch((e: unknown) => {
    ElMessage.error(e instanceof Error ? e.message : 'Could not save the appearance');
  });
}

function goBack() {
  router.push(`/${props.profile}/settings`);
}

// ── Theme ──

const choices = computed(() => themeChoices(s.value));

function pickTheme(id: string) {
  appearance.chooseTheme(id).catch((e: unknown) => {
    ElMessage.error(e instanceof Error ? e.message : 'Could not save the theme');
  });
}

// ── Custom colors ──

type ColorField = 'customAccent' | 'customBackground' | 'customSurface' | 'customText';

const COLOR_FIELDS: { field: ColorField; label: string; hint: string }[] = [
  { field: 'customBackground', label: 'Background', hint: 'The page behind everything. A dark one makes this a dark theme.' },
  { field: 'customSurface', label: 'Panels', hint: 'Panels, cards, menus and the agent’s messages.' },
  { field: 'customText', label: 'Text', hint: 'Body text. Secondary text and borders are mixed from it.' },
  { field: 'customAccent', label: 'Accent', hint: 'Buttons, links, highlights and your own messages.' },
];

// What each hex box shows while it is being typed in.
const hexDrafts = ref<Record<ColorField, string>>({
  customAccent: '', customBackground: '', customSurface: '', customText: '',
});
watch(
  () => COLOR_FIELDS.map(c => s.value[c.field]),
  () => { for (const c of COLOR_FIELDS) hexDrafts.value[c.field] = s.value[c.field]; },
  { immediate: true },
);

function setColor(field: ColorField, value: string) {
  const parsed = parseHex(value);
  if (!parsed) {
    hexDrafts.value[field] = s.value[field];
    return;
  }
  const hex = toHex(parsed);
  hexDrafts.value[field] = hex;
  if (hex !== s.value[field]) save({ [field]: hex });
}

const copyFrom = ref('');
function copyColors(id: string) {
  const preset = themePreset(id);
  copyFrom.value = '';
  if (!preset) return;
  const p = preset.palette;
  save({
    customAccent: p.accent,
    customBackground: p.background,
    customSurface: p.surface,
    customText: p.text,
  });
}

/** Pairs a reader would struggle with, and how badly. */
const readability = computed(() => {
  const p = customPalette(s.value);
  const warnings: string[] = [];
  const check = (a: string, b: string, min: number, what: string) => {
    const ratio = contrast(a, b);
    if (ratio < min) warnings.push(`${what} (${ratio.toFixed(1)}:1 — aim for ${min}:1 or more).`);
  };
  check(p.text, p.surface, 4.5, 'Text is hard to read on the panels');
  check(p.text, p.background, 4.5, 'Text is hard to read on the background');
  check(p.accent, p.surface, 3, 'Links and icons in the accent color are hard to see on the panels');
  return warnings;
});

// ── Font ──

const customFontDraft = ref(s.value.customFont);
watch(() => s.value.customFont, (v) => { customFontDraft.value = v; });
const customFontValid = computed(() => isFontFamily(customFontDraft.value));

function pickFont(id: string) {
  if (id !== s.value.font) save({ font: id });
}

function applyCustomFont() {
  if (!customFontValid.value) return;
  const value = customFontDraft.value.trim();
  if (value !== s.value.customFont || s.value.font !== 'custom') {
    save({ font: 'custom', customFont: value });
  }
}

const customFontStack = computed(() =>
  customFontValid.value ? `${customFontDraft.value}, system-ui, sans-serif` : undefined);

// ── Text size ──

const fontSizeDraft = ref(s.value.fontSize);
watch(() => s.value.fontSize, (v) => { fontSizeDraft.value = v; });
const sizeMarks = { 80: '80%', 100: '100%', 120: '120%', 140: '140%' };

function setFontSize(value: number | number[]) {
  const size = Array.isArray(value) ? value[0] : value;
  if (size !== s.value.fontSize) save({ fontSize: size });
}

// ── Reset ──

async function resetAll() {
  try {
    await ElMessageBox.confirm(
      'Go back to the Light theme and the default font and text size? Your custom colors are cleared too.',
      'Reset appearance',
      { type: 'warning', confirmButtonText: 'Reset', cancelButtonText: 'Cancel' },
    );
  } catch {
    return;
  }
  try {
    await appearance.resetAll();
    ElMessage.success('Appearance reset');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Could not reset the appearance');
  }
}
</script>

<template>
  <div class="appearance-page">
    <div class="appearance-container">
      <div class="appearance-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Settings
        </button>
        <h1 class="appearance-title">Appearance</h1>
        <p class="appearance-subtitle">
          Saved for profile <strong>{{ profile }}</strong> on every device it signs in on.
          <span class="save-state" :class="{ visible: appearance.saving }">Saving…</span>
        </p>
      </div>

      <!-- Theme -->
      <section class="ap-section" aria-labelledby="ap-theme">
        <h2 id="ap-theme" class="ap-heading">Theme</h2>
        <div class="theme-grid" role="radiogroup" aria-labelledby="ap-theme">
          <button
            v-for="choice in choices"
            :key="choice.id"
            type="button"
            role="radio"
            class="theme-card"
            :class="{ selected: s.theme === choice.id }"
            :aria-checked="s.theme === choice.id"
            @click="pickTheme(choice.id)"
          >
            <span class="theme-thumb">
              <ThemeSwatch :palette="choice.palette" :second="choice.second" />
              <Icon v-if="s.theme === choice.id" icon="mdi:check-circle" class="theme-check" />
            </span>
            <span class="theme-name">{{ choice.label }}</span>
            <span class="theme-desc">{{ choice.description }}</span>
          </button>
        </div>
      </section>

      <!-- Custom colors -->
      <section v-if="s.theme === 'custom'" class="ap-section" aria-labelledby="ap-custom">
        <div class="ap-heading-row">
          <h2 id="ap-custom" class="ap-heading">Custom colors</h2>
          <ElSelect
            v-model="copyFrom"
            class="copy-from"
            placeholder="Copy colors from a theme…"
            size="small"
            @change="copyColors"
          >
            <ElOption v-for="preset in THEME_PRESETS" :key="preset.id" :label="preset.label" :value="preset.id" />
          </ElSelect>
        </div>
        <p class="ap-hint">Four colors make the theme; hover shades, borders and secondary text are mixed from them.</p>
        <div class="color-list">
          <div v-for="c in COLOR_FIELDS" :key="c.field" class="color-row">
            <label class="color-swatch" :style="{ background: s[c.field] }">
              <input
                type="color"
                class="color-native"
                :value="s[c.field]"
                :aria-label="`${c.label} color`"
                @input="setColor(c.field, ($event.target as HTMLInputElement).value)"
              />
            </label>
            <div class="color-text">
              <span class="color-label">{{ c.label }}</span>
              <span class="color-hint">{{ c.hint }}</span>
            </div>
            <ElInput
              v-model="hexDrafts[c.field]"
              class="color-hex"
              size="small"
              maxlength="7"
              :aria-label="`${c.label} hex value`"
              @change="setColor(c.field, $event)"
            />
          </div>
        </div>
        <ul v-if="readability.length" class="readability">
          <li v-for="w in readability" :key="w">
            <Icon icon="mdi:alert-outline" class="readability-icon" />
            {{ w }}
          </li>
        </ul>
      </section>

      <!-- Font -->
      <section class="ap-section" aria-labelledby="ap-font">
        <h2 id="ap-font" class="ap-heading">Font</h2>
        <p class="ap-hint">A font shows only where the device has it; each choice falls back to a look-alike.</p>
        <div class="font-grid" role="radiogroup" aria-labelledby="ap-font">
          <button
            v-for="font in FONT_PRESETS"
            :key="font.id"
            type="button"
            role="radio"
            class="font-card"
            :class="{ selected: s.font === font.id }"
            :aria-checked="s.font === font.id"
            @click="pickFont(font.id)"
          >
            <span class="font-sample" :style="{ fontFamily: font.stack }">Aa</span>
            <span class="font-name" :style="{ fontFamily: font.stack }">{{ font.label }}</span>
            <span class="font-desc">{{ font.description }}</span>
          </button>
          <button
            type="button"
            role="radio"
            class="font-card"
            :class="{ selected: s.font === 'custom' }"
            :aria-checked="s.font === 'custom'"
            @click="applyCustomFont"
          >
            <span class="font-sample" :style="{ fontFamily: customFontStack }">Aa</span>
            <span class="font-name">Custom</span>
            <span class="font-desc">Any font installed on this device.</span>
          </button>
        </div>
        <div v-if="s.font === 'custom'" class="custom-font">
          <ElInput
            v-model="customFontDraft"
            placeholder="e.g. Georgia, serif"
            maxlength="200"
            aria-label="Custom font family"
            @change="applyCustomFont"
          />
          <p v-if="!customFontValid" class="field-error">
            Use font names separated by commas — letters, digits, spaces, quotes, dots, dashes and underscores only.
          </p>
          <p v-else class="ap-hint">The first font on the list that this device has is used; press Enter to apply.</p>
        </div>
      </section>

      <!-- Text size -->
      <section class="ap-section" aria-labelledby="ap-size">
        <div class="ap-heading-row">
          <h2 id="ap-size" class="ap-heading">Text size</h2>
          <span class="size-value">{{ fontSizeDraft }}%</span>
        </div>
        <div class="size-slider">
          <ElSlider
            v-model="fontSizeDraft"
            :min="FONT_SIZE_MIN"
            :max="FONT_SIZE_MAX"
            :step="FONT_SIZE_STEP"
            :marks="sizeMarks"
            show-stops
            :format-tooltip="(v: number) => `${v}%`"
            aria-label="Text size"
            @change="setFontSize"
          />
        </div>
        <p class="size-sample">
          The quick brown fox jumps over the lazy dog. 0123456789
        </p>
      </section>

      <div class="ap-footer">
        <ElButton @click="resetAll">
          <Icon icon="mdi:restore" class="btn-icon" />
          Reset to defaults
        </ElButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.appearance-page {
  width: 100%;
  height: 100%;
  overflow-y: auto;
  background: var(--bg-color);
  padding: 24px;
  box-sizing: border-box;
}
.appearance-container { max-width: 880px; margin: 0 auto; padding-bottom: 32px; }
.appearance-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.appearance-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.appearance-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.save-state {
  margin-left: 8px;
  color: var(--text-tertiary);
  opacity: 0;
  transition: opacity 0.2s ease;
}
.save-state.visible { opacity: 1; }

.ap-section {
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 18px 20px 20px;
  margin-bottom: 16px;
}
.ap-heading { font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 4px 0; }
.ap-heading-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 4px;
}
.ap-heading-row .ap-heading { margin: 0; }
.ap-hint { font-size: 0.82rem; color: var(--text-secondary); margin: 0 0 12px 0; line-height: 1.45; }

/* ── Theme cards ── */
.theme-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px;
  margin-top: 12px;
}
.theme-card,
.font-card {
  font: inherit;
  text-align: left;
  cursor: pointer;
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 10px;
  color: var(--text-primary);
  transition: border-color 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
}
.theme-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 8px 10px;
}
.theme-card:hover,
.font-card:hover { border-color: var(--border-hover); background: var(--surface-hover); }
.theme-card.selected,
.font-card.selected {
  border-color: var(--primary-color);
  box-shadow: 0 0 0 1px var(--primary-color);
}
.theme-card:focus-visible,
.font-card:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
.theme-thumb { position: relative; display: block; margin-bottom: 4px; }
.theme-check {
  position: absolute;
  top: 5px;
  right: 5px;
  font-size: 18px;
  color: var(--primary-color);
  background: var(--surface-color);
  border-radius: 50%;
}
.theme-name { font-size: 0.875rem; font-weight: 600; padding: 0 2px; }
.theme-desc { font-size: 0.75rem; color: var(--text-secondary); line-height: 1.35; padding: 0 2px; }

/* ── Custom colors ── */
.copy-from { width: 210px; }
.color-list { display: flex; flex-direction: column; gap: 10px; }
.color-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.color-swatch {
  position: relative;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  border: 1px solid var(--border-hover);
  flex-shrink: 0;
  cursor: pointer;
  overflow: hidden;
}
.color-native {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  opacity: 0;
  cursor: pointer;
  border: none;
  padding: 0;
}
.color-text { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.color-label { font-size: 0.875rem; font-weight: 600; color: var(--text-primary); }
.color-hint { font-size: 0.78rem; color: var(--text-secondary); line-height: 1.35; }
.color-hex { width: 100px; flex-shrink: 0; }
.color-hex :deep(input) { font-family: ui-monospace, 'Cascadia Code', Menlo, Consolas, monospace; }
/* Fixed colors, not the theme's: this box says the theme's text is hard to
   read, so it must not be written in that text. */
.readability {
  list-style: none;
  margin: 14px 0 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: #fef3c7;
  border: 1px solid #f59e0b;
  color: #78350f;
  font-size: 0.82rem;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.readability li { display: flex; align-items: flex-start; gap: 6px; line-height: 1.4; }
.readability-icon { color: #b45309; flex-shrink: 0; margin-top: 2px; }

/* ── Fonts ── */
.font-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px;
}
.font-card {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px 12px;
}
.font-sample { font-size: 1.9rem; line-height: 1.15; color: var(--text-primary); }
.font-name { font-size: 0.9rem; font-weight: 600; }
.font-desc { font-size: 0.75rem; color: var(--text-secondary); line-height: 1.35; }
.custom-font { margin-top: 12px; max-width: 420px; }
.custom-font .ap-hint { margin: 6px 0 0; }
.field-error { font-size: 0.8rem; color: var(--danger-color); margin: 6px 0 0; }

/* ── Text size ── */
.size-value { font-size: 0.875rem; font-weight: 600; color: var(--primary-color); font-variant-numeric: tabular-nums; }
.size-slider { padding: 4px 12px 22px; }
.size-sample { margin: 8px 0 0; color: var(--text-primary); line-height: 1.6; }

.ap-footer { display: flex; justify-content: flex-end; }
.btn-icon { margin-right: 6px; }
</style>
