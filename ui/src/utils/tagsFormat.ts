// Pure helpers for the Cremind Tag pages: labels, pill colours, icon names and
// the settings form's inherit/override model. No Vue, no fetch — so they are
// cheap to reuse from every tags component and to test under node:test.
import type {
  TagDelivery, TagEffectiveOptions, TagOptions, TagOptionsPatch, TagRoute,
} from '../services/tagsApi';

export type PillType = 'primary' | 'success' | 'info' | 'warning' | 'danger';

// ── devices ────────────────────────────────────────────────────────────────

const DEVICE_STATUS: Record<string, { label: string; type: PillType }> = {
  ok: { label: 'ok', type: 'success' },
  pending: { label: 'pending', type: 'warning' },
  assigning: { label: 'assigning', type: 'warning' },
  offline: { label: 'offline', type: 'info' },
  error: { label: 'error', type: 'danger' },
  // Its screen clear failed or expired 3 times; an admin must claim or release it again.
  clear_failed: { label: 'clear failed', type: 'danger' },
  // Its bridge refused the assignment (full) and it was detached; assign it elsewhere or release it.
  assign_failed: { label: 'assign failed', type: 'danger' },
  unclaimed: { label: 'unclaimed', type: 'info' },
};

export function deviceStatusPill(status: string | null | undefined): { label: string; type: PillType } {
  const key = status || 'unclaimed';
  return DEVICE_STATUS[key] ?? { label: key, type: 'info' };
}

/** Mirrors ``app/tags/projection.py`` ``BATTERY_LOW_MV``. */
export const BATTERY_LOW_MV = 2400;

export function formatBattery(mv: number | null | undefined): string {
  if (mv == null) return '—';
  return `${(mv / 1000).toFixed(2)} V`;
}

export function batteryIcon(mv: number | null | undefined): string {
  if (mv == null) return 'mdi:battery-unknown';
  if (mv < BATTERY_LOW_MV) return 'mdi:battery-alert-variant-outline';
  if (mv < 2700) return 'mdi:battery-30';
  if (mv < 2900) return 'mdi:battery-60';
  return 'mdi:battery';
}

/** Statuses only an admin action (claim / assign / release) clears. */
export const STUCK_STATUSES = ['clear_failed', 'assign_failed'] as const;

export function isStuck(status: string | null | undefined): boolean {
  return !!status && (STUCK_STATUSES as readonly string[]).includes(status);
}

export const CAPACITY_NOTE = 'A released tag still holds its slot: a slot frees only when the tag moves '
  + 'to another bridge or is forgotten.';

/** A bridge's assignment-table use, for the hardware page and bridge pickers.
 *  With `forTagId`, the count leaves that tag out (the server does the same:
 *  keeping a tag on its own bridge never needs a new slot). */
export function bridgeCapacity(
  bridge: { id: string; max_tags?: number | null; assigned_count?: number },
  forTagId?: { id: string; bridge_device_id: string | null } | null,
): { assigned: number; max: number | null; full: boolean; label: string; tooltip: string } {
  const max = typeof bridge.max_tags === 'number' ? bridge.max_tags : null;
  const all = bridge.assigned_count ?? 0;
  const assigned = forTagId && forTagId.bridge_device_id === bridge.id ? Math.max(0, all - 1) : all;
  const full = max !== null && assigned >= max;
  const tags = (n: number) => (n === 1 ? '1 tag' : `${n} tags`);
  const label = max === null ? tags(all) : `${all} / ${max} tags`;
  const tooltip = max === null
    ? `${tags(all)} assigned; this bridge has not reported how many it can hold. ${CAPACITY_NOTE}`
    : `${all} of the ${max} tags this bridge can hold${full ? ' — full' : ''}. ${CAPACITY_NOTE}`;
  return { assigned, max, full, label, tooltip };
}

export function formatRssi(rssi: number | null | undefined): string {
  return rssi == null ? '—' : `${rssi} dBm`;
}

export function panelLabel(d: { width: number | null; height: number | null; planes?: number | null }): string {
  if (!d.width || !d.height) return '';
  const colours = d.planes && d.planes > 1 ? ` · ${d.planes} colours` : '';
  return `${d.width}×${d.height}${colours}`;
}

export function deviceTitle(d: { name?: string | null; hw_id: string }): string {
  return (d.name || '').trim() || d.hw_id;
}

// ── deliveries ─────────────────────────────────────────────────────────────

/** In order; the last one is also a terminal outcome (app/tags/storage.py). */
export const DELIVERY_STAGES = [
  'queued', 'companion_accepted', 'gateway_received', 'bridge_received',
  'transferring', 'refreshing', 'displayed',
] as const;
export const TERMINAL_STAGES = ['displayed', 'superseded', 'expired', 'cancelled', 'failed', 'uncertain'] as const;

const STAGE_LABEL: Record<string, string> = {
  queued: 'queued',
  companion_accepted: 'accepted by companion',
  gateway_received: 'at gateway',
  bridge_received: 'at bridge',
  transferring: 'transferring',
  refreshing: 'refreshing screen',
  displayed: 'displayed',
  superseded: 'superseded',
  expired: 'expired',
  cancelled: 'cancelled',
  failed: 'failed',
  uncertain: 'uncertain',
};

export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return '—';
  return STAGE_LABEL[stage] ?? stage.replace(/_/g, ' ');
}

export function stagePillType(stage: string | null | undefined): PillType {
  switch (stage) {
    case 'displayed': return 'success';
    case 'failed': return 'danger';
    case 'uncertain': return 'warning';
    case 'superseded':
    case 'expired':
    case 'cancelled':
    case 'queued':
      return 'info';
    default:
      return 'primary';
  }
}

export function isTerminalStage(stage: string | null | undefined): boolean {
  return !!stage && (TERMINAL_STAGES as readonly string[]).includes(stage);
}

/** Stage timeline rows for the drawer: every forward stage in order (reached
 *  or not), then the terminal outcome when it is not `displayed`. */
export function stageTimeline(d: Pick<TagDelivery, 'stage' | 'stage_times' | 'created_at'>):
  { stage: string; at: number | null; reached: boolean; current: boolean }[] {
  const times = d.stage_times || {};
  const rows: { stage: string; at: number | null; reached: boolean; current: boolean }[] = DELIVERY_STAGES.map((stage) => ({
    stage,
    at: stage === 'queued' ? (times.queued ?? d.created_at) : (times[stage] ?? null),
    reached: stage === 'queued' || times[stage] != null || d.stage === stage,
    current: d.stage === stage,
  }));
  if (d.stage && !(DELIVERY_STAGES as readonly string[]).includes(d.stage)) {
    rows.push({ stage: d.stage, at: times[d.stage] ?? null, reached: true, current: true });
  }
  return rows;
}

/** The confirm text for cancelling a delivery that has not finished. Past
 *  `queued` the companion already holds the card; cancelling still works —
 *  Cremind tells it to drop the card. */
export function cancelPrompt(d: Pick<TagDelivery, 'stage' | 'kind' | 'card'>): string {
  const what = `"${d.card?.title || kindLabel(d.kind)}"`;
  if (d.stage === 'queued') return `Cancel ${what}? It has not left Cremind yet, so it will not reach the tag.`;
  return `Cancel ${what}? The companion already has it; Cremind tells it to drop the card. `
    + 'If the tag is already redrawing with it, the card goes at the next redraw.';
}

export function cancelledMessage(res: { resolved: unknown }): string {
  return res.resolved ? 'Cancelled — the companion was told to drop the card' : 'Delivery cancelled';
}

export const TIMING_PARTS = [
  { key: 'wake_ms', label: 'Wake' },
  { key: 'mesh_ms', label: 'Mesh' },
  { key: 'transfer_ms', label: 'Transfer' },
  { key: 'refresh_ms', label: 'Refresh' },
] as const;

export function formatMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

// ── commands ───────────────────────────────────────────────────────────────

const COMMAND_STATUS: Record<string, { label: string; type: PillType }> = {
  queued: { label: 'queued', type: 'info' },
  claimed: { label: 'running', type: 'warning' },
  succeeded: { label: 'succeeded', type: 'success' },
  failed: { label: 'failed', type: 'danger' },
  expired: { label: 'expired', type: 'info' },
  cancelled: { label: 'cancelled', type: 'info' },
};

export function commandStatusPill(status: string | null | undefined): { label: string; type: PillType } {
  const key = status || 'queued';
  return COMMAND_STATUS[key] ?? { label: key, type: 'info' };
}

export function isCommandActive(status: string | null | undefined): boolean {
  return status === 'queued' || status === 'claimed';
}

const COMMAND_LABEL: Record<string, string> = {
  scan_unprovisioned: 'Scan for bridges',
  provision_bridge: 'Provision bridge',
  configure_bridge: 'Configure bridge',
  remove_bridge: 'Remove bridge',
  identify: 'Identify',
  refresh_tag: 'Refresh tag',
  install_fontpack: 'Install font pack',
  collect_diagnostics: 'Collect diagnostics',
  assign_tag: 'Assign tag',
  clear_tag: 'Clear tag',
};

export function commandLabel(kind: string): string {
  return COMMAND_LABEL[kind] ?? kind.replace(/_/g, ' ');
}

/** Pull the bridges a finished `scan_unprovisioned` found out of its result.
 *  The result shape is the companion's; accept the likely spellings. */
export function scanResults(result: any): { uuid: string; rssi: number | null; name: string | null }[] {
  const list = Array.isArray(result) ? result
    : Array.isArray(result?.devices) ? result.devices
      : Array.isArray(result?.unprovisioned) ? result.unprovisioned
        : Array.isArray(result?.found) ? result.found
          : Array.isArray(result?.bridges) ? result.bridges
            : [];
  const out: { uuid: string; rssi: number | null; name: string | null }[] = [];
  for (const item of list) {
    if (typeof item === 'string' && item.trim()) {
      out.push({ uuid: item.trim(), rssi: null, name: null });
    } else if (item && typeof item === 'object') {
      const uuid = String(item.uuid ?? item.id ?? item.hw_id ?? '').trim();
      if (!uuid) continue;
      out.push({
        uuid,
        rssi: typeof item.rssi === 'number' ? item.rssi : null,
        name: typeof item.name === 'string' && item.name ? item.name : null,
      });
    }
  }
  return out;
}

// ── card icons ─────────────────────────────────────────────────────────────

// The tag's built-in icon set (app/tags/cards.py ICONS, Material names) drawn
// with the closest mdi glyph so the picker shows what each one means.
const ICON_GLYPH: Record<string, string> = {
  info: 'mdi:information-outline',
  check_circle: 'mdi:check-circle-outline',
  error: 'mdi:alert-circle-outline',
  warning: 'mdi:alert-outline',
  help: 'mdi:help-circle-outline',
  chat: 'mdi:chat-outline',
  task: 'mdi:clipboard-check-outline',
  schedule: 'mdi:clock-outline',
  event: 'mdi:calendar-blank-outline',
  notifications: 'mdi:bell-outline',
  sync: 'mdi:sync',
  battery_full: 'mdi:battery',
  battery_low: 'mdi:battery-low',
  wifi_off: 'mdi:wifi-off',
  link_off: 'mdi:link-off',
  description: 'mdi:file-document-outline',
  bolt: 'mdi:lightning-bolt-outline',
  push_pin: 'mdi:pin-outline',
  bar_chart: 'mdi:chart-bar',
  approval: 'mdi:check-decagram-outline',
  hourglass: 'mdi:timer-sand',
  bug_report: 'mdi:bug-outline',
  folder: 'mdi:folder-outline',
  person: 'mdi:account-outline',
};

export function cardIconGlyph(icon: string | null | undefined): string {
  return (icon && ICON_GLYPH[icon]) || 'mdi:card-text-outline';
}

export function iconLabel(icon: string): string {
  return icon.replace(/_/g, ' ');
}

// ── card kinds (routing) ───────────────────────────────────────────────────

const KIND_INFO: Record<string, { label: string; hint: string }> = {
  notification: { label: 'Notifications', hint: 'Notifications, and people subscribing to a channel' },
  task_outcome: { label: 'Results', hint: 'A reply or an automation run finished or failed' },
  needs_input: { label: 'Needs your input', hint: 'A chat or an automation run is waiting for your answer' },
  excerpt: { label: 'Reply excerpts', hint: 'The start of a finished reply (only while excerpts are on)' },
  progress: { label: 'Progress', hint: 'An automation run\'s progress, redrawn at the cadence below' },
  health: { label: 'Channel health', hint: 'A channel stopped or was unlinked' },
  indexing_problem: { label: 'Indexing problems', hint: 'My Documents could not index a file' },
  calendar: { label: 'Calendar', hint: 'Upcoming calendar events' },
  automation: { label: 'Automations', hint: 'An automation failed, and upcoming runs' },
  usage: { label: 'Usage', hint: 'Token usage and cost summaries' },
  tag_diagnostics: { label: 'Tag diagnostics', hint: 'A tag\'s battery is low or it stopped answering' },
  pinned_note: { label: 'Pinned note', hint: 'A note you put on a tag' },
  clear: { label: 'Clear screen', hint: 'Blanks the screen' },
  resolved: { label: 'Resolved', hint: 'Retires an earlier card' },
};

export function kindLabel(kind: string): string {
  return KIND_INFO[kind]?.label ?? kind.replace(/_/g, ' ');
}

export function kindHint(kind: string): string {
  return KIND_INFO[kind]?.hint ?? '';
}

// ── settings form (inherit / override) ─────────────────────────────────────

export type ScalarOptionKey = 'layout' | 'show_excerpts' | 'qr_links' | 'progress_cadence_s' | 'language' | 'timezone';
export const SCALAR_OPTION_KEYS: ScalarOptionKey[] = [
  'layout', 'show_excerpts', 'qr_links', 'progress_cadence_s', 'language', 'timezone',
];

/** The form: every key present; ``null`` = inherit the layer below. */
export interface TagOptionsDraft {
  layout: string | null;
  show_excerpts: boolean | null;
  qr_links: boolean | null;
  progress_cadence_s: number | null;
  language: string | null;
  timezone: string | null;
  routes: Record<string, TagRoute | null>;
}

/** What a key inherits when this layer does not set it: ``lower`` (the admin
 *  defaults, for a profile; nothing, for the defaults themselves) over the
 *  server's ``builtin`` layer (from GET /api/tags/settings or …/defaults). */
export function inheritedOptions(lower: TagOptions | null | undefined,
  builtin: TagEffectiveOptions): TagEffectiveOptions {
  const base = lower || {};
  return {
    layout: base.layout ?? builtin.layout,
    show_excerpts: base.show_excerpts ?? builtin.show_excerpts,
    qr_links: base.qr_links ?? builtin.qr_links,
    progress_cadence_s: base.progress_cadence_s ?? builtin.progress_cadence_s,
    language: base.language ?? builtin.language,
    timezone: base.timezone ?? builtin.timezone,
    routes: { ...builtin.routes, ...(base.routes || {}) },
  };
}

export function draftFromOptions(options: TagOptions | null | undefined, kinds: string[]): TagOptionsDraft {
  const o = options || {};
  const routes: Record<string, TagRoute | null> = {};
  for (const kind of kinds) {
    const r = o.routes?.[kind];
    routes[kind] = r == null ? null : Array.isArray(r) ? [...r] : r;
  }
  return {
    layout: o.layout ?? null,
    show_excerpts: o.show_excerpts ?? null,
    qr_links: o.qr_links ?? null,
    progress_cadence_s: o.progress_cadence_s ?? null,
    language: o.language ?? null,
    timezone: o.timezone ?? null,
    routes,
  };
}

/** The ``options`` object to PUT: only the keys this layer overrides. */
export function optionsFromDraft(draft: TagOptionsDraft): TagOptions {
  const out: TagOptions = {};
  for (const key of SCALAR_OPTION_KEYS) {
    const v = draft[key];
    if (v !== null && v !== undefined) (out as any)[key] = key === 'language' && typeof v === 'string' ? v.trim() : v;
  }
  const routes: Record<string, TagRoute> = {};
  for (const [kind, route] of Object.entries(draft.routes)) {
    if (route !== null && route !== undefined) routes[kind] = Array.isArray(route) ? [...route] : route;
  }
  if (Object.keys(routes).length) out.routes = routes;
  return out;
}

/** The PATCH for what changed between two drafts of the same layer: a
 *  changed key carries its new value, or ``null`` when it went back to
 *  inheriting; ``routes`` only the kinds that changed. Keys nobody touched are
 *  left out, so a concurrent edit elsewhere (the CLI) is not overwritten. */
export function draftPatch(saved: TagOptionsDraft, current: TagOptionsDraft): TagOptionsPatch {
  const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  const before = optionsFromDraft(saved);
  const after = optionsFromDraft(current);
  const out: TagOptionsPatch = {};
  for (const key of SCALAR_OPTION_KEYS) {
    if (!same(before[key], after[key])) (out as any)[key] = after[key] ?? null;
  }
  const kinds = new Set([...Object.keys(before.routes || {}), ...Object.keys(after.routes || {})]);
  const routes: Record<string, TagRoute | null> = {};
  for (const kind of kinds) {
    const a = before.routes?.[kind];
    const b = after.routes?.[kind];
    if (!same(a, b)) routes[kind] = b ?? null;
  }
  if (Object.keys(routes).length) out.routes = routes;
  return out;
}

/** Why the draft cannot be saved yet, or '' when it can. */
export function draftProblem(draft: TagOptionsDraft): string {
  for (const [kind, route] of Object.entries(draft.routes)) {
    if (Array.isArray(route) && route.length === 0) {
      return `Pick at least one tag for "${kindLabel(kind)}", or choose None.`;
    }
  }
  const cadence = draft.progress_cadence_s;
  if (cadence !== null && (!Number.isFinite(cadence) || cadence < 60 || cadence > 3600)) {
    return 'Progress cadence must be between 60 and 3600 seconds.';
  }
  if (draft.language !== null && !draft.language.trim()) {
    return 'Enter a language tag such as "en", or inherit the default.';
  }
  return '';
}

export function routeSummary(route: TagRoute | null | undefined, nameOf: (id: string) => string = (id) => id): string {
  if (route === 'all') return 'All tags';
  if (route === 'none') return 'None';
  if (Array.isArray(route)) return route.length ? route.map(nameOf).join(', ') : 'No tags';
  return '—';
}

export function formatCadence(seconds: number | null | undefined): string {
  if (seconds == null) return '—';
  if (seconds % 60 === 0) {
    const m = seconds / 60;
    return m === 1 ? '1 minute' : `${m} minutes`;
  }
  return `${seconds} s`;
}
