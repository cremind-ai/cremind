/**
 * API client for the Google Suite skills' account links.
 *
 * Mirrors calendarApi.ts / googleDriveApi.ts: each call resolves the base URL
 * from the active agent URL and attaches a Bearer token.
 *
 * Two shapes of failure, handled differently on purpose. A network error or an
 * unexpected status throws. But a *partial* unlink — the credential is gone yet
 * Google was not told, or a file survived the wipe — is returned so the caller can
 * render it as durable text: a 4-second toast is the wrong container for "your
 * grant is still live at Google".
 */

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(token: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

export interface GoogleListenerInfo {
  declared: boolean;
  autostart_rows: number;
}

export interface GoogleWatchInfo {
  active: boolean;
  /** Unix seconds, or null when no channel is registered. */
  expires_at: number | null;
}

export interface GoogleSkillRow {
  skill: string;
  label: string;
  tool_id: string;
  installed: boolean;
  enabled: boolean;
  linked: boolean;
  email: string | null;
  account_key: string | null;
  scopes: string[];
  /** Linked with the user's own OAuth client rather than the shared Cremind one. */
  own_client: boolean;
  listener: GoogleListenerInfo;
  watch: GoogleWatchInfo;
  subscriptions: { idle_after_unlink: number };
  /**
   * Other skills whose grant dies with this one. Google revokes per (app,
   * account), so a per-skill unlink declines to revoke while this is non-empty.
   */
  siblings_sharing_grant: string[];
  /** The one authoritative sentence describing what unlinking costs. */
  consequence: string;
}

export interface GoogleAccountGroup {
  email: string;
  skills: string[];
  shared_grant: boolean;
}

export interface GoogleAccountsPayload {
  ok: boolean;
  profile: string;
  revoke_url: string;
  skills: GoogleSkillRow[];
  accounts: GoogleAccountGroup[];
  calendar: {
    source: string | null;
    connected: boolean;
    app_credential_present: boolean;
  };
}

export interface GoogleUnlinkResult {
  skill: string;
  label: string;
  ok: boolean;
  unlinked: boolean;
  already: boolean;
  email: string | null;
  revoked: boolean;
  revoke_attempted: boolean;
  revoke_status: string;
  revoke_error: string | null;
  watch_stopped: boolean;
  watch_error: string | null;
  listener_stopped: boolean;
  autostart_removed: number;
  cleaned: string[];
  failed_paths: string[];
  /** True when a credential file survived the wipe — the one hard failure. */
  still_linked: boolean;
  siblings_sharing_grant: string[];
  app_credential_at_risk: boolean;
  calendar_source_after: string | null;
  subscriptions_idle: number;
  message: string;
  error?: string;
}

export interface GoogleUnlinkAllResult {
  ok: boolean;
  results: GoogleUnlinkResult[];
  unlinked: number;
  already: number;
  failed: string[];
  message: string;
  error?: string;
}

export async function getGoogleAccounts(
  agentUrl: string,
  token: string,
): Promise<GoogleAccountsPayload> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/google/accounts`, { headers: authHeaders(token) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.message || data.error || `Failed to load Google accounts: ${res.statusText}`);
  }
  return data as GoogleAccountsPayload;
}

export async function unlinkGoogleSkill(
  agentUrl: string,
  token: string,
  skill: string,
  options: { revoke?: boolean; forceRevoke?: boolean } = {},
): Promise<GoogleUnlinkResult> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/google/accounts/${encodeURIComponent(skill)}/unlink`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({
      revoke: options.revoke !== false,
      force_revoke: options.forceRevoke === true,
    }),
  });
  const data = await res.json().catch(() => ({}));
  // A 500 `wipe_failed` still carries the full result, and the user has to read
  // it — throwing would reduce it to a status line.
  if (!res.ok && !data.message) {
    throw new Error(data.error || `Failed to unlink ${skill}: ${res.statusText}`);
  }
  return data as GoogleUnlinkResult;
}

export async function unlinkAllGoogle(
  agentUrl: string,
  token: string,
  options: { revoke?: boolean } = {},
): Promise<GoogleUnlinkAllResult> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/google/unlink-all`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ revoke: options.revoke !== false }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok && !data.message) {
    throw new Error(data.error || `Failed to unlink Google accounts: ${res.statusText}`);
  }
  return data as GoogleUnlinkAllResult;
}

/** The confirm-dialog body for one skill: the real consequence, plus the warnings. */
export function unlinkConsequence(row: GoogleSkillRow, revoke = true): string {
  const parts: string[] = [row.consequence];
  if (row.listener.declared) {
    parts.push(
      `The ${row.skill} listener stops and its autostart registration is removed — ` +
        'register it again after re-linking.',
    );
  }
  if (revoke && row.siblings_sharing_grant.length) {
    parts.push(
      'Google lists Cremind as one app, so the grant is shared with ' +
        `${row.siblings_sharing_grant.join(', ')}. Cremind will not revoke it while ` +
        'those are still linked — unlink them too, or use "Unlink all".',
    );
  }
  if (row.subscriptions.idle_after_unlink > 0) {
    parts.push(
      `${row.subscriptions.idle_after_unlink} event automation(s) on this skill stop ` +
        'firing until you re-link and register its listener again.',
    );
  }
  if (!revoke) {
    parts.push('The grant stays live in your Google account.');
  }
  return parts.join('\n\n');
}
