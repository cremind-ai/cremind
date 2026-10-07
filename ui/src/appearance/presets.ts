/**
 * The appearance presets — color themes and font stacks — and the per-profile
 * settings that pick among them.
 *
 * The settings live server-side as the ``appearance.*`` keys of the profile's
 * config (app/config/config_schema.py), so the ids here must match that
 * schema's enums: tests/api/test_user_config_appearance.py reads the `id:`
 * lines of THEME_PRESETS and FONT_PRESETS, and DEFAULT_APPEARANCE, out of this
 * file and compares them with the server.
 */

import { isHexColor, luminance } from './color';

export type ThemeMode = 'light' | 'dark';

/**
 * A theme's colors. Four are required; everything else (hover shades, borders,
 * secondary text, Element Plus's tint scales) is derived from them by
 * themeTokens() unless given here.
 */
export interface Palette {
  mode: ThemeMode;
  accent: string;
  background: string;
  surface: string;
  text: string;
  surfaceHover?: string;
  border?: string;
  borderHover?: string;
  textSecondary?: string;
  textTertiary?: string;
  success?: string;
  danger?: string;
  warning?: string;
  info?: string;
  /** Exact CSS variable values that win over anything derived. */
  vars?: Record<string, string>;
}

export interface ThemePreset {
  id: string;
  label: string;
  description: string;
  palette: Palette;
}

export interface FontPreset {
  id: string;
  label: string;
  description: string;
  stack: string;
}

// Light and Dark are today's two themes, value for value (style.css keeps the
// same values as the no-JavaScript fallback), so nobody's app changes look
// on upgrade. Both share the navy strip the Workspace and terminal panels
// have always had.
const NAVY_STRIP: Record<string, string> = {
  '--strip-bg': '#0f172a',
  '--strip-bg-deep': '#0b1220',
  '--strip-border': '#1f2937',
  '--strip-hover': '#1e293b',
  '--strip-text': '#94a3b8',
  '--strip-title': '#cbd5f5',
  '--strip-text-strong': '#e5e7eb',
};

const LIGHT_VARS: Record<string, string> = {
  ...NAVY_STRIP,
  '--primary-color': '#2563eb',
  '--primary-light': '#3b82f6',
  '--primary-dark': '#1d4ed8',
  '--sidebar-bg': '#ffffff',
  '--hover-bg': '#f1f5f9',
  '--el-color-primary-light-3': '#3b82f6',
  '--el-color-primary-light-5': '#60a5fa',
  '--el-color-primary-light-7': '#93c5fd',
  '--el-color-primary-light-9': '#dbeafe',
  '--el-color-primary-dark-2': '#1d4ed8',
  '--el-color-info': '#64748b',
  '--el-bg-color-overlay': 'rgba(255, 255, 255, 0.95)',
  '--el-text-color-regular': '#475569',
  '--el-text-color-placeholder': '#94a3b8',
  '--el-border-color-light': '#f1f5f9',
  '--el-border-color-lighter': '#f8fafc',
  '--el-border-color-extra-light': '#f8fafc',
  '--el-fill-color': '#f8fafc',
  '--el-fill-color-light': '#f1f5f9',
  '--el-fill-color-lighter': '#f8fafc',
  '--el-fill-color-dark': '#ebedf0',
  '--el-fill-color-darker': '#e6e8eb',
};

const DARK_VARS: Record<string, string> = {
  ...NAVY_STRIP,
  '--primary-color': '#3b82f6',
  '--primary-light': '#60a5fa',
  '--primary-dark': '#2563eb',
  '--sidebar-bg': '#1e293b',
  '--hover-bg': '#334155',
  '--el-color-primary-light-3': '#60a5fa',
  '--el-color-primary-light-5': '#93c5fd',
  '--el-color-primary-light-7': '#3b82f6',
  '--el-color-primary-light-9': '#1e3a8a',
  '--el-color-primary-dark-2': '#2563eb',
  '--el-color-info': '#94a3b8',
  '--el-bg-color-overlay': 'rgba(30, 41, 59, 0.95)',
  '--el-text-color-regular': '#e2e8f0',
  '--el-text-color-placeholder': '#64748b',
  '--el-border-color-light': '#334155',
  '--el-border-color-lighter': '#1e293b',
  '--el-border-color-extra-light': '#1e293b',
  '--el-fill-color': '#334155',
  '--el-fill-color-light': '#334155',
  '--el-fill-color-lighter': '#1e293b',
  '--el-fill-color-dark': '#334155',
  '--el-fill-color-darker': '#0f172a',
};

/** The named themes, in the order the pickers show them. */
export const THEME_PRESETS: ThemePreset[] = [
  {
    id: 'light',
    label: 'Light',
    description: 'Clean white panels with a blue accent.',
    palette: {
      mode: 'light',
      accent: '#2563eb',
      background: '#f8fafc',
      surface: '#ffffff',
      text: '#334155',
      surfaceHover: '#f1f5f9',
      border: '#e2e8f0',
      borderHover: '#cbd5e1',
      textSecondary: '#64748b',
      textTertiary: '#94a3b8',
      success: '#10b981',
      danger: '#ef4444',
      warning: '#f59e0b',
      vars: LIGHT_VARS,
    },
  },
  {
    id: 'dark',
    label: 'Dark',
    description: 'Deep navy panels with a blue accent.',
    palette: {
      mode: 'dark',
      accent: '#3b82f6',
      background: '#0f172a',
      surface: '#1e293b',
      text: '#f1f5f9',
      surfaceHover: '#334155',
      border: '#334155',
      borderHover: '#475569',
      textSecondary: '#cbd5e1',
      textTertiary: '#94a3b8',
      success: '#34d399',
      danger: '#f87171',
      warning: '#fbbf24',
      vars: DARK_VARS,
    },
  },
  {
    id: 'paper',
    label: 'Paper',
    description: 'Warm cream and sepia, softer on the eyes than white.',
    palette: {
      mode: 'light',
      accent: '#a0522d',
      background: '#f4eee2',
      surface: '#fbf8f1',
      text: '#3d3428',
      surfaceHover: '#efe7d6',
      border: '#e2d7c2',
      borderHover: '#cdbfa3',
      textSecondary: '#6b5d4b',
      textTertiary: '#998a73',
      success: '#4d7c0f',
      danger: '#b91c1c',
      warning: '#b45309',
    },
  },
  {
    id: 'mint',
    label: 'Mint',
    description: 'Pale sage with a calm green accent.',
    palette: {
      mode: 'light',
      accent: '#047857',
      background: '#f1f6f3',
      surface: '#ffffff',
      text: '#1e3a2f',
      surfaceHover: '#e6efe9',
      border: '#d2e2d8',
      borderHover: '#b3cdbd',
      textSecondary: '#4f6b5d',
      textTertiary: '#85a092',
      success: '#15803d',
      danger: '#dc2626',
      warning: '#b45309',
    },
  },
  {
    id: 'rose',
    label: 'Rosé',
    description: 'Blush pink with a rose accent.',
    palette: {
      mode: 'light',
      accent: '#be185d',
      background: '#fcf4f6',
      surface: '#ffffff',
      text: '#3f2430',
      surfaceHover: '#f8e8ed',
      border: '#f0d5de',
      borderHover: '#e2b5c4',
      textSecondary: '#7a5462',
      textTertiary: '#ad8a97',
      success: '#059669',
      danger: '#dc2626',
      warning: '#b45309',
    },
  },
  {
    id: 'dim',
    label: 'Dim',
    description: 'Soft charcoal: a dark theme with less glare than Dark.',
    palette: {
      mode: 'dark',
      accent: '#4184e4',
      background: '#22272e',
      surface: '#2d333b',
      text: '#adbac7',
      surfaceHover: '#373e47',
      border: '#444c56',
      borderHover: '#545d68',
      textSecondary: '#909dab',
      textTertiary: '#768390',
      success: '#57ab5a',
      danger: '#e5534b',
      warning: '#c69026',
    },
  },
  {
    id: 'nord',
    label: 'Nord',
    description: 'Cool arctic blue-grey with a frost accent.',
    palette: {
      mode: 'dark',
      accent: '#88c0d0',
      background: '#2e3440',
      surface: '#3b4252',
      text: '#eceff4',
      surfaceHover: '#434c5e',
      border: '#4c566a',
      borderHover: '#5e6a80',
      textSecondary: '#d8dee9',
      textTertiary: '#a3acbd',
      success: '#a3be8c',
      danger: '#e06c75',
      warning: '#ebcb8b',
    },
  },
  {
    id: 'midnight',
    label: 'Midnight',
    description: 'True black, for OLED screens and dark rooms.',
    palette: {
      mode: 'dark',
      accent: '#6366f1',
      background: '#000000',
      surface: '#0e0e10',
      text: '#e4e4e7',
      surfaceHover: '#1b1b1f',
      border: '#27272a',
      borderHover: '#3f3f46',
      textSecondary: '#a1a1aa',
      textTertiary: '#71717a',
      success: '#22c55e',
      danger: '#f87171',
      warning: '#facc15',
    },
  },
  {
    id: 'high_contrast',
    label: 'High contrast',
    description: 'Black on white with strong borders, for the most legible text.',
    palette: {
      mode: 'light',
      accent: '#0040c1',
      background: '#ffffff',
      surface: '#ffffff',
      text: '#000000',
      surfaceHover: '#ebebeb',
      border: '#767676',
      borderHover: '#000000',
      textSecondary: '#262626',
      textTertiary: '#4d4d4d',
      success: '#006e2e',
      danger: '#b3001b',
      warning: '#7a4a00',
      info: '#333333',
      vars: {
        // Element Plus draws table, card and divider lines in its lighter
        // border shades, which white-on-white would make vanish.
        '--el-border-color-light': '#767676',
        '--el-border-color-lighter': '#949494',
        '--el-border-color-extra-light': '#b3b3b3',
      },
    },
  },
];

/** The font presets, in the order the Appearance page shows them. A stack is a
 *  list of look-alikes: the first one the device has wins. */
export const FONT_PRESETS: FontPreset[] = [
  {
    id: 'default',
    label: 'Inter',
    description: 'The Cremind default.',
    stack: "'Inter', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif",
  },
  {
    id: 'system',
    label: 'System',
    description: "Your device's own interface font.",
    stack: "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'Noto Sans', sans-serif",
  },
  {
    id: 'humanist',
    label: 'Humanist',
    description: 'A warm, open sans-serif.',
    stack: "Seravek, 'Gill Sans Nova', Ubuntu, Calibri, 'DejaVu Sans', source-sans-pro, sans-serif",
  },
  {
    id: 'rounded',
    label: 'Rounded',
    description: 'Soft, rounded letters.',
    stack: "ui-rounded, 'SF Pro Rounded', 'Hiragino Maru Gothic ProN', Quicksand, Comfortaa, Manjari, 'Arial Rounded MT', 'Arial Rounded MT Bold', Calibri, source-sans-pro, sans-serif",
  },
  {
    id: 'serif',
    label: 'Serif',
    description: 'A bookish face for long reading.',
    stack: "Charter, 'Bitstream Charter', 'Sitka Text', Cambria, Georgia, serif",
  },
  {
    id: 'legible',
    label: 'Legible',
    description: 'Wide, distinct letters for easier reading.',
    stack: "'Atkinson Hyperlegible Next', 'Atkinson Hyperlegible', Verdana, 'Segoe UI', Tahoma, sans-serif",
  },
  {
    id: 'mono',
    label: 'Monospace',
    description: 'Every letter the same width.',
    stack: "ui-monospace, 'Cascadia Code', 'SF Mono', 'Source Code Pro', Menlo, Consolas, 'DejaVu Sans Mono', monospace",
  },
];

/** What a custom font list falls back to, so a font the device lacks lands on
 *  the system font rather than the browser's default serif. */
const CUSTOM_FONT_FALLBACK = 'system-ui, sans-serif';

export const THEME_IDS = [...THEME_PRESETS.map(p => p.id), 'system', 'custom'] as const;
export const FONT_IDS = [...FONT_PRESETS.map(p => p.id), 'custom'] as const;
export type ThinkingProcessMode = 'collapsed' | 'live';

export const FONT_SIZE_MIN = 80;
export const FONT_SIZE_MAX = 140;
export const FONT_SIZE_STEP = 5;

export interface AppearanceSettings {
  theme: string;
  font: string;
  /** Percent of the normal text size. */
  fontSize: number;
  customFont: string;
  customAccent: string;
  customBackground: string;
  customSurface: string;
  customText: string;
  /** Not appearance: whether a reply's Thinking Process opens by itself
   *  (`chat.thinking_process`, set with the Auto-open switch on the row). It
   *  is read, cached and saved with the rest, so it lives here too. */
  thinkingProcess: ThinkingProcessMode;
}

export const DEFAULT_APPEARANCE: AppearanceSettings = {
  theme: 'light',
  font: 'default',
  fontSize: 100,
  customFont: 'system-ui',
  customAccent: '#2563eb',
  customBackground: '#f8fafc',
  customSurface: '#ffffff',
  customText: '#334155',
  thinkingProcess: 'collapsed',
};

/** Each setting's per-profile config key. */
export const APPEARANCE_KEYS: Record<keyof AppearanceSettings, string> = {
  theme: 'appearance.theme',
  font: 'appearance.font',
  fontSize: 'appearance.font_size',
  customFont: 'appearance.custom_font',
  customAccent: 'appearance.custom_accent',
  customBackground: 'appearance.custom_background',
  customSurface: 'appearance.custom_surface',
  customText: 'appearance.custom_text',
  thinkingProcess: 'chat.thinking_process',
};

// The server's check for appearance.custom_font, so the page can refuse what
// the server would.
const FONT_FAMILY = /^[A-Za-z0-9 ,'"._-]{1,200}$/;

export function isFontFamily(value: unknown): value is string {
  return typeof value === 'string' && FONT_FAMILY.test(value) && value.replace(/[ ,'"]/g, '') !== '';
}

/** A setting's value if it is one this build can paint, else `undefined`. */
function valid<K extends keyof AppearanceSettings>(key: K, value: unknown): AppearanceSettings[K] | undefined {
  switch (key) {
    case 'theme':
      return (typeof value === 'string' && (THEME_IDS as readonly string[]).includes(value) ? value : undefined) as AppearanceSettings[K] | undefined;
    case 'font':
      return (typeof value === 'string' && (FONT_IDS as readonly string[]).includes(value) ? value : undefined) as AppearanceSettings[K] | undefined;
    case 'fontSize': {
      const n = typeof value === 'string' ? Number(value) : value;
      if (typeof n !== 'number' || !Number.isFinite(n)) return undefined;
      return Math.min(FONT_SIZE_MAX, Math.max(FONT_SIZE_MIN, Math.round(n))) as AppearanceSettings[K];
    }
    case 'customFont':
      return (isFontFamily(value) ? value : undefined) as AppearanceSettings[K] | undefined;
    case 'thinkingProcess':
      return (value === 'collapsed' || value === 'live' ? value : undefined) as AppearanceSettings[K] | undefined;
    default:
      return (isHexColor(value) ? value.toLowerCase() : undefined) as AppearanceSettings[K] | undefined;
  }
}

/** Settings from anything stored (a cache, an old build's leftovers): what
 *  this build cannot paint falls back to the default, one setting at a time. */
export function normalizeAppearance(raw: unknown): AppearanceSettings {
  const source = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {};
  const out = { ...DEFAULT_APPEARANCE };
  for (const key of Object.keys(DEFAULT_APPEARANCE) as (keyof AppearanceSettings)[]) {
    const value = valid(key, source[key]);
    if (value !== undefined) (out as Record<string, unknown>)[key] = value;
  }
  return out;
}

/**
 * Settings from GET /api/config/user: each key's override, else the server's
 * default for it (`values` holds `null` for a key never set).
 */
export function appearanceFromConfig(
  values: Record<string, unknown>,
  defaults: Record<string, unknown> = {},
): AppearanceSettings {
  const raw: Record<string, unknown> = {};
  for (const [field, key] of Object.entries(APPEARANCE_KEYS)) {
    const value = values[key];
    raw[field] = value === null || value === undefined ? defaults[key] : value;
  }
  return normalizeAppearance(raw);
}

/** The PUT /api/config/user body for some settings. */
export function appearanceToConfig(patch: Partial<AppearanceSettings>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [field, value] of Object.entries(patch)) {
    const key = APPEARANCE_KEYS[field as keyof AppearanceSettings];
    if (key && value !== undefined) out[key] = value;
  }
  return out;
}

export function themePreset(id: string): ThemePreset | undefined {
  return THEME_PRESETS.find(p => p.id === id);
}

/** The palette of the custom theme: light or dark is whichever its background
 *  is next to its text. */
export function customPalette(s: Pick<AppearanceSettings, 'customAccent' | 'customBackground' | 'customSurface' | 'customText'>): Palette {
  return {
    mode: luminance(s.customBackground) < luminance(s.customText) ? 'dark' : 'light',
    accent: s.customAccent,
    background: s.customBackground,
    surface: s.customSurface,
    text: s.customText,
  };
}

export interface ThemeChoice {
  id: string;
  label: string;
  description: string;
  palette: Palette;
  /** Match system's second half: the dark look beside the light one. */
  second?: Palette;
}

/** Every theme a picker offers, in order; the custom one shows `s`'s colors. */
export function themeChoices(s: AppearanceSettings): ThemeChoice[] {
  const [light, dark, ...rest] = THEME_PRESETS;
  return [
    light,
    dark,
    {
      id: 'system',
      label: 'Match system',
      description: 'Light or Dark, following your device, and switching when it does.',
      palette: light.palette,
      second: dark.palette,
    },
    ...rest,
    {
      id: 'custom',
      label: 'Custom',
      description: 'Your own accent, background, panel and text colors.',
      palette: customPalette(s),
    },
  ];
}

/** The palette the settings paint, with "system" decided by `systemDark`. */
export function resolvePalette(s: AppearanceSettings, systemDark: boolean): Palette {
  if (s.theme === 'custom') return customPalette(s);
  const id = s.theme === 'system' ? (systemDark ? 'dark' : 'light') : s.theme;
  return (themePreset(id) ?? THEME_PRESETS[0]).palette;
}

/** The CSS font-family the settings ask for. */
export function fontStack(s: Pick<AppearanceSettings, 'font' | 'customFont'>): string {
  if (s.font === 'custom') {
    return isFontFamily(s.customFont) ? `${s.customFont}, ${CUSTOM_FONT_FALLBACK}` : FONT_PRESETS[0].stack;
  }
  return (FONT_PRESETS.find(f => f.id === s.font) ?? FONT_PRESETS[0]).stack;
}
