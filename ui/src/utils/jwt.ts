/**
 * Read-only JWT claim helpers.
 *
 * The SPA never verifies a token — the server does that on every request.
 * These helpers only *read* the payload segment so a page can show what the
 * session already knows (when it expires), which is why every failure mode
 * degrades to an empty string instead of throwing: a token this code cannot
 * parse is still a perfectly usable bearer credential.
 */

/** Base64url-decode one JWT segment into its parsed JSON claims.
 *  Returns null for anything that isn't a decodable JSON object. */
function decodeClaims(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const encoded = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = encoded + '='.repeat((4 - (encoded.length % 4)) % 4);
    const bytes = Uint8Array.from(atob(padded), char => char.charCodeAt(0));
    const claims = JSON.parse(new TextDecoder().decode(bytes));
    if (!claims || typeof claims !== 'object') return null;
    return claims as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** ISO 8601 string of the token's ``exp`` claim, or '' when there is no
 *  usable expiry. The config export prints this verbatim, and the Setup
 *  Wizard gets the same string straight from the setup response — so an
 *  unparseable token has to render as "no expiry shown" rather than a
 *  misleading date. */
export function jwtExpiry(token: string): string {
  if (!token) return '';
  const claims = decodeClaims(token);
  if (!claims) return '';
  const exp = claims.exp;
  // ``exp`` is seconds since the epoch (RFC 7519). Reject anything that
  // isn't a finite number — a string or a null would silently become
  // 1970-01-01 through the Date constructor.
  if (typeof exp !== 'number' || !Number.isFinite(exp)) return '';
  try {
    return new Date(exp * 1000).toISOString();
  } catch {
    return '';
  }
}
