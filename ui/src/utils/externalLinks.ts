// Routes clicks on external anchors to the OS default handler (browser / mail
// client / dialer) instead of letting Electron open them in a NEW APP WINDOW.
//
// Under Electron, a clicked ``<a>`` whose href points off the app's origin
// (with or without ``target="_blank"``) would otherwise hit the main process
// ``setWindowOpenHandler`` and spawn a new Electron BrowserWindow — not the
// user's browser. We intercept such clicks here and hand the URL to
// ``window.cremind.openExternal``, which the main process opens via
// ``shell.openExternal`` behind a scheme allowlist.
//
// Deliberately NOT affected (and must stay in-app):
//   - Programmatic ``window.open`` — OAuth/A2A sign-in popups (need the
//     same-context ``window.opener.postMessage`` callback) and blob:/about:
//     file previews (shell.openExternal can't render those). Those aren't
//     anchor clicks, so this handler never sees them. Any NEW external
//     programmatic open should call ``window.cremind.openExternal`` directly.
//   - Same-origin / backend links (SPA routes, ``/api/files/`` previews).
//
// In the web build ``window.cremind`` is absent, so the handler no-ops and the
// browser handles ``target="_blank"`` natively.

// Origins that must stay in-app rather than open externally: the live SPA
// origin plus the configured backend. The wheel-served SPA lives on a separate
// listener (default 1515) from the API (default 1112), so we add the 1515
// variant too — mirrors the 1112→1515 mapping in ui/src/main.ts's pivot logic.
function internalOrigins(): Set<string> {
  const origins = new Set<string>([window.location.origin]);
  const agentUrl = window.cremind?.config?.agentUrl;
  if (agentUrl) {
    try {
      const u = new URL(agentUrl);
      origins.add(u.origin);
      if (u.port === '1112' || !u.port) {
        u.port = '1515';
        origins.add(u.origin);
      }
    } catch {
      /* malformed agentUrl — ignore */
    }
  }
  return origins;
}

// Use composedPath() (not closest from e.target) so a click landing on a child
// element inside the anchor — or, in future, inside a shadow root — still
// resolves to the anchor.
function anchorFromEvent(e: Event): HTMLAnchorElement | null {
  for (const node of e.composedPath()) {
    if (node instanceof HTMLAnchorElement && node.href) return node;
  }
  return null;
}

function handleClick(e: MouseEvent): void {
  if (!window.cremind?.openExternal) return; // web build → native behaviour
  if (e.defaultPrevented) return;
  if (e.type === 'auxclick' && e.button !== 1) return; // only middle-click
  const anchor = anchorFromEvent(e);
  if (!anchor) return;
  if (anchor.hasAttribute('download')) return; // let downloads download

  let u: URL;
  try {
    u = new URL(anchor.href, window.location.href);
  } catch {
    return;
  }

  // mailto:/tel: have no origin — hand straight to the OS handler.
  if (u.protocol === 'mailto:' || u.protocol === 'tel:') {
    e.preventDefault();
    void window.cremind.openExternal(anchor.href);
    return;
  }

  // Only externalize web links to other origins. file:/blob:/about:/javascript:
  // /data: are left untouched, and same-origin / backend links stay in-app.
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return;
  if (internalOrigins().has(u.origin)) return;

  e.preventDefault();
  void window.cremind.openExternal(anchor.href);
}

export function installExternalLinkInterceptor(): void {
  // Capture phase so we run before Vue/Element Plus handlers and before the
  // default navigation / window.open that would spawn an Electron window.
  document.addEventListener('click', handleClick, true);
  document.addEventListener('auxclick', handleClick, true);
}
