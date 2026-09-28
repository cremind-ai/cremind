/**
 * One Cremind Connect setup session as a dialog runs it (Connect gateway,
 * Recover on this computer): create it, open its link, follow its state
 * (fast polling through the tagsSetup store), notice when Connect has not
 * answered after 8 s, confirm the words, cancel, start again.
 *
 * The launch link (it carries the session's one-use capability) is kept in
 * memory only, for "Open Cremind Connect again" / "Continue after
 * installation" while the session can still be picked up. A session resumed
 * after a page refresh has no link any more: continuing it starts a new one.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useTagsSetupStore } from '../stores/tagsSetup';
import { TagsApiError } from '../services/tagsApi';
import type { SetupSession } from '../services/tagsSetupApi';
import { launchConnect } from '../services/connectBridge';
import {
  connectStep, isSessionTerminal, sessionLinkUsable, setupErrorMessage,
} from '../utils/tagsSetupFormat';

/** How long Connect gets to pick up a link before the page offers the installer. */
export const CONNECT_ANSWER_MS = 8000;

type Creator = () => Promise<{ session: SetupSession; launchUrl: string }>;

export function useConnectSession() {
  const store = useTagsSetupStore();
  const sessionId = ref<string | null>(null);
  const launchUrl = ref<string | null>(null);
  const waitedLong = ref(false);
  const launchError = ref('');
  const actionError = ref('');
  const busy = ref<'' | 'start' | 'confirm' | 'cancel'>('');
  let waitTimer: ReturnType<typeof setTimeout> | null = null;

  const session = computed<SetupSession | null>(() => (sessionId.value ? store.sessions[sessionId.value] ?? null : null));
  const step = computed(() => connectStep(session.value));
  const cancellable = computed(() => {
    const s = session.value?.state;
    return s === 'waiting_for_connect' || s === 'waiting_for_approval' || s === 'waiting_for_confirmation';
  });

  function clearWait() {
    if (waitTimer) clearTimeout(waitTimer);
    waitTimer = null;
  }

  function armWait() {
    clearWait();
    waitedLong.value = false;
    waitTimer = setTimeout(() => {
      waitTimer = null;
      if (step.value === 'open') waitedLong.value = true;
    }, CONNECT_ANSWER_MS);
  }

  /** Follow an existing session (a resume) or one just created. */
  function attach(id: string, url: string | null = null) {
    if (sessionId.value && sessionId.value !== id) store.unfollow('session', sessionId.value);
    sessionId.value = id;
    launchUrl.value = url;
    actionError.value = '';
    store.follow('session', id);
    if (isSessionTerminal(store.sessions[id]?.state)) store.unfollow('session', id);
  }

  async function open(url: string) {
    launchError.value = '';
    armWait();
    const res = await launchConnect(url);
    if (!res.ok) launchError.value = res.error || 'Cremind Connect could not be opened.';
  }

  /** Create a session and open its link — call it straight from the click. */
  async function begin(create: Creator): Promise<boolean> {
    busy.value = 'start';
    actionError.value = '';
    try {
      const previous = session.value;
      const { session: created, launchUrl: url } = await create();
      if (previous && previous.id !== created.id && previous.state === 'waiting_for_connect') {
        void store.cancelSession(previous.id).catch(() => undefined);
      }
      attach(created.id, url);
      await open(url);
      return true;
    } catch (e) {
      actionError.value = errorText(e);
      return false;
    } finally {
      busy.value = '';
    }
  }

  /** "Open Cremind Connect again" / "Continue after installation". */
  async function reopen(create: Creator): Promise<void> {
    if (launchUrl.value && sessionLinkUsable(session.value)) await open(launchUrl.value);
    else await begin(create);
  }

  async function confirm(): Promise<void> {
    const id = sessionId.value;
    if (!id) return;
    busy.value = 'confirm';
    actionError.value = '';
    try {
      await store.confirmSession(id);
    } catch (e) {
      // 410 session_expired: the next poll marks it expired and offers a new start.
      actionError.value = errorText(e);
    } finally {
      busy.value = '';
    }
  }

  /** Cancel while it can be cancelled; true when nothing is left running. */
  async function cancel(): Promise<boolean> {
    const id = sessionId.value;
    if (!id || !cancellable.value) return true;
    busy.value = 'cancel';
    actionError.value = '';
    try {
      await store.cancelSession(id);
      return true;
    } catch (e) {
      // Already redeemed: Connect is finishing it; it can no longer be stopped here.
      actionError.value = errorText(e);
      return false;
    } finally {
      busy.value = '';
    }
  }

  function detach() {
    clearWait();
    if (sessionId.value) store.unfollow('session', sessionId.value);
    sessionId.value = null;
    launchUrl.value = null;
    waitedLong.value = false;
    launchError.value = '';
    actionError.value = '';
    busy.value = '';
  }

  function errorText(e: unknown): string {
    if (e instanceof TagsApiError) return setupErrorMessage(e.code, { fallback: e.message });
    return 'Cremind could not be reached. Check the connection and try again.';
  }

  // Connect answered: the installer offer is no longer needed.
  watch(step, (s) => { if (s !== 'open') { clearWait(); waitedLong.value = false; } });

  onBeforeUnmount(detach);

  return {
    sessionId,
    session,
    step,
    cancellable,
    waitedLong,
    launchError,
    actionError,
    busy,
    hasLink: computed(() => !!launchUrl.value && sessionLinkUsable(session.value)),
    attach,
    begin,
    reopen,
    confirm,
    cancel,
    detach,
  };
}
