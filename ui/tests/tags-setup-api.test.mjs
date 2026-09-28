// The simple-setup client (cremind-tag docs/setup-api.md §1): paths, methods,
// bodies, Bearer auth, the error shape — and the Idempotency-Key every
// mutation carries.
//
// A retry of the same action whose first outcome is unknown (the network
// dropped the answer, or the server failed) must send the SAME key, so the
// server answers with the first result instead of pairing twice. Once an
// answer arrived — even a refusal like `gateway_offline` — the next attempt is
// a new action with a NEW key (the same key would replay the refusal after
// the cause was fixed). A different body is a different action.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, json, load } from './harness.mjs'

let env = installBrowser()
const api = await load('src/services/tagsSetupApi.ts')

const URL_ = 'http://localhost:1180'
const TOKEN = 'jwt-ann'
const CODE = '4D6KRART000G40R40M30E2097'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

const SESSION = {
  id: 'ses-1', operation: 'connect_gateway', state: 'waiting_for_connect',
  expires_at: '2026-09-28T10:05:00.000Z', created_at: '2026-09-28T10:00:00.000Z',
  computer: null, verification_phrase: null, gateway: null, native_approved: false,
  browser_confirmed: false, companion_id: null, operation_id: null, error: null,
}
const DISCOVERY = {
  id: 'dis-1', role: 'tag', short_id: '1A2B3C4D', state: 'scanning', started_at: 'x', expires_at: 'y',
  candidates: [], recommended: null, error: null,
}
const PAIRING = {
  id: 'op-1', kind: 'pair_tag', state: 'running', stage: 'pairing', stage_detail: 'Waiting for the tag to wake',
  device: null, error: null, created_at: 'x', updated_at: 'y', role: 'tag', first_tag: true,
}

function bodyOf(call) {
  return call.init.body === undefined ? undefined : JSON.parse(call.init.body)
}
function keyOf(call) {
  return call.init.headers['Idempotency-Key']
}
function refusal(status, error, message, extra = {}) {
  return json({ error, message, detail: message, ...extra }, status)
}

test('the list is a plain GET with the Bearer token and no Idempotency-Key', async () => {
  env = installBrowser()
  env.route('/api/tags/connections', () => json({
    simple_setup: true, connections: [], computers: [], active: { sessions: [], operations: [] },
  }))
  const answer = await api.getConnections(URL_, TOKEN)
  assert.equal(answer.simple_setup, true)
  const [call] = env.calls
  assert.equal(call.url, `${URL_}/api/tags/connections`)
  assert.equal(call.init.method, 'GET')
  assert.equal(call.init.headers.Authorization, `Bearer ${TOKEN}`)
  assert.equal(keyOf(call), undefined)
})

test('every endpoint uses its path and method, and every mutation carries a UUID key', async () => {
  env = installBrowser()
  env.route('/api/tags/setup-sessions', (_url, init) => json(
    init.method === 'POST' ? { session: SESSION, launch_url: 'cremind-connect://setup?v=1&session=ses-1' } : { session: SESSION },
    init.method === 'POST' ? 201 : 200))
  env.route('/api/tags/setup-sessions/ses-1/confirm', () => json({ session: { ...SESSION, state: 'redeeming' } }))
  env.route('/api/tags/discovery', () => json({ discovery: DISCOVERY }, 201))
  env.route('/api/tags/discovery/dis-1', () => json({ discovery: DISCOVERY }))
  env.route('/api/tags/pairings', () => json({ pairing: PAIRING }, 201))
  env.route('/api/tags/pairings/op-1', () => json({ pairing: PAIRING }))
  env.route('/api/tags/devices/', (url) => {
    if (url.endsWith('/unpair')) return json({ operation: { id: 'op-2', kind: 'unpair' }, device: { id: 'd1' } })
    if (url.endsWith('/test')) return json({ delivery: { id: 77 } }, 201)
    return json({ device: { id: 'd1', paused: url.endsWith('/pause') } })
  })
  env.route('/api/tags/recoveries', () => json({ recovery: { id: 'rec-1' }, session: SESSION, launch_url: 'cremind-connect://x' }, 201))
  env.route('/api/tags/recoveries/rec-1', () => json({ recovery: { id: 'rec-1', devices: [] } }))
  env.route('/api/tags/connect', () => json({ latest_version: '0.2.0', downloads: {} }))

  const created = await api.createSetupSession(URL_, TOKEN, { operation: 'connect_gateway', server_url: 'http://localhost:1180' })
  assert.equal(created.launch_url, 'cremind-connect://setup?v=1&session=ses-1')
  assert.equal((await api.getSetupSession(URL_, TOKEN, 'ses-1')).id, 'ses-1')
  assert.equal((await api.confirmSetupSession(URL_, TOKEN, 'ses-1')).state, 'redeeming')
  await api.cancelSetupSession(URL_, TOKEN, 'ses-1')
  assert.equal((await api.startDiscovery(URL_, TOKEN, { role: 'tag', setup_code: CODE })).id, 'dis-1')
  await api.getDiscovery(URL_, TOKEN, 'dis-1')
  assert.equal((await api.startPairing(URL_, TOKEN, { discovery_id: 'dis-1', candidate_id: 'c1' })).first_tag, true)
  await api.getPairing(URL_, TOKEN, 'op-1')
  await api.cancelPairing(URL_, TOKEN, 'op-1')
  await api.unpairDevice(URL_, TOKEN, 'd1')
  assert.equal((await api.setDevicePaused(URL_, TOKEN, 'd1', true)).paused, true)
  assert.equal((await api.setDevicePaused(URL_, TOKEN, 'd1', false)).paused, false)
  assert.equal((await api.sendTestCard(URL_, TOKEN, 'd1')).id, 77)
  await api.renameSetupDevice(URL_, TOKEN, 'd1', 'Kitchen')
  const rec = await api.startRecovery(URL_, TOKEN, { companion_id: 'comp-1', server_url: 'http://localhost:1180' })
  assert.equal(rec.recovery.id, 'rec-1')
  await api.getRecovery(URL_, TOKEN, 'rec-1')
  await api.getConnectDownloads(URL_, TOKEN)

  const seen = env.calls.map((c) => [c.init.method, c.url.replace(URL_, ''), bodyOf(c)])
  assert.deepEqual(seen, [
    ['POST', '/api/tags/setup-sessions', { operation: 'connect_gateway', server_url: 'http://localhost:1180' }],
    ['GET', '/api/tags/setup-sessions/ses-1', undefined],
    ['POST', '/api/tags/setup-sessions/ses-1/confirm', {}],
    ['DELETE', '/api/tags/setup-sessions/ses-1', undefined],
    ['POST', '/api/tags/discovery', { role: 'tag', setup_code: CODE }],
    ['GET', '/api/tags/discovery/dis-1', undefined],
    ['POST', '/api/tags/pairings', { discovery_id: 'dis-1', candidate_id: 'c1' }],
    ['GET', '/api/tags/pairings/op-1', undefined],
    ['DELETE', '/api/tags/pairings/op-1', undefined],
    ['POST', '/api/tags/devices/d1/unpair', {}],
    ['POST', '/api/tags/devices/d1/pause', {}],
    ['POST', '/api/tags/devices/d1/resume', {}],
    ['POST', '/api/tags/devices/d1/test', {}],
    ['PATCH', '/api/tags/devices/d1', { name: 'Kitchen' }],
    ['POST', '/api/tags/recoveries', { companion_id: 'comp-1', server_url: 'http://localhost:1180' }],
    ['GET', '/api/tags/recoveries/rec-1', undefined],
    ['GET', '/api/tags/connect', undefined],
  ])
  for (const call of env.calls) {
    assert.equal(call.init.headers.Authorization, `Bearer ${TOKEN}`)
    if (call.init.method === 'GET') assert.equal(keyOf(call), undefined, `${call.url} is a read`)
    else assert.match(keyOf(call), UUID, `${call.init.method} ${call.url} carries a key`)
  }
  const keys = env.calls.filter((c) => c.init.method !== 'GET').map(keyOf)
  assert.equal(new Set(keys).size, keys.length, 'separate calls get separate keys')
})

test('ids are path-encoded', async () => {
  env = installBrowser()
  env.route('/api/tags/', () => json({ session: SESSION }))
  await api.getSetupSession(URL_, TOKEN, 'a/b c')
  assert.equal(env.calls[0].url, `${URL_}/api/tags/setup-sessions/a%2Fb%20c`)
})

test('the server URL sent to Connect is the origin of the backend this page talks to', () => {
  env = installBrowser({ href: 'http://192.168.1.20:1515/#/ann/settings/tags' })
  assert.equal(api.setupServerUrl(''), 'http://192.168.1.20:1515')
  assert.equal(api.setupServerUrl('https://cremind.example.com/'), 'https://cremind.example.com')
  assert.equal(api.setupServerUrl('http://localhost:1180'), 'http://localhost:1180')
  // The server refuses a path (422 invalid_server_url): only the origin goes.
  assert.equal(api.setupServerUrl('https://cremind.example.com:8443/app/'), 'https://cremind.example.com:8443')
})

test('a retry after a dropped answer reuses the key; the next action after an answer gets a new one', async () => {
  env = installBrowser()
  const keys = new api.IdempotencyKeys()
  let mode = 'network'
  env.route('/api/tags/discovery', () => {
    if (mode === 'network') throw new TypeError('Failed to fetch')
    if (mode === 'server') return json({ error: 'internal', message: 'boom' }, 503)
    if (mode === 'offline') return refusal(409, 'gateway_offline', 'The gateway is offline.')
    return json({ discovery: DISCOVERY }, 201)
  })
  const body = { role: 'tag', setup_code: CODE }
  const send = () => keys.run('discovery:tag', body, (key) => api.startDiscovery(URL_, TOKEN, body, key))

  await assert.rejects(send(), TypeError) // the answer never came
  mode = 'server'
  await assert.rejects(send(), (e) => e instanceof Error && e.status === 503)
  mode = 'ok'
  assert.equal((await send()).id, 'dis-1')
  const [first, second, third] = env.calls.map(keyOf)
  assert.match(first, UUID)
  assert.equal(second, first, 'retry after a network failure: same key')
  assert.equal(third, first, 'retry after a 503: same key')

  // A new attempt of the same action after it succeeded is a new action.
  mode = 'offline'
  await assert.rejects(send(), (e) => e.code === 'gateway_offline')
  const fourth = keyOf(env.calls[3])
  assert.notEqual(fourth, first)
  // A refusal is an answer too: fixing the cause and trying again must not replay it.
  mode = 'ok'
  await send()
  const fifth = keyOf(env.calls[4])
  assert.notEqual(fifth, fourth)
})

test('a different body (another code) is a different action with a new key', async () => {
  env = installBrowser()
  const keys = new api.IdempotencyKeys()
  env.route('/api/tags/discovery', () => { throw new TypeError('Failed to fetch') })
  const send = (code) => {
    const body = { role: 'tag', setup_code: code }
    return keys.run('discovery:tag', body, (key) => api.startDiscovery(URL_, TOKEN, body, key))
  }
  await assert.rejects(send(CODE), TypeError)
  await assert.rejects(send(CODE), TypeError)
  await assert.rejects(send('48206-0G100-0G40R-40M30-E2096'), TypeError)
  const [a, b, c] = env.calls.map(keyOf)
  assert.equal(a, b)
  assert.notEqual(c, a)
  // The key map never holds a request body (a setup code is a credential).
  assert.ok(!JSON.stringify([...keys.open]).includes(CODE))
})

test('without crypto.randomUUID (plain HTTP on a LAN) keys are still v4 UUIDs', () => {
  const real = globalThis.crypto
  const getRandomValues = real.getRandomValues.bind(real)
  Object.defineProperty(globalThis, 'crypto', { value: { getRandomValues }, configurable: true })
  try {
    const a = api.newIdempotencyKey()
    const b = api.newIdempotencyKey()
    assert.match(a, UUID)
    assert.notEqual(a, b)
  } finally {
    Object.defineProperty(globalThis, 'crypto', { value: real, configurable: true })
  }
})

test('refusals keep their status and code; an unreadable body falls back to the status line', async () => {
  env = installBrowser()
  env.route('/api/tags/connections', () => refusal(403, 'simple_setup_disabled', 'Simple setup is off on this server.'))
  env.route('/api/tags/discovery', () => refusal(422, 'setup_code_wrong_role', 'This is a bridge label, not a tag label.'))
  env.route('/api/tags/setup-sessions/ses-1/confirm', () => refusal(409, 'not_approved', 'Approve it in Cremind Connect first.'))
  env.route('/api/tags/setup-sessions/ses-2/confirm', () => refusal(410, 'session_expired', 'The setup session expired.'))
  env.route('/api/tags/pairings/op-9', () => new Response('<html>bad gateway</html>', { status: 502, statusText: 'Bad Gateway' }))
  const cases = [
    [() => api.getConnections(URL_, TOKEN), 403, 'simple_setup_disabled'],
    [() => api.startDiscovery(URL_, TOKEN, { role: 'tag', setup_code: CODE }), 422, 'setup_code_wrong_role'],
    [() => api.confirmSetupSession(URL_, TOKEN, 'ses-1'), 409, 'not_approved'],
    [() => api.confirmSetupSession(URL_, TOKEN, 'ses-2'), 410, 'session_expired'],
    [() => api.getPairing(URL_, TOKEN, 'op-9'), 502, null],
  ]
  for (const [call, status, code] of cases) {
    await assert.rejects(call(), (e) => {
      assert.equal(e.name, 'TagsApiError')
      assert.equal(e.status, status)
      assert.equal(e.code, code)
      return true
    })
  }
  assert.equal(api.outcomeUnknown(new TypeError('Failed to fetch')), true)
})
