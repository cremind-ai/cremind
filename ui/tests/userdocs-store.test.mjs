// Which connection the userDocs store holds, and that nothing crosses profiles.
//
// The profile-events SSE is deliberately opened only on chat routes (each
// origin gets ~6 HTTP/1.1 connections). So User Document Search progress rides
// that stream's `userdocs` topic on chat routes, opens its own stream only
// while My Documents is mounted, and otherwise polls `/status` — and only while
// the feature is on. A token change (profile switch, logout) must drop the held
// snapshot at once, and an answer still in flight for the old token must not
// land afterwards: that would show one profile's folder to another.
import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, installBrowser, json, load, until } from './harness.mjs'

const AGENT = 'http://localhost:1515'

function snap(overrides = {}) {
  return { v: 1, boot: 'b', seq: 1, enabled: true, state: 'idle', reason: null, ...overrides }
}

async function setup() {
  const env = installBrowser()
  // The settings store reads its agent URL from the Electron bridge first;
  // giving it one keeps the bundle away from Vite's import.meta.env.
  globalThis.window.cremind = { config: { agentUrl: AGENT } }
  const mod = await load('tests/userdocs-store-entry.ts')
  mod.setActivePinia(mod.createPinia())
  const settings = mod.useSettingsStore()
  settings.authToken = 'tok-ann'
  const store = mod.useUserDocsStore()
  return { env, mod, settings, store }
}

const subscribers = () => globalThis.__userDocsSubscribers

test('chat routes subscribe to the profile-events topic, and its frames land', async () => {
  const { env, store } = await setup()
  store.connect(AGENT)
  store.setRoute('chat')
  assert.equal(store.transport, 'profile-events')
  assert.equal(subscribers().length, 1)
  assert.equal(env.callsTo('/api/userdocs/status').length, 0)

  subscribers()[0](snap({ seq: 2, state: 'indexing', stages: { dirty: 5, indexed: 5 } }))
  await flush()
  assert.equal(store.snapshot.state, 'indexing')
  assert.equal(store.isActive, true)
  assert.equal(store.progressPct, 50)

  // A late replay of an older frame changes nothing.
  subscribers()[0](snap({ seq: 1, state: 'idle' }))
  assert.equal(store.snapshot.state, 'indexing')
  store.disconnect()
  assert.equal(subscribers().length, 0)
})

test('other pages poll while the feature is on, and stop once it is off', async () => {
  const { env, store } = await setup()
  let answer = snap({ seq: 5, enabled: true, state: 'idle' })
  env.route('/api/userdocs/status', () => json(answer))
  store.connect(AGENT)
  store.setRoute('settings')
  assert.equal(store.transport, 'poll')
  await until(() => store.snapshot?.seq === 5, 'first poll')
  assert.equal(env.callsTo('/api/userdocs/status').at(-1).init.headers.Authorization, 'Bearer tok-ann')

  // The profile switched the feature off somewhere: the next answer says so
  // and polling stops rather than costing a request every 15 s forever.
  answer = snap({ seq: 6, enabled: false, state: 'disabled' })
  await store.refresh()
  await flush()
  assert.equal(store.transport, 'none')
  store.disconnect()
})

test('a page that is not profile-scoped opens nothing', async () => {
  const { env, store } = await setup()
  env.route('/api/userdocs/status', () => json(snap()))
  store.connect(AGENT)
  store.setRoute('home')
  assert.equal(store.transport, 'none')
  await flush()
  assert.equal(env.callsTo('/api/userdocs').length, 0)
  store.disconnect()
})

test('My Documents opens its own authenticated stream and parses its frames', async () => {
  const { env, store } = await setup()
  env.route('/api/userdocs/stream', (_url, init) => {
    assert.equal(init.headers.Authorization, 'Bearer tok-ann')
    const frame = snap({ seq: 3, state: 'scanning', phase: 'scanning (10 files seen)' })
    return new Response(
      `event: userdocs\ndata: ${JSON.stringify(frame)}\n\n: keepalive\n\nevent: ready\ndata: {}\n\n`,
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    )
  })
  store.connect(AGENT)
  // The route is reported before the page mounts: the stream opens right
  // away, with no stray poll in between.
  store.setRoute('user-documents-settings')
  assert.equal(store.transport, 'page-stream')
  const detach = store.attachPage()
  assert.equal(store.transport, 'page-stream')
  await until(() => store.snapshot?.state === 'scanning', 'stream frame')
  assert.equal(store.snapshot.phase, 'scanning (10 files seen)')
  assert.equal(env.callsTo('/api/userdocs/status').length, 0)

  // Leaving the page hands over to the route's own rule (a poll here).
  env.route('/api/userdocs/status', () => json(snap({ seq: 4 })))
  store.setRoute('settings')
  detach()
  assert.equal(store.transport, 'poll')
  store.disconnect()
})

test('a token change drops the held snapshot and ignores answers for the old token', async () => {
  const { env, settings, store } = await setup()
  const slow = deferred()
  env.route('/api/userdocs/status', () => slow.promise)
  store.connect(AGENT)
  store.setRoute('settings')
  store.applySnapshot(snap({ seq: 1, state: 'indexing' }))
  assert.equal(store.snapshot.state, 'indexing')

  // Switch profile while the first poll is still in flight.
  env.route('/api/userdocs/status', () => json(snap({ boot: 'x', seq: 1, state: 'idle', enabled: true })))
  settings.authToken = 'tok-bob'
  await flush()
  assert.notEqual(store.snapshot?.state, 'indexing')

  slow.resolve(json(snap({ seq: 99, state: 'hold', reason: 'root_unavailable' })))
  await flush(6)
  assert.notEqual(store.snapshot?.state, 'hold', "the previous profile's answer must not land")
  const auth = env.callsTo('/api/userdocs/status').map(c => c.init.headers.Authorization)
  assert.equal(auth.at(-1), 'Bearer tok-bob')
  store.disconnect()
})
