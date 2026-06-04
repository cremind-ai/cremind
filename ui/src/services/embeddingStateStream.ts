/**
 * Subscribes to the live Vector Embedding lifecycle SSE stream.
 *
 * The backend ({@link app/api/embedding_stream.py}) pushes a snapshot
 * frame on every state transition (`disabled`/`initializing`/
 * `rebuilding`/`ready`/`failed`) plus the current phase string
 * (`loading_model`, `connecting_store`, `rebuilding_*`, …). Each frame
 * carries the full state so consumers don't need to refetch via REST.
 *
 * Mirrors {@link openSettingsStateStream}: EventSource cannot send
 * Authorization headers, but we don't actually need auth here (the
 * stream is unauthenticated to support the pre-token setup wizard).
 * We still use fetch + ReadableStream + manual SSE parsing so the
 * exponential-backoff reconnect logic matches the rest of the app.
 *
 * Shared across browser tabs by `createSharedStream` so multiple tabs
 * of the same origin only hold one underlying connection.
 */

import { createSharedStream, type SharedStreamHandle, type SharedStreamRawHandle } from './sharedStream';
import type { EmbeddingStatus } from './configApi';

export type EmbeddingStateStreamHandle = SharedStreamHandle;

export interface EmbeddingStateSnapshot {
  status: EmbeddingStatus;
  phase: string | null;
  error: string | null;
  ready: boolean;
  busy: boolean;
  enabled: boolean;
}

interface StateFrame {
  type: 'state' | 'ready';
  data: Partial<EmbeddingStateSnapshot>;
}

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function openEmbeddingStateStreamRaw(
  agentUrl: string,
  onState: (snapshot: EmbeddingStateSnapshot) => void,
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
        const url = `${base}/api/config/embedding/stream`;
        const headers: Record<string, string> = { Accept: 'text/event-stream' };

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
              const payload = JSON.parse(dataLines.join('\n')) as StateFrame;
              if (payload.type === 'state' && payload.data) {
                onState(payload.data as EmbeddingStateSnapshot);
              }
            } catch (err) {
              console.warn('[embeddingStateStream] bad frame:', dataLines, err);
            }
          }
        }
        return;
      } catch (err: any) {
        if (closed || err?.name === 'AbortError') return;
        const wait = backoffs[Math.min(attempt, backoffs.length - 1)];
        attempt += 1;
        console.warn(`[embeddingStateStream] reconnecting in ${wait}ms after error:`, err);
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

export function openEmbeddingStateStream(
  agentUrl: string,
  onState: (snapshot: EmbeddingStateSnapshot) => void,
  onError?: (e: any) => void,
): EmbeddingStateStreamHandle {
  return createSharedStream<EmbeddingStateSnapshot>({
    key: 'cremind:embedding',
    bufferSize: 1,
    openRaw: (handleEvent, handleError) =>
      openEmbeddingStateStreamRaw(agentUrl, handleEvent, handleError),
    onEvent: onState,
    onError,
  });
}
