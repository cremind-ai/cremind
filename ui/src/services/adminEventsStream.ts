/**
 * Combined admin-page SSE subscriber.
 *
 * The Events page renders both the Skill Events table (`SkillEventsPage`)
 * and the File Watchers table (`FileWatcherSection`). Each used to open its
 * own long-lived SSE (`/api/skill-events/admin/stream` and
 * `/api/file-watchers/admin/stream`), holding two of the browser's ~6
 * HTTP/1.1 connection slots per origin. Stacked on the always-on embedding
 * stream and the chat `profile-events` stream, that saturated the pool and
 * stalled later REST requests with "Provisional headers are shown".
 *
 * This multiplexes both admin snapshots onto a single backend SSE
 * (`/api/admin/events-stream`). Within a tab the two consumers share one
 * underlying connection via the ref-counted subscribe API below; across
 * tabs `createSharedStream` collapses it to a single connection per origin.
 */

import { ref } from 'vue';
import type { FileWatcherSubscription } from './fileWatchersApi';
import type { ListenerStatus, SkillEventSubscription } from './skillEventsApi';
import type { ScheduleEventSubscription } from './calendarApi';
import type { EventRun, EventRunStatus } from './eventRunsApi';
import {
  createSharedStream,
  type SharedStreamHandle,
  type SharedStreamRawHandle,
} from './sharedStream';

/**
 * Live connection state of the shared admin SSE, so views can warn when their
 * data may be stale. Tracked by the leader tab's raw loop (§ openAdminEventsRaw);
 * follower tabs stay `idle` (they receive frames over BroadcastChannel and have
 * no raw loop of their own). `idle` before the first connect and after close —
 * neither warrants a "stale" banner, only `reconnecting` does.
 */
export type AdminStreamStatus = 'idle' | 'connected' | 'reconnecting';
export const adminEventsStatus = ref<AdminStreamStatus>('idle');

export interface SkillEventsAdminSnapshot {
  subscriptions: SkillEventSubscription[];
  listeners: Record<string, ListenerStatus>;
}

export interface FileWatchersAdminSnapshot {
  subscriptions: FileWatcherSubscription[];
}

export interface ScheduleEventsAdminSnapshot {
  subscriptions: ScheduleEventSubscription[];
  enabled: boolean;
}

export interface EventRunSubscriptionSummary {
  run_count: number;
  active_count: number;
  pending_count: number;
  last_run_at: number | null;
  // Status of the most-recent run of this rule (whole-table, survives snapshot
  // aging). Optional so an older server that doesn't send it degrades cleanly.
  last_status?: EventRunStatus | null;
}

export interface EventRunsAdminSnapshot {
  runs: EventRun[];
  // keyed by `${source_kind}:${subscription_id}`
  summaries: Record<string, EventRunSubscriptionSummary>;
}

type SkillEventsCallback = (snap: SkillEventsAdminSnapshot) => void;
type FileWatchersCallback = (snap: FileWatchersAdminSnapshot) => void;
type ScheduleEventsCallback = (snap: ScheduleEventsAdminSnapshot) => void;
type EventRunsCallback = (snap: EventRunsAdminSnapshot) => void;

interface SkillEventsFrame { event: 'skill-events'; data: SkillEventsAdminSnapshot; }
interface FileWatchersFrame { event: 'file-watchers'; data: FileWatchersAdminSnapshot; }
interface ScheduleEventsFrame { event: 'schedule-events'; data: ScheduleEventsAdminSnapshot; }
interface EventRunsFrame { event: 'event-runs'; data: EventRunsAdminSnapshot; }
interface ReadyFrame { event: 'ready'; data: Record<string, never>; }
type AdminFrame =
  | SkillEventsFrame
  | FileWatchersFrame
  | ScheduleEventsFrame
  | EventRunsFrame
  | ReadyFrame;

interface Connection {
  shared: SharedStreamHandle | null;
  agentUrl: string;
  authToken: string;
  skillEventsSubs: Set<SkillEventsCallback>;
  fileWatchersSubs: Set<FileWatchersCallback>;
  scheduleEventsSubs: Set<ScheduleEventsCallback>;
  eventRunsSubs: Set<EventRunsCallback>;
  // Last snapshot of each kind, replayed to a subscriber that joins after
  // the shared stream's initial frames have already arrived (e.g. the two
  // Events-page consumers register one tick apart).
  lastSkillEvents: SkillEventsAdminSnapshot | null;
  lastFileWatchers: FileWatchersAdminSnapshot | null;
  lastScheduleEvents: ScheduleEventsAdminSnapshot | null;
  lastEventRuns: EventRunsAdminSnapshot | null;
}

let connection: Connection | null = null;

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) return agentUrl;
  return `${window.location.origin}${agentUrl}`;
}

function openAdminEventsRaw(
  conn: Connection,
  onEvent: (frame: AdminFrame) => void,
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
        const url = `${base}/api/admin/events-stream`;
        const headers: Record<string, string> = { Accept: 'text/event-stream' };
        if (conn.authToken) headers['Authorization'] = `Bearer ${conn.authToken}`;

        const res = await fetch(url, { headers, signal: controller.signal });
        if (!res.ok || !res.body) {
          throw new Error(`SSE failed: ${res.status} ${res.statusText}`);
        }
        attempt = 0;
        adminEventsStatus.value = 'connected';

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
              if (eventName === 'skill-events') {
                onEvent({ event: 'skill-events', data: data as SkillEventsAdminSnapshot });
              } else if (eventName === 'file-watchers') {
                onEvent({ event: 'file-watchers', data: data as FileWatchersAdminSnapshot });
              } else if (eventName === 'schedule-events') {
                onEvent({ event: 'schedule-events', data: data as ScheduleEventsAdminSnapshot });
              } else if (eventName === 'event-runs') {
                onEvent({ event: 'event-runs', data: data as EventRunsAdminSnapshot });
              } else if (eventName === 'ready') {
                onEvent({ event: 'ready', data: {} });
              }
            } catch (err) {
              console.warn('[adminEventsStream] bad frame:', dataLines, err);
            }
          }
        }
        return;
      } catch (err: any) {
        if (closed || err?.name === 'AbortError') return;
        adminEventsStatus.value = 'reconnecting';
        const wait = backoffs[Math.min(attempt, backoffs.length - 1)];
        attempt += 1;
        console.warn(`[adminEventsStream] reconnecting in ${wait}ms after error:`, err);
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
      adminEventsStatus.value = 'idle';
    },
  };
}

function dispatchFrame(conn: Connection, frame: AdminFrame) {
  if (frame.event === 'skill-events') {
    conn.lastSkillEvents = frame.data;
    for (const cb of conn.skillEventsSubs) {
      try { cb(frame.data); } catch (e) { console.warn('[adminEventsStream] skill-events sub threw', e); }
    }
  } else if (frame.event === 'file-watchers') {
    conn.lastFileWatchers = frame.data;
    for (const cb of conn.fileWatchersSubs) {
      try { cb(frame.data); } catch (e) { console.warn('[adminEventsStream] file-watchers sub threw', e); }
    }
  } else if (frame.event === 'schedule-events') {
    conn.lastScheduleEvents = frame.data;
    for (const cb of conn.scheduleEventsSubs) {
      try { cb(frame.data); } catch (e) { console.warn('[adminEventsStream] schedule-events sub threw', e); }
    }
  } else if (frame.event === 'event-runs') {
    conn.lastEventRuns = frame.data;
    for (const cb of conn.eventRunsSubs) {
      try { cb(frame.data); } catch (e) { console.warn('[adminEventsStream] event-runs sub threw', e); }
    }
  }
  // 'ready' is a no-op marker; consumers act on snapshot frames.
}

function startShared(conn: Connection) {
  conn.shared = createSharedStream<AdminFrame>({
    key: 'cremind:admin-events',
    bufferSize: 4,
    openRaw: (handleEvent, handleError) => openAdminEventsRaw(conn, handleEvent, handleError),
    onEvent: (frame) => dispatchFrame(conn, frame),
  });
}

function ensureConnection(agentUrl: string, authToken: string): Connection {
  // A changed token (profile switch / re-login) must re-establish the
  // backend stream so it is scoped to the new profile.
  if (connection && connection.authToken !== authToken) {
    if (connection.shared) connection.shared.close();
    connection = null;
  }
  if (!connection) {
    connection = {
      shared: null,
      agentUrl,
      authToken,
      skillEventsSubs: new Set(),
      fileWatchersSubs: new Set(),
      scheduleEventsSubs: new Set(),
      eventRunsSubs: new Set(),
      lastSkillEvents: null,
      lastFileWatchers: null,
      lastScheduleEvents: null,
      lastEventRuns: null,
    };
    startShared(connection);
  }
  return connection;
}

function maybeClose(conn: Connection) {
  if (
    conn.skillEventsSubs.size > 0 ||
    conn.fileWatchersSubs.size > 0 ||
    conn.scheduleEventsSubs.size > 0 ||
    conn.eventRunsSubs.size > 0
  ) return;
  if (conn.shared) conn.shared.close();
  if (connection === conn) connection = null;
}

export interface AdminEventsSubHandle {
  close: () => void;
}

export function subscribeSkillEventsAdmin(
  agentUrl: string,
  authToken: string,
  onSnapshot: SkillEventsCallback,
): AdminEventsSubHandle {
  const conn = ensureConnection(agentUrl, authToken);
  conn.skillEventsSubs.add(onSnapshot);
  if (conn.lastSkillEvents) {
    try { onSnapshot(conn.lastSkillEvents); } catch (e) {
      console.warn('[adminEventsStream] skill-events late-replay threw', e);
    }
  }
  return {
    close() {
      conn.skillEventsSubs.delete(onSnapshot);
      maybeClose(conn);
    },
  };
}

export function subscribeFileWatchersAdmin(
  agentUrl: string,
  authToken: string,
  onSnapshot: FileWatchersCallback,
): AdminEventsSubHandle {
  const conn = ensureConnection(agentUrl, authToken);
  conn.fileWatchersSubs.add(onSnapshot);
  if (conn.lastFileWatchers) {
    try { onSnapshot(conn.lastFileWatchers); } catch (e) {
      console.warn('[adminEventsStream] file-watchers late-replay threw', e);
    }
  }
  return {
    close() {
      conn.fileWatchersSubs.delete(onSnapshot);
      maybeClose(conn);
    },
  };
}

export function subscribeScheduleEventsAdmin(
  agentUrl: string,
  authToken: string,
  onSnapshot: ScheduleEventsCallback,
): AdminEventsSubHandle {
  const conn = ensureConnection(agentUrl, authToken);
  conn.scheduleEventsSubs.add(onSnapshot);
  if (conn.lastScheduleEvents) {
    try { onSnapshot(conn.lastScheduleEvents); } catch (e) {
      console.warn('[adminEventsStream] schedule-events late-replay threw', e);
    }
  }
  return {
    close() {
      conn.scheduleEventsSubs.delete(onSnapshot);
      maybeClose(conn);
    },
  };
}

export function subscribeEventRunsAdmin(
  agentUrl: string,
  authToken: string,
  onSnapshot: EventRunsCallback,
): AdminEventsSubHandle {
  const conn = ensureConnection(agentUrl, authToken);
  conn.eventRunsSubs.add(onSnapshot);
  if (conn.lastEventRuns) {
    try { onSnapshot(conn.lastEventRuns); } catch (e) {
      console.warn('[adminEventsStream] event-runs late-replay threw', e);
    }
  }
  return {
    close() {
      conn.eventRunsSubs.delete(onSnapshot);
      maybeClose(conn);
    },
  };
}
