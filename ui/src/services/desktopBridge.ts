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
