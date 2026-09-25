/**
 * The standalone User Document Search progress stream (`GET /api/userdocs/stream`).
 *
 * Used only while Settings → My Documents is mounted. Chat routes get the same
 * frames as the `userdocs` topic of the multiplexed profile-events stream, and
 * every other page polls `GET /api/userdocs/status` — the profile-events SSE is
 * deliberately not opened off the chat routes (see App.vue), and this page is
 * where live, per-file progress is actually worth a connection.
 *
 * Unlike the embedding stream this one is authenticated, so it follows the
 * profile-events pattern: fetch + a manually parsed body (EventSource cannot
 * send an Authorization header), exponential-backoff reconnects, and one
 * connection per origin shared across tabs by `createSharedStream`. The shared
 * key names the profile through `credentialFreeAuthScope`, never the token.
 */

import {
  createSharedStream,
  credentialFreeAuthScope,
  type SharedStreamHandle,
  type SharedStreamRawHandle,
} from './sharedStream';
import type { UserDocsSnapshot } from './userdocsApi';

export type UserDocsStreamHandle = SharedStreamHandle;

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) return agentUrl;
  return `${window.location.origin}${agentUrl}`;
}

function openUserDocsStreamRaw(
  agentUrl: string,
  authToken: string,
  onSnapshot: (snapshot: UserDocsSnapshot) => void,
): SharedStreamRawHandle {
  const controller = new AbortController();
  let closed = false;
  let attempt = 0;
  const backoffs = [1000, 2000, 5000, 10000, 30000];

  const run = async () => {
    while (!closed) {
      try {
        const url = `${resolveBaseUrl(agentUrl)}/api/userdocs/stream`;
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

            // Keepalive comments (": keepalive") and the `ready` marker carry
            // no snapshot; only `event: userdocs` frames do.
            let eventName: string | null = null;
            const dataLines: string[] = [];
            for (const rawLine of frame.split(/\r?\n/)) {
              if (rawLine.startsWith('event:')) {
                eventName = rawLine.slice(6).trim();
              } else if (rawLine.startsWith('data:')) {
                dataLines.push(rawLine.slice(5).replace(/^ /, ''));
              }
            }
            if (eventName !== 'userdocs' || dataLines.length === 0) continue;

            try {
              onSnapshot(JSON.parse(dataLines.join('\n')) as UserDocsSnapshot);
            } catch (err) {
              console.warn('[userdocsStream] bad frame:', dataLines, err);
            }
          }
        }
        // The server ended the stream (restart, proxy timeout): reconnect
        // rather than go quiet — the page is still open and still watching.
        // Through the same backoff, so a server that keeps closing at once
        // is not hammered.
        throw new Error('stream ended');
      } catch (err: any) {
        if (closed || err?.name === 'AbortError') return;
        const wait = backoffs[Math.min(attempt, backoffs.length - 1)];
        attempt += 1;
        console.warn(`[userdocsStream] reconnecting in ${wait}ms after error:`, err);
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

/** Subscribe to this profile's snapshots. The first frame arrives on connect. */
export function openUserDocsStream(
  agentUrl: string,
  authToken: string,
  onSnapshot: (snapshot: UserDocsSnapshot) => void,
  onError?: (err: unknown) => void,
): UserDocsStreamHandle {
  return createSharedStream<UserDocsSnapshot>({
    publicKey: `cremind:userdocs:${credentialFreeAuthScope(authToken)}`,
    // Snapshot-style: each frame replaces the last, so a joining tab needs
    // only the newest one.
    bufferSize: 1,
    openRaw: (handleEvent) => openUserDocsStreamRaw(agentUrl, authToken, handleEvent),
    onEvent: onSnapshot,
    onError,
  });
}
