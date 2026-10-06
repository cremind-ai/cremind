// Pure helpers for simple hardware setup (Settings → Tags): plain-language
// labels, and what the dialogs do next for a given server answer. No Vue, no
// fetch.
//
// Wording rule: people see gateways, bridges, tags and computers — never
// serial ports, mesh addresses, epochs, credential ids or internal project
// names. A device id appears only as a short suffix, and only where two
// devices could otherwise look the same. A computer is named the way it
// reports itself ("Office PC"), and what a computer can do comes from its own
// report, never from where this page runs.
import type {
  Discovery, DiscoveryCandidate, GatewayCandidate, GatewayHost, HostOperation, SetupCapacity,
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

/** "3 of 20 tags" — a bridge's (or a gateway's own) room for tags. */
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

export function humanize(code: string | null | undefined): string {
  const text = (code || '').replace(/_/g, ' ').trim();
  return text ? text[0].toUpperCase() + text.slice(1) : '';
}

// ── what can be added now ──────────────────────────────────────────────────

export interface SetupReadiness {
  connected: TagConnection[];
  readyBridges: SetupDevice[];
  /** Connected gateways that take a tag themselves: ready, and reaching tags on their own radio. */
  tagGateways: TagConnection[];
  canAddBridge: boolean;
  addBridgeReason: string;
  canAddTag: boolean;
  addTagReason: string;
}

/**
 * What can be added now. A tag needs something that can reach it: a connected
 * gateway that reaches tags itself (newer gateways do), or a ready bridge (a
 * bridge only carries updates farther). An older gateway reaches no tag
 * itself, so its tags wait for a bridge.
 */
export function setupReadiness(connections: TagConnection[]): SetupReadiness {
  const live = connections.filter((c) => c.status !== 'removal_pending');
  const connected = live.filter((c) => c.status === 'connected' && !c.paused);
  let addBridgeReason = '';
  if (!connected.length) {
    if (!live.length) addBridgeReason = 'Connect a gateway first.';
    else if (live.some((c) => c.status === 'setting_up')) addBridgeReason = 'Wait until the gateway is connected.';
    else if (live.every((c) => c.paused || c.status === 'paused')) addBridgeReason = 'Your gateway is paused. Resume it first.';
    else addBridgeReason = 'Your gateway is offline. Check that it is plugged in and that its computer is on.';
  }
  const tagGateways = connected.filter((c) => c.gateway?.serves_tags === true && c.gateway.state === 'ready');
  const bridges = connected.flatMap((c) => c.bridges).filter((b) => b.state !== 'removal_pending');
  const readyBridges = bridges.filter((b) => b.state === 'ready' && !b.paused);
  let addTagReason = '';
  if (!readyBridges.length && !tagGateways.length) {
    if (!connected.length) addTagReason = addBridgeReason;
    else if (connected.some((c) => c.gateway?.serves_tags === true)) addTagReason = 'Wait until the gateway shows Ready.';
    else if (!bridges.length) addTagReason = 'Your gateway cannot reach tags itself. Add a bridge first.';
    else if (bridges.every((b) => b.paused)) addTagReason = 'Your bridges are paused. Resume one first.';
    else addTagReason = 'Wait until a bridge shows Ready.';
  }
  return {
    connected,
    readyBridges,
    tagGateways,
    canAddBridge: connected.length > 0,
    addBridgeReason,
    canAddTag: readyBridges.length > 0 || tagGateways.length > 0,
    addTagReason,
  };
}

/** Where a tag being added should be, in words: close to "your gateway or one of your bridges". */
export function tagReach(r: Pick<SetupReadiness, 'tagGateways' | 'readyBridges'>): string {
  const gateways = r.tagGateways.length;
  if (gateways && r.readyBridges.length) {
    return gateways > 1 ? 'one of your gateways or bridges' : 'your gateway or one of your bridges';
  }
  if (gateways) return gateways > 1 ? 'one of your gateways' : 'your gateway';
  return 'one of your bridges';
}

/** The gateway or bridge a tag connects through, as the page names it (null: none, e.g. its bridge was removed). */
export function tagParent(
  t: Pick<SetupDevice, 'bridge_id'>,
  c: Pick<TagConnection, 'name' | 'gateway' | 'bridges'>,
): { kind: 'gateway' | 'bridge'; title: string; device: SetupDevice } | null {
  if (!t.bridge_id) return null;
  if (c.gateway?.id && t.bridge_id === c.gateway.id) return { kind: 'gateway', title: connectionTitle(c), device: c.gateway };
  const b = c.bridges.find((x) => x.id === t.bridge_id);
  return b ? { kind: 'bridge', title: setupDeviceTitle(b), device: b } : null;
}

/**
 * A discovery candidate as people know it: the gateway — a bridge's search, or
 * a tag the gateway's own radio heard — by its connection's title (with its
 * computer, for a bridge's choice of gateway), a bridge by its name.
 */
export function candidateTitle(
  c: Pick<DiscoveryCandidate, 'gateway_id' | 'bridge_id' | 'bridge_name' | 'bridge_kind'>,
  role: 'bridge' | 'tag',
  connections: TagConnection[],
): string {
  const conn = connections.find((x) => x.id === c.gateway_id);
  if (role === 'bridge') {
    if (!conn) return 'A gateway';
    return conn.computer?.name ? `${connectionTitle(conn)} on ${conn.computer.name}` : connectionTitle(conn);
  }
  if (c.bridge_kind === 'gateway') return (c.bridge_name || '').trim() || (conn ? connectionTitle(conn) : 'Your gateway');
  if (c.bridge_name) return c.bridge_name;
  const b = connections.flatMap((x) => x.bridges).find((x) => x.id === c.bridge_id);
  return b ? setupDeviceTitle(b) : 'A bridge';
}

// ── setup sessions ─────────────────────────────────────────────────────────

export const SESSION_TERMINAL = ['completed', 'cancelled', 'expired', 'failed'] as const;

export function isSessionTerminal(state: string | null | undefined): boolean {
  return !!state && (SESSION_TERMINAL as readonly string[]).includes(state);
}

/** Setting up a gateway computer: where an `enroll_host` session is, as the dialog shows it. */
export type EnrollStep = 'open' | 'approve' | 'confirm' | 'finish' | 'done' | 'failed';

export function enrollStep(session: Pick<SetupSession, 'state'> | null | undefined): EnrollStep {
  switch (session?.state) {
    case undefined:
    case null:
    case 'waiting_for_connect': return 'open';
    case 'waiting_for_approval': return 'approve';
    case 'waiting_for_confirmation': return 'confirm';
    case 'redeeming': return 'finish';
    case 'completed': return 'done';
    default: return isSessionTerminal(session?.state) ? 'failed' : 'open';
  }
}

/** Why an `enroll_host` session ended without a computer, in words. */
export function enrollFailure(session: Pick<SetupSession, 'state' | 'error'> | null | undefined): string {
  switch (session?.state) {
    case 'expired': return 'The setup took longer than five minutes and has expired. Start again.';
    case 'cancelled': return 'The setup was cancelled. Nothing was changed.';
    default:
      return setupErrorMessage(session?.error?.code, {
        fallback: session?.error?.message || 'The computer could not finish the setup. Start again.',
      });
  }
}

// ── gateway computers ──────────────────────────────────────────────────────

/** A computer by its own name, or words that still read well in a sentence. */
export function hostName(h: Pick<GatewayHost, 'name' | 'kind'> | null | undefined): string {
  const name = (h?.name || '').trim();
  if (name) return name;
  return h?.kind === 'server' ? 'the computer Cremind runs on' : 'this computer';
}

/** "Plug the gateway into **Office PC**, where Cremind is running." — split around the name so it can be bold. */
export function plugInstruction(
  h: Pick<GatewayHost, 'name' | 'kind'> | null | undefined,
  what = 'the gateway',
): { before: string; name: string; after: string } {
  return { before: `Plug ${what} into `, name: hostName(h), after: ', where Cremind is running.' };
}

/** "The Cremind server" / "Cremind app": what runs the gateways there. */
export function hostKindLabel(kind: string | null | undefined): string {
  return kind === 'server' ? 'Cremind server' : kind === 'desktop' ? 'Cremind app' : 'Computer';
}

export function hostStatePill(h: Pick<GatewayHost, 'online' | 'state' | 'readiness'>): Pill {
  if (!h.online) return { label: 'Offline', type: 'info' };
  switch (h.state) {
    case 'running':
      return h.readiness?.state === 'partial'
        ? { label: 'Ready, screens waiting', type: 'warning' }
        : { label: 'Ready', type: 'success' };
    case 'paused': return { label: 'Paused for an update', type: 'info' };
    case 'unavailable': return { label: 'Components needed', type: 'warning' };
    case 'busy_elsewhere': return { label: 'Used by another Cremind', type: 'warning' };
    case 'disabled': return { label: 'Turned off', type: 'info' };
    case 'stalled': return { label: 'Not responding', type: 'warning' };
    case 'failed': return { label: 'Stopped with an error', type: 'danger' };
    case 'stopped': return { label: 'Stopped', type: 'info' };
    default: return { label: 'Starting', type: 'info' };
  }
}

/** The things that can stand between a person and a connected gateway, each with its own words. */
export type HostProblem =
  | 'no_gateway' | 'usb_access_denied' | 'gateway_busy' | 'unsupported_firmware'
  | 'components_unavailable' | 'host_offline' | 'owned_elsewhere' | 'recovery_required';

export interface ProblemText {
  title: string;
  text: string;
  /** The way forward the dialog offers: search again, prepare the components, recover here. */
  action: 'retry' | 'prepare' | 'recover' | null;
}

type HostFacts = Pick<GatewayHost, 'name' | 'kind' | 'platform' | 'usb' | 'access'>;

function usbAccessText(h: HostFacts | null | undefined, name: string): string {
  if (h?.usb?.container) {
    return `Cremind runs in a container on ${name}, so it cannot open USB ports there. Map the gateway's USB `
      + 'device into the container (see "Gateways and containers" in the Cremind guide), then search again.';
  }
  switch (h?.platform) {
    case 'linux':
      return `${name} does not let Cremind open the gateway's USB port. Add the account Cremind runs as to the `
        + '"dialout" group, sign out and back in, then search again.';
    case 'macos':
      return 'macOS refused access to the gateway\'s USB port. Unplug the gateway, plug it back in, then search again.';
    case 'windows':
      return 'Windows refused access to the gateway\'s USB port. Close other programs that may use it, unplug the '
        + 'gateway and plug it back in, then search again.';
    default:
      return `${name} refused access to the gateway's USB port. Unplug the gateway, plug it back in, then search again.`;
  }
}

/** The title, the words and the way forward for a problem on computer `h`. */
export function problemText(problem: HostProblem, h?: HostFacts | null): ProblemText {
  const name = hostName(h);
  switch (problem) {
    case 'no_gateway':
      return {
        title: 'No gateway detected',
        text: `Cremind did not find a gateway on ${name}'s USB ports. Check that it is plugged in with a data `
          + 'cable (some cables only charge), then search again.',
        action: 'retry',
      };
    case 'usb_access_denied':
      return { title: 'USB access denied', text: usbAccessText(h, name), action: 'retry' };
    case 'gateway_busy':
      return {
        title: 'Gateway busy in another application',
        text: `Another program on ${name} is using the gateway: a serial monitor, a firmware tool, or an older `
          + 'Cremind Connect. Close it, then search again.',
        action: 'retry',
      };
    case 'unsupported_firmware':
      return {
        title: 'Unsupported firmware',
        text: 'This gateway runs older firmware that Cremind cannot set up. Update its firmware, then search again.',
        action: 'retry',
      };
    case 'components_unavailable':
      if (h?.kind === 'server' && h.access?.can_manage) {
        return {
          title: 'Required components unavailable',
          text: `${name} is missing the components Cremind needs to drive gateways. Prepare them here; it `
            + 'takes a few minutes.',
          action: 'prepare',
        };
      }
      return {
        title: 'Required components unavailable',
        text: h?.kind === 'server'
          ? `${name} is missing the components Cremind needs to drive gateways. Ask the admin to prepare them in `
            + 'Settings → Tags.'
          : `The Cremind app on ${name} is missing the components it needs to drive gateways. Update the app there, `
            + 'then try again.',
        action: null,
      };
    case 'host_offline':
      return {
        title: 'Computer offline',
        text: `${name} is not reachable right now. Make sure it is on and that Cremind is running there, then try `
          + 'again.',
        action: 'retry',
      };
    case 'owned_elsewhere':
      return {
        title: 'Gateway owned elsewhere',
        text: 'This gateway already belongs to another Cremind or profile. Remove it there, or reset it (hold its '
          + 'button while plugging it in) to set it up here.',
        action: null,
      };
    case 'recovery_required':
      return {
        title: 'Recovery required',
        text: `This gateway is connected through another computer. Recover it here to move it, with its bridges `
          + `and tags, to ${name}.`,
        action: 'recover',
      };
  }
}

/** What keeps a computer from searching right now (null: nothing). */
export function hostBlock(h: GatewayHost): ProblemText | null {
  if (!h.online) return problemText('host_offline', h);
  if (h.state === 'unavailable') return problemText('components_unavailable', h);
  if (h.state === 'running' && h.usb && h.usb.available === false) {
    const text = problemText('usb_access_denied', h);
    return { ...text, text: h.usb.reason ? `${h.usb.reason} ${text.text}` : text.text };
  }
  if (h.state === 'running' || h.state === 'paused') return null;
  if (h.state === 'busy_elsewhere') {
    return {
      title: 'Gateways run in another Cremind',
      text: h.reason || `Another Cremind on ${hostName(h)} drives the gateways. Use that one, or close it.`,
      action: null,
    };
  }
  if (h.state === 'disabled') {
    return { title: 'Hardware setup is off', text: h.reason || 'Hardware setup is turned off on this server.', action: null };
  }
  return {
    title: 'Gateway support is not running',
    text: `${h.reason ? `${h.reason} ` : ''}Restart Cremind on ${hostName(h)} if this does not clear up.`,
    action: 'retry',
  };
}

const ERROR_PROBLEM: Record<string, HostProblem> = {
  gateway_not_found: 'no_gateway',
  usb_access_denied: 'usb_access_denied',
  access_denied: 'usb_access_denied',
  gateway_busy: 'gateway_busy',
  busy: 'gateway_busy',
  unsupported_firmware: 'unsupported_firmware',
  v1_firmware: 'unsupported_firmware',
  components_unavailable: 'components_unavailable',
  host_offline: 'host_offline',
  host_not_running: 'host_offline',
  owned_elsewhere: 'owned_elsewhere',
  recovery_required: 'recovery_required',
};

/** The problem a refusal or a failed search/connection stands for (null: an error of another kind). */
export function errorProblem(code: string | null | undefined): HostProblem | null {
  return (code && ERROR_PROBLEM[code]) || null;
}

const CANDIDATE_ORDER: Record<string, number> = { usable: 0, recovery_required: 1, already_connected: 2 };

export type ScanDecision =
  | { kind: 'searching' }
  | { kind: 'found'; candidates: GatewayCandidate[]; preselect: string | null }
  | { kind: 'problem'; problem: HostProblem }
  | { kind: 'failed'; error: SetupError | null }
  | { kind: 'cancelled' };

/**
 * What the Connect dialog does with a search: keep waiting; show what was
 * found (the free gateways first, the only free one preselected); or, with
 * nothing found, say why — the ports that did not answer name the reason.
 */
export function scanDecision(op: Pick<HostOperation, 'state' | 'candidates' | 'ports' | 'error'>): ScanDecision {
  switch (op.state) {
    case 'cancelled': return { kind: 'cancelled' };
    case 'failed': {
      const problem = errorProblem(op.error?.code);
      return problem ? { kind: 'problem', problem } : { kind: 'failed', error: op.error ?? null };
    }
    case 'succeeded': break;
    default: return { kind: 'searching' };
  }
  const found = [...(op.candidates || [])].sort((a, b) =>
    (CANDIDATE_ORDER[a.state] ?? 9) - (CANDIDATE_ORDER[b.state] ?? 9) || a.short_id.localeCompare(b.short_id));
  if (found.length) {
    const usable = found.filter((c) => c.state === 'usable');
    return { kind: 'found', candidates: found, preselect: usable.length === 1 ? usable[0].id : null };
  }
  const reasons = new Set((op.ports || []).map((p) => p.reason));
  if (reasons.has('no_access')) return { kind: 'problem', problem: 'usb_access_denied' };
  if (reasons.has('busy')) return { kind: 'problem', problem: 'gateway_busy' };
  if (reasons.has('v1_firmware')) return { kind: 'problem', problem: 'unsupported_firmware' };
  return { kind: 'problem', problem: 'no_gateway' };
}

const CANDIDATE_PILL: Record<string, Pill> = {
  usable: { label: 'Ready to connect', type: 'success' },
  already_connected: { label: 'Connected here', type: 'info' },
  recovery_required: { label: 'Recovery required', type: 'warning' },
  owned_elsewhere: { label: 'Owned elsewhere', type: 'danger' },
  unsupported_firmware: { label: 'Unsupported firmware', type: 'warning' },
  busy: { label: 'In use', type: 'warning' },
  access_denied: { label: 'No USB access', type: 'warning' },
  not_a_gateway: { label: 'Not a gateway', type: 'info' },
  device_rejected: { label: 'Refused', type: 'danger' },
  no_answer: { label: 'No answer', type: 'info' },
};

export interface CandidateView {
  title: string;
  detail: string;
  pill: Pill;
  /** What the person can do with it here. */
  action: 'connect' | 'recover' | null;
  problem: HostProblem | null;
}

export function candidateView(c: GatewayCandidate): CandidateView {
  const problem = c.state === 'usable' || c.state === 'already_connected' ? null : errorProblem(c.state);
  const pill = CANDIDATE_PILL[c.state] ?? { label: humanize(c.state), type: 'info' };
  // The server's sentence, unless it only says what the chip already says.
  const message = (c.message || '').trim();
  const repeats = message.replace(/[.\s]+$/, '').toLowerCase() === pill.label.toLowerCase();
  return {
    title: `Gateway ${shortSuffix(c.short_id)}`.trim(),
    detail: [repeats ? '' : message, c.fw ? `Firmware ${c.fw}` : ''].filter(Boolean).join(' · '),
    pill,
    action: c.state === 'usable' ? 'connect' : c.state === 'recovery_required' ? 'recover' : null,
    problem,
  };
}

/** The steps a connection goes through, as the dialog shows them. */
export const CONNECT_STAGES = ['Finding the gateway', 'Checking it', 'Setting it up', 'Connecting'] as const;

/** The index into CONNECT_STAGES (its length once connected). */
export function connectStageIndex(op: Pick<HostOperation, 'state' | 'stage'> | null | undefined): number {
  if (!op) return 0;
  if (op.state === 'succeeded') return CONNECT_STAGES.length;
  switch (op.stage) {
    case 'checking': return 1;
    case 'registering': return 2;
    case 'claiming': return 3;
    default: return 0;
  }
}

/** One line on where a search, a connection or a preparation is. */
export function hostOpProgressLabel(op: Pick<HostOperation, 'kind' | 'state' | 'stage' | 'stage_detail'>): string {
  if (op.stage_detail) return op.stage_detail.replace(/…$/, '').trim() + '…';
  if (op.state === 'queued') {
    return op.kind === 'host_prepare' ? 'Getting ready…' : 'Waiting for the computer to pick this up…';
  }
  if (op.kind === 'host_scan') return 'Looking at the USB ports…';
  if (op.kind === 'host_prepare') return 'Preparing the components…';
  return `${CONNECT_STAGES[Math.min(connectStageIndex(op), CONNECT_STAGES.length - 1)]}…`;
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
  // A recovery started from an older Cremind Connect waits for its computer.
  if (op.state === 'waiting_for_connect') return 'Waiting for the gateway\'s computer';
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
  gateway_offline: 'The gateway is offline. Check that it is plugged in and that its computer is on.',
  no_ready_bridge: 'Your gateway cannot reach tags itself. Add a bridge first, and wait until it shows Ready.',
  device_owned: 'This device already belongs to another Cremind or profile. It has to be removed there (or reset) before it can be added here.',
  candidate_not_eligible: 'That gateway or bridge cannot take this tag right now. Choose another one.',
  bridge_full: 'That gateway or bridge has no room for more tags. Choose another one, or remove a tag from it first.',
  session_expired: 'This setup took too long and has expired. Start again.',
  fontpack_mismatch: 'This bridge needs a font update: connect it to a gateway computer with USB, then try again.',
  v1_firmware: 'This device has older software that simple setup does not support.',
  grant_refused: 'The device refused the change. Try again; if it keeps happening, reset the device.',
  timeout: 'The device did not answer in time. Keep it close and try again.',
  cancelled: 'Setup was cancelled.',
  recovery_key_unavailable: 'This server cannot recover the devices because its recovery key is missing. Restore it from a backup first.',
  wrong_gateway: 'The gateway plugged in is not the one being recovered. Plug in the right gateway and try again.',
  idempotency_key_reused: 'Something changed while retrying. Try again.',
  // Codes the server adds beyond setup-api.md.
  gateway_paused: 'The gateway is paused. Resume it first.',
  discovery_closed: 'This search has ended. Search again.',
  device_not_found: 'That device was not found. It may have been removed meanwhile.',
  connection_not_found: 'That gateway is no longer connected. Choose another one.',
  session_not_found: 'This setup no longer exists. Start again.',
  nothing_to_recover: 'This gateway is not waiting to be recovered on that computer.',
  removal_pending: 'This gateway is being removed.',
  // Gateway computers.
  candidate_expired: 'That search result is too old. Search again.',
  candidate_not_found: 'That gateway is not among this computer\'s recent search results. Search again.',
  gateway_changed: 'Another gateway answered on that USB port. Search again.',
  claim_failed: 'The gateway could not be claimed. Unplug it, plug it back in, and try again.',
  already_connected: 'This gateway is already connected.',
  already_claiming: 'The gateway is being claimed, so this can no longer be cancelled. Remove it afterwards to undo.',
  host_access_denied: 'This profile may not use that computer\'s USB ports. Ask the admin to allow it.',
  host_not_found: 'That computer is no longer available.',
  host_busy: 'Another Cremind on that computer drives the gateways.',
  host_not_shareable: 'A computer set up by a profile stays that profile\'s.',
  admin_required: 'Only the admin profile can do this.',
  host_error: 'The computer could not finish. Try again.',
  prepare_failed: 'Preparing the components did not finish. Try again.',
  prepared_on_that_computer: 'Components are prepared on the computer itself.',
  host_not_removable: 'The computer the Cremind server runs on cannot be removed.',
  host_removed: 'That computer was removed.',
  not_approved: 'Approve on the other computer first.',
  declined: 'Setting up the computer was declined on it.',
  not_a_gateway: 'That device is not a gateway.',
  device_rejected: 'The gateway\'s identity does not check out. Unplug it and try again; if this repeats, reset it.',
  // Tags enrolled with the hardware tools (imported by tag id).
  invalid_tag_id: 'A tag id is 8 characters, 0-9 and A-F, as the hardware tools printed it (for example 1A2B3C4D).',
  import_not_allowed: 'Only the owner of the computer your gateway is plugged into can add tags enrolled there (on the server\'s own computer, the admin).',
  not_enrolled_here: 'The hardware tools on your gateway\'s computer have no tag with that id. Plug the gateway into the computer where the tag was enrolled.',
  panel_unsupported: 'This tag\'s display is not supported by its firmware yet. Flash newer tag firmware and enroll it again.',
};

/**
 * A refusal or failure in plain words. Known codes get our own sentence (the
 * server's may name internals); one of the gateway-computer problems gets its
 * sentence from `problemText`; an unknown code falls back to the server's
 * message, then to a generic line. `near`: what a tag should be close to
 * (`tagReach`), for a tag that was not found.
 */
export function setupErrorMessage(
  code: string | null | undefined,
  opts: {
    role?: 'gateway' | 'bridge' | 'tag'; fallback?: string | null; host?: HostFacts | null; near?: string;
  } = {},
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
      if (role === 'tag') {
        return `The tag was not found. Tags check in about every 30 seconds: keep it close to ${
          opts.near || 'your gateway or one of your bridges'} and try again.`;
      }
      return 'That was not found. It may have been removed meanwhile.';
    default: {
      if (code && ERROR_TEXT[code]) return ERROR_TEXT[code];
      const problem = errorProblem(code);
      if (problem) return problemText(problem, opts.host).text;
      return (opts.fallback || '').trim() || 'Something went wrong. Try again.';
    }
  }
}

// ── setups still running (resume after a refresh) ──────────────────────────

export type PendingSetupKind =
  | 'enroll' | 'connect' | 'move' | 'discovery' | 'pair_bridge' | 'pair_tag' | 'recover_gateway';

export interface PendingSetup {
  kind: PendingSetupKind;
  id: string;
  title: string;
  detail: string;
}

const ENROLL_WAIT: Record<string, string> = {
  waiting_for_connect: 'Waiting for the Cremind app to open the link',
  waiting_for_approval: 'Waiting for approval on the computer',
  waiting_for_confirmation: 'Waiting for you to confirm the words',
  redeeming: 'Finishing',
};

/** The setups the server says are still running, as banners with a Continue. */
export function pendingSetups(
  operations: SetupOperation[], hostOps: HostOperation[] = [], sessions: SetupSession[] = [],
): PendingSetup[] {
  const out: PendingSetup[] = [];
  for (const s of sessions) {
    if (s.operation !== 'enroll_host' || isSessionTerminal(s.state)) continue;
    out.push({ kind: 'enroll', id: s.id, title: 'Setting up a gateway computer',
               detail: ENROLL_WAIT[s.state] ?? humanize(s.state) });
  }
  for (const h of hostOps) {
    if (h.kind !== 'host_connect' || isOperationTerminal(h.state)) continue;
    out.push(h.recover
      ? { kind: 'move', id: h.id, title: 'Moving a gateway to another computer', detail: hostOpProgressLabel(h) }
      : { kind: 'connect', id: h.id, title: 'Connecting a gateway', detail: hostOpProgressLabel(h) });
  }
  for (const op of operations) {
    if (isOperationTerminal(op.state)) continue;
    const detail = operationProgressLabel(op);
    // An import (a tag enrolled with the hardware tools) resumes in Add tag like a pairing.
    if (op.kind === 'pair_tag' || op.kind === 'import_tag') out.push({ kind: 'pair_tag', id: op.id, title: 'Adding a tag', detail });
    else if (op.kind === 'pair_bridge') out.push({ kind: 'pair_bridge', id: op.id, title: 'Adding a bridge', detail });
    else if (op.kind === 'discovery') out.push({ kind: 'discovery', id: op.id, title: 'Looking for a device', detail });
    else if (op.kind === 'recover_gateway') {
      out.push({ kind: 'recover_gateway', id: op.id, title: 'Moving devices to the gateway\'s new computer', detail });
    }
  }
  return out;
}
