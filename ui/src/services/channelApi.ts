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

export interface ChannelCatalogField {
  description: string;
  type: 'string' | 'number' | 'boolean';
  secret?: boolean;
  required?: boolean;
}

export interface ChannelCatalogMode {
  id: string;
  label: string;
  instructions?: string;
  setup_kind?: 'qr' | string;
  /** When ``false``, the catalog declares the mode but the adapter is not
   *  yet shipped. The UI disables the option in pickers; the API rejects
   *  registration with HTTP 400. Default ``true`` when the field is
   *  omitted from the TOML. */
  implemented?: boolean;
  fields?: Record<string, ChannelCatalogField>;
}

export interface ChannelCatalogEntry {
  type: string;
  display_name: string;
  icon?: string;
  supports_bot?: boolean;
  supports_normal?: boolean;
  auth_modes?: string[];
  default_response_mode?: 'detail' | 'normal';
  modes: ChannelCatalogMode[];
  implemented?: boolean;
  /** Whether this platform's adapter can take part in a group chat. Derived
   *  server-side from the adapter classes, so the toggle is only offered where
   *  it can actually do something. */
  supports_group_chats?: boolean;
}

export interface ChannelRow {
  id: string;
  profile: string;
  channel_type: string;
  mode: string;
  auth_mode: 'none' | 'otp' | 'password';
  response_mode: 'detail' | 'normal';
  enabled: boolean;
  config: Record<string, any>;
  state: Record<string, any>;
  status?: 'running' | 'stopped' | 'unlinked';
  created_at: number;
  updated_at: number;
}

/** Token + cost totals for one subscriber's conversation. */
export interface ChannelSenderUsage {
  input_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  total_usd: number;
  request_count: number;
}

export interface ChannelSenderRow {
  id: string;
  channel_id: string;
  sender_id: string;
  display_name: string | null;
  /**
   * Contact's number in canonical digits (E.164 without '+'), when known.
   * Derived automatically for WhatsApp (its sender ids are phone-based);
   * elsewhere it is set by the operator via `cremind channels set-phone`, and
   * it is what lets a direct send address someone from a list of numbers.
   */
  phone: string | null;
  /** WhatsApp linked-identity alias, used to recognise `@lid` replies. */
  wa_lid?: string | null;
  /**
   * This client's override of the profile's "confirm before messaging clients"
   * setting: `'skip'` sends directly, `'required'` always asks, `null` inherits.
   */
  send_confirmation: 'required' | 'skip' | null;
  authenticated: boolean;
  pending_otp: string | null;
  pending_otp_expires_at: number | null;
  conversation_id: string | null;
  /** Null when the subscriber has no recorded usage yet. */
  usage?: ChannelSenderUsage | null;
  created_at: number;
  updated_at: number;
}

/**
 * Filter config for a channel in ``mode === 'notification'``, stored under
 * ``config.notification_filter``. Mirrors ``app/channels/notification_filter.py``.
 * A notification is delivered only if it matches ALL set dimensions (empty list
 * = no constraint on that dimension). The backend normalizes/validates on write.
 */
export interface NotificationQuietHours {
  enabled: boolean;
  start: string; // HH:MM (24h)
  end: string;   // HH:MM (24h)
  tz: string;    // IANA name; '' = server local
  allow_high: boolean;
}

export interface NotificationFilter {
  version?: number;
  min_priority: 'all' | 'high';
  kinds: string[];
  exclude_kinds: string[];
  source_kinds: string[];
  subscription_ids: string[];
  conversation_ids: string[];
  keywords: string[];
  keywords_mode: 'any' | 'all';
  quiet_hours: NotificationQuietHours;
}

/** Notification ``kind`` values Cremind emits (checkbox source for the UI). */
export const NOTIFICATION_KINDS = [
  'event_run_completed', 'event_run_failed', 'event_run_pending',
  'completed', 'error', 'started', 'skill_register_required',
] as const;

/** Trigger engines behind an event run. */
export const NOTIFICATION_SOURCE_KINDS = [
  'schedule', 'file_watcher', 'skill_event',
] as const;

/** Default filter seeded into a fresh notification channel — forwards
 *  everything except the noisy ``started`` ping and ``channel_otp`` codes. */
export function defaultNotificationFilter(): NotificationFilter {
  return {
    version: 1,
    min_priority: 'all',
    kinds: [],
    exclude_kinds: ['started', 'channel_otp'],
    source_kinds: [],
    subscription_ids: [],
    conversation_ids: [],
    keywords: [],
    keywords_mode: 'any',
    quiet_hours: { enabled: false, start: '22:00', end: '07:00', tz: '', allow_high: true },
  };
}

function readChannelEntry(raw: any): ChannelCatalogEntry | null {
  const ch = raw?.channel;
  if (!ch || !ch.type || !ch.display_name) return null;
  return {
    type: ch.type,
    display_name: ch.display_name,
    icon: ch.icon,
    supports_bot: ch.supports_bot,
    supports_normal: ch.supports_normal,
    auth_modes: ch.auth_modes,
    default_response_mode: ch.default_response_mode || raw?.response?.default_mode,
    modes: ch.modes || [],
    implemented: ch.implemented !== false,
    supports_group_chats: ch.supports_group_chats === true,
  };
}

export async function fetchChannelCatalog(
  agentUrl: string, authToken: string,
): Promise<Record<string, ChannelCatalogEntry>> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/channels/catalog`, { headers: authHeaders(authToken) });
  if (!res.ok) throw new Error(`Failed to fetch channel catalog: ${res.statusText}`);
  const data = await res.json();
  const out: Record<string, ChannelCatalogEntry> = {};
  for (const [k, v] of Object.entries(data.channels || {})) {
    const entry = readChannelEntry(v);
    if (entry) out[k] = entry;
  }
  return out;
}

/**
 * Public counterpart to {@link fetchChannelCatalog} — same payload, no
 * auth required. Used by the setup wizard's Channels step before any
 * JWT exists. Backed by ``/api/config/channel-catalog``, which serves
 * the same TOML-backed metadata as ``/api/channels/catalog``.
 */
export async function fetchChannelCatalogPublic(
  agentUrl: string,
): Promise<Record<string, ChannelCatalogEntry>> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/config/channel-catalog`);
  if (!res.ok) throw new Error(`Failed to fetch channel catalog: ${res.statusText}`);
  const data = await res.json();
  const out: Record<string, ChannelCatalogEntry> = {};
  for (const [k, v] of Object.entries(data.channels || {})) {
    const entry = readChannelEntry(v);
    if (entry) out[k] = entry;
  }
  return out;
}

export async function fetchChannels(
  agentUrl: string, authToken: string,
): Promise<ChannelRow[]> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/channels`, { headers: authHeaders(authToken) });
  if (!res.ok) throw new Error(`Failed to fetch channels: ${res.statusText}`);
  const data = await res.json();
  return data.channels || [];
}

export interface CreateChannelPayload {
  channel_type: string;
  mode: string;
  auth_mode?: 'none' | 'otp' | 'password';
  response_mode?: 'detail' | 'normal';
  config?: Record<string, any>;
  enabled?: boolean;
}

export async function createChannel(
  agentUrl: string, authToken: string, payload: CreateChannelPayload,
): Promise<ChannelRow> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/channels`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to create channel: ${res.statusText}`);
  }
  const data = await res.json();
  return data.channel;
}

export interface UpdateChannelPayload {
  mode?: string;
  auth_mode?: 'none' | 'otp' | 'password';
  response_mode?: 'detail' | 'normal';
  enabled?: boolean;
  config?: Record<string, any>;
}

export async function updateChannel(
  agentUrl: string, authToken: string, channelId: string,
  payload: UpdateChannelPayload,
): Promise<ChannelRow> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/channels/${encodeURIComponent(channelId)}`, {
    method: 'PATCH',
    headers: authHeaders(authToken),
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to update channel: ${res.statusText}`);
  }
  const data = await res.json();
  return data.channel;
}

export async function deleteChannel(
  agentUrl: string, authToken: string, channelId: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/channels/${encodeURIComponent(channelId)}`, {
    method: 'DELETE',
    headers: authHeaders(authToken),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to delete channel: ${res.statusText}`);
  }
}

export async function fetchChannelSenders(
  agentUrl: string, authToken: string, channelId: string,
): Promise<ChannelSenderRow[]> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/channels/${encodeURIComponent(channelId)}/senders`, {
    headers: authHeaders(authToken),
  });
  if (!res.ok) throw new Error(`Failed to fetch senders: ${res.statusText}`);
  const data = await res.json();
  return data.senders || [];
}

/**
 * Approve (`authenticated=true`) or revoke a channel subscriber — the operator
 * side of the notification-channel `approval` subscription-auth method. The
 * sender must already exist (they've contacted the channel), else the server 404s.
 */
export async function setSenderAuthenticated(
  agentUrl: string, authToken: string, channelId: string,
  senderId: string, authenticated: boolean,
): Promise<ChannelSenderRow> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/senders/${encodeURIComponent(senderId)}`,
    {
      method: 'PATCH',
      headers: authHeaders(authToken),
      body: JSON.stringify({ authenticated }),
    },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to update subscriber: ${res.statusText}`);
  }
  const data = await res.json();
  return data.sender;
}

/**
 * Override (or clear) whether the agent must ask before messaging this client.
 *
 * `'skip'` lets it send directly — what an unattended automation needs;
 * `'required'` keeps asking even when the profile setting is off; `null`
 * inherits the profile setting. Someone who has never messaged the channel is
 * always confirmed regardless.
 */
export async function setSenderConfirmation(
  agentUrl: string, authToken: string, channelId: string,
  senderId: string, mode: 'required' | 'skip' | null,
): Promise<ChannelSenderRow> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/senders/${encodeURIComponent(senderId)}`,
    {
      method: 'PATCH',
      headers: authHeaders(authToken),
      body: JSON.stringify({ send_confirmation: mode }),
    },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to update client: ${res.statusText}`);
  }
  const data = await res.json();
  return data.sender;
}

/**
 * Wipe a subscriber's conversation history. The messages go; the conversation
 * itself stays, so their next message continues in it and the usage totals
 * shown on this page survive the wipe. 409s while a run is in progress.
 */
export async function clearSenderHistory(
  agentUrl: string, authToken: string, channelId: string, senderId: string,
): Promise<{ conversation_id: string | null; cleared_messages: number }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/senders/${encodeURIComponent(senderId)}/messages`,
    { method: 'DELETE', headers: authHeaders(authToken) },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to clear history: ${res.statusText}`);
  }
  return res.json();
}

/**
 * Delete a channel client outright — as if they had never messaged Cremind.
 *
 * Unlike `clearSenderHistory` (which keeps the person and only wipes their
 * messages), this removes their conversation, the automations homed on it, and
 * the sender record itself including their access state. Their next message
 * arrives as a genuine first contact. Recorded usage totals survive but stop
 * being attributed to anyone. 409s while a run is in progress.
 */
export async function deleteSender(
  agentUrl: string, authToken: string, channelId: string, senderId: string,
): Promise<{ conversation_id: string | null; deleted_messages: number }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/senders/${encodeURIComponent(senderId)}`,
    { method: 'DELETE', headers: authHeaders(authToken) },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to delete client: ${res.statusText}`);
  }
  return res.json();
}

/**
 * Frames emitted by ``GET /api/channels/{id}/auth-events`` for any
 * channel currently in an interactive-pairing state. WhatsApp emits
 * ``qr``; Telegram userbot emits ``code_required`` and (if 2FA is
 * enabled) ``password_required``.
 */
export type ChannelAuthEvent =
  | { kind: 'qr'; qr: string }
  | { kind: 'code_required'; phone?: string; error?: string }
  | { kind: 'password_required'; error?: string }
  | { kind: 'ready' }
  | { kind: 'disconnected'; logged_out?: boolean }
  /** The platform revoked this session from the other side (phone logout,
   *  session displaced by a login elsewhere). Terminal: only re-pairing
   *  recovers it. */
  | { kind: 'unlinked'; reason?: string; logged_out?: boolean; detail?: string }
  | { kind: 'error'; error?: string };

/** @deprecated Use {@link ChannelAuthEvent}; the QR-only union is retained
 *  for back-compat with code that hasn't migrated yet. */
export type ChannelQrEvent = ChannelAuthEvent;

export interface ChannelAuthStreamHandle {
  close: () => void;
}

/** @deprecated alias. */
export type ChannelQrStreamHandle = ChannelAuthStreamHandle;

/**
 * Subscribe to the channel's interactive-pairing event stream (SSE).
 *
 * Used by both WhatsApp (QR scan) and Telegram userbot (code + 2FA).
 * Mirrors the conversation/notifications SSE plumbing — uses fetch +
 * ReadableStream because EventSource can't send Authorization headers.
 * Frames arrive as `data: {…}\n\n`. The handle's `close()` aborts the
 * connection.
 */
export function openChannelAuthStream(
  agentUrl: string,
  authToken: string,
  channelId: string,
  onEvent: (event: ChannelAuthEvent) => void,
  onError?: (e: any) => void,
): ChannelAuthStreamHandle {
  const controller = new AbortController();
  let closed = false;

  (async () => {
    try {
      const base = resolveBaseUrl(agentUrl);
      const url = `${base}/api/channels/${encodeURIComponent(channelId)}/auth-events`;
      const headers: Record<string, string> = { Accept: 'text/event-stream' };
      if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
      const res = await fetch(url, { headers, signal: controller.signal });
      if (!res.ok || !res.body) {
        throw new Error(`Auth-events stream failed: ${res.status} ${res.statusText}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      while (!closed) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        // eslint-disable-next-line no-cond-assign
        while ((idx = (() => {
          const a = buffer.indexOf('\n\n');
          const b = buffer.indexOf('\r\n\r\n');
          if (a === -1) return b;
          if (b === -1) return a;
          return Math.min(a, b);
        })()) !== -1) {
          const sep = buffer[idx] === '\r' ? 4 : 2;
          const frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + sep);
          const dataLines: string[] = [];
          for (const line of frame.split(/\r?\n/)) {
            if (line.startsWith('data:')) {
              dataLines.push(line.slice(5).replace(/^ /, ''));
            }
          }
          if (dataLines.length === 0) continue;
          try {
            const payload = JSON.parse(dataLines.join('\n')) as ChannelAuthEvent;
            onEvent(payload);
          } catch (err) {
            console.warn('[channelAuthStream] bad frame:', dataLines, err);
          }
        }
      }
    } catch (err: any) {
      if (closed || err?.name === 'AbortError') return;
      if (onError) onError(err);
    }
  })();

  return {
    close() {
      if (closed) return;
      closed = true;
      controller.abort();
    },
  };
}

/** @deprecated alias for {@link openChannelAuthStream}. */
export const openChannelQrStream = openChannelAuthStream;

/**
 * Erase a channel's saved pairing session and restart it pairing from scratch.
 *
 * The way out of a session the platform invalidated behind our back — the same
 * account paired somewhere else, a device revoked. Such a session still looks
 * valid to the adapter, which keeps restoring it instead of pairing, so no QR
 * or code is ever produced. This wipes it and restarts the adapter (re-enabling
 * the channel if a remote logout had disabled it), keeping the channel's
 * senders and bound groups — unlike deleting and re-adding it.
 *
 * The caller should re-open the auth stream afterwards to pick up the new
 * adapter's QR / code prompt.
 */
export async function repairChannel(
  agentUrl: string, authToken: string, channelId: string,
): Promise<ChannelRow> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/repair`,
    { method: 'POST', headers: authHeaders(authToken), body: '{}' },
  );
  const data = await res.json().catch(() => ({} as any));
  if (!res.ok) {
    // A 409 still carries the (reset, but not restarted) channel; the message
    // is the persisted reason the adapter refused to come back up.
    throw new Error(data.error || data.message || `Failed to reset the session: ${res.statusText}`);
  }
  return data.channel;
}

/**
 * Submit interactive-pairing input — a verification code or a 2FA
 * password — to a channel's running adapter.
 *
 * Returns `409 No auth input expected` when the adapter isn't currently
 * waiting for input (most often because pairing already completed).
 */
export async function submitChannelAuthInput(
  agentUrl: string, authToken: string, channelId: string,
  payload: { code?: string; password?: string },
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/auth-input`,
    { method: 'POST', headers: authHeaders(authToken), body: JSON.stringify(payload) },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || err.message || `Auth input failed: ${res.statusText}`);
  }
}

// ── channel group chats ────────────────────────────────────────────────────
//
// A platform group this channel's own account is in — a Telegram supergroup, a
// Slack channel. Unrelated to `groupChatApi.ts`, which is Cremind's own rooms
// where several profiles' agents talk to each other.

export type ChannelGroupStatus = 'pending' | 'approved' | 'blocked';

export interface ChannelGroupMember {
  member_id: string;
  alt_ids: string[];
  display_name: string;
  username: string;
  is_bot: boolean;
  role: string | null;
  /** `roster` came from the platform's member list; `seen` from having posted. */
  source: 'roster' | 'seen';
  first_seen_at: number | null;
  last_seen_at: number | null;
  message_count: number;
  /** What the runtime gate would decide for this member — computed server-side
   *  so the switch and the agent's behaviour cannot disagree. */
  responds: boolean;
}

export interface ChannelGroupPolicy {
  mode: 'everyone' | 'selected';
  allow: string[];
  deny: string[];
}

export interface ChannelGroupSettings {
  member_policy: ChannelGroupPolicy;
  respond_mode: 'mention_or_relevant' | 'mention_only';
  max_agent_posts_per_minute: number;
  max_consecutive_bot_messages: number;
}

/** What this platform can do, read off the adapter class server-side. */
export interface ChannelGroupCapabilities {
  roster: boolean;
  join_events: boolean;
  bot_flag: boolean;
  /** Whether the platform can name the groups the account is already in. When
   *  false, a group is only ever reached by somebody posting in it. */
  listing: boolean;
}

export interface ChannelGroup {
  id: string;
  channel_id: string;
  profile: string;
  platform_chat_id: string;
  chat_type: string | null;
  title: string;
  status: ChannelGroupStatus;
  discovered_via: 'join' | 'message' | 'picked' | 'sweep';
  conversation_id: string | null;
  settings: ChannelGroupSettings;
  members: ChannelGroupMember[];
  member_count: number;
  capabilities: ChannelGroupCapabilities;
  roster_refreshed_at: number | null;
  last_message_at: number | null;
  created_at: number;
  updated_at: number;
}

export interface ChannelGroupList {
  groups: ChannelGroup[];
  /** Whether the channel's own toggle is on. Off means nothing new will ever
   *  appear here, which is worth saying rather than showing an empty list. */
  group_chats_enabled: boolean;
}

export async function fetchChannelGroups(
  agentUrl: string, authToken: string, channelId: string,
  opts: { status?: ChannelGroupStatus } = {},
): Promise<ChannelGroupList> {
  const base = resolveBaseUrl(agentUrl);
  const query = opts.status ? `?status=${encodeURIComponent(opts.status)}` : '';
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/groups${query}`,
    { headers: authHeaders(authToken) },
  );
  if (!res.ok) throw new Error(`Failed to fetch group chats: ${res.statusText}`);
  const data = await res.json();
  return {
    groups: data.groups || [],
    group_chats_enabled: data.group_chats_enabled === true,
  };
}

export async function updateChannelGroup(
  agentUrl: string, authToken: string, channelId: string, groupId: string,
  patch: {
    status?: ChannelGroupStatus;
    settings?: Partial<ChannelGroupSettings>;
    title?: string;
  },
): Promise<ChannelGroup> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/groups/${encodeURIComponent(groupId)}`,
    { method: 'PATCH', headers: authHeaders(authToken), body: JSON.stringify(patch) },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to update the group: ${res.statusText}`);
  }
  const data = await res.json();
  return data.group;
}

export async function deleteChannelGroup(
  agentUrl: string, authToken: string, channelId: string, groupId: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/groups/${encodeURIComponent(groupId)}`,
    { method: 'DELETE', headers: authHeaders(authToken) },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to forget the group: ${res.statusText}`);
  }
}

export async function refreshChannelGroupRoster(
  agentUrl: string, authToken: string, channelId: string, groupId: string,
): Promise<{ group: ChannelGroup; source: 'roster' | 'unsupported' }> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/groups/${encodeURIComponent(groupId)}/roster`,
    { method: 'POST', headers: authHeaders(authToken), body: '{}' },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to refresh the members: ${res.statusText}`);
  }
  return res.json();
}


/** A group the account is in, as offered by the picker. */
export interface AvailableChannelGroup {
  platform_chat_id: string;
  title: string;
  chat_type: string | null;
  member_count: number | null;
  /** Set when we already know this group, so the picker can say so instead of
   *  offering a choice the operator already made. */
  tracked: { id: string; status: ChannelGroupStatus } | null;
}

export interface AvailableChannelGroupList {
  /** False when the platform cannot enumerate groups at all (a Telegram bot,
   *  the Zalo bot). Distinct from an empty list, and said differently. */
  supported: boolean;
  groups: AvailableChannelGroup[];
}

export async function fetchAvailableChannelGroups(
  agentUrl: string, authToken: string, channelId: string,
): Promise<AvailableChannelGroupList> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/groups/available`,
    { headers: authHeaders(authToken) },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to list groups: ${res.statusText}`);
  }
  const data = await res.json();
  return { supported: data.supported === true, groups: data.groups || [] };
}

export async function addChannelGroup(
  agentUrl: string, authToken: string, channelId: string,
  group: { platform_chat_id: string; title?: string; chat_type?: string | null },
): Promise<ChannelGroup> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/channels/${encodeURIComponent(channelId)}/groups`,
    {
      method: 'POST',
      headers: authHeaders(authToken),
      body: JSON.stringify(group),
    },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to enable the group: ${res.statusText}`);
  }
  const data = await res.json();
  return data.group;
}
