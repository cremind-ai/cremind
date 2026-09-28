// The search-then-connect flow of the gateway dialogs (Connect gateway, Move
// to another computer): pick a gateway computer, search its USB ports, then
// connect — or recover onto it — what the search found. Everything runs on
// the server as operations the tagsSetup store follows, so a closed dialog or
// a page refresh loses nothing: the page resumes a connection from its id.
//
// Requests name a computer and an opaque search result, never a USB port.
import { computed, ref } from 'vue';
import { useTagsSetupStore } from '../stores/tagsSetup';
import { TagsApiError } from '../services/tagsApi';
import type { GatewayHost, HostOperation } from '../services/tagsSetupApi';
import {
  errorProblem, hostBlock, isOperationTerminal, scanDecision, setupErrorMessage, type HostProblem,
  type ScanDecision,
} from '../utils/tagsSetupFormat';

export type GatewayStep = 'choose' | 'plug' | 'searching' | 'results' | 'connecting' | 'done' | 'failed';

export function useGatewayConnect() {
  const store = useTagsSetupStore();
  const hostId = ref<string | null>(null);
  const scanId = ref<string | null>(null);
  const connectId = ref<string | null>(null);
  /** `search` | `connect` | `cancel` while a request is out. */
  const busy = ref('');
  /** A refusal of the last request, in plain words (and the problem it stands for, if one). */
  const actionError = ref('');
  const actionProblem = ref<HostProblem | null>(null);

  const host = computed<GatewayHost | null>(() => store.hosts.find((h) => h.id === hostId.value) ?? null);
  const scan = computed<HostOperation | null>(() => (scanId.value ? store.hostOps[scanId.value] ?? null : null));
  const connection = computed<HostOperation | null>(() =>
    (connectId.value ? store.hostOps[connectId.value] ?? null : null));
  const decision = computed<ScanDecision | null>(() => (scan.value ? scanDecision(scan.value) : null));
  /** What keeps the chosen computer from searching right now. */
  const blocked = computed(() => (host.value ? hostBlock(host.value) : null));

  const step = computed<GatewayStep>(() => {
    const c = connection.value;
    if (connectId.value) {
      if (!c) return store.lost[connectId.value] ? 'failed' : 'connecting';
      if (c.state === 'succeeded') return 'done';
      if (isOperationTerminal(c.state)) return 'failed';
      return 'connecting';
    }
    if (!hostId.value) return 'choose';
    if (scanId.value) return decision.value?.kind === 'searching' || !scan.value ? 'searching' : 'results';
    return 'plug';
  });

  /** Why the connection did not finish, in plain words. */
  const failure = computed(() => {
    const c = connection.value;
    if (connectId.value && store.lost[connectId.value]) {
      return { problem: null as HostProblem | null, text: 'This connection no longer exists. Start again.' };
    }
    if (!c || c.state === 'succeeded' || !isOperationTerminal(c.state)) return null;
    if (c.state === 'cancelled') return { problem: null, text: 'The connection was cancelled. Nothing was changed.' };
    return {
      problem: errorProblem(c.error?.code),
      text: setupErrorMessage(c.error?.code, { role: 'gateway', fallback: c.error?.message, host: host.value }),
    };
  });

  /** Could the person still take the connection back? (Not once the gateway is being claimed.) */
  const cancellable = computed(() => {
    const c = connection.value;
    return !!c && !isOperationTerminal(c.state) && !c.companion_id;
  });

  function refused(e: unknown) {
    if (e instanceof TagsApiError) {
      actionProblem.value = errorProblem(e.code);
      actionError.value = setupErrorMessage(e.code, { role: 'gateway', fallback: e.message, host: host.value });
    } else {
      actionProblem.value = null;
      actionError.value = 'Cremind could not be reached. Try again.';
    }
  }

  function clearError() {
    actionError.value = '';
    actionProblem.value = null;
  }

  function choose(id: string | null) {
    detach();
    hostId.value = id;
    scanId.value = null;
    connectId.value = null;
    clearError();
  }

  async function search(): Promise<void> {
    if (!hostId.value) return;
    clearError();
    busy.value = 'search';
    try {
      if (scanId.value) store.unfollow('hostop', scanId.value);
      const op = await store.scanHost(hostId.value);
      scanId.value = op.id;
      store.follow('hostop', op.id);
    } catch (e) {
      refused(e);
      void store.loadHosts();
    } finally {
      busy.value = '';
    }
  }

  async function connect(candidateId: string, opts: { name?: string; recover?: boolean } = {}): Promise<void> {
    if (!hostId.value) return;
    clearError();
    busy.value = 'connect';
    try {
      const op = await store.connectCandidate(hostId.value, candidateId, opts);
      connectId.value = op.id;
      store.follow('hostop', op.id);
    } catch (e) {
      refused(e);
    } finally {
      busy.value = '';
    }
  }

  /** Follow a connection still running (Continue after a refresh). */
  function attach(operationId: string) {
    const known = store.hostOps[operationId] ?? store.activeHostOps.find((o) => o.id === operationId);
    hostId.value = known?.host_id ?? hostId.value;
    connectId.value = operationId;
    store.follow('hostop', operationId);
  }

  /** Take back a connection that has not claimed the gateway; false when it could not be. */
  async function cancel(): Promise<boolean> {
    const id = connectId.value;
    if (!id || !cancellable.value) return true;
    busy.value = 'cancel';
    try {
      const op = await store.cancelHostOp(id);
      return isOperationTerminal(op.state);
    } catch (e) {
      refused(e);
      return false;
    } finally {
      busy.value = '';
    }
  }

  /** Start over on the same computer (after a failure). */
  function again() {
    detach();
    scanId.value = null;
    connectId.value = null;
    clearError();
  }

  /** Stop following (the dialog closed); what runs on the server goes on. */
  function detach() {
    if (scanId.value) store.unfollow('hostop', scanId.value);
    if (connectId.value) store.unfollow('hostop', connectId.value);
  }

  return {
    hostId, scanId, connectId, busy, actionError, actionProblem,
    host, scan, connection, decision, blocked, step, failure, cancellable,
    choose, search, connect, attach, cancel, again, detach, clearError,
  };
}
