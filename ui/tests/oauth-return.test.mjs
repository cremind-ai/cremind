// Unit tests for the OAuth return helpers (ui/src/services/oauthReturn.ts)
// and the Electron transition's route exclusion. Plain node:test + esbuild, the
// same pattern as ui/electron/https.test.mjs: bundle the TypeScript, import it
// from a data: URL, and drive it with fake window / fetch / BroadcastChannel
// objects so no browser is needed.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { build, transform } from 'esbuild'

const here = path.dirname(fileURLToPath(import.meta.url))
const ui = path.join(here, '..')

async function importCode(code) {
  return import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
}

const oauth = await (async () => {
  const result = await build({
    entryPoints: [path.join(ui, 'src', 'services', 'oauthReturn.ts')],
    bundle: true, format: 'esm', platform: 'neutral', write: false, logLevel: 'silent',
  })
  return importCode(result.outputFiles[0].text)
})()

const transitionState = await (async () => {
  const source = await readFile(path.join(ui, 'electron', 'transitionState.ts'), 'utf8')
  const { code } = await transform(source, { loader: 'ts', format: 'esm' })
  return importCode(code)
})()

// ── fakes ────────────────────────────────────────────────────────────────────

function fakeWindow(origin = 'https://localhost:1515', opener = null) {
  const listeners = new Map()
  return {
    location: { origin, href: `${origin}/#/`, protocol: new URL(origin).protocol },
    opener,
    addEventListener(type, fn, capture) {
      if (!listeners.has(type)) listeners.set(type, [])
      listeners.get(type).push({ fn, capture: !!capture })
    },
    removeEventListener(type, fn, capture) {
      const list = listeners.get(type) || []
      const index = list.findIndex(entry => entry.fn === fn && entry.capture === !!capture)
      if (index >= 0) list.splice(index, 1)
    },
    dispatch(type, event) {
      for (const { fn } of [...(listeners.get(type) || [])]) fn(event)
    },
    listenerCount(type) {
      return (listeners.get(type) || []).length
    },
  }
}

// Synchronous, same-process stand-in for BroadcastChannel: delivers to every
// other open channel of the same name, never back to the sender.
class FakeChannel {
  static open = []
  static posted = []
  constructor(name) {
    this.name = name
    this.closed = false
    this.onmessage = null
    FakeChannel.open.push(this)
  }
  postMessage(data) {
    FakeChannel.posted.push({ name: this.name, data })
    for (const other of FakeChannel.open) {
      if (other !== this && !other.closed && other.name === this.name && other.onmessage) {
        other.onmessage({ data })
      }
    }
  }
  close() {
    this.closed = true
  }
  static reset() {
    FakeChannel.open = []
    FakeChannel.posted = []
  }
}

function fakeFetch(responder = () => new Response('{}', { status: 200 })) {
  const calls = []
  const fn = async (url, init = {}) => {
    calls.push({ url: String(url), init })
    return responder(String(url), init)
  }
  fn.calls = calls
  return fn
}

const saved = {
  window: globalThis.window,
  fetch: globalThis.fetch,
  BroadcastChannel: globalThis.BroadcastChannel,
}
function withGlobals({ window, fetch, BroadcastChannel } = {}) {
  globalThis.window = window
  if (fetch) globalThis.fetch = fetch
  globalThis.BroadcastChannel = BroadcastChannel
}
test.afterEach(() => {
  if (saved.window === undefined) delete globalThis.window
  else globalThis.window = saved.window
  globalThis.fetch = saved.fetch
  globalThis.BroadcastChannel = saved.BroadcastChannel
  FakeChannel.reset()
})

const STATE = 'Abc_123-xyzABC_123-xyzABC_123'
const REF = 'R'.repeat(43)

function consentUrl({
  scheme = 'https', host = 'accounts.google.com', path: urlPath = '/o/oauth2/v2/auth',
  state = STATE, redirect = 'http://localhost:1515/api/oauth/callback',
} = {}) {
  const url = new URL(`${scheme}://${host}${urlPath}`)
  url.searchParams.set('response_type', 'code')
  url.searchParams.set('client_id', 'cremind.apps.googleusercontent.com')
  if (redirect !== null) url.searchParams.set('redirect_uri', redirect)
  if (state !== null) url.searchParams.set('state', state)
  url.searchParams.set('code_challenge_method', 'S256')
  return url.href
}

function anchor(href) {
  return { tagName: 'A', href }
}

// Every event interference is counted rather than asserted inline: the recorder
// swallows its own exceptions, so a throwing stub would go unnoticed.
const interference = []
function clickEvent(target, { type = 'click', button = 0 } = {}) {
  return {
    type,
    button,
    composedPath: () => [{ tagName: 'SPAN' }, target, { tagName: 'DIV' }],
    preventDefault: () => interference.push('preventDefault'),
    stopPropagation: () => interference.push('stopPropagation'),
    stopImmediatePropagation: () => interference.push('stopImmediatePropagation'),
  }
}

// ── parseGoogleConsentUrl ─────────────────────────────────────────────────────

test('consent URLs for the three loopback callbacks are recognised with their flow', () => {
  const parse = oauth.parseGoogleConsentUrl
  assert.deepEqual(parse(consentUrl()), { state: STATE, flow: 'skill' })
  assert.deepEqual(
    parse(consentUrl({ redirect: 'http://127.0.0.1:1515/api/oauth/google-calendar/callback' })),
    { state: STATE, flow: 'calendar' },
  )
  assert.deepEqual(
    parse(consentUrl({ redirect: 'http://[::1]:1515/api/oauth/google-drive/callback' })),
    { state: STATE, flow: 'drive' },
  )
  // An https loopback redirect (a pinned CREMIND_OAUTH_REDIRECT_URI) still correlates.
  assert.deepEqual(
    parse(consentUrl({ redirect: 'https://localhost:9443/api/oauth/callback' })),
    { state: STATE, flow: 'skill' },
  )
  // The whole 127.0.0.0/8 block is loopback; so is a portless host.
  assert.equal(parse(consentUrl({ redirect: 'http://127.8.9.10/api/oauth/callback' }))?.flow, 'skill')
  assert.equal(parse(consentUrl({ redirect: 'http://localhost/api/oauth/callback' }))?.flow, 'skill')
  // oauthlib's InstalledAppFlow uses the v1 endpoint.
  assert.equal(parse(consentUrl({ path: '/o/oauth2/auth' }))?.flow, 'skill')
  assert.equal(parse(consentUrl({ state: 'a'.repeat(8) }))?.state, 'a'.repeat(8))
  assert.equal(parse(consentUrl({ state: 'a'.repeat(128) }))?.state, 'a'.repeat(128))
})

test('anything that is not a Cremind loopback Google consent is ignored', () => {
  const parse = oauth.parseGoogleConsentUrl
  const rejected = {
    'public redirect': consentUrl({ redirect: 'https://cremind.example.com/api/oauth/callback' }),
    'lookalike loopback host': consentUrl({ redirect: 'http://localhost.evil.example/api/oauth/callback' }),
    'private LAN redirect': consentUrl({ redirect: 'http://10.0.0.5:1515/api/oauth/callback' }),
    'credentials in redirect': consentUrl({ redirect: 'http://user:pw@localhost:1515/api/oauth/callback' }),
    'non-http redirect': consentUrl({ redirect: 'file:///api/oauth/callback' }),
    'unparseable redirect': consentUrl({ redirect: 'not a url' }),
    'missing redirect': consentUrl({ redirect: null }),
    'other callback path': consentUrl({ redirect: 'http://localhost:1515/api/oauth/a2a/callback' }),
    'callback path with a suffix': consentUrl({ redirect: 'http://localhost:1515/api/oauth/callback/x' }),
    'prototype key as path': consentUrl({ redirect: 'http://localhost:1515/__proto__' }),
    'wrong host': consentUrl({ host: 'accounts.google.com.evil.example' }),
    'other Google host': consentUrl({ host: 'oauth2.googleapis.com' }),
    'plain http Google': consentUrl({ scheme: 'http' }),
    'explicit port': consentUrl({ host: 'accounts.google.com:444' }),
    'not the OAuth path': consentUrl({ path: '/signin/v2/identifier' }),
    'missing state': consentUrl({ state: null }),
    'short state': consentUrl({ state: 'abc1234' }),
    'long state': consentUrl({ state: 'a'.repeat(129) }),
    'state with bad characters': consentUrl({ state: 'bad state/../..' }),
    'relative href': '/o/oauth2/v2/auth?state=' + STATE,
    'garbage': 'javascript:alert(1)',
  }
  for (const [label, href] of Object.entries(rejected)) {
    assert.equal(parse(href), null, label)
  }
})

// ── consent recorder ──────────────────────────────────────────────────────────

function recorderHarness(route = { name: 'conversation', params: { profile: 'alice' }, fullPath: '/alice/c/42' }) {
  const win = fakeWindow()
  const fetch = fakeFetch()
  withGlobals({ window: win, fetch, BroadcastChannel: FakeChannel })
  const router = { currentRoute: { value: route } }
  const store = {
    agentUrl: 'https://localhost:1515',
    getTokenForProfile: profile => (profile === 'alice' ? 'alice-token' : ''),
  }
  const uninstall = oauth.installOAuthConsentRecorder({
    router,
    getSession: () => oauth.consentSessionFor(router.currentRoute.value, store),
  })
  return { win, fetch, router, uninstall }
}

test('a consent-link click on a profile page records {state, route, flow} with keepalive and the profile token', () => {
  const { win, fetch, uninstall } = recorderHarness()
  assert.equal(win.listenerCount('click'), 1)
  assert.equal(win.listenerCount('auxclick'), 1)
  win.dispatch('click', clickEvent(anchor(consentUrl({
    redirect: 'http://localhost:1515/api/oauth/google-drive/callback',
  }))))
  assert.equal(fetch.calls.length, 1)
  const [{ url, init }] = fetch.calls
  assert.equal(url, 'https://localhost:1515/api/oauth/return/context')
  assert.equal(init.method, 'POST')
  assert.equal(init.keepalive, true)
  assert.equal(init.headers.Authorization, 'Bearer alice-token')
  assert.equal(init.headers['Content-Type'], 'application/json')
  assert.deepEqual(JSON.parse(init.body), { state: STATE, route: '/alice/c/42', flow: 'drive' })

  // Middle-click opens links too; other auxiliary buttons do not.
  win.dispatch('auxclick', clickEvent(anchor(consentUrl()), { type: 'auxclick', button: 1 }))
  win.dispatch('auxclick', clickEvent(anchor(consentUrl()), { type: 'auxclick', button: 2 }))
  assert.equal(fetch.calls.length, 2)
  assert.equal(JSON.parse(fetch.calls[1].init.body).flow, 'skill')

  uninstall()
  assert.equal(win.listenerCount('click'), 0)
  assert.equal(win.listenerCount('auxclick'), 0)
  win.dispatch('click', clickEvent(anchor(consentUrl())))
  assert.equal(fetch.calls.length, 2)
  assert.deepEqual(interference, [])
})

test('the recorder stays silent off profile pages, without a token, and for other links', () => {
  const { win, fetch, router, uninstall } = recorderHarness()
  // Other links, a non-anchor click and an SVG anchor (non-string href).
  win.dispatch('click', clickEvent(anchor('https://example.com/docs')))
  win.dispatch('click', clickEvent(anchor(consentUrl({ redirect: 'https://cremind.example/api/oauth/callback' }))))
  win.dispatch('click', clickEvent({ tagName: 'BUTTON' }))
  win.dispatch('click', clickEvent({ tagName: 'a', href: { baseVal: consentUrl() } }))
  // Public routes are not profile routes, even when their path names one.
  for (const value of [
    { name: 'home', params: {}, fullPath: '/' },
    { name: 'login', params: { profile: 'alice' }, fullPath: '/login/alice' },
    { name: 'oauth-return', params: {}, fullPath: '/oauth-return' },
    { name: 'chat', params: { profile: 'bob' }, fullPath: '/bob' }, // no token for bob
  ]) {
    router.currentRoute.value = value
    win.dispatch('click', clickEvent(anchor(consentUrl())))
  }
  assert.equal(fetch.calls.length, 0)
  assert.deepEqual(interference, [])
  uninstall()
})

test('a failing session lookup or request never breaks the click', async () => {
  const win = fakeWindow()
  withGlobals({ window: win, fetch: fakeFetch(() => { throw new TypeError('offline') }) })
  const uninstall = oauth.installOAuthConsentRecorder({
    router: { currentRoute: { value: { fullPath: '/alice' } } },
    getSession: () => { throw new Error('store not ready') },
  })
  assert.doesNotThrow(() => win.dispatch('click', clickEvent(anchor(consentUrl()))))
  assert.deepEqual(interference, [])
  uninstall()
  assert.equal(
    await oauth.recordOAuthReturnContext('https://localhost:1515', 'tok', { state: STATE, route: '/alice', flow: 'skill' }),
    false,
  )
  globalThis.fetch = fakeFetch(() => new Response('{"error":"conflict"}', { status: 409 }))
  assert.equal(
    await oauth.recordOAuthReturnContext('https://localhost:1515', 'tok', { state: STATE, route: '/alice', flow: 'skill' }),
    false,
  )
})

// ── notify / onOAuthReturn ────────────────────────────────────────────────────

// The initiating page (``opener``) and the return page (``popupWin``), each with
// its own window object. ``window`` is a single global, so every callback runs
// with the global pointed at the window it belongs to.
function pagePair({ openerOrigin = 'https://localhost:1515', popupOrigin = 'https://localhost:1515' } = {}) {
  const openerWin = fakeWindow(openerOrigin)
  const popupProxy = {} // what window.open returned to the opener
  const posted = []
  const popupWin = fakeWindow(popupOrigin, {
    postMessage(data, target) {
      posted.push({ data, target })
      const previous = globalThis.window
      globalThis.window = openerWin
      try {
        openerWin.dispatch('message', { data, origin: popupOrigin, source: popupProxy })
      } finally {
        globalThis.window = previous
      }
    },
  })
  return { openerWin, popupWin, popupProxy, posted }
}

// Handler that records each notice together with how it was delivered.
function collector(seen) {
  return (notice, delivery) => seen.push({ ...notice, ...delivery })
}

test('a return notice reaches the opener across origins, carrying only type/flow/outcome/profile', () => {
  const { openerWin, popupWin, popupProxy, posted } = pagePair({ popupOrigin: 'https://127.0.0.1:1515' })
  withGlobals({ window: openerWin, BroadcastChannel: FakeChannel })
  const seen = []
  const unsubscribe = oauth.onOAuthReturn(collector(seen), { popup: popupProxy })

  globalThis.window = popupWin
  oauth.notifyOAuthReturn({
    flow: 'calendar', outcome: 'received', profile: 'alice', code: 'leak', state: STATE, ref: REF,
  })
  assert.equal(posted.length, 1)
  assert.equal(posted[0].target, '*')
  assert.deepEqual(posted[0].data, {
    type: 'cremind:oauth-return', flow: 'calendar', outcome: 'received', profile: 'alice',
  })
  assert.deepEqual(FakeChannel.posted.map(entry => entry.data), [posted[0].data])
  assert.equal(FakeChannel.posted[0].name, 'cremind:oauth-return')
  assert.deepEqual(seen, [{ flow: 'calendar', outcome: 'received', profile: 'alice', fromPopup: true }])

  globalThis.window = openerWin
  unsubscribe()
  assert.equal(openerWin.listenerCount('message'), 0)
  assert.ok(FakeChannel.open.every(channel => channel.closed))
})

test('window messages are accepted only from this origin or the popup this page opened', () => {
  const openerWin = fakeWindow('https://localhost:1515')
  withGlobals({ window: openerWin, BroadcastChannel: undefined })
  const popup = {}
  const seen = []
  const unsubscribe = oauth.onOAuthReturn(collector(seen), { popup })
  const data = { type: 'cremind:oauth-return', flow: 'drive', outcome: 'denied' }

  openerWin.dispatch('message', { data, origin: 'https://evil.example', source: {} })
  openerWin.dispatch('message', { data: { ...data, type: 'other' }, origin: 'https://localhost:1515', source: {} })
  openerWin.dispatch('message', { data: { ...data, flow: 'gmail' }, origin: 'https://localhost:1515', source: {} })
  openerWin.dispatch('message', { data: { ...data, outcome: 'linked' }, origin: 'https://localhost:1515', source: {} })
  openerWin.dispatch('message', { data: 'cremind:oauth-return', origin: 'https://localhost:1515', source: {} })
  assert.deepEqual(seen, [])

  // Same origin (another tab of this app) and the popup on any origin are fine —
  // but only the popup's own message counts as coming from the popup.
  openerWin.dispatch('message', { data, origin: 'https://localhost:1515', source: {} })
  openerWin.dispatch('message', { data: { ...data, outcome: 'invalid' }, origin: 'http://127.0.0.1:1515', source: popup })
  assert.deepEqual(seen, [
    { flow: 'drive', outcome: 'denied', profile: null, fromPopup: false },
    { flow: 'drive', outcome: 'invalid', profile: null, fromPopup: true },
  ])
  unsubscribe()
  openerWin.dispatch('message', { data: { ...data, outcome: 'failed' }, origin: 'https://localhost:1515', source: {} })
  assert.equal(seen.length, 2)

  // Without a popup reference only this origin is trusted, and nothing is "from the popup".
  const onlySameOrigin = []
  const second = oauth.onOAuthReturn(collector(onlySameOrigin))
  openerWin.dispatch('message', { data: { ...data, flow: 'skill' }, origin: 'http://127.0.0.1:1515', source: popup })
  assert.deepEqual(onlySameOrigin, [])
  openerWin.dispatch('message', { data: { ...data, flow: 'skill' }, origin: 'https://localhost:1515', source: popup })
  assert.deepEqual(onlySameOrigin, [{ flow: 'skill', outcome: 'denied', profile: null, fromPopup: false }])
  second()
  assert.equal(openerWin.listenerCount('message'), 0)
})

test('a same-origin popup notice arriving by postMessage and BroadcastChannel is handled once', () => {
  const { openerWin, popupWin, popupProxy } = pagePair()
  withGlobals({ window: openerWin, BroadcastChannel: FakeChannel })
  const seen = []
  const unsubscribe = oauth.onOAuthReturn(collector(seen), { popup: popupProxy })
  globalThis.window = popupWin
  oauth.notifyOAuthReturn({ flow: 'drive', outcome: 'received', profile: 'alice' })
  assert.deepEqual(seen, [{ flow: 'drive', outcome: 'received', profile: 'alice', fromPopup: true }])
  // A different outcome is a different notice.
  oauth.notifyOAuthReturn({ flow: 'drive', outcome: 'failed', profile: 'alice' })
  assert.equal(seen.length, 2)
  globalThis.window = openerWin
  unsubscribe()
  globalThis.window = popupWin
  oauth.notifyOAuthReturn({ flow: 'drive', outcome: 'denied', profile: 'alice' })
  assert.equal(seen.length, 2)
})

test('a tab without an opener still reaches other same-origin tabs, and nothing throws without either', () => {
  const listener = fakeWindow()
  withGlobals({ window: listener, BroadcastChannel: FakeChannel })
  const seen = []
  const unsubscribe = oauth.onOAuthReturn(collector(seen))
  globalThis.window = fakeWindow() // the consent tab: opened from a chat link, no opener
  oauth.notifyOAuthReturn({ flow: 'skill', outcome: 'received' })
  assert.deepEqual(seen, [{ flow: 'skill', outcome: 'received', profile: null, fromPopup: false }])
  globalThis.window = listener
  unsubscribe()

  globalThis.BroadcastChannel = undefined
  globalThis.window = fakeWindow('https://localhost:1515', {
    postMessage() { throw new Error('opener navigated away') },
  })
  assert.doesNotThrow(() => oauth.notifyOAuthReturn({ flow: 'skill', outcome: 'denied' }))
})

test("another profile's return reaches a waiting page only as a hint, never as its popup's verdict", () => {
  // Tab 1: alice's Drive section, waiting on the popup it opened. Tab 2: bob
  // cancels his own picker; his return page reaches tab 1 by the channel alone.
  const aliceTab = fakeWindow()
  withGlobals({ window: aliceTab, BroadcastChannel: FakeChannel })
  const alicePopup = {}
  const seen = []
  const unsubscribe = oauth.onOAuthReturn(collector(seen), { popup: alicePopup })
  globalThis.window = fakeWindow() // bob's return page
  oauth.notifyOAuthReturn({ flow: 'drive', outcome: 'denied', profile: 'bob' })
  assert.deepEqual(seen, [{ flow: 'drive', outcome: 'denied', profile: 'bob', fromPopup: false }])
  globalThis.window = aliceTab
  unsubscribe()
})

test("a popup's own copy is not swallowed when its channel copy lands first", () => {
  const openerWin = fakeWindow()
  withGlobals({ window: openerWin, BroadcastChannel: FakeChannel })
  const popup = {}
  const seen = []
  const unsubscribe = oauth.onOAuthReturn(collector(seen), { popup })
  const data = { type: 'cremind:oauth-return', flow: 'calendar', outcome: 'denied', profile: 'alice' }
  const viaChannel = payload => new FakeChannel('cremind:oauth-return').postMessage(payload)
  const viaWindow = (payload, source) => openerWin.dispatch('message', {
    data: payload, origin: 'https://localhost:1515', source,
  })

  viaChannel(data) // the hint arrives first...
  viaWindow(data, {}) // ...an ordinary same-origin copy is still just a duplicate...
  viaWindow(data, popup) // ...but the popup's own copy gets through: only it may settle a wait.
  viaChannel(data) // After that, every copy is a duplicate, whichever way it comes.
  viaWindow(data, popup)
  assert.deepEqual(seen, [
    { flow: 'calendar', outcome: 'denied', profile: 'alice', fromPopup: false },
    { flow: 'calendar', outcome: 'denied', profile: 'alice', fromPopup: true },
  ])
  // Another profile's notice is a different notice, not a duplicate of alice's.
  viaChannel({ ...data, profile: 'bob' })
  assert.deepEqual(seen[2], { flow: 'calendar', outcome: 'denied', profile: 'bob', fromPopup: false })
  assert.equal(seen.length, 3)
  unsubscribe()
})

test('notices carry a validated profile, and a receiver drops one it cannot read', () => {
  // Sender: only a profile the server could have recorded is ever broadcast.
  withGlobals({ window: fakeWindow(), BroadcastChannel: FakeChannel })
  const cases = [
    ['alice', 'alice'], ['a'.repeat(64), 'a'.repeat(64)], ['team_b-2', 'team_b-2'],
    ['Alice', null], ['a b', null], ['../x', null], ['a'.repeat(65), null], ['', null],
    [42, null], [undefined, null], [null, null],
  ]
  for (const [profile] of cases) oauth.notifyOAuthReturn({ flow: 'skill', outcome: 'received', profile })
  assert.deepEqual(FakeChannel.posted.map(entry => entry.data.profile), cases.map(([, expected]) => expected))

  // Receiver: absent or null means nobody recorded one; a malformed one drops
  // the notice rather than letting it pass as "could be anyone's".
  const listener = fakeWindow()
  withGlobals({ window: listener, BroadcastChannel: undefined })
  const seen = []
  const unsubscribe = oauth.onOAuthReturn(collector(seen))
  const send = extra => listener.dispatch('message', {
    data: { type: 'cremind:oauth-return', flow: 'calendar', ...extra },
    origin: 'https://localhost:1515',
    source: {},
  })
  for (const profile of ['Bob', '../bob', 'a'.repeat(65), '', 7, ['bob'], { name: 'bob' }]) {
    send({ outcome: 'denied', profile })
  }
  assert.deepEqual(seen, [])
  send({ outcome: 'received' })
  send({ outcome: 'failed', profile: null })
  send({ outcome: 'denied', profile: 'bob' })
  assert.deepEqual(seen.map(({ outcome, profile }) => [outcome, profile]), [
    ['received', null], ['failed', null], ['denied', 'bob'],
  ])
  unsubscribe()
})

// ── consume ───────────────────────────────────────────────────────────────────

test('consume redeems a ref without credentials and maps unknown refs to null', async () => {
  withGlobals({ window: fakeWindow() })
  globalThis.fetch = fakeFetch(() => new Response(JSON.stringify({
    outcome: 'received', flow: 'calendar', profile: 'alice', route: '/alice/calendar', message: null,
  }), { status: 200 }))
  assert.deepEqual(await oauth.consumeOAuthReturn('https://localhost:1515', REF), {
    outcome: 'received', flow: 'calendar', profile: 'alice', route: '/alice/calendar', message: null,
  })
  const [{ url, init }] = globalThis.fetch.calls
  assert.equal(url, 'https://localhost:1515/api/oauth/return/consume')
  assert.equal(init.method, 'POST')
  assert.equal(init.headers.Authorization, undefined)
  assert.deepEqual(JSON.parse(init.body), { ref: REF })

  // Context-less refs (a consent nobody recorded) come back with null profile/route.
  globalThis.fetch = fakeFetch(() => new Response(JSON.stringify({
    outcome: 'denied', flow: 'skill', profile: null, route: null, message: 'access_denied',
  }), { status: 200 }))
  assert.deepEqual(await oauth.consumeOAuthReturn('', REF), {
    outcome: 'denied', flow: 'skill', profile: null, route: null, message: 'access_denied',
  })
  assert.equal(globalThis.fetch.calls[0].url, 'https://localhost:1515/api/oauth/return/consume')

  globalThis.fetch = fakeFetch(() => new Response('{"error":"unknown"}', { status: 404 }))
  assert.equal(await oauth.consumeOAuthReturn('https://localhost:1515', REF), null)
  globalThis.fetch = fakeFetch(() => new Response('{"error":"bad"}', { status: 400 }))
  assert.equal(await oauth.consumeOAuthReturn('https://localhost:1515', REF), null)
  // An outcome this page cannot act on is not a result...
  globalThis.fetch = fakeFetch(() => new Response('{"outcome":"linked","flow":"skill"}', { status: 200 }))
  assert.equal(await oauth.consumeOAuthReturn('https://localhost:1515', REF), null)
  // ...but a spent ref with a real outcome and an unrecognised flow still is.
  globalThis.fetch = fakeFetch(() => new Response('{"outcome":"failed","flow":null}', { status: 200 }))
  assert.deepEqual(await oauth.consumeOAuthReturn('https://localhost:1515', REF), {
    outcome: 'failed', flow: null, profile: null, route: null, message: null,
  })
  // A malformed ref is never sent.
  globalThis.fetch = fakeFetch()
  assert.equal(await oauth.consumeOAuthReturn('https://localhost:1515', 'short'), null)
  assert.equal(await oauth.consumeOAuthReturn('https://localhost:1515', `${'a'.repeat(40)}/../`), null)
  assert.equal(globalThis.fetch.calls.length, 0)
  // A server that could not be asked is an error (the page offers a retry),
  // not "expired".
  globalThis.fetch = fakeFetch(() => new Response('', { status: 503 }))
  await assert.rejects(() => oauth.consumeOAuthReturn('https://localhost:1515', REF))
  globalThis.fetch = fakeFetch(() => { throw new TypeError('network down') })
  await assert.rejects(() => oauth.consumeOAuthReturn('https://localhost:1515', REF))
})

// ── return page wording ───────────────────────────────────────────────────────

test("the return page names Google only when the result proves the consent was Google's", () => {
  const home = { label: 'Cremind home', restores: false }
  const copyFor = (state, result, extra = {}) => oauth.returnCopy({ state, result, destination: home, ...extra })
  const text = copy => [copy.title, ...copy.lines].join('\n')
  const result = over => ({
    outcome: 'denied', flow: 'skill', profile: null, route: null,
    message: 'Authorization was not granted (access_denied).', ...over,
  })

  // /api/oauth/callback also serves the Atlassian skills, so a skill return
  // nobody recorded may well be Jira's: it must not claim Google answered.
  const unrecorded = result()
  assert.equal(oauth.isGoogleReturn(unrecorded), false)
  const denied = copyFor('denied', unrecorded)
  assert.equal(denied.title, 'Sign-in was not completed')
  assert.deepEqual(denied.lines, [
    'The provider reported that access was not granted, so nothing was linked.',
    'The provider said: Authorization was not granted (access_denied).',
    'Ask the agent to link the account again to retry.',
  ])
  for (const outcome of ['received', 'denied', 'failed', 'invalid']) {
    assert.doesNotMatch(text(copyFor(outcome, { ...unrecorded, outcome })), /Google/, outcome)
  }
  assert.equal(copyFor('received', { ...unrecorded, outcome: 'received' }).title, 'The sign-in response reached Cremind')
  assert.equal(
    copyFor('failed', { ...unrecorded, outcome: 'failed' }).lines[0],
    'The sign-in response arrived, but Cremind could not process it.',
  )
  // An unrecognised flow proves nothing either, even with a profile.
  assert.equal(oauth.isGoogleReturn(result({ flow: null, profile: 'alice' })), false)
  assert.doesNotMatch(text(copyFor('denied', result({ flow: null, profile: 'alice' }))), /Google/)
  // States without a result are always neutral.
  assert.equal(copyFor('working', null).title, 'Finishing sign-in…')
  for (const state of ['working', 'unreachable', 'unmatched']) {
    assert.doesNotMatch(text(copyFor(state, null)), /Google/, state)
  }
  assert.equal(
    copyFor('unmatched', null, { invalidStateCallback: true }).lines[0],
    'The response did not carry a request Cremind can identify.',
  )
  assert.match(copyFor('unreachable', null).lines[0], /^The sign-in response arrived, but this page could not reach/)

  // Calendar and Drive have Google-only callbacks...
  assert.equal(oauth.isGoogleReturn(result({ flow: 'calendar' })), true)
  const calendar = copyFor('denied', result({ flow: 'calendar' }))
  assert.equal(calendar.title, 'Google sign-in was not completed')
  assert.deepEqual(calendar.lines, [
    'Google reported that access was not granted, so nothing was linked.',
    'Google said: Authorization was not granted (access_denied).',
    'Use Connect Google on the Calendar page to retry.',
  ])
  assert.equal(copyFor('received', result({ flow: 'drive', outcome: 'received' })).title, 'Google’s response reached Cremind')
  assert.match(copyFor('invalid', result({ flow: 'drive', outcome: 'invalid' })).lines[0], /the request Google answered/)
  // ...and a recorded skill consent is a Google one: the recorder records nothing else.
  assert.equal(oauth.isGoogleReturn(result({ profile: 'alice' })), true)
  assert.equal(copyFor('denied', result({ profile: 'alice' })).title, 'Google sign-in was not completed')

  // A restorable page is named on the way back.
  const back = oauth.returnCopy({
    state: 'received', result: result({ outcome: 'received', profile: 'alice' }),
    destination: { label: 'your chat', restores: true },
  })
  assert.equal(back.lines[0], 'Returning you to your chat…')
})

// ── route guard stash ─────────────────────────────────────────────────────────

test('the return route lifts its query exactly once', () => {
  assert.equal(oauth.stashOAuthReturnQuery({}), false)
  assert.equal(oauth.takeOAuthReturnQuery(), null)
  assert.equal(oauth.stashOAuthReturnQuery({ ref: REF }), true)
  assert.deepEqual(oauth.takeOAuthReturnQuery(), { ref: REF, error: null })
  assert.equal(oauth.takeOAuthReturnQuery(), null)
  assert.equal(oauth.stashOAuthReturnQuery({ error: 'invalid_state' }), true)
  assert.deepEqual(oauth.takeOAuthReturnQuery(), { ref: null, error: 'invalid_state' })
  // Junk is still stripped from the URL, but nothing usable is kept.
  assert.equal(oauth.stashOAuthReturnQuery({ ref: 'not a ref', code: 'x', error: '<b>' }), true)
  assert.deepEqual(oauth.takeOAuthReturnQuery(), { ref: null, error: null })
  assert.equal(oauth.stashOAuthReturnQuery({ ref: [REF, REF] }), true)
  assert.deepEqual(oauth.takeOAuthReturnQuery(), { ref: null, error: null })
})

// ── Electron HTTPS transition ─────────────────────────────────────────────────

test('the Electron HTTPS transition never treats the OAuth return route as a profile', () => {
  assert.equal(transitionState.transitionProfile('http://127.0.0.1:1515/#/oauth-return'), null)
  assert.equal(transitionState.transitionProfile('http://127.0.0.1:1515/#/oauth-return?ref=abc'), null)
  assert.equal(transitionState.transitionProfile('http://127.0.0.1:1515/#/oauth-return', 'alice'), null)
  assert.equal(transitionState.transitionProfile('http://127.0.0.1:1515/electron-renderer/#/alice/calendar'), 'alice')
  assert.deepEqual(
    transitionState.filterTransitionState('http://127.0.0.1:1515/#/oauth-return', {
      local: { profile_id: 'alice', agent_token_alice: 'alice-token', theme: 'dark' },
      session: { 'cremind:draft:alice:1': 'draft' },
    }),
    { local: { theme: 'dark' }, session: {} },
  )
})
