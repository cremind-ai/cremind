/**
 * Cross-tab shared SSE stream.
 *
 * Browsers cap concurrent HTTP/1.1 connections per origin at 6. Each long-lived
 * SSE stream this UI opens (conversation, notifications, processes, etc.) holds
 * one of those slots open until the tab closes, so a couple of tabs of the same
 * origin can starve ordinary REST requests of connection slots — the symptom is
 * a `pending` request in DevTools with the "Provisional headers are shown"
 * warning.
 *
 * `createSharedStream` solves this by ensuring at most ONE tab per stream key
 * actually holds the SSE connection. That tab is the "leader"; other tabs are
 * "followers" that receive each event over a `BroadcastChannel`. Leader
 * election uses the Web Locks API — when the leader tab closes (or crashes),
 * the browser releases the lock and the next waiting tab transparently takes
 * over.
 *
 * Late joiners receive a snapshot of recent events from the leader's ring
 * buffer so they don't miss anything emitted before they subscribed. If a
 * browser lacks `navigator.locks` or `BroadcastChannel`, sharing degrades and
 * each tab opens its own raw stream — i.e. legacy behaviour, no regression.
 */

export interface SharedStreamHandle {
  close: () => void;
}

export interface SharedStreamRawHandle {
  close: () => void;
}

export interface SharedStreamOptions<TEvent> {
  /** Stable identifier shared across tabs (e.g. `cremind:conv:<id>`). */
  key: string;
  /**
   * Opens the underlying SSE stream. Only invoked in the leader tab.
   * Must call `onEvent` for each parsed payload and `onError` on terminal
   * failure. The returned handle's `close()` must abort the connection.
   */
  openRaw: (
    onEvent: (e: TEvent) => void,
    onError: (err: any) => void,
  ) => SharedStreamRawHandle;
  /** Consumer callback. Fires for both leader-fetched and follower-broadcast events. */
  onEvent: (e: TEvent) => void;
  /** Optional error callback. Only fires for the local tab's own failures. */
  onError?: (err: any) => void;
  /**
   * Maximum events the leader keeps in its replay buffer for late-joining
   * followers. For snapshot-style streams (full state per frame) keep this
   * small (1–4); for delta-style streams (conversation seq events) larger
   * is better. Defaults to 256.
   */
  bufferSize?: number;
}

interface EventMessage<TEvent> {
  kind: 'event';
  payload: TEvent;
}

interface SnapshotRequestMessage {
  kind: 'request-snapshot';
}

interface SnapshotMessage<TEvent> {
  kind: 'snapshot';
  events: TEvent[];
}

interface LeaderChangedMessage {
  kind: 'leader-changed';
}

type ChannelMessage<TEvent> =
  | EventMessage<TEvent>
  | SnapshotRequestMessage
  | SnapshotMessage<TEvent>
  | LeaderChangedMessage;

function isSharingSupported(): boolean {
  if (typeof navigator === 'undefined') return false;
  if (typeof BroadcastChannel === 'undefined') return false;
  const locks = (navigator as Navigator & { locks?: LockManager }).locks;
  return typeof locks?.request === 'function';
}

export function createSharedStream<TEvent>(
  opts: SharedStreamOptions<TEvent>,
): SharedStreamHandle {
  const bufferSize = opts.bufferSize ?? 256;

  if (!isSharingSupported()) {
    const handle = opts.openRaw(opts.onEvent, opts.onError ?? (() => {}));
    return { close: () => handle.close() };
  }

  const channelName = `cremind-stream:${opts.key}`;
  const lockName = `cremind-stream-lock:${opts.key}`;
  const channel = new BroadcastChannel(channelName);

  let closed = false;
  let isLeader = false;
  let rawHandle: SharedStreamRawHandle | null = null;
  let resolveLockHeld: (() => void) | null = null;
  const lockController = new AbortController();
  const buffer: TEvent[] = [];

  const pushToBuffer = (e: TEvent) => {
    buffer.push(e);
    if (buffer.length > bufferSize) {
      buffer.splice(0, buffer.length - bufferSize);
    }
  };

  const onMessage = (msg: MessageEvent) => {
    if (closed) return;
    const data = msg.data as ChannelMessage<TEvent> | null | undefined;
    if (!data || typeof data !== 'object') return;

    switch (data.kind) {
      case 'event':
        if (!isLeader) opts.onEvent(data.payload);
        break;
      case 'request-snapshot':
        if (isLeader && buffer.length > 0) {
          channel.postMessage({ kind: 'snapshot', events: buffer.slice() });
        }
        break;
      case 'snapshot':
        if (!isLeader) {
          for (const e of data.events) opts.onEvent(e);
        }
        break;
      case 'leader-changed':
        if (!isLeader) {
          channel.postMessage({ kind: 'request-snapshot' });
        }
        break;
    }
  };

  channel.addEventListener('message', onMessage);

  const becomeLeader = async () => {
    if (closed) return;
    isLeader = true;

    const handleEvent = (e: TEvent) => {
      pushToBuffer(e);
      try {
        channel.postMessage({ kind: 'event', payload: e });
      } catch {
        // BroadcastChannel can throw if the message is uncloneable. Skip silently.
      }
      opts.onEvent(e);
    };
    const handleError = (err: any) => {
      if (opts.onError) opts.onError(err);
    };

    rawHandle = opts.openRaw(handleEvent, handleError);

    try {
      channel.postMessage({ kind: 'leader-changed' });
    } catch {
      // ignore
    }

    await new Promise<void>(resolve => {
      resolveLockHeld = resolve;
    });
  };

  const locks = (navigator as Navigator & { locks: LockManager }).locks;
  locks
    .request(lockName, { mode: 'exclusive', signal: lockController.signal }, becomeLeader)
    .catch((err: any) => {
      if (closed) return;
      if (err?.name === 'AbortError') return;
      if (opts.onError) opts.onError(err);
    });

  // Follower bootstrap: ask any current leader for its buffered tail. Run on
  // a microtask so the message listener above is wired up before we post.
  Promise.resolve().then(() => {
    if (closed || isLeader) return;
    try {
      channel.postMessage({ kind: 'request-snapshot' });
    } catch {
      // ignore
    }
  });

  return {
    close() {
      if (closed) return;
      closed = true;
      channel.removeEventListener('message', onMessage);
      if (isLeader) {
        rawHandle?.close();
        rawHandle = null;
        resolveLockHeld?.();
      } else {
        lockController.abort();
      }
      try {
        channel.close();
      } catch {
        // ignore
      }
    },
  };
}
