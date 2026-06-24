// Formatting helpers shared by the Usage & Cost dashboard and the in-chat
// usage chip/panel. Kept tiny and dependency-free.

/** Token counts with thousands separators: 12345 → "12,345". */
export function formatTokens(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString();
}

/** Compact token counts for tight spaces: 12345 → "12.3K", 2_400_000 → "2.4M". */
export function formatTokensCompact(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return String(v);
}

/**
 * USD with precision that adapts to magnitude, so sub-cent estimates stay
 * legible: <$0.01 → 4dp ($0.0034), <$1 → 3dp ($0.123), else 2dp ($12.34).
 */
export function formatUsd(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v === 0) return '$0.00';
  if (v < 0.01) return `$${v.toFixed(4)}`;
  if (v < 1) return `$${v.toFixed(3)}`;
  return `$${v.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** Fraction (0..1) → integer percent: 0.382 → "38%". */
export function formatPercent(frac: number | null | undefined): string {
  return `${Math.round((frac ?? 0) * 100)}%`;
}

/** Epoch-ms → locale date+time, blank on falsy. */
export function formatTimestamp(ms: number | null | undefined): string {
  if (!ms) return '';
  try {
    return new Date(ms).toLocaleString();
  } catch {
    return '';
  }
}
