<script setup lang="ts">
/**
 * Landing page for ``/#/oauth-return`` — where the server sends the browser once
 * it has handled a consent callback: Google's for the Calendar page, the Drive
 * section and the Google skills, and Atlassian's for the jira/confluence skills,
 * which share ``/api/oauth/callback``. So the wording (``returnCopy``) names
 * Google only when the result proves the consent was Google's. Why the URL
 * carries only a one-time ``ref``, and why the profile and page to return to come
 * from the server's record rather than from this URL, is explained in
 * services/oauthReturn.ts. This view only presents the outcome and gets the user
 * back:
 *
 * - ``received`` → tell the opener, close the popup. A tab that cannot be closed
 *   by script (the consent was opened from a chat link) restores the page the consent
 *   started on when this browser is signed in to that profile, and otherwise
 *   goes to Cremind home.
 * - ``denied`` / ``failed`` / ``invalid`` → say what happened, with a way back.
 * - no, unknown, used or expired ref → "could not be matched", with a way home.
 *
 * The wording stays at "response received": whether the account actually linked
 * is confirmed by the flow that started it (the chat, the Calendar page, the
 * Drive section), not by this page.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { RouterLink, useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { safeRedirectTarget } from '../utils/loginRedirect';
import {
  consumeOAuthReturn, notifyOAuthReturn, returnCopy, takeOAuthReturnQuery,
  type OAuthReturnResult, type OAuthReturnViewState,
} from '../services/oauthReturn';

// Long enough for a permitted window.close() to take effect; a page still
// running after it is a tab the browser would not let us close.
const CLOSE_CHECK_MS = 300;
// Without a page to restore, leave the result readable for a moment before home.
const HOME_DELAY_MS = 2500;

const router = useRouter();
const settings = useSettingsStore();

const state = ref<OAuthReturnViewState>('working');
const result = ref<OAuthReturnResult | null>(null);
// The callback itself rejected the response's ``state`` (see oauth_callback.py).
const invalidStateCallback = ref(false);
let retryRef: string | null = null;
let timer: number | undefined;

const hasOpener = (() => {
  try {
    return !!window.opener && window.opener !== window;
  } catch {
    return false;
  }
})();

/**
 * Where "back" goes: the recorded page, but only when this browser holds a
 * token for the profile that recorded it — the route guard would otherwise send
 * the user to a login screen they did not ask for. Everything else is home.
 */
const destination = computed(() => {
  const r = result.value;
  if (r?.profile && r.route && settings.getTokenForProfile(r.profile)) {
    const path = safeRedirectTarget(r.route, r.profile);
    if (path && path !== '/') {
      const resolved = router.resolve(path);
      if (resolved.matched.length) {
        let label = 'the page you started from';
        if (resolved.name === 'chat') label = 'your chat';
        else if (resolved.name === 'conversation') label = 'your conversation';
        else if (resolved.meta.title) label = `the ${resolved.meta.title} page`;
        return { path: resolved.fullPath, label, restores: true };
      }
    }
  }
  return { path: '/', label: 'Cremind home', restores: false };
});

const copy = computed(() => returnCopy({
  state: state.value,
  result: result.value,
  destination: destination.value,
  invalidStateCallback: invalidStateCallback.value,
}));

/** The page talks to the server that served it: that is where the ref lives. */
function apiOrigin(): string {
  const { protocol, origin } = window.location;
  return protocol === 'http:' || protocol === 'https:' ? origin : settings.agentUrl;
}

function clearTimer() {
  if (timer !== undefined) {
    window.clearTimeout(timer);
    timer = undefined;
  }
}

function closeWindow() {
  try {
    window.close();
  } catch {
    /* not ours to close */
  }
}

function go(path: string) {
  void router.replace(path).catch(() => {
    /* a guard may redirect the navigation; not an error here */
  });
}

function leaveAfterReceived() {
  // A popup the Calendar/Drive page opened closes here. A tab opened from a chat
  // link has no script opener, so the browser keeps it and we fall through.
  closeWindow();
  timer = window.setTimeout(() => {
    const dest = destination.value;
    if (dest.restores) {
      go(dest.path);
      return;
    }
    timer = window.setTimeout(() => go('/'), HOME_DELAY_MS);
  }, CLOSE_CHECK_MS);
}

async function redeem(refValue: string) {
  clearTimer();
  state.value = 'working';
  retryRef = null;
  let out: OAuthReturnResult | null;
  try {
    out = await consumeOAuthReturn(apiOrigin(), refValue);
  } catch {
    // Not consumed (or the answer was lost): the same ref may still be good.
    retryRef = refValue;
    state.value = 'unreachable';
    return;
  }
  if (!out) {
    state.value = 'unmatched';
    return;
  }
  result.value = out;
  // A notice without a known flow would be addressed to no one. The profile lets
  // other profiles' tabs, which share the BroadcastChannel, tell it is not theirs.
  if (out.flow) notifyOAuthReturn({ flow: out.flow, outcome: out.outcome, profile: out.profile });
  state.value = out.outcome;
  if (out.outcome === 'received') leaveAfterReceived();
}

function retry() {
  if (retryRef) void redeem(retryRef);
}

onMounted(() => {
  // The route guard already lifted these out of the address bar.
  const pending = takeOAuthReturnQuery();
  if (!pending?.ref) {
    invalidStateCallback.value = pending?.error === 'invalid_state';
    state.value = 'unmatched';
    return;
  }
  void redeem(pending.ref);
});

onBeforeUnmount(clearTimer);
</script>

<template>
  <div class="oauth-return">
    <div class="card" :class="`tone-${copy.tone}`" role="status" aria-live="polite">
      <div v-if="state === 'working'" class="spinner" aria-hidden="true"></div>
      <Icon v-else :icon="copy.icon" class="state-icon" aria-hidden="true" />
      <h1>{{ copy.title }}</h1>
      <p v-for="(line, i) in copy.lines" :key="i" :class="{ muted: i > 0 }">{{ line }}</p>

      <div v-if="state !== 'working'" class="actions">
        <button v-if="state === 'unreachable'" type="button" class="action primary" @click="retry">
          <Icon icon="mdi:refresh" aria-hidden="true" /> Try again
        </button>
        <RouterLink
          v-if="state === 'received'"
          :to="destination.path" replace class="action primary"
        >
          Continue now
        </RouterLink>
        <RouterLink
          v-else-if="state === 'denied' || state === 'failed' || state === 'invalid'"
          :to="destination.path" replace class="action primary"
        >
          {{ destination.restores ? `Return to ${destination.label}` : 'Go to Cremind home' }}
        </RouterLink>
        <RouterLink v-else to="/" replace class="action">Go to Cremind home</RouterLink>
        <button
          v-if="hasOpener && state !== 'received'"
          type="button" class="action" @click="closeWindow"
        >
          Close window
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.oauth-return {
  width: 100%;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  box-sizing: border-box;
  background: var(--bg-color);
}
.card {
  width: 100%;
  max-width: 480px;
  padding: 32px;
  text-align: center;
  color: var(--text-primary);
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
}
.state-icon { font-size: 2.75rem; color: var(--text-tertiary); }
.tone-ok .state-icon { color: var(--success-color); }
.tone-warn .state-icon { color: var(--warning-color); }
.tone-error .state-icon { color: var(--danger-color); }
h1 { margin: 12px 0 10px; font-size: 1.25rem; font-weight: 700; color: var(--text-primary); }
p { margin: 0 0 8px; font-size: 0.925rem; line-height: 1.5; color: var(--text-primary); }
p.muted { font-size: 0.875rem; color: var(--text-secondary); }
.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 20px;
}
.action {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  font: inherit;
  font-size: 0.875rem;
  text-decoration: none;
  color: var(--text-primary);
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  cursor: pointer;
}
.action:hover { background: var(--surface-hover); border-color: var(--border-hover); }
.action.primary { color: #fff; background: var(--primary-color); border-color: var(--primary-color); }
.action.primary:hover { background: var(--primary-dark); border-color: var(--primary-dark); }
.spinner {
  width: 36px;
  height: 36px;
  margin: 4px auto 0;
  border-radius: 50%;
  border: 3px solid var(--border-color);
  border-top-color: var(--primary-color);
  animation: oauth-return-spin 0.9s linear infinite;
}
@keyframes oauth-return-spin {
  to { transform: rotate(360deg); }
}
</style>
