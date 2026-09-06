import { X509Certificate } from 'node:crypto'
import { isIP } from 'node:net'
import type { ChildProcess } from 'node:child_process'

/** Parse installer dotenv files as data. Never execute a shell or expand secrets. */
export function parseInstallEnv(text: string): Record<string, string> {
  const values: Record<string, string> = {}
  for (const line of text.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const match = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line)
    if (!match) continue
    let value = match[2].trim()
    if (value.startsWith('"') || value.startsWith("'")) {
      const quote = value[0]
      const end = value.lastIndexOf(quote)
      if (end < 1 || !/^\s*(?:#.*)?$/.test(value.slice(end + 1))) continue
      value = value.slice(1, end)
      if (quote === '"') value = value.replace(/\\([\\"nr])/g, (_m, c: string) => c === 'n' ? '\n' : c === 'r' ? '\r' : c)
    } else {
      value = value.startsWith('#') ? '' : value.replace(/\s+#.*$/, '').trimEnd()
    }
    values[match[1]] = value
  }
  return values
}

export function httpOrigin(value: string | undefined): string | null {
  try {
    const url = new URL(value ?? '')
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null
    return url.origin
  } catch { return null }
}

/** The native listener keeps its TCP port, including an explicitly used port 80. */
export function httpsOrigin(value: string): string {
  const url = new URL(value)
  const port = url.port || (url.protocol === 'http:' ? '80' : '443')
  url.protocol = 'https:'
  url.port = port
  return url.origin
}

/** Map only the standard HTTP endpoint to its standard HTTPS counterpart. */
export function defaultPortHttpsOrigin(value: string): string | null {
  const source = httpOrigin(value)
  if (!source) return null
  const url = new URL(source)
  if (url.protocol !== 'http:' || (url.port && url.port !== '80')) return null
  url.protocol = 'https:'
  url.port = ''
  return url.origin
}

type TlsStatusRecord = {
  serving_https?: unknown
  ready?: unknown
  instance_id?: unknown
  transition?: unknown
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

/** Installation ids are persistent 192-bit values written as lowercase hex. */
export function tlsStatusInstanceId(value: unknown): string | null {
  const status = objectRecord(value) as TlsStatusRecord | null
  const candidate = status?.instance_id
  return typeof candidate === 'string' && /^[a-f0-9]{48}$/.test(candidate)
    ? candidate : null
}

/**
 * Authenticate a closed-app HTTP:80 -> HTTPS:443 discovery using identity
 * remembered during an earlier, readable connection to the same installation.
 */
export function isExpectedDefaultPortTlsStatus(
  value: unknown,
  sourceOrigin: string,
  targetOrigin: string,
  expectedInstanceId: string,
): boolean {
  const status = objectRecord(value) as TlsStatusRecord | null
  const transition = objectRecord(status?.transition)
  const source = httpOrigin(sourceOrigin)
  const target = httpOrigin(targetOrigin)
  return Boolean(
    status
      && tlsStatusInstanceId(status) === expectedInstanceId
      && status.serving_https === true
      && status.ready !== false
      && transition
      && transition.phase === 'active'
      && transition.same_public_port === false
      && source
      && target
      && defaultPortHttpsOrigin(source) === target
      && httpOrigin(typeof transition.source_origin === 'string' ? transition.source_origin : undefined) === source
      && httpOrigin(typeof transition.target_origin === 'string' ? transition.target_origin : undefined) === target,
  )
}

// tls.py publishes the durable "activating" phase before its 202 response is
// written. Keep the owned child alive long enough for Chromium to receive it.
export const ACTIVATION_RESPONSE_GRACE_MS = 2_000

export function waitForActivationResponseGrace(
  delayMs = ACTIVATION_RESPONSE_GRACE_MS,
): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, delayMs)))
}

export function resolveBackendOrigin(
  configured: string,
  env: Record<string, string | undefined>,
  setupComplete: boolean,
  enabledBySettings = false,
): string {
  const fallbackPort = /^\d+$/.test(env.CREMIND_UI_PORT ?? '') && Number(env.CREMIND_UI_PORT) > 0
    ? env.CREMIND_UI_PORT! : '1515'
  const url = new URL(httpOrigin(configured) || httpOrigin(env.APP_URL) || `http://127.0.0.1:${fallbackPort}`)
  // Existing desktop configs used the default literal even with a custom bind.
  if (['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
      && url.port === '1515' && fallbackPort !== '1515') url.port = fallbackPort
  const mode = (env.CREMIND_SSL ?? '').trim().toLowerCase()
  const hasPair = Boolean(env.CREMIND_SSL_CERTFILE?.trim() && env.CREMIND_SSL_KEYFILE?.trim())
  if (enabledBySettings || hasPair || ['auto', 'true', '1', 'yes'].includes(mode) || (mode === 'after-setup' && setupComplete)) {
    return httpsOrigin(url.origin)
  }
  if (mode === 'after-setup' && !setupComplete && url.protocol === 'https:') {
    const port = url.port || '443'
    url.protocol = 'http:'
    url.port = port
  }
  return url.origin
}

/** Trust only the native installation's exact, valid generated leaf certificate. */
export function isExpectedLocalCertificate(
  presentedPem: string, expectedPem: string, caPem: string, hostname: string,
  now = Date.now(),
): boolean {
  try {
    const leaf = new X509Certificate(presentedPem)
    const expected = new X509Certificate(expectedPem)
    const ca = new X509Certificate(caPem)
    if (!leaf.raw.equals(expected.raw) || leaf.ca || !ca.ca) return false
    for (const cert of [leaf, ca]) {
      if (!(Date.parse(cert.validFrom) <= now && now <= Date.parse(cert.validTo))) return false
    }
    if (!leaf.checkIssued(ca) || !leaf.verify(ca.publicKey)) return false
    if (!leaf.keyUsage?.includes('1.3.6.1.5.5.7.3.1')) return false
    const host = hostname.replace(/^\[|\]$/g, '')
    return Boolean(isIP(host) ? leaf.checkIP(host) : leaf.checkHost(host, { subject: 'never' }))
  } catch { return false }
}

/** Never respawn while the previous child is still releasing its listeners. */
export function stopOwnedBackend(
  child: ChildProcess, terminateTree: (pid: number) => void,
  graceMs = 15000, forceMs = 5000,
): Promise<boolean> {
  return new Promise((resolve) => {
    let finished = false
    let timer: ReturnType<typeof setTimeout>
    const done = (stopped: boolean) => {
      if (finished) return
      finished = true
      clearTimeout(timer)
      child.off('exit', onExit)
      resolve(stopped)
    }
    const onExit = () => done(true)
    child.once('exit', onExit)
    timer = setTimeout(() => {
      try {
        if (child.pid) terminateTree(child.pid)
        else child.kill('SIGKILL')
      } catch { /* surface failure below if the owned process cannot stop */ }
      if (!finished) timer = setTimeout(() => done(child.exitCode !== null), forceMs)
    }, graceMs)
    try { child.kill('SIGTERM') } catch {
      if (child.exitCode !== null) done(true)
    }
  })
}
