/**
 * Validate a post-login return path captured in the ``?redirect=`` query param.
 *
 * Guards against open-redirect and cross-profile jumps: the value must be an
 * internal absolute path (a single leading ``/``, never protocol-relative
 * ``//``) that is either the profile-neutral root or whose first path segment
 * is exactly ``profile``. That rule means
 *   - re-logging-in as a *different* profile never lands on someone else's page
 *     (falls back to that profile's default destination instead), and
 *   - we never bounce back into an auth screen (``/login/...`` / ``/setup...``).
 *
 * ``router.currentRoute.value.fullPath`` (the captured value) is URL-encoded
 * while the route ``profile`` param is decoded, so the first segment is decoded
 * before comparison.
 *
 * Returns the original path when safe, or ``null`` to signal "use the default
 * destination".
 */
export function safeRedirectTarget(raw: unknown, profile: string): string | null {
  if (typeof raw !== 'string' || !raw || !profile) return null;
  // Internal absolute path only — reject protocol-relative ``//host`` values.
  if (!raw.startsWith('/') || raw.startsWith('//')) return null;

  const path = raw.split('?')[0].split('#')[0];
  if (path === '/') return raw;
  let seg = path.split('/')[1] || '';
  try {
    seg = decodeURIComponent(seg);
  } catch {
    // Malformed escape sequence — treat as a mismatch below.
  }
  if (seg === 'login' || seg === 'setup') return null;
  if (seg !== profile) return null;

  return raw;
}

/** Profile-less pages the router sends to the current profile's copy. */
const PROFILE_NEUTRAL_PATHS = new Set(['/settings/tags']);

/**
 * Where a profile-less link (``/settings/tags``) goes: that page of the
 * profile this tab uses, else of the first signed-in profile, else the
 * profile selector with ``?redirect=`` (see profileNeutralTarget).
 */
export function currentProfilePath(
  path: string,
  current: { profileId: string; loggedIn: string[] },
): { path: string; query?: Record<string, string> } {
  const profile = current.profileId || current.loggedIn[0] || '';
  if (!profile) return { path: '/', query: { redirect: path } };
  return { path: `/${encodeURIComponent(profile)}${path}` };
}

/**
 * A profile-less ``?redirect=`` (``/settings/tags``, opened while nobody was
 * signed in) as that page of the profile just chosen; ``null`` for anything
 * else. Only exact known paths map, so this opens no new redirect.
 */
export function profileNeutralTarget(raw: unknown, profile: string): string | null {
  if (typeof raw !== 'string' || !profile || !PROFILE_NEUTRAL_PATHS.has(raw)) return null;
  return `/${encodeURIComponent(profile)}${raw}`;
}
