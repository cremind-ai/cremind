/**
 * The client-protocol marker every request to the Cremind backend carries.
 *
 * Some identifiers changed meaning in the document-search rename: the tool id
 * `documentation_search` used to be Cremind's own manual search and is now the
 * personal-document search, and cleanup/setup keys moved with it. A tab still
 * running the old SPA would send the old meaning under the new name — turning
 * off the wrong tool, cleaning the wrong index — and the server cannot tell the
 * two apart from the body alone. So an up-to-date client says which protocol it
 * speaks, and the server refuses the affected mutations from one that does not.
 *
 * Added by the one `window.fetch` wrapper in `main.ts` (the same chokepoint the
 * 401 handler uses), so every service module, fetch-based SSE reader and the
 * A2A transport carry it without each remembering to. Only for the backend's
 * own origin — a custom header on a third-party request (Google, the Hub) would
 * turn a simple request into a CORS preflight that origin never agreed to — and
 * never for `/api/tls/*`: the HTTPS hand-off calls those across origins, and
 * their CORS answer (`TlsHandoffCors`) names exactly the headers it allows.
 */

export const CLIENT_PROTOCOL_HEADER = 'X-Cremind-Client-Protocol';
export const CLIENT_PROTOCOL_VERSION = '2';

/**
 * Whether a request to `url` should carry the marker. `backendOrigin` is the
 * resolved API origin (`getApiOrigin()`); an empty or unparsable one matches
 * nothing, so a misconfigured build sends no header rather than leaking it.
 */
export function wantsClientProtocol(url: URL, backendOrigin: string): boolean {
  if (url.pathname.startsWith('/api/tls/')) return false;
  if (!backendOrigin) return false;
  let origin: string;
  try {
    origin = new URL(backendOrigin, window.location.href).origin;
  } catch {
    return false;
  }
  return url.origin === origin;
}

/**
 * The `init` to hand to the real `fetch` for this request: the caller's own,
 * plus the marker when the request is for the backend. Returns `init` untouched
 * otherwise (or when the caller already set the header).
 *
 * A `Request` input keeps its own headers: `fetch(request, init)` replaces the
 * request's headers wholesale when `init.headers` is given, so they are copied
 * across before the marker is added.
 */
export function withClientProtocol(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  backendOrigin: string,
): RequestInit | undefined {
  let url: URL;
  try {
    const raw = input instanceof Request ? input.url : String(input);
    url = new URL(raw, window.location.href);
  } catch {
    return init;
  }
  if (!wantsClientProtocol(url, backendOrigin)) return init;
  const source = init?.headers ?? (input instanceof Request ? input.headers : undefined);
  const headers = new Headers(source);
  if (headers.has(CLIENT_PROTOCOL_HEADER)) return init;
  headers.set(CLIENT_PROTOCOL_HEADER, CLIENT_PROTOCOL_VERSION);
  return { ...(init ?? {}), headers };
}
