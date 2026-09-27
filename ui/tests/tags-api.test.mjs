// The Cremind Tag client: methods, paths, auth, bodies, and the error shape.
//
// Pages branch on the server's error code — `otp_refused` is shown inside the
// Display dialog, `clear_pending` explains the screen is still being blanked,
// `bridge_required` lands on the claim dialog's bridge field — so every non-2xx
// must surface as a TagsApiError that keeps `status`, `code` and the server's
// sentence. The preview is a PNG behind the Bearer header: fetched as a blob,
// with "nothing stored yet" (404 `no_preview`) answered as null, not an error.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, json, load } from './harness.mjs'

let env = installBrowser()
const api = await load('src/services/tagsApi.ts')

const URL_ = 'http://localhost:1180'
const TOKEN = 'jwt-ann'

const DEVICE = {
  id: 'dev-1', companion_id: 'comp-1', kind: 'tag', hw_id: '1A2B3C4D', name: 'Desk',
  owner_profile: 'ann', bridge_device_id: 'br-1', epoch: 3, rotation: 0, board: 16, panel: 1,
  width: 400, height: 300, planes: 1, fw: '0.1.0', info: {}, status: 'ok', battery_mv: 2900,
  rssi: -61, last_contact_at: 1_790_000_000_000, desired_revision: 18, displayed_revision: 17,
  displayed_digest: 'abcd1234', clear_required: false, claimed_at: 1, created_at: 1, updated_at: 2,
}
const DELIVERY = {
  id: 501, seq: 9, profile: 'ann', device_id: 'dev-1', companion_id: 'comp-1', epoch: 3,
  event_id: null, kind: 'pinned_note', priority: 55, replace_key: 'pinned:dev-1', resolves: null,
  card: { v: 1, kind: 'pinned_note', title: 'Back at 3' }, stage: 'queued', terminal: false,
  outcome: null, status_code: null, revision: null, digest: null, detail: null, timing: null,
  stage_times: {}, created_at: 10, updated_at: 10, expires_at: 20, finished_at: null,
}

function bodyOf(call) {
  return call.init.body === undefined ? undefined : JSON.parse(call.init.body)
}

function only(path) {
  const calls = env.callsTo(path)
  assert.equal(calls.length, 1, `one call to ${path}`)
  return calls[0]
}

function refusal(status, error, message, extra = {}) {
  return json({ error, message, detail: message, ...extra }, status)
}

test('the overview is a GET with the Bearer token', async () => {
  env = installBrowser()
  env.route('/api/tags', () => json({ profile: 'ann', enabled: true, devices: [DEVICE], counts: { devices: 1 } }))
  const ov = await api.getTagsOverview(URL_, TOKEN)
  assert.equal(ov.devices[0].id, 'dev-1')
  const call = only('/api/tags')
  assert.equal(call.url, `${URL_}/api/tags`)
  assert.equal(call.init.method, 'GET')
  assert.equal(call.init.headers.Authorization, `Bearer ${TOKEN}`)
  assert.equal(call.init.body, undefined)
})

test('a relative agent URL resolves against the page origin', async () => {
  env = installBrowser({ href: 'http://localhost:1515/#/ann/tags' })
  env.route('/api/tags/settings', () => json({ profile: 'ann', icons: [] }))
  await api.getTagSettings('', TOKEN)
  assert.equal(only('/api/tags/settings').url, 'http://localhost:1515/api/tags/settings')
})

test('settings: the switch sends only `enabled`, the form sends only `options`', async () => {
  env = installBrowser()
  env.route('/api/tags/settings', (_url, init) => json({ profile: 'ann', ...JSON.parse(init.body) }))
  await api.saveTagSettings(URL_, TOKEN, { enabled: true })
  await api.saveTagSettings(URL_, TOKEN, { options: { show_excerpts: true, routes: { usage: 'all' } } })
  const calls = env.callsTo('/api/tags/settings')
  assert.deepEqual(calls.map((c) => c.init.method), ['PUT', 'PUT'])
  assert.deepEqual(calls.map(bodyOf), [
    { enabled: true },
    { options: { show_excerpts: true, routes: { usage: 'all' } } },
  ])
})

test('invalid_settings keeps the field details', async () => {
  env = installBrowser()
  env.route('/api/tags/settings', () => refusal(422, 'invalid_settings',
    'progress_cadence_s: must be a number of seconds between 60 and 3600',
    { details: { progress_cadence_s: 'must be a number of seconds between 60 and 3600' } }))
  await assert.rejects(api.saveTagSettings(URL_, TOKEN, { options: { progress_cadence_s: 5 } }), (e) => {
    assert.ok(e instanceof api.TagsApiError)
    assert.equal(e.status, 422)
    assert.equal(e.code, 'invalid_settings')
    assert.deepEqual(e.details, { progress_cadence_s: 'must be a number of seconds between 60 and 3600' })
    assert.match(e.message, /between 60 and 3600/)
    return true
  })
})

test('display posts the note and returns the delivery', async () => {
  env = installBrowser()
  env.route('/api/tags/devices/dev-1/display', () => json({ delivery: DELIVERY }, 201))
  const note = { title: 'Back at 3', body: 'Coffee', icon: 'push_pin', ttl_s: 3600 }
  const delivery = await api.displayOnTag(URL_, TOKEN, 'dev-1', note)
  assert.equal(delivery.id, 501)
  const call = only('/api/tags/devices/dev-1/display')
  assert.equal(call.init.method, 'POST')
  assert.equal(call.init.headers.Authorization, `Bearer ${TOKEN}`)
  assert.equal(call.init.headers['Content-Type'], 'application/json')
  assert.deepEqual(bodyOf(call), note)
})

test('display: otp_refused (422) and clear_pending (409) surface with their codes', async () => {
  env = installBrowser()
  env.route('/api/tags/devices/dev-1/display', (_url, init) => {
    const body = JSON.parse(init.body)
    if (/code/.test(body.title)) {
      return refusal(422, 'otp_refused', 'The text looks like it contains a one-time code; codes are never shown on a tag.')
    }
    return refusal(409, 'clear_pending', "The tag's screen is still being cleared after a change of owner; try again shortly.")
  })
  await assert.rejects(api.displayOnTag(URL_, TOKEN, 'dev-1', { title: 'Your code is 482913' }), (e) => {
    assert.ok(e instanceof api.TagsApiError)
    assert.equal(e.status, 422)
    assert.equal(e.code, 'otp_refused')
    assert.equal(api.tagsErrorCode(e), 'otp_refused')
    assert.match(e.message, /one-time code/)
    return true
  })
  await assert.rejects(api.displayOnTag(URL_, TOKEN, 'dev-1', { title: 'Hello' }), (e) => {
    assert.equal(e.status, 409)
    assert.equal(e.code, 'clear_pending')
    return true
  })
  assert.equal(api.tagsErrorCode(new Error('x')), null)
})

test('clear, refresh and identify are POSTs on the device', async () => {
  env = installBrowser()
  env.route('/api/tags/devices/dev-1/clear', () => json({ delivery: { ...DELIVERY, kind: 'clear' } }, 201))
  env.route('/api/tags/devices/dev-1/refresh', () => json({ command: { id: 'c1', kind: 'refresh_tag' } }, 202))
  env.route('/api/tags/devices/dev-1/identify', () => json({ command: { id: 'c2', kind: 'identify' } }, 202))
  assert.equal((await api.clearTag(URL_, TOKEN, 'dev-1')).kind, 'clear')
  assert.equal((await api.refreshTag(URL_, TOKEN, 'dev-1')).kind, 'refresh_tag')
  assert.equal((await api.identifyTag(URL_, TOKEN, 'dev-1')).kind, 'identify')
  for (const path of ['clear', 'refresh', 'identify']) {
    assert.equal(only(`/api/tags/devices/dev-1/${path}`).init.method, 'POST')
  }
})

test('rename PATCHes the name; ids are path-encoded', async () => {
  env = installBrowser()
  env.route('/api/tags/devices/', () => json({ device: { ...DEVICE, name: 'Kitchen' } }))
  const device = await api.renameTagDevice(URL_, TOKEN, 'dev/1 x', 'Kitchen')
  assert.equal(device.name, 'Kitchen')
  const call = env.calls[0]
  assert.equal(call.url, `${URL_}/api/tags/devices/dev%2F1%20x`)
  assert.equal(call.init.method, 'PATCH')
  assert.deepEqual(bodyOf(call), { name: 'Kitchen' })
})

test('delivery history pages with device/state/limit/before and omits what is unset', async () => {
  env = installBrowser()
  env.route('/api/tags/deliveries', () => json({ deliveries: [DELIVERY], next_before: 501 }))
  const page = await api.listTagDeliveries(URL_, TOKEN, { device: 'dev-1', state: 'active', limit: 20, before: 700 })
  assert.equal(page.next_before, 501)
  await api.listTagDeliveries(URL_, TOKEN, { device: 'dev-1', before: null })
  await api.listTagDeliveries(URL_, TOKEN)
  const urls = env.callsTo('/api/tags/deliveries').map((c) => c.url)
  assert.deepEqual(urls, [
    `${URL_}/api/tags/deliveries?device=dev-1&state=active&limit=20&before=700`,
    `${URL_}/api/tags/deliveries?device=dev-1`,
    `${URL_}/api/tags/deliveries`,
  ])
})

test('cancel: 409 already_terminal keeps the finished delivery in the body', async () => {
  env = installBrowser()
  const finished = { ...DELIVERY, stage: 'displayed', terminal: true, outcome: 'displayed' }
  env.route('/api/tags/deliveries/501/cancel', () => refusal(409, 'already_terminal',
    'The delivery already finished (displayed).', { delivery: finished }))
  await assert.rejects(api.cancelTagDelivery(URL_, TOKEN, 501), (e) => {
    assert.equal(e.code, 'already_terminal')
    assert.equal(e.status, 409)
    assert.deepEqual(e.body.delivery, finished)
    return true
  })
  assert.equal(only('/api/tags/deliveries/501/cancel').init.method, 'POST')
})

test('an error without a JSON body falls back to the status line', async () => {
  env = installBrowser()
  env.route('/api/tags/deliveries/9', () => new Response('<html>boom</html>', { status: 502, statusText: 'Bad Gateway' }))
  await assert.rejects(api.getTagDelivery(URL_, TOKEN, 9), (e) => {
    assert.ok(e instanceof api.TagsApiError)
    assert.equal(e.status, 502)
    assert.equal(e.code, null)
    assert.equal(e.message, 'Request failed: 502 Bad Gateway')
    return true
  })
})

test('the preview is fetched as a blob with the Bearer token and its revision', async () => {
  env = installBrowser()
  const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3])
  env.route('/api/tags/devices/dev-1/preview', () => new Response(png, {
    status: 200, headers: { 'Content-Type': 'image/png', 'X-Tag-Revision': '18' },
  }))
  const preview = await api.fetchTagPreview(URL_, TOKEN, 'dev-1', 'desired')
  assert.equal(preview.revision, 18)
  assert.equal(preview.blob.type, 'image/png')
  assert.deepEqual(new Uint8Array(await preview.blob.arrayBuffer()), png)
  const call = only('/api/tags/devices/dev-1/preview')
  assert.equal(call.url, `${URL_}/api/tags/devices/dev-1/preview?kind=desired`)
  assert.equal(call.init.headers.Authorization, `Bearer ${TOKEN}`)
})

test('preview: an unreadable revision header is null; no_preview is null; other 404s throw', async () => {
  env = installBrowser()
  env.route('/api/tags/devices/a/preview', () => new Response(new Uint8Array([1]), { status: 200 }))
  env.route('/api/tags/devices/b/preview', () => refusal(404, 'no_preview', 'The companion has not sent a displayed preview yet.'))
  env.route('/api/tags/devices/c/preview', () => refusal(404, 'device_not_found', 'No tag with that id.'))
  assert.equal((await api.fetchTagPreview(URL_, TOKEN, 'a', 'displayed')).revision, null)
  assert.equal(await api.fetchTagPreview(URL_, TOKEN, 'b', 'displayed'), null)
  await assert.rejects(api.fetchTagPreview(URL_, TOKEN, 'c', 'displayed'), (e) => {
    assert.equal(e.code, 'device_not_found')
    assert.equal(e.status, 404)
    return true
  })
})

test('content credentials: create sends companion + label, revoke is a DELETE', async () => {
  env = installBrowser()
  const cred = { id: 'tagc_x', companion_id: 'comp-1', kind: 'content', label: 'Desk', revoked: false }
  env.route('/api/tags/credentials', (_url, init) => {
    if (init.method === 'POST') {
      return json({ credential: cred, secret: 's3cret', authorization: 'CremindTag tagc_x.s3cret' }, 201)
    }
    if (init.method === 'DELETE') return json({ credential: { ...cred, revoked: true } })
    return json({ credentials: [cred] })
  })
  const created = await api.createContentCredential(URL_, TOKEN, { companion_id: 'comp-1', label: 'Desk' })
  assert.equal(created.secret, 's3cret')
  assert.equal(created.authorization, 'CremindTag tagc_x.s3cret')
  assert.equal((await api.revokeContentCredential(URL_, TOKEN, 'tagc_x')).revoked, true)
  assert.equal((await api.listContentCredentials(URL_, TOKEN)).length, 1)
  const calls = env.callsTo('/api/tags/credentials')
  assert.deepEqual(calls.map((c) => [c.init.method, c.url.replace(URL_, '')]), [
    ['POST', '/api/tags/credentials'],
    ['DELETE', '/api/tags/credentials/tagc_x'],
    ['GET', '/api/tags/credentials'],
  ])
  assert.deepEqual(bodyOf(calls[0]), { companion_id: 'comp-1', label: 'Desk' })
})

test('companions list unwraps the array', async () => {
  env = installBrowser()
  env.route('/api/tags/companions', () => json({ companions: [{ id: 'comp-1', name: 'Desk PC', online: true }] }))
  const list = await api.listTagCompanions(URL_, TOKEN)
  assert.deepEqual(list.map((c) => c.name), ['Desk PC'])
})

test('admin: claim sends owner/bridge/name, and bridge_required / unknown_profile keep their codes', async () => {
  env = installBrowser()
  env.route('/api/tags/hardware/tags/dev-1/claim', (_url, init) => {
    const body = JSON.parse(init.body)
    if (body.owner === 'ghost') return refusal(422, 'unknown_profile', "No profile named 'ghost'.")
    if (!body.bridge_id) return refusal(409, 'bridge_required', "Name the bridge to assign the tag to ('bridge_id'): the companion has 2 bridges.")
    return json({ device: { ...DEVICE, owner_profile: body.owner }, commands: [{ kind: 'assign_tag' }, { kind: 'clear_tag' }] })
  })
  await assert.rejects(api.claimTag(URL_, TOKEN, 'dev-1', { owner: 'ann' }), (e) => {
    assert.equal(e.status, 409)
    assert.equal(e.code, 'bridge_required')
    return true
  })
  await assert.rejects(api.claimTag(URL_, TOKEN, 'dev-1', { owner: 'ghost', bridge_id: 'br-1' }), (e) => {
    assert.equal(e.status, 422)
    assert.equal(e.code, 'unknown_profile')
    return true
  })
  const ok = await api.claimTag(URL_, TOKEN, 'dev-1', { owner: 'bob', bridge_id: 'br-1', name: 'Kitchen' })
  assert.equal(ok.device.owner_profile, 'bob')
  const bodies = env.callsTo('/claim').map(bodyOf)
  assert.deepEqual(bodies[2], { owner: 'bob', bridge_id: 'br-1', name: 'Kitchen' })
  assert.ok(env.callsTo('/claim').every((c) => c.init.method === 'POST'))
})

test('admin: assign, release, rename, forget (409 tag_owned) and commands', async () => {
  env = installBrowser()
  env.route('/api/tags/hardware/tags/dev-1/assign', () => json({ device: DEVICE, command: { kind: 'assign_tag' } }))
  env.route('/api/tags/hardware/tags/dev-1/release', () => json({ device: { ...DEVICE, owner_profile: null }, commands: [] }))
  env.route('/api/tags/hardware/devices/dev-1', (_url, init) => (init.method === 'DELETE'
    ? refusal(409, 'tag_owned', "The tag is owned by 'ann'; release it first.", { device: DEVICE })
    : json({ device: { ...DEVICE, name: 'Hall' } })))
  env.route('/api/tags/hardware/commands', (_url, init) => (init.method === 'POST'
    ? json({ command: { id: 'cmd-1', ...JSON.parse(init.body), status: 'queued' } }, 202)
    : json({ command: { id: 'cmd-1', status: 'succeeded', result: { devices: [] } } })))

  await api.assignTagBridge(URL_, TOKEN, 'dev-1', 'br-2')
  await api.releaseTag(URL_, TOKEN, 'dev-1')
  assert.equal((await api.renameHardwareDevice(URL_, TOKEN, 'dev-1', 'Hall')).name, 'Hall')
  await assert.rejects(api.forgetHardwareDevice(URL_, TOKEN, 'dev-1'), (e) => {
    assert.equal(e.code, 'tag_owned')
    assert.equal(e.status, 409)
    return true
  })
  const cmd = await api.queueTagCommand(URL_, TOKEN, { companion_id: 'comp-1', kind: 'scan_unprovisioned', args: { duration_s: 30 } })
  assert.equal(cmd.status, 'queued')
  assert.equal((await api.getTagCommand(URL_, TOKEN, 'cmd-1')).status, 'succeeded')

  assert.deepEqual(bodyOf(only('/assign')), { bridge_id: 'br-2' })
  assert.equal(only('/release').init.method, 'POST')
  const deviceCalls = env.callsTo('/api/tags/hardware/devices/dev-1')
  assert.deepEqual(deviceCalls.map((c) => c.init.method), ['PATCH', 'DELETE'])
  assert.deepEqual(bodyOf(deviceCalls[0]), { name: 'Hall' })
  const cmdCalls = env.callsTo('/api/tags/hardware/commands')
  assert.deepEqual(bodyOf(cmdCalls[0]), { companion_id: 'comp-1', kind: 'scan_unprovisioned', args: { duration_s: 30 } })
  assert.equal(cmdCalls[1].url, `${URL_}/api/tags/hardware/commands/cmd-1`)
})

test('admin: register / rotate / delete companions, and the defaults are PUT whole', async () => {
  env = installBrowser()
  env.route('/api/tags/hardware/companions', () => json({
    companion: { id: 'comp-2', name: 'Lab' },
    credential: { id: 'tagc_h' }, secret: 'hw', authorization: 'CremindTag tagc_h.hw',
  }, 201))
  // Later routes win: the delete path is a prefix of the rotate path.
  env.route('/api/tags/hardware/companions/comp-2', () => json({ deleted: true }))
  env.route('/api/tags/hardware/companions/comp-2/rotate', () => json({
    credential: { id: 'tagc_h2' }, secret: 'hw2', authorization: 'CremindTag tagc_h2.hw2', revoked: ['tagc_h'],
  }))
  env.route('/api/tags/hardware/defaults', (_url, init) => json({
    defaults: init.body ? JSON.parse(init.body).defaults : {}, builtin: { layout: 'status' },
  }))

  const reg = await api.registerTagCompanion(URL_, TOKEN, 'Lab')
  assert.equal(reg.authorization, 'CremindTag tagc_h.hw')
  const rot = await api.rotateTagCompanion(URL_, TOKEN, 'comp-2')
  assert.deepEqual(rot.revoked, ['tagc_h'])
  await api.deleteTagCompanion(URL_, TOKEN, 'comp-2')
  const saved = await api.saveTagDefaults(URL_, TOKEN, { language: 'vi', routes: { usage: 'all' } })
  assert.deepEqual(saved.defaults, { language: 'vi', routes: { usage: 'all' } })
  await api.getTagDefaults(URL_, TOKEN)

  assert.deepEqual(bodyOf(env.callsTo('/api/tags/hardware/companions')[0]), { name: 'Lab' })
  assert.equal(only('/rotate').init.method, 'POST')
  const del = env.calls.find((c) => c.init.method === 'DELETE')
  assert.equal(del.url, `${URL_}/api/tags/hardware/companions/comp-2`)
  const defaults = env.callsTo('/api/tags/hardware/defaults')
  assert.deepEqual(defaults.map((c) => c.init.method), ['PUT', 'GET'])
  assert.deepEqual(bodyOf(defaults[0]), { defaults: { language: 'vi', routes: { usage: 'all' } } })
})

test('a 403 from a hardware route for a non-admin keeps its status', async () => {
  env = installBrowser()
  env.route('/api/tags/hardware', () => json({ error: 'Admin profile required' }, 403))
  await assert.rejects(api.getTagHardware(URL_, TOKEN), (e) => {
    assert.equal(e.status, 403)
    assert.equal(e.message, 'Admin profile required')
    return true
  })
})
