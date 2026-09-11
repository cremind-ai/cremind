/**
 * Google consent windows: opening them, and hearing that they answered.
 *
 * A provider redirects a consent to a loopback callback on the Cremind server:
 * ``/api/oauth/callback`` for the chat-linked skills — the Google ones AND the
 * Atlassian jira/confluence skills, which share that route — and
 * ``/api/oauth/google-calendar/callback`` / ``/api/oauth/google-drive/callback``
 * for the Calendar page and the Drive section. The server does its part there
 * (inbox write, token exchange, picker record) and answers with a small page
 * that posts the notice below and then closes the consent window
 * (app/api/oauth_close.py). Nothing navigates: the window was opened for that
 * one round trip, and the page the user is actually waiting on is this one.
 *
 * So this module is only the listening half. The notice reaches us twice — as a
 * ``postMessage`` to the opener (popups) and on a BroadcastChannel (every
 * same-origin tab) — carrying ``{type, flow, outcome, profile}`` and nothing
 * else: no ref, no state, no authorization code. That is what makes ``'*'`` an
 * acceptable postMessage target, and it is also why a notice is a HINT, never a
 * verdict: the channel reaches every tab of every profile signed in to this
 * browser, so a page answers one by asking the server about its OWN flow (see
 * ``onOAuthReturn``).
 *
 * ``received`` only means the provider's response reached Cremind; the
 * initiating flow (the skill's link wait, the Calendar page, the Drive section)
 * is what confirms the token exchange.
 *
 * The other half is ``installConsentWindowOpener``. The Calendar page and the
 * Drive section already open their consent with ``window.open``; a consent link
 * the agent posted in chat is a plain anchor, which the message renderer gives
 * ``target="_blank"``. A browser lets a script close only a window a script
 * opened, so that anchor's tab would be stuck on the callback page with nothing
 * to do — and before the renderer added that target, the click took the user's
 * own chat tab to Google and left it on the callback. Opening recognised
 * consent links with ``window.open`` instead makes every consent window one
 * that can (and does) close itself.
 *
 * Deliberately free of Pinia/router/Element Plus imports: callers pass what they
 * need, which keeps the module unit-testable under plain Node (ui/tests).
 */

export type OAuthReturnFlow = 'skill' | 'calendar' | 'drive';
export type OAuthReturnOutcome = 'received' | 'denied' | 'failed' | 'invalid';

/** The only thing a consent page ever broadcasts. No ref, state or code. */
export interface OAuthReturnNotice {
  flow: OAuthReturnFlow;
  outcome: OAuthReturnOutcome;
  /** The profile the server knew the flow belonged to; null when it knew none. */
  profile: string | null;
}

/** How a notice reached this page — see ``onOAuthReturn``. */
export interface OAuthReturnDelivery {
  /** True only for a window message whose source is the popup this page opened. */
  fromPopup: boolean;
}

/** Kept identical to MESSAGE_TYPE / CHANNEL_NAME in app/api/oauth_close.py. */
export const OAUTH_RETURN_MESSAGE_TYPE = 'cremind:oauth-return';
export const OAUTH_RETURN_CHANNEL = 'cremind:oauth-return';

// The server's profile rule (app/api/oauth_close.py's callers).
const PROFILE_RE = /^[a-z0-9_-]{1,64}$/;
const FLOWS = new Set<string>(['skill', 'calendar', 'drive']);
const OUTCOMES = new Set<string>(['received', 'denied', 'failed', 'invalid']);
// The server's own callback-state rule (app/api/oauth_callback.py _STATE_RE).
const STATE_RE = /^[A-Za-z0-9_-]{8,128}$/;
// Exact callback paths → flow. A Map, not an object literal, so a crafted
// redirect path such as ``__proto__`` can never resolve to anything.
const CALLBACK_FLOWS = new Map<string, OAuthReturnFlow>([
  ['/api/oauth/callback', 'skill'],
  ['/api/oauth/google-calendar/callback', 'calendar'],
  ['/api/oauth/google-drive/callback', 'drive'],
]);

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
 * callbacks, and name the flow it belongs to. Strict on purpose: only such a
 * consent comes back to a page of ours that can close the window, so only such a
 * link is worth opening differently from every other link in a message.
 */
export function parseGoogleConsentUrl(href: string): { state: string; flow: OAuthReturnFlow } | null {
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
 * Open clicked Google consent links with ``window.open`` so the consent window
 * is one a script may close — which is what lets it close itself when the
 * callback answers (app/api/oauth_close.py).
 *
 * Listens on WINDOW in the capture phase, so it sees the click before the
 * document-level external-link interceptor (utils/externalLinks.ts). Under
 * Electron it does nothing at all: there the consent belongs in the OS browser,
 * which that interceptor hands it to, and no page of ours could close it
 * anyway. Modified and non-primary clicks are left alone — "open in new
 * window", "copy link" and the like must keep working. Returns an uninstall
 * function.
 */
export function installConsentWindowOpener(): () => void {
  const onClick = (event: MouseEvent) => {
    try {
      if (window.cremind) return;          // Electron: the OS browser takes it
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const href = anchorHref(event);
      if (!href || !parseGoogleConsentUrl(href)) return;
      // 'consent' names the window: a second click reuses it rather than
      // leaving abandoned consent tabs behind.
      const opened = window.open(href, 'cremind-oauth-consent');
      if (opened) event.preventDefault();  // a blocked popup keeps the anchor's own behaviour
    } catch {
      // The link must open regardless of anything this handler gets wrong.
    }
  };
  window.addEventListener('click', onClick, true);
  return () => window.removeEventListener('click', onClick, true);
}

function noticeProfile(value: unknown): string | null {
  return typeof value === 'string' && PROFILE_RE.test(value) ? value : null;
}

function parseNotice(data: unknown): OAuthReturnNotice | null {
  if (!data || typeof data !== 'object') return null;
  const raw = data as Record<string, unknown>;
  if (raw.type !== OAUTH_RETURN_MESSAGE_TYPE) return null;
  if (typeof raw.flow !== 'string' || !FLOWS.has(raw.flow)) return null;
  if (typeof raw.outcome !== 'string' || !OUTCOMES.has(raw.outcome)) return null;
  // Absent or null is a response the server could not attribute to a profile.
  // A profile we cannot read is dropped with its notice rather than passed on
  // as profile-less, which a handler may treat as "could be mine".
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
