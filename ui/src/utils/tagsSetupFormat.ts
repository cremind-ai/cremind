// Pure helpers for simple hardware setup (Settings → Tags): plain-language
// labels, what the dialogs do next for a given server answer, and which
// Cremind Connect installer fits this computer. No Vue, no fetch.
//
// Wording rule: people see gateways, bridges, tags, computers and Cremind
// Connect — never serial ports, mesh addresses, epochs, credential ids or
// internal project names. A device id appears only as a short suffix, and
// only where two devices could otherwise look the same.
import type {
  ConnectDownloadKey, ConnectDownloads, Discovery, DiscoveryCandidate, SetupCapacity,
  SetupDeliveryStatus, SetupDevice, SetupError, SetupOperation, SetupSession, TagConnection,
} from '../services/tagsSetupApi';
import type { PillType } from './tagsFormat';

export type Pill = { label: string; type: PillType };

/** An action on a device row: the main ones as buttons (`inline`), the rest in a menu. */
export interface RowAction {
  key: string;
  label: string;
  icon?: string;
  inline?: boolean;
  primary?: boolean;
  danger?: boolean;
  disabled?: boolean;
  /** Why it is disabled (the button's title, and read out with its name). */
  reason?: string;
}

// ── time and ids ───────────────────────────────────────────────────────────

/** ISO 8601 → epoch ms (null when absent or unreadable). */
export function isoToMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

/** "…3C4D": the end of a label's short id, to tell two devices apart. */
export function shortSuffix(shortId: string | null | undefined): string {
  const s = (shortId || '').trim().toUpperCase();
  return s ? `…${s.slice(-4)}` : '';
}

const KIND_WORD: Record<string, string> = { gateway: 'Gateway', bridge: 'Bridge', tag: 'Tag' };

/** The name people gave it, else "Tag …3C4D". */
export function setupDeviceTitle(d: Pick<SetupDevice, 'name' | 'kind' | 'short_id'>): string {
  const name = (d.name || '').trim();
  if (name) return name;
  return `${KIND_WORD[d.kind] ?? 'Device'} ${shortSuffix(d.short_id)}`.trim();
}

/** A gateway's row title: its own name, else the connection's. */
export function connectionTitle(c: Pick<TagConnection, 'name' | 'gateway'>): string {
  return (c.gateway?.name || '').trim() || (c.name || '').trim() || setupDeviceTitle(c.gateway);
}

// ── status chips ───────────────────────────────────────────────────────────

const CONNECTION_STATUS: Record<string, Pill> = {
  setting_up: { label: 'Setting up', type: 'warning' },
  connected: { label: 'Connected', type: 'success' },
  offline: { label: 'Offline', type: 'info' },
  paused: { label: 'Paused', type: 'info' },
  recovery_pending: { label: 'Recovery pending', type: 'warning' },
  removal_pending: { label: 'Removal pending', type: 'warning' },
};

export function connectionStatusPill(c: Pick<TagConnection, 'status' | 'paused'>): Pill {
  if (c.paused && c.status !== 'removal_pending') return CONNECTION_STATUS.paused;
  return CONNECTION_STATUS[c.status] ?? { label: humanize(c.status), type: 'info' };
}

const DEVICE_STATE: Record<string, Pill> = {
  pairing: { label: 'Setting up', type: 'warning' },
  paired: { label: 'Finishing setup', type: 'warning' },
  ready: { label: 'Ready', type: 'success' },
  offline: { label: 'Offline', type: 'info' },
  recovery_pending: { label: 'Recovery pending', type: 'warning' },
  removal_pending: { label: 'Removal pending', type: 'warning' },
  reconciling: { label: 'Checking', type: 'warning' },
};

export function deviceStatePill(d: Pick<SetupDevice, 'state' | 'paused'>): Pill {
  if (d.paused && d.state !== 'removal_pending') return { label: 'Paused', type: 'info' };
  return DEVICE_STATE[d.state] ?? { label: humanize(d.state), type: 'info' };
}

/** "3 of 20 tags" — a bridge's room for tags. */
export function capacityLabel(cap: SetupCapacity | null | undefined): string {
  if (!cap || !cap.max_tags) return '';
  return `${cap.assigned} of ${cap.max_tags} tags`;
}

export function capacityFull(cap: SetupCapacity | null | undefined): boolean {
  return !!cap && !!cap.max_tags && cap.assigned >= cap.max_tags;
}

/** What is happening with a tag's screen, in words. */
export function deliveryLabel(delivery: SetupDeliveryStatus | null | undefined, paused = false): string {
  if (paused) return 'Paused: no new updates';
  if (!delivery) return 'No updates yet';
  if (delivery.clear_required || delivery.status === 'clear_pending') return 'Clearing the screen';
  if (delivery.status === 'failed') return 'The last update did not arrive';
  const n = delivery.pending_count || 0;
  if (delivery.status === 'pending' || n > 0) {
    return n === 1 ? '1 update on its way' : n > 1 ? `${n} updates on their way` : 'An update is on its way';
  }
  if (!delivery.displayed_revision) return 'Nothing shown yet';
  return 'Up to date';
}

export function deliveryTone(delivery: SetupDeliveryStatus | null | undefined): 'ok' | 'busy' | 'bad' {
  if (!delivery) return 'ok';
  if (delivery.status === 'failed') return 'bad';
  if (delivery.status === 'pending' || delivery.status === 'clear_pending' || delivery.pending_count > 0) return 'busy';
  return 'ok';
}

/** Battery as a person reads it: a level word, not millivolts. */
export function batteryLevel(mv: number | null | undefined): { label: string; low: boolean } | null {
  if (mv == null) return null;
  if (mv < 2400) return { label: 'Battery low', low: true };
  if (mv < 2700) return { label: 'Battery fair', low: false };
  return { label: 'Battery good', low: false };
}

export function signalLabel(rssi: number | null | undefined): string {
  if (rssi == null) return '';
  if (rssi >= -60) return 'Strong signal';
  if (rssi >= -75) return 'Good signal';
  if (rssi >= -85) return 'Weak signal';
  return 'Very weak signal';
}

/** Is dotted version `a` newer than `b`? ("0.10.0" > "0.9.3"; missing parts count as 0.) */
export function isNewerVersion(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return false;
  const parse = (v: string) => v.replace(/^v/i, '').split(/[.+-]/).slice(0, 3).map((x) => Number.parseInt(x, 10) || 0);
  const [x, y] = [parse(a), parse(b)];
  for (let i = 0; i < 3; i += 1) {
    if ((x[i] ?? 0) !== (y[i] ?? 0)) return (x[i] ?? 0) > (y[i] ?? 0);
  }
  return false;
}

export function humanize(code: string | null | undefined): string {
  const text = (code || '').replace(/_/g, ' ').trim();
  return text ? text[0].toUpperCase() + text.slice(1) : '';
}

// ── what can be added now ──────────────────────────────────────────────────

export interface SetupReadiness {
  connected: TagConnection[];
  readyBridges: SetupDevice[];
  canAddBridge: boolean;
  addBridgeReason: string;
  canAddTag: boolean;
  addTagReason: string;
}

export function setupReadiness(connections: TagConnection[]): SetupReadiness {
  const live = connections.filter((c) => c.status !== 'removal_pending');
  const connected = live.filter((c) => c.status === 'connected' && !c.paused);
  let addBridgeReason = '';
  if (!connected.length) {
    if (!live.length) addBridgeReason = 'Connect a gateway first.';
    else if (live.some((c) => c.status === 'setting_up')) addBridgeReason = 'Wait until the gateway is connected.';
    else if (live.every((c) => c.paused || c.status === 'paused')) addBridgeReason = 'Your gateway is paused. Resume it first.';
    else addBridgeReason = 'Your gateway is offline. Plug it in and make sure Cremind Connect is running on its computer.';
  }
  const bridges = connected.flatMap((c) => c.bridges).filter((b) => b.state !== 'removal_pending');
  const readyBridges = bridges.filter((b) => b.state === 'ready' && !b.paused);
  let addTagReason = '';
  if (!readyBridges.length) {
    if (!connected.length) addTagReason = addBridgeReason;
    else if (!bridges.length) addTagReason = 'Add a bridge first.';
    else if (bridges.every((b) => b.paused)) addTagReason = 'Your bridges are paused. Resume one first.';
    else addTagReason = 'Wait until a bridge shows Ready.';
  }
  return {
    connected,
    readyBridges,
    canAddBridge: connected.length > 0,
    addBridgeReason,
    canAddTag: readyBridges.length > 0,
    addTagReason,
  };
}

// ── setup sessions (Connect gateway, Recover) ──────────────────────────────

export const SESSION_TERMINAL = ['completed', 'cancelled', 'expired', 'failed'] as const;

export function isSessionTerminal(state: string | null | undefined): boolean {
  return !!state && (SESSION_TERMINAL as readonly string[]).includes(state);
}

export function isSessionFailed(state: string | null | undefined): boolean {
  return state === 'cancelled' || state === 'expired' || state === 'failed';
}

/** Can Connect still pick this session up from its link? */
export function sessionLinkUsable(s: Pick<SetupSession, 'state' | 'expires_at'> | null, now = Date.now()): boolean {
  if (!s || s.state !== 'waiting_for_connect') return false;
  const expires = isoToMs(s.expires_at);
  return expires == null || expires - now > 15_000;
}

export type ConnectStep = 'plug' | 'open' | 'approve' | 'confirm' | 'finish' | 'done' | 'failed';

export function connectStep(session: Pick<SetupSession, 'state'> | null): ConnectStep {
  if (!session) return 'plug';
  switch (session.state) {
    case 'waiting_for_connect': return 'open';
    case 'waiting_for_approval': return 'approve';
    case 'waiting_for_confirmation': return 'confirm';
    case 'redeeming':
    case 'connecting': return 'finish';
    case 'completed': return 'done';
    default: return isSessionFailed(session.state) ? 'failed' : 'open';
  }
}

/** The ElSteps index for a step (plug, open, approve, confirm, finish). */
export function connectStepIndex(step: ConnectStep, lastActive: number): number {
  const order: ConnectStep[] = ['plug', 'open', 'approve', 'confirm', 'finish'];
  if (step === 'done') return order.length;
  if (step === 'failed') return lastActive;
  return order.indexOf(step);
}

export function finishLabel(session: Pick<SetupSession, 'state' | 'operation'>): string {
  if (session.state === 'redeeming') return 'Cremind Connect is setting up the connection…';
  return session.operation === 'recover'
    ? 'Taking over the gateway on this computer…'
    : 'Connecting to the gateway…';
}

// ── discovery and pairing (Add bridge, Add tag) ────────────────────────────

export type DiscoveryDecision =
  | { kind: 'searching' }
  | { kind: 'pair'; candidateId: string }
  | { kind: 'choose'; preselect: string | null }
  | { kind: 'unavailable'; reason: string | null }
  | { kind: 'not_found' }
  | { kind: 'failed'; error: SetupError | null }
  | { kind: 'cancelled' };

/** Recommended first, then eligible by signal, then the ones that cannot take it. */
export function sortCandidates(d: Pick<Discovery, 'candidates' | 'recommended'>): DiscoveryCandidate[] {
  const rank = (c: DiscoveryCandidate) => (c.id === d.recommended && c.eligible ? 0 : c.eligible ? 1 : 2);
  return [...(d.candidates || [])].sort((a, b) =>
    rank(a) - rank(b) || (b.rssi ?? -999) - (a.rssi ?? -999) || a.id.localeCompare(b.id));
}

/**
 * What the Add dialogs do with a discovery answer: keep looking; pair at once
 * when exactly one candidate can take the device; otherwise let the person
 * choose (recommended preselected).
 */
export function discoveryDecision(d: Pick<Discovery, 'state' | 'candidates' | 'recommended' | 'error'>): DiscoveryDecision {
  switch (d.state) {
    case 'failed': return { kind: 'failed', error: d.error };
    case 'cancelled': return { kind: 'cancelled' };
    case 'not_found': return { kind: 'not_found' };
    case 'found': break;
    default: return { kind: 'searching' }; // scanning (or a state this page does not know yet)
  }
  const candidates = d.candidates || [];
  if (!candidates.length) return { kind: 'searching' };
  const eligible = candidates.filter((c) => c.eligible);
  if (!eligible.length) return { kind: 'unavailable', reason: candidates.find((c) => c.reason)?.reason ?? null };
  if (eligible.length === 1) return { kind: 'pair', candidateId: eligible[0].id };
  const preselect = eligible.some((c) => c.id === d.recommended) ? d.recommended : sortCandidates(d)[0]?.id ?? null;
  return { kind: 'choose', preselect };
}

export const OPERATION_TERMINAL = ['succeeded', 'failed', 'cancelled'] as const;

export function isOperationTerminal(state: string | null | undefined): boolean {
  return !!state && (OPERATION_TERMINAL as readonly string[]).includes(state);
}

/** One line on where a pairing (or any operation) is. */
export function operationProgressLabel(op: Pick<SetupOperation, 'state' | 'stage' | 'stage_detail'>): string {
  if (op.stage_detail) return op.stage_detail;
  // A recovery waits for its Cremind Connect session before it runs.
  if (op.state === 'waiting_for_connect') return 'Waiting for Cremind Connect';
  if (op.state === 'queued') return 'Getting ready…';
  if (op.state === 'pending_device') return 'Waiting for the device to answer…';
  if (op.stage) return `${humanize(op.stage)}…`;
  return 'Working…';
}

export function isWaitingForWake(op: Pick<SetupOperation, 'stage_detail'> | null | undefined): boolean {
  return !!op?.stage_detail && /wake/i.test(op.stage_detail);
}

// ── errors in plain words ──────────────────────────────────────────────────

const ERROR_TEXT: Record<string, string> = {
  simple_setup_disabled: 'Simple hardware setup is turned off on this server.',
  setup_code_invalid: 'That setup code is not valid. Check the label and try again.',
  no_gateway: 'Connect a gateway first.',
  gateway_required: 'Choose which gateway to use.',
  gateway_offline: 'The gateway is offline. Plug it in and make sure Cremind Connect is running on its computer.',
  no_ready_bridge: 'Add a bridge first, and wait until it shows Ready.',
  device_owned: 'This device already belongs to another Cremind or profile. It has to be removed there (or reset) before it can be added here.',
  candidate_not_eligible: 'That bridge cannot take this tag right now. Choose another one.',
  bridge_full: 'That bridge has no room for more tags. Choose another bridge, or remove a tag from it first.',
  session_expired: 'This setup took too long and has expired. Start again.',
  not_approved: 'Approve the connection in the Cremind Connect window first.',
  already_redeemed: 'Cremind Connect is already finishing this setup, so it can no longer be cancelled.',
  fontpack_mismatch: 'This bridge needs a font update: connect it to this computer with USB, then try again.',
  v1_firmware: 'This device has older software that simple setup does not support.',
  grant_refused: 'The device refused the change. Try again; if it keeps happening, reset the device.',
  timeout: 'The device did not answer in time. Keep it close and try again.',
  cancelled: 'Setup was cancelled.',
  recovery_key_unavailable: 'This server cannot recover the devices because its recovery key is missing. Restore it from a backup first.',
  wrong_gateway: 'The gateway plugged into this computer is not the one being recovered. Plug in the right gateway and try again.',
  already_bound: 'Another copy of Cremind Connect already picked up this setup. Start again.',
  idempotency_key_reused: 'Something changed while retrying. Try again.',
  // Codes the server adds beyond setup-api.md.
  gateway_paused: 'The gateway is paused. Resume it first.',
  discovery_closed: 'This search has ended. Search again.',
  device_not_found: 'That device was not found. It may have been removed meanwhile.',
  connection_not_found: 'That gateway is no longer connected. Choose another one.',
  session_not_found: 'This setup no longer exists. Start again.',
  session_mismatch: 'Confirm this setup in the browser window that started it, or start again here.',
  nothing_to_recover: 'This gateway has not finished connecting yet, so there is nothing to recover.',
  removal_pending: 'This gateway is being removed.',
};

/**
 * A refusal or failure in plain words. Known codes get our own sentence (the
 * server's may name internals); an unknown one falls back to the server's
 * message, then to a generic line.
 */
export function setupErrorMessage(
  code: string | null | undefined,
  opts: { role?: 'gateway' | 'bridge' | 'tag'; fallback?: string | null } = {},
): string {
  const role = opts.role;
  switch (code) {
    case 'already_paired':
      return `This ${role ?? 'device'} is already set up in this profile.`;
    case 'setup_code_wrong_role':
      return role === 'tag'
        ? 'This is a bridge\'s code. Use Add bridge for bridges.'
        : role === 'bridge' ? 'This is a tag\'s code. Use Add tag for tags.' : 'This code is for another kind of device.';
    case 'setup_code_rejected':
      return `The ${role ?? 'device'} did not accept this code. Check that the label belongs to this ${role ?? 'device'}, then try again.`;
    case 'not_found':
      if (role === 'bridge') return 'The bridge was not found. Check that it is plugged in and close to the gateway, then try again.';
      if (role === 'tag') return 'The tag was not found. Tags check in about every 30 seconds: keep it close to a bridge and try again.';
      return 'That was not found. It may have been removed meanwhile.';
    default:
      if (code && ERROR_TEXT[code]) return ERROR_TEXT[code];
      return (opts.fallback || '').trim() || 'Something went wrong. Try again.';
  }
}

// ── setups still running (resume after a refresh) ──────────────────────────

export type PendingSetupKind = 'connect' | 'recover' | 'discovery' | 'pair_bridge' | 'pair_tag' | 'recover_gateway';

export interface PendingSetup {
  kind: PendingSetupKind;
  id: string;
  title: string;
  detail: string;
}

const SESSION_WAIT: Record<string, string> = {
  waiting_for_connect: 'Waiting for Cremind Connect to open',
  waiting_for_approval: 'Waiting for approval in the Cremind Connect window',
  waiting_for_confirmation: 'Waiting for you to confirm the words',
  redeeming: 'Finishing the connection',
  connecting: 'Connecting to the gateway',
};

/** The setups the server says are still running, as banners with a Continue. */
export function pendingSetups(sessions: SetupSession[], operations: SetupOperation[]): PendingSetup[] {
  const out: PendingSetup[] = [];
  for (const s of sessions) {
    if (isSessionTerminal(s.state) || (s.operation !== 'connect_gateway' && s.operation !== 'recover')) continue;
    out.push({
      kind: s.operation === 'recover' ? 'recover' : 'connect',
      id: s.id,
      title: s.operation === 'recover' ? 'Recovering a gateway on this computer' : 'Connecting a gateway',
      detail: SESSION_WAIT[s.state] ?? humanize(s.state),
    });
  }
  const recoverSessions = out.some((p) => p.kind === 'recover');
  for (const op of operations) {
    if (isOperationTerminal(op.state)) continue;
    const detail = operationProgressLabel(op);
    if (op.kind === 'pair_tag') out.push({ kind: 'pair_tag', id: op.id, title: 'Adding a tag', detail });
    else if (op.kind === 'pair_bridge') out.push({ kind: 'pair_bridge', id: op.id, title: 'Adding a bridge', detail });
    else if (op.kind === 'discovery') out.push({ kind: 'discovery', id: op.id, title: 'Looking for a device', detail });
    else if (op.kind === 'recover_gateway' && !recoverSessions) {
      out.push({ kind: 'recover_gateway', id: op.id, title: 'Recovering a gateway on this computer', detail });
    }
  }
  return out;
}

// ── Cremind Connect installers ─────────────────────────────────────────────

export type ClientOs = 'windows' | 'macos' | 'linux' | 'other';
export interface ClientPlatform { os: ClientOs; arch: 'arm64' | 'x64' | null }

interface NavigatorLike {
  userAgent?: string;
  platform?: string;
  userAgentData?: { platform?: string } | null;
}

/** The browser's OS (and architecture where the browser says it). */
export function detectClientPlatform(nav: NavigatorLike | null | undefined): ClientPlatform {
  const hint = `${nav?.userAgentData?.platform ?? ''} ${nav?.platform ?? ''} ${nav?.userAgent ?? ''}`.toLowerCase();
  if (/android|iphone|ipad|ipod|cros/.test(hint)) return { os: 'other', arch: null };
  if (/win/.test(hint)) return { os: 'windows', arch: /arm64|aarch64/.test(hint) ? 'arm64' : 'x64' };
  if (/mac/.test(hint)) return { os: 'macos', arch: null }; // Safari and Firefox never say which chip
  if (/linux|x11/.test(hint)) return { os: 'linux', arch: /aarch64|arm/.test(hint) ? 'arm64' : 'x64' };
  return { os: 'other', arch: null };
}

export interface InstallerChoice { key: ConnectDownloadKey; label: string; url: string }

const INSTALLER_LABEL: Record<ConnectDownloadKey, string> = {
  'windows-x64': 'Windows',
  'macos-arm64': 'macOS (Apple silicon)',
  'macos-x64': 'macOS (Intel)',
  'linux-x64-deb': 'Linux (.deb, for Ubuntu and Debian)',
  'linux-x64-tar': 'Linux (.tar.gz)',
};
const INSTALLER_ORDER: ConnectDownloadKey[] = ['windows-x64', 'macos-arm64', 'macos-x64', 'linux-x64-deb', 'linux-x64-tar'];

/** The installers for this computer first; the rest under "Other systems". */
export function installerChoices(
  downloads: ConnectDownloads | null | undefined,
  platform: ClientPlatform,
): { recommended: InstallerChoice[]; others: InstallerChoice[] } {
  const all: InstallerChoice[] = [];
  for (const key of INSTALLER_ORDER) {
    const link = downloads?.downloads?.[key];
    if (link?.url) all.push({ key, label: INSTALLER_LABEL[key], url: link.url });
  }
  let want: ConnectDownloadKey[] = [];
  if (platform.os === 'windows') want = ['windows-x64'];
  else if (platform.os === 'macos') {
    want = platform.arch === 'arm64' ? ['macos-arm64'] : platform.arch === 'x64' ? ['macos-x64'] : ['macos-arm64', 'macos-x64'];
  } else if (platform.os === 'linux') want = ['linux-x64-deb', 'linux-x64-tar'];
  return {
    recommended: all.filter((c) => want.includes(c.key)),
    others: all.filter((c) => !want.includes(c.key)),
  };
}
