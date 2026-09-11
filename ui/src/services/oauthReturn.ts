/**
 * OAuth return — bringing the browser back to the page that started a consent.
 *
 * A provider redirects a consent to a loopback callback on the Cremind server:
 * ``/api/oauth/callback`` for the chat-linked skills — the Google ones AND the
 * Atlassian jira/confluence skills, which share that route — and
 * ``/api/oauth/google-calendar/callback`` / ``/api/oauth/google-drive/callback``
 * for the Calendar page and the Drive section. The server does its part there
 * (inbox write, token exchange, picker record) and then answers with a 303 to
 * ``/#/oauth-return?ref=<one-time ref>``. This module is the SPA's half of that
 * round trip:
 *
 * - **Before consent — remember where we were.** Chat links are plain anchors,
 *   so a window capture-phase click listener recognises a Google consent URL and
 *   posts ``{state, route, flow}`` to ``/api/oauth/return/context``. (The
 *   Calendar and Drive pages navigate their popups by script, not by a click, so
 *   they send ``return_route`` on their own start calls instead. Atlassian
 *   consents are not recorded, so their return falls back to Cremind home.) The
 *   server keys the record by a HASH of ``state`` for ten minutes and binds it to
 *   the recording profile — the raw state is never stored, and another profile
 *   can neither read nor overwrite it.
 * - **After consent — a ref, never the grant.** The callback claims that record
 *   and mints an unrelated one-time ``ref``. Neither the authorization code nor
 *   ``state`` appears in the return URL, so browser history, a copied address or
 *   an extension reading the tab cannot replay the grant. ``consume`` is
 *   unauthenticated on purpose — a consent finished in another browser holds no
 *   token — and single-use: a second consume of the same ref is a 404.
 * - **Tell the initiator.** The return page posts ``{type, flow, outcome,
 *   profile}`` to its opener (popups) and on a BroadcastChannel (other
 *   same-origin tabs), so the Calendar/Drive pages re-check at once instead of on
 *   their next poll. A notice is a hint, never a verdict: the channel reaches
 *   every tab of every profile signed in to this browser, so a page answers it by
 *   asking the server about its OWN flow (see ``onOAuthReturn``). The payload
 *   carries no ref, state or code — the profile slug is a filter, not a
 *   credential, and sits in every profile URL anyway — which is why ``'*'`` is an
 *   acceptable postMessage target: the opener may legitimately sit on the other
 *   scheme or host name of the same server (http vs https, localhost vs
 *   127.0.0.1) once HTTPS recovery redirected the callback.
 *
 * ``received`` only means the provider's response reached Cremind; the
 * initiating flow (the skill's link wait, the Calendar page, the Drive section)
 * is what confirms the token exchange. Missing context — a consent opened in
 * another browser, an expired record, a reloaded return page — falls back to
 * Cremind home rather than guessing at a page. The return page names Google only
 * when the result proves the consent was Google's (``isGoogleReturn``).
 *
 * Deliberately free of Pinia/router/Element Plus imports: callers pass what they
 * need, which keeps the module unit-testable under plain Node (ui/tests).
 */
import { PROFILE_ROUTES } from '../router/profileRoutes';

export type OAuthReturnFlow = 'skill' | 'calendar' | 'drive';
export type OAuthReturnOutcome = 'received' | 'denied' | 'failed' | 'invalid';

/** A Google consent URL Cremind will be able to correlate on return. */
export interface GoogleConsent {
  state: string;
  flow: OAuthReturnFlow;
}

/** What ``POST /api/oauth/return/consume`` hands back for a valid ref. */
export interface OAuthReturnResult {
  outcome: OAuthReturnOutcome;
  /** Which callback the provider hit; null only if the server's record named none it knows. */
  flow: OAuthReturnFlow | null;
  /** The profile that recorded the originating page; null when none did. */
  profile: string | null;
  /** The recorded page (always under ``/<profile>``); null when none was recorded. */
  route: string | null;
  message: string | null;
}

/** The only thing the return page ever broadcasts. No ref, state or code. */
export interface OAuthReturnNotice {
  flow: OAuthReturnFlow;
  outcome: OAuthReturnOutcome;
  /** The profile that recorded the consent; null when none did (or an older page sent it). */
  profile: string | null;
}

/** How a notice reached this page — see ``onOAuthReturn``. */
export interface OAuthReturnDelivery {
  /** True only for a window message whose source is the popup this page opened. */
  fromPopup: boolean;
}

/** The session a consent click is recorded under. */
export interface ConsentSession {
  agentUrl: string;
  token: string;
  profile: string;
}

export const OAUTH_RETURN_MESSAGE_TYPE = 'cremind:oauth-return';
/** BroadcastChannel name. A constant, never derived from a token or ref. */
export const OAUTH_RETURN_CHANNEL = 'cremind:oauth-return';

// The server's own callback-state rule (app/api/oauth_callback.py _STATE_RE). A
// state it would refuse is not worth recording.
const STATE_RE = /^[A-Za-z0-9_-]{8,128}$/;
// secrets.token_urlsafe(32) is 43 characters; stay lenient on length and strict
// on charset — the server is the one that decides whether a ref is real.
const REF_RE = /^[A-Za-z0-9_-]{16,256}$/;
// The server's profile rule (app/api/oauth_return.py _PROFILE_RE).
const PROFILE_RE = /^[a-z0-9_-]{1,64}$/;

// Exact callback paths → flow. A Map, not an object literal, so a crafted
// redirect path such as ``__proto__`` can never resolve to anything.
const CALLBACK_FLOWS = new Map<string, OAuthReturnFlow>([
  ['/api/oauth/callback', 'skill'],
  ['/api/oauth/google-calendar/callback', 'calendar'],
  ['/api/oauth/google-drive/callback', 'drive'],
]);
const FLOWS = new Set<string>(['skill', 'calendar', 'drive']);
const OUTCOMES = new Set<string>(['received', 'denied', 'failed', 'invalid']);

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl.replace(/\/+$/, '');
  }
  return `${window.location.origin}${agentUrl}`;
}

/**
 * Loopback host names as WHATWG ``URL.hostname`` reports them — the parser has
 * already canonicalised ``127.1`` / ``0x7f.0.0.1`` to dotted form and
 * ``[0:0::1]`` to ``[::1]``, so exact matching is sufficient.
 */
function isLoopbackHost(hostname: string): boolean {
  if (hostname === 'localhost' || hostname === '[::1]') return true;
  const v4 = /^127\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(hostname);
  return !!v4 && v4.slice(1).every((octet) => Number(octet) <= 255);
}

/**
 * Recognise a Google consent URL whose callback is one of Cremind's own loopback
 * callbacks, and name the flow it belongs to. Strict on purpose: only these
 * consents come back through ``/#/oauth-return``, so recording anything else
 * would just leave orphan records on the server.
 */
export function parseGoogleConsentUrl(href: string): GoogleConsent | null {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com' || url.port !== '') {
    return null;
  }
  if (url.username || url.password || !url.pathname.startsWith('/o/oauth2/')) return null;
  const state = url.searchParams.get('state') ?? '';
  if (!STATE_RE.test(state)) return null;
  const redirect = url.searchParams.get('redirect_uri');
  if (!redirect) return null;
  let callback: URL;
  try {
    callback = new URL(redirect);
  } catch {
    return null;
  }
  // https is accepted too: an https loopback APP_URL is advertised as its http
  // loopback twin today, but a pinned redirect may still name the https form.
  if (callback.protocol !== 'http:' && callback.protocol !== 'https:') return null;
  if (callback.username || callback.password || !isLoopbackHost(callback.hostname)) return null;
  const flow = CALLBACK_FLOWS.get(callback.pathname);
  return flow ? { state, flow } : null;
}

/**
 * The session a consent click on ``route`` is recorded under, or null when the
 * click is not on a profile page this browser is signed in to. Takes the settings
 * store as a parameter (rather than calling ``useSettingsStore``) so the store is
 * only touched at click time — after the TLS hand-off guard has restored this
 * origin's storage, never before it.
 */
export function consentSessionFor(
  route: { name?: unknown; params: Record<string, unknown> },
  store: { agentUrl: string; getTokenForProfile(profile: string): string },
): ConsentSession | null {
  const name = typeof route.name === 'string' ? route.name : '';
  const profile = route.params.profile;
  if (!name || !PROFILE_ROUTES.has(name) || typeof profile !== 'string' || !profile) return null;
  const token = store.getTokenForProfile(profile);
  return token ? { agentUrl: store.agentUrl, token, profile } : null;
}

/**
 * Record the page a consent was opened from. Best-effort by contract: any failure
 * (offline, 401, 409 because another profile already holds that state) just
 * means the return page falls back to Cremind home, so it resolves ``false``
 * instead of throwing. ``keepalive`` lets the request outlive a same-tab
 * navigation to Google. A 401 here never ejects the session — see
 * ``shouldHandle401`` in sessionExpiry.ts.
 */
export async function recordOAuthReturnContext(
  agentUrl: string,
  token: string,
  context: { state: string; route: string; flow: OAuthReturnFlow },
): Promise<boolean> {
  try {
    const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/oauth/return/context`, {
      method: 'POST',
      keepalive: true,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ state: context.state, route: context.route, flow: context.flow }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

// composedPath(), not ``closest`` from the target: a click on an icon or span
// inside the link still resolves to the anchor. Duck-typed (tagName + string
// href) so SVG anchors — whose href is not a string — are skipped.
function anchorHref(event: Event): string | null {
  const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
  for (const node of path) {
    const el = node as { tagName?: unknown; href?: unknown } | null;
    if (el && typeof el.tagName === 'string' && el.tagName.toUpperCase() === 'A'
      && typeof el.href === 'string' && el.href) {
      return el.href;
    }
  }
  return null;
}

/**
 * Watch for clicks on Google consent links and record the page they were clicked
 * on. Listens on WINDOW in the capture phase so it runs before the document-level
 * external-link interceptor (utils/externalLinks.ts), which in Electron swallows
 * the click to open the OS browser. It only observes: it never calls
 * ``preventDefault``/``stopPropagation`` and never throws into the click.
 * Returns an uninstall function.
 */
export function installOAuthConsentRecorder(options: {
  router: { currentRoute: { value: { fullPath: string } } };
  getSession: () => ConsentSession | null;
}): () => void {
  const onClick = (event: Event) => {
    try {
      // auxclick is every non-primary button; only the middle button opens links.
      if (event.type === 'auxclick' && (event as MouseEvent).button !== 1) return;
      const href = anchorHref(event);
      if (!href) return;
      const consent = parseGoogleConsentUrl(href);
      if (!consent) return;
      const session = options.getSession();
      if (!session) return;
      void recordOAuthReturnContext(session.agentUrl, session.token, {
        state: consent.state,
        route: options.router.currentRoute.value.fullPath,
        flow: consent.flow,
      });
    } catch {
      // Recording is a convenience; the link must open regardless.
    }
  };
  window.addEventListener('click', onClick, true);
  window.addEventListener('auxclick', onClick, true);
  return () => {
    window.removeEventListener('click', onClick, true);
    window.removeEventListener('auxclick', onClick, true);
  };
}

function parseResult(data: unknown): OAuthReturnResult | null {
  if (!data || typeof data !== 'object') return null;
  const raw = data as Record<string, unknown>;
  // The outcome is what the page acts on, so it must be one we know. The ref is
  // already spent by now, so an unrecognised flow degrades to neutral wording
  // rather than to "could not be matched".
  if (typeof raw.outcome !== 'string' || !OUTCOMES.has(raw.outcome)) return null;
  const text = (value: unknown) => (typeof value === 'string' && value ? value : null);
  return {
    outcome: raw.outcome as OAuthReturnOutcome,
    flow: typeof raw.flow === 'string' && FLOWS.has(raw.flow) ? raw.flow as OAuthReturnFlow : null,
    profile: text(raw.profile),
    route: text(raw.route),
    message: text(raw.message),
  };
}

/**
 * Redeem a return ref. Resolves ``null`` when the server does not know it
 * (unknown, expired or already used — all one answer by design); rejects only
 * when the server could not be asked at all, so the page can offer a retry
 * instead of claiming the ref expired.
 */
export async function consumeOAuthReturn(
  agentUrl: string,
  ref: string,
): Promise<OAuthReturnResult | null> {
  if (!REF_RE.test(ref)) return null;
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/oauth/return/consume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ref }),
    cache: 'no-store',
  });
  if (res.status === 400 || res.status === 404 || res.status === 410) return null;
  if (!res.ok) throw new Error(`The server answered ${res.status}`);
  return parseResult(await res.json().catch(() => null));
}

/** What the return page is showing. */
export type OAuthReturnViewState =
  | 'working' | 'received' | 'denied' | 'failed' | 'invalid' | 'unmatched' | 'unreachable';

export interface OAuthReturnCopy {
  tone: 'busy' | 'ok' | 'warn' | 'error';
  icon: string;
  title: string;
  lines: string[];
}

/**
 * Whether ``result`` provably answers a GOOGLE consent. The Calendar and Drive
 * callbacks are Google's alone, but ``/api/oauth/callback`` also serves the
 * Atlassian jira/confluence skills, so flow ``skill`` by itself proves nothing.
 * A recorded profile does: the SPA's only recorder of ``skill`` contexts
 * (``installOAuthConsentRecorder``) records accounts.google.com consents only.
 */
export function isGoogleReturn(result: OAuthReturnResult | null): boolean {
  if (!result) return false;
  if (result.flow === 'calendar' || result.flow === 'drive') return true;
  return result.flow === 'skill' && result.profile !== null;
}

const CONFIRMER: Record<OAuthReturnFlow, string> = {
  skill: 'The chat that asked for the link confirms once the account is connected.',
  calendar: 'The Calendar page confirms once Google Calendar is connected.',
  drive: 'The Drive section confirms which files were granted.',
};
const RETRY_HINT: Record<OAuthReturnFlow, string> = {
  skill: 'Ask the agent to link the account again to retry.',
  calendar: 'Use Connect Google on the Calendar page to retry.',
  drive: 'Use Grant access under Settings → GSuite to retry.',
};

/**
 * The return page's wording for ``state``. Pure — the view supplies where "back"
 * leads (it needs the router and the stored tokens) — so it is testable under
 * plain Node. Neutral unless ``isGoogleReturn`` proves otherwise: a Jira user
 * must not be told that Google answered, let alone that Google refused.
 */
export function returnCopy(view: {
  state: OAuthReturnViewState;
  result: OAuthReturnResult | null;
  destination: { label: string; restores: boolean };
  /** The callback refused the response's ``state`` outright (``?error=invalid_state``). */
  invalidStateCallback?: boolean;
}): OAuthReturnCopy {
  const r = view.result;
  // A server record that names no flow we know still has a real outcome.
  const flow = r?.flow ?? null;
  const confirmer = flow
    ? CONFIRMER[flow] : 'The page that started the sign-in confirms once the account is connected.';
  const retryHint = flow ? RETRY_HINT[flow] : 'Start the sign-in again from the page you began on.';
  const google = isGoogleReturn(r);
  const response = google ? 'Google’s response' : 'The sign-in response';
  const provider = google ? 'Google' : 'The provider';
  switch (view.state) {
    case 'working':
      return { tone: 'busy', icon: '', title: 'Finishing sign-in…', lines: [] };
    case 'received':
      return {
        tone: 'ok',
        icon: 'mdi:check-circle-outline',
        title: `${response} reached Cremind`,
        lines: [
          view.destination.restores
            ? `Returning you to ${view.destination.label}…`
            : 'Taking you to Cremind home… If you started from another window, you can close this one.',
          confirmer,
        ],
      };
    case 'denied':
      return {
        tone: 'warn',
        icon: 'mdi:close-circle-outline',
        title: google ? 'Google sign-in was not completed' : 'Sign-in was not completed',
        lines: [
          `${provider} reported that access was not granted, so nothing was linked.`,
          r?.message ? `${provider} said: ${r.message}` : '',
          retryHint,
        ].filter(Boolean),
      };
    case 'failed':
      return {
        tone: 'error',
        icon: 'mdi:alert-circle-outline',
        title: 'Cremind couldn’t finish the sign-in',
        lines: [
          `${response} arrived, but Cremind could not process it.`,
          r?.message ?? '',
          retryHint,
        ].filter(Boolean),
      };
    case 'invalid':
      return {
        tone: 'warn',
        icon: 'mdi:timer-sand-complete',
        title: 'This sign-in request is no longer active',
        lines: [
          `Cremind no longer recognizes ${google ? 'the request Google answered' : 'the request this response answers'}`
            + ' — it may have timed out, been cancelled, or been replaced by a newer one.',
          retryHint,
        ],
      };
    case 'unreachable':
      return {
        tone: 'error',
        icon: 'mdi:lan-disconnect',
        title: 'Couldn’t reach Cremind',
        lines: [
          `${response} arrived, but this page could not reach the Cremind server to `
            + 'finish. Check the connection (or port-forward) and try again.',
        ],
      };
    default:
      return {
        tone: 'warn',
        icon: 'mdi:link-variant-off',
        title: 'This sign-in response couldn’t be matched',
        lines: [
          view.invalidStateCallback
            ? 'The response did not carry a request Cremind can identify.'
            : 'It may have expired or already been used — each return link works once, '
              + 'for ten minutes.',
          'If you were linking an account, go back to the page you started from to check '
            + 'whether it went through.',
        ],
      };
  }
}

// Parameters captured from ``/#/oauth-return?...`` by the route guard, which
// then redirects to the bare route — so a reload, the back button or a copied
// address never carries a live ref. The view takes them exactly once.
let pendingQuery: { ref: string | null; error: string | null } | null = null;

/**
 * Called by the ``oauth-return`` route guard. Returns true when the URL carried
 * anything, i.e. when the guard must redirect to the clean route.
 */
export function stashOAuthReturnQuery(query: Record<string, unknown>): boolean {
  if (!Object.keys(query).length) return false;
  const ref = typeof query.ref === 'string' && REF_RE.test(query.ref) ? query.ref : null;
  const error = typeof query.error === 'string' && /^[a-z_]{1,64}$/.test(query.error)
    ? query.error : null;
  pendingQuery = { ref, error };
  return true;
}

/** One-shot read of what the route guard captured (null after a reload). */
export function takeOAuthReturnQuery(): { ref: string | null; error: string | null } | null {
  const taken = pendingQuery;
  pendingQuery = null;
  return taken;
}

function noticeProfile(value: unknown): string | null {
  return typeof value === 'string' && PROFILE_RE.test(value) ? value : null;
}

/**
 * Tell whoever started the consent that the provider answered: the opener (a
 * popup the Calendar/Drive page opened) and any same-origin tab. Never throws —
 * a closed or cross-origin opener must not break the return page.
 */
export function notifyOAuthReturn(notice: OAuthReturnNotice): void {
  const payload = {
    type: OAUTH_RETURN_MESSAGE_TYPE,
    flow: notice.flow,
    outcome: notice.outcome,
    profile: noticeProfile(notice.profile),
  };
  try {
    const opener = window.opener as Window | null;
    if (opener && opener !== window) opener.postMessage(payload, '*');
  } catch {
    /* opener closed or inaccessible */
  }
  try {
    if (typeof BroadcastChannel === 'function') {
      const channel = new BroadcastChannel(OAUTH_RETURN_CHANNEL);
      // Delivery targets are fixed when the message is posted, so closing
      // straight away does not drop it.
      channel.postMessage(payload);
      channel.close();
    }
  } catch {
    /* BroadcastChannel unavailable */
  }
}

function parseNotice(data: unknown): OAuthReturnNotice | null {
  if (!data || typeof data !== 'object') return null;
  const raw = data as Record<string, unknown>;
  if (raw.type !== OAUTH_RETURN_MESSAGE_TYPE) return null;
  if (typeof raw.flow !== 'string' || !FLOWS.has(raw.flow)) return null;
  if (typeof raw.outcome !== 'string' || !OUTCOMES.has(raw.outcome)) return null;
  // Absent or null is a consent nobody recorded (or an older return page). A
  // profile we cannot read is dropped with its notice rather than passed on as
  // profile-less, which a handler may treat as "could be mine".
  let profile: string | null = null;
  if (raw.profile !== undefined && raw.profile !== null) {
    profile = noticeProfile(raw.profile);
    if (profile === null) return null;
  }
  return { flow: raw.flow as OAuthReturnFlow, outcome: raw.outcome as OAuthReturnOutcome, profile };
}

// A same-origin popup's notice arrives twice — once from ``opener.postMessage``
// and once on the BroadcastChannel. Collapse the pair so a handler shows one toast.
const DUPLICATE_WINDOW_MS = 2000;

/**
 * Subscribe to return notices. Window messages are accepted from this origin, or
 * from ``popup`` (the consent window this page opened) whatever origin it has
 * ended up on; anything else is ignored. Returns an unsubscribe function.
 *
 * A notice is a HINT to ask the server, never a verdict on this page's flow: the
 * BroadcastChannel carries every tab's returns for every profile signed in to
 * this browser, and any same-origin tab can post a window message. Only
 * ``fromPopup`` — a window message from the consent window this page itself
 * opened — ties a notice to this page's own round, so it is the only delivery a
 * handler may settle a wait on without asking the server first.
 */
export function onOAuthReturn(
  handler: (notice: OAuthReturnNotice, delivery: OAuthReturnDelivery) => void,
  options: { popup?: Window | null } = {},
): () => void {
  let active = true;
  let lastKey = '';
  let lastAt = 0;
  let lastFromPopup = false;
  const deliver = (data: unknown, fromPopup: boolean) => {
    if (!active) return;
    const notice = parseNotice(data);
    if (!notice) return;
    const key = `${notice.flow}:${notice.outcome}:${notice.profile ?? ''}`;
    const now = Date.now();
    // The two copies race, and the channel's may land first. Its duplicate from
    // the popup still goes through: that copy is the only one allowed to settle
    // a wait, so the hint that happened to arrive first must not swallow it.
    if (key === lastKey && now - lastAt < DUPLICATE_WINDOW_MS && (lastFromPopup || !fromPopup)) {
      return;
    }
    lastKey = key;
    lastAt = now;
    lastFromPopup = fromPopup;
    try {
      handler(notice, { fromPopup });
    } catch {
      /* a handler bug must not take the listener down with it */
    }
  };
  const popup = options.popup ?? null;
  const onMessage = (event: MessageEvent) => {
    const fromPopup = popup !== null && event.source === popup;
    if (event.origin !== window.location.origin && !fromPopup) return;
    deliver(event.data, fromPopup);
  };
  window.addEventListener('message', onMessage);
  let channel: BroadcastChannel | null = null;
  try {
    if (typeof BroadcastChannel === 'function') {
      channel = new BroadcastChannel(OAUTH_RETURN_CHANNEL);
      channel.onmessage = (event: MessageEvent) => deliver(event.data, false);
    }
  } catch {
    channel = null;
  }
  return () => {
    active = false;
    window.removeEventListener('message', onMessage);
    try {
      channel?.close();
    } catch {
      /* already closed */
    }
    channel = null;
  };
}
