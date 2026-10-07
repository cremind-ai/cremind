// Test entry for tests/appearance.test.mjs: the appearance store with the
// Pinia it runs on (harness.load() bundles a fresh copy per call), and the
// pure theme, font and Thinking Process helpers.
export { createPinia, setActivePinia } from 'pinia';
export { useAppearanceStore } from '../../src/stores/appearance';
export { useSettingsStore } from '../../src/stores/settings';
export { contrast, mix, readableOn } from '../../src/appearance/color';
export {
  DEFAULT_APPEARANCE,
  FONT_PRESETS,
  THEME_PRESETS,
  appearanceFromConfig,
  appearanceToConfig,
  customPalette,
  fontStack,
  normalizeAppearance,
  resolvePalette,
  themeChoices,
} from '../../src/appearance/presets';
export { computeAppearance, themeTokens } from '../../src/appearance/tokens';
export { openAfterModeChange, shouldAutoClose, shouldAutoOpen } from '../../src/utils/thinkingDisclosure';
