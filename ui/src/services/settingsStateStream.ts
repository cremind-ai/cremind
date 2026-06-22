/**
 * Subscribes to the live Settings-page state SSE stream.
 *
 * The backend emits a wakeup ping (``{type: "changed"}``) whenever any
 * resource rendered on the Settings → Tools & Skills page or the Agents
 * drawer changes — tool config, agent register/enable/auth, LLM provider
 * config, setup completion, skill mode, skill add/remove on disk. The
 * ping carries no payload; consumers refetch the resources they render.
 *
 * Mirrors {@link openProcessesStream}: EventSource cannot send
 * Authorization headers, so we use fetch + ReadableStream and parse SSE
 * frames manually. Reconnects with exponential backoff are transparent.
 *
 * Shared across browser tabs by `createSharedStream`.
 */

import { createSharedStream, type SharedStreamHandle, type SharedStreamRawHandle } from './sharedStream';

export type SettingsStateStreamHandle = SharedStreamHandle;

export interface SettingsStateChange {
  /** Emitted once per server-side state change. No payload. */
  ts: number;
}

interface ChangedFrame {
  type: 'changed' | 'ready';
  data: Record<string, never>;
}

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function openSettingsStateStreamRaw(
  agentUrl: string,
  authToken: string,
  onChange: (e: SettingsStateChange) => void,
  _onError: (err: any) => void,
): SharedStreamRawHandle {
  const controller = new AbortController();
  let closed = false;
  let attempt = 0;
  const backoffs = [1000, 2000, 5000, 10000, 30000];

  const run = async () => {
    while (!closed) {
      try {
        const base = resolveBaseUrl(agentUrl);
        const url = `${base}/api/settings/state/stream`;
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
              const payload = JSON.parse(dataLines.join('\n')) as ChangedFrame;
              if (payload.type === 'changed') {
                onChange({ ts: Date.now() });
              }
            } catch (err) {
              console.warn('[settingsStateStream] bad frame:', dataLines, err);
            }
          }
        }
        return;
      } catch (err: any) {
        if (closed || err?.name === 'AbortError') return;
        const wait = backoffs[Math.min(attempt, backoffs.length - 1)];
        attempt += 1;
        console.warn(`[settingsStateStream] reconnecting in ${wait}ms after error:`, err);
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

export function openSettingsStateStream(
  agentUrl: string,
  authToken: string,
  profileKey: string,
  onChange: (e: SettingsStateChange) => void,
  onError?: (e: any) => void,
): SettingsStateStreamHandle {
  return createSharedStream<SettingsStateChange>({
    key: `cremind:settings:${profileKey}`,
    bufferSize: 1,
    openRaw: (handleEvent, handleError) =>
      openSettingsStateStreamRaw(agentUrl, authToken, handleEvent, handleError),
    onEvent: onChange,
    onError,
  });
}
