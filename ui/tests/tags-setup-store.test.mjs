// The simple-setup store (Settings → Tags): what it polls and when, and how
// the two main flows move through the server's states.
//
// Connect gateway: the page searches a gateway computer's USB ports (a
// `host_scan` operation: queued → running → succeeded with what it found),
// then connects the gateway found there (a `host_connect`: checking →
// registering → claiming → succeeded once the worker claimed it and reported
// in). Add tag: a discovery scans until it finds the tag; with exactly one
// bridge that can take it the page pairs at once, and the pairing runs
// (waiting for the tag to wake) until it succeeds. While a dialog follows
// any of them, polling is fast (1.5 s) and reads only what is followed; when
// it ends the store stops following it, drops back to the 15 s list poll and
// re-reads the lists at once so the new device appears. Nothing survives a
// profile switch.
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

function host(extra = {}) {
  return {
    id: 'h-srv', kind: 'server', name: 'Office PC', platform: 'windows', version: '0.0.19', online: true,
    last_seen_at: '2026-09-28T10:00:00.000Z', state: 'running', reason: null,
    readiness: { state: 'ready', components: [] }, usb: { available: true, container: false, reason: null },
    gateways: [], access: { can_use: true, reason: null, can_manage: true, profiles: [] }, connections: 0, ...extra,
  }
}

function hostOp(kind, state, extra = {}) {
  return {
    id: kind === 'host_scan' ? 'scan-1' : 'conn-1', kind, state, stage: state === 'queued' ? 'queued' : 'started',
    stage_detail: null, host_id: 'h-srv', error: null, created_at: 'x', updated_at: 'y', expires_at: 'z', ...extra,
  }
}

function candidate(id, state, extra = {}) {
  return {
    id, device_id: id.padEnd(32, '0'), short_id: `${id.toUpperCase()}000000`.slice(0, 8), fw: '0.2.0', proto: 2,
    board: 19, state, message: '', companion_id: null, expires_at: 'z', ...extra,
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

test('connect gateway: search a computer, connect what it found, then the list is re-read', async () => {
  const { env, M, store } = await setup()
  let list = listAnswer()
  env.route('/api/tags/connections', (_url, init) => (init.method === 'POST'
    ? json({ operation: hostOp('host_connect', 'queued', { candidate_id: 'hc_1' }) }, 202)
    : json(list)))
  env.route('/api/tags/hosts', () => json({ hosts: [host()], active: [] }))
  env.route('/api/tags/hosts/h-srv/scan', () => json({ operation: hostOp('host_scan', 'queued') }, 202))
  const found = candidate('hc_1', 'usable', { message: 'Ready to connect.' })
  env.route('/api/tags/operations/scan-1', sequence('operation', [
    hostOp('host_scan', 'running', { stage: 'scanning', stage_detail: "Looking at this computer's USB ports" }),
    hostOp('host_scan', 'succeeded', { stage: 'done', candidates: [found], ports: [] }),
  ]))
  env.route('/api/tags/operations/conn-1', sequence('operation', [
    hostOp('host_connect', 'running', { stage: 'checking', stage_detail: 'Checking the gateway', candidate_id: 'hc_1' }),
    hostOp('host_connect', 'running', { stage: 'claiming', stage_detail: 'Connecting to the gateway…', candidate_id: 'hc_1', companion_id: 'comp-1' }),
    hostOp('host_connect', 'succeeded', { stage: 'done', candidate_id: 'hc_1', companion_id: 'comp-1' }),
  ]))

  await Promise.all([store.loadConnections(), store.loadHosts()])
  assert.equal(store.availability, 'available')
  assert.equal(store.hostsLoaded, true)
  assert.deepEqual(store.usableHosts.map((h) => h.id), ['h-srv'])
  assert.equal(store.pollIntervalMs, M.SLOW_POLL_MS)
  assert.equal(store.readiness.addBridgeReason, 'Connect a gateway first.')

  const scan = await store.scanHost('h-srv')
  const post = env.callsTo('/api/tags/hosts/h-srv/scan')[0]
  assert.equal(post.init.method, 'POST')
  assert.ok(post.init.headers['Idempotency-Key'])
  store.follow('hostop', scan.id)
  assert.equal(store.pollIntervalMs, M.FAST_POLL_MS)
  const listCalls = () => env.callsTo('/api/tags/connections').filter((c) => c.init.method === 'GET').length
  const before = listCalls()

  await store.tick()
  assert.deepEqual(M.scanDecision(store.hostOps['scan-1']), { kind: 'searching' })
  assert.equal(M.hostOpProgressLabel(store.hostOps['scan-1']), "Looking at this computer's USB ports…")
  assert.equal(listCalls(), before, 'fast ticks read only what is followed')
  await store.tick()
  const decision = M.scanDecision(store.hostOps['scan-1'])
  assert.equal(decision.kind, 'found')
  assert.equal(decision.preselect, 'hc_1', 'the only free gateway is preselected')
  assert.equal(store.isFollowing('hostop', 'scan-1'), false, 'a finished search is no longer followed')

  const started = await store.connectCandidate('h-srv', decision.preselect, { name: ' Office gateway ' })
  const connect = env.callsTo('/api/tags/connections').find((c) => c.init.method === 'POST')
  assert.deepEqual(JSON.parse(connect.init.body), { host_id: 'h-srv', candidate_id: 'hc_1', name: 'Office gateway' })
  assert.ok(connect.init.headers['Idempotency-Key'])
  store.follow('hostop', started.id)
  await store.tick()
  assert.equal(M.connectStageIndex(store.hostOps['conn-1']), 1)
  await store.tick()
  assert.equal(M.connectStageIndex(store.hostOps['conn-1']), 3)

  // Connected: no longer followed, back to the slow poll, and the list is
  // re-read in the same tick so the new gateway shows up.
  list = listAnswer([connection()])
  const listBefore = listCalls()
  await store.tick()
  assert.equal(store.hostOps['conn-1'].state, 'succeeded')
  assert.equal(M.connectStageIndex(store.hostOps['conn-1']), M.CONNECT_STAGES.length)
  assert.equal(store.isFollowing('hostop', 'conn-1'), false)
  assert.equal(store.pollIntervalMs, M.SLOW_POLL_MS)
  assert.equal(listCalls(), listBefore + 1)
  assert.equal(store.connections.length, 1)
  assert.equal(store.readiness.canAddBridge, true)
  assert.equal(store.readiness.canAddTag, false)
  assert.equal(store.readiness.addTagReason, 'Your gateway cannot reach tags itself. Add a bridge first.')
})

test('a gateway that reaches tags itself takes a tag with no bridge; an older one still needs a bridge', async () => {
  const { env, M, store } = await setup()
  const gateway = (extra = {}) => device('gateway', 'gw-1', { serves_tags: true, capacity: { max_tags: 20, assigned: 0 }, ...extra })
  let list = listAnswer([connection({ gateway: gateway() })])
  env.route('/api/tags/connections', () => json(list))
  const read = async (connections) => {
    list = listAnswer(connections)
    await store.loadConnections()
    return store.readiness
  }

  let r = await read([connection({ gateway: gateway() })])
  assert.equal(r.canAddTag, true, 'no bridge needed')
  assert.equal(r.addTagReason, '')
  assert.deepEqual(r.tagGateways.map((c) => c.id), ['comp-1'])
  assert.equal(M.tagReach(r), 'your gateway')

  // An older gateway reaches no tag itself: a bridge first.
  r = await read([connection({ gateway: device('gateway', 'gw-1', { serves_tags: false }) })])
  assert.equal(r.canAddTag, false)
  assert.equal(r.addTagReason, 'Your gateway cannot reach tags itself. Add a bridge first.')
  assert.equal(M.tagReach(r), 'one of your bridges')
  r = await read([connection({ gateway: device('gateway', 'gw-1', { serves_tags: false }), bridges: [device('bridge', 'br-1')] })])
  assert.equal(r.canAddTag, true)

  // Paused, offline or still setting up: the same reasons Add bridge gives.
  r = await read([connection({ gateway: gateway(), paused: true, status: 'paused' })])
  assert.equal(r.canAddTag, false)
  assert.equal(r.addTagReason, 'Your gateway is paused. Resume it first.')
  assert.equal(r.addTagReason, r.addBridgeReason)
  r = await read([connection({ gateway: gateway(), status: 'offline' })])
  assert.equal(r.addTagReason, 'Your gateway is offline. Check that it is plugged in and that its computer is on.')
  r = await read([connection({ gateway: gateway(), status: 'setting_up' })])
  assert.equal(r.addTagReason, 'Wait until the gateway is connected.')
  // It reaches tags, but is not ready yet (checked again after a restore).
  r = await read([connection({ gateway: gateway({ state: 'reconciling' }) })])
  assert.equal(r.canAddTag, false)
  assert.equal(r.addTagReason, 'Wait until the gateway shows Ready.')

  // Both: the tag can be near either.
  r = await read([connection({ gateway: gateway(), bridges: [device('bridge', 'br-1')] })])
  assert.equal(M.tagReach(r), 'your gateway or one of your bridges')
  assert.equal(M.setupErrorMessage('not_found', { role: 'tag', near: M.tagReach(r) }),
    'The tag was not found. Tags check in about every 30 seconds: keep it close to your gateway or one of your bridges and try again.')
})

test('where a tag connects, in the words the page uses: the gateway by its connection, a bridge by its name', async () => {
  const { M } = await setup()
  const hall = device('bridge', 'br-1', { name: 'Hall' })
  const conn = connection({ bridges: [hall] })
  const cand = (extra = {}) => ({
    id: 'c1', gateway_id: 'comp-1', bridge_id: 'br-1', bridge_kind: 'bridge', bridge_name: null, rssi: -50,
    seen_at: 'z', capacity: null, eligible: true, reason: null, ...extra,
  })
  // The gateway's own radio heard the tag: named as its connection (the server's name first).
  assert.equal(M.candidateTitle(cand({ bridge_kind: 'gateway', bridge_id: 'gw-1', bridge_name: 'Office gateway' }), 'tag', [conn]), 'Office gateway')
  assert.equal(M.candidateTitle(cand({ bridge_kind: 'gateway', bridge_id: 'gw-1' }), 'tag', [conn]), 'Desk gateway')
  // A bridge by its name; an older server sends no bridge_kind (its candidates were bridges).
  assert.equal(M.candidateTitle(cand(), 'tag', [conn]), 'Hall')
  assert.equal(M.candidateTitle(cand({ bridge_kind: undefined, bridge_name: 'Porch' }), 'tag', [conn]), 'Porch')
  assert.equal(M.candidateTitle(cand({ bridge_id: 'gone' }), 'tag', [conn]), 'A bridge')
  // A bridge's search: the gateway with its computer.
  assert.equal(M.candidateTitle(cand({ bridge_kind: 'gateway', bridge_id: null }), 'bridge', [conn]), 'Desk gateway on DESKTOP-ABC')

  // A tag's parent: its gateway or one of the connection's bridges.
  const parent = (t) => { const p = M.tagParent(t, conn); return p && { kind: p.kind, title: p.title } }
  assert.deepEqual(parent({ bridge_id: 'gw-1' }), { kind: 'gateway', title: 'Desk gateway' })
  assert.deepEqual(parent({ bridge_id: 'br-1' }), { kind: 'bridge', title: 'Hall' })
  assert.equal(parent({ bridge_id: null }), null)

  // Refusals say gateway or bridge, never only bridge.
  assert.equal(M.setupErrorMessage('no_ready_bridge'),
    'Your gateway cannot reach tags itself. Add a bridge first, and wait until it shows Ready.')
  assert.equal(M.setupErrorMessage('candidate_not_eligible'),
    'That gateway or bridge cannot take this tag right now. Choose another one.')
  assert.equal(M.setupErrorMessage('bridge_full'),
    'That gateway or bridge has no room for more tags. Choose another one, or remove a tag from it first.')
})

test('what a search means, and the words for each problem a gateway computer can have', async () => {
  const { M } = await setup()
  const done = (ports) => ({ state: 'succeeded', candidates: [], ports, error: null })
  assert.deepEqual(M.scanDecision(done([])), { kind: 'problem', problem: 'no_gateway' })
  assert.deepEqual(M.scanDecision(done([{ reason: 'no_access', detail: '' }])), { kind: 'problem', problem: 'usb_access_denied' })
  assert.deepEqual(M.scanDecision(done([{ reason: 'busy', detail: '' }])), { kind: 'problem', problem: 'gateway_busy' })
  assert.deepEqual(M.scanDecision(done([{ reason: 'v1_firmware', detail: '' }])), { kind: 'problem', problem: 'unsupported_firmware' })
  assert.deepEqual(M.scanDecision({ state: 'failed', error: { code: 'host_offline', message: 'x' } }),
    { kind: 'problem', problem: 'host_offline' })

  // Free gateways first (the only free one preselected); one of yours driven from elsewhere offers recovery.
  const found = M.scanDecision({
    state: 'succeeded', candidates: [candidate('b', 'owned_elsewhere'), candidate('a', 'usable')], ports: [], error: null,
  })
  assert.deepEqual(found.candidates.map((c) => c.id), ['a', 'b'])
  assert.equal(found.preselect, 'a')
  assert.equal(M.candidateView(candidate('r', 'recovery_required')).action, 'recover')
  assert.equal(M.candidateView(candidate('o', 'owned_elsewhere')).problem, 'owned_elsewhere')
  assert.equal(M.candidateView(candidate('u', 'usable')).action, 'connect')

  // Each problem has its own title and a way forward, naming the computer — never a port.
  const office = host({ platform: 'linux' })
  const titles = ['no_gateway', 'usb_access_denied', 'gateway_busy', 'unsupported_firmware', 'components_unavailable',
    'host_offline', 'owned_elsewhere', 'recovery_required'].map((p) => M.problemText(p, office).title)
  assert.deepEqual(titles, ['No gateway detected', 'USB access denied', 'Gateway busy in another application',
    'Unsupported firmware', 'Required components unavailable', 'Computer offline', 'Gateway owned elsewhere',
    'Recovery required'])
  assert.match(M.problemText('no_gateway', office).text, /Office PC's USB ports/)
  assert.match(M.problemText('usb_access_denied', office).text, /dialout/)
  assert.match(M.problemText('usb_access_denied', host({ usb: { available: false, container: true, reason: null } })).text,
    /container/)
  assert.equal(M.problemText('components_unavailable', office).action, 'prepare')
  assert.equal(M.problemText('components_unavailable', host({ access: { can_use: true, reason: null, can_manage: false } })).action,
    null)
  assert.deepEqual(M.plugInstruction(office),
    { before: 'Plug the gateway into ', name: 'Office PC', after: ', where Cremind is running.' })

  // A computer that cannot search says why.
  assert.equal(M.hostBlock(host()), null)
  assert.equal(M.hostBlock(host({ online: false })).title, 'Computer offline')
  assert.equal(M.hostBlock(host({ state: 'unavailable' })).action, 'prepare')
  assert.equal(M.hostBlock(host({ usb: { available: false, container: true, reason: null } })).title, 'USB access denied')
})

test('an update that brings new fonts: the computer stays ready, and says how to install them', async () => {
  const { M } = await setup()
  const outdated = {
    state: 'ready', fonts_update: true,
    components: [{ key: 'fonts', state: 'outdated', detail: 'Tag screens use font pack b6009b469294a509; …' }],
  }
  const member = { can_use: true, reason: null, can_manage: false }
  assert.deepEqual(M.hostStatePill(host()), { label: 'Ready', type: 'success' })
  assert.deepEqual(M.hostStatePill(host({ readiness: outdated })), { label: 'Ready, font update available', type: 'success' })
  assert.deepEqual(M.hostStatePill(host({ readiness: { state: 'partial', components: [] } })),
    { label: 'Ready, screens waiting', type: 'warning' })
  // Not a problem: the computer still searches and connects; a note says what to do.
  assert.equal(M.hostBlock(host({ readiness: outdated })), null)
  assert.equal(M.fontsUpdateNote(host()), null)
  assert.match(M.fontsUpdateNote(host({ readiness: outdated })), /screens keep working.*Prepare the components to install it/)
  assert.match(M.fontsUpdateNote(host({ readiness: outdated, access: member })), /admin can install it in Settings → Tags/)
  // A desktop computer prepares its own components: the page cannot do it from here.
  assert.match(M.fontsUpdateNote(host({ readiness: outdated, kind: 'desktop', name: 'Laptop', access: member })),
    /Run "cremind tags host prepare" on Laptop, then restart Cremind there/)
  // Offline, or not running: nothing to say about fonts.
  assert.equal(M.fontsUpdateNote(host({ readiness: outdated, online: false })), null)
  assert.equal(M.fontsUpdateNote(host({ readiness: outdated, state: 'unavailable' })), null)
})

test('a connection still running shows as a setup to continue, also after a refresh', async () => {
  const { env, store } = await setup()
  env.route('/api/tags/connections', () => json(listAnswer()))
  env.route('/api/tags/hosts', () => json({
    hosts: [host()],
    active: [
      hostOp('host_connect', 'running', { stage: 'claiming', stage_detail: 'Connecting to the gateway…', companion_id: 'comp-1' }),
      hostOp('host_scan', 'running'),
    ],
  }))
  await Promise.all([store.loadConnections(), store.loadHosts()])
  assert.deepEqual(store.pending.map((p) => [p.kind, p.id, p.title, p.detail]),
    [['connect', 'conn-1', 'Connecting a gateway', 'Connecting to the gateway…']])
  // Continued from the banner: its last known state shows at once.
  store.follow('hostop', 'conn-1')
  assert.equal(store.hostOps['conn-1'].stage, 'claiming')
})

test('set up a gateway computer: the link stays with the caller, the store follows the session to done', async () => {
  const { env, M, store } = await setup()
  const SECRET = 'T0ken-that-must-never-be-stored-AAAAAAAAAAAAAAAA'
  const computer = { installation_id: 'inst-9', name: 'LAPTOP-9', platform: 'windows', version: '0.0.19' }
  const enroll = (state, extra = {}) => session(state, { operation: 'enroll_host', computer, ...extra })
  env.route('/api/tags/connections', () => json(listAnswer()))
  env.route('/api/tags/setup-sessions', () => json({
    session: enroll('waiting_for_connect', { computer: null }),
    launch_url: `cremind://tags/setup?v=1&server=${encodeURIComponent(AGENT)}&session=ses-1&token=${SECRET}`,
  }, 201))
  env.route('/api/tags/setup-sessions/ses-1', sequence('session', [
    enroll('waiting_for_approval', { verification_phrase: 'amber orbit lantern tidal' }),
    enroll('waiting_for_confirmation', { verification_phrase: 'amber orbit lantern tidal', native_approved: true }),
    enroll('completed', { verification_phrase: 'amber orbit lantern tidal', host_id: 'h-lap' }),
  ]))
  env.route('/api/tags/setup-sessions/ses-1/confirm', () => json({ session: enroll('redeeming') }))

  const { session: created, launchUrl } = await store.startSession('enroll_host')
  assert.ok(launchUrl.startsWith('cremind://tags/setup?v=1&server='))
  const post = env.callsTo('/api/tags/setup-sessions').find((c) => c.init.method === 'POST')
  assert.deepEqual(JSON.parse(post.init.body), { operation: 'enroll_host', server_url: AGENT })
  assert.equal(M.enrollStep(created), 'open')
  assert.ok(!JSON.stringify(store.sessions).includes(SECRET), "the link's secret never lands in the store")
  assert.deepEqual(M.pendingSetups([], [], [created]).map((p) => [p.kind, p.title]),
    [['enroll', 'Setting up a gateway computer']])

  store.follow('session', 'ses-1')
  await store.tick()
  assert.equal(M.enrollStep(store.sessions['ses-1']), 'approve')
  await store.tick()
  assert.equal(M.enrollStep(store.sessions['ses-1']), 'confirm')
  await store.confirmSession('ses-1')
  assert.equal(M.enrollStep(store.sessions['ses-1']), 'finish')
  await store.tick()
  assert.equal(M.enrollStep(store.sessions['ses-1']), 'done')
  assert.equal(store.sessions['ses-1'].host_id, 'h-lap')
  assert.equal(store.isFollowing('session', 'ses-1'), false)
  assert.equal(M.enrollFailure({ state: 'expired', error: null }),
    'The setup took longer than five minutes and has expired. Start again.')
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

test('a tag enrolled with the hardware tools: imported by its tag id, then followed like a pairing', async () => {
  const { env, M, store } = await setup()
  env.route('/api/tags/connections', () => json(listAnswer([connection()])))
  await store.loadConnections()
  const op = (state, extra = {}) => ({
    id: 'op-9', kind: 'import_tag', state, stage: 'clearing', stage_detail: null, device: null, error: null,
    created_at: 'x', updated_at: 'y', role: 'tag', first_tag: true, ...extra,
  })
  env.route('/api/tags/imports', () => json({ pairing: op('queued') }, 201))
  env.route('/api/tags/pairings/op-9', sequence('pairing', [
    op('running', { stage_detail: 'Waiting for the tag to wake' }),
    op('succeeded', { device: device('tag', 'tag-9', { name: 'Shelf', short_id: 'D1F06B9A' }) }),
  ]))

  const started = await store.importTag(' d1f06b9a ', ' Shelf ')
  const post = env.callsTo('/api/tags/imports')[0]
  assert.equal(post.init.method, 'POST')
  assert.deepEqual(JSON.parse(post.init.body), { tag_id: 'd1f06b9a', name: 'Shelf' })
  assert.ok(post.init.headers['Idempotency-Key'])
  store.follow('pairing', started.id)
  await store.tick()
  assert.equal(M.isWaitingForWake(store.pairings['op-9']), true)
  await store.tick()
  assert.equal(store.pairings['op-9'].state, 'succeeded')
  assert.equal(store.isFollowing('pairing', 'op-9'), false)

  // Still running after a refresh: it resumes in Add tag, like a pairing.
  assert.deepEqual(M.pendingSetups([op('running')]).map((p) => [p.kind, p.id, p.title]),
    [['pair_tag', 'op-9', 'Adding a tag']])
  // Its refusals, in plain words.
  assert.match(M.setupErrorMessage('not_enrolled_here', { role: 'tag' }), /computer where the tag was enrolled/)
  assert.match(M.setupErrorMessage('import_not_allowed', { role: 'tag' }), /owner of the computer/)
  assert.match(M.setupErrorMessage('invalid_tag_id', { role: 'tag' }), /8 characters/)
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
  const slowHosts = deferred()
  env.route('/api/tags/connections', () => slow.promise)
  env.route('/api/tags/hosts', () => slowHosts.promise)
  const pending = store.loadConnections()
  const pendingHosts = store.loadHosts()
  store.follow('session', 'ses-1')

  settings.authToken = 'tok-bob'
  store.reset('bob')
  slow.resolve(json(listAnswer([connection()])))
  slowHosts.resolve(json({ hosts: [host()], active: [hostOp('host_connect', 'running')] }))
  await Promise.all([pending, pendingHosts])
  await flush()
  assert.equal(store.connections.length, 0, "ann's gateways never show for bob")
  assert.equal(store.hosts.length, 0, "nor ann's computers")
  assert.equal(store.pending.length, 0)
  assert.equal(store.following.length, 0)
  assert.equal(store.availability, 'unknown')
})
