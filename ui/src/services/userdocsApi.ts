/**
 * Typed client for User Document Search (`/api/userdocs/*`).
 *
 * Backs Settings → My Documents, the NavRail sync chip and the admin gate card
 * on the Vector Embedding page. Every route acts on the caller's own profile —
 * the server takes it from the Bearer token, never from a body or query field —
 * so nothing here names a profile.
 *
 * Errors keep the server's machine-readable shape (`UserDocsApiError.code`,
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
export type UserDocsState =
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
export type UserDocsToolMode = 'hidden' | 'normal' | 'lexical_only' | 'partial';

export type RootMode = 'inherit' | 'custom';

export type StorageLevel = 'ok' | 'warn' | 'budget' | 'disk_low' | 'disk_critical';

/** Per-file index status (the `files.status` column of the index). */
export type UserDocsFileStatus =
  | 'dirty'
  | 'indexed'
  | 'metadata_only'
  | 'awaiting_extractor'
  | 'deferred'
  | 'error'
  | 'missing'
  | 'tombstone';

export interface UserDocsSourceState {
  enabled: boolean;
  root_mode: RootMode | null;
  root: string | null;
  first_sync_confirmed: boolean;
}

export interface UserDocsBatch {
  label: string | null;
  total: number;
  done: number;
  failed: number;
  skipped: number;
  eta_s?: number | null;
  rate_per_min?: number | null;
}

export interface UserDocsCurrentFile {
  name: string;
  rel_path: string;
  /** read | hash | extract | chunk | index */
  stage: string;
  /** Only while indexing: chunks written so far. */
  progress: { done: number; total: number } | null;
  started_at: number;
}

export interface UserDocsRecentItem {
  ts: number;
  /** added | updated | moved | removed | metadata_only | failed | skipped */
  kind: string;
  name: string;
  rel_path: string;
  message: string;
}

export interface UserDocsFailedItem {
  fid: string | null;
  name: string;
  rel_path: string;
  reason: string | null;
  message: string;
}

export interface UserDocsStorageSnapshot {
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

export interface UserDocsEstimate {
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

export type UserDocsConfirmation =
  | { kind: 'first_sync'; estimate: UserDocsEstimate }
  | { kind: 'mass_delete'; missing: number; total: number }
  | { kind: 'root_change'; from: string | null; to: string | null; files: number };

/** Where an indexed file comes from. */
export type UserDocsSourceKind = 'local' | 'drive';

/**
 * Why the Drive source is held (`source_state('drive')`). Revoked and
 * unlinked hide Drive from search and remove its index at `detail.purge_at`;
 * unreachable and misconfigured keep the index searchable (possibly stale)
 * and never remove anything.
 */
export type UserDocsDriveHoldReason =
  | 'auth_revoked'
  | 'drive_unlinked'
  | 'drive_unreachable'
  | 'drive_misconfigured';

/** A folder in `include_folders`, as the Drive view reports it: a bare
 *  Drive folder id, or one the server resolved to a name. */
export type UserDocsDriveFolderRef = string | { id: string; name?: string | null; path?: string | null };

/**
 * The Drive half of the snapshot (`snapshot.drive`, the runtime's
 * `DriveSource.view()`). It is reported beside the top-level state rather
 * than folded into it, because that state belongs to the local folder: a
 * Drive hold must not hide a local one, nor the reverse.
 */
export interface UserDocsDriveView {
  enabled: boolean;
  /** live | hold | disabled */
  state: 'live' | 'hold' | 'disabled' | string;
  reason: UserDocsDriveHoldReason | string | null;
  /** Hold details: `purge_at` (epoch ms, revoked / unlinked only), `message`. */
  detail?: Record<string, any> | null;
  /** The Google account the index was built from. */
  account_email?: string | null;
  identity_set?: boolean;
  whole_drive?: boolean;
  include_folders?: UserDocsDriveFolderRef[];
  /** Last change-feed poll and last full reconcile (epoch s or ms). */
  last_sync_at?: number | null;
  last_full_at?: number | null;
  counts?: { indexed?: number; pending?: number; error?: number; metadata_only?: number };
  /** A held mass removal, confirmed with `source: 'drive'`. */
  confirmation?: UserDocsConfirmation | null;
}

/**
 * The one snapshot every surface reads (`app/userdocs/state.py`). Each frame
 * replaces the previous one outright. Everything after `sources` is only
 * present while the sync engine runs for the profile.
 */
export interface UserDocsSnapshot {
  v: number;
  /** Server process id; with `seq` it orders frames (see `isStaleSnapshot`). */
  boot?: string;
  seq?: number;
  /** Server wall clock, epoch ms. */
  ts?: number;
  allowed?: boolean;
  enabled: boolean;
  state: UserDocsState;
  reason: string | null;
  tool_mode?: UserDocsToolMode;
  sources?: { local: UserDocsSourceState | null; drive: UserDocsSourceState | null };
  /** Hold details: root_invalid {code, message, root}, root_unavailable
   *  {why, …docker status}, pending_root_change {from, to}. */
  detail?: Record<string, any> | null;
  phase?: string | null;
  batch?: UserDocsBatch;
  /** File counts by `UserDocsFileStatus`. */
  stages?: Record<string, number>;
  current?: UserDocsCurrentFile[];
  queue_preview?: string[];
  recent?: UserDocsRecentItem[];
  failed_preview?: UserDocsFailedItem[];
  vision?: Record<string, unknown>;
  storage?: UserDocsStorageSnapshot;
  reembed?: { done: number; total: number };
  vector_coverage_pct?: number | null;
  confirmation?: UserDocsConfirmation | null;
  engine_sources?: Record<string, { root?: string; watch?: string | null; watch_reason?: string | null }>;
  watch?: { mode: string | null; reason: string | null };
  /** Google Drive, reported separately from the local folder's state. */
  drive?: UserDocsDriveView | null;
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
  held: Pick<UserDocsSnapshot, 'boot' | 'seq'> | null | undefined,
  next: Pick<UserDocsSnapshot, 'boot' | 'seq'>,
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

export interface UserDocsOptions {
  caption: { enabled: boolean; daily_cap: number | null; min_px: number; min_kb: number };
  caption_consent: { at: number | null; provider: string; model: string } | null;
  identity: { author_names: string[]; emails: string[]; camera_devices: string[] };
  allow_in: { web_cli: boolean; channels: boolean; rooms: boolean };
  observer: { mode: 'auto' | 'native' | 'poll' };
  reconcile_interval_min: number;
  include_folders: string[];
}

export interface UserDocsSource {
  kind: 'local' | 'drive';
  enabled: boolean;
  root_mode: RootMode;
  root_path: string | null;
  excludes: ExcludeRule[];
  options: UserDocsOptions;
  first_sync_confirmed: boolean;
  updated_at: number | null;
}

export interface UserDocsPolicyView {
  allowed: boolean;
  effective: boolean;
  reason: 'admin_gate_off' | 'embedding_disabled' | null;
  is_admin: boolean;
  working_dir: string;
  /** Non-admin profiles may only index inside the working directory. */
  root_constraint: 'any' | 'working_dir';
  vision_daily_cap_default: number;
  max_file_mb: number;
}

export interface UserDocsDriveLink {
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
  index?: UserDocsDriveView | null;
}

/** Why images can't be sent to a vision model right now. The first four are
 *  fixed in Settings → LLM Providers → Specialized Vision Model. */
export type UserDocsVisionReason =
  | 'vision_disabled'
  | 'vision_model_unset'
  | 'vision_model_auth_incompatible'
  | 'vision_model_not_capable'
  | 'vision_model_error'
  | 'no_consent';

/** The profile's Specialized Vision Model as captioning sees it. Captions
 *  never fall back to the main model: no vision model means no captions. */
export interface UserDocsVisionView {
  /** A model is set and this profile consented to it. */
  ready: boolean;
  provider?: string | null;
  model?: string | null;
  reason: UserDocsVisionReason | null;
  consent?: boolean;
  /** What the recorded consent names — may be an older model. */
  consent_for?: { at: number | null; provider: string; model: string } | null;
  quota?: { day: string; used: number; ocr_pages: number; cap: number };
}

export interface UserDocsSettings {
  local: UserDocsSource;
  drive: UserDocsSource;
  policy_view: UserDocsPolicyView;
  drive_link: UserDocsDriveLink;
  vision?: UserDocsVisionView;
}

/** Options a settings PUT may change; each group merges key by key on the
 *  server. Vision consent is not here: only the `consent_vision` control
 *  action records it (and the server ignores it in a PUT). */
export interface UserDocsOptionsPatch {
  caption?: Partial<UserDocsOptions['caption']>;
  identity?: Partial<UserDocsOptions['identity']>;
  allow_in?: Partial<UserDocsOptions['allow_in']>;
  observer?: UserDocsOptions['observer'];
  reconcile_interval_min?: number;
  include_folders?: string[];
}

/** The fields a settings PUT may carry; only those present are changed. */
export interface UserDocsSettingsPatch {
  kind: 'local' | 'drive';
  enabled?: boolean;
  root_mode?: RootMode;
  root_path?: string | null;
  excludes?: ExcludeRule[];
  options?: UserDocsOptionsPatch;
  /** Only valid while turning the source off. */
  delete_index?: boolean;
}

export interface UserDocsSettingsSaved {
  settings: UserDocsSettings;
  snapshot: UserDocsSnapshot;
}

export interface RootCheck {
  ok: boolean;
  path: string | null;
  /** not_found | not_directory | not_readable | inside_system_dir |
   *  forbidden_system_path | outside_working_dir */
  code: string | null;
  message: string | null;
  /** Paths under the root that are never indexed (Cremind's system folder). */
  locked_excludes: string[];
}

export interface BrowseResult {
  path: string;
  parent: string | null;
  entries: { name: string; path: string }[];
  truncated: boolean;
  /** Quick-jump roots: the working directory, plus home for the admin. */
  roots: string[];
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

export interface UserDocsAdminPolicy {
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
export interface UserDocsFeatureMissing {
  feature_key: string;
  extras: string[];
  requires_restart_after_install: boolean;
}

export interface UserDocsAdminView {
  policy: UserDocsAdminPolicy;
  effective: boolean;
  reason: 'admin_gate_off' | 'embedding_disabled' | null;
  embedding_enabled: boolean;
  feature: { missing: UserDocsFeatureMissing[] };
  /** Flags only — an admin overview never shows another profile's paths. */
  profiles: { profile: string; local_enabled?: boolean; drive_enabled?: boolean }[];
}

// ── engine-backed shapes ───────────────────────────────────────────────────

export type UserDocsControlAction =
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

export interface UserDocsControlRequest {
  action: UserDocsControlAction;
  /** Which source the action is for (rescan, sync_now, confirm_deletions,
   *  reject_deletions, retry_failed, reindex). The server defaults to local,
   *  so it is only sent for Drive. */
  source?: UserDocsSourceKind;
  /** File ids (8-character cite ids) or paths relative to the root (for
   *  Drive: display paths or Drive file ids). */
  targets?: string[];
  /** rebuild: also re-read every file, not just re-embed the stored text. */
  reextract?: boolean;
  /** consent_vision: the "provider/model" the user was shown. The server
   *  refuses (VisionModelChanged) if the model has changed since. */
  model?: string;
}

export interface UserDocsControlResult {
  accepted: boolean;
  snapshot: UserDocsSnapshot;
  /** reindex / retry_failed: how many files were queued. */
  files?: number;
}

export interface UserDocsFileRow {
  /** 8-character citation id — what Reindex/Retry target. */
  fid: string | null;
  id: number;
  rel_path: string;
  name: string;
  kind: string | null;
  status: UserDocsFileStatus;
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

export interface UserDocsFilesQuery {
  status?: string | null;
  kind?: string | null;
  source?: string | null;
  q?: string | null;
  after?: { rel_path: string; id: number } | null;
  limit?: number;
}

export interface UserDocsFilesPage {
  files: UserDocsFileRow[];
  next: { rel_path: string; id: number } | null;
  counts: Record<string, number>;
}

export interface UserDocsFileDetail extends UserDocsFileRow {
  doc_meta: Record<string, any> | null;
  exif: Record<string, any> | null;
  error: string | null;
  attempts: number | null;
  first_seen_at: number | null;
  taken_at: number | null;
  doc_created_at: number | null;
}

export interface UserDocsActivityEvent {
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

export interface UserDocsStorageInfo {
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
 * A non-2xx answer from `/api/userdocs/*`, with the server's structure kept.
 * `code` is the body's `error` field (`ValidationFailed`,
 * `ConfirmationRequired`, `FeatureNotInstalled`, `FeatureDisabledByAdmin`,
 * `EmbeddingDisabled`, `DriveNotLinked`, `DriveFoldersRequired`,
 * `DriveUnreachable`, `NotEnabled`, `EngineNotRunning`, `NotFound`, …).
 */
export class UserDocsApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  /** ValidationFailed: field → message. */
  readonly details: Record<string, string> | null;
  /** ConfirmationRequired: what the change would do. */
  readonly plan: ChangePlan | null;
  /** ConfirmationRequired: the token that confirms exactly this change. */
  readonly confirm: string | null;
  /** FeatureNotInstalled: the extras to install. */
  readonly missing: UserDocsFeatureMissing[] | null;
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
    this.name = 'UserDocsApiError';
    this.status = status;
    this.code = typeof body.error === 'string' ? body.error : null;
    this.details = details;
    this.plan = body.plan && Array.isArray(body.plan.effects) ? body.plan as ChangePlan : null;
    this.confirm = typeof body.confirm === 'string' ? body.confirm : null;
    this.missing = Array.isArray(body.missing) ? body.missing as UserDocsFeatureMissing[] : null;
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
    throw new UserDocsApiError(
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
    if (e instanceof UserDocsApiError && e.code === 'ConfirmationRequired' && e.plan && e.confirm) {
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

export function getUserDocsStatus(agentUrl: string, token: string): Promise<UserDocsSnapshot> {
  return request(agentUrl, token, '/api/userdocs/status');
}

export function getUserDocsSettings(agentUrl: string, token: string): Promise<UserDocsSettings> {
  return request(agentUrl, token, '/api/userdocs/settings');
}

/** One PUT /settings. Prefer `saveUserDocsSettings`, which handles the plan. */
export function putUserDocsSettings(
  agentUrl: string,
  token: string,
  patch: UserDocsSettingsPatch & { confirm?: string },
): Promise<UserDocsSettingsSaved> {
  return request(agentUrl, token, '/api/userdocs/settings', { method: 'PUT', body: patch });
}

export function saveUserDocsSettings(
  agentUrl: string,
  token: string,
  patch: UserDocsSettingsPatch,
): Promise<ConfirmOutcome<UserDocsSettingsSaved>> {
  return sendWithConfirm(confirm => putUserDocsSettings(
    agentUrl, token, confirm ? { ...patch, confirm } : patch,
  ));
}

/** Would this folder be accepted? `path: null` asks about the inherited root. */
export function validateUserDocsRoot(
  agentUrl: string,
  token: string,
  path: string | null,
): Promise<RootCheck> {
  const body = path === null ? { root_mode: 'inherit' } : { path };
  return request(agentUrl, token, '/api/userdocs/validate-root', { method: 'POST', body });
}

export function browseUserDocsFolders(
  agentUrl: string,
  token: string,
  path: string | null,
  hidden = false,
): Promise<BrowseResult> {
  const params = new URLSearchParams();
  if (path) params.set('path', path);
  if (hidden) params.set('hidden', '1');
  const qs = params.toString();
  return request(agentUrl, token, `/api/userdocs/browse${qs ? `?${qs}` : ''}`);
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
 * picker. `parent: null` lists the top. Throws `UserDocsApiError` —
 * `DriveNotLinked` (409) or `DriveUnreachable` (503).
 */
export function listDriveFolders(
  agentUrl: string,
  token: string,
  parent: string | null,
): Promise<DriveFolderListing> {
  const qs = parent ? `?parent=${encodeURIComponent(parent)}` : '';
  return request(agentUrl, token, `/api/userdocs/drive/folders${qs}`);
}

export function getUserDocsAdmin(agentUrl: string, token: string): Promise<UserDocsAdminView> {
  return request(agentUrl, token, '/api/userdocs/admin');
}

/** Write part of the admin gate. Throws `UserDocsApiError` — `code`
 *  `FeatureNotInstalled` carries `missing` for the install dialog. */
export function putUserDocsAdmin(
  agentUrl: string,
  token: string,
  policy: Partial<UserDocsAdminPolicy>,
): Promise<UserDocsAdminView> {
  return request(agentUrl, token, '/api/userdocs/admin', { method: 'PUT', body: { policy } });
}

/** One POST /control. `rebuild` asks for confirmation — use `runUserDocsControl`. */
export function controlUserDocs(
  agentUrl: string,
  token: string,
  req: UserDocsControlRequest & { confirm?: string },
): Promise<UserDocsControlResult> {
  return request(agentUrl, token, '/api/userdocs/control', { method: 'POST', body: req });
}

export function runUserDocsControl(
  agentUrl: string,
  token: string,
  req: UserDocsControlRequest,
): Promise<ConfirmOutcome<UserDocsControlResult>> {
  return sendWithConfirm(confirm => controlUserDocs(
    agentUrl, token, confirm ? { ...req, confirm } : req,
  ));
}

export function listUserDocsFiles(
  agentUrl: string,
  token: string,
  query: UserDocsFilesQuery = {},
): Promise<UserDocsFilesPage> {
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
  return request(agentUrl, token, `/api/userdocs/files${qs ? `?${qs}` : ''}`);
}

export function getUserDocsFile(
  agentUrl: string,
  token: string,
  fid: string,
): Promise<UserDocsFileDetail> {
  return request(agentUrl, token, `/api/userdocs/files/${encodeURIComponent(fid)}`);
}

/** Newest first; page older with `before` = the last event id seen. */
export function listUserDocsActivity(
  agentUrl: string,
  token: string,
  opts: { before?: number | null; limit?: number } = {},
): Promise<{ events: UserDocsActivityEvent[] }> {
  const params = new URLSearchParams();
  if (opts.before != null) params.set('before', String(opts.before));
  if (opts.limit) params.set('limit', String(opts.limit));
  const qs = params.toString();
  return request(agentUrl, token, `/api/userdocs/activity${qs ? `?${qs}` : ''}`);
}

export function getUserDocsEstimate(agentUrl: string, token: string): Promise<UserDocsEstimate> {
  return request(agentUrl, token, '/api/userdocs/estimate');
}

export function startUserDocsEstimate(
  agentUrl: string,
  token: string,
): Promise<{ state: 'running' }> {
  return request(agentUrl, token, '/api/userdocs/estimate', { method: 'POST' });
}

export function getUserDocsStorage(agentUrl: string, token: string): Promise<UserDocsStorageInfo> {
  return request(agentUrl, token, '/api/userdocs/storage');
}
