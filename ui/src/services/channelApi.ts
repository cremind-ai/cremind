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

export interface ChannelSenderRow {
  id: string;
  channel_id: string;
  sender_id: string;
  display_name: string | null;
  authenticated: boolean;
  pending_otp: string | null;
  pending_otp_expires_at: number | null;
  conversation_id: string | null;
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
