// Settings → My Documents exists only while Vector Embedding is on.
//
// Every way in follows one rule (src/utils/myDocumentsAccess.ts): the card on
// the Settings list, the NavRail sync chip, the route guard and the open page
// itself. The embedding store's `enabled` reads false until its first snapshot,
// and a fresh load runs the route guard before App.vue has even connected the
// stream — so the trap these tests pin is reading that default as "off": a
// reload of My Documents must never bounce to the Settings list on it.
import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, installBrowser, json, load } from './harness.mjs'

const AGENT = 'http://localhost:1515'
const STATUS = '/api/config/embedding/status'

function state(overrides = {}) {
  return { status: 'ready', phase: null, error: null, ready: true, busy: false, enabled: true, ...overrides }
}

async function setup({ token = 'tok-ann' } = {}) {
  const env = installBrowser()
  // The settings store reads its agent URL from the Electron bridge first;
  // giving it one keeps the bundle away from Vite's import.meta.env.
  globalThis.window.cremind = { config: { agentUrl: AGENT } }
  const mod = await load('tests/entries/embedding-status.ts')
  mod.setActivePinia(mod.createPinia())
  const settings = mod.useSettingsStore()
  settings.authToken = token
  const store = mod.useEmbeddingStatusStore()
  return { env, mod, settings, store }
}

const pushState = snap => { for (const cb of globalThis.__embeddingSubscribers) cb(snap) }

// ── the rules ───────────────────────────────────────────────────────────────

// The pure rules share a bundle with the store, whose imports read browser
// storage as they load.
const { mod } = await setup()

test('open only once the state is known — the default is not "off"', () => {
  assert.equal(mod.myDocumentsOpen({ known: false, enabled: false }), null)
  assert.equal(mod.myDocumentsOpen({ known: true, enabled: false }), false)
  assert.equal(mod.myDocumentsOpen({ known: true, enabled: true }), true)
})

test('the route guard redirects only on a known "off"', () => {
  assert.equal(mod.myDocumentsRouteDecision(true, 'ann'), true)
  // Unknown (the state did not arrive in time): the page opens and leaves by
  // itself if the state then says off.
  assert.equal(mod.myDocumentsRouteDecision(null, 'ann'), true)
  assert.deepEqual(mod.myDocumentsRouteDecision(false, 'ann'), { path: '/ann/settings', replace: true })
  assert.equal(mod.settingsListPath('bob'), '/bob/settings')
})

test('the message says whether it was just turned off', () => {
  assert.match(mod.myDocumentsClosedMessage(true), /needs Vector Embedding, which was turned off/)
  assert.match(mod.myDocumentsClosedMessage(false), /needs Vector Embedding, which is off/)
})

test('Settings list: My Documents follows embedding for every profile, admin cards the admin', () => {
  const cards = [
    { route: 'llm' },
    { route: 'embedding', adminOnly: true },
    { route: 'documents', requiresEmbedding: true },
  ]
  const routes = (isAdmin, known, enabled) =>
    mod.visibleSettingsCards(cards, { isAdmin, embedding: { known, enabled } }).map(c => c.route)

  assert.deepEqual(routes(true, true, true), ['llm', 'embedding', 'documents'])
  assert.deepEqual(routes(true, true, false), ['llm', 'embedding'])
  assert.deepEqual(routes(false, true, true), ['llm', 'documents'])
  assert.deepEqual(routes(false, true, false), ['llm'])
  // Not known yet: hidden rather than shown and then taken away.
  assert.deepEqual(routes(false, false, false), ['llm'])
  assert.deepEqual(routes(true, false, true), ['llm', 'embedding'])
})

test('the sync chip needs embedding on as well as something to show', () => {
  const on = { known: true, enabled: true }
  const off = { known: true, enabled: false }
  const unknown = { known: false, enabled: false }
  const busy = { isActive: true, needsAttention: false }
  const alert = { isActive: false, needsAttention: true }
  const quiet = { isActive: false, needsAttention: false }
  assert.equal(mod.documentsChipVisible(on, busy), true)
  assert.equal(mod.documentsChipVisible(on, alert), true)
  assert.equal(mod.documentsChipVisible(on, quiet), false)
  assert.equal(mod.documentsChipVisible(off, busy), false)
  assert.equal(mod.documentsChipVisible(off, alert), false)
  assert.equal(mod.documentsChipVisible(unknown, alert), false)
})

// ── the embedding store's "known" ───────────────────────────────────────────

test('known flips on the first stream snapshot; whenKnown then answers at once', async () => {
  const { env, store } = await setup()
  assert.equal(store.known, false)
  assert.equal(store.enabled, false)
  store.connect(AGENT)
  assert.equal(globalThis.__embeddingSubscribers.length, 1)

  pushState(state({ enabled: true }))
  assert.equal(store.known, true)
  assert.equal(store.enabled, true)
  assert.equal(await store.whenKnown(), true)
  pushState(state({ enabled: false, status: 'disabled', ready: false }))
  assert.equal(await store.whenKnown(), false)
  // Known already: no request.
  assert.equal(env.callsTo(STATUS).length, 0)
  store.disconnect()
})

test('a fresh load asks the status endpoint once, before any stream exists', async () => {
  const { env, store } = await setup()
  env.route(STATUS, () => json(state({ enabled: false, status: 'disabled', ready: false })))
  // Two callers (the guard and the Settings list), one request.
  const [a, b] = await Promise.all([store.whenKnown(1000), store.whenKnown(1000)])
  assert.equal(a, false)
  assert.equal(b, false)
  assert.equal(store.known, true)
  assert.equal(store.status, 'disabled')
  assert.equal(env.callsTo(STATUS).length, 1)
})

test('a stream frame beats a slower status answer, which is then dropped', async () => {
  const { env, store } = await setup()
  const slow = deferred()
  env.route(STATUS, () => slow.promise)
  store.connect(AGENT)
  const pending = store.whenKnown(1000)
  await flush()
  pushState(state({ enabled: false, status: 'disabled', ready: false }))
  assert.equal(await pending, false)
  // The older REST answer lands afterwards and must not undo the frame.
  slow.resolve(json(state({ enabled: true })))
  await flush()
  assert.equal(store.enabled, false)
  store.disconnect()
})

test('nothing answers in time: null, and the default is still not taken for "off"', async () => {
  const { env, store } = await setup()
  const never = deferred()
  env.route(STATUS, () => never.promise)
  assert.equal(await store.whenKnown(30), null)
  assert.equal(store.known, false)
  assert.equal(mod.myDocumentsRouteDecision(null, 'ann'), true)
  // A failed request is the same: wait for the stream, then give up.
  const again = await setup()
  assert.equal(await again.store.whenKnown(30), null)
  assert.equal(again.env.callsTo(STATUS).length, 1)
})

test('the state is server-wide: a profile switch keeps it known', async () => {
  const { settings, store } = await setup()
  store.connect(AGENT)
  pushState(state({ enabled: true }))
  settings.authToken = 'tok-bob'
  await flush()
  assert.equal(store.known, true)
  assert.equal(await store.whenKnown(), true)
  store.disconnect()
})
