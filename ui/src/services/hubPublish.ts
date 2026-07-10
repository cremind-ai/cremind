/**
 * One-click "Upload to Cremind Hub" — the browser hand-off (Method 2).
 *
 * Uploading only STORES the blueprint on the Hub as a draft; the user then decides whether
 * to publish it from the Hub's blueprint page.
 *
 * Flow (ordering matters — see below):
 *  1. Open the Hub authorize page in a new TAB FIRST, synchronously in the click handler,
 *     so the browser doesn't block it (any `await` before `window.open` loses the user
 *     gesture). If the user isn't signed into the Hub, that page redirects to its own
 *     `/login?returnTo=…`.
 *  2. Fetch the blueprint archive from the LOCAL backend as a Blob.
 *  3. Await the Hub's postMessage carrying a single-use upload token, accepting it only
 *     from the exact Hub origin and the tab we opened.
 *  4. Upload the archive cross-origin to `<HUB>/api/blueprints` with `Authorization:
 *     Bearer <token>` (no cookies → simple CORS). The Hub stores it as a DRAFT.
 *  5. Navigate the SAME tab to the uploaded (draft) Hub blueprint page.
 *
 * The Hub session cookie is never available cross-origin, which is exactly why the token
 * is minted in a first-party Hub tab and used as a Bearer here.
 */

import { fetchBlueprintBlob } from './blueprintApi';

export interface UploadResult {
  url: string; // relative Hub path, e.g. /blueprints/<canonical>
  hubUrl: string; // absolute page URL
}

interface UploadOptions {
  agentUrl: string; // local backend
  authToken: string; // local JWT
  hubUrl: string; // hub base (from getHubUrl)
  name: string; // stored archive filename (BlueprintEntry.name)
  displayName?: string;
  timeoutMs?: number;
}

const TOKEN_MESSAGE_TYPE = 'cremind-publish-token';

function stripSuffix(name: string): string {
  return name.replace(/\.cremind-blueprint$/i, '');
}

export async function uploadBlueprintToHub(opts: UploadOptions): Promise<UploadResult> {
  const hubBase = opts.hubUrl.replace(/\/$/, '');
  const hubOrigin = new URL(hubBase).origin;
  const base = stripSuffix(opts.name);
  const display = opts.displayName || base;
  const spaOrigin = window.location.origin;

  const authorizeUrl =
    `${hubBase}/publish/authorize?app=cremind` +
    `&origin=${encodeURIComponent(spaOrigin)}` +
    `&name=${encodeURIComponent(base)}` +
    `&display=${encodeURIComponent(display)}`;

  // (1) Open a new TAB FIRST — before any await — or the browser blocks it. No window
  // features string → a tab, not a popup window.
  const tab = window.open(authorizeUrl, '_blank');
  if (!tab) {
    throw new Error('Could not open a new tab — allow pop-ups/tabs for this site, then try again.');
  }

  try {
    // (2) Fetch the archive from the local backend.
    const blob = await fetchBlueprintBlob(opts.agentUrl, opts.authToken, opts.name);

    // (3) Await the upload token via postMessage from the Hub tab.
    const token = await waitForToken(tab, hubOrigin, opts.timeoutMs ?? 5 * 60_000);

    // (4) Upload cross-origin with the Bearer token (no cookies).
    const form = new FormData();
    form.append('file', new File([blob], opts.name, { type: 'application/gzip' }));
    const res = await fetch(`${hubBase}/api/blueprints`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      credentials: 'omit',
      body: form,
    });
    if (!res.ok) {
      let msg = res.statusText;
      try {
        const body = await res.json();
        msg = body?.message || body?.error || msg;
      } catch {
        /* keep statusText */
      }
      throw new Error(`Upload failed: ${msg}`);
    }
    const body = (await res.json()) as { url: string };
    const abs = `${hubBase}${body.url}`;

    // (5) Land on the uploaded (draft) Hub page — reuse the same tab.
    if (window.cremind?.openExternal) {
      window.cremind.openExternal(abs);
      if (!tab.closed) tab.close();
    } else if (!tab.closed) {
      tab.location.href = abs;
    } else {
      window.open(abs, '_blank');
    }
    return { url: body.url, hubUrl: abs };
  } catch (e) {
    if (!tab.closed) tab.close();
    throw e;
  }
}

/** Resolve with the token, or reject on cancel (popup closed) / timeout. */
function waitForToken(popup: Window, hubOrigin: string, timeoutMs: number): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    let done = false;
    const finish = (fn: () => void) => {
      if (done) return;
      done = true;
      window.removeEventListener('message', onMessage);
      clearInterval(closedTimer);
      clearTimeout(timeoutTimer);
      fn();
    };

    const onMessage = (e: MessageEvent) => {
      if (e.origin !== hubOrigin) return;
      if (e.source !== popup) return;
      const data = e.data as { type?: string; token?: string };
      if (data?.type !== TOKEN_MESSAGE_TYPE || !data.token) return;
      finish(() => resolve(data.token as string));
    };

    const closedTimer = setInterval(() => {
      if (popup.closed) finish(() => reject(new Error('Publishing cancelled.')));
    }, 500);

    const timeoutTimer = setTimeout(
      () => finish(() => reject(new Error('Timed out waiting for Hub authorization.'))),
      timeoutMs,
    );

    window.addEventListener('message', onMessage);
  });
}
