/**
 * Turns appearance settings into what the document needs: the CSS variables
 * every component reads (style.css names them), the light/dark mode, and
 * whether text on the accent color must be dark.
 *
 * Element Plus is themed through the same variables. Its tint scales
 * (`--el-color-primary-light-3` … `-light-9`, `-dark-2`) are generated here
 * for every status color, toward white in a light theme and toward the
 * background in a dark one — a scale left undeclared keeps Element Plus's own
 * light-theme value, which is how a dark theme ended up with pale tags.
 */

import { mix, readableOn, withAlpha } from './color';
import {
  fontStack,
  resolvePalette,
  type AppearanceSettings,
  type Palette,
  type ThemeMode,
} from './presets';

export interface AppliedAppearance {
  mode: ThemeMode;
  /** `dark` when the accent is too light for white text on it. */
  onPrimary: 'light' | 'dark';
  vars: Record<string, string>;
}

const TINTS = [3, 5, 7, 8, 9] as const;

/** Every color variable for one palette. */
export function themeTokens(p: Palette): Record<string, string> {
  const light = p.mode === 'light';
  const { accent, background: bg, surface, text } = p;
  const surfaceHover = p.surfaceHover ?? mix(surface, text, light ? 0.05 : 0.1);
  const border = p.border ?? mix(surface, text, light ? 0.12 : 0.16);
  const borderHover = p.borderHover ?? mix(surface, text, light ? 0.22 : 0.26);
  const textSecondary = p.textSecondary ?? mix(text, surface, light ? 0.32 : 0.24);
  const textTertiary = p.textTertiary ?? mix(text, surface, light ? 0.5 : 0.45);
  const success = p.success ?? (light ? '#10b981' : '#34d399');
  const danger = p.danger ?? (light ? '#ef4444' : '#f87171');
  const warning = p.warning ?? (light ? '#f59e0b' : '#fbbf24');
  const info = p.info ?? textSecondary;
  // What Element Plus's "light-N" tints fade toward.
  const fade = light ? '#ffffff' : bg;
  // The dark strip over the Workspace and terminal panels: a near-black of
  // the theme's own hue, so it reads as part of the theme in a warm or green
  // one rather than as a navy bar.
  const strip = light ? mix(text, '#000000', 0.55) : mix(bg, '#000000', 0.3);

  const vars: Record<string, string> = {
    '--primary-color': accent,
    '--primary-light': mix(accent, '#ffffff', light ? 0.15 : 0.2),
    '--primary-dark': mix(accent, '#000000', 0.15),
    '--on-primary': readableOn(accent, light ? text : bg),
    '--bg-color': bg,
    '--surface-color': surface,
    '--surface-hover': surfaceHover,
    '--border-color': border,
    '--border-hover': borderHover,
    '--text-primary': text,
    '--text-secondary': textSecondary,
    '--text-tertiary': textTertiary,
    '--success-color': success,
    '--danger-color': danger,
    '--warning-color': warning,
    '--sidebar-bg': surface,
    '--hover-bg': surfaceHover,
    '--strip-bg': strip,
    '--strip-bg-deep': mix(strip, '#000000', 0.25),
    '--strip-border': mix(strip, '#ffffff', 0.1),
    '--strip-hover': mix(strip, '#ffffff', 0.1),
    '--strip-text': mix(strip, '#ffffff', 0.58),
    '--strip-title': mix(strip, '#ffffff', 0.78),
    '--strip-text-strong': mix(strip, '#ffffff', 0.88),

    '--el-bg-color': bg,
    '--el-bg-color-page': bg,
    '--el-bg-color-overlay': withAlpha(surface, 0.97),
    '--el-text-color-primary': text,
    '--el-text-color-regular': mix(text, surface, light ? 0.12 : 0.06),
    '--el-text-color-secondary': textSecondary,
    '--el-text-color-placeholder': textTertiary,
    '--el-text-color-disabled': mix(text, surface, light ? 0.6 : 0.55),
    '--el-border-color': border,
    '--el-border-color-light': mix(surface, text, light ? 0.08 : 0.13),
    '--el-border-color-lighter': mix(surface, text, light ? 0.05 : 0.1),
    '--el-border-color-extra-light': mix(surface, text, light ? 0.03 : 0.07),
    '--el-border-color-dark': borderHover,
    '--el-border-color-darker': mix(surface, text, light ? 0.3 : 0.34),
    '--el-fill-color-blank': surface,
    '--el-fill-color': mix(surface, text, light ? 0.03 : 0.12),
    '--el-fill-color-light': surfaceHover,
    '--el-fill-color-lighter': mix(surface, text, light ? 0.025 : 0.05),
    '--el-fill-color-extra-light': mix(surface, text, light ? 0.015 : 0.03),
    '--el-fill-color-dark': mix(surface, text, light ? 0.08 : 0.16),
    '--el-fill-color-darker': mix(surface, text, light ? 0.11 : 0.2),
    '--el-mask-color': withAlpha(bg, 0.8),
    '--el-mask-color-extra-light': withAlpha(bg, 0.3),
    '--el-box-shadow': `0 1px 3px rgba(0, 0, 0, ${light ? 0.1 : 0.3})`,
    '--el-box-shadow-light': `0 1px 2px rgba(0, 0, 0, ${light ? 0.05 : 0.2})`,
    '--el-box-shadow-lighter': `0 1px 2px rgba(0, 0, 0, ${light ? 0.03 : 0.1})`,
  };

  const statuses: [string, string][] = [
    ['primary', accent], ['success', success], ['warning', warning],
    ['danger', danger], ['error', danger], ['info', info],
  ];
  for (const [name, color] of statuses) {
    vars[`--el-color-${name}`] = color;
    for (const n of TINTS) vars[`--el-color-${name}-light-${n}`] = mix(color, fade, n / 10);
    // Pressed and active states: darker in a light theme, lighter in a dark one.
    vars[`--el-color-${name}-dark-2`] = mix(color, light ? '#000000' : '#ffffff', 0.2);
  }

  return { ...vars, ...p.vars };
}

/** The font variables: the family, and the root size every `rem` scales by. */
export function fontTokens(s: Pick<AppearanceSettings, 'font' | 'customFont' | 'fontSize'>): Record<string, string> {
  return {
    '--font-sans': fontStack(s),
    '--root-font-size': `${s.fontSize}%`,
  };
}

/** Everything to apply for these settings (`systemDark` decides "system"). */
export function computeAppearance(s: AppearanceSettings, systemDark: boolean): AppliedAppearance {
  const palette = resolvePalette(s, systemDark);
  const vars = { ...themeTokens(palette), ...fontTokens(s) };
  return {
    mode: palette.mode,
    onPrimary: vars['--on-primary'] === '#ffffff' ? 'light' : 'dark',
    vars,
  };
}
