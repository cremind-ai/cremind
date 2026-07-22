// Human-friendly relative timestamps for the conversation list and the
// notification list. ``now`` is passed in (rather than read from Date.now()
// internally) so a caller can drive a whole list off a single ticking ref and
// keep every row's timestamp consistent within one paint.

function formatShortDate(ts: number, now: number): string {
  const d = new Date(ts);
  const nd = new Date(now);
  const opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' };
  // Only spell out the year when it's not the current one — keeps the common
  // case short ("Jul 3") and disambiguates older conversations ("Jul 3, 2024").
  if (d.getFullYear() !== nd.getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString(undefined, opts);
}

/**
 * "just now" → "5m ago" → "3h ago" → "2d ago" (up to a week), then a short
 * absolute date. Clamps future timestamps (clock skew) to "just now".
 */
export function formatRelativeTime(ts: number, now: number = Date.now()): string {
  const diff = Math.max(0, now - ts);
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return 'just now';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}d ago`;
  return formatShortDate(ts, now);
}
