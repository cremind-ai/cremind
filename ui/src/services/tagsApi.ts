// Typed client for Cremind Tag (e-paper companion screens).
//
//   /api/tags/*           the calling profile's own tags (app/api/tags.py)
//   /api/tags/hardware/*  admin only: companions, inventory, ownership (app/api/tags_hardware.py)
//
// Timestamps are epoch **milliseconds** everywhere in these payloads. Errors are
// `{"error": <code>, "message": <sentence>, "detail": <same>}`; they surface as
// `TagsApiError` so a page can branch on the code (`otp_refused`,
// `clear_pending`, `bridge_required`, `already_terminal`, …). Another
// profile's device or delivery id answers 404, never 403.
//
// The CLI counterpart is `cremind tags …` / `cremind tags hardware …`.

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(authToken: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
  return headers;
}

// ── shapes ─────────────────────────────────────────────────────────────────

export type TagDeviceKind = 'gateway' | 'bridge' | 'tag';
/** `unclaimed` | `assigning` | `ok` | `pending` | `offline` | `error` |
 *  `clear_failed` (its clear failed or expired 3 times: an admin must claim or
 *  release it again) — an open set. */
export type TagDeviceStatus = string;

export interface TagDevice {
  id: string;
  companion_id: string;
  kind: TagDeviceKind;
  hw_id: string;
  name: string;
  owner_profile: string | null;
  bridge_device_id: string | null;
  epoch: number;
  rotation: number;
  board: number | null;
  panel: number | null;
  width: number | null;
  height: number | null;
  planes: number | null;
  fw: string | null;
  info: Record<string, any>;
  status: TagDeviceStatus;
  battery_mv: number | null;
  rssi: number | null;
  last_contact_at: number | null;
  desired_revision: number;
  displayed_revision: number;
  displayed_digest: string | null;
  clear_required: boolean;
  claimed_at: number | null;
  created_at: number;
  updated_at: number;
  /** Overview / detail only: the revision of the stored preview per kind.
   *  A change of owner drops them (and resets the name and revisions). */
  previews?: { desired: number | null; displayed: number | null };
  /** Overview only: deliveries still on their way to this tag. */
  pending_count?: number;
  /** Overview only. */
  companion_name?: string | null;
  companion_online?: boolean;
}

/** The card a delivery carries (connector-api.md "Job shape"). */
export interface TagCard {
  v?: number;
  kind?: string;
  severity?: string;
  icon?: string;
  title?: string;
  body?: string | null;
  lang?: string;
  ts?: string;
  progress?: any;
  link?: string | null;
  source?: { type?: string; id?: string | null } | null;
  [key: string]: any;
}

export interface TagDeliveryTiming {
  wake_ms?: number;
  mesh_ms?: number;
  transfer_ms?: number;
  refresh_ms?: number;
  [key: string]: any;
}

export interface TagDelivery {
  id: number;
  seq: number;
  profile: string;
  device_id: string;
  companion_id: string | null;
  epoch: number;
  event_id: string | null;
  kind: string;
  priority: number;
  replace_key: string | null;
  resolves: string | null;
  card: TagCard | null;
  stage: string;
  terminal: boolean;
  outcome: string | null;
  status_code: number | null;
  revision: number | null;
  digest: string | null;
  detail: string | null;
  timing: TagDeliveryTiming | null;
  /** stage → epoch ms it was reached. */
  stage_times: Record<string, number>;
  created_at: number;
  updated_at: number;
  expires_at: number;
  finished_at: number | null;
}

export interface TagCommand {
  id: string;
  companion_id: string;
  kind: string;
  args: Record<string, any>;
  requested_by: string;
  /** `queued` | `claimed` | `succeeded` | `failed` | `expired` | `cancelled`. */
  status: string;
  result: any;
  error: string | null;
  created_at: number;
  claimed_at: number | null;
  completed_at: number | null;
  expires_at: number;
}

export interface TagCredential {
  id: string;
  companion_id: string;
  kind: 'hardware' | 'content' | string;
  profile: string | null;
  label: string;
  created_by: string;
  created_at: number;
  last_used_at: number | null;
  revoked_at: number | null;
  revoked: boolean;
}

/** What every profile may see of a companion (to create a content credential). */
export interface TagCompanionSummary {
  id: string;
  name: string;
  online: boolean;
  last_seen_at: number | null;
  version: string | null;
}

/** The admin's view of a companion. */
export interface TagCompanion extends TagCompanionSummary {
  created_by: string;
  created_at: number;
  updated_at: number;
  host: string | null;
  heartbeat: Record<string, any> | null;
  /** Its hardware credentials (live and revoked). */
  credentials?: TagCredential[];
}

/** `"all"` every tag the profile owns, `"none"`, or specific device ids. */
export type TagRoute = 'all' | 'none' | string[];

export interface TagOptions {
  layout?: string;
  show_excerpts?: boolean;
  qr_links?: boolean;
  progress_cadence_s?: number;
  language?: string;
  /** `""` = the profile's own Cremind timezone. */
  timezone?: string;
  routes?: Record<string, TagRoute>;
}

export interface TagEffectiveOptions {
  layout: string;
  show_excerpts: boolean;
  qr_links: boolean;
  progress_cadence_s: number;
  language: string;
  timezone: string;
  routes: Record<string, TagRoute>;
}

/** A partial options write (PATCH): a key given replaces, `null` inherits
 *  again; `routes` merges per card kind the same way. */
export type TagOptionsPatch = {
  [K in Exclude<keyof TagOptions, 'routes'>]?: TagOptions[K] | null;
} & { routes?: Record<string, TagRoute | null> | null };

export interface TagSettings {
  profile: string;
  enabled: boolean;
  /** This profile's own overrides (only the keys it set). */
  options: TagOptions;
  /** The admin's defaults (only the keys the admin set). */
  defaults: TagOptions;
  /** The built-in layer under the admin defaults. */
  builtin: TagEffectiveOptions;
  effective: TagEffectiveOptions;
  /** The IANA timezone the companion renders times in (resolved). */
  timezone: string;
  updated_at: number | null;
  routable_kinds: string[];
  layouts: string[];
  icons: string[];
  is_admin: boolean;
}

export interface TagCounts {
  devices: number;
  active_deliveries: number;
  needs_input: number;
  failed_24h: number;
}

export interface TagOverview {
  profile: string;
  enabled: boolean;
  devices: TagDevice[];
  counts: TagCounts;
}

export interface TagSecretPayload {
  credential: TagCredential;
  /** Shown once; Cremind keeps only a hash. */
  secret: string;
  /** The full `Authorization` header value the companion sends. */
  authorization: string;
}

export interface TagHardware {
  companions: TagCompanion[];
  devices: TagDevice[];
  commands: TagCommand[];
}

export interface DisplayNotePayload {
  title: string;
  body?: string;
  icon?: string;
  ttl_s?: number;
  /** false (default): the note is a card of its own. true: it takes the tag's
   *  one replaceable note slot, retiring the previous replace-note. */
  replace?: boolean;
}

export interface DeliveryQuery {
  device?: string;
  /** `active` | `terminal` | a stage name. */
  state?: string;
  limit?: number;
  before?: number | null;
}

export interface DeliveryPage {
  deliveries: TagDelivery[];
  next_before: number | null;
}

export interface TagPreview {
  blob: Blob;
  /** From `X-Tag-Revision` (CORS-exposed); null if a proxy strips it. */
  revision: number | null;
}

// ── errors ─────────────────────────────────────────────────────────────────

/** A non-2xx answer from `/api/tags/*`, with the server's code kept. */
export class TagsApiError extends Error {
  readonly status: number;
  /** The body's `error` field (`otp_refused`, `clear_pending`, `bridge_required`, …). */
  readonly code: string | null;
  /** `invalid_settings`: field → message. */
  readonly details: Record<string, string> | null;
  readonly body: Record<string, any>;

  constructor(status: number, body: Record<string, any>, fallback: string) {
    const details = body.details && typeof body.details === 'object'
      ? body.details as Record<string, string>
      : null;
    const message = typeof body.message === 'string' && body.message
      ? body.message
      : typeof body.detail === 'string' && body.detail
        ? body.detail
        : typeof body.error === 'string' && body.error
          ? body.error
          : fallback;
    super(message);
    this.name = 'TagsApiError';
    this.status = status;
    this.code = typeof body.error === 'string' ? body.error : null;
    this.details = details;
    this.body = body;
  }
}

/** The error's code when it is a TagsApiError, else null. */
export function tagsErrorCode(e: unknown): string | null {
  return e instanceof TagsApiError ? e.code : null;
}

async function failure(res: Response): Promise<TagsApiError> {
  const body = await res.json().catch(() => ({}));
  return new TagsApiError(
    res.status,
    body && typeof body === 'object' && !Array.isArray(body) ? body : {},
    `Request failed: ${res.status} ${res.statusText}`,
  );
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
  if (!res.ok) throw await failure(res);
  return res.json() as Promise<T>;
}

const enc = encodeURIComponent;

// ── the profile's own tags ─────────────────────────────────────────────────

export function getTagsOverview(agentUrl: string, token: string): Promise<TagOverview> {
  return request(agentUrl, token, '/api/tags');
}

export function getTagSettings(agentUrl: string, token: string): Promise<TagSettings> {
  return request(agentUrl, token, '/api/tags/settings');
}

/** PATCH: merge `options` into the profile's own overrides (`null` inherits
 *  again); `enabled` alone flips the switch. */
export function patchTagSettings(
  agentUrl: string,
  token: string,
  body: { enabled?: boolean; options?: TagOptionsPatch },
): Promise<TagSettings> {
  return request(agentUrl, token, '/api/tags/settings', { method: 'PATCH', body });
}

/** PUT: `options` REPLACES the profile's own overrides (omit it to leave them);
 *  a key left out inherits the admin default. */
export function saveTagSettings(
  agentUrl: string,
  token: string,
  body: { enabled?: boolean; options?: TagOptions },
): Promise<TagSettings> {
  return request(agentUrl, token, '/api/tags/settings', { method: 'PUT', body });
}

export function getTagDevice(
  agentUrl: string, token: string, deviceId: string,
): Promise<{ device: TagDevice; deliveries: TagDelivery[] }> {
  return request(agentUrl, token, `/api/tags/devices/${enc(deviceId)}`);
}

/** `name` must be 1..128 characters once stripped (422 `invalid_name`). */
export async function renameTagDevice(
  agentUrl: string, token: string, deviceId: string, name: string,
): Promise<TagDevice> {
  const res = await request<{ device: TagDevice }>(agentUrl, token,
    `/api/tags/devices/${enc(deviceId)}`, { method: 'PATCH', body: { name } });
  return res.device;
}

/** Pin a note on one tag. 422 `otp_refused` when the text looks like a
 *  one-time code; 409 `clear_pending` while the screen is being cleared. */
export async function displayOnTag(
  agentUrl: string, token: string, deviceId: string, note: DisplayNotePayload,
): Promise<TagDelivery> {
  const res = await request<{ delivery: TagDelivery }>(agentUrl, token,
    `/api/tags/devices/${enc(deviceId)}/display`, { method: 'POST', body: note });
  return res.delivery;
}

export async function clearTag(
  agentUrl: string, token: string, deviceId: string,
): Promise<TagDelivery> {
  const res = await request<{ delivery: TagDelivery }>(agentUrl, token,
    `/api/tags/devices/${enc(deviceId)}/clear`, { method: 'POST', body: {} });
  return res.delivery;
}

export async function refreshTag(
  agentUrl: string, token: string, deviceId: string,
): Promise<TagCommand> {
  const res = await request<{ command: TagCommand }>(agentUrl, token,
    `/api/tags/devices/${enc(deviceId)}/refresh`, { method: 'POST', body: {} });
  return res.command;
}

export async function identifyTag(
  agentUrl: string, token: string, deviceId: string,
): Promise<TagCommand> {
  const res = await request<{ command: TagCommand }>(agentUrl, token,
    `/api/tags/devices/${enc(deviceId)}/identify`, { method: 'POST', body: {} });
  return res.command;
}

/** The stored preview PNG, or null while the companion has sent none
 *  (404 `no_preview`). Any other failure throws. */
export async function fetchTagPreview(
  agentUrl: string, token: string, deviceId: string, kind: 'desired' | 'displayed',
): Promise<TagPreview | null> {
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(
    `${resolveBaseUrl(agentUrl)}/api/tags/devices/${enc(deviceId)}/preview?kind=${enc(kind)}`,
    { headers },
  );
  if (!res.ok) {
    const err = await failure(res);
    if (res.status === 404 && err.code === 'no_preview') return null;
    throw err;
  }
  const raw = res.headers.get('X-Tag-Revision');
  const revision = raw != null && raw !== '' && Number.isFinite(Number(raw)) ? Number(raw) : null;
  return { blob: await res.blob(), revision };
}

export function listTagDeliveries(
  agentUrl: string, token: string, query: DeliveryQuery = {},
): Promise<DeliveryPage> {
  const params = new URLSearchParams();
  if (query.device) params.set('device', query.device);
  if (query.state) params.set('state', query.state);
  if (query.limit != null) params.set('limit', String(query.limit));
  if (query.before != null) params.set('before', String(query.before));
  const qs = params.toString();
  return request(agentUrl, token, `/api/tags/deliveries${qs ? `?${qs}` : ''}`);
}

export async function getTagDelivery(
  agentUrl: string, token: string, deliveryId: number,
): Promise<TagDelivery> {
  const res = await request<{ delivery: TagDelivery }>(agentUrl, token,
    `/api/tags/deliveries/${enc(String(deliveryId))}`);
  return res.delivery;
}

/** Cancel a card that has not finished — also after the companion fetched
 *  it: `resolved` is the job that tells the companion to drop it (null when
 *  the tag has changed hands). 409 `already_terminal` once it finished. */
export function cancelTagDelivery(
  agentUrl: string, token: string, deliveryId: number,
): Promise<{ delivery: TagDelivery; resolved: Record<string, any> | null }> {
  return request(agentUrl, token,
    `/api/tags/deliveries/${enc(String(deliveryId))}/cancel`, { method: 'POST', body: {} });
}

export async function listTagCompanions(
  agentUrl: string, token: string,
): Promise<TagCompanionSummary[]> {
  const res = await request<{ companions: TagCompanionSummary[] }>(agentUrl, token,
    '/api/tags/companions');
  return res.companions ?? [];
}

export async function listContentCredentials(
  agentUrl: string, token: string,
): Promise<TagCredential[]> {
  const res = await request<{ credentials: TagCredential[] }>(agentUrl, token,
    '/api/tags/credentials');
  return res.credentials ?? [];
}

/** The secret is in this response only. */
export function createContentCredential(
  agentUrl: string, token: string, body: { companion_id: string; label?: string },
): Promise<TagSecretPayload> {
  return request(agentUrl, token, '/api/tags/credentials', { method: 'POST', body });
}

export async function revokeContentCredential(
  agentUrl: string, token: string, credentialId: string,
): Promise<TagCredential> {
  const res = await request<{ credential: TagCredential }>(agentUrl, token,
    `/api/tags/credentials/${enc(credentialId)}`, { method: 'DELETE' });
  return res.credential;
}

// ── admin: hardware ────────────────────────────────────────────────────────

export function getTagHardware(agentUrl: string, token: string): Promise<TagHardware> {
  return request(agentUrl, token, '/api/tags/hardware');
}

export function registerTagCompanion(
  agentUrl: string, token: string, name: string,
): Promise<TagSecretPayload & { companion: TagCompanion }> {
  return request(agentUrl, token, '/api/tags/hardware/companions', {
    method: 'POST', body: { name },
  });
}

export function rotateTagCompanion(
  agentUrl: string, token: string, companionId: string,
): Promise<TagSecretPayload & { revoked: string[] }> {
  return request(agentUrl, token,
    `/api/tags/hardware/companions/${enc(companionId)}/rotate`, { method: 'POST', body: {} });
}

export async function deleteTagCompanion(
  agentUrl: string, token: string, companionId: string,
): Promise<void> {
  await request(agentUrl, token, `/api/tags/hardware/companions/${enc(companionId)}`,
    { method: 'DELETE' });
}

export async function queueTagCommand(
  agentUrl: string,
  token: string,
  body: { companion_id: string; kind: string; args?: Record<string, any> },
): Promise<TagCommand> {
  const res = await request<{ command: TagCommand }>(agentUrl, token,
    '/api/tags/hardware/commands', { method: 'POST', body });
  return res.command;
}

export async function getTagCommand(
  agentUrl: string, token: string, commandId: string,
): Promise<TagCommand> {
  const res = await request<{ command: TagCommand }>(agentUrl, token,
    `/api/tags/hardware/commands/${enc(commandId)}`);
  return res.command;
}

/** 409 `bridge_required` when the companion has several bridges and none was
 *  named; 422 `unknown_profile` for an owner that does not exist. */
export function claimTag(
  agentUrl: string,
  token: string,
  deviceId: string,
  body: { owner: string; bridge_id?: string; name?: string },
): Promise<{ device: TagDevice; commands: TagCommand[] }> {
  return request(agentUrl, token, `/api/tags/hardware/tags/${enc(deviceId)}/claim`, {
    method: 'POST', body,
  });
}

export function assignTagBridge(
  agentUrl: string, token: string, deviceId: string, bridgeId: string,
): Promise<{ device: TagDevice; command: TagCommand }> {
  return request(agentUrl, token, `/api/tags/hardware/tags/${enc(deviceId)}/assign`, {
    method: 'POST', body: { bridge_id: bridgeId },
  });
}

export function releaseTag(
  agentUrl: string, token: string, deviceId: string,
): Promise<{ device: TagDevice; commands: TagCommand[] }> {
  return request(agentUrl, token, `/api/tags/hardware/tags/${enc(deviceId)}/release`, {
    method: 'POST', body: {},
  });
}

export async function renameHardwareDevice(
  agentUrl: string, token: string, deviceId: string, name: string,
): Promise<TagDevice> {
  const res = await request<{ device: TagDevice }>(agentUrl, token,
    `/api/tags/hardware/devices/${enc(deviceId)}`, { method: 'PATCH', body: { name } });
  return res.device;
}

/** 409 `tag_owned` for a tag a profile still owns — release it first. */
export async function forgetHardwareDevice(
  agentUrl: string, token: string, deviceId: string,
): Promise<TagDevice> {
  const res = await request<{ deleted: boolean; device: TagDevice }>(agentUrl, token,
    `/api/tags/hardware/devices/${enc(deviceId)}`, { method: 'DELETE' });
  return res.device;
}

export function getTagDefaults(
  agentUrl: string, token: string,
): Promise<{ defaults: TagOptions; builtin: TagEffectiveOptions }> {
  return request(agentUrl, token, '/api/tags/hardware/defaults');
}

/** Merge into the admin defaults (`null` removes a default). */
export function patchTagDefaults(
  agentUrl: string, token: string, defaults: TagOptionsPatch,
): Promise<{ defaults: TagOptions; builtin: TagEffectiveOptions }> {
  return request(agentUrl, token, '/api/tags/hardware/defaults', {
    method: 'PATCH', body: { defaults },
  });
}

/** Replaces the whole admin defaults object. */
export function saveTagDefaults(
  agentUrl: string, token: string, defaults: TagOptions,
): Promise<{ defaults: TagOptions; builtin: TagEffectiveOptions }> {
  return request(agentUrl, token, '/api/tags/hardware/defaults', {
    method: 'PUT', body: { defaults },
  });
}
