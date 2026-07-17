/**
 * Global session-expiry handler.
 *
 * When any backend call returns HTTP 401 (an expired or invalidated JWT
 * mid-session), eject the user to the profile selector (``/``) with the page
 * they were on captured as ``?redirect=``, so re-login returns them there. The
 * ``window.fetch`` interceptor in ``main.ts`` is what detects the 401 and calls
 * ``handleUnauthorized`` — this module owns the decision + teardown.
 *
 * The teardown mirrors ``App.vue``'s ``handleLogout`` on purpose. A token-only
 * removal is not enough: the multiplexed profile-events SSE
 * (``profileEventsStream.ts``) is keyed by ``authToken`` and retries forever on
 * a 401, so we must clear the reactive ``authToken`` (which trips the watchers
 * that release the remaining stream subscribers) and disconnect the chat store,
 * or the dead connection keeps hammering the backend after the redirect.
 */
import { ElMessage } from 'element-plus';

import router, { withinJustUpdatedGrace } from '../router';
import { useSettingsStore } from '../stores/settings';
import { useChatStore } from '../stores/chat';
import { getApiOrigin } from './a2aClient';

// Routes where a 401 is expected and must NOT trigger a redirect: the
// ProfileSelector startup ``/api/me`` probes, LoginPage token verification, and
// the SetupWizard's pre-token calls all live here.
const AUTH_ROUTE_NAMES = new Set(['home', 'login', 'setup', 'setup-profile']);

// Idempotency guard so a burst of concurrent 401s (or the SSE retry loop) only
// redirects once. Reset once the redirect settles (see ``handleUnauthorized``).
let redirecting = false;

function onAuthRoute(): boolean {
  const name = router.currentRoute.value.name;
  // A falsy name means the router has not finished its first navigation yet
  // (pre-``router.isReady()``); treat that as an auth route so an early 401
  // (e.g. ``activateProfile``'s fire-and-forget ``fetchMe``) can't misfire.
  if (!name || typeof name !== 'string') return true;
  return AUTH_ROUTE_NAMES.has(name);
}

/**
 * Should a 401 from this URL trigger the global logout? Only when it targets
 * the Cremind backend API — never a remote A2A agent, the Hub, or an external
 * OAuth endpoint. Uses the store-first origin resolver (``getApiOrigin``), not
 * ``getAgentUrl`` which can hold a stale Electron bridge snapshot.
 */
export function shouldHandle401(url: URL): boolean {
  let backendOrigin: string;
  try {
    backendOrigin = getApiOrigin();
  } catch {
    return false;
  }
  if (!backendOrigin) return false;

  let originToMatch: string;
  try {
    originToMatch = new URL(backendOrigin, window.location.href).origin;
  } catch {
    return false;
  }
  if (url.origin !== originToMatch) return false;

  // In the prod single-origin deployment the SPA and API share an origin, so
  // narrow to paths that are actually backend endpoints (the A2A JSON-RPC root
  // ``/``, ``/api/*``, and the agent card under ``/.well-known/``). ``GET /``
  // returns 200 HTML, so the root check only ever matches a real A2A 401.
  const p = url.pathname;
  return p === '/' || p.startsWith('/api') || p.startsWith('/.well-known/');
}

export function handleUnauthorized(): void {
  if (redirecting || onAuthRoute()) return;
  try {
    // A transient 401 during a post-upgrade backend restart must not eject the
    // user — the router guard honors the same grace window.
    if (withinJustUpdatedGrace()) return;
  } catch {
    /* grace check is best-effort */
  }

  redirecting = true;
  try {
    const current = router.currentRoute.value;
    const profile = typeof current.params.profile === 'string' ? current.params.profile : '';
    const fullPath = current.fullPath;

    const settingsStore = useSettingsStore();
    const chatStore = useChatStore();

    if (chatStore.isConnected) chatStore.disconnect();
    settingsStore.authToken = '';
    settingsStore.profileId = '';
    if (profile) settingsStore.removeTokenForProfile(profile);

    ElMessage.warning('Your session has expired. Please log in again.');

    void router
      .replace({ path: '/', query: { redirect: fullPath } })
      .catch(() => {
        /* a guard may redirect the navigation; not an error for us */
      })
      .finally(() => {
        redirecting = false;
      });
  } catch {
    // Never let the handler wedge the flag on an unexpected failure.
    redirecting = false;
  }
}
