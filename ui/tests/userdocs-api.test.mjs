// The User Document Search client: the confirm round trip and the error shape.
//
// Destructive changes are two-step on the server. The first request answers
// `409 ConfirmationRequired` with a plan and a token bound to exactly that
// change; repeating the SAME request with the token applies it. If the change
// grew meanwhile the server answers with a new plan and token instead. The page
// shows each plan in a dialog, so the client must hand back the plan and a
// `confirm()` rather than a flattened error — and `confirm()` must resend the
// identical body plus the token, or the server just asks again.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, json, load } from './harness.mjs'

let env = installBrowser()
const api = await load('src/services/userdocsApi.ts')

const URL_ = 'http://localhost:1515'
const TOKEN = 'jwt-ann'

const PLAN = {
  effects: [{ kind: 'purge_all', files: 1300, bytes: 1024, detail: {} }],
  destructive: true,
}
const SAVED = {
  settings: { local: { enabled: false } },
  snapshot: { v: 1, boot: 'b', seq: 9, enabled: false, state: 'disabled', reason: null },
}

function bodyOf(call) {
  return JSON.parse(call.init.body)
}

test('a plain save resolves to the result, sent once', async () => {
  env = installBrowser()
  env.route('/api/userdocs/settings', () => json(SAVED))
  const outcome = await api.saveUserDocsSettings(URL_, TOKEN, { kind: 'local', enabled: false })
  assert.equal(outcome.kind, 'done')
  assert.deepEqual(outcome.result, SAVED)
  const calls = env.callsTo('/api/userdocs/settings')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].init.method, 'PUT')
  assert.equal(calls[0].init.headers.Authorization, `Bearer ${TOKEN}`)
  assert.deepEqual(bodyOf(calls[0]), { kind: 'local', enabled: false })
})

test('ConfirmationRequired comes back as the plan, and confirm() resends the same body with the token', async () => {
  env = installBrowser()
  env.route('/api/userdocs/settings', (_url, init) => {
    const body = JSON.parse(init.body)
    if (body.confirm === 'tok-1') return json(SAVED)
    return json({
      error: 'ConfirmationRequired',
      message: 'This change removes indexed content. Review the plan and confirm.',
      plan: PLAN,
      confirm: 'tok-1',
    }, 409)
  })

  const patch = { kind: 'local', enabled: false, delete_index: true }
  const first = await api.saveUserDocsSettings(URL_, TOKEN, patch)
  assert.equal(first.kind, 'confirm')
  assert.deepEqual(first.plan, PLAN)
  assert.equal(first.token, 'tok-1')
  assert.match(first.message, /Review the plan/)

  const second = await first.confirm()
  assert.equal(second.kind, 'done')
  assert.deepEqual(second.result, SAVED)

  const bodies = env.callsTo('/api/userdocs/settings').map(bodyOf)
  assert.deepEqual(bodies, [patch, { ...patch, confirm: 'tok-1' }])
})

test('a plan that grew while the dialog was open is asked about again', async () => {
  env = installBrowser()
  let round = 0
  env.route('/api/userdocs/settings', (_url, init) => {
    const body = JSON.parse(init.body)
    round += 1
    if (body.confirm === 'tok-2') return json(SAVED)
    const grown = { ...PLAN, effects: [{ ...PLAN.effects[0], files: round === 1 ? 10 : 5000 }] }
    return json({ error: 'ConfirmationRequired', message: 'Confirm.', plan: grown, confirm: `tok-${round}` }, 409)
  })

  const first = await api.saveUserDocsSettings(URL_, TOKEN, { kind: 'local', excludes: [] })
  assert.equal(first.plan.effects[0].files, 10)
  const second = await first.confirm()
  assert.equal(second.kind, 'confirm')
  assert.equal(second.plan.effects[0].files, 5000)
  const third = await second.confirm()
  assert.equal(third.kind, 'done')
  assert.deepEqual(
    env.callsTo('/api/userdocs/settings').map(c => bodyOf(c).confirm),
    [undefined, 'tok-1', 'tok-2'],
  )
})

test('rebuild goes through the same round trip on /control', async () => {
  env = installBrowser()
  env.route('/api/userdocs/control', (_url, init) => {
    const body = JSON.parse(init.body)
    if (body.confirm === 'rb') return json({ accepted: true, snapshot: SAVED.snapshot }, 202)
    return json({
      error: 'ConfirmationRequired',
      message: 'Rebuilding re-embeds the whole index. Review and confirm.',
      plan: { effects: [{ kind: 'reembed_all', files: 3, bytes: 0, detail: { chunks: 40 } }], destructive: true },
      confirm: 'rb',
    }, 409)
  })
  const first = await api.runUserDocsControl(URL_, TOKEN, { action: 'rebuild', reextract: true })
  assert.equal(first.kind, 'confirm')
  const done = await first.confirm()
  assert.equal(done.kind, 'done')
  assert.equal(done.result.accepted, true)
  assert.deepEqual(
    env.callsTo('/api/userdocs/control').map(bodyOf),
    [{ action: 'rebuild', reextract: true }, { action: 'rebuild', reextract: true, confirm: 'rb' }],
  )
})

test('any other error is thrown with the server structure kept', async () => {
  env = installBrowser()
  env.route('/api/userdocs/settings', () => json({
    error: 'ValidationFailed', details: { root_path: 'That folder does not exist.' }, code: 'not_found',
  }, 400))
  const err = await api.saveUserDocsSettings(URL_, TOKEN, { kind: 'local', root_mode: 'custom', root_path: '/x' })
    .then(() => null, e => e)
  assert.ok(err instanceof api.UserDocsApiError)
  assert.equal(err.status, 400)
  assert.equal(err.code, 'ValidationFailed')
  assert.deepEqual(err.details, { root_path: 'That folder does not exist.' })
  assert.equal(err.message, 'That folder does not exist.')
})

test('FeatureNotInstalled from the admin gate carries what to install', async () => {
  env = installBrowser()
  const missing = [{ feature_key: 'userdocs', extras: ['documents', 'userdocs'], requires_restart_after_install: false }]
  env.route('/api/userdocs/admin', () => json({
    error: 'FeatureNotInstalled', missing, message: 'User Document Search requires optional dependencies…',
  }, 409))
  const err = await api.putUserDocsAdmin(URL_, TOKEN, { allowed: true }).then(() => null, e => e)
  assert.equal(err.code, 'FeatureNotInstalled')
  assert.deepEqual(err.missing, missing)
  assert.deepEqual(bodyOf(env.callsTo('/api/userdocs/admin')[0]), { policy: { allowed: true } })
})

test('an engine that is not running is recognisable', async () => {
  env = installBrowser()
  env.route('/api/userdocs/files', () => json({ error: 'EngineNotRunning', message: 'User Document Search is not running on this server.' }, 503))
  const err = await api.listUserDocsFiles(URL_, TOKEN).then(() => null, e => e)
  assert.equal(err.status, 503)
  assert.equal(err.code, 'EngineNotRunning')
})

test('file listing sends the keyset cursor and filters as query parameters', async () => {
  env = installBrowser()
  env.route('/api/userdocs/files', () => json({ files: [], next: null, counts: {} }))
  await api.listUserDocsFiles(URL_, TOKEN, {
    status: 'error', q: 'report', after: { rel_path: 'a/b.docx', id: 42 }, limit: 100,
  })
  const url = new URL(env.callsTo('/api/userdocs/files')[0].url)
  assert.equal(url.searchParams.get('status'), 'error')
  assert.equal(url.searchParams.get('q'), 'report')
  assert.equal(url.searchParams.get('after_path'), 'a/b.docx')
  assert.equal(url.searchParams.get('after_id'), '42')
  assert.equal(url.searchParams.get('limit'), '100')
})

test('validate-root asks about the inherited root without a path', async () => {
  env = installBrowser()
  env.route('/api/userdocs/validate-root', () => json({ ok: true, path: '/home/ann', code: null, message: null, locked_excludes: [] }))
  await api.validateUserDocsRoot(URL_, TOKEN, null)
  await api.validateUserDocsRoot(URL_, TOKEN, '/home/ann/Docs')
  assert.deepEqual(
    env.callsTo('/api/userdocs/validate-root').map(bodyOf),
    [{ root_mode: 'inherit' }, { path: '/home/ann/Docs' }],
  )
})

test('isStaleSnapshot orders by boot and seq only', () => {
  const held = { boot: 'a', seq: 5 }
  assert.equal(api.isStaleSnapshot(held, { boot: 'a', seq: 4 }), true)
  assert.equal(api.isStaleSnapshot(held, { boot: 'a', seq: 5 }), true)
  assert.equal(api.isStaleSnapshot(held, { boot: 'a', seq: 6 }), false)
  assert.equal(api.isStaleSnapshot(held, { boot: 'b', seq: 1 }), false)
  assert.equal(api.isStaleSnapshot(null, { boot: 'a', seq: 1 }), false)
  assert.equal(api.isStaleSnapshot(held, {}), false)
})
