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
 * election uses the Web Locks API when available. Plain HTTP LAN pages commonly
 * lack that secure-context API, so they use a short localStorage lease instead.
 * When the leader closes or its lease expires, another tab takes over.
 *
 * Late joiners receive a snapshot of recent events from the leader's ring
 * buffer so they don't miss anything emitted before they subscribed. If a
 * browser lacks `BroadcastChannel`, sharing degrades and each tab opens its own
 * raw stream.
 */

export interface SharedStreamHandle {
  close: () => void;
}

export interface SharedStreamRawHandle {
  close: () => void;
}

export interface SharedStreamOptions<TEvent> {
  /** Stable, credential-free identifier shared across tabs. It is exposed in
   * BroadcastChannel and Web Locks names, so bearer tokens must never appear. */
  publicKey: string;
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
  return typeof BroadcastChannel !== 'undefined';
}

/** Derive a nonsecret, collision-free-enough stream scope from JWT claims.
 * JWT payload claims are not credentials; the signed token and its signature
 * never enter a channel, lock, or persistent-storage name. */
export function credentialFreeAuthScope(authToken: string): string {
  try {
    const parts = authToken.split('.');
    if (parts.length !== 3) return 'invalid-session';
    const encoded = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = encoded + '='.repeat((4 - (encoded.length % 4)) % 4);
    const bytes = Uint8Array.from(atob(padded), char => char.charCodeAt(0));
    const claims = JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>;
    const profile = typeof claims.sub === 'string' ? claims.sub
      : typeof claims.profile === 'string' ? claims.profile : '';
    if (!profile) return 'invalid-session';
    const serial = Number.isInteger(claims.tsr) ? String(claims.tsr) : '0';
    const issued = typeof claims.iat === 'number' || typeof claims.iat === 'string'
      ? String(claims.iat) : 'unknown';
    const expires = typeof claims.exp === 'number' || typeof claims.exp === 'string'
      ? String(claims.exp) : 'unknown';
    return `profile=${encodeURIComponent(profile)}:serial=${serial}:iat=${encodeURIComponent(issued)}:exp=${encodeURIComponent(expires)}`;
  } catch {
    return 'invalid-session';
  }
}

export function createSharedStream<TEvent>(
  opts: SharedStreamOptions<TEvent>,
): SharedStreamHandle {
  const bufferSize = opts.bufferSize ?? 256;

  if (!isSharingSupported()) {
    const handle = opts.openRaw(opts.onEvent, opts.onError ?? (() => {}));
    return { close: () => handle.close() };
  }

  const channelName = `cremind-stream:${opts.publicKey}`;
  const lockName = `cremind-stream-lock:${opts.publicKey}`;
  const channel = new BroadcastChannel(channelName);

  let closed = false;
  let isLeader = false;
  let rawHandle: SharedStreamRawHandle | null = null;
  let resolveLockHeld: (() => void) | null = null;
  let leaseTimer: ReturnType<typeof setInterval> | null = null;
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

  const startLeader = () => {
    if (closed || isLeader) return;
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

  };

  const stopLeader = () => {
    if (!isLeader) return;
    isLeader = false;
    rawHandle?.close();
    rawHandle = null;
    buffer.splice(0);
  };

  const becomeLeader = async () => {
    startLeader();
    await new Promise<void>(resolve => {
      resolveLockHeld = resolve;
    });
    stopLeader();
  };

  const locks = (navigator as Navigator & { locks?: LockManager }).locks;
  if (typeof locks?.request === 'function') {
    locks
      .request(lockName, { mode: 'exclusive', signal: lockController.signal }, becomeLeader)
      .catch((err: any) => {
        if (closed) return;
        if (err?.name === 'AbortError') return;
        if (opts.onError) opts.onError(err);
      });
  } else {
    // Web Locks is a secure-context API, so LAN installs commonly lack it
    // while still having BroadcastChannel. Use a short localStorage lease to
    // retain one SSE connection per origin on plain HTTP.
    // publicKey contains no credential, so retaining it verbatim avoids the
    // profile-crossing collision risk of a short noncryptographic hash.
    const leaseKey = `cremind-stream-lease:${opts.publicKey}`;
    const owner = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    const ttlMs = 6000;
    const tickMs = 2000;
    const parseLease = () => {
      try {
        return JSON.parse(localStorage.getItem(leaseKey) ?? 'null') as {
          owner?: string;
          expires?: number;
        } | null;
      } catch { return null; }
    };
    const elect = () => {
      if (closed) return;
      const now = Date.now();
      const existing = parseLease();
      if (existing?.owner === owner || !existing?.expires || existing.expires <= now) {
        try {
          localStorage.setItem(leaseKey, JSON.stringify({ owner, expires: now + ttlMs }));
          const won = parseLease()?.owner === owner;
          if (won) startLeader();
          else stopLeader();
        } catch {
          // Storage-disabled browsers cannot coordinate safely. Open a raw
          // stream as the established legacy fallback.
          startLeader();
        }
      } else {
        stopLeader();
      }
    };
    elect();
    leaseTimer = setInterval(elect, tickMs);
    window.addEventListener('storage', elect);
    (channel as BroadcastChannel & { __cremindLeaseCleanup?: () => void }).__cremindLeaseCleanup = () => {
      window.removeEventListener('storage', elect);
      try {
        if (parseLease()?.owner === owner) localStorage.removeItem(leaseKey);
      } catch { /* ignore */ }
    };
  }

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
      if (leaseTimer !== null) clearInterval(leaseTimer);
      (channel as BroadcastChannel & { __cremindLeaseCleanup?: () => void }).__cremindLeaseCleanup?.();
      if (isLeader) {
        stopLeader();
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
