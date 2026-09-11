// Shared loader for the SPA regression tests (node:test, no browser).
//
// A TypeScript entry under src/ is bundled with esbuild — vue included, the
// network-backed SSE multiplexer stubbed — and imported from a data: URL.
// Every `load()` returns a *fresh* module instance, because the transition
// coordinator keeps module-level state (poll generation, pinned transition)
// that must not leak between tests. Browser globals are fakes installed on
// globalThis before the import: a scriptable location, in-memory storage, a
// silent BroadcastChannel (a real one would keep the test process alive and
// talk to the other copies), and a fetch routed through `env.route()`.
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const here = path.dirname(fileURLToPath(import.meta.url))
const uiRoot = path.resolve(here, '..')

const STUBS = {
  // The real module opens a shared EventSource per profile. Tests push
  // announcements through the captured callback instead.
  './profileEventsStream': `
    export function subscribeTransportChange(agentUrl, token, onChange) {
      (globalThis.__transportSubscribers ||= []).push(onChange)
      return { close() {} }
    }
  `,
}

const bundles = new Map()
let instance = 0

async function bundle(entry) {
  if (!bundles.has(entry)) {
    bundles.set(entry, build({
      entryPoints: [path.join(uiRoot, entry)],
      bundle: true,
      format: 'esm',
      platform: 'browser',
      target: 'es2022',
      write: false,
      logLevel: 'silent',
      define: {
        'process.env.NODE_ENV': '"production"',
        __VUE_OPTIONS_API__: 'false',
        __VUE_PROD_DEVTOOLS__: 'false',
        __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false',
      },
      plugins: [{
        name: 'test-stubs',
        setup(builder) {
          builder.onResolve({ filter: /profileEventsStream$/ }, args => ({
            path: './profileEventsStream', namespace: 'stub', pluginData: args.path,
          }))
          builder.onLoad({ filter: /.*/, namespace: 'stub' }, args => ({
            contents: STUBS[args.path], loader: 'js',
          }))
        },
      }],
    }).then(result => result.outputFiles[0].text))
  }
  return bundles.get(entry)
}

/** Import a fresh copy of `entry` (a path relative to ui/). */
export async function load(entry) {
  const code = `${await bundle(entry)}\n//# instance ${instance++}\n`
  return import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
}

class MemoryStorage {
  #items = new Map()
  get length() { return this.#items.size }
  key(index) { return [...this.#items.keys()][index] ?? null }
  getItem(key) { return this.#items.has(key) ? this.#items.get(key) : null }
  setItem(key, value) { this.#items.set(String(key), String(value)) }
  removeItem(key) { this.#items.delete(key) }
  clear() { this.#items.clear() }
}

class SilentBroadcastChannel {
  constructor(name) { this.name = name }
  postMessage() {}
  addEventListener() {}
  removeEventListener() {}
  close() {}
}

export function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  })
}

/** A promise with its resolve/reject handles exposed. */
export function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

/** Let every queued promise continuation run (fetch bodies included). */
export async function flush(rounds = 3) {
  for (let i = 0; i < rounds; i += 1) await new Promise(resolve => setImmediate(resolve))
}

/** Advance mocked timers in small steps, draining promises in between, so
 *  loops that await between timers make progress like they would for real. */
export async function advance(timers, ms, step = 50) {
  for (let elapsed = 0; elapsed < ms; elapsed += step) {
    timers.tick(Math.min(step, ms - elapsed))
    await flush()
  }
}

/** Wait (on real time) until `predicate()` holds. */
export async function until(predicate, label = 'condition', limitMs = 3000) {
  const deadline = Date.now() + limitMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`)
    await flush(1)
  }
}

/**
 * Install fake browser globals. `env.route(match, handler)` answers fetches
 * whose URL ends with/contains `match`; unrouted requests reject like a
 * refused connection. Every call is recorded in `env.calls`.
 */
export function installBrowser({ href = 'http://localhost:1515/#/alice/c/42' } = {}) {
  let url = new URL(href)
  const replaced = []
  const calls = []
  const routes = []
  const location = {
    get href() { return url.href },
    get origin() { return url.origin },
    get protocol() { return url.protocol },
    get host() { return url.host },
    get hostname() { return url.hostname },
    get port() { return url.port },
    get pathname() { return url.pathname },
    get search() { return url.search },
    get hash() { return url.hash },
    replace(next) { replaced.push(String(next)) },
    assign(next) { replaced.push(String(next)) },
  }
  const windowObject = {
    location,
    history: {
      state: null,
      replaceState(_state, _title, next) { if (next) url = new URL(String(next), url) },
      pushState(_state, _title, next) { if (next) url = new URL(String(next), url) },
    },
    addEventListener() {},
    removeEventListener() {},
    // Through globalThis at call time, so node:test's mocked timers apply.
    setTimeout: (...args) => globalThis.setTimeout(...args),
    clearTimeout: (...args) => globalThis.clearTimeout(...args),
    setInterval: (...args) => globalThis.setInterval(...args),
    clearInterval: (...args) => globalThis.clearInterval(...args),
    cremind: undefined,
    opener: null,
  }
  globalThis.window = windowObject
  globalThis.document = {
    visibilityState: 'visible',
    addEventListener() {},
    removeEventListener() {},
  }
  globalThis.localStorage = new MemoryStorage()
  globalThis.sessionStorage = new MemoryStorage()
  globalThis.BroadcastChannel = SilentBroadcastChannel
  globalThis.__transportSubscribers = []
  globalThis.fetch = async (input, init = {}) => {
    const requested = String(input)
    calls.push({ url: requested, init })
    const handler = routes.find(route => requested.includes(route.match))
    if (!handler) throw new TypeError(`Failed to fetch ${requested}`)
    return handler.respond(requested, init)
  }
  return {
    replaced,
    calls,
    route(match, respond) { routes.unshift({ match, respond }) },
    callsTo(match) { return calls.filter(call => call.url.includes(match)) },
    setHref(next) { url = new URL(next) },
  }
}

// ── transition fixtures ────────────────────────────────────────────────────

export const INSTANCE = 'a'.repeat(48)
export const CA = 'CA:AA:AA'
export const LEAF_A = 'LE:AF:0A'
export const LEAF_B = 'LE:AF:0B'
export const TICKET = 'T'.repeat(43)

export function transition(overrides = {}) {
  return {
    version: 1,
    id: 'x'.repeat(32),
    phase: 'activating',
    source_origin: 'http://localhost:1515',
    target_origin: 'https://localhost:1515',
    instance_id: INSTANCE,
    certificate_kind: 'local',
    certificate_sha256: LEAF_A,
    ca_sha256: CA,
    same_public_port: true,
    public_port: 1515,
    created_at: 1_700_000_000,
    expires_at: null,
    awaiting_operator: false,
    activation_error: null,
    ...overrides,
  }
}

export function status(transitionOverrides = {}, overrides = {}) {
  const target = transition({ phase: 'active', ...transitionOverrides })
  return {
    instance_id: INSTANCE,
    serving_https: true,
    ready: true,
    certificate_error: null,
    mode: 'auto',
    install_mode: 'kubernetes',
    management: 'external',
    restart_supported: true,
    transition: target,
    ca_sha256: target.ca_sha256,
    certificate_kind: target.certificate_kind,
    certificate_sha256: target.certificate_sha256,
    same_public_port: target.same_public_port,
    public_port: target.public_port,
    https_url: 'https://localhost:1515',
    activation_error: null,
    ...overrides,
  }
}

/** Store a live handoff ticket the way primeTlsHandoff does. */
export function seedTicket(transitionId, ticket = TICKET, validForSeconds = 600) {
  globalThis.sessionStorage.setItem(`cremind:https-ticket:${transitionId}`, JSON.stringify({
    ticket, expires_at: Date.now() / 1000 + validForSeconds,
  }))
}
