/**
 * Where the VNC desktop answers, composed in the browser.
 *
 * The split is deliberate: the *server* names the shape (its own published
 * port, a path on Cremind's origin, or a tunnel that has to exist first) —
 * only it knows how the image and the chart wired noVNC — while the *browser*
 * fills in the address. A container has no idea which host name reached it
 * (localhost, a LAN name, an Ingress host, a port-forward), so a URL composed
 * server-side would be right on the developer's box and wrong everywhere else.
 *
 * These are pure string builders with no Vue and no fetch, so the config
 * export, the Desktop card and the tests all compose the same URL.
 */

import type { VncAccess } from '../services/configApi';

/** The desktop's path when noVNC serves itself (Docker, and the Kubernetes
 *  relay where noVNC has its own Service port). */
const DIRECT_NOVNC_PATH = '/vnc.html';

/** The desktop's path behind the Kubernetes proxy sidecar, which fronts the
 *  SPA, the API and noVNC on one origin. */
const PROXY_NOVNC_PATH = '/vnc/vnc.html';

/** Query the desktop windows open with: skip noVNC's own connect splash, and
 *  let the remote screen follow the window instead of scrolling inside it. */
const VNC_WINDOW_QUERY = 'autoconnect=1&resize=remote';

/** noVNC on its own published port on `host` — the Docker desktop image, and
 *  anything else that publishes 6080 next to Cremind's own port.
 *
 *  Always http: that port is websockify's, not Cremind's, so it is plain even
 *  when Cremind itself serves HTTPS. */
export function novncUrlForDockerHost(host: string, port: number, path = DIRECT_NOVNC_PATH): string {
  return `http://${host}:${port}${path}`;
}

/** noVNC as another path on Cremind's own origin — the Kubernetes proxy
 *  sidecar. It inherits the scheme and the tunnel the user already has open,
 *  so `origin` must be passed in whole rather than rebuilt from parts. */
export function novncUrlOnOrigin(origin: string, path = PROXY_NOVNC_PATH): string {
  return `${origin}${path}`;
}

/** The URL to show and open for `vnc`, or null when this install has no
 *  desktop (native, and the basic image).
 *
 *  `loc` is `window.location` narrowed to the two fields that matter, so the
 *  callers can be tested without a DOM.
 */
export function novncDisplayUrl(
  vnc: VncAccess,
  loc: { hostname: string; origin: string },
): string | null {
  if (!vnc.enabled || !vnc.access) return null;
  switch (vnc.access) {
    case 'direct':
      // Host from this page, port from the server: the container cannot know
      // which address the browser used to reach it.
      return novncUrlForDockerHost(
        loc.hostname,
        vnc.novnc_port ?? 6080,
        vnc.novnc_path || DIRECT_NOVNC_PATH,
      );
    case 'same_origin':
      return novncUrlOnOrigin(loc.origin, vnc.novnc_path || PROXY_NOVNC_PATH);
    case 'port_forward':
      // Only reachable through the tunnel the card prints, and a tunnel always
      // lands on the machine running kubectl — never on `loc.hostname`, which
      // may be the Ingress host.
      return novncUrlForDockerHost(
        'localhost',
        vnc.novnc_port ?? 6080,
        vnc.novnc_path || DIRECT_NOVNC_PATH,
      );
  }
}

/** The same URL with the window query noVNC needs to connect on its own.
 *  Idempotent, so a `novnc_url` the chart already decorated is left alone. */
export function novncOpenUrl(display: string): string {
  if (display.includes('autoconnect=')) return display;
  return `${display}${display.includes('?') ? '&' : '?'}${VNC_WINDOW_QUERY}`;
}
