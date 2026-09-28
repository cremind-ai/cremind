/**
 * "Is Cremind Connect running on this computer, and which one is it?"
 *
 * - Desktop app with the Connect bridge: ask it (`status()`), install or
 *   start it (`install()`).
 * - Browser: a `probe` setup session. Its link opens Connect, which binds the
 *   session and so tells the page its computer name, version and installation
 *   id; the session completes right there. No answer within 8 s means Connect
 *   is missing (or not running): the page offers the installer and "Continue
 *   after installation", which reuses the link while it is valid.
 *
 * The last result is remembered per profile (localStorage, best effort).
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useTagsSetupStore } from '../stores/tagsSetup';
import { TagsApiError } from '../services/tagsApi';
import {
  connectBridge, launchConnect, recallConnectCheck, rememberConnectCheck, type ConnectCheckMemory,
} from '../services/connectBridge';
import { isSessionFailed, sessionLinkUsable, setupErrorMessage } from '../utils/tagsSetupFormat';
import { CONNECT_ANSWER_MS } from './useConnectSession';

export type ProbeState = 'idle' | 'checking' | 'found' | 'missing' | 'failed';
export interface ConnectFound { computer?: string; version?: string; installationId?: string }

export function useConnectProbe(profile: () => string) {
  const store = useTagsSetupStore();
  const bridge = connectBridge();
  const state = ref<ProbeState>('idle');
  const found = ref<ConnectFound | null>(null);
  const desktop = ref<CremindConnectStatus | null>(null);
  const error = ref('');
  const installing = ref(false);
  const remembered = ref<ConnectCheckMemory | null>(recallConnectCheck(profile()));
  const sessionId = ref<string | null>(null);
  let launchUrl: string | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const session = computed(() => (sessionId.value ? store.sessions[sessionId.value] ?? null : null));

  function clearTimer() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  function remember(value: ConnectCheckMemory) {
    remembered.value = value;
    rememberConnectCheck(profile(), value);
  }

  function settleFound(info: ConnectFound) {
    clearTimer();
    found.value = info;
    state.value = 'found';
    remember({ found: true, ...info, at: Date.now() });
  }

  function settleMissing() {
    clearTimer();
    found.value = null;
    state.value = 'missing';
    remember({ found: false, at: Date.now() });
  }

  function fail(e: unknown) {
    clearTimer();
    state.value = 'failed';
    error.value = e instanceof TagsApiError
      ? setupErrorMessage(e.code, { fallback: e.message })
      : e instanceof Error && bridge ? e.message : 'Cremind could not be reached. Try again.';
  }

  async function checkDesktop(): Promise<void> {
    if (!bridge) return;
    state.value = 'checking';
    error.value = '';
    try {
      const s = await bridge.status();
      desktop.value = s;
      if (s.installed && s.running) settleFound({ computer: s.computer, version: s.version, installationId: s.installationId });
      else settleMissing();
    } catch (e) {
      fail(e);
    }
  }

  async function launch(url: string) {
    clearTimer();
    timer = setTimeout(() => {
      timer = null;
      if (state.value === 'checking') settleMissing();
    }, CONNECT_ANSWER_MS);
    const res = await launchConnect(url);
    if (!res.ok) error.value = res.error || 'Cremind Connect could not be opened.';
  }

  /** Check now. In a browser, call it straight from the click (it opens a link). */
  async function check(): Promise<void> {
    if (bridge) return checkDesktop();
    error.value = '';
    state.value = 'checking';
    try {
      const { session: s, launchUrl: url } = await store.startSession('probe');
      if (sessionId.value) store.unfollow('session', sessionId.value);
      sessionId.value = s.id;
      launchUrl = url;
      store.follow('session', s.id);
      await launch(url);
    } catch (e) {
      fail(e);
    }
  }

  /** "Continue after installation": the same link while Connect can still pick it up. */
  async function retry(): Promise<void> {
    if (bridge) return checkDesktop();
    if (launchUrl && sessionLinkUsable(session.value)) {
      state.value = 'checking';
      error.value = '';
      await launch(launchUrl);
    } else {
      await check();
    }
  }

  /** Desktop app: install (or start, or update) the bundled Cremind Connect. */
  async function installDesktop(): Promise<void> {
    if (!bridge) return;
    installing.value = true;
    error.value = '';
    try {
      const res = await bridge.install();
      if (!res.ok) error.value = res.error || 'Cremind Connect could not be installed.';
      await checkDesktop();
    } catch (e) {
      fail(e);
    } finally {
      installing.value = false;
    }
  }

  /** Start over (a dialog opened again): nothing checked yet. */
  function reset(): void {
    clearTimer();
    if (sessionId.value) store.unfollow('session', sessionId.value);
    sessionId.value = null;
    launchUrl = null;
    state.value = 'idle';
    found.value = null;
    error.value = '';
  }

  // Connect bound the probe: it runs here, and says who it is. (A late answer still counts.)
  watch(session, (s) => {
    if (!s || state.value === 'found') return;
    if (s.computer || s.state === 'completed') {
      settleFound({ computer: s.computer?.name, version: s.computer?.version, installationId: s.computer?.installation_id });
    } else if (isSessionFailed(s.state) && state.value === 'checking') {
      settleMissing();
    }
  });

  onBeforeUnmount(() => {
    clearTimer();
    if (sessionId.value) store.unfollow('session', sessionId.value);
    launchUrl = null;
  });

  return {
    state,
    found,
    desktop,
    error,
    installing,
    remembered,
    isDesktop: !!bridge,
    check,
    retry,
    installDesktop,
    reset,
  };
}
