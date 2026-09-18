/**
 * Subscribes to the live Setup Wizard progress SSE stream.
 *
 * The backend ({@link app/api/setup_stream.py}) emits one
 * ``event: log`` frame per phase of the ``POST /api/config/setup``
 * handler, plus an initial ``event: ready`` once the stream is live.
 * Each ``log`` frame's payload is ``{ step, message, level, ts }``.
 *
 * Connection is short-lived: opened just before posting Complete Setup,
 * closed when the post returns (or on error).
 *
 * Auth mirrors the backend gate in ``app/api/setup_stream.py``: the stream is
 * open during the first-run bootstrap window (no JWT can exist yet), and
 * admin-only once setup is complete. So every profile created *after* the
 * first needs the admin token — without it the stream 401s and the wizard's
 * live-log panel silently stays empty for the whole run. That is why this uses
 * fetch + ReadableStream rather than `EventSource`, which cannot send an
 * Authorization header.
 */

export interface SetupLogEntry {
  step: string;
  message: string;
  level: 'info' | 'success' | 'warning' | 'error';
  ts: number;
}

export interface SetupProgressStreamHandle {
  close: () => void;
}

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

export function openSetupProgressStream(
  agentUrl: string,
  /** Admin JWT. Empty during first-run setup, where the stream is open. */
  authToken: string,
  onLog: (entry: SetupLogEntry) => void,
  onError?: (err: unknown) => void,
): SetupProgressStreamHandle {
  const controller = new AbortController();
  let closed = false;

  const run = async () => {
    try {
      const base = resolveBaseUrl(agentUrl);
      const url = `${base}/api/config/setup/stream`;
      const headers: Record<string, string> = { Accept: 'text/event-stream' };
      if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
      const res = await fetch(url, {
        headers,
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        throw new Error(`SSE failed: ${res.status} ${res.statusText}`);
      }

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

          let eventName = 'message';
          const dataLines: string[] = [];
          for (const rawLine of frame.split(/\r?\n/)) {
            if (rawLine.startsWith('event:')) {
              eventName = rawLine.slice(6).trim();
            } else if (rawLine.startsWith('data:')) {
              dataLines.push(rawLine.slice(5).replace(/^ /, ''));
            }
          }
          if (eventName !== 'log' || dataLines.length === 0) continue;

          try {
            const payload = JSON.parse(dataLines.join('\n')) as SetupLogEntry;
            if (payload && typeof payload.message === 'string') {
              onLog(payload);
            }
          } catch (err) {
            console.warn('[setupProgressStream] bad frame:', dataLines, err);
          }
        }
      }
    } catch (err: unknown) {
      if (closed || (err as { name?: string } | undefined)?.name === 'AbortError') {
        return;
      }
      onError?.(err);
    }
  };

  void run();

  return {
    close() {
      if (closed) return;
      closed = true;
      controller.abort();
    },
  };
}
