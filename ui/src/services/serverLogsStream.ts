/**
 * Subscribes to the server-wide log stream over SSE for the Developer page.
 *
 * The first batch of frames is the server's ring-buffer backfill (~500
 * most recent records), followed by a single `ready` frame, followed by
 * the live tail. Filtering by level happens client-side, so the stream
 * carries every log record regardless of which chips the UI has checked.
 *
 * Mirrors {@link openProcessesStream}: EventSource cannot send
 * Authorization headers, so we use fetch + ReadableStream and parse SSE
 * frames manually. Reconnects with exponential backoff are transparent.
 *
 * Shared across browser tabs by `createSharedStream`, so multiple
 * Developer-page tabs of the same origin hold only one connection — a
 * raw per-tab stream would multiply against Chrome's HTTP/1.1
 * 6-per-origin cap.
 */

import { createSharedStream, type SharedStreamHandle, type SharedStreamRawHandle } from './sharedStream';

export interface LogEntry {
  ts: string;
  level: string;
  source: string;
  message: string;
}

interface LogFrame {
  type: 'log' | 'ready';
  data?: LogEntry;
}

export type ServerLogsStreamHandle = SharedStreamHandle;

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function openServerLogsStreamRaw(
  agentUrl: string,
  authToken: string,
  onFrame: (frame: LogFrame) => void,
  onError: (err: unknown) => void,
): SharedStreamRawHandle {
  const controller = new AbortController();
  let closed = false;
  let attempt = 0;
  const backoffs = [1000, 2000, 5000, 10000, 30000];

  const run = async () => {
    while (!closed) {
      try {
        const base = resolveBaseUrl(agentUrl);
        const url = `${base}/api/server/logs/stream`;
        const headers: Record<string, string> = { Accept: 'text/event-stream' };
        if (authToken) headers['Authorization'] = `Bearer ${authToken}`;

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
            const frame = buffer.slice(0, idx);
            buffer = buffer.slice(idx + sep);

            const dataLines: string[] = [];
            for (const rawLine of frame.split(/\r?\n/)) {
              if (rawLine.startsWith('data:')) {
                dataLines.push(rawLine.slice(5).replace(/^ /, ''));
              }
            }
            if (dataLines.length === 0) continue;

            try {
              // Forward the whole frame (log AND ready) through the shared
              // channel so the `ready` marker lands in the leader's ring
              // buffer and reaches late-joining followers.
              const payload = JSON.parse(dataLines.join('\n')) as LogFrame;
              onFrame(payload);
            } catch (err) {
              console.warn('[serverLogsStream] bad frame:', dataLines, err);
            }
          }
        }
        return;
      } catch (err: unknown) {
        if (closed || (err as { name?: string })?.name === 'AbortError') return;
        onError(err);
        const wait = backoffs[Math.min(attempt, backoffs.length - 1)];
        attempt += 1;
        console.warn(`[serverLogsStream] reconnecting in ${wait}ms after error:`, err);
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

export function openServerLogsStream(
  agentUrl: string,
  authToken: string,
  onLog: (entry: LogEntry) => void,
  onReady?: () => void,
  onError?: (err: unknown) => void,
): ServerLogsStreamHandle {
  // The backend emits exactly one `ready` after its ~500-record backfill.
  // On a busy server that marker can scroll out of the leader's ring
  // before a late follower joins, so it would never see one — fire a
  // synthetic `ready` after a short grace period as a fallback. The
  // Developer page only uses `ready` for a "streaming" flag + one
  // scroll-to-bottom, so a synthesized marker is benign.
  let readyFired = false;
  let readyTimer: ReturnType<typeof setTimeout> | null = null;
  const fireReady = () => {
    if (readyFired) return;
    readyFired = true;
    if (readyTimer !== null) { clearTimeout(readyTimer); readyTimer = null; }
    try { onReady?.(); } catch (e) { console.warn('[serverLogsStream] onReady threw', e); }
  };
  readyTimer = setTimeout(fireReady, 1500);

  const handle = createSharedStream<LogFrame>({
    // Namespace by token so an admin tab (leader) never broadcasts admin's
    // logs to a different profile's follower. Mirrors processesStream.
    key: `cremind:server-logs:${authToken || 'anon'}`,
    // Strictly greater than the backend ring (_RING_CAP = 500) so a late
    // follower's replay from the leader covers at least the backend
    // backfill, `ready` marker included.
    bufferSize: 600,
    openRaw: (handleEvent, handleError) =>
      openServerLogsStreamRaw(agentUrl, authToken, handleEvent, handleError),
    onEvent: (frame) => {
      if (frame.type === 'log' && frame.data) {
        onLog(frame.data);
      } else if (frame.type === 'ready') {
        fireReady();
      }
    },
    onError,
  });

  return {
    close() {
      if (readyTimer !== null) { clearTimeout(readyTimer); readyTimer = null; }
      handle.close();
    },
  };
}
