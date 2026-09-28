// The simple-setup store (Settings → Tags): what it polls and when, and how
// the two main flows move through the server's states.
//
// Connect gateway: a setup session goes waiting_for_connect → (Connect binds)
// waiting_for_approval → (native approval) waiting_for_confirmation → (this
// page confirms) redeeming → connecting → completed. Add tag: a discovery
// scans until it finds the tag; with exactly one bridge that can take it the
// page pairs at once, and the pairing runs (waiting for the tag to wake) until
// it succeeds. While a dialog follows either, polling is fast (1.5 s) and
// reads only what is followed; when it ends the store stops following it,
// drops back to the 15 s list poll and re-reads the list at once so the new
// device appears. Nothing survives a profile switch.
import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, installBrowser, json, load } from './harness.mjs'

const AGENT = 'http://localhost:1515'
const CODE = '4D6KRART000G40R40M30E2097'

function session(state, extra = {}) {
  return {
    id: 'ses-1', operation: 'connect_gateway', state,
    expires_at: '2026-09-28T10:05:00.000Z', created_at: '2026-09-28T10:00:00.000Z',
    computer: null, verification_phrase: null, gateway: null, native_approved: false,
    browser_confirmed: false, companion_id: null, operation_id: null, error: null, ...extra,
  }
}

function device(kind, id, extra = {}) {
  return {
    id, binding_id: `b-${id}`, kind, name: '', device_id: 'f'.repeat(32), short_id: '1A2B3C4D',
    state: 'ready', paused: false, generation: 1, fw: '0.2.0', board: 19, last_contact_at: null,
    battery_mv: null, rssi: null, capacity: null, fontpack_ok: null, bridge_id: null, delivery: null,
    affected_tag_ids: null, ...extra,
  }
}

function connection(extra = {}) {
  return {
    id: 'comp-1', name: 'Desk gateway', status: 'connected', paused: false,
    computer: { installation_id: 'inst-1', name: 'DESKTOP-ABC', platform: 'windows', version: '0.2.0', last_seen_at: null },
    gateway: device('gateway', 'gw-1'), bridges: [], tags: [], last_seen_at: null, created_at: 'x', ...extra,
  }
}

function listAnswer(connections = [], active = {}) {
  return {
    simple_setup: true, connections, computers: [],
    active: { sessions: active.sessions ?? [], operations: active.operations ?? [] },
  }
}

async function setup() {
  const env = installBrowser()
  // The settings store reads the backend URL from the desktop bridge first.
  globalThis.window.cremind = { config: { agentUrl: AGENT } }
  const M = await load('tests/entries/tags-setup-store.ts')
  M.setActivePinia(M.createPinia())
  const settings = M.useSettingsStore()
  settings.authToken = 'tok-ann'
  const store = M.useTagsSetupStore()
  store.reset('ann')
  return { env, M, settings, store }
}

/** Answer each GET with `{[field]: next item}` (the last one repeats). */
function sequence(field, answers) {
  let i = 0
  return () => json({ [field]: answers[Math.min(i++, answers.length - 1)] })
}

test('connect gateway: fast polling follows the session through every state, then the list is re-read', async () => {
  const { env, M, store } = await setup()
  let list = listAnswer()
  env.route('/api/tags/connections', () => json(list))
  env.route('/api/tags/setup-sessions', () => json({
    session: session('waiting_for_connect'), launch_url: 'cremind-connect://setup?v=1&session=ses-1&token=t',
  }, 201))
  const phrase = 'amber orbit lantern tidal'
  const computer = { installation_id: 'inst-1', name: 'DESKTOP-ABC', platform: 'windows', version: '0.2.0' }
  const gateway = { device_id: 'f'.repeat(32), short_id: '9C8B7A65', fw: '0.2.0', usable: true }
  env.route('/api/tags/setup-sessions/ses-1', sequence('session', [
    session('waiting_for_connect'),
    session('waiting_for_approval', { computer, verification_phrase: phrase }),
    session('waiting_for_confirmation', { computer, verification_phrase: phrase, gateway, native_approved: true }),
    session('connecting', { computer, gateway, native_approved: true, browser_confirmed: true, companion_id: 'comp-1' }),
    session('completed', { computer, gateway, native_approved: true, browser_confirmed: true, companion_id: 'comp-1' }),
  ]))
  env.route('/api/tags/setup-sessions/ses-1/confirm', () => json({
    session: session('redeeming', { computer, gateway, native_approved: true, browser_confirmed: true }),
  }))

  await store.loadConnections()
  assert.equal(store.availability, 'available')
  assert.equal(store.pollIntervalMs, M.SLOW_POLL_MS)
  assert.equal(store.readiness.canAddBridge, false)
  assert.equal(store.readiness.addBridgeReason, 'Connect a gateway first.')

  const { session: created, launchUrl } = await store.startSession('connect_gateway')
  assert.equal(launchUrl, 'cremind-connect://setup?v=1&session=ses-1&token=t')
  const post = env.callsTo('/api/tags/setup-sessions').find((c) => c.init.method === 'POST')
  assert.deepEqual(JSON.parse(post.init.body), { operation: 'connect_gateway', server_url: AGENT })
  assert.ok(post.init.headers['Idempotency-Key'])

  store.follow('session', created.id)
  assert.equal(store.pollIntervalMs, M.FAST_POLL_MS)
  const listCalls = () => env.callsTo('/api/tags/connections').length
  const before = listCalls()

  const states = []
  for (let i = 0; i < 3; i += 1) {
    await store.tick()
    states.push(store.sessions['ses-1'].state)
  }
  assert.deepEqual(states, ['waiting_for_connect', 'waiting_for_approval', 'waiting_for_confirmation'])
  assert.equal(M.connectStep(store.sessions['ses-1']), 'confirm')
  assert.equal(store.sessions['ses-1'].verification_phrase, phrase)
  assert.equal(listCalls(), before, 'fast ticks read only the followed session')

  const confirmed = await store.confirmSession('ses-1')
  assert.equal(confirmed.state, 'redeeming')
  assert.equal(store.sessions['ses-1'].state, 'redeeming')
  const confirm = env.callsTo('/confirm')[0]
  assert.equal(confirm.init.method, 'POST')
  assert.ok(confirm.init.headers['Idempotency-Key'])

  await store.tick()
  assert.equal(store.sessions['ses-1'].state, 'connecting')
  assert.equal(M.connectStep(store.sessions['ses-1']), 'finish')
  assert.equal(store.isFollowing('session', 'ses-1'), true)

  // Completed: no longer followed, back to the slow poll, and the list is
  // re-read in the same tick so the new gateway shows up.
  list = listAnswer([connection()])
  await store.tick()
  assert.equal(store.sessions['ses-1'].state, 'completed')
  assert.equal(M.connectStep(store.sessions['ses-1']), 'done')
  assert.equal(store.isFollowing('session', 'ses-1'), false)
  assert.equal(store.pollIntervalMs, M.SLOW_POLL_MS)
  assert.equal(listCalls(), before + 1)
  assert.equal(store.connections.length, 1)
  assert.equal(store.readiness.canAddBridge, true)
  assert.equal(store.readiness.canAddTag, false)
  assert.equal(store.readiness.addTagReason, 'Add a bridge first.')
})

test('add tag: scanning → found with one bridge → pair at once → waiting for the tag to wake → ready', async () => {
  const { env, M, store } = await setup()
  const bridge = device('bridge', 'br-1', { name: 'Hall', capacity: { max_tags: 20, assigned: 3 } })
  let list = listAnswer([connection({ bridges: [bridge] })])
  env.route('/api/tags/connections', () => json(list))
  await store.loadConnections()
  assert.equal(store.readiness.canAddTag, true)

  const scanning = { id: 'dis-1', role: 'tag', short_id: '1A2B3C4D', state: 'scanning', started_at: 'x', expires_at: 'y', candidates: [], recommended: null, error: null }
  const candidate = { id: 'c1', gateway_id: 'comp-1', bridge_id: 'br-1', bridge_name: 'Hall', rssi: -61, seen_at: 'z', capacity: { max_tags: 20, assigned: 3 }, eligible: true, reason: null }
  env.route('/api/tags/discovery', () => json({ discovery: scanning }, 201))
  env.route('/api/tags/discovery/dis-1', sequence('discovery', [
    scanning,
    { ...scanning, state: 'found', candidates: [candidate], recommended: 'c1' },
  ]))
  const pairing = (state, extra = {}) => ({
    id: 'op-7', kind: 'pair_tag', state, stage: 'pairing', stage_detail: null, device: null, error: null,
    created_at: 'x', updated_at: 'y', role: 'tag', first_tag: true, ...extra,
  })
  env.route('/api/tags/pairings', () => json({ pairing: pairing('queued') }, 201))
  const paired = device('tag', 'tag-1', { name: 'Kitchen', bridge_id: 'br-1' })
  env.route('/api/tags/pairings/op-7', sequence('pairing', [
    pairing('running', { stage_detail: 'Waiting for the tag to wake' }),
    pairing('succeeded', { device: paired }),
  ]))

  const discovery = await store.startDiscovery('tag', CODE)
  const post = env.callsTo('/api/tags/discovery')[0]
  assert.equal(post.init.method, 'POST')
  assert.deepEqual(JSON.parse(post.init.body), { role: 'tag', setup_code: CODE })
  assert.ok(post.init.headers['Idempotency-Key'])
  store.follow('discovery', discovery.id)
  assert.equal(store.pollIntervalMs, M.FAST_POLL_MS)

  await store.tick()
  assert.deepEqual(M.discoveryDecision(store.discoveries['dis-1']), { kind: 'searching' })
  await store.tick()
  const decision = M.discoveryDecision(store.discoveries['dis-1'])
  assert.deepEqual(decision, { kind: 'pair', candidateId: 'c1' })

  // The dialog pairs at once with the only bridge that can take the tag.
  store.unfollow('discovery', 'dis-1')
  const started = await store.startPairing('dis-1', decision.candidateId, ' Kitchen ')
  assert.deepEqual(JSON.parse(env.callsTo('/api/tags/pairings')[0].init.body),
    { discovery_id: 'dis-1', candidate_id: 'c1', name: 'Kitchen' })
  store.follow('pairing', started.id)

  await store.tick()
  const running = store.pairings['op-7']
  assert.equal(running.state, 'running')
  assert.equal(M.isWaitingForWake(running), true)
  assert.equal(M.operationProgressLabel(running), 'Waiting for the tag to wake')

  list = listAnswer([connection({ bridges: [{ ...bridge, capacity: { max_tags: 20, assigned: 4 } }], tags: [paired] })])
  const listBefore = env.callsTo('/api/tags/connections').length
  await store.tick()
  assert.equal(store.pairings['op-7'].state, 'succeeded')
  assert.equal(store.pairings['op-7'].first_tag, true)
  assert.equal(store.isFollowing('pairing', 'op-7'), false)
  assert.equal(store.pollIntervalMs, M.SLOW_POLL_MS)
  assert.equal(env.callsTo('/api/tags/connections').length, listBefore + 1)
  assert.deepEqual(store.tags.map((t) => t.device.name), ['Kitchen'])
})

test('several bridges hear the tag: the page lets the person choose, recommended preselected', async () => {
  const { M } = await setup()
  const base = { id: 'd', state: 'found', error: null, recommended: 'c2' }
  const c = (id, rssi, eligible = true, reason = null) => ({ id, gateway_id: 'comp-1', bridge_id: id, bridge_name: id, rssi, seen_at: 'z', capacity: null, eligible, reason })
  assert.deepEqual(M.discoveryDecision({ ...base, candidates: [c('c1', -50), c('c2', -70)] }), { kind: 'choose', preselect: 'c2' })
  // One eligible among several: straight to pairing.
  assert.deepEqual(M.discoveryDecision({ ...base, candidates: [c('c1', -50, false, 'bridge_full'), c('c2', -70)] }), { kind: 'pair', candidateId: 'c2' })
  assert.deepEqual(M.discoveryDecision({ ...base, candidates: [c('c1', -50, false, 'bridge_full')] }), { kind: 'unavailable', reason: 'bridge_full' })
  assert.deepEqual(M.discoveryDecision({ ...base, state: 'not_found', candidates: [] }), { kind: 'not_found' })
})

test('stopping a pairing that is still being reconciled keeps following it until it ends', async () => {
  const { env, M, store } = await setup()
  env.route('/api/tags/connections', () => json(listAnswer([connection()])))
  await store.loadConnections()
  const op = (state) => ({
    id: 'op-8', kind: 'pair_tag', state, stage: 'reconciling', stage_detail: null, device: null, error: null,
    created_at: 'x', updated_at: 'y', role: 'tag', first_tag: false,
  })
  let final = 'running'
  env.route('/api/tags/pairings/op-8', (_url, init) => json({ pairing: init.method === 'DELETE' ? op('running') : op(final) }))
  store.follow('pairing', 'op-8')
  await store.tick()
  const stopped = await store.cancelPairing('op-8')
  assert.equal(stopped.state, 'running', 'the device may already have taken it: the server checks first')
  assert.equal(env.callsTo('/api/tags/pairings/op-8').find((c) => c.init.method === 'DELETE').init.headers['Idempotency-Key'].length, 36)
  assert.equal(store.isFollowing('pairing', 'op-8'), true)
  assert.equal(store.pollIntervalMs, M.FAST_POLL_MS)
  final = 'cancelled'
  await store.tick()
  assert.equal(store.pairings['op-8'].state, 'cancelled')
  assert.equal(store.isFollowing('pairing', 'op-8'), false)
})

test('a retried mutation after a dropped answer sends the same key', async () => {
  const { env, store } = await setup()
  let fail = true
  env.route('/api/tags/discovery', () => {
    if (fail) throw new TypeError('Failed to fetch')
    return json({ discovery: { id: 'dis-2', state: 'scanning', candidates: [] } }, 201)
  })
  await assert.rejects(store.startDiscovery('tag', CODE), TypeError)
  fail = false
  await store.startDiscovery('tag', CODE)
  const [a, b] = env.callsTo('/api/tags/discovery').map((c) => c.init.headers['Idempotency-Key'])
  assert.equal(a, b)
})

test('an expired or vanished session stops being followed', async () => {
  const { env, store } = await setup()
  env.route('/api/tags/connections', () => json(listAnswer()))
  env.route('/api/tags/setup-sessions', () => json({ session: session('waiting_for_connect'), launch_url: 'x' }, 201))
  env.route('/api/tags/setup-sessions/ses-1', () => json({ error: 'session_expired', message: 'Expired.' }, 410))
  await store.loadConnections()
  await store.startSession('connect_gateway')
  store.follow('session', 'ses-1')
  await store.tick()
  assert.equal(store.sessions['ses-1'].state, 'expired')
  assert.equal(store.lost['ses-1'], 'expired')
  assert.equal(store.isFollowing('session', 'ses-1'), false)
})

test('simple setup off (or an older server): the page gets told, and polling stops', async () => {
  const { env, store } = await setup()
  env.route('/api/tags/connections', () => json({ simple_setup: false, connections: [], computers: [], active: { sessions: [], operations: [] } }))
  await store.loadConnections()
  assert.equal(store.availability, 'disabled')

  const second = await setup()
  second.env.route('/api/tags/connections', () => json({ error: 'simple_setup_disabled', message: 'Off.' }, 403))
  await second.store.loadConnections()
  assert.equal(second.store.availability, 'disabled')
  const calls = second.env.callsTo('/api/tags/connections').length
  await second.store.tick()
  assert.equal(second.env.callsTo('/api/tags/connections').length, calls, 'nothing polls while it is off')

  const third = await setup()
  third.env.route('/api/tags/connections', () => json({ detail: 'Not Found' }, 404))
  await third.store.loadConnections()
  assert.equal(third.store.availability, 'unsupported')
  assert.equal(env.callsTo('/api/tags/connections').length, 1)
})

test('nothing crosses profiles: a switch clears the store and drops answers for the old token', async () => {
  const { env, settings, store } = await setup()
  const slow = deferred()
  env.route('/api/tags/connections', () => slow.promise)
  const pending = store.loadConnections()
  store.follow('session', 'ses-1')

  settings.authToken = 'tok-bob'
  store.reset('bob')
  slow.resolve(json(listAnswer([connection()])))
  await pending
  await flush()
  assert.equal(store.connections.length, 0, "ann's gateways never show for bob")
  assert.equal(store.following.length, 0)
  assert.equal(store.availability, 'unknown')
})
