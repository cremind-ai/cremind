/**
 * Typed client for Documentation search (`/api/documentation-search/*`).
 *
 * Backs Settings → My Documents (the admin's server-wide gate included, as that
 * page's Administrator settings section) and the NavRail sync chip. Every route
 * acts on the caller's own profile — the server takes it from the Bearer token,
 * never from a body or query field — so nothing here names a profile.
 *
 * Errors keep the server's machine-readable shape (`DocumentsApiError.code`,
 * `.details`, `.plan`, `.confirm`, `.missing`) instead of flattening it into a
 * message, because the UI branches on it: `ConfirmationRequired` opens a plan
 * dialog, `FeatureNotInstalled` opens the install dialog, `EngineNotRunning`
 * swaps the live panels for an explanation.
 *
 * Destructive changes are two-step on the server (a 409 carrying a plan and a
 * token, then the same request again with the token). `sendWithConfirm` turns
 * that into a value the page can act on: either the result, or the plan plus a
 * `confirm()` that repeats the request with the token.
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

// ── shared shapes ──────────────────────────────────────────────────────────

/** Top-level sync state, in the precedence order of the design's state table. */
export type DocumentsState =
  | 'disabled'
  | 'suspended'
  | 'blocked'
  | 'hold'
  | 'awaiting_confirmation'
  | 'estimating'
  | 'paused'
  | 'scanning'
  | 'reembedding'
  | 'indexing'
  | 'idle'
  | 'error'
  | 'unknown';

/** How the agent tool behaves right now. */
export type DocumentsToolMode = 'hidden' | 'normal' | 'lexical_only' | 'partial';

export type StorageLevel = 'ok' | 'warn' | 'budget' | 'disk_low' | 'disk_critical';

/** Per-file index status (the `files.status` column of the index). */
export type DocumentsFileStatus =
  | 'dirty'
  | 'indexed'
  | 'metadata_only'
  | 'awaiting_extractor'
  | 'deferred'
  | 'error'
  | 'missing'
  | 'tombstone';

export interface DocumentsSourceState {
  enabled: boolean;
  /** The folder the index was built from: the profile's working directory,
   *  or its previous one while a move waits for `confirm_root_change`. */
  root: string | null;
  first_sync_confirmed: boolean;
}

export interface DocumentsBatch {
  label: string | null;
  total: number;
  done: number;
  failed: number;
  skipped: number;
  eta_s?: number | null;
  rate_per_min?: number | null;
}

export interface DocumentsCurrentFile {
  name: string;
  rel_path: string;
  /** read | hash | extract | chunk | index */
  stage: string;
  /** Only while indexing: chunks written so far. */
  progress: { done: number; total: number } | null;
  started_at: number;
}

export interface DocumentsRecentItem {
  ts: number;
  /** added | updated | moved | removed | metadata_only | failed | skipped */
  kind: string;
  name: string;
  rel_path: string;
  message: string;
}

export interface DocumentsFailedItem {
  fid: string | null;
  name: string;
  rel_path: string;
  reason: string | null;
  message: string;
}

export interface DocumentsStorageSnapshot {
  level?: StorageLevel;
  used_bytes?: number;
  global_bytes?: number;
  budget_bytes?: number;
  profile_budget_bytes?: number | null;
  free_bytes?: number | null;
  total_bytes?: number | null;
  method?: string | null;
  message?: string | null;
}

export interface DocumentsEstimate {
  state: 'none' | 'running' | 'done' | 'error' | 'stopped';
  files?: number;
  dirs?: number;
  bytes?: number;
  by_kind?: Record<string, number>;
  images_to_caption?: number;
  placeholders?: number;
  chunks?: number;
  index_bytes?: number;
  seconds?: number;
  root?: string | null;
  error?: string | null;
  started_at?: number;
  finished_at?: number;
}

export type DocumentsConfirmation =
  | { kind: 'first_sync'; estimate: DocumentsEstimate }
  | { kind: 'mass_delete'; missing: number; total: number }
  | {
    kind: 'root_change';
    from: string | null;
    to: string | null;
    /** Indexed files now. */
    files: number;
    /** How many of them are outside the new folder and would leave the
     *  index; null when the engine could not count. */
    leaving?: number | null;
    message?: string;
  };

/** Where an indexed file comes from. */
export type DocumentsSourceKind = 'local' | 'drive';

/**
 * Why the Drive source is held (`source_state('drive')`). Revoked and
 * unlinked hide Drive from search and remove its index at `detail.purge_at`;
 * unreachable and misconfigured keep the index searchable (possibly stale)
 * and never remove anything.
 */
export type DocumentsDriveHoldReason =
  | 'auth_revoked'
  | 'drive_unlinked'
  | 'drive_unreachable'
  | 'drive_misconfigured';

/** A folder in `include_folders`, as the Drive view reports it: a bare
 *  Drive folder id, or one the server resolved to a name. */
export type DocumentsDriveFolderRef = string | { id: string; name?: string | null; path?: string | null };

/**
 * The Drive half of the snapshot (`snapshot.drive`, the runtime's
 * `DriveSource.view()`). It is reported beside the top-level state rather
 * than folded into it, because that state belongs to the local folder: a
 * Drive hold must not hide a local one, nor the reverse.
 */
export interface DocumentsDriveView {
  enabled: boolean;
  /** live | hold | disabled */
  state: 'live' | 'hold' | 'disabled' | string;
  reason: DocumentsDriveHoldReason | string | null;
  /** Hold details: `purge_at` (epoch ms, revoked / unlinked only), `message`. */
  detail?: Record<string, any> | null;
  /** The Google account the index was built from. */
  account_email?: string | null;
  identity_set?: boolean;
  whole_drive?: boolean;
  include_folders?: DocumentsDriveFolderRef[];
  /** Last change-feed poll and last full reconcile (epoch s or ms). */
  last_sync_at?: number | null;
  last_full_at?: number | null;
  counts?: { indexed?: number; pending?: number; error?: number; metadata_only?: number };
  /** A held mass removal, confirmed with `source: 'drive'`. */
  confirmation?: DocumentsConfirmation | null;
}

/**
 * Whether the indexed folder outlives the container (`snapshot.docker`, the
 * runtime's `docker_view()` over `deploy_env.docker_root_status`). Read once
 * per configure; null while the local folder is off.
 */
export interface DocumentsDockerStatus {
  in_container: boolean;
  /** The container is a Kubernetes pod: the fix is the chart's work volume. */
  kubernetes: boolean;
  /** False: the folder is only a directory in the container's own layer.
   *  Null: unknown (not Linux, no mountinfo). */
  root_mounted: boolean | null;
  persistent?: boolean | null;
  fstype?: string | null;
  /** The compose file bind-mounts a host folder there (`CREMIND_DOCUMENTS_BIND=1`);
   *  a missing mount is then the `root_unavailable(bind_missing)` hold instead. */
  bind_expected: boolean;
  /** The compose `volumes:` line that mounts a host folder there. */
  snippet: string | null;
}

/**
 * The one snapshot every surface reads (`app/documents/state.py`). Each frame
 * replaces the previous one outright. Everything after `sources` is only
 * present while the sync engine runs for the profile.
 */
export interface DocumentsSnapshot {
  v: number;
  /** Server process id; with `seq` it orders frames (see `isStaleSnapshot`). */
  boot?: string;
  seq?: number;
  /** Server wall clock, epoch ms. */
  ts?: number;
  allowed?: boolean;
  enabled: boolean;
  state: DocumentsState;
  reason: string | null;
  tool_mode?: DocumentsToolMode;
  sources?: { local: DocumentsSourceState | null; drive: DocumentsSourceState | null };
  /** Hold details: root_invalid {code, message, root}, root_unavailable
   *  {why, …docker status}, pending_root_change {from, to, message}. */
  detail?: Record<string, any> | null;
  phase?: string | null;
  batch?: DocumentsBatch;
  /** File counts by `DocumentsFileStatus`. */
  stages?: Record<string, number>;
  current?: DocumentsCurrentFile[];
  queue_preview?: string[];
  recent?: DocumentsRecentItem[];
  failed_preview?: DocumentsFailedItem[];
  vision?: Record<string, unknown>;
  storage?: DocumentsStorageSnapshot;
  reembed?: { done: number; total: number };
  vector_coverage_pct?: number | null;
  confirmation?: DocumentsConfirmation | null;
  engine_sources?: Record<string, { root?: string; watch?: string | null; watch_reason?: string | null }>;
  watch?: { mode: string | null; reason: string | null };
  /** The container under the local folder (see `dockerWarning`). */
  docker?: DocumentsDockerStatus | null;
  /** Google Drive, reported separately from the local folder's state. */
  drive?: DocumentsDriveView | null;
}

/**
 * Whether `next` is older than (or the same as) the frame already held.
 *
 * `seq` is one server-wide counter, so frames from the same process (`boot`)
 * are totally ordered whatever path they took — SSE, a poll, a PUT response —
 * and a late replay (the multiplexed stream's last frame handed to a new
 * subscriber, a cross-tab buffer) is dropped. A different `boot` means the
 * server restarted and its counter began again, so the frame is taken as is.
 * Deliberately not ordered by the wall clock (`ts`) instead: a clock set back
 * across a restart would then reject every frame of the new process and
 * freeze the page, while taking an old process's replay costs one stale frame
 * until the next arrives. A frame without `boot`/`seq` (the minimal snapshot
 * the server sends when it cannot build one) is never stale.
 */
export function isStaleSnapshot(
  held: Pick<DocumentsSnapshot, 'boot' | 'seq'> | null | undefined,
  next: Pick<DocumentsSnapshot, 'boot' | 'seq'>,
): boolean {
  if (!held || !held.boot || !next.boot || held.boot !== next.boot) return false;
  return typeof held.seq === 'number' && typeof next.seq === 'number' && next.seq <= held.seq;
}

// ── settings ───────────────────────────────────────────────────────────────

export type ExcludeType = 'glob' | 'dir' | 'ext';
export type ExcludeMode = 'skip' | 'metadata_only';

export interface ExcludeRule {
  pattern: string;
  type: ExcludeType;
  mode: ExcludeMode;
}

export interface DocumentsOptions {
  caption: { enabled: boolean; daily_cap: number | null; min_px: number; min_kb: number };
  caption_consent: { at: number | null; provider: string; model: string } | null;
  identity: { author_names: string[]; emails: string[]; camera_devices: string[] };
  allow_in: { web_cli: boolean; channels: boolean; rooms: boolean };
  observer: { mode: 'auto' | 'native' | 'poll' };
  reconcile_interval_min: number;
  include_folders: string[];
}

export interface DocumentsSource {
  kind: 'local' | 'drive';
  enabled: boolean;
  /** The folder the index was built from (see `DocumentsPolicyView.working_dir`
   *  for the folder it follows). */
  root_path: string | null;
  excludes: ExcludeRule[];
  options: DocumentsOptions;
  first_sync_confirmed: boolean;
  updated_at: number | null;
}

export interface DocumentsPolicyView {
  allowed: boolean;
  effective: boolean;
  reason: 'admin_gate_off' | 'embedding_disabled' | null;
  is_admin: boolean;
  /** The folder that is indexed: the caller's own working directory. */
  working_dir: string | null;
  /** Only the admin changes a working directory (Settings → Profiles). */
  working_dir_editable: boolean;
  vision_daily_cap_default: number;
  max_file_mb: number;
}

export interface DocumentsDriveLink {
  linked: boolean;
  email?: string | null;
  /** The scopes include all of Drive (`drive` / `drive.readonly`): such an
   *  account must choose `include_folders` before Drive can be turned on. */
  whole_drive?: boolean;
  access_model?: string | null;
  /** The token predates the scopes Cremind now asks for — re-link. */
  scopes_stale?: boolean;
  /** The account the Drive index was built from; after a re-link to another
   *  address it differs from `email` until the engine has re-indexed. */
  identity_email?: string | null;
  /** The engine's Drive view as of the settings read (same shape as
   *  `snapshot.drive`); the live snapshot supersedes it. */
  index?: DocumentsDriveView | null;
}

/** Why images can't be sent to a vision model right now. The first four are
 *  fixed in Settings → LLM Providers → Specialized Vision Model. */
export type DocumentsVisionReason =
  | 'vision_disabled'
  | 'vision_model_unset'
  | 'vision_model_auth_incompatible'
  | 'vision_model_not_capable'
  | 'vision_model_error'
  | 'no_consent';

/** The profile's Specialized Vision Model as captioning sees it. Captions
 *  never fall back to the main model: no vision model means no captions. */
export interface DocumentsVisionView {
  /** A model is set and this profile consented to it. */
  ready: boolean;
  provider?: string | null;
  model?: string | null;
  reason: DocumentsVisionReason | null;
  consent?: boolean;
  /** What the recorded consent names — may be an older model. */
  consent_for?: { at: number | null; provider: string; model: string } | null;
  quota?: { day: string; used: number; ocr_pages: number; cap: number };
}

export interface DocumentsSettings {
  local: DocumentsSource;
  drive: DocumentsSource;
  policy_view: DocumentsPolicyView;
  drive_link: DocumentsDriveLink;
  vision?: DocumentsVisionView;
}

/** Options a settings PUT may change; each group merges key by key on the
 *  server. Vision consent is not here: only the `consent_vision` control
 *  action records it (and the server ignores it in a PUT). */
export interface DocumentsOptionsPatch {
  caption?: Partial<DocumentsOptions['caption']>;
  identity?: Partial<DocumentsOptions['identity']>;
  allow_in?: Partial<DocumentsOptions['allow_in']>;
  observer?: DocumentsOptions['observer'];
  reconcile_interval_min?: number;
  include_folders?: string[];
}

/** The fields a settings PUT may carry; only those present are changed.
 *  There is no folder: it is always the profile's working directory (the
 *  server refuses `root_path` / `root_mode` with `root_not_configurable`). */
export interface DocumentsSettingsPatch {
  kind: 'local' | 'drive';
  enabled?: boolean;
  excludes?: ExcludeRule[];
  options?: DocumentsOptionsPatch;
  /** Only valid while turning the source off. */
  delete_index?: boolean;
}

export interface DocumentsSettingsSaved {
  settings: DocumentsSettings;
  snapshot: DocumentsSnapshot;
}

// ── change plans ───────────────────────────────────────────────────────────

export interface PlanEffect {
  /** purge_out_of_scope | purge_all | purge_drive | reembed_all | reextract_all */
  kind: string;
  files: number;
  bytes: number;
  detail: Record<string, any>;
}

export interface ChangePlan {
  effects: PlanEffect[];
  destructive: boolean;
}

// ── admin gate ─────────────────────────────────────────────────────────────

export interface DocumentsAdminPolicy {
  allowed: boolean;
  storage_budget_mb: number;
  per_profile_budget_mb: number;
  vision_daily_cap_default: number;
  max_file_mb: number;
  workers: number;
  /** 0 = measure the disk. */
  vector_capacity_mb: number;
  /** 0 = measure the disk. */
  db_capacity_mb: number;
}

/** Same entry shape as the embedding PUT's 409, so its install dialog reuses. */
export interface DocumentsFeatureMissing {
  feature_key: string;
  extras: string[];
  requires_restart_after_install: boolean;
}

export interface DocumentsAdminView {
  policy: DocumentsAdminPolicy;
  effective: boolean;
  reason: 'admin_gate_off' | 'embedding_disabled' | null;
  embedding_enabled: boolean;
  feature: { missing: DocumentsFeatureMissing[] };
  /** Flags only — an admin overview never shows another profile's paths. */
  profiles: { profile: string; local_enabled?: boolean; drive_enabled?: boolean }[];
}

// ── engine-backed shapes ───────────────────────────────────────────────────

export type DocumentsControlAction =
  | 'start'
  | 'pause'
  | 'resume'
  | 'rescan'
  | 'sync_now'
  | 'reindex'
  | 'retry_failed'
  | 'rebuild'
  | 'confirm_deletions'
  | 'reject_deletions'
  | 'confirm_root_change'
  | 'consent_vision'
  | 'revoke_vision_consent';

export interface DocumentsControlRequest {
  action: DocumentsControlAction;
  /** Which source the action is for (rescan, sync_now, confirm_deletions,
   *  reject_deletions, retry_failed, reindex). The server defaults to local,
   *  so it is only sent for Drive. */
  source?: DocumentsSourceKind;
  /** File ids (8-character cite ids) or paths relative to the root (for
   *  Drive: display paths or Drive file ids). */
  targets?: string[];
  /** rebuild: also re-read every file, not just re-embed the stored text. */
  reextract?: boolean;
  /** consent_vision: the "provider/model" the user was shown. The server
   *  refuses (VisionModelChanged) if the model has changed since. */
  model?: string;
}

export interface DocumentsControlResult {
  accepted: boolean;
  snapshot: DocumentsSnapshot;
  /** reindex / retry_failed: how many files were queued. */
  files?: number;
}

export interface DocumentsFileRow {
  /** 8-character citation id — what Reindex/Retry target. */
  fid: string | null;
  id: number;
  rel_path: string;
  name: string;
  kind: string | null;
  status: DocumentsFileStatus;
  status_reason: string | null;
  size: number | null;
  /** epoch ms */
  modified: number | null;
  /** epoch ms */
  indexed_at: number | null;
  chunks: number | null;
  caption_state: string | null;
  /** `local` | `drive`. */
  source: string | null;
  /** Drive rows: the file's page in Google Drive. Put in an href only
   *  through `safeWebLink` (https only). */
  web_link: string | null;
}

export interface DocumentsFilesQuery {
  status?: string | null;
  kind?: string | null;
  source?: string | null;
  q?: string | null;
  after?: { rel_path: string; id: number } | null;
  limit?: number;
}

export interface DocumentsFilesPage {
  files: DocumentsFileRow[];
  next: { rel_path: string; id: number } | null;
  counts: Record<string, number>;
}

export interface DocumentsFileDetail extends DocumentsFileRow {
  doc_meta: Record<string, any> | null;
  exif: Record<string, any> | null;
  error: string | null;
  attempts: number | null;
  first_seen_at: number | null;
  taken_at: number | null;
  doc_created_at: number | null;
}

export interface DocumentsActivityEvent {
  id: number;
  /** epoch ms */
  ts: number | null;
  source: string | null;
  level: 'info' | 'warning' | 'error' | string;
  kind: string;
  file_id: number | null;
  rel_path: string | null;
  message: string;
  detail: Record<string, any> | null;
}

export interface DocumentsStorageInfo {
  index_bytes: number;
  vector_bytes_est: number;
  total_bytes: number;
  level: StorageLevel;
  message: string | null;
  budget_bytes: number | null;
  profile_budget_bytes: number | null;
  free_bytes: number | null;
  disk_total_bytes: number | null;
  method: string | null;
}

// ── errors ─────────────────────────────────────────────────────────────────

/**
 * A non-2xx answer from `/api/documentation-search/*`, with the server's structure kept.
 * `code` is the body's `error` field (`ValidationFailed`,
 * `ConfirmationRequired`, `FeatureNotInstalled`, `FeatureDisabledByAdmin`,
 * `EmbeddingDisabled`, `DriveNotLinked`, `DriveFoldersRequired`,
 * `DriveUnreachable`, `NotEnabled`, `EngineNotRunning`, `NotFound`, …).
 */
export class DocumentsApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  /** ValidationFailed: field → message. */
  readonly details: Record<string, string> | null;
  /** ConfirmationRequired: what the change would do. */
  readonly plan: ChangePlan | null;
  /** ConfirmationRequired: the token that confirms exactly this change. */
  readonly confirm: string | null;
  /** FeatureNotInstalled: the extras to install. */
  readonly missing: DocumentsFeatureMissing[] | null;
  readonly body: Record<string, any>;

  constructor(status: number, body: Record<string, any>, fallback: string) {
    const details = body.details && typeof body.details === 'object'
      ? body.details as Record<string, string>
      : null;
    const message = typeof body.message === 'string' && body.message
      ? body.message
      : details
        ? Object.values(details).join('; ')
        : typeof body.error === 'string' && body.error
          ? body.error
          : fallback;
    super(message);
    this.name = 'DocumentsApiError';
    this.status = status;
    this.code = typeof body.error === 'string' ? body.error : null;
    this.details = details;
    this.plan = body.plan && Array.isArray(body.plan.effects) ? body.plan as ChangePlan : null;
    this.confirm = typeof body.confirm === 'string' ? body.confirm : null;
    this.missing = Array.isArray(body.missing) ? body.missing as DocumentsFeatureMissing[] : null;
    this.body = body;
  }
}

async function request<T>(
  agentUrl: string,
  token: string,
  path: string,
  init: { method?: string; body?: unknown } = {},
): Promise<T> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}${path}`, {
    method: init.method ?? 'GET',
    headers: authHeaders(token),
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new DocumentsApiError(
      res.status,
      body && typeof body === 'object' ? body : {},
      `Request failed: ${res.status} ${res.statusText}`,
    );
  }
  return res.json() as Promise<T>;
}

// ── the confirm round trip ─────────────────────────────────────────────────

export type ConfirmOutcome<T> =
  | { kind: 'done'; result: T }
  | {
    kind: 'confirm';
    plan: ChangePlan;
    /** The server's explanation of why it asks. */
    message: string;
    token: string;
    /** Repeat the same request with the token. The server may answer with a
     *  new plan (the change grew while the dialog was open), so this resolves
     *  to another outcome rather than straight to the result. */
    confirm: () => Promise<ConfirmOutcome<T>>;
  };

/**
 * Run `send` and turn a `409 ConfirmationRequired` into a `confirm` outcome.
 * `send` receives the confirmation token on the repeat (undefined the first
 * time) and must send the otherwise identical request — the token is bound to
 * the exact change, so a different body would just be asked about again.
 * Every other error is thrown unchanged.
 */
export async function sendWithConfirm<T>(
  send: (confirm?: string) => Promise<T>,
  confirm?: string,
): Promise<ConfirmOutcome<T>> {
  try {
    return { kind: 'done', result: await send(confirm) };
  } catch (e) {
    if (e instanceof DocumentsApiError && e.code === 'ConfirmationRequired' && e.plan && e.confirm) {
      const token = e.confirm;
      return {
        kind: 'confirm',
        plan: e.plan,
        message: e.message,
        token,
        confirm: () => sendWithConfirm(send, token),
      };
    }
    throw e;
  }
}

// ── routes ─────────────────────────────────────────────────────────────────

export function getDocumentsStatus(agentUrl: string, token: string): Promise<DocumentsSnapshot> {
  return request(agentUrl, token, '/api/documentation-search/status');
}

export function getDocumentsSettings(agentUrl: string, token: string): Promise<DocumentsSettings> {
  return request(agentUrl, token, '/api/documentation-search/settings');
}

/** One PUT /settings. Prefer `saveDocumentsSettings`, which handles the plan. */
export function putDocumentsSettings(
  agentUrl: string,
  token: string,
  patch: DocumentsSettingsPatch & { confirm?: string },
): Promise<DocumentsSettingsSaved> {
  return request(agentUrl, token, '/api/documentation-search/settings', { method: 'PUT', body: patch });
}

export function saveDocumentsSettings(
  agentUrl: string,
  token: string,
  patch: DocumentsSettingsPatch,
): Promise<ConfirmOutcome<DocumentsSettingsSaved>> {
  return sendWithConfirm(confirm => putDocumentsSettings(
    agentUrl, token, confirm ? { ...patch, confirm } : patch,
  ));
}

export interface DriveFolder {
  id: string;
  name: string;
}

export interface DriveFolderListing {
  folders: DriveFolder[];
  /** The folder listed; null at the top (My Drive for a whole-Drive account,
   *  the granted folders for a per-file one). */
  parent: DriveFolder | null;
  whole_drive: boolean;
}

/**
 * One level of the linked Google Drive's folders, for the `include_folders`
 * picker. `parent: null` lists the top. Throws `DocumentsApiError` —
 * `DriveNotLinked` (409) or `DriveUnreachable` (503).
 */
export function listDriveFolders(
  agentUrl: string,
  token: string,
  parent: string | null,
): Promise<DriveFolderListing> {
  const qs = parent ? `?parent=${encodeURIComponent(parent)}` : '';
  return request(agentUrl, token, `/api/documentation-search/drive/folders${qs}`);
}

export function getDocumentsAdmin(agentUrl: string, token: string): Promise<DocumentsAdminView> {
  return request(agentUrl, token, '/api/documentation-search/admin');
}

/** Write part of the admin gate. Throws `DocumentsApiError` — `code`
 *  `FeatureNotInstalled` carries `missing` for the install dialog. */
export function putDocumentsAdmin(
  agentUrl: string,
  token: string,
  policy: Partial<DocumentsAdminPolicy>,
): Promise<DocumentsAdminView> {
  return request(agentUrl, token, '/api/documentation-search/admin', { method: 'PUT', body: { policy } });
}

/** One POST /control. `rebuild` asks for confirmation — use `runDocumentsControl`. */
export function controlDocuments(
  agentUrl: string,
  token: string,
  req: DocumentsControlRequest & { confirm?: string },
): Promise<DocumentsControlResult> {
  return request(agentUrl, token, '/api/documentation-search/control', { method: 'POST', body: req });
}

export function runDocumentsControl(
  agentUrl: string,
  token: string,
  req: DocumentsControlRequest,
): Promise<ConfirmOutcome<DocumentsControlResult>> {
  return sendWithConfirm(confirm => controlDocuments(
    agentUrl, token, confirm ? { ...req, confirm } : req,
  ));
}

export function listDocumentsFiles(
  agentUrl: string,
  token: string,
  query: DocumentsFilesQuery = {},
): Promise<DocumentsFilesPage> {
  const params = new URLSearchParams();
  if (query.status) params.set('status', query.status);
  if (query.kind) params.set('kind', query.kind);
  if (query.source) params.set('source', query.source);
  if (query.q) params.set('q', query.q);
  if (query.after) {
    params.set('after_path', query.after.rel_path);
    params.set('after_id', String(query.after.id));
  }
  if (query.limit) params.set('limit', String(query.limit));
  const qs = params.toString();
  return request(agentUrl, token, `/api/documentation-search/files${qs ? `?${qs}` : ''}`);
}

export function getDocumentsFile(
  agentUrl: string,
  token: string,
  fid: string,
): Promise<DocumentsFileDetail> {
  return request(agentUrl, token, `/api/documentation-search/files/${encodeURIComponent(fid)}`);
}

/** Newest first; page older with `before` = the last event id seen. */
export function listDocumentsActivity(
  agentUrl: string,
  token: string,
  opts: { before?: number | null; limit?: number } = {},
): Promise<{ events: DocumentsActivityEvent[] }> {
  const params = new URLSearchParams();
  if (opts.before != null) params.set('before', String(opts.before));
  if (opts.limit) params.set('limit', String(opts.limit));
  const qs = params.toString();
  return request(agentUrl, token, `/api/documentation-search/activity${qs ? `?${qs}` : ''}`);
}

export function getDocumentsEstimate(agentUrl: string, token: string): Promise<DocumentsEstimate> {
  return request(agentUrl, token, '/api/documentation-search/estimate');
}

export function startDocumentsEstimate(
  agentUrl: string,
  token: string,
): Promise<{ state: 'running' }> {
  return request(agentUrl, token, '/api/documentation-search/estimate', { method: 'POST' });
}

export function getDocumentsStorage(agentUrl: string, token: string): Promise<DocumentsStorageInfo> {
  return request(agentUrl, token, '/api/documentation-search/storage');
}
