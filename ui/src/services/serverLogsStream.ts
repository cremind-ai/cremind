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
 */

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

export interface ServerLogsStreamHandle {
  close(): void;
}

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

export function openServerLogsStream(
  agentUrl: string,
  authToken: string,
  onLog: (entry: LogEntry) => void,
  onReady?: () => void,
  onError?: (err: unknown) => void,
): ServerLogsStreamHandle {
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
              const payload = JSON.parse(dataLines.join('\n')) as LogFrame;
              if (payload.type === 'log' && payload.data) {
                onLog(payload.data);
              } else if (payload.type === 'ready') {
                onReady?.();
              }
            } catch (err) {
              console.warn('[serverLogsStream] bad frame:', dataLines, err);
            }
          }
        }
        return;
      } catch (err: unknown) {
        if (closed || (err as { name?: string })?.name === 'AbortError') return;
        onError?.(err);
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
