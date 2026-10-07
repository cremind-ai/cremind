/**
 * Puts an appearance on the document, and keeps the copy of it that
 * index.html's first script paints before anything else loads.
 *
 * The copy is per profile (`appearance_<profile>`), plus the last one shown in
 * this browser (`appearance_last`) for the pages before sign-in. Both hold
 * the computed variables, not just the settings, so that first script needs
 * no theme logic of its own; with "system" both modes are kept and it picks.
 */

import { computeAppearance, type AppliedAppearance } from './tokens';
import { normalizeAppearance, type AppearanceSettings } from './presets';

const CACHE_VERSION = 1;
export const LAST_KEY = 'appearance_last';
export const profileKey = (profile: string) => `appearance_${profile}`;

export interface CachedAppearance {
  v: number;
  settings: AppearanceSettings;
  applied: AppliedAppearance;
  /** With the "system" theme: the look for each device mode. */
  system?: { light: AppliedAppearance; dark: AppliedAppearance };
}

function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

/** The variables already on <html>: index.html's script may have set some
 *  that the first real apply no longer sets, and those must go. */
let appliedKeys: string[] | null = null;

function currentKeys(root: HTMLElement): string[] {
  const keys: string[] = [];
  for (let i = 0; i < root.style.length; i++) {
    const name = root.style.item(i);
    if (name.startsWith('--')) keys.push(name);
  }
  return keys;
}

export function applyAppearance(a: AppliedAppearance, root: HTMLElement = document.documentElement): void {
  root.setAttribute('data-theme', a.mode);
  root.setAttribute('data-on-primary', a.onPrimary);
  for (const key of appliedKeys ?? currentKeys(root)) {
    if (!(key in a.vars)) root.style.removeProperty(key);
  }
  for (const [key, value] of Object.entries(a.vars)) root.style.setProperty(key, value);
  appliedKeys = Object.keys(a.vars);
}

export function readCachedAppearance(key: string): CachedAppearance | null {
  try {
    const raw = storage()?.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedAppearance;
    if (parsed?.v !== CACHE_VERSION || !parsed.settings) return null;
    return { ...parsed, settings: normalizeAppearance(parsed.settings) };
  } catch {
    return null;
  }
}

/** Remember what `profile` (or, without one, this browser) is showing. */
export function cacheAppearance(
  profile: string,
  settings: AppearanceSettings,
  applied: AppliedAppearance,
): void {
  const entry: CachedAppearance = { v: CACHE_VERSION, settings, applied };
  if (settings.theme === 'system') {
    entry.system = {
      light: computeAppearance(settings, false),
      dark: computeAppearance(settings, true),
    };
  }
  const json = JSON.stringify(entry);
  const store = storage();
  if (!store) return;
  try {
    if (profile) store.setItem(profileKey(profile), json);
    store.setItem(LAST_KEY, json);
    // An older build reads only this: keep it naming the mode on screen, so
    // going back to one keeps light or dark.
    store.setItem('theme', applied.mode);
  } catch {
    // Storage full or blocked: the next load paints from the stylesheet, then
    // the server's values. Nothing to do here.
  }
}
