/**
 * The little color math the themes need: mixing two colors, and the WCAG
 * contrast ratio that decides whether text on a color should be white or dark.
 *
 * Colors are `#rrggbb` hex strings throughout (what the server accepts for the
 * custom theme); `#rgb` is read too, so a preset may be written short.
 */

export interface Rgb {
  r: number;
  g: number;
  b: number;
}

/** `#rrggbb` or `#rgb` → channels 0–255; `null` for anything else. */
export function parseHex(hex: string): Rgb | null {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  let h = m[1];
  if (h.length === 3) h = h.split('').map(c => c + c).join('');
  const n = parseInt(h, 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

export function isHexColor(value: unknown): value is string {
  return typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value);
}

function rgb(hex: string): Rgb {
  const c = parseHex(hex);
  if (!c) throw new Error(`not a hex color: ${hex}`);
  return c;
}

export function toHex({ r, g, b }: Rgb): string {
  const part = (v: number) => Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, '0');
  return `#${part(r)}${part(g)}${part(b)}`;
}

/** `a` moved `amount` (0–1) of the way to `b`. */
export function mix(a: string, b: string, amount: number): string {
  const x = rgb(a);
  const y = rgb(b);
  return toHex({
    r: x.r + (y.r - x.r) * amount,
    g: x.g + (y.g - x.g) * amount,
    b: x.b + (y.b - x.b) * amount,
  });
}

/** The color at `opacity`, as `rgba(...)`. */
export function withAlpha(hex: string, opacity: number): string {
  const { r, g, b } = rgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${opacity})`;
}

/** WCAG relative luminance, 0 (black) – 1 (white). */
export function luminance(hex: string): number {
  const { r, g, b } = rgb(hex);
  const lin = (v: number) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/** WCAG contrast ratio of two colors, 1 – 21. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * Text color for a fill: white while white still reads on it (3:1, the bar for
 * bold UI text), otherwise whichever of white and `ink` reads better.
 */
export function readableOn(fill: string, ink: string): string {
  const onWhite = contrast(fill, '#ffffff');
  if (onWhite >= 3) return '#ffffff';
  return contrast(fill, ink) > onWhite ? ink : '#ffffff';
}
