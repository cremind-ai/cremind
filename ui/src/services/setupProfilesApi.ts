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

export interface SetupProfileServerConfig {
  db_provider?: 'sqlite' | 'postgres';
  sqlite_db_path?: string;
  service_name?: string;
  agent_name?: string;
  working_dir?: string;
  user_working_dir?: string;
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
}

export async function fetchSetupProfiles(agentUrl: string): Promise<SetupProfilesResponse> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/setup-profiles`);
  if (!res.ok) throw new Error(`Failed to fetch setup profiles: ${res.statusText}`);
  return res.json();
}
