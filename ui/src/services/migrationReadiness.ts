import { computed, readonly, ref } from 'vue';

const pendingUploads = ref(0);
const migrating = ref(false);
const waiters = new Set<() => void>();
const TAB_ID = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
export const migrationTabId = TAB_ID;
const CHANNEL_NAME = 'cremind:https-migration-readiness';
const activeRequests = new Map<string, string>();
const ownedRequests = new Map<string, string>();
let channel: BroadcastChannel | null = null;
let responderRefs = 0;
let ticketPreparer: ((transitionId: string) => Promise<void>) | null = null;
let localGates = 0;
const READY_TIMEOUT_MS = 5 * 60_000;
/** How long the initiating tab waits for the OTHER tabs that answered its
 *  barrier. Short on purpose: this barrier is a courtesy — it gives each tab a
 *  chance to finish an upload and mint a handoff before the address changes —
 *  while the guarantee lives on the server, whose activation does not advance
 *  the transport epoch until every registered client has acknowledged. A tab
 *  that cannot answer in this window (its own upload is still running, it was
 *  frozen by the browser) therefore must not stall the switch silently: the
 *  wait moves on, the server keeps counting, and the page shows what it is
 *  waiting for instead of a button that does nothing. */
const SIBLING_READY_TIMEOUT_MS = 15_000;

export type MigrationUploadLease = () => void;

type ReadinessMessage = {
  kind: 'request' | 'seen' | 'ready' | 'failed' | 'release';
  nonce: string;
  transitionId: string;
  tabId: string;
};

function send(message: ReadinessMessage) {
  try { channel?.postMessage(message); } catch { /* best effort */ }
}

function notifyReady() {
  if (pendingUploads.value !== 0) return;
  for (const resolve of waiters) resolve();
  waiters.clear();
}

function refreshMigrating() {
  migrating.value = localGates > 0 || activeRequests.size > 0 || ownedRequests.size > 0;
}

/** Block new uploads while a coordinator owns an activation attempt. */
export function beginMigrationGate(): () => void {
  localGates += 1;
  refreshMigrating();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    localGates = Math.max(0, localGates - 1);
    refreshMigrating();
  };
}

/** Mark an upload that must settle before a transport migration reloads. */
export function beginMigrationUpload(): MigrationUploadLease {
  if (migrating.value) {
    throw new Error(
      'HTTPS activation is already preparing this tab. Wait for the secure page to open before starting another upload.',
    );
  }
  pendingUploads.value += 1;
  let ended = false;
  return () => {
    if (ended) return;
    ended = true;
    pendingUploads.value = Math.max(0, pendingUploads.value - 1);
    notifyReady();
  };
}

/** Run an upload behind the migration barrier. A caller that owns a wider
 * provision/upload/state-save transaction can pass its existing lease so the
 * operation is counted once and remains pending until that caller commits its
 * local state. */
export async function trackMigrationUpload<T>(
  operation: () => Promise<T>,
  existingLease?: MigrationUploadLease,
): Promise<T> {
  const ownedLease = existingLease ? null : beginMigrationUpload();
  try {
    return await operation();
  } finally {
    ownedLease?.();
  }
}

export function waitForMigrationReady(timeoutMs?: number): Promise<void> {
  if (pendingUploads.value === 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const ready = () => {
      if (timer) clearTimeout(timer);
      waiters.delete(ready);
      resolve();
    };
    waiters.add(ready);
    if (timeoutMs !== undefined) {
      timer = setTimeout(() => {
        waiters.delete(ready);
        reject(new Error('A file upload did not finish within five minutes. Finish or cancel it, then retry HTTPS activation.'));
      }, Math.max(0, timeoutMs));
    }
  });
}

/** Register this tab's private ticket-mint callback. Readiness messages carry
 * only a transition id; credentials and tickets stay inside their own tab. */
export function setMigrationTicketPreparer(
  prepare: ((transitionId: string) => Promise<void>) | null,
): void {
  ticketPreparer = prepare;
}

function onReadinessMessage(event: MessageEvent) {
  const message = event.data as Partial<ReadinessMessage> | null;
  if (!message || typeof message !== 'object'
    || typeof message.nonce !== 'string'
    || typeof message.transitionId !== 'string'
    || typeof message.tabId !== 'string') return;
  if (message.kind === 'release') {
    activeRequests.delete(message.nonce);
    refreshMigrating();
    return;
  }
  if (message.kind !== 'request' || message.tabId === TAB_ID) return;
  const nonce = message.nonce;
  const transitionId = message.transitionId;
  const first = !activeRequests.has(nonce);
  activeRequests.set(nonce, transitionId);
  migrating.value = true;
  send({ ...message as ReadinessMessage, kind: 'seen', tabId: TAB_ID });
  if (first) {
    void waitForMigrationReady(READY_TIMEOUT_MS).then(async () => {
      if (activeRequests.get(nonce) === transitionId) {
        if (ticketPreparer) await ticketPreparer(transitionId);
        send({ ...message as ReadinessMessage, kind: 'ready', tabId: TAB_ID });
      }
    }).catch(() => {
      if (activeRequests.get(nonce) === transitionId) {
        send({ ...message as ReadinessMessage, kind: 'failed', tabId: TAB_ID });
      }
    });
  }
}

function announcePageDeparture() {
  // A closing or discarded tab can no longer finish an in-flight upload. Do
  // not leave the initiating tab blocked for the full timeout; when this tab
  // returns from the back-forward cache it will recover from the durable
  // transition and keep its completed attachment references.
  for (const [nonce, transitionId] of activeRequests) {
    send({ kind: 'ready', nonce, transitionId, tabId: TAB_ID });
  }
  for (const transitionId of [...ownedRequests.keys()]) {
    releaseBrowserMigration(transitionId);
  }
}

/** Respond to a same-origin tab's pre-restart upload barrier. */
export function installMigrationReadinessResponder(): () => void {
  responderRefs += 1;
  if (!channel && typeof BroadcastChannel !== 'undefined') {
    channel = new BroadcastChannel(CHANNEL_NAME);
    channel.addEventListener('message', onReadinessMessage);
    window.addEventListener('pagehide', announcePageDeparture);
  }
  return () => {
    responderRefs = Math.max(0, responderRefs - 1);
    if (responderRefs === 0 && channel) {
      channel.removeEventListener('message', onReadinessMessage);
      window.removeEventListener('pagehide', announcePageDeparture);
      channel.close();
      channel = null;
    }
  };
}

/** Wait until every active same-origin tab that heard the barrier has finished
 * its current uploads. Suspended tabs recover independently when they wake.
 *
 * Returns the nonce this round registered, so its owner can release exactly its
 * own round later (a newer round for the same transition takes the slot over,
 * and an abandoned one must not cancel it). */
export async function waitForBrowserMigrationReady(transitionId: string): Promise<string> {
  migrating.value = true;
  const deadline = Date.now() + READY_TIMEOUT_MS;
  await waitForMigrationReady(Math.max(0, deadline - Date.now()));
  const nonce = `${TAB_ID}-${Math.random().toString(36).slice(2)}`;
  if (typeof BroadcastChannel === 'undefined') return nonce;
  if (!channel) installMigrationReadinessResponder();

  const seen = new Set<string>();
  const ready = new Set<string>();
  const failed = new Set<string>();
  ownedRequests.set(transitionId, nonce);
  let wake: (() => void) | null = null;
  const onMessage = (event: MessageEvent) => {
    const message = event.data as Partial<ReadinessMessage> | null;
    if (!message || message.nonce !== nonce || message.transitionId !== transitionId
      || typeof message.tabId !== 'string') return;
    if (message.kind === 'seen') seen.add(message.tabId);
    if (message.kind === 'ready') ready.add(message.tabId);
    if (message.kind === 'failed') failed.add(message.tabId);
    wake?.();
  };
  channel!.addEventListener('message', onMessage);
  const request: ReadinessMessage = { kind: 'request', nonce, transitionId, tabId: TAB_ID };
  try {
    // Repeat during a short discovery window so a tab mounting at the same
    // moment still joins the barrier.
    const discoveryEnds = Date.now() + 800;
    while (Date.now() < discoveryEnds) {
      send(request);
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    // A tab that answers `failed` has actively given up on its handoff, which
    // is worth stopping for. One that simply does not answer is not: it may be
    // mid-upload or frozen, and the server's own readiness round — which this
    // barrier only front-runs — keeps waiting for it either way, visibly.
    const siblingDeadline = Date.now() + SIBLING_READY_TIMEOUT_MS;
    while ([...seen].some(tabId => !ready.has(tabId))) {
      if (failed.size > 0) {
        throw new Error('Another Cremind tab could not save a fresh HTTPS handoff. Finish or cancel its upload, then retry.');
      }
      if (Date.now() >= siblingDeadline || Date.now() >= deadline) break;
      await new Promise<void>(resolve => {
        wake = resolve;
        setTimeout(resolve, 500);
      });
      wake = null;
    }
  } catch (error) {
    releaseBrowserMigration(transitionId, nonce);
    throw error;
  } finally {
    channel!.removeEventListener('message', onMessage);
  }
  return nonce;
}

/**
 * Drop this tab's barrier for ``transitionId``.
 *
 * ``nonce`` names the round being released. Without it the call releases
 * whatever round this tab currently owns (what a coordinator wants when a
 * transition is rolled back or cancelled); with it, a round that has already
 * been superseded releases nothing — otherwise an abandoned attempt would
 * broadcast the CURRENT round's nonce and every sibling would stop answering
 * the barrier that is still running.
 */
export function releaseBrowserMigration(transitionId: string, nonce?: string): void {
  const owned = ownedRequests.get(transitionId);
  if (nonce !== undefined && owned !== undefined && owned !== nonce) return;
  if (owned) {
    send({ kind: 'release', nonce: owned, transitionId, tabId: TAB_ID });
    ownedRequests.delete(transitionId);
  }
  for (const [requestNonce, id] of activeRequests) {
    if (id === transitionId) activeRequests.delete(requestNonce);
  }
  refreshMigrating();
}

export const migrationReadiness = {
  pendingUploads: readonly(pendingUploads),
  ready: computed(() => pendingUploads.value === 0),
  migrating: readonly(migrating),
};
