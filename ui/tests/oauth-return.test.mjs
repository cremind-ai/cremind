// Unit tests for the OAuth consent helpers (ui/src/services/oauthReturn.ts):
// recognising a Google consent link, opening it in a window that can close
// itself, and receiving the notice the closing page posts (app/api/oauth_close.py).
// Plain node:test + esbuild, the same pattern as ui/electron/https.test.mjs:
// bundle the TypeScript, import it from a data: URL, and drive it with fake
// window / BroadcastChannel objects so no browser is needed.
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

const transitionState = await (async () => {
  const source = await readFile(path.join(ui, 'electron', 'transitionState.ts'), 'utf8')
  const { code } = await transform(source, { loader: 'ts', format: 'esm' })
  return importCode(code)
})()

const oauth = await (async () => {
  const result = await build({
    entryPoints: [path.join(ui, 'src', 'services', 'oauthReturn.ts')],
    bundle: true, format: 'esm', platform: 'neutral', write: false, logLevel: 'silent',
  })
  return importCode(result.outputFiles[0].text)
})()

// ── fakes ────────────────────────────────────────────────────────────────────

function fakeWindow(origin = 'https://localhost:1515', opener = null, opened = () => ({})) {
  const listeners = new Map()
  return {
    location: { origin, href: `${origin}/#/`, protocol: new URL(origin).protocol },
    opener,
    opens: [],
    open(url, name) {
      this.opens.push({ url, name })
      return opened()
    },
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
  interference.length = 0
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

// ── consent window opener ────────────────────────────────────────────────────

function openerHarness({ opened = () => ({ id: 'popup' }), electron = false } = {}) {
  const win = fakeWindow('https://localhost:1515', null, opened)
  if (electron) win.cremind = { openExternal: () => {} }
  withGlobals({ window: win, BroadcastChannel: FakeChannel })
  return { win, uninstall: oauth.installConsentWindowOpener() }
}

test('a Google consent link opens in a window this page opened, so it can close itself', () => {
  const { win, uninstall } = openerHarness()
  assert.equal(win.listenerCount('click'), 1)
  const href = consentUrl({ redirect: 'http://localhost:1515/api/oauth/google-drive/callback' })
  win.dispatch('click', clickEvent(anchor(href)))
  assert.deepEqual(win.opens, [{ url: href, name: 'cremind-oauth-consent' }])
  // The anchor's own navigation is cancelled: without that the consent would
  // ALSO take this tab (the renderer's target="_blank" is applied to a live
  // DOM, not to these fakes) — and only the scripted window can be closed.
  assert.deepEqual(interference, ['preventDefault'])

  uninstall()
  assert.equal(win.listenerCount('click'), 0)
  win.dispatch('click', clickEvent(anchor(href)))
  assert.equal(win.opens.length, 1)
})

test('a blocked popup leaves the link alone rather than swallowing the click', () => {
  const { win, uninstall } = openerHarness({ opened: () => null })
  win.dispatch('click', clickEvent(anchor(consentUrl())))
  assert.equal(win.opens.length, 1)
  assert.deepEqual(interference, [], 'the anchor must still open the consent itself')
  uninstall()
})

test('only a Cremind loopback Google consent is opened this way', () => {
  const { win, uninstall } = openerHarness()
  const untouched = [
    anchor('https://example.com/docs'),
    anchor(consentUrl({ redirect: 'https://cremind.example/api/oauth/callback' })),
    { tagName: 'BUTTON' },
    { tagName: 'a', href: { baseVal: consentUrl() } },
  ]
  for (const target of untouched) win.dispatch('click', clickEvent(target))
  // Modified and non-primary clicks belong to the browser ("open in new
  // window", "copy link", middle-click).
  const consent = anchor(consentUrl())
  win.dispatch('click', clickEvent(consent, { button: 1 }))
  for (const key of ['metaKey', 'ctrlKey', 'shiftKey', 'altKey']) {
    win.dispatch('click', { ...clickEvent(consent), [key]: true })
  }
  win.dispatch('click', { ...clickEvent(consent), defaultPrevented: true })
  assert.deepEqual(win.opens, [])
  assert.deepEqual(interference, [])
  uninstall()
})

test('under Electron the consent is left to the OS browser', () => {
  // ui/src/utils/externalLinks.ts hands it to shell.openExternal, and no page
  // of ours could close a window in another browser anyway.
  const { win, uninstall } = openerHarness({ electron: true })
  win.dispatch('click', clickEvent(anchor(consentUrl())))
  assert.deepEqual(win.opens, [])
  assert.deepEqual(interference, [])
  uninstall()
})

test('a window.open that throws never breaks the link', () => {
  const win = fakeWindow('https://localhost:1515', null, () => { throw new Error('blocked') })
  withGlobals({ window: win, BroadcastChannel: FakeChannel })
  const uninstall = oauth.installConsentWindowOpener()
  assert.doesNotThrow(() => win.dispatch('click', clickEvent(anchor(consentUrl()))))
  assert.deepEqual(interference, [])
  uninstall()
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

// What the server's closing page does (app/api/oauth_close.py): post the notice
// to the opener and on the channel, then close. Written out here rather than
// imported, so these tests pin the wire contract the page has to keep.
function emitNotice({ flow, outcome, profile = null }) {
  const payload = { type: 'cremind:oauth-return', flow, outcome, profile }
  try {
    const opener = globalThis.window.opener
    if (opener && opener !== globalThis.window) opener.postMessage(payload, '*')
  } catch { /* opener closed or inaccessible */ }
  try {
    const channel = new globalThis.BroadcastChannel('cremind:oauth-return')
    channel.postMessage(payload)
    channel.close()
  } catch { /* BroadcastChannel unavailable */ }
}

test('a return notice reaches the opener across origins, carrying only type/flow/outcome/profile', () => {
  const { openerWin, popupWin, popupProxy, posted } = pagePair({ popupOrigin: 'https://127.0.0.1:1515' })
  withGlobals({ window: openerWin, BroadcastChannel: FakeChannel })
  const seen = []
  const unsubscribe = oauth.onOAuthReturn(collector(seen), { popup: popupProxy })

  globalThis.window = popupWin
  emitNotice({
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
  emitNotice({ flow: 'drive', outcome: 'received', profile: 'alice' })
  assert.deepEqual(seen, [{ flow: 'drive', outcome: 'received', profile: 'alice', fromPopup: true }])
  // A different outcome is a different notice.
  emitNotice({ flow: 'drive', outcome: 'failed', profile: 'alice' })
  assert.equal(seen.length, 2)
  globalThis.window = openerWin
  unsubscribe()
  globalThis.window = popupWin
  emitNotice({ flow: 'drive', outcome: 'denied', profile: 'alice' })
  assert.equal(seen.length, 2)
})

test('a tab without an opener still reaches other same-origin tabs, and nothing throws without either', () => {
  const listener = fakeWindow()
  withGlobals({ window: listener, BroadcastChannel: FakeChannel })
  const seen = []
  const unsubscribe = oauth.onOAuthReturn(collector(seen))
  globalThis.window = fakeWindow() // the consent tab: opened from a chat link, no opener
  emitNotice({ flow: 'skill', outcome: 'received' })
  assert.deepEqual(seen, [{ flow: 'skill', outcome: 'received', profile: null, fromPopup: false }])
  globalThis.window = listener
  unsubscribe()

  globalThis.BroadcastChannel = undefined
  globalThis.window = fakeWindow('https://localhost:1515', {
    postMessage() { throw new Error('opener navigated away') },
  })
  assert.doesNotThrow(() => emitNotice({ flow: 'skill', outcome: 'denied' }))
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
  emitNotice({ flow: 'drive', outcome: 'denied', profile: 'bob' })
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

test('a receiver drops a notice whose profile it cannot read', () => {
  // Absent or null means the server could not attribute the response; a malformed one drops
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
