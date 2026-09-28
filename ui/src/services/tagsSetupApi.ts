// Typed client for simple Cremind Tag hardware setup — the profile API of
// cremind-tag docs/setup-api.md §1 (JWT, the caller's profile only):
//
//   GET    /api/tags/connections             gateways (one per Connect worker), bridges, tags,
//                                            computers, and the setups still running
//   GET    /api/tags/connect                 Cremind Connect installers per OS
//   POST   /api/tags/setup-sessions          connect_gateway | recover | probe → {session, launch_url}
//   GET    /api/tags/setup-sessions/{id}     progress
//   POST   /api/tags/setup-sessions/{id}/confirm   the browser's "yes, same words"
//   DELETE /api/tags/setup-sessions/{id}     cancel before it is redeemed
//   POST   /api/tags/discovery               look for a bridge/tag by its setup code
//   GET    /api/tags/discovery/{id}
//   POST   /api/tags/pairings                pair a discovery candidate
//   GET / DELETE /api/tags/pairings/{id}
//   POST   /api/tags/devices/{id}/unpair | pause | resume | test
//   POST   /api/tags/recoveries              move a gateway to this computer → {recovery, session, launch_url}
//   GET    /api/tags/recoveries/{id}
//
// Same conventions as tagsApi.ts (base URL, Bearer auth, TagsApiError with
// the server's `error` code). Unlike the older /api/tags/* payloads,
// timestamps here are ISO 8601 strings. Every mutation carries an
// `Idempotency-Key`; `IdempotencyKeys` keeps one key per action while its
// outcome is unknown, so retrying the same action after a dropped answer
// cannot run it twice. CLI counterpart: `cremind tags devices …`.
import { TagsApiError, tagsBaseUrl, tagsRequest, type TagDelivery } from './tagsApi';

// ── shapes ─────────────────────────────────────────────────────────────────

export type SetupDeviceKind = 'gateway' | 'bridge' | 'tag';
/** `offline` is derived: paired or ready, but no contact for 2 minutes. */
export type SetupDeviceState =
  | 'pairing' | 'paired' | 'ready' | 'offline'
  | 'recovery_pending' | 'removal_pending' | 'reconciling';

export interface SetupCapacity { max_tags: number; assigned: number }

export interface SetupDeliveryStatus {
  pending_count: number;
  displayed_revision: number;
  desired_revision: number;
  clear_required: boolean;
  /** `ok` | `pending` | `clear_pending` | `failed`. */
  status: string;
}

export interface SetupDevice {
  /** The device's id for /api/tags/devices/{id}/… (the same id the Tags page
   *  uses). Null while the binding has no device record yet: no actions then. */
  id: string | null;
  binding_id: string;
  kind: SetupDeviceKind;
  name: string;
  /** 32 hex; never shown whole. */
  device_id: string;
  /** 8 hex, as on the label. */
  short_id: string;
  state: SetupDeviceState | string;
  paused: boolean;
  generation: number;
  fw: string | null;
  board: number | null;
  last_contact_at: string | null;
  battery_mv: number | null;
  rssi: number | null;
  /** Bridges. */
  capacity?: SetupCapacity | null;
  /** Bridges: false = its font pack is not the one Connect uses. */
  fontpack_ok?: boolean | null;
  /** Tags: the bridge it is assigned to. */
  bridge_id?: string | null;
  /** Tags. */
  delivery?: SetupDeliveryStatus | null;
  /** Bridges: the tags assigned to it. */
  affected_tag_ids?: string[] | null;
}

export type ComputerPlatform = 'windows' | 'macos' | 'linux';

export interface SetupComputer {
  installation_id: string;
  name: string;
  platform: ComputerPlatform | string;
  version: string;
  last_seen_at?: string | null;
}

export type ConnectionStatus =
  | 'setting_up' | 'connected' | 'offline' | 'paused' | 'recovery_pending' | 'removal_pending';

/** One gateway = one Cremind Connect worker (a private companion). `id` is the companion id. */
export interface TagConnection {
  id: string;
  name: string;
  status: ConnectionStatus | string;
  paused: boolean;
  computer: SetupComputer | null;
  gateway: SetupDevice;
  bridges: SetupDevice[];
  tags: SetupDevice[];
  last_seen_at: string | null;
  created_at: string;
}

export interface SetupError { code: string; message: string }

export type SetupSessionOperation = 'connect_gateway' | 'recover' | 'probe';
export type SetupSessionState =
  | 'waiting_for_connect' | 'waiting_for_approval' | 'waiting_for_confirmation'
  | 'redeeming' | 'connecting' | 'completed' | 'cancelled' | 'expired' | 'failed';

export interface SetupSession {
  id: string;
  operation: SetupSessionOperation | string;
  state: SetupSessionState | string;
  expires_at: string;
  created_at: string;
  computer: { installation_id: string; name: string; platform: string; version: string } | null;
  verification_phrase: string | null;
  gateway: { device_id: string; short_id: string; fw: string | null; usable: boolean } | null;
  native_approved: boolean;
  browser_confirmed: boolean;
  companion_id: string | null;
  operation_id: string | null;
  error: SetupError | null;
}

export type SetupOperationKind =
  | 'discovery' | 'pair_bridge' | 'pair_tag' | 'unpair'
  | 'recover_gateway' | 'release_gateway' | 'claim_gateway';
export type SetupOperationState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'pending_device';

export interface SetupOperation {
  id: string;
  kind: SetupOperationKind | string;
  state: SetupOperationState | string;
  stage: string;
  stage_detail: string | null;
  device: SetupDevice | null;
  error: SetupError | null;
  created_at: string;
  updated_at: string;
}

export interface DiscoveryCandidate {
  id: string;
  /** The companion (connection) id of the gateway that heard it. */
  gateway_id: string;
  /** Tags: the ready bridge that heard it in setup mode. */
  bridge_id: string | null;
  bridge_name: string | null;
  rssi: number | null;
  seen_at: string;
  capacity: SetupCapacity | null;
  eligible: boolean;
  /** Why not eligible: `bridge_full`, … */
  reason: string | null;
}

export type DiscoveryState = 'scanning' | 'found' | 'not_found' | 'failed' | 'cancelled';

export interface Discovery {
  id: string;
  role: 'bridge' | 'tag';
  short_id: string;
  state: DiscoveryState | string;
  started_at: string;
  expires_at: string;
  candidates: DiscoveryCandidate[];
  /** The strongest recent signal among eligible candidates. */
  recommended: string | null;
  error: SetupError | null;
}

export interface Pairing extends SetupOperation {
  role: 'bridge' | 'tag';
  /** The profile's first tag: the page offers to turn Tags on. */
  first_tag: boolean;
}

export interface RecoveryDevice {
  id: string | null;
  kind: SetupDeviceKind | string;
  name: string;
  /** `pending` | `rekeyed` | `recovery_pending` | `failed`. */
  state: string;
}

export interface Recovery extends SetupOperation {
  companion_id: string;
  devices: RecoveryDevice[];
}

export interface ConnectionsAnswer {
  simple_setup: boolean;
  connections: TagConnection[];
  computers: SetupComputer[];
  active: { sessions: SetupSession[]; operations: SetupOperation[] };
}

export type ConnectDownloadKey = 'windows-x64' | 'macos-arm64' | 'macos-x64' | 'linux-x64-deb' | 'linux-x64-tar';

export interface ConnectDownloads {
  latest_version: string;
  downloads: Partial<Record<ConnectDownloadKey, { url: string; sha256?: string }>>;
}

// ── idempotency ────────────────────────────────────────────────────────────

/** A random v4 UUID. `crypto.randomUUID` exists only in secure contexts
 *  (HTTPS or localhost); a LAN install over plain HTTP still has
 *  `getRandomValues`. */
export function newIdempotencyKey(): string {
  const c: Crypto | undefined = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const b = new Uint8Array(16);
  if (c && typeof c.getRandomValues === 'function') c.getRandomValues(b);
  else for (let i = 0; i < 16; i += 1) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function canonical(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value ?? null);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  const obj = value as Record<string, unknown>;
  return `{${Object.keys(obj).filter((k) => obj[k] !== undefined).sort()
    .map((k) => `${JSON.stringify(k)}:${canonical(obj[k])}`).join(',')}}`;
}

/** A 53-bit hash (cyrb53) of a body: the key map keeps no request body — a
 *  setup code included — only this. */
function fingerprint(body: unknown): string {
  const text = canonical(body);
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36);
}

/** True when a failed mutation may still have happened on the server: no
 *  answer at all (network), or a server-side failure. A 4xx is an answer. */
export function outcomeUnknown(e: unknown): boolean {
  if (e instanceof TagsApiError) return e.status >= 500 || e.status === 408 || e.status === 429;
  return true;
}

/**
 * One Idempotency-Key per user action. Retrying the same action with the same
 * body while its first outcome is unknown sends the same key, so the server
 * answers with the first result instead of running it twice. Once an answer
 * arrives (success or a 4xx refusal), the next attempt is a new action with a
 * new key — otherwise a refusal (e.g. `gateway_offline`) would be replayed
 * after the cause was fixed. A different body is a different action.
 */
export class IdempotencyKeys {
  private readonly open = new Map<string, { fingerprint: string; key: string }>();

  /** The key the next attempt of `action` with `body` uses. */
  keyFor(action: string, body: unknown): string {
    const fp = fingerprint(body);
    const held = this.open.get(action);
    if (held && held.fingerprint === fp) return held.key;
    const key = newIdempotencyKey();
    this.open.set(action, { fingerprint: fp, key });
    return key;
  }

  async run<T>(action: string, body: unknown, send: (key: string) => Promise<T>): Promise<T> {
    const key = this.keyFor(action, body);
    try {
      const out = await send(key);
      this.settle(action, key);
      return out;
    } catch (e) {
      if (!outcomeUnknown(e)) this.settle(action, key);
      throw e;
    }
  }

  /** Forget every held key (a profile switch). */
  clear(): void {
    this.open.clear();
  }

  private settle(action: string, key: string): void {
    if (this.open.get(action)?.key === key) this.open.delete(action);
  }
}

// ── requests ───────────────────────────────────────────────────────────────

const enc = encodeURIComponent;

function mutation<T>(
  agentUrl: string, token: string, path: string, method: string, body: unknown, key?: string,
): Promise<T> {
  return tagsRequest<T>(agentUrl, token, path, {
    method,
    body,
    headers: { 'Idempotency-Key': key || newIdempotencyKey() },
  });
}

/** Where Cremind Connect calls back: the origin (`scheme://host[:port]`, no
 *  path — the server refuses one) of the backend this page talks to. */
export function setupServerUrl(agentUrl: string): string {
  const base = tagsBaseUrl(agentUrl);
  try {
    return new URL(base).origin;
  } catch {
    return base.replace(/\/+$/, '');
  }
}

/** 403 `simple_setup_disabled` when the server has simple setup off. */
export function getConnections(agentUrl: string, token: string): Promise<ConnectionsAnswer> {
  return tagsRequest(agentUrl, token, '/api/tags/connections');
}

export function getConnectDownloads(agentUrl: string, token: string): Promise<ConnectDownloads> {
  return tagsRequest(agentUrl, token, '/api/tags/connect');
}

/** `launch_url` (the `cremind-connect://setup?…` link) comes in this answer only. */
export function createSetupSession(
  agentUrl: string,
  token: string,
  body: { operation: SetupSessionOperation; server_url: string; companion_id?: string },
  key?: string,
): Promise<{ session: SetupSession; launch_url: string }> {
  return mutation(agentUrl, token, '/api/tags/setup-sessions', 'POST', body, key);
}

export async function getSetupSession(agentUrl: string, token: string, id: string): Promise<SetupSession> {
  const res = await tagsRequest<{ session: SetupSession }>(agentUrl, token, `/api/tags/setup-sessions/${enc(id)}`);
  return res.session;
}

/** 409 `not_approved` before the Connect window approved; 410 `session_expired`. */
export async function confirmSetupSession(
  agentUrl: string, token: string, id: string, key?: string,
): Promise<SetupSession> {
  const res = await mutation<{ session: SetupSession }>(agentUrl, token,
    `/api/tags/setup-sessions/${enc(id)}/confirm`, 'POST', {}, key);
  return res.session;
}

/** 409 `already_redeemed` once Connect redeemed it. */
export async function cancelSetupSession(
  agentUrl: string, token: string, id: string, key?: string,
): Promise<SetupSession> {
  const res = await mutation<{ session: SetupSession }>(agentUrl, token,
    `/api/tags/setup-sessions/${enc(id)}`, 'DELETE', undefined, key);
  return res.session;
}

/** 422 `setup_code_invalid` / `setup_code_wrong_role`; 409 `no_gateway`,
 *  `gateway_required`, `gateway_offline`, `no_ready_bridge`, `device_owned`. */
export async function startDiscovery(
  agentUrl: string,
  token: string,
  body: { role: 'bridge' | 'tag'; setup_code: string; gateway_id?: string; duration_s?: number },
  key?: string,
): Promise<Discovery> {
  const res = await mutation<{ discovery: Discovery }>(agentUrl, token, '/api/tags/discovery', 'POST', body, key);
  return res.discovery;
}

export async function getDiscovery(agentUrl: string, token: string, id: string): Promise<Discovery> {
  const res = await tagsRequest<{ discovery: Discovery }>(agentUrl, token, `/api/tags/discovery/${enc(id)}`);
  return res.discovery;
}

/** 409 `candidate_not_eligible` (e.g. the bridge filled up meanwhile). */
export async function startPairing(
  agentUrl: string,
  token: string,
  body: { discovery_id: string; candidate_id: string; name?: string },
  key?: string,
): Promise<Pairing> {
  const res = await mutation<{ pairing: Pairing }>(agentUrl, token, '/api/tags/pairings', 'POST', body, key);
  return res.pairing;
}

export async function getPairing(agentUrl: string, token: string, id: string): Promise<Pairing> {
  const res = await tagsRequest<{ pairing: Pairing }>(agentUrl, token, `/api/tags/pairings/${enc(id)}`);
  return res.pairing;
}

/** Cancel and reconcile: a pairing that may already have committed on the
 *  device is checked, and either kept or left unowned. */
export async function cancelPairing(agentUrl: string, token: string, id: string, key?: string): Promise<Pairing> {
  const res = await mutation<{ pairing: Pairing }>(agentUrl, token,
    `/api/tags/pairings/${enc(id)}`, 'DELETE', undefined, key);
  return res.pairing;
}

/** Access is revoked at once; the device is cleaned up when reachable. */
export function unpairDevice(
  agentUrl: string, token: string, deviceId: string, key?: string,
): Promise<{ operation: SetupOperation; device: SetupDevice }> {
  return mutation(agentUrl, token, `/api/tags/devices/${enc(deviceId)}/unpair`, 'POST', {}, key);
}

/** Rename a gateway, bridge or tag of this profile (the existing device PATCH;
 *  422 `invalid_name` outside 1..128 characters). */
export async function renameSetupDevice(
  agentUrl: string, token: string, deviceId: string, name: string, key?: string,
): Promise<Record<string, unknown> | null> {
  const res = await mutation<{ device?: Record<string, unknown> }>(agentUrl, token,
    `/api/tags/devices/${enc(deviceId)}`, 'PATCH', { name }, key);
  return res.device ?? null;
}

/** A gateway pauses (or resumes) its whole connection. */
export async function setDevicePaused(
  agentUrl: string, token: string, deviceId: string, paused: boolean, key?: string,
): Promise<SetupDevice> {
  const res = await mutation<{ device: SetupDevice }>(agentUrl, token,
    `/api/tags/devices/${enc(deviceId)}/${paused ? 'pause' : 'resume'}`, 'POST', {}, key);
  return res.device;
}

/** A test card for one tag. */
export async function sendTestCard(
  agentUrl: string, token: string, deviceId: string, key?: string,
): Promise<TagDelivery> {
  const res = await mutation<{ delivery: TagDelivery }>(agentUrl, token,
    `/api/tags/devices/${enc(deviceId)}/test`, 'POST', {}, key);
  return res.delivery;
}

export function startRecovery(
  agentUrl: string,
  token: string,
  body: { companion_id: string; server_url: string },
  key?: string,
): Promise<{ recovery: Recovery; session: SetupSession; launch_url: string }> {
  return mutation(agentUrl, token, '/api/tags/recoveries', 'POST', body, key);
}

export async function getRecovery(agentUrl: string, token: string, id: string): Promise<Recovery> {
  const res = await tagsRequest<{ recovery: Recovery }>(agentUrl, token, `/api/tags/recoveries/${enc(id)}`);
  return res.recovery;
}
