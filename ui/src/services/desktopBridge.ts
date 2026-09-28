// The desktop app's "keep Cremind running" bridge, as Settings → Tags uses it.
//
// The desktop app may expose `window.cremind.background`: keep the local
// Cremind running after the window closes, through the tray and the startup
// settings. It is optional and feature-detected (the SPA can be newer than
// the desktop shell that loads it, and a browser has none), and its own
// `status().applicable` says whether the backend is the app's to keep —
// never guessed from this page's address.

export type BackgroundBridge = NonNullable<NonNullable<Window['cremind']>['background']>;

export function backgroundBridge(): BackgroundBridge | null {
  const b = typeof window === 'undefined' ? undefined : window.cremind?.background;
  return b && typeof b.status === 'function' && typeof b.enable === 'function' ? b : null;
}

export type TagsHostBridge = NonNullable<NonNullable<Window['cremind']>['tagsHost']>;

/** The desktop app's gateway-computer bridge: it sets up the computer it runs on from a `cremind://tags/setup`
 *  link (its own approval dialog shows the words) and reports whether it can — never guessed from here. */
export function tagsHostBridge(): TagsHostBridge | null {
  const b = typeof window === 'undefined' ? undefined : window.cremind?.tagsHost;
  return b && typeof b.status === 'function' && typeof b.enroll === 'function' ? b : null;
}

/**
 * Hand a `cremind://tags/setup` link to the Cremind app on this computer: over IPC in the desktop app, else by
 * clicking a hidden anchor inside the person's click (a browser hands a custom scheme to the OS only from a user
 * gesture; `openExternal` refuses custom schemes on purpose). The link is never stored.
 */
export async function openSetupLink(url: string): Promise<{ ok: boolean; error?: string }> {
  const bridge = tagsHostBridge();
  if (bridge) {
    try {
      return await bridge.enroll(url);
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) };
    }
  }
  try {
    const a = document.createElement('a');
    a.href = url;
    a.rel = 'noopener noreferrer';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    a.remove();
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}
