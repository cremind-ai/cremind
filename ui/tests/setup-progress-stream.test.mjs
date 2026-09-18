// The Setup Wizard's live progress stream, and the header it must carry.
//
// `GET /api/config/setup/stream` is open during the first-run bootstrap window
// (no JWT can exist yet) and admin-only once setup is complete — see
// app/api/setup_stream.py. This client sent no Authorization header at all, so
// every profile created *after* the first got a 401 and the wizard's live-log
// panel stayed empty for the whole ~70s run. Nothing surfaced it, because the
// one call site passed no error handler either.
//
// So two things are pinned here: the token reaches the request, and a refused
// stream is reported rather than swallowed.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, load, until } from './harness.mjs'

const env = installBrowser()
const { openSetupProgressStream } = await load('src/services/setupProgressStream.ts')

const STREAM = '/api/config/setup/stream'

/** A response whose body streams `frames` and then ends. */
function sse(...frames) {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      controller.close()
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function lastStreamCall() {
  return [...env.calls].reverse().find(call => call.url.includes(STREAM))
}

test('the admin token is sent, so a post-setup stream is not refused', async () => {
  env.route(STREAM, () => sse('event: ready\ndata: {}\n\n'))

  const handle = openSetupProgressStream('http://localhost:1515', 'admin-jwt', () => {})
  await until(() => lastStreamCall(), 'the stream request')
  handle.close()

  assert.equal(lastStreamCall().init.headers.Authorization, 'Bearer admin-jwt')
})

test('no header during the bootstrap window, where no token can exist', async () => {
  env.route(STREAM, () => sse('event: ready\ndata: {}\n\n'))

  const handle = openSetupProgressStream('http://localhost:1515', '', () => {})
  await until(() => lastStreamCall(), 'the stream request')
  handle.close()

  // Absent, not empty: `Bearer ` would be a malformed credential.
  assert.equal('Authorization' in lastStreamCall().init.headers, false)
})

test('log frames reach the caller', async () => {
  env.route(STREAM, () => sse(
    'event: ready\ndata: {}\n\n',
    'event: log\ndata: {"step":"tool_configs","message":"Configuring exec_shell…",'
      + '"level":"info","ts":1}\n\n',
  ))

  const seen = []
  const handle = openSetupProgressStream('http://localhost:1515', 'admin-jwt', e => seen.push(e))
  await until(() => seen.length > 0, 'a log frame')
  handle.close()

  assert.equal(seen[0].message, 'Configuring exec_shell…')
  assert.equal(seen[0].step, 'tool_configs')
})

test('a 401 is reported, not swallowed', async () => {
  env.route(STREAM, () => new Response('{"error":"Unauthenticated"}', { status: 401 }))

  const errors = []
  const handle = openSetupProgressStream(
    'http://localhost:1515', '', () => {}, err => errors.push(err),
  )
  await until(() => errors.length > 0, 'the error callback')
  handle.close()

  assert.match(String(errors[0].message), /401/)
})

test('closing before the response arrives stays silent', async () => {
  // The wizard closes the stream in a `finally` as soon as the POST returns;
  // that abort is routine and must not surface as a failure.
  env.route(STREAM, () => sse('event: ready\ndata: {}\n\n'))

  const errors = []
  const handle = openSetupProgressStream(
    'http://localhost:1515', 'admin-jwt', () => {}, err => errors.push(err),
  )
  handle.close()
  await until(() => lastStreamCall(), 'the stream request')

  assert.deepEqual(errors, [])
})
