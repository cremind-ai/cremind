/**
 * API client for the setup wizard's environment-profile catalogue.
 *
 * Profiles bundle the per-environment defaults (Local desktop, Docker compose,
 * production server) that the wizard uses to pre-fill each step's form. The
 * canonical source is `app/config/setup_profiles.toml` on the server, and
 * the active preset id is taken from `SETUP_WIZARD_ENV` in the project
 * `.env`. The wizard only seeds defaults — every field stays editable.
 */

import type { EmbeddingSetupConfig } from './configApi';

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

// No working directory here: each profile has its own, and the server
// suggests it per profile (``SetupProfilesResponse.suggested_working_dir``).
export interface SetupProfileServerConfig {
  db_provider?: 'sqlite' | 'postgres';
  sqlite_db_path?: string;
  service_name?: string;
  agent_name?: string;
  system_dir?: string;
  postgres?: {
    host?: string;
    port?: number;
    database?: string;
    user?: string;
    password?: string;
    sslmode?: string;
  };
}

export interface SetupProfile {
  id: string;
  label: string;
  description: string;
  server_config: SetupProfileServerConfig;
  embedding_config: Partial<EmbeddingSetupConfig>;
}

export interface SetupProfilesResponse {
  profiles: SetupProfile[];
  /** Profile id from SETUP_WIZARD_ENV; null when unset, blank, or invalid. */
  selected: string | null;
  /** The working directory the profile being set up will get: the admin's
   *  on first setup (created by the server), else the ``profile`` asked
   *  about. Null when the server will not say (after setup, to a caller
   *  that may not see it) or predates per-profile folders. */
  suggested_working_dir?: string | null;
  /** True only on first setup — afterwards a profile's folder changes from
   *  Settings → Profiles (admin), never from its own setup. */
  working_dir_editable?: boolean;
}

export async function fetchSetupProfiles(
  agentUrl: string,
  /** After first setup: the profile being set up, and the admin token that
   *  may see its folder. */
  opts: { profile?: string; token?: string } = {},
): Promise<SetupProfilesResponse> {
  const base = resolveBaseUrl(agentUrl);
  const query = opts.profile ? `?profile=${encodeURIComponent(opts.profile)}` : '';
  const res = await fetch(`${base}/api/config/setup-profiles${query}`, {
    headers: opts.token ? { Authorization: `Bearer ${opts.token}` } : {},
  });
  if (!res.ok) throw new Error(`Failed to fetch setup profiles: ${res.statusText}`);
  return res.json();
}
