/**
 * API client for server configuration, LLM providers, tool management, and profiles.
 */

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(token: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  return headers;
}

// ── Setup ──

export async function checkSetupStatus(
  agentUrl: string,
  profile?: string
): Promise<{
  setup_complete: boolean;
  profile_exists?: boolean;
  has_profiles?: boolean;
}> {
  const base = resolveBaseUrl(agentUrl);
  const params = profile ? `?profile=${encodeURIComponent(profile)}` : '';
  const res = await fetch(`${base}/api/config/setup-status${params}`);
  if (!res.ok) throw new Error(`Failed to check setup status: ${res.statusText}`);
  return res.json();
}

// ── Per-service deployment capabilities ──
//
// Returned by GET /api/services/capabilities. Drives the Deployment-mode
// radio in each Setup Wizard step: which modes does this service support,
// and what defaults should each mode seed the form with? SQLite is
// intentionally absent — the wizard renders no radio for local-only
// services.

export type DeploymentMode = 'docker' | 'native' | 'external';

export interface ServiceCapability {
  id: string;
  display_name: string;
  category: 'database' | 'vectorstore';
  supported_modes: DeploymentMode[];
  docker_defaults: { in_network_host: string; in_network_port: number } | null;
  native_defaults: { kind: 'in_process' | 'subprocess'; data_subpath: string; port: number | null } | null;
  external_defaults: { host: string; port: number } | null;
}

export interface ServiceCapabilitiesResponse {
  services: Record<string, ServiceCapability>;
  /** False when the cremind process can't drive ``docker compose`` on
   *  this host (no socket, no compose file). The wizard masks the
   *  Docker radio for every service in that case. */
  docker_available: boolean;
  /** INSTALL_MODE from the rendered .env. The backend has already
   *  filtered each service's ``supported_modes`` by the catalog's
   *  mode rule for this mode; the UI just renders what came back. */
  install_mode?: string | null;
}

export async function fetchServiceCapabilities(
  agentUrl: string,
  // Optional because the Setup Wizard calls this endpoint pre-setup,
  // before any JWT can exist. Post-setup callers (Settings → Vector
  // Embedding) MUST pass the admin token — the backend gates this
  // route behind ``require_admin`` once ``setup_complete`` is true, and
  // an unauthenticated call there 401s silently and breaks the
  // deployment-mode radio.
  token?: string,
): Promise<ServiceCapabilitiesResponse> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/services/capabilities`, {
    headers: authHeaders(token ?? ''),
  });
  if (!res.ok) throw new Error(`Failed to fetch service capabilities: ${res.statusText}`);
  return res.json();
}

export async function resetOrphanedSetup(
  agentUrl: string
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/reset-orphaned-setup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to reset setup: ${res.statusText}`);
  }
  return res.json();
}

export interface EmbeddingSetupConfig {
  enabled: boolean;
  provider: 'me5' | 'gemma';
  hf_token: string;
  vectorstore: {
    provider: 'qdrant' | 'chroma';
    /** Stamped by ``_resolve_vectorstore`` on the backend after
     *  provisioning; also accepted on input so the wizard can request
     *  a specific mode. */
    deployment_mode: DeploymentMode;
    qdrant: {
      deployment_mode: DeploymentMode;
      host: string;
      port: number;
      api_key: string;
      https: boolean;
    };
    chroma: {
      deployment_mode: DeploymentMode;
      host: string;
      port: number;
      ssl: boolean;
      api_key: string;
      /** Only used when deployment_mode === 'native'. */
      persist_path: string;
    };
  };
}

export async function completeSetup(
  agentUrl: string,
  config: {
    profile: string;
    server_config?: Record<string, string>;
    embedding_config?: EmbeddingSetupConfig;
    llm_config?: Record<string, string>;
    tool_configs?: Record<string, Record<string, string>>;
  },
  // Empty for first-run setup (endpoint is open in setup mode); the admin
  // token for any subsequent profile, which the backend requires via
  // ``require_admin``. The target profile is taken from ``config.profile``,
  // not the token — so admin's JWT authorizes creating a differently-named
  // profile.
  token?: string,
): Promise<{ success: boolean; token: string; expires_at: string; profile: string }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/setup`, {
    method: 'POST',
    headers: authHeaders(token ?? ''),
    body: JSON.stringify(config),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Setup failed: ${res.statusText}`);
  }
  return res.json();
}

export interface InstallSecrets {
  deployment: 'docker' | 'native';
  available: boolean;
  // Docker runtime block — populated when the backend detects a docker
  // runtime (compose env vars set, compose env file present, or host
  // docker bundle on disk).
  vnc_password?: string | null;
  app_url?: string | null;
  resolution?: string | null;
  install_mode?: string | null;
  cors_allowed_origins?: string | null;
  setup_wizard_env?: string | null;
  api_port?: number | null;
  spa_port?: number | null;
  novnc_port?: number | null;
  vnc_port?: number | null;
  // Postgres block — populated when the Setup Wizard configured Postgres
  // (bootstrap.toml has db_provider="postgres").
  pg_host?: string | null;
  pg_port?: number | null;
  pg_user?: string | null;
  pg_password?: string | null;
  pg_database?: string | null;
  pg_sslmode?: string | null;
  pg_deployment_mode?: 'docker' | 'external' | null;
}

export async function fetchInstallSecrets(
  agentUrl: string,
  token: string,
): Promise<InstallSecrets> {
  try {
    const base = resolveBaseUrl(agentUrl);
    const res = await fetch(`${base}/api/config/install-secrets`, {
      headers: authHeaders(token),
    });
    if (!res.ok) {
      return { deployment: 'native', available: false };
    }
    return await res.json();
  } catch {
    return { deployment: 'native', available: false };
  }
}

export type EmbeddingStatus =
  | 'disabled'
  | 'initializing'
  | 'rebuilding'
  | 'ready'
  | 'failed';

export interface EmbeddingStatusResponse {
  enabled: boolean;
  status: EmbeddingStatus;
  ready: boolean;
  busy?: boolean;
  phase?: string | null;
  error: string | null;
}

// EmbeddingConfig is the same shape the Settings page reads/writes back.
export type EmbeddingConfig = EmbeddingSetupConfig;

export async function getEmbeddingConfig(
  agentUrl: string,
  token: string,
): Promise<{ config: EmbeddingConfig; status: EmbeddingStatus; ready: boolean; busy?: boolean; phase?: string | null; error: string | null }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/embedding`, {
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to load embedding config: ${res.statusText}`);
  }
  return res.json();
}

/** One entry in the 409 ``missing`` array returned by the embedding
 *  PUT when the user enables a provider whose pip extras aren't on
 *  disk. Plural (vs. the tool-toggle's singular) because enabling
 *  embedding can require both the embedding provider extras AND the
 *  vector-store provider extras in one shot. */
export interface EmbeddingFeatureMissing {
  feature_key: string;
  extras: string[];
  requires_restart_after_install: boolean;
}

export interface EmbeddingFeaturesNotInstalledDetail {
  missing: EmbeddingFeatureMissing[];
  message: string;
}

export class EmbeddingFeaturesNotInstalledError extends Error {
  readonly detail: EmbeddingFeaturesNotInstalledDetail;

  constructor(detail: EmbeddingFeaturesNotInstalledDetail) {
    super(detail.message);
    this.name = 'EmbeddingFeaturesNotInstalledError';
    this.detail = detail;
  }
}

export async function applyEmbeddingConfig(
  agentUrl: string,
  token: string,
  config: EmbeddingConfig,
  // ``deferApply`` persists the new config without scheduling the
  // worker-thread apply. The Settings page sets it after a feature
  // install that flagged ``requires_restart`` — torch +
  // sentence-transformers can't be hot-loaded, so attempting the apply
  // would just FAIL the embedding state. The persisted enabled flag is
  // picked up on the next boot by ``initialize_embedding_subsystem``,
  // which loads the model in a fresh process and marks state READY.
  options?: { deferApply?: boolean },
): Promise<EmbeddingStatusResponse> {
  const base = resolveBaseUrl(agentUrl);
  const body = options?.deferApply
    ? { ...config, defer_apply: true }
    : config;
  const res = await fetch(`${base}/api/config/embedding`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (res.status === 409) {
    // Backend preflight blocked the apply because one or more required
    // optional dependencies aren't installed. Surface the structured
    // payload so the Settings page can open the install dialog, drive
    // the SSE install, and retry this call on success.
    const body = await res.json().catch(() => ({}));
    if (body && body.error === 'FeatureNotInstalled' && Array.isArray(body.missing)) {
      throw new EmbeddingFeaturesNotInstalledError(body as EmbeddingFeaturesNotInstalledDetail);
    }
  }
  if (!res.ok && res.status !== 202) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to update embedding config: ${res.statusText}`);
  }
  return res.json();
}

export async function getEmbeddingStatus(agentUrl: string): Promise<EmbeddingStatusResponse> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/embedding/status`);
  if (!res.ok) throw new Error(`Failed to load embedding status: ${res.statusText}`);
  return res.json();
}

export async function startEmbeddingInitialization(
  agentUrl: string
): Promise<EmbeddingStatusResponse> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/embedding/initialize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok && res.status !== 202) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to start embedding initialization: ${res.statusText}`);
  }
  return res.json();
}

export async function reconfigure(
  agentUrl: string,
  token: string
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/reconfigure`, {
    method: 'POST',
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to reconfigure: ${res.statusText}`);
  return res.json();
}

// ── Server Config ──

export async function getServerConfig(
  agentUrl: string,
  token: string
): Promise<{ config: Record<string, string> }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/server`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to get server config: ${res.statusText}`);
  return res.json();
}

export async function updateServerConfig(
  agentUrl: string,
  token: string,
  config: Record<string, string>
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/server`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify({ config }),
  });
  if (!res.ok) throw new Error(`Failed to update server config: ${res.statusText}`);
  return res.json();
}

// ── User Config (Settings → Config) ──

export type UserConfigFieldType = 'number' | 'string' | 'boolean' | 'enum';

/** Optional semantic format hint for a `string` field (e.g. a timezone picker). */
export type UserConfigFieldFormat = 'timezone';

export interface UserConfigField {
  type: UserConfigFieldType;
  default?: unknown;
  label?: string;
  description?: string;
  min?: number;
  max?: number;
  step?: number;
  enum?: string[];
  format?: UserConfigFieldFormat;
}

export interface UserConfigGroup {
  label: string;
  description?: string;
  fields: Record<string, UserConfigField>;
}

export interface UserConfigSchema {
  groups: Record<string, UserConfigGroup>;
}

export interface UserConfigValues {
  profile: string;
  values: Record<string, unknown>;
  defaults: Record<string, unknown>;
}

export async function getUserConfigSchema(
  agentUrl: string,
  token: string
): Promise<UserConfigSchema> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/schema`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to get config schema: ${res.statusText}`);
  return res.json();
}

export async function getUserConfig(
  agentUrl: string,
  token: string
): Promise<UserConfigValues> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/user`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to get user config: ${res.statusText}`);
  return res.json();
}

export async function updateUserConfig(
  agentUrl: string,
  token: string,
  values: Record<string, unknown>
): Promise<{ success: boolean; updated: string[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/user`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify({ values }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const detail = data.details ? ` (${JSON.stringify(data.details)})` : '';
    throw new Error(`${data.error || 'Failed to update user config'}${detail}`);
  }
  return res.json();
}

export async function resetUserConfigKey(
  agentUrl: string,
  token: string,
  key: string
): Promise<{ success: boolean; deleted: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/config/user/${encodeURIComponent(key)}`,
    {
      method: 'DELETE',
      headers: authHeaders(token),
    }
  );
  if (!res.ok) throw new Error(`Failed to reset config key: ${res.statusText}`);
  return res.json();
}

// ── LLM Providers ──

export interface ProviderConfigField {
  description: string;
  type: string;
  secret: boolean;
  required: boolean;
  default?: string;
  configured: boolean;
}

export interface AuthMethodField {
  description: string;
  type: string;
  secret: boolean;
  required: boolean;
  default?: string;
  configured: boolean;
}

export interface AuthMethod {
  id: string;
  label: string;
  hint?: string;
  instructions?: string;
  kind: string; // "api_key" | "token" | "oauth" | "service_account" | "none"
  is_default?: boolean;
  fields: Record<string, AuthMethodField>;
}

export interface LLMProvider {
  name: string;
  display_name: string;
  requires_api_key: boolean;
  requires_service_account: boolean;
  configured: boolean;
  model_count: number;
  config_fields?: Record<string, ProviderConfigField>;
  current_values?: Record<string, string>;
  auth_methods?: AuthMethod[];
  active_auth_method?: string;
  // Present (true) on user-defined custom providers; `base_url` carries their
  // stored API Base URL so the editor can prefill it.
  is_custom?: boolean;
  base_url?: string;
}

// A model row as entered in the custom-provider editor. Distinct from
// `LLMModel`, which is the read-only catalog view.
//   - `supports_reasoning`: whether the model supports Reasoning Effort. When
//     true it enables the Reasoning Effort selector in the Model /
//     Low-Performance sections and tells the agent to skip its own think-tool.
//   - prices: per-1M-token USD; `null`/blank = unknown cost (not tracked).
export interface CustomProviderModel {
  id: string;
  display_name: string;
  vision: boolean;
  audio: boolean;
  supports_reasoning: boolean;
  input_price_per_1m: number | null;
  output_price_per_1m: number | null;
  cache_read_price_per_1m: number | null;
  cache_write_price_per_1m: number | null;
}

export interface CustomProviderInput {
  display_name: string;
  base_url: string;
  api_key?: string;
  models: CustomProviderModel[];
}

export interface LLMModel {
  id: string;
  display_name: string;
  group_hint: string;
  // Nullable: custom-provider models omit a price when the user leaves it blank
  // (→ unknown cost). Built-in catalog models always carry a number.
  input_price_per_1m: number | null;
  output_price_per_1m: number | null;
  cache_read_price_per_1m?: number | null;
  cache_write_price_per_1m?: number | null;
  reasoning_effort?: string[];
  vision?: boolean;
  audio?: boolean;
}

export async function listLLMProviders(
  agentUrl: string,
  token: string
): Promise<{ providers: LLMProvider[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/providers`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to list providers: ${res.statusText}`);
  return res.json();
}

export async function getProviderModels(
  agentUrl: string,
  token: string,
  providerName: string
): Promise<{ provider: Record<string, unknown>; models: LLMModel[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/providers/${encodeURIComponent(providerName)}/models`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to get models: ${res.statusText}`);
  return res.json();
}

export async function updateProvider(
  agentUrl: string,
  token: string,
  providerName: string,
  config: Record<string, unknown>
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/providers/${encodeURIComponent(providerName)}`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify(config),
  });
  if (!res.ok) throw new Error(`Failed to update provider: ${res.statusText}`);
  return res.json();
}

export async function deleteProviderConfig(
  agentUrl: string,
  token: string,
  providerName: string
): Promise<{ success: boolean; deleted_keys: number }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/providers/${encodeURIComponent(providerName)}/config`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to remove provider config: ${res.statusText}`);
  return res.json();
}

export async function createCustomProvider(
  agentUrl: string,
  token: string,
  body: CustomProviderInput
): Promise<{ success: boolean; name: string }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/providers/custom`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      if (data?.error) detail = data.error;
    } catch { /* keep statusText */ }
    throw new Error(detail);
  }
  return res.json();
}

// ── Device Code Flow (GitHub Copilot) ──

export interface DeviceCodeResponse {
  verification_uri: string;
  user_code: string;
  device_code: string;
  expires_in: number;
  interval: number;
}

export async function startDeviceCodeFlow(
  agentUrl: string,
  token: string
): Promise<DeviceCodeResponse> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/auth/device-code/start`, {
    method: 'POST',
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to start device code flow: ${res.statusText}`);
  return res.json();
}

export async function pollDeviceCode(
  agentUrl: string,
  token: string,
  deviceCode: string
): Promise<{ status: 'pending' | 'complete' | 'expired' | 'error'; slow_down?: boolean; error?: string; access_token?: string }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/auth/device-code/poll`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ device_code: deviceCode }),
  });
  if (!res.ok) throw new Error(`Failed to poll device code: ${res.statusText}`);
  return res.json();
}

// ── Codex OAuth Flow ("Sign in with ChatGPT" for the OpenAI provider) ──

export interface CodexOAuthStart {
  authorize_url: string;
  state: string;
  redirect_uri: string;
  listener_active: boolean;
  listener_error?: string | null;
  expires_in: number;
}

export interface CodexOAuthStatus {
  status: 'pending' | 'complete' | 'error' | 'expired';
  email?: string | null;
  plan_type?: string | null;
  account_id?: string | null;
  error?: string;
}

export async function startCodexOAuth(
  agentUrl: string,
  token: string
): Promise<CodexOAuthStart> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/auth/codex/start`, {
    method: 'POST',
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to start ChatGPT sign-in: ${res.statusText}`);
  return res.json();
}

export async function getCodexOAuthStatus(
  agentUrl: string,
  token: string,
  state: string
): Promise<CodexOAuthStatus> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/auth/codex/status?state=${encodeURIComponent(state)}`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to check sign-in status: ${res.statusText}`);
  return res.json();
}

export async function completeCodexOAuth(
  agentUrl: string,
  token: string,
  redirectUrl: string,
  state?: string
): Promise<CodexOAuthStatus> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/auth/codex/complete`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ redirect_url: redirectUrl, state }),
  });
  if (!res.ok) throw new Error(`Failed to complete sign-in: ${res.statusText}`);
  return res.json();
}

export async function cancelCodexOAuth(
  agentUrl: string,
  token: string,
  state: string
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  await fetch(`${base}/api/llm/auth/codex/cancel`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ state }),
  }).catch(() => { /* best-effort cleanup */ });
}

export async function getModelGroups(
  agentUrl: string,
  token: string
): Promise<{ model_groups: Record<string, string>; default_provider: string; reasoning_efforts: Record<string, string | null>; vision_enabled: boolean; audio_enabled: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/llm/model-groups`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to get model groups: ${res.statusText}`);
  return res.json();
}

export async function updateModelGroups(
  agentUrl: string,
  token: string,
  modelGroups: Record<string, string>,
  defaultProvider?: string,
  reasoningEfforts?: Record<string, string | null>,
  visionEnabled?: boolean,
  audioEnabled?: boolean,
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const body: Record<string, unknown> = { model_groups: modelGroups };
  if (defaultProvider) body.default_provider = defaultProvider;
  if (reasoningEfforts) body.reasoning_efforts = reasoningEfforts;
  if (visionEnabled !== undefined) body.vision_enabled = visionEnabled;
  if (audioEnabled !== undefined) body.audio_enabled = audioEnabled;
  const res = await fetch(`${base}/api/llm/model-groups`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to update model groups: ${res.statusText}`);
  return res.json();
}

// ── Tools ──

export interface ToolConfigField {
  description: string;
  type: string;
  secret: boolean;
  configured: boolean;
  /** Whether the variable must be set. Skills declare this per-variable; when
   *  false (or with a default) the field is editable but never blocks config. */
  required?: boolean;
  enum?: string[];
  default?: unknown;
  /** When true, a live option list is available from
   *  ``getToolVariableOptions`` (GET /api/tools/{id}/variable-options). Advisory:
   *  values are not validated against it, so the field stays free-form. */
  dynamic_options?: boolean;
}

export interface ToolStatus {
  /** Unique system identifier (slug). */
  tool_id: string;
  /** Human-readable display name shown in the UI. */
  name: string;
  /** Description shown in tool listings. */
  description: string;
  /** One of: 'builtin' | 'a2a' | 'mcp' | 'skill'.
   *  (Intrinsic tools are filtered out server-side.) */
  tool_type: 'builtin' | 'a2a' | 'mcp' | 'skill' | string;
  enabled: boolean;
  /** Profile-independent default enabled state (ignores this profile's
   *  overrides). Used by the Setup Wizard to show pristine defaults for a
   *  brand-new profile instead of the admin profile's customized toggles. */
  default_enabled?: boolean;
  configured: boolean;
  /** TOML required_config schema for built-in tools. {} for skills/a2a/mcp. */
  required_fields: Record<string, ToolConfigField>;
  /** Per-profile per-tool config grouped by scope. */
  config: {
    arguments: Record<string, unknown>;
    variables: Record<string, string>;
    meta?: Record<string, string>;
  };
  arguments_schema: Record<string, unknown> | null;
  /** Optional fields surfaced by the registry: */
  url?: string;
  owner_profile?: string | null;
  is_stub?: boolean;
  connection_error?: string | null;
  /** Built-in tools only: false until a child LLM is bound (post-setup). */
  llm_bound?: boolean;
  /** Skills only: declared in SKILL.md frontmatter as
   *  ``metadata.long_running_app`` -- a helper process the skill wants
   *  registered for autostart. Absent when the skill doesn't declare one. */
  long_running_app?: { command: string; description?: string };
  /** Built-in tools only: optional pip-extras group required for this
   *  tool to run (e.g. ``"browser"`` for the Browser tool). When set,
   *  the Setup Wizard auto-installs it on submit, and the post-setup
   *  enable handler pre-flights it (HTTP 409 + install dialog). */
  requires_feature?: string | null;
  /** Skills only: true when the skill's on-disk directory is a shipped
   *  built-in (under ``app/skills/builtin``). Drives the Settings page
   *  "Reset to Default" (built-in) vs "Delete" (imported) action. */
  is_builtin?: boolean;
  /** Built-in tools only: when true, the tool is visible but its enable/disable
   *  toggle is locked ON (the API rejects disable). The UI renders the switch
   *  disabled with a lock icon. Sourced from TOOL_CONFIG.locked. */
  toggle_locked?: boolean;
  /** Built-in groups / MCP servers only: true when the tool exposes more than
   *  one sub-tool ("leaf"), so the Settings card shows a per-sub-tool toggle
   *  section. The UI lazy-loads the leaf list (``listToolLeaves``) only when set. */
  supports_leaf_toggle?: boolean;
}

export async function listTools(
  agentUrl: string,
  token: string
): Promise<{ tools: ToolStatus[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/tools`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to list tools: ${res.statusText}`);
  return res.json();
}

/** Fetch full config + schema for a tool (used by the per-tool view). */
export async function getToolConfig(
  agentUrl: string,
  token: string,
  toolId: string
): Promise<ToolStatus> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/tools/${encodeURIComponent(toolId)}`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to get tool config: ${res.statusText}`);
  return res.json();
}

/** Update Tool Variables (env-style secrets / required_config values). */
export async function updateToolConfig(
  agentUrl: string,
  token: string,
  toolId: string,
  variables: Record<string, string>
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/tools/${encodeURIComponent(toolId)}/variables`, {
    method: 'PUT',
    headers: authHeaders(token),
    // The Model field is a free-form combobox (the dropdown lists valid ids but
    // typing a custom one is intentional), so opt out of the server-side
    // dynamic-option check that guards the agent/CLI path.
    body: JSON.stringify({ variables, allow_unknown: true }),
  });
  if (!res.ok) throw new Error(`Failed to update tool variables: ${res.statusText}`);
  return res.json();
}

/** Toggle a tool's enabled state for the current profile. */
/** Backend payload for a 409 returned from `PUT /api/tools/{id}/enabled`
 *  when the tool's optional feature is not yet installed. The frontend
 *  drives ``streamFeaturesInstall`` and retries the toggle after the
 *  install reaches ``event: done`` with ``ok=true``.
 */
export interface FeatureNotInstalledDetail {
  tool_id: string;
  feature_key: string;
  extras: string[];
  requires_restart_after_install: boolean;
  message: string;
}

export class FeatureNotInstalledError extends Error {
  readonly detail: FeatureNotInstalledDetail;

  constructor(detail: FeatureNotInstalledDetail) {
    super(detail.message);
    this.name = 'FeatureNotInstalledError';
    this.detail = detail;
  }
}

export async function setToolEnabled(
  agentUrl: string,
  token: string,
  toolId: string,
  enabled: boolean
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/tools/${encodeURIComponent(toolId)}/enabled`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify({ enabled }),
  });
  if (res.status === 409) {
    // The backend pre-flight detected that the tool's optional feature
    // is not installed yet. Surface the structured payload so callers
    // can drive the install dialog and retry the toggle.
    const body = await res.json().catch(() => ({}));
    if (body && body.error === 'FeatureNotInstalled') {
      throw new FeatureNotInstalledError(body as FeatureNotInstalledDetail);
    }
  }
  if (!res.ok) throw new Error(`Failed to set tool enabled: ${res.statusText}`);
  return res.json();
}

// ── Per-sub-tool ("leaf") enable/disable ──

/** One sub-tool of a built-in group or MCP server. */
export interface ToolLeaf {
  /** Original sub-tool name within the group (not the namespaced function name). */
  leaf_name: string;
  /** Display name. */
  name: string;
  /** Short description shown next to the toggle. */
  description: string;
  /** Whether this sub-tool is currently enabled for the profile. */
  enabled: boolean;
}

export interface ToolLeavesResponse {
  /** False for single-leaf tools (toggling is redundant with the tool switch). */
  supports_leaf_toggle: boolean;
  /** True when an MCP server is disconnected and its sub-tools can't be listed. */
  disconnected: boolean;
  leaves: ToolLeaf[];
}

/** One live option for a `dynamic_options` tool variable. */
export interface VariableOption {
  id: string;
  label?: string;
}

/** Live option list for one `dynamic_options` variable. */
export interface VariableOptionsResult {
  options: VariableOption[];
  /** Non-fatal per-variable error (e.g. no credential / API rejected the key).
   *  When set with empty options, the UI falls back to a text input. */
  error?: string | null;
  /** Non-secret label of the credential source the list was fetched with. */
  source?: string | null;
}

/** Fetch live option lists for a tool's `dynamic_options` variables. Tools
 *  without a dynamic-options hook return an empty `variables` map. */
export async function getToolVariableOptions(
  agentUrl: string,
  token: string,
  toolId: string,
  refresh = false
): Promise<{ tool_id: string; variables: Record<string, VariableOptionsResult> }> {
  const base = resolveBaseUrl(agentUrl);
  const suffix = refresh ? '?refresh=1' : '';
  const res = await fetch(
    `${base}/api/tools/${encodeURIComponent(toolId)}/variable-options${suffix}`,
    { headers: authHeaders(token) }
  );
  if (!res.ok) throw new Error(`Failed to get variable options: ${res.statusText}`);
  return res.json();
}

/** List a tool's sub-tools with their per-profile enabled state. */
export async function listToolLeaves(
  agentUrl: string,
  token: string,
  toolId: string
): Promise<ToolLeavesResponse> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/tools/${encodeURIComponent(toolId)}/leaves`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to list sub-tools: ${res.statusText}`);
  return res.json();
}

/** Enable/disable one or more sub-tools. Pass a single key for a per-row
 *  toggle, or many for "Enable all" / "Disable all". */
export async function setToolLeaves(
  agentUrl: string,
  token: string,
  toolId: string,
  leaves: Record<string, boolean>
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/tools/${encodeURIComponent(toolId)}/leaves`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify({ leaves }),
  });
  if (!res.ok) throw new Error(`Failed to update sub-tools: ${res.statusText}`);
  return res.json();
}

// ── Skill lifecycle (delete / reset / import) ──

/** Pull a JSON ``error`` message off a non-OK response, falling back to status. */
async function readError(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => null);
  if (body && typeof body.error === 'string' && body.error) return body.error;
  return `${fallback}: ${res.statusText}`;
}

/** Result shape shared by skill import endpoints. */
export interface SkillImportResult {
  success: boolean;
  installed: string[];
  skipped?: { name: string; reason: string }[];
}

/** Delete an external skill, or reset a built-in skill to its shipped default.
 *  The backend decides which based on whether the skill is a built-in; the
 *  returned ``reset`` flag echoes that decision. */
export async function deleteSkill(
  agentUrl: string,
  token: string,
  toolId: string
): Promise<{ success: boolean; reset: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/skills/${encodeURIComponent(toolId)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(await readError(res, 'Failed to delete skill'));
  return res.json();
}

/** Import skills from an uploaded archive (.zip/.tar.gz/...). */
export async function importSkillArchive(
  agentUrl: string,
  token: string,
  file: File
): Promise<SkillImportResult> {
  const base = resolveBaseUrl(agentUrl);
  const formData = new FormData();
  formData.append('file', file);
  // Note: no Content-Type header — the browser sets the multipart boundary.
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${base}/api/skills/import/archive`, {
    method: 'POST',
    headers,
    body: formData,
  });
  if (!res.ok) throw new Error(await readError(res, 'Failed to import skill'));
  return res.json();
}

/** Import skills from a public GitHub repository URL. */
export async function importSkillFromGitHub(
  agentUrl: string,
  token: string,
  url: string
): Promise<SkillImportResult> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/skills/import/github`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ url }),
  });
  if (!res.ok) throw new Error(await readError(res, 'Failed to import skill'));
  return res.json();
}

/** Import skills from a Cremind Hub link (skill page URL or bare skill name). */
export async function importSkillFromHub(
  agentUrl: string,
  token: string,
  link: string
): Promise<SkillImportResult> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/skills/import/hub`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ link }),
  });
  if (!res.ok) throw new Error(await readError(res, 'Failed to import skill'));
  return res.json();
}

/** One SSE frame emitted by ``POST /api/features/install``. */
export interface FeatureInstallEvent {
  event: 'start' | 'log' | 'post_install' | 'done' | 'error';
  message?: string;
  ok: boolean;
  meta?: Record<string, unknown>;
  // Present on the final ``event: done`` frame.
  restart_required?: boolean;
  installed?: string[];
  failed?: string[];
  already_present?: string[];
  error?: string | null;
}

export interface StreamFeaturesInstallResult {
  ok: boolean;
  restart_required: boolean;
  installed: string[];
  failed: string[];
  error: string | null;
}

/** POST ``/api/features/install`` and pump SSE frames into ``onEvent``.
 *
 *  Mirrors the consumer pattern used by ``embeddingStateStream.ts`` but
 *  short-lived: one install -> one stream -> resolve. Rejects if the
 *  HTTP request itself fails; resolves with the final ``StreamFeaturesInstallResult``
 *  on ``event: done`` regardless of success/failure (callers branch on
 *  ``result.ok`` and ``result.failed``).
 */
export async function streamFeaturesInstall(
  agentUrl: string,
  token: string,
  features: string[],
  onEvent: (event: FeatureInstallEvent) => void
): Promise<StreamFeaturesInstallResult> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/features/install`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ features }),
  });
  if (!res.ok || !res.body) {
    throw new Error(`Failed to start feature install: ${res.statusText}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let lastDone: FeatureInstallEvent | null = null;

  // Standard SSE framing: frames are separated by a blank line; each
  // frame's first non-empty line names the event (`event: log`) and the
  // next line carries the payload (`data: {...}`). We accumulate bytes
  // into ``buffer`` and emit complete frames as they arrive.
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const rawFrame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const lines = rawFrame.split('\n');
      let eventName = 'message';
      let dataLine = '';
      for (const line of lines) {
        if (line.startsWith('event:')) eventName = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLine += line.slice(5).trim();
      }
      if (!dataLine) continue;
      let payload: Record<string, unknown> = {};
      try {
        payload = JSON.parse(dataLine);
      } catch {
        continue;
      }
      const frame: FeatureInstallEvent = {
        event: eventName as FeatureInstallEvent['event'],
        ok: payload.ok !== false,
        ...payload,
      };
      onEvent(frame);
      if (eventName === 'done') lastDone = frame;
    }
  }

  if (!lastDone) {
    return { ok: false, restart_required: false, installed: [], failed: features, error: 'install stream ended without a done event' };
  }
  return {
    ok: lastDone.ok && !(lastDone.failed && lastDone.failed.length),
    restart_required: !!lastDone.restart_required,
    installed: lastDone.installed ?? [],
    failed: lastDone.failed ?? [],
    error: (lastDone.error as string | null | undefined) ?? null,
  };
}

// ── System Variables ──

export interface SystemVar {
  name: string;
  description: string;
  value: string | null;
  secret: boolean;
}

export async function fetchSystemVars(
  agentUrl: string,
  token: string
): Promise<SystemVar[]> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/system-vars`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to list system vars: ${res.statusText}`);
  return res.json();
}

// ── Profiles ──

/** Public (no auth): profile names only, for the login screen's dropdown. */
export async function listPublicProfileNames(
  agentUrl: string
): Promise<{ profiles: string[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles/names`);
  if (!res.ok) throw new Error(`Failed to list profile names: ${res.statusText}`);
  return res.json();
}

export async function listProfiles(
  agentUrl: string,
  token: string
): Promise<{ profiles: string[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to list profiles: ${res.statusText}`);
  return res.json();
}

export async function createProfile(
  agentUrl: string,
  token: string,
  name: string
): Promise<{ success: boolean; profile: string }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to create profile: ${res.statusText}`);
  }
  return res.json();
}

export async function deleteProfile(
  agentUrl: string,
  token: string,
  name: string
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to delete profile: ${res.statusText}`);
  return res.json();
}

export async function getPersona(
  agentUrl: string,
  token: string,
  profileName: string
): Promise<{ content: string }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles/${encodeURIComponent(profileName)}/persona`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to load persona: ${res.statusText}`);
  return res.json();
}

export async function updatePersona(
  agentUrl: string,
  token: string,
  profileName: string,
  content: string
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles/${encodeURIComponent(profileName)}/persona`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify({ content }),
  });
  if (!res.ok) throw new Error(`Failed to update persona: ${res.statusText}`);
  return res.json();
}

// ── Agent name ──

export interface AgentNameEntry {
  profile: string;
  name: string;
}

/** Agent names for every visible profile — feeds the chat `@` menu. */
export async function fetchAgentNames(
  agentUrl: string,
  token: string
): Promise<{ agents: AgentNameEntry[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles/agent-names`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to list agent names: ${res.statusText}`);
  return res.json();
}

export async function getAgentName(
  agentUrl: string,
  token: string,
  profileName: string
): Promise<{ name: string }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles/${encodeURIComponent(profileName)}/agent-name`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to load agent name: ${res.statusText}`);
  return res.json();
}

export async function setAgentName(
  agentUrl: string,
  token: string,
  profileName: string,
  name: string
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles/${encodeURIComponent(profileName)}/agent-name`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to update agent name: ${res.statusText}`);
  }
  return res.json();
}

// ── Tool Arguments ──

export async function getToolArguments(
  agentUrl: string,
  token: string,
  toolName: string
): Promise<{ arguments: Record<string, unknown> }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/tools/${encodeURIComponent(toolName)}/arguments`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to load tool arguments: ${res.statusText}`);
  return res.json();
}

export async function updateToolArguments(
  agentUrl: string,
  token: string,
  toolName: string,
  args: Record<string, unknown>
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/tools/${encodeURIComponent(toolName)}/arguments`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify({ arguments: args }),
  });
  if (!res.ok) throw new Error(`Failed to update tool arguments: ${res.statusText}`);
  return res.json();
}

// ── Agents (for the unified agents/tools page) ──

export interface RemoteAgentInfo {
  /** Unique system identifier; the value to pass in /api/agents/{tool_id} URLs. */
  tool_id: string;
  /** Human-readable display name. */
  name: string;
  encoded_name: string;
  description: string;
  url: string;
  badge_class: string;
  status_text: string;
  expiration_info: { timestamp: number; formatted: string; relative: string } | null;
  show_authenticate: boolean;
  show_unlink: boolean;
  arguments_schema: Record<string, unknown> | null;
  agent_type: 'a2a' | 'mcp' | string;
  enabled: boolean;
  /** Profile that originally registered this tool (a2a/mcp only). */
  owner_profile: string | null;
  is_stub: boolean;
  connection_error: string | null;
}

export async function listAgents(
  agentUrl: string,
  token: string
): Promise<{ agents: RemoteAgentInfo[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/agents`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to list agents: ${res.statusText}`);
  return res.json();
}

export async function addAgent(
  agentUrl: string,
  token: string,
  config: { url?: string; type: string; json_config?: string; description?: string }
): Promise<{ success: boolean; agent: { name: string; description: string; url: string; type: string } }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/agents`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(config),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to add agent: ${res.statusText}`);
  }
  return res.json();
}

export async function removeAgent(
  agentUrl: string,
  token: string,
  agentName: string
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/agents/${encodeURIComponent(agentName)}`,
    { method: 'DELETE', headers: authHeaders(token) }
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to remove agent: ${res.statusText}`);
  }
  return res.json();
}

export async function updateAgentConfig(
  agentUrl: string,
  token: string,
  agentName: string,
  config: { description?: string | null }
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/agents/${encodeURIComponent(agentName)}/config`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify(config),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to update agent config: ${res.statusText}`);
  }
  return res.json();
}
