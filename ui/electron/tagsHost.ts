// This computer as a Cremind Tag gateway computer: the computer a gateway
// plugs into, driving it for a Cremind that cannot see its USB ports (this
// computer's own Docker install, or a Cremind elsewhere).
//
// - A ``cremind://tags/setup?…`` link (from the OS, a second launch, or the
//   renderer's bridge) is validated here — exact action, one of each
//   parameter, a bare http(s) origin, a canonical session id, a token shape,
//   a pin only with https — and handed to ``cremind tags host enroll
//   --events`` on its STDIN: never on a command line (process lists), never
//   logged (only its redacted form), never stored.
// - The approval is this app's own dialog: server, profile, computer and the
//   four words the Cremind page shows too (``approve``).
// - Once enrolled, ``cremind tags host run`` is supervised: restarted with
//   back-off (1 s doubling to 60 s, reset after a minute of running), asked
//   to stop by closing its stdin at quit. Exit 3 (Cremind removed this
//   computer) and 4 (not set up) end the supervision with a notice.
//
// No Electron import here: main.ts injects dialogs, spawning and paths, so
// this module is tested on plain Node (tagsHost.test.mjs).

import fs from 'node:fs'
import path from 'node:path'
import type { ChildProcess, SpawnOptions } from 'node:child_process'

export const LINK_SCHEME = 'cremind:'
const MAX_LINK = 4096
const TOKEN = /^[A-Za-z0-9_-]{16,512}$/
const PIN = /^[0-9A-Fa-f]{64}$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
export const EXIT_REVOKED = 3
export const EXIT_NOT_ENROLLED = 4
const BACKOFF_START_MS = 1000
const BACKOFF_MAX_MS = 60_000
const HEALTHY_RUN_MS = 60_000
const STOP_GRACE_MS = 5000

export interface SetupLink {
  server: string
  session: string
  /** The whole link, secret included: memory only. */
  href: string
}

export class LinkRefused extends Error {}

/** A bare ``scheme://host[:port]`` origin, or null. */
function bareOrigin(value: string | null): string | null {
  if (!value || value.length > 255 || /[\s\\]/.test(value)) return null
  let url: URL
  try { url = new URL(value) } catch { return null }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  if (url.username || url.password || (url.pathname !== '/' && url.pathname !== '') || url.search || url.hash) return null
  if (value.includes('?') || value.includes('#') || value.includes('@')) return null
  return url.origin
}

/** Validate a ``cremind://tags/setup?…`` link (see the module comment); throws :class:`LinkRefused`. */
export function parseTagsSetupLink(raw: string): SetupLink {
  const text = (raw || '').trim()
  if (!text) throw new LinkRefused('No link was given.')
  if (text.length > MAX_LINK || /[\x00-\x1f\x7f]/.test(text)) throw new LinkRefused('This is not a Cremind setup link.')
  let url: URL
  try { url = new URL(text) } catch { throw new LinkRefused('This is not a Cremind setup link.') }
  if (url.protocol !== LINK_SCHEME) throw new LinkRefused('This is not a Cremind link.')
  // WHATWG URL keeps "tags" as the host of a non-special scheme: cremind://tags/setup
  if (url.host.toLowerCase() !== 'tags' || url.pathname !== '/setup' || url.hash) {
    throw new LinkRefused('This Cremind link asks for something this version cannot do.')
  }
  const seen = new Map<string, string>()
  for (const [key, value] of url.searchParams) {
    if (seen.has(key)) throw new LinkRefused(`The link gives '${key}' more than once.`)
    seen.set(key, value)
  }
  if (seen.get('v') !== '1') throw new LinkRefused('This link needs a newer Cremind. Update the Cremind app.')
  const server = bareOrigin(seen.get('server') ?? null)
  if (!server) throw new LinkRefused('The Cremind server address in the link is not valid.')
  const session = (seen.get('session') || '').toLowerCase()
  if (!UUID.test(session)) throw new LinkRefused("The link's setup session is not valid.")
  if (!TOKEN.test(seen.get('token') || '')) throw new LinkRefused("The link's setup code is missing or damaged.")
  const pin = seen.get('pin')
  if (pin !== undefined && (!PIN.test(pin) || !server.startsWith('https://'))) {
    throw new LinkRefused("The link's certificate fingerprint is not valid.")
  }
  return { server, session, href: text }
}

/** A link safe to log: the token is replaced. */
export function redactLink(raw: string): string {
  return String(raw || '').replace(/([?&]token=)[^&#]*/gi, '$1…')
}

export type EnrollEvent = { event: string; [key: string]: unknown }

/** One JSON line of ``enroll --events`` (anything else is ignored). */
export function parseEventLine(line: string): EnrollEvent | null {
  const text = line.trim()
  if (!text.startsWith('{')) return null
  try {
    const value = JSON.parse(text)
    return value && typeof value === 'object' && typeof value.event === 'string' ? value as EnrollEvent : null
  } catch {
    return null
  }
}

export interface Bound { server: string; profile: string; computer: string; words: string }

export interface TagsHostStatus {
  available: boolean
  reason?: string
  /** The gateway components are not installed in this app yet (``prepare`` installs them). */
  needsRuntime?: boolean
  enrolled: boolean
  server?: string
  profile?: string
  hostId?: string
  running: boolean
  state?: string
  detail?: string
}

export interface HostDeps {
  /** The ``cremind`` executable that can run ``tags host`` (null: the gateway components are not installed). */
  runtimeExe(): string | null
  env(): NodeJS.ProcessEnv
  systemDir(): string
  spawn(command: string, args: string[], options: SpawnOptions): ChildProcess
  /** This computer's own native Cremind drives its USB ports: nothing to set up here. */
  localBackendDrivesUsb(): boolean
  /** Ask the person (this app's dialog) to approve setting up this computer. */
  approve(bound: Bound): Promise<boolean>
  notify(title: string, body: string, kind: 'info' | 'error'): void
  log(line: string): void
  setTimer?(fn: () => void, ms: number): unknown
  clearTimer?(handle: unknown): void
  now?(): number
}

interface EnrollmentFile { server?: string; profile?: string; host_id?: string; host_name?: string }

export class TagsHostManager {
  private runner: ChildProcess | null = null
  private runnerStartedAt = 0
  private backoffMs = BACKOFF_START_MS
  private restartTimer: unknown = null
  private stopping = false
  private enrolling = false
  state = 'stopped'
  detail = ''

  constructor(private readonly deps: HostDeps) {}

  private now(): number { return this.deps.now ? this.deps.now() : Date.now() }

  private enrollmentPath(): string {
    return path.join(this.deps.systemDir(), '.tag-runtime', 'remote', 'enrollment.json')
  }

  /** The public part of this computer's enrollment (never its secret), or null. */
  enrollment(): EnrollmentFile | null {
    try {
      const doc = JSON.parse(fs.readFileSync(this.enrollmentPath(), 'utf8'))
      return doc && typeof doc === 'object' && typeof doc.host_id === 'string' ? doc : null
    } catch {
      return null
    }
  }

  status(): TagsHostStatus {
    const enrollment = this.enrollment()
    const base = {
      enrolled: !!enrollment, server: enrollment?.server, profile: enrollment?.profile, hostId: enrollment?.host_id,
      running: !!this.runner && this.runner.exitCode === null, state: this.state, detail: this.detail || undefined,
    }
    if (this.deps.localBackendDrivesUsb()) {
      return { ...base, available: false,
        reason: 'Cremind runs on this computer and drives its USB ports itself: no setup is needed here.' }
    }
    if (!this.deps.runtimeExe()) {
      return { ...base, available: false, needsRuntime: true,
        reason: 'The Cremind app on this computer does not have its gateway components yet.' }
    }
    return { ...base, available: true }
  }

  /** Complete a setup link (see the module comment). */
  async enroll(raw: string): Promise<{ ok: boolean; error?: string }> {
    let link: SetupLink
    try { link = parseTagsSetupLink(raw) } catch (e) { return { ok: false, error: (e as Error).message } }
    const status = this.status()
    if (!status.available) return { ok: false, error: status.reason }
    if (this.enrolling) return { ok: false, error: 'This computer is already being set up.' }
    const exe = this.deps.runtimeExe() as string
    this.enrolling = true
    this.deps.log(`setting up this computer for ${link.server} (${redactLink(link.href)})`)
    try {
      return await new Promise((resolve) => {
        const child = this.deps.spawn(exe, ['tags', 'host', 'enroll', '--events'], {
          env: this.deps.env(), stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
        })
        let outcome: { ok: boolean; error?: string } | null = null
        let buffer = ''
        let stderr = ''
        const answer = (text: string) => { try { child.stdin?.write(`${text}\n`) } catch { /* it ended */ } }
        child.stdout?.setEncoding('utf8')
        child.stdout?.on('data', (chunk: string) => {
          buffer += chunk
          let at = buffer.indexOf('\n')
          while (at >= 0) {
            const event = parseEventLine(buffer.slice(0, at))
            buffer = buffer.slice(at + 1)
            at = buffer.indexOf('\n')
            if (!event) continue
            if (event.event === 'bound') {
              const bound: Bound = { server: String(event.server || link.server), profile: String(event.profile || ''),
                computer: String(event.computer || ''), words: String(event.words || '') }
              void this.deps.approve(bound).then((ok) => answer(ok ? 'approve' : 'decline'), () => answer('decline'))
            } else if (event.event === 'enrolled') {
              outcome = { ok: true }
            } else if (event.event === 'failed') {
              outcome = { ok: false, error: String(event.message || 'The setup did not finish.') }
            }
          }
        })
        child.stderr?.setEncoding('utf8')
        child.stderr?.on('data', (chunk: string) => { stderr = (stderr + chunk).slice(-2000) })
        child.on('error', (err) => {
          outcome = { ok: false, error: `The gateway components could not be started (${err.message}).` }
        })
        child.on('close', (code) => {
          if (!outcome) outcome = code === 0 ? { ok: true } : { ok: false, error: stderr.trim() || 'The setup did not finish.' }
          resolve(outcome)
        })
        answer(link.href)
      })
    } finally {
      this.enrolling = false
      if (this.enrollment()) {
        this.restartRunner()
      }
    }
  }

  /** Start the runner if this computer is set up (at launch, after an enrollment). */
  startIfEnrolled(): void {
    if (this.enrollment() && this.status().available) this.startRunner()
  }

  private startRunner(): void {
    if (this.runner && this.runner.exitCode === null) return
    const exe = this.deps.runtimeExe()
    if (!exe) return
    this.stopping = false
    this.state = 'starting'
    this.detail = ''
    let log: fs.WriteStream | null = null
    try {
      log = fs.createWriteStream(path.join(this.deps.systemDir(), 'tag-host.log'), { flags: 'a' })
    } catch { /* logging is best effort */ }
    const child = this.deps.spawn(exe, ['tags', 'host', 'run'], {
      env: { ...this.deps.env(), CREMIND_TAG_HOST_SUPERVISED: '1' }, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
    })
    this.runner = child
    this.runnerStartedAt = this.now()
    child.stdout?.on('data', (chunk) => { log?.write(chunk); this.state = 'running' })
    child.stderr?.on('data', (chunk) => {
      log?.write(chunk)
      this.detail = String(chunk).trim().split('\n').pop()?.slice(0, 300) || this.detail
    })
    child.on('error', (err) => { this.detail = err.message })
    child.on('close', (code) => {
      log?.end()
      if (this.runner === child) this.runner = null
      if (this.stopping) { this.state = 'stopped'; return }
      if (code === EXIT_REVOKED) {
        this.state = 'revoked'
        this.deps.notify('This computer no longer drives gateways',
          'Cremind removed it as a gateway computer. Set it up again from Settings → Tags if you still need it.', 'error')
        return
      }
      if (code === EXIT_NOT_ENROLLED) { this.state = 'not_enrolled'; return }
      const ranFor = this.now() - this.runnerStartedAt
      if (ranFor >= HEALTHY_RUN_MS) this.backoffMs = BACKOFF_START_MS
      this.state = 'restarting'
      const delay = this.backoffMs
      this.backoffMs = Math.min(this.backoffMs * 2, BACKOFF_MAX_MS)
      this.deps.log(`gateway runner ended (${code}); again in ${Math.round(delay / 1000)} s`)
      const set = this.deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms))
      this.restartTimer = set(() => { this.restartTimer = null; this.startRunner() }, delay)
    })
  }

  private restartRunner(): void {
    this.stopRunner()
    this.backoffMs = BACKOFF_START_MS
    // The old one exits on its stdin closing; start once it is gone.
    const waitThenStart = (tries: number) => {
      if (this.runner && this.runner.exitCode === null && tries > 0) {
        const set = this.deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms))
        set(() => waitThenStart(tries - 1), 200)
        return
      }
      this.startRunner()
    }
    waitThenStart(50)
  }

  /** Ask the runner to stop (its stdin closes; a kill follows after a grace period). */
  stopRunner(): void {
    this.stopping = true
    if (this.restartTimer) {
      (this.deps.clearTimer ?? ((h) => clearTimeout(h as NodeJS.Timeout)))(this.restartTimer)
      this.restartTimer = null
    }
    const child = this.runner
    if (!child || child.exitCode !== null) return
    try { child.stdin?.end() } catch { /* already closed */ }
    const set = this.deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms))
    set(() => { if (child.exitCode === null) { try { child.kill() } catch { /* gone */ } } }, STOP_GRACE_MS)
  }

  /** At quit: stop now (the app is about to exit). */
  stopRunnerSync(killTree: (pid: number) => void): void {
    this.stopping = true
    const child = this.runner
    if (!child || child.exitCode !== null || !child.pid) return
    try { child.stdin?.end() } catch { /* already closed */ }
    killTree(child.pid)
  }

  /** Stop being a gateway computer (``cremind tags host forget --yes``). */
  async forget(): Promise<{ ok: boolean; error?: string }> {
    const exe = this.deps.runtimeExe()
    if (!exe) return { ok: false, error: 'The gateway components are not installed here.' }
    this.stopRunner()
    return new Promise((resolve) => {
      const child = this.deps.spawn(exe, ['tags', 'host', 'forget', '--yes'], {
        env: this.deps.env(), stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
      })
      let stderr = ''
      child.stderr?.setEncoding('utf8')
      child.stderr?.on('data', (chunk: string) => { stderr = (stderr + chunk).slice(-2000) })
      child.on('error', (err) => resolve({ ok: false, error: err.message }))
      child.on('close', (code) => {
        this.state = 'stopped'
        resolve(code === 0 ? { ok: true } : { ok: false, error: stderr.trim() || 'Cremind could not be told.' })
      })
    })
  }
}
