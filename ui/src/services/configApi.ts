/**
 * API client for server configuration, LLM providers, tool management, and profiles.
 */

// Type-only: the setup response echoes the channel rows it created, in the
// same shape the channels API returns them.
import type { ChannelRow } from './channelApi';
import { trackMigrationUpload } from './migrationReadiness';

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

// ── TLS state (CREMIND_SSL) ──
//
// Returned as the ``tls`` block of GET /api/services/capabilities. The whole
// decision the Setup Wizard needs is ``pending_https``: the backend sets it
// true only when *this* server will really come back on HTTPS after its next
// boot, and false wherever TLS can never happen (Electron,
// ``CREMIND_UI_PORT=0``). Gating the "Secure this install" step on it
// therefore inherits those exclusions without the UI sniffing the
// environment itself.
export interface TlsStatus {
  /** ``CREMIND_SSL``: '' (plain HTTP), 'auto' (HTTPS from boot one), or
   *  'after-setup' (HTTP until the wizard finishes, then HTTPS). */
  mode: '' | 'auto' | 'after-setup';
  /** Whether the process answering right now bound TLS. */
  serving_https: boolean;
  /** Whether it will bind TLS on its next boot. See the note above. */
  pending_https: boolean;
  /** SHA-256 of the local CA, colon-separated uppercase hex — the exact
   *  form OS dialogs and browser certificate viewers display. ``null``
   *  when no CA has been generated. */
  ca_sha256: string | null;
  /** Where the server expects to answer once it is serving HTTPS. */
  https_url: string | null;
  /** Whether something supervises this process, so a restart actually
   *  comes back (Docker / Kubernetes) rather than leaving it down. */
  restart_supported: boolean;
  /** Whether THIS server can trust the CA on THIS device — native installs
   *  answering their own machine's browser only. Optional: older servers
   *  omit it, and the wizard degrades to the manual commands. */
  local_trust?: TlsLocalTrust;
}

/** The server-side "Trust it on this device" capability. ``supported`` is
 *  computed per request: it is true only when the server process runs on the
 *  same machine as this browser (native install, loopback peer), i.e. when
 *  ``POST /api/tls/trust`` would land the CA in the right trust store. */
export interface TlsLocalTrust {
  supported: boolean;
  /** Human name of the store the server would write ("the current user's
   *  Trusted Root store", "your login keychain"). Null when unsupported. */
  store: string | null;
  /** Which OS-side prompt to forewarn about (Windows confirmation dialog,
   *  macOS password prompt). */
  os_prompt: 'windows' | 'macos' | 'linux' | null;
  /** Definitive only on Windows; null means unknown, not "no". */
  already_trusted: boolean | null;
  /** Why ``supported`` is false, in showable words. */
  reason: string | null;
}

export interface TrustLocalCaResult {
  trusted: boolean;
  already_trusted?: boolean;
  store?: string | null;
  error?: string;
  /** Copy-pasteable fallback commands when the server-side attempt failed. */
  manual_commands?: string[];
}

export type TlsTransitionPhase = 'prepared' | 'quiescing' | 'activating' | 'active' | 'cancelled';

/** Public, credential-free information used while the server changes origin. */
export interface TlsTransition {
  version: 1;
  id: string;
  phase: TlsTransitionPhase;
  source_origin: string;
  target_origin: string;
  instance_id: string;
  ca_sha256: string | null;
  certificate_kind?: 'none' | 'local' | 'custom' | 'external';
  certificate_sha256?: string | null;
  same_public_port?: boolean;
  public_port?: number;
  created_at: number;
  expires_at: number | null;
  /** The switch is recorded but waiting for a person to change the deployment
   *  (a Helm or Compose upgrade, an unsupervised restart). While this holds the
   *  HTTP application is still serving on the old token epoch and nothing has
   *  been invalidated, so a tab must not block on an HTTPS address that does
   *  not exist yet — and the switch can still be cancelled. */
  awaiting_operator?: boolean;
  /** HTTPS came up but the on-host token files could not be re-signed; the
   *  switch is stuck until an administrator fixes it. */
  activation_error?: string | null;
}

/** One line of deployment guidance. Only a `command` is a shell line, and only
 *  a `command` is ever copyable — a `note` is prose the user reads, so giving
 *  it a copy button is what made the activation step unreadable. Built by
 *  ``app/config/tls_steps.py``; commands arrive bare (no "Run " prefix, no
 *  trailing period, no backticks) so the clipboard text runs as-is. */
export interface TlsInstructionStep {
  kind: 'note' | 'command';
  text: string;
}

export interface TlsRuntimeStatus {
  instance_id: string;
  serving_https: boolean;
  ready?: boolean;
  certificate_error?: string | null;
  mode: '' | 'auto' | 'after-setup' | 'custom';
  install_mode: string;
  management: 'native' | 'electron' | 'external';
  /** True after activation persistence; the client chooses the correct
   * supervisor (REST, Electron IPC, or deployment command). */
  restart_required?: boolean;
  /** A supervised native backend durably armed its own delayed restart. */
  restart_scheduled?: boolean;
  /** Activation persisted, but the supervisor could not schedule restart. */
  restart_error?: string | null;
  /** Number of enrolled tabs still saving uploads and private handoffs. */
  quiesce_pending?: number;
  /** Whether this switch can still be called off from here. False once HTTPS
   *  genuinely serves, and while a restart into it is already armed. */
  can_cancel?: boolean;
  /** Mirrors `transition.activation_error` for clients that read the top level. */
  activation_error?: string | null;
  /** A cancel that could not put the previous native settings back. */
  revert_error?: string | null;
  restart_supported: boolean;
  transition: TlsTransition | null;
  ca_sha256: string | null;
  certificate_kind?: 'none' | 'local' | 'custom' | 'external';
  certificate_sha256?: string | null;
  same_public_port?: boolean;
  public_port?: number;
  https_url: string | null;
  /** The deployment runbook, in the order to follow it. */
  steps?: TlsInstructionStep[];
  /** Legacy flat rendering of `steps`, kept on the wire for CLIs older than
   *  the note/command split. The SPA reads `steps`. */
  instructions?: string[];
}

export interface TlsHandoffState {
  route?: string;
  mount?: '/' | '/electron-renderer/';
  draft?: unknown;
  drafts?: Record<string, string>;
  preferences?: Record<string, string>;
}

export interface TlsHandoffResult {
  ticket: string;
  expires_at: number;
}

export interface TlsHandoffRedeemResult {
  profile: string;
  token: string;
  route: string;
  state?: TlsHandoffState | null;
}

/** A valid one-use ticket whose original profile session has expired or was
 * revoked. Profile and route are safe recovery metadata; no credential is
 * included in this error. */
export class TlsHandoffSessionError extends Error {
  constructor(
    message: string,
    public readonly profile: string,
    public readonly route: string,
  ) {
    super(message);
    this.name = 'TlsHandoffSessionError';
  }
}

/** HTTP failure from a TLS transition endpoint. Callers use only the status
 * code to choose a credential-free login fallback after a session expires. */
export class TlsApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = 'TlsApiError';
  }
}

async function tlsJson<T>(
  url: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(url, { cache: 'no-store', ...init });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const message = typeof body?.error === 'string' ? body.error : `${res.status} ${res.statusText}`;
    throw new TlsApiError(message, res.status);
  }
  return body as T;
}

/** Read the live TLS state and its deployment runbook.
 *
 *  The token is optional, and the call must keep working without one: the
 *  pre-sign-in recovery page polls this, and during the HTTPS handoff the
 *  transition code polls the *target* origin, where this browser holds no
 *  session yet. Pass it wherever the SPA has one, because what comes back
 *  depends on who asked — the Kubernetes ``kubernetes`` identity, and the
 *  runbook naming the real namespace / release / Deployment instead of
 *  placeholders, are admin-only (see ``tls_status_payload``). An anonymous
 *  Settings page therefore renders the very placeholder runbook the identity
 *  feature exists to remove.
 *
 *  Only the bearer, never a Content-Type: this is a GET, and the token-less
 *  cross-origin polls above stay CORS-simple with no headers at all.
 */
export function fetchTlsStatus(agentUrl: string, token?: string): Promise<TlsRuntimeStatus> {
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return tlsJson<TlsRuntimeStatus>(`${resolveBaseUrl(agentUrl)}/api/tls/status`, { headers });
}

export function prepareHttps(
  agentUrl: string,
  token: string,
  sourceOrigin = window.location.origin,
): Promise<TlsRuntimeStatus> {
  return tlsJson<TlsRuntimeStatus>(`${resolveBaseUrl(agentUrl)}/api/tls/prepare`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ source_origin: sourceOrigin }),
  });
}

export async function activateHttps(
  agentUrl: string,
  token: string,
  transitionId: string,
  certificateSha256: string | null,
  restart = true,
  onQuiescing?: (status: TlsRuntimeStatus) => void,
  continueWaiting: () => boolean = () => true,
): Promise<TlsRuntimeStatus> {
  const deadline = Date.now() + 5 * 60_000;
  while (true) {
    const result = await tlsJson<TlsRuntimeStatus>(`${resolveBaseUrl(agentUrl)}/api/tls/activate`, {
      method: 'POST',
      headers: authHeaders(token),
      body: JSON.stringify({
        transition_id: transitionId,
        certificate_sha256: certificateSha256,
        restart,
      }),
    });
    if (result.transition?.phase !== 'quiescing') return result;
    onQuiescing?.(result);
    if (!continueWaiting()) throw new Error('HTTPS activation was cancelled.');
    if (Date.now() >= deadline) {
      throw new Error(
        `${result.quiesce_pending ?? 'Some'} Cremind tab(s) did not finish preparing for HTTPS. `
        + 'Finish or cancel uploads in those tabs, close obsolete tabs, or cancel and retry the switch.',
      );
    }
    await new Promise(resolve => setTimeout(resolve, 300));
  }
}

export function registerTlsClient(
  agentUrl: string,
  token: string,
  tabId: string,
): Promise<TlsRuntimeStatus> {
  return tlsJson<TlsRuntimeStatus>(`${resolveBaseUrl(agentUrl)}/api/tls/client`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ tab_id: tabId }),
  });
}

export function unregisterTlsClient(
  agentUrl: string,
  token: string,
  tabId: string,
  keepalive = false,
): Promise<TlsRuntimeStatus> {
  return tlsJson<TlsRuntimeStatus>(`${resolveBaseUrl(agentUrl)}/api/tls/client`, {
    method: 'DELETE',
    headers: authHeaders(token),
    body: JSON.stringify({ tab_id: tabId }),
    keepalive,
  });
}

export function acknowledgeTlsReady(
  agentUrl: string,
  token: string,
  tabId: string,
  transitionId: string,
): Promise<TlsRuntimeStatus> {
  return tlsJson<TlsRuntimeStatus>(`${resolveBaseUrl(agentUrl)}/api/tls/ready`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ tab_id: tabId, transition_id: transitionId }),
  });
}

export function cancelHttps(
  agentUrl: string,
  token: string,
  transitionId: string,
): Promise<TlsRuntimeStatus> {
  return tlsJson<TlsRuntimeStatus>(`${resolveBaseUrl(agentUrl)}/api/tls/cancel`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ transition_id: transitionId }),
  });
}

export function createTlsHandoff(
  agentUrl: string,
  token: string,
  payload: {
    transition_id: string;
    source_origin: string;
    target_origin: string;
    route: string;
    state?: TlsHandoffState;
  },
): Promise<TlsHandoffResult> {
  return tlsJson<TlsHandoffResult>(`${resolveBaseUrl(agentUrl)}/api/tls/handoff`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(payload),
  });
}

export async function redeemTlsHandoff(
  targetOrigin: string,
  ticket: string,
): Promise<TlsHandoffRedeemResult> {
  const res = await fetch(`${targetOrigin.replace(/\/$/, '')}/api/tls/handoff/redeem`, {
    method: 'POST',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticket }),
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401 && typeof body?.profile === 'string' && typeof body?.route === 'string') {
    throw new TlsHandoffSessionError(
      typeof body?.error === 'string' ? body.error : 'The original session expired.',
      body.profile,
      body.route,
    );
  }
  if (!res.ok) {
    throw new Error(typeof body?.error === 'string' ? body.error : `${res.status} ${res.statusText}`);
  }
  return body as TlsHandoffRedeemResult;
}

/** One-click CA trust — ``POST /api/tls/trust``. The fingerprint echo is
 *  mandatory: it proves this page read the CA from the same origin and pins
 *  the request to that exact CA.
 *
 *  The token is optional because the Setup Wizard calls this inside the
 *  pre-setup bootstrap window, where no JWT can exist yet and the backend
 *  skips its admin gate. Post-setup callers (Settings → HTTPS & Certificate)
 *  MUST pass the admin token, or ``require_admin`` 401s the request. */
export async function trustLocalCa(
  agentUrl: string,
  caSha256: string,
  token?: string,
): Promise<TrustLocalCaResult> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/tls/trust`, {
    method: 'POST',
    headers: authHeaders(token ?? ''),
    body: JSON.stringify({ ca_sha256: caSha256 }),
  });
  // Every outcome — success, refusal, tool failure — carries a structured
  // body; prefer its message over an opaque HTTP error.
  try {
    return (await res.json()) as TrustLocalCaResult;
  } catch {
    return { trusted: false, error: `Trust request failed: ${res.statusText}` };
  }
}

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
  /** What TLS this server serves, and what it will serve next. Optional:
   *  a server older than the after-setup HTTPS feature omits the block
   *  entirely, and every consumer must degrade to the feature simply not
   *  existing rather than assuming plain HTTP forever. */
  tls?: TlsStatus;
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

/** Everything the Setup Wizard reads off ``POST /api/config/setup``.
 *
 *  Fields past ``profile`` are optional because they are conditional on the
 *  server: ``channels``/``channel_errors`` only appear when the wizard asked
 *  for channels, and the three ``tls_*`` fields only exist on a server new
 *  enough to have the after-setup HTTPS hand-off. Callers fall back to what
 *  ``/api/services/capabilities`` already told them.
 */
export interface CompleteSetupResponse {
  success: boolean;
  token: string;
  expires_at?: string;
  profile: string;
  /** A newly-installed feature (torch, a DB driver, …) needs a fresh
   *  process before it can be activated. Unrelated to the TLS switch. */
  restart_required?: boolean;
  channels?: ChannelRow[];
  channel_errors?: Array<{ channel_type: string | null; error: string }>;
  /** The server will serve HTTPS after its next boot — the wizard should
   *  restart it and pivot the browser to ``next_origin``. */
  tls_pending?: boolean;
  /** Where to send the browser after the restart. Derived server-side from
   *  the request's own Host header, so it is already correct behind a
   *  port-forward or an SSH tunnel. */
  next_origin?: string | null;
  /** Whether the wizard may trigger that restart itself. */
  restart_supported?: boolean;
  /** Which supervisor owns the server process used by the HTTPS pivot. */
  tls_management?: 'native' | 'electron' | 'external';
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
): Promise<CompleteSetupResponse> {
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

/** Ask the server to restart itself (admin-gated; returns 202 Accepted).
 *
 *  Same endpoint and success rule as ``triggerHttpRestart`` in
 *  ``composables/useServerRestart.ts``, but parameterised rather than
 *  reading the settings store: the HTTPS pivot fires this with the token
 *  the Setup Wizard has just minted, which is not the active session token
 *  yet at the moment of the call.
 */
export async function requestServerRestart(
  agentUrl: string,
  token: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${base}/api/system/restart`, {
    method: 'POST',
    headers,
  });
  // 202 is the happy path (``res.ok`` already covers it); 401/403/5xx
  // surface as errors so the caller can offer a manual fallback.
  if (!res.ok && res.status !== 202) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as { error?: string }).error ?? `HTTP ${res.status}`);
  }
}

// ── Deployment identity and desktop access ──
//
// Both blocks travel on ``GET /api/system/environment`` *and* on
// ``GET /api/config/install-secrets``, in the same shape. The Setup Wizard
// only ever reads install-secrets and the Developer page reads the
// environment, so a fact carried by one endpoint alone would be missing from
// half the places that describe the same install.

/** What a pod cannot work out about itself. The chart states it in
 *  ``CREMIND_K8S_*`` and the app echoes it here, so the UI can print
 *  ``kubectl --namespace lee-cremind port-forward svc/cremind 1515:80``
 *  instead of ``<namespace>`` / ``<release>`` placeholders.
 *
 *  The whole block is null off Kubernetes. Individual fields stay null on an
 *  older chart that injects no identity env — ``source`` says how much is
 *  known: ``chart`` (stated outright) or ``inferred`` (read off the pod name
 *  and the service-account namespace file, which cannot recover the release). */
export interface KubernetesIdentity {
  namespace?: string | null;
  /** Helm release name — the argument ``helm upgrade`` takes. */
  release?: string | null;
  /** Deployment name, which the chart also uses as the Service name. */
  workload?: string | null;
  service?: string | null;
  service_port?: number | null;
  source?: 'chart' | 'inferred' | null;
  /** Ready-to-paste reconnect command; null unless the namespace and the
   *  Service are both known. */
  port_forward?: string | null;
}

/** One bare ``kubectl port-forward`` line and the URL it makes reachable. */
export interface VncPortForwardCommand {
  label: string;
  /** A single shell line — no "Run " prefix, no trailing period — so
   *  ``DeploymentSteps`` can copy it verbatim. */
  command: string;
  open_url: string;
}

/** How the VNC desktop is reached on this install. Only the server knows how
 *  noVNC was wired (its own published port, the Kubernetes proxy sidecar's
 *  path, or a TCP relay that needs a tunnel), so it names the shape and the
 *  browser fills in the address it actually used — see ``utils/vncDesktop.ts``.
 *
 *  ``enabled`` false (native installs, and the basic image) means there is no
 *  desktop at all: ``access`` is null and the card stays hidden. */
export interface VncAccess {
  enabled: boolean;
  /** ``direct`` — noVNC on its own port on the same host as Cremind;
   *  ``same_origin`` — served by the proxy sidecar on Cremind's own origin;
   *  ``port_forward`` — reachable only through the tunnel below. */
  access: 'direct' | 'same_origin' | 'port_forward' | null;
  novnc_path?: string | null;
  novnc_port?: number | null;
  /** Absolute URL when the deployment states one (the chart's
   *  ``CREMIND_NOVNC_URL``); null when the browser has to compose it. */
  novnc_url?: string | null;
  port_forward_commands?: VncPortForwardCommand[];
  /** Why this install is reached that way — including, for ``direct``, that
   *  Cremind's own HTTPS does not cover the noVNC port. */
  scheme_note?: string | null;
}

export interface InstallSecrets {
  /** Container or host process — that is the only question this field
   *  answers, so a Kubernetes install reports ``docker``. ``install_mode``
   *  below is what names the deployment. */
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
  // Set by deployments that know where noVNC answers (the Helm chart), for
  // the cases the browser cannot infer — see the API's comment.
  novnc_url?: string | null;
  // Which database backend bootstrap.toml records. The Developer page's
  // config re-download needs it because ``/api/config/server`` deliberately
  // never returns it — ``db_provider`` is a bootstrap-only key. Optional: a
  // server older than this field simply omits it and the caller falls back.
  db_provider?: string | null;
  // Postgres block — populated when the Setup Wizard configured Postgres
  // (bootstrap.toml has db_provider="postgres").
  pg_host?: string | null;
  pg_port?: number | null;
  pg_user?: string | null;
  pg_password?: string | null;
  pg_database?: string | null;
  pg_sslmode?: string | null;
  pg_deployment_mode?: 'docker' | 'external' | null;
  /** Which cluster object this install is, and how to reach its desktop —
   *  the same blocks ``/api/system/environment`` reports. Repeated here
   *  because the Setup Wizard never calls that endpoint: it builds the whole
   *  config file out of install-secrets. Null / absent on older servers. */
  kubernetes?: KubernetesIdentity | null;
  vnc?: VncAccess | null;
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

/** List a provider's models.
 *
 * ``authMethod`` previews the model set a given auth method serves without
 * having to save it first — some providers (OpenAI's Codex OAuth) serve a
 * different model set per method, so the pickers pass the *selected* method
 * rather than letting the server fall back to the stored/default one.
 */
export async function getProviderModels(
  agentUrl: string,
  token: string,
  providerName: string,
  authMethod?: string
): Promise<{ provider: Record<string, unknown>; models: LLMModel[] }> {
  const base = resolveBaseUrl(agentUrl);
  const query = authMethod ? `?auth_method=${encodeURIComponent(authMethod)}` : '';
  const res = await fetch(`${base}/api/llm/providers/${encodeURIComponent(providerName)}/models${query}`, {
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
  // How the server is deployed, and — for containerized installs — what that
  // deployment needs before the browser can reach the callback listener
  // (a published port under Docker, a port-forward under Kubernetes).
  // `capture_hint` is null when capture is unconditional (native installs).
  deployment?: 'native' | 'docker' | 'kubernetes';
  capture_hint?: string | null;
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
  return trackMigrationUpload(async () => {
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
  });
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

// ── Runtime environment ──

/** What the server's CPU can actually execute.
 *
 *  This exists because a bundled single-file binary is compiled for a
 *  microarchitecture level, not merely for x86-64: the Claude Code CLI needs
 *  x86-64-v2, and on a virtual CPU that reports none of those flags it spins
 *  for ever instead of failing. So the flags are a first-class fact about the
 *  install, worth showing next to the OS and the Python version.
 *
 *  Every field is optional: the backend only knows any of this on Linux and
 *  only for x86_64, and an older backend omits the block entirely. */
export interface CpuFeatures {
  /** ``platform.machine()`` — ``x86_64``, ``aarch64``, … */
  arch?: string | null;
  /** The CPU's own model name, e.g. "QEMU Virtual CPU version 2.5+". */
  model?: string | null;
  /** Whether the kernel sees itself running under a hypervisor. */
  hypervisor?: boolean | null;
  /** False when the flags could not be read at all — not Linux, not x86_64 —
   *  in which case ``present``/``missing`` say nothing and must not be shown
   *  as if they did. */
  flags_known?: boolean | null;
  present?: string[];
  missing?: string[];
  /** The highest x86-64 microarchitecture level the flags support. Null when
   *  the flags are unknown. */
  x86_64_level?: 'v1' | 'v2' | 'v3' | null;
}

/** What ``GET /api/system/environment`` reports about the running install:
 *  how it was installed, how it is deployed, and where its data lives.
 *
 *  Every field is optional because the endpoint's shape grows over time and
 *  this SPA is served by whatever backend the user upgraded to — a missing
 *  field must render as "unknown", never break the card. */
export interface SystemEnvironment {
  /** Upgrade channel this install follows: production | test | dev. */
  release_channel?: string | null;
  /** How Cremind was installed: docker | native | kubernetes. */
  install_mode?: string | null;
  /** How it is deployed: local | server | custom | kubernetes. */
  deployment?: string | null;
  /** Whether the process runs inside a container. */
  container?: boolean | null;
  /** Which image was built: basic (headless) or desktop (VNC). */
  image_flavor?: string | null;
  /** Whether the VNC desktop is part of this deployment. */
  vnc_enabled?: boolean | null;
  /** Whether something restarts the process for us (Docker, Kubernetes, a
   *  boot service) — the same fact the Restart card warns about. */
  supervised?: boolean | null;
  /** Whether the backend was launched by the Electron shell. */
  electron?: boolean | null;
  os?: string | null;
  os_release?: string | null;
  python_version?: string | null;
  backend_version?: string | null;
  app_url?: string | null;
  system_dir?: string | null;
  install_dir?: string | null;
  host?: string | null;
  /** The zone this profile's schedules really fire in, resolved per caller:
   *  its own ``system.timezone``, else the admin's, else ``CREMIND_TIMEZONE``,
   *  else the host clock. Never blank, so it is the timezone worth showing. */
  effective_timezone?: string | null;
  /** The ``CREMIND_TIMEZONE`` boot default on its own — blank on most
   *  installs, and only one of the inputs to the resolved zone above. */
  boot_timezone?: string | null;
  /** The custom-deployment .env values (listen host, public URL, allowed
   *  origins, wizard preset) — only meaningful when ``deployment`` is
   *  ``custom``, and what the config export re-emits as its custom fields. */
  deployment_custom_fields?: Record<string, string> | null;
  /** Which namespace, Helm release and Deployment / Service this pod is. Null
   *  off Kubernetes, and null per field on a chart too old to state them. */
  kubernetes?: KubernetesIdentity | null;
  /** How to reach the VNC desktop, or ``enabled: false`` when there is none. */
  vnc?: VncAccess | null;
  /** What this host's CPU supports — the fact that decides whether a bundled
   *  binary built for a newer microarchitecture level can run here at all. */
  cpu?: CpuFeatures | null;
}

export async function fetchSystemEnvironment(
  agentUrl: string,
  token: string,
): Promise<SystemEnvironment> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/system/environment`, {
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to load environment: ${res.statusText}`);
  }
  return res.json();
}

// ── Coding agents (Claude Code / Codex) ──

/** How a coding agent is signed in.
 *
 *  The login belongs to the agent's own CLI, not to Cremind's LLM provider
 *  settings — the Claude CLI keeps its credential independently of the
 *  Anthropic provider — so this describes a command to run, never a route
 *  into Settings. */
export interface CodingAgentSignIn {
  /** ``terminal`` — Cremind runs ``cli_login`` in a built-in PTY and the user
   *  answers its prompts there; ``device_code`` — the card shows a URL and a
   *  code and waits. */
  method: 'terminal' | 'device_code';
  label: string;
  instructions: string;
  /** The same commands as a user would type them on the server host, for the
   *  "or run this on the server" line. */
  cli_login: string;
  cli_logout: string;
}

/** The agent's own command-line tool cannot run on this server's CPU.
 *
 *  This is not "not signed in" and not "not installed": the binary is there
 *  and it is the right binary, but it was built for a microarchitecture level
 *  the host does not provide, so every invocation of it hangs. Nothing that
 *  goes through the CLI can work while this is set — not signing in, not the
 *  sign-in check, not a coding task — and no Cremind-side setting changes
 *  that, because the fix is on the machine (or its hypervisor). The remedy
 *  therefore names what an operator has to change, and the UI's job is to say
 *  so immediately rather than let each surface hang in its own way. */
export interface CodingAgentHostBlock {
  /** Which check refused. ``cpu_features`` today; kept open so a future
   *  blocker can be told apart without reading the prose. */
  code: string;
  /** One sentence naming the CPU and the level it is short of — the same
   *  sentence the backend puts in the listing's ``message``. */
  message: string;
  /** What to change on the host to make the CLI runnable. */
  remedy: string;
  cpu_model: string | null;
  /** The microarchitecture flags the CLI needs and this CPU lacks. */
  missing: string[];
  hypervisor: boolean;
}

export interface CodingAgentStatus {
  tool_id: string;
  display_name: string;
  /** Optional-dependency key to hand ``streamFeaturesInstall``. */
  feature_key: string;
  extras: string[];
  sdk_installed: boolean;
  requires_restart_after_install: boolean;
  enabled: boolean;
  /** Non-secret label of where the credential comes from
   *  (``profile_setup_token``, ``host_claude_login``, …). Null when the
   *  agent has no credential at all. */
  credential_source: string | null;
  /** Whose login that credential is: ``profile`` (this profile signed in
   *  itself) or ``shared`` (the server-wide login every profile inherits).
   *  Null when no CLI login is in play — an API key has no scope, and a
   *  profile that never signed in and has no fallback has nothing to scope. */
  credential_scope: 'profile' | 'shared' | null;
  /** The CLI home the login lives in (``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``),
   *  so the card can say where signing out would take effect. */
  cli_home: string | null;
  /** Non-secret account summary read off that home — email, plan, org. Null
   *  when nothing is signed in, or when the home says nothing about it. */
  account_hint: Record<string, unknown> | null;
  /** Whether the CLI binary is actually present on this server. The SDK can
   *  be installed while the binary is not, and sign-in needs the binary — so
   *  this gates the Sign-in button independently of ``sdk_installed``. */
  cli_available: boolean;
  credentials_configured: boolean;
  sign_in: CodingAgentSignIn;
  /** Set when this host cannot run the agent's CLI at all, so the card can
   *  refuse up front instead of offering buttons that would hang. Optional:
   *  a backend that predates the check omits it, and the page then behaves
   *  exactly as it did before. */
  cli_blocked?: CodingAgentHostBlock | null;
  /** Human-readable summary of the state above. */
  message: string;
}

export async function fetchCodingAgents(
  agentUrl: string,
  token: string,
): Promise<{ agents: CodingAgentStatus[] }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/coding-agents`, {
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to list coding agents: ${res.statusText}`);
  }
  return res.json();
}

/** Run the agent's own live sign-in check. The payload is whatever the
 *  tool's status leaf reports (``logged_in``, a detail message, the resolved
 *  credential source) — the same structure the agent sees, so the UI and the
 *  chat answer can never disagree. */
export async function probeCodingAgent(
  agentUrl: string,
  token: string,
  toolId: string,
  opts?: { fresh?: boolean },
): Promise<Record<string, unknown>> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/coding-agents/${encodeURIComponent(toolId)}/probe`,
    {
      method: 'POST',
      headers: authHeaders(token),
      // A probe spawns the CLI, so the server holds its answer for a few
      // seconds. ``fresh`` is what an explicit "Check sign-in" click — or the
      // moment a login dialog closes — must send: the user has just changed
      // the thing being reported, and the cached answer predates the change.
      body: JSON.stringify(opts?.fresh ? { fresh: true } : {}),
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to check sign-in: ${res.statusText}`);
  }
  return res.json();
}

// ── Codex device-code sign-in ──
//
// The server holds the live SDK handle (and the ``codex app-server`` child it
// spawned) for up to fifteen minutes; ``login_id`` is the only handle the
// browser ever gets, and it dies with the server process.

export interface CodexDeviceLoginStart {
  login_id: string;
  verification_url: string;
  user_code: string;
  /** Seconds the code stays valid. */
  expires_in: number;
  /** Set instead of the fields above when the CLI could be reached but the
   *  flow refused to start — a 200 with a reason beats a bare 500. */
  error?: string | null;
}

export interface CodexDeviceLoginStatus {
  login_id: string;
  status: 'starting' | 'pending' | 'success' | 'error' | 'cancelled';
  detail?: string | null;
  /** Non-secret account summary once ``status`` is ``success``. */
  account?: Record<string, unknown> | null;
  verification_url?: string | null;
  user_code?: string | null;
}

/** Start the flow. ``signal`` exists because starting it means spawning a CLI
 *  on the server: the route bounds itself, but a request that never answers at
 *  all (a wedged child, a proxy that swallowed the response) has no bound on
 *  this side, and the caller is left showing "starting" for good. */
export async function startCodexDeviceLogin(
  agentUrl: string,
  token: string,
  profile?: string,
  signal?: AbortSignal,
): Promise<CodexDeviceLoginStart> {
  const base = resolveBaseUrl(agentUrl);
  // The server resolves the profile from the bearer token; the query is only
  // ever a redundant statement of the same fact, so it is left off unless the
  // caller passes one explicitly.
  const params = profile ? `?profile=${encodeURIComponent(profile)}` : '';
  const res = await fetch(`${base}/api/coding-agents/codex/login${params}`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({}),
    signal,
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to start sign-in: ${res.statusText}`);
  }
  return res.json();
}

export async function getCodexDeviceLogin(
  agentUrl: string,
  token: string,
  loginId: string,
): Promise<CodexDeviceLoginStatus> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/coding-agents/codex/login/${encodeURIComponent(loginId)}`,
    { headers: authHeaders(token) },
  );
  // The session lives only in the server's memory, so a restart mid-login
  // answers 404 rather than an error status. Say what happened instead of
  // leaving the dialog spinning on "failed to check".
  if (res.status === 404) {
    throw new Error('Sign-in interrupted (the server restarted). Start again.');
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to check sign-in: ${res.statusText}`);
  }
  return res.json();
}

export async function cancelCodexDeviceLogin(
  agentUrl: string,
  token: string,
  loginId: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  await fetch(
    `${base}/api/coding-agents/codex/login/${encodeURIComponent(loginId)}/cancel`,
    { method: 'POST', headers: authHeaders(token) },
  ).catch(() => { /* best-effort cleanup — the session times out anyway */ });
}

// ── Terminal sign-in, sign-out, and what the CLI looks like on the server ──

/** A PTY running the agent's own ``login`` command. Same fields as a
 *  ``TerminalRow`` from ``terminalApi.ts`` (the dialog hands ``terminal_id``
 *  to ``TerminalSession``), plus what the login is for. */
export interface CodingAgentLoginTerminal {
  terminal_id: string;
  title: string;
  shell: string;
  working_dir: string;
  /** Unix seconds (wall clock) when the terminal was created. */
  created_at: number;
  tool_id: string;
  scope: 'profile' | 'shared';
  /** The CLI home this login will land in. */
  cli_home: string;
  /** The argv the PTY is running, joined for display. */
  command: string;
}

/** Open a terminal already running the agent's login command.
 *
 *  ``scope`` defaults to this profile's own CLI home; ``shared`` writes the
 *  server-wide login every profile falls back to and is admin-only. */
export async function openCodingAgentLoginTerminal(
  agentUrl: string,
  token: string,
  toolId: string,
  opts: { scope?: 'profile' | 'shared'; cols?: number; rows?: number } = {},
): Promise<CodingAgentLoginTerminal> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/coding-agents/${encodeURIComponent(toolId)}/login-terminal`,
    { method: 'POST', headers: authHeaders(token), body: JSON.stringify(opts) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to open the sign-in terminal: ${res.statusText}`);
  }
  return res.json();
}

export async function logoutCodingAgent(
  agentUrl: string,
  token: string,
  toolId: string,
  opts?: { scope?: 'profile' | 'shared' },
): Promise<{ ok: boolean; scope: 'profile' | 'shared'; detail?: string | null }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/coding-agents/${encodeURIComponent(toolId)}/logout`,
    { method: 'POST', headers: authHeaders(token), body: JSON.stringify(opts ?? {}) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to sign out: ${res.statusText}`);
  }
  return res.json();
}

/** Where the agent's CLI is on the server and how it would be invoked there.
 *
 *  This is what makes ``cremind tools coding-agents login`` able to refuse
 *  politely: the CLI compares this binary and system dir against its own
 *  machine and, when they are not the same box, tells the user which host to
 *  run the login on instead of execing something that isn't there. */
export interface CodingAgentCli {
  tool_id: string;
  /** Absolute path on the server, or null when no binary was found. */
  binary: string | null;
  binary_source: 'tool_variable' | 'bundled' | 'path' | null;
  login_argv: string[];
  logout_argv: string[];
  status_argv: string[];
  /** Environment that points the CLI at this profile's home, and at the
   *  shared one — exported before the login is exec'd. */
  profile_env: Record<string, string>;
  shared_env: Record<string, string>;
  server_hostname: string;
  system_dir: string;
  platform: string;
  /** Set when the binary above exists but this host cannot execute it, so the
   *  CLI can refuse before exec'ing something that would never return.
   *  Optional for the same reason as on the listing row. */
  cli_blocked?: CodingAgentHostBlock | null;
}

export async function getCodingAgentCli(
  agentUrl: string,
  token: string,
  toolId: string,
): Promise<CodingAgentCli> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/coding-agents/${encodeURIComponent(toolId)}/cli`,
    { headers: authHeaders(token) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to look up the CLI: ${res.statusText}`);
  }
  return res.json();
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
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to delete profile: ${res.statusText}`);
  }
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

export async function getInstructions(
  agentUrl: string,
  token: string,
  profileName: string
): Promise<{ content: string }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles/${encodeURIComponent(profileName)}/instructions`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to load instructions: ${res.statusText}`);
  return res.json();
}

export async function updateInstructions(
  agentUrl: string,
  token: string,
  profileName: string,
  content: string
): Promise<{ success: boolean }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/profiles/${encodeURIComponent(profileName)}/instructions`, {
    method: 'PUT',
    headers: authHeaders(token),
    body: JSON.stringify({ content }),
  });
  if (!res.ok) throw new Error(`Failed to update instructions: ${res.statusText}`);
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
