// Where this install's noVNC desktop actually answers.
//
// The backend works it out (app/config/runtime_env.py) because only it knows
// the compose port, the Kubernetes proxy path and whether in-pod TLS turned
// the sidecar into a TCP relay; it publishes the unauthenticated subset on
// /api/services/tray-capabilities. Electron only turns that descriptor into a
// URL. Kept free of electron imports so these rules stay unit-testable on their
// own; main.ts itself is only reachable through the bundled harness in
// https.test.mjs.

export type VncAccess = 'direct' | 'same_origin' | 'port_forward'

export type VncDescriptor = {
  enabled?: boolean
  access?: VncAccess | null
  novnc_path?: string | null
  novnc_port?: number | string | null
}

/** ``autoconnect`` skips noVNC's connect splash; ``resize=remote`` is standard
 *  UX polish for a windowed viewer. */
export const VNC_WINDOW_QUERY = 'autoconnect=1&resize=remote'

// docker-compose's ``NOVNC_PORT:-6080`` (install/templates/docker-compose.yml.tmpl)
// and the Service port the Kubernetes relay exposes.
const DEFAULT_NOVNC_PORT = 6080

function novncPort(raw: unknown): number {
  const port = typeof raw === 'string' ? Number(raw) : raw
  return typeof port === 'number' && Number.isInteger(port) && port > 0 && port < 65536
    ? port : DEFAULT_NOVNC_PORT
}

// A path is resolved against an origin, so a protocol-relative value
// ("//elsewhere.example/vnc.html") would silently move the host. Only a single
// leading slash is a path.
function novncPath(raw: unknown, fallback: string): string {
  return typeof raw === 'string' && raw.startsWith('/') && !raw.startsWith('//')
    ? raw : fallback
}

function windowUrl(base: string, pathname: string): string | null {
  try {
    const url = new URL(pathname, base)
    // A descriptor that already carries connect parameters keeps them.
    if (!url.search) url.search = VNC_WINDOW_QUERY
    return url.toString()
  } catch { return null }
}

/**
 * The noVNC URL to point a desktop window at, or null when this install has
 * no desktop to open.
 *
 * ``descriptor`` null means the backend predates the descriptor: fall back to
 * the rule that shipped before it, where a Docker install on a non-basic image
 * always meant noVNC on 6080 (every pre-flavor image was a desktop image).
 */
export function vncUrlFor(
  agentUrl: string,
  descriptor: VncDescriptor | null | undefined,
  installMode: 'docker' | 'native' | 'kubernetes' | null,
  imageFlavor: 'desktop' | 'basic' | null,
): string | null {
  let agent: URL
  try { agent = new URL(agentUrl) } catch { return null }
  if (agent.protocol !== 'http:' && agent.protocol !== 'https:') return null
  // noVNC owns a separate listener: enabling TLS on Cremind's own origin does
  // not enable it on the noVNC port, so ``direct`` is always plain http.
  const directBase = (port: number): string => `http://${agent.hostname}:${port}`

  if (!descriptor) {
    if (installMode !== 'docker' || imageFlavor === 'basic') return null
    return windowUrl(directBase(DEFAULT_NOVNC_PORT), '/vnc.html')
  }
  if (!descriptor.enabled || !descriptor.access) return null
  const port = novncPort(descriptor.novnc_port)
  switch (descriptor.access) {
    case 'direct':
      return windowUrl(directBase(port), '/vnc.html')
    case 'same_origin':
      // The sidecar serves noVNC on the app's own origin, so it follows
      // whatever scheme the user reaches Cremind with.
      return windowUrl(agent.origin, novncPath(descriptor.novnc_path, '/vnc/vnc.html'))
    case 'port_forward':
      // Only reachable through the user's own ``kubectl port-forward`` tunnel,
      // which always lands on their machine.
      return windowUrl(`http://localhost:${port}`, novncPath(descriptor.novnc_path, '/vnc.html'))
    default:
      return null
  }
}

/**
 * Sanitize a URL a renderer asked us to open as a desktop window.
 *
 * The renderer is never trusted with this: it hands over whatever the backend
 * advertised, and a crafted value could otherwise ask a preload-less,
 * sandboxed window to load ``file://`` or a custom protocol handler. Embedded
 * credentials are refused too: they would be handed to whatever host answers.
 */
export function validVncUrl(raw: unknown): string | null {
  if (typeof raw !== 'string' || !raw) return null
  let url: URL
  try { url = new URL(raw) } catch { return null }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  if (url.username || url.password) return null
  if (!url.search) url.search = VNC_WINDOW_QUERY
  return url.toString()
}
