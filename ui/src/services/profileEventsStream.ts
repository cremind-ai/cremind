/**
 * Multiplexed per-profile SSE subscriber.
 *
 * Combines notifications, conversations-list, per-conversation streaming
 * events, and the settings-state / processes / embedding-state streams
 * into one underlying SSE per authenticated profile. Chrome's HTTP/1.1
 * 6-per-host cap was being saturated by these long-lived streams,
 * stalling later requests with "Provisional headers are shown" once
 * multiple tabs were open. Multiplexing collapses everything to a single
 * shared connection per origin via `createSharedStream`, regardless of
 * how many tabs, conversations, or feature pages are active.
 *
 * The standalone endpoints still exist (the CLI consumes them, and the
 * embedding one is intentionally unauthenticated so the pre-token setup
 * wizard can subscribe before this auth-gated stream is reachable). The
 * embeddingStatus store picks the multiplexed source when a token is
 * present and falls back to the standalone stream otherwise.
 */

import type { ConversationSummary } from './conversationApi';
import type { EmbeddingStateSnapshot } from './embeddingStateStream';
import type { ProcessRow } from './processApi';
import type { EventNotificationEntry } from './skillEventsApi';
import {
  createSharedStream,
  type SharedStreamHandle,
  type SharedStreamRawHandle,
} from './sharedStream';

export interface ConversationsListSnapshot {
  conversations: ConversationSummary[];
}

export interface ConversationStreamEvent {
  seq?: number;
  type: string;
  data: any;
}

type NotifCallback = (entry: EventNotificationEntry) => void;
type ConvsCallback = (snap: ConversationsListSnapshot) => void;
type ConvEventCallback = (event: ConversationStreamEvent) => void;
type SettingsCallback = (e: { ts: number }) => void;
type ProcessesCallback = (rows: ProcessRow[]) => void;
type EmbeddingCallback = (snap: EmbeddingStateSnapshot) => void;

interface NotifFrame { event: 'notification'; data: EventNotificationEntry; }
interface ConvsFrame { event: 'conversations-list'; data: ConversationsListSnapshot; }
interface ConvEventFrame {
  event: 'conversation-event';
  data: { conversation_id: string; seq?: number; type: string; data: any };
}
interface SettingsStateFrame { event: 'settings-state'; data: Record<string, never>; }
interface ProcessesFrame { event: 'processes'; data: { processes?: ProcessRow[] }; }
interface EmbeddingFrame { event: 'embedding-state'; data: EmbeddingStateSnapshot; }
interface ReadyFrame { event: 'ready'; data: Record<string, never>; }
type ProfileEventsFrame =
  | NotifFrame
  | ConvsFrame
  | ConvEventFrame
  | SettingsStateFrame
  | ProcessesFrame
  | EmbeddingFrame
  | ReadyFrame;

/**
 * Per-conversation rolling buffer so a late `subscribeConversation`
 * caller catches recent events for an in-progress run. Mirrors the
 * backend ring-buffer replay that the legacy per-conversation SSE
 * provided on connect — including its lifecycle: the buffer is scoped
 * to the *current* run (reset on `user_message`/`event_trigger_message`,
 * dropped on `complete`, matching stream_bus.start_run/end_run). A
 * completed run's messages are already persisted, so replaying its
 * events to a late subscriber would render duplicate bubbles on top of
 * the fetched history.
 */
const CONV_BUFFER_CAP = 256;

interface Connection {
  shared: SharedStreamHandle | null;
  agentUrl: string;
  authToken: string;
  channelType: string | null;
  notifSubs: Set<NotifCallback>;
  convsSubs: Set<ConvsCallback>;
  convSubs: Map<string, Set<ConvEventCallback>>;
  settingsSubs: Set<SettingsCallback>;
  processesSubs: Set<ProcessesCallback>;
  embeddingSubs: Set<EmbeddingCallback>;
  lastSnapshot: ConversationsListSnapshot | null;
  // Last folded-in snapshots, replayed to late subscribers so a component
  // mounting after connect renders immediately instead of waiting for the
  // next mutation. Mirror `lastSnapshot` for the conversations list.
  lastProcesses: ProcessRow[] | null;
  lastEmbedding: EmbeddingStateSnapshot | null;
  // Whether at least one settings-state frame has been seen on this
  // connection — gates the synthetic late-subscriber ping (see
  // subscribeSettingsState).
  settingsPinged: boolean;
  notifCursor: number;
  convBuffers: Map<string, ConversationStreamEvent[]>;
  // Conversations that produced at least one event since the last `ready`
  // frame. Used to sweep stale buffers on (re)connect — see dispatchFrame.
  convSeenSinceReady: Set<string>;
}

const connections = new Map<string, Connection>();

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) return agentUrl;
  return `${window.location.origin}${agentUrl}`;
}

function openProfileEventsRaw(
  conn: Connection,
  onEvent: (frame: ProfileEventsFrame) => void,
  _onError: (err: any) => void,
): SharedStreamRawHandle {
  const controller = new AbortController();
  let closed = false;
  let attempt = 0;
  const backoffs = [1000, 2000, 5000, 10000, 30000];

  const run = async () => {
    while (!closed) {
      try {
        const base = resolveBaseUrl(conn.agentUrl);
        const params = new URLSearchParams();
        params.set('since', String(conn.notifCursor));
        if (conn.channelType) params.set('channel_type', conn.channelType);
        const url = `${base}/api/profile-events/stream?${params.toString()}`;
        const headers: Record<string, string> = { Accept: 'text/event-stream' };
        if (conn.authToken) headers['Authorization'] = `Bearer ${conn.authToken}`;

        const res = await fetch(url, { headers, signal: controller.signal });
        if (!res.ok || !res.body) {
          throw new Error(`SSE failed: ${res.status} ${res.statusText}`);
        }
        attempt = 0;

        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        while (!closed) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let idx: number;
          while (
            (idx = (() => {
              const a = buffer.indexOf('\n\n');
              const b = buffer.indexOf('\r\n\r\n');
              if (a === -1) return b;
              if (b === -1) return a;
              return Math.min(a, b);
            })()) !== -1
          ) {
            const sep = buffer[idx] === '\r' ? 4 : 2;
            const frameStr = buffer.slice(0, idx);
            buffer = buffer.slice(idx + sep);

            let eventName: string | null = null;
            const dataLines: string[] = [];
            for (const rawLine of frameStr.split(/\r?\n/)) {
              if (rawLine.startsWith('event:')) {
                eventName = rawLine.slice(6).trim();
              } else if (rawLine.startsWith('data:')) {
                dataLines.push(rawLine.slice(5).replace(/^ /, ''));
              }
            }
            if (!eventName || dataLines.length === 0) continue;

            try {
              const data = JSON.parse(dataLines.join('\n'));
              if (eventName === 'notification') {
                onEvent({ event: 'notification', data: data as EventNotificationEntry });
              } else if (eventName === 'conversations-list') {
                onEvent({ event: 'conversations-list', data: data as ConversationsListSnapshot });
              } else if (eventName === 'conversation-event') {
                onEvent({ event: 'conversation-event', data });
              } else if (eventName === 'settings-state') {
                onEvent({ event: 'settings-state', data: {} });
              } else if (eventName === 'processes') {
                onEvent({ event: 'processes', data });
              } else if (eventName === 'embedding-state') {
                onEvent({ event: 'embedding-state', data: data as EmbeddingStateSnapshot });
              } else if (eventName === 'ready') {
                onEvent({ event: 'ready', data: {} });
              }
            } catch (err) {
              console.warn('[profileEventsStream] bad frame:', dataLines, err);
            }
          }
        }
        return;
      } catch (err: any) {
        if (closed || err?.name === 'AbortError') return;
        const wait = backoffs[Math.min(attempt, backoffs.length - 1)];
        attempt += 1;
        console.warn(`[profileEventsStream] reconnecting in ${wait}ms after error:`, err);
        await new Promise(r => setTimeout(r, wait));
      }
    }
  };

  run();

  return {
    close() {
      if (closed) return;
      closed = true;
      controller.abort();
    },
  };
}

function dispatchFrame(conn: Connection, frame: ProfileEventsFrame) {
  if (frame.event === 'notification') {
    conn.notifCursor = Math.max(conn.notifCursor, frame.data.created_at);
    for (const cb of conn.notifSubs) {
      try { cb(frame.data); } catch (e) { console.warn('[profileEventsStream] notif sub threw', e); }
    }
  } else if (frame.event === 'conversations-list') {
    conn.lastSnapshot = frame.data;
    for (const cb of conn.convsSubs) {
      try { cb(frame.data); } catch (e) { console.warn('[profileEventsStream] convs sub threw', e); }
    }
  } else if (frame.event === 'conversation-event') {
    const { conversation_id: convId, ...rest } = frame.data;
    if (!convId) return;
    const event: ConversationStreamEvent = {
      seq: rest.seq,
      type: rest.type,
      data: rest.data,
    };
    conn.convSeenSinceReady.add(convId);
    // Buffer for late subscribers within this tab, mirroring the backend
    // ring's lifecycle (stream_bus.start_run/end_run): a run-start marker
    // resets the buffer, the terminal `complete` drops it. `error` is NOT
    // terminal — stream_runner always publishes `complete` after it.
    if (event.type === 'user_message' || event.type === 'event_trigger_message') {
      conn.convBuffers.set(convId, [event]);
    } else if (event.type === 'complete') {
      conn.convBuffers.delete(convId);
    } else {
      let buf = conn.convBuffers.get(convId);
      if (!buf) {
        buf = [];
        conn.convBuffers.set(convId, buf);
      }
      buf.push(event);
      if (buf.length > CONV_BUFFER_CAP) {
        buf.splice(0, buf.length - CONV_BUFFER_CAP);
      }
    }
    // Live dispatch
    const subs = conn.convSubs.get(convId);
    if (subs) {
      for (const cb of subs) {
        try { cb(event); } catch (e) { console.warn('[profileEventsStream] conv sub threw', e); }
      }
    }
  } else if (frame.event === 'settings-state') {
    conn.settingsPinged = true;
    const payload = { ts: Date.now() };
    for (const cb of conn.settingsSubs) {
      try { cb(payload); } catch (e) { console.warn('[profileEventsStream] settings sub threw', e); }
    }
  } else if (frame.event === 'processes') {
    const rows = frame.data.processes ?? [];
    conn.lastProcesses = rows;
    for (const cb of conn.processesSubs) {
      try { cb(rows); } catch (e) { console.warn('[profileEventsStream] processes sub threw', e); }
    }
  } else if (frame.event === 'embedding-state') {
    conn.lastEmbedding = frame.data;
    for (const cb of conn.embeddingSubs) {
      try { cb(frame.data); } catch (e) { console.warn('[profileEventsStream] embedding sub threw', e); }
    }
  } else if (frame.event === 'ready') {
    // The replay phase of a (re)connect just ended. The backend replays the
    // full ring of every *active* conversation before emitting `ready`, so
    // any buffered conversation that produced no frame since the previous
    // `ready` was not replayed as active — its run ended while we were
    // disconnected and its `complete` was lost. Drop those buffers so a
    // later subscriber doesn't re-render a run already in persisted history.
    for (const convId of Array.from(conn.convBuffers.keys())) {
      if (!conn.convSeenSinceReady.has(convId)) conn.convBuffers.delete(convId);
    }
    conn.convSeenSinceReady.clear();
  }
}

function startShared(conn: Connection) {
  conn.shared = createSharedStream<ProfileEventsFrame>({
    key: `cremind:profile-events:${conn.authToken}:${conn.channelType ?? 'all'}`,
    bufferSize: 256,
    openRaw: (handleEvent, handleError) =>
      openProfileEventsRaw(conn, handleEvent, handleError),
    onEvent: (frame) => dispatchFrame(conn, frame),
  });
}

function ensureConnection(
  agentUrl: string,
  authToken: string,
  channelType: string | null | undefined,
  sinceMs: number,
): { conn: Connection; key: string } {
  const key = authToken;
  let conn = connections.get(key);
  if (!conn) {
    conn = {
      shared: null,
      agentUrl,
      authToken,
      channelType: channelType ?? null,
      notifSubs: new Set(),
      convsSubs: new Set(),
      convSubs: new Map(),
      settingsSubs: new Set(),
      processesSubs: new Set(),
      embeddingSubs: new Set(),
      lastSnapshot: null,
      lastProcesses: null,
      lastEmbedding: null,
      settingsPinged: false,
      notifCursor: sinceMs,
      convBuffers: new Map(),
      convSeenSinceReady: new Set(),
    };
    connections.set(key, conn);
    startShared(conn);
    return { conn, key };
  }
  // Caller has a specific channelType preference — re-establish if different.
  // `undefined` means "no preference" (notifications or per-conversation
  // subscriber); only explicit string-or-null values from conversations-list
  // trigger restart.
  if (channelType !== undefined && channelType !== conn.channelType) {
    conn.channelType = channelType;
    conn.lastSnapshot = null;
    if (conn.shared) conn.shared.close();
    startShared(conn);
  }
  return { conn, key };
}

function maybeClose(key: string, conn: Connection) {
  if (
    conn.notifSubs.size > 0
    || conn.convsSubs.size > 0
    || conn.convSubs.size > 0
    || conn.settingsSubs.size > 0
    || conn.processesSubs.size > 0
    || conn.embeddingSubs.size > 0
  ) return;
  if (conn.shared) conn.shared.close();
  connections.delete(key);
}

export interface ProfileEventsSubHandle {
  close: () => void;
}

export function subscribeNotifications(
  agentUrl: string,
  authToken: string,
  sinceMs: number,
  onNotification: NotifCallback,
): ProfileEventsSubHandle {
  const { conn, key } = ensureConnection(agentUrl, authToken, undefined, sinceMs);
  conn.notifSubs.add(onNotification);
  return {
    close() {
      conn.notifSubs.delete(onNotification);
      maybeClose(key, conn);
    },
  };
}

export function subscribeConversationsList(
  agentUrl: string,
  authToken: string,
  channelType: string | null,
  onSnapshot: ConvsCallback,
): ProfileEventsSubHandle {
  const { conn, key } = ensureConnection(agentUrl, authToken, channelType, 0);
  conn.convsSubs.add(onSnapshot);
  if (conn.lastSnapshot) {
    try { onSnapshot(conn.lastSnapshot); } catch (e) {
      console.warn('[profileEventsStream] late-replay threw', e);
    }
  }
  return {
    close() {
      conn.convsSubs.delete(onSnapshot);
      maybeClose(key, conn);
    },
  };
}

export function subscribeConversation(
  agentUrl: string,
  authToken: string,
  conversationId: string,
  onEvent: ConvEventCallback,
): ProfileEventsSubHandle {
  const { conn, key } = ensureConnection(agentUrl, authToken, undefined, 0);
  let subs = conn.convSubs.get(conversationId);
  if (!subs) {
    subs = new Set();
    conn.convSubs.set(conversationId, subs);
  }
  subs.add(onEvent);
  // Replay this tab's buffered events for the conversation so the new
  // subscriber catches an in-progress run that started before it
  // registered. dispatchFrame keeps the buffer scoped to the current run
  // (reset on run-start, dropped on `complete`), so replay never
  // re-renders a finished run; the chat store's per-conversation seqSeen
  // dedupe absorbs any overlap if the same events arrive again live.
  const buf = conn.convBuffers.get(conversationId);
  if (buf) {
    for (const event of buf) {
      try { onEvent(event); } catch (e) {
        console.warn('[profileEventsStream] conv replay threw', e);
      }
    }
  }
  return {
    close() {
      const set = conn.convSubs.get(conversationId);
      if (!set) return;
      set.delete(onEvent);
      if (set.size === 0) conn.convSubs.delete(conversationId);
      maybeClose(key, conn);
    },
  };
}

export function subscribeSettingsState(
  agentUrl: string,
  authToken: string,
  onChange: SettingsCallback,
): ProfileEventsSubHandle {
  // These folded-in subscribers don't consume notifications, but they may
  // be the ones that open the shared connection (e.g. embedding-state is
  // App-level and mounts on non-chat pages where no notification sub
  // exists). Seed the notification cursor at "now" so opening the stream
  // doesn't trigger a full notification-buffer replay the notifications
  // subscriber never wanted (Sidebar itself opens with since=Date.now()).
  const { conn, key } = ensureConnection(agentUrl, authToken, undefined, Date.now());
  conn.settingsSubs.add(onChange);
  // A late joiner gets one synthetic ping so it triggers its initial
  // refetch even though the backend's connect-time ping already fired for
  // an earlier subscriber. The FIRST subscriber does not get one here — it
  // rides the backend's own connect-time ping (see dispatchFrame), so the
  // `settingsPinged` gate prevents a double initial ping.
  if (conn.settingsPinged) {
    try { onChange({ ts: Date.now() }); } catch (e) {
      console.warn('[profileEventsStream] settings late-ping threw', e);
    }
  }
  return {
    close() {
      conn.settingsSubs.delete(onChange);
      maybeClose(key, conn);
    },
  };
}

export function subscribeProcesses(
  agentUrl: string,
  authToken: string,
  onSnapshot: ProcessesCallback,
): ProfileEventsSubHandle {
  // See subscribeSettingsState — seed the cursor at "now" so opening the
  // shared connection here never replays the notification buffer.
  const { conn, key } = ensureConnection(agentUrl, authToken, undefined, Date.now());
  conn.processesSubs.add(onSnapshot);
  if (conn.lastProcesses) {
    try { onSnapshot(conn.lastProcesses); } catch (e) {
      console.warn('[profileEventsStream] processes late-replay threw', e);
    }
  }
  return {
    close() {
      conn.processesSubs.delete(onSnapshot);
      maybeClose(key, conn);
    },
  };
}

export function subscribeEmbeddingState(
  agentUrl: string,
  authToken: string,
  onState: EmbeddingCallback,
): ProfileEventsSubHandle {
  // See subscribeSettingsState — seed the cursor at "now" so opening the
  // shared connection here never replays the notification buffer.
  const { conn, key } = ensureConnection(agentUrl, authToken, undefined, Date.now());
  conn.embeddingSubs.add(onState);
  if (conn.lastEmbedding) {
    try { onState(conn.lastEmbedding); } catch (e) {
      console.warn('[profileEventsStream] embedding late-replay threw', e);
    }
  }
  return {
    close() {
      conn.embeddingSubs.delete(onState);
      maybeClose(key, conn);
    },
  };
}
