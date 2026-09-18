// What `completeSetup` throws, and why the shape matters.
//
// A setup run takes tens of seconds, and the server finishes it whether or not
// the client is still listening. So the wizard has to tell three failures apart:
//
//   - a transport failure (no response at all) — the outcome is unknown, and the
//     profile may well have been created, so the wizard probes and offers to
//     adopt rather than showing a dead toast;
//   - 409 `setup_in_progress` — the original run is still going, so the adopt
//     retry must wait rather than give up;
//   - anything else the server answered — authoritative, surface it as-is.
//
// The first is a bare TypeError from fetch. The other two are only separable if
// the server's `code` survives onto the thrown error, which is what this pins.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, json, load } from './harness.mjs'

let env = installBrowser()
const { completeSetup } = await load('src/services/configApi.ts')

const SETUP = '/api/config/setup'
const CONFIG = { profile: 'javis' }

/** Point every test at the current env's routes. */
function route(respond) {
  env.route(SETUP, respond)
}

async function submit() {
  return completeSetup('http://localhost:1515', CONFIG, 'admin-jwt')
}

test('a still-running setup is identifiable by its code', async () => {
  route(() => json(
    { error: "Setup for profile 'javis' is already running.", code: 'setup_in_progress' },
    409,
  ))

  const err = await submit().then(() => null, e => e)
  assert.equal(err.code, 'setup_in_progress')
  assert.equal(err.status, 409)
})

test('a taken name is a 409 too, but carries no such code', async () => {
  // Same status, opposite meaning: waiting would never resolve this one.
  route(() => json({ error: "Profile 'javis' already exists" }, 409))

  const err = await submit().then(() => null, e => e)
  assert.equal(err.code, undefined)
  assert.equal(err.status, 409)
  assert.match(err.message, /already exists/)
})

test('the server message is preserved for the user', async () => {
  route(() => json({ error: 'Admin profile required' }, 403))

  const err = await submit().then(() => null, e => e)
  assert.equal(err.message, 'Admin profile required')
  assert.equal(err.status, 403)
})

test('a dropped connection throws a TypeError, not a server error', async () => {
  // A fresh env so no route from an earlier test answers: the harness rejects
  // unrouted requests the way a refused connection does. This is the case the
  // wizard treats as "outcome unknown" and probes rather than reporting.
  env = installBrowser()
  try {
    const err = await submit().then(() => null, e => e)

    assert.ok(err instanceof TypeError, `expected TypeError, got ${err?.constructor?.name}`)
    assert.equal(err.status, undefined)
  } finally {
    env = installBrowser()
  }
})

test('a success returns the parsed body', async () => {
  route(() => json({ success: true, token: 'jwt-for-javis', profile: 'javis' }))

  assert.equal((await submit()).token, 'jwt-for-javis')
})
