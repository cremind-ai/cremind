/**
 * Subscribes to live skill-events admin-page snapshots over SSE.
 *
 * Replaces the manual ``Refresh`` button on the events page: the backend
 * pushes a fresh ``{ subscriptions, listeners }`` snapshot whenever a
 * subscription is created or deleted, or a listener is started. Every
 * frame is the complete state for the caller's profile, so the client
 * just replaces its in-memory copy on each push.
 *
 * Mirrors {@link openProcessesStream}: EventSource cannot send Authorization
 * headers, so we use fetch + ReadableStream and parse SSE frames manually.
 * Reconnects with exponential backoff are transparent.
 *
 * The connection is shared across browser tabs by `createSharedStream`.
 */

import type { ListenerStatus, SkillEventSubscription } from './skillEventsApi';
import { createSharedStream, type SharedStreamHandle, type SharedStreamRawHandle } from './sharedStream';

export type SkillEventsAdminStreamHandle = SharedStreamHandle;

export interface SkillEventsAdminSnapshot {
  subscriptions: SkillEventSubscription[];
  listeners: Record<string, ListenerStatus>;
}

interface AdminFrame {
  type: 'snapshot' | 'ready';
  data: Partial<SkillEventsAdminSnapshot>;
}

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function openSkillEventsAdminStreamRaw(
  agentUrl: string,
  authToken: string,
  onSnapshot: (snap: SkillEventsAdminSnapshot) => void,
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
        const url = `${base}/api/skill-events/admin/stream`;
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
              const payload = JSON.parse(dataLines.join('\n')) as AdminFrame;
              if (payload.type === 'snapshot') {
                onSnapshot({
                  subscriptions: payload.data.subscriptions ?? [],
                  listeners: payload.data.listeners ?? {},
                });
              }
            } catch (err) {
              console.warn('[skillEventsAdminStream] bad frame:', dataLines, err);
            }
          }
        }
        return;
      } catch (err: any) {
        if (closed || err?.name === 'AbortError') return;
        const wait = backoffs[Math.min(attempt, backoffs.length - 1)];
        attempt += 1;
        console.warn(`[skillEventsAdminStream] reconnecting in ${wait}ms after error:`, err);
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

export function openSkillEventsAdminStream(
  agentUrl: string,
  authToken: string,
  onSnapshot: (snap: SkillEventsAdminSnapshot) => void,
  onError?: (e: any) => void,
): SkillEventsAdminStreamHandle {
  return createSharedStream<SkillEventsAdminSnapshot>({
    key: 'cremind:skill-events:admin',
    bufferSize: 1,
    openRaw: (handleEvent, handleError) =>
      openSkillEventsAdminStreamRaw(agentUrl, authToken, handleEvent, handleError),
    onEvent: onSnapshot,
    onError,
  });
}
