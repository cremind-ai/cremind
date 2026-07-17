/**
 * Validate a post-login return path captured in the ``?redirect=`` query param.
 *
 * Guards against open-redirect and cross-profile jumps: the value must be an
 * internal absolute path (a single leading ``/``, never protocol-relative
 * ``//``) whose first path segment is exactly ``profile``. That rule means
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
