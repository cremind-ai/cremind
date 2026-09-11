// Background-tab migration (httpsTransition) and the Settings/setup pivot
// (useHttpsPivot) against a scripted server. The regressions pinned here:
//   - a replaced pod renews the leaf under the same CA and the tab still moves,
//     carrying its handoff ticket (or, without one, a login keeping the route);
//   - a probe that resolves after the switch was cancelled never navigates;
//   - interrupted and hanging probes are retried / abandoned, not fatal;
//   - an activation whose 202 was lost to the restart still completes, and
//     reports only what is true once the boundary moved (no cancel, no runbook);
//   - Electron keeps waiting (with its reason) instead of flapping to failed;
//   - a pivot on another address than the one that prepared the switch probes,
//     links and mints tickets for *this* browser's address;
//   - a prepared/quiescing switch never sticks behind the blocking overlay
//     because one status read timed out;
//   - Settings' capabilities read is bounded, so the page reaches its recovery
//     view even when that request never answers.
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CA, LEAF_B, TICKET, advance, deferred, flush, installBrowser, json, load, seedTicket,
  status, transition, until,
} from './harness.mjs'

const TARGET_STATUS = 'https://localhost:1515/api/tls/status'
const SOURCE_STATUS = 'http://localhost:1515/api/tls/status'
const HANDOFF_URL = `https://localhost:1515/#/tls-handoff?ticket=${TICKET}`
const LOGIN_URL = 'https://localhost:1515/#/login/alice?redirect=%2Falice%2Fc%2F42'
// A laptop reaching the server by its LAN address, for a switch the fixtures
// prepare from the server's own localhost.
const LAN = 'http://192.168.1.5:1515'
const LAN_SECURE = 'https://192.168.1.5:1515'
const LAN_TARGET_STATUS = `${LAN_SECURE}/api/tls/status`
const LAN_HANDOFF_URL = `${LAN_SECURE}/#/tls-handoff?ticket=${TICKET}`
const LAN_LOGIN_URL = `${LAN_SECURE}/#/login/alice?redirect=%2Falice%2Fc%2F42`

function fakeTime(t) {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: 1_800_000_000_000 })
  return t.mock.timers
}

// ── background tabs ───────────────────────────────────────────────────────

test('pod replacement: a renewed leaf under the same CA moves the tab with its ticket', async () => {
  const env = installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  const pinned = transition()
  seedTicket(pinned.id)
  env.route(TARGET_STATUS, () => json(status({ certificate_sha256: LEAF_B })))

  await mod.handleHttpsTransition(pinned, 'http://localhost:1515', '')

  assert.deepEqual(env.replaced, [HANDOFF_URL])
  assert.equal(mod.httpsTransitionState.phase.value, 'moving')
})

test('without a live ticket the tab signs in on HTTPS and keeps its route', async () => {
  const env = installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  env.route(TARGET_STATUS, () => json(status({ certificate_sha256: LEAF_B })))

  await mod.handleHttpsTransition(transition(), 'http://localhost:1515', '')

  assert.deepEqual(env.replaced, [LOGIN_URL])
})

test('the recovery link carries the ticket while one is cached, else the login', async () => {
  installBrowser()
  // Only the durable record is known — a reload on a server that no longer
  // answers plaintext — and the link must still lead somewhere useful.
  globalThis.localStorage.setItem('cremind:https-transition', JSON.stringify(transition()))
  const withoutTicket = await load('src/services/httpsTransition.ts')
  assert.equal(withoutTicket.httpsTransitionState.recoveryUrl.value, LOGIN_URL)
  assert.equal(withoutTicket.savedHttpsTransition()?.id, transition().id)

  seedTicket(transition().id)
  const withTicket = await load('src/services/httpsTransition.ts')
  assert.equal(withTicket.httpsTransitionState.recoveryUrl.value, HANDOFF_URL)
})

test('a different CA never moves the tab', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  env.route(TARGET_STATUS, () => json(status({ ca_sha256: 'CA:BB:BB' })))

  void mod.handleHttpsTransition(transition(), 'http://localhost:1515', '')
  await advance(timers, 50_000, 250)

  assert.deepEqual(env.replaced, [])
  assert.equal(mod.httpsTransitionState.phase.value, 'attention')
  assert.equal(mod.httpsTransitionState.reason.value, 'certificate-mismatch')
  // Clean up the running loop so it does not outlive the test.
  await mod.handleHttpsTransition(transition({ phase: 'cancelled' }), 'http://localhost:1515', '')
})

test('a probe resolving after the switch was cancelled does not navigate', async () => {
  const env = installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  const pinned = transition()
  seedTicket(pinned.id)
  const answer = deferred()
  // Ignores the abort signal on purpose: the answer arrives anyway.
  env.route(TARGET_STATUS, () => answer.promise)

  const moving = mod.handleHttpsTransition(pinned, 'http://localhost:1515', '')
  await until(() => env.callsTo(TARGET_STATUS).length === 1, 'the first probe')
  await mod.handleHttpsTransition({ ...pinned, phase: 'cancelled' }, 'http://localhost:1515', '')
  // A perfectly valid answer — only the cancellation may stop the move.
  answer.resolve(json(status()))
  await moving
  await flush()

  assert.deepEqual(env.replaced, [])
  assert.equal(mod.httpsTransitionState.phase.value, 'idle')
  // And the cancelled switch dropped its single-use ticket.
  assert.equal(globalThis.sessionStorage.getItem(`cremind:https-ticket:${pinned.id}`), null)
})

test('interrupted probes are retried until the secure server answers', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  const pinned = transition()
  seedTicket(pinned.id)
  let probes = 0
  env.route(TARGET_STATUS, () => {
    probes += 1
    if (probes <= 2) throw new TypeError('connection reset by the rollout')
    return json(status({ certificate_sha256: LEAF_B }))
  })

  void mod.handleHttpsTransition(pinned, 'http://localhost:1515', '')
  await advance(timers, 4_000)

  assert.equal(probes, 3)
  assert.deepEqual(env.replaced, [HANDOFF_URL])
})

test('a probe that never answers is abandoned after five seconds', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  const pinned = transition()
  seedTicket(pinned.id)
  let probes = 0
  env.route(TARGET_STATUS, () => {
    probes += 1
    // The first request hangs forever and ignores its abort signal.
    return probes === 1 ? deferred().promise : json(status({ certificate_sha256: LEAF_B }))
  })

  void mod.handleHttpsTransition(pinned, 'http://localhost:1515', '')
  await advance(timers, 4_800)
  assert.equal(probes, 1, 'still inside the five-second bound')
  assert.deepEqual(env.replaced, [])

  await advance(timers, 2_000) // bound + the 1.5s pause before the next probe
  assert.equal(probes, 2)
  assert.deepEqual(env.replaced, [HANDOFF_URL])
})

test('after 45 seconds the tab explains why, by reason', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  env.route(TARGET_STATUS, () => json(status({
    phase: 'activating', activation_error: 'Token files could not be re-signed.',
  })))

  void mod.handleHttpsTransition(transition(), 'http://localhost:1515', '')
  await advance(timers, 30_000, 250)
  assert.equal(mod.httpsTransitionState.phase.value, 'waiting')
  await advance(timers, 16_000, 250)

  assert.equal(mod.httpsTransitionState.phase.value, 'attention')
  assert.equal(mod.httpsTransitionState.reason.value, 'activation-failed')
  assert.match(mod.httpsTransitionState.error.value, /answers, but activation failed: Token files/)
  assert.deepEqual(env.replaced, [])
  await mod.handleHttpsTransition(transition({ phase: 'cancelled' }), 'http://localhost:1515', '')
})

test('the OAuth return page is never mistaken for a profile', async () => {
  const env = installBrowser({ href: 'http://localhost:1515/#/oauth-return?ref=abc' })
  globalThis.localStorage.setItem('profile_id', 'alice')
  const mod = await load('src/services/httpsTransition.ts')
  env.route(TARGET_STATUS, () => json(status()))

  await mod.handleHttpsTransition(transition(), 'http://localhost:1515', '')

  // Falls back to the stored profile, never to "oauth-return".
  assert.equal(env.replaced.length, 1)
  assert.match(env.replaced[0], /#\/login\/alice\?redirect=/)
})

/** Run the coordinator's clock for `ms`, recording every phase it showed. */
async function phasesOver(timers, mod, ms) {
  const seen = new Set([mod.httpsTransitionState.phase.value])
  for (let elapsed = 0; elapsed < ms; elapsed += 250) {
    await advance(timers, 250, 50)
    seen.add(mod.httpsTransitionState.phase.value)
  }
  return seen
}

test('a prepared switch never raises the blocking overlay over a status read that timed out', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  const prepared = transition({ phase: 'prepared' })
  // This tab already heard of the prepared switch (SSE, heartbeat).
  await mod.handleHttpsTransition(prepared, 'http://localhost:1515', '')
  let reads = 0
  env.route(SOURCE_STATUS, () => {
    reads += 1
    // The first read hangs past its bound (a proxy mid-rollout); later ones answer.
    return reads === 1 ? deferred().promise : json(status({ phase: 'prepared' }, { serving_https: false }))
  })
  env.route(TARGET_STATUS, () => { throw new TypeError('no HTTPS listener: nothing activated') })

  const stop = mod.installHttpsTransitionCoordinator('http://localhost:1515', '')
  const seen = await phasesOver(timers, mod, 9_000)

  assert.ok(reads >= 2, 'the source was read again after the timeout')
  assert.ok(!seen.has('attention'), `phases seen: ${[...seen].join(', ')}`)
  assert.equal(mod.httpsTransitionState.phase.value, 'idle')
  assert.equal(mod.httpsTransitionState.error.value, null)
  // The heartbeat's re-announcement, and "Check now", leave it that way.
  await mod.handleHttpsTransition(prepared, 'http://localhost:1515', '')
  mod.retryHttpsTransition()
  assert.equal(mod.httpsTransitionState.phase.value, 'idle')
  stop()
})

test('an acknowledged quiescing tab keeps waiting through a status read that timed out', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  const quiescing = transition({ phase: 'quiescing' })
  env.route('/api/tls/handoff', () => json({ ticket: TICKET, expires_at: Date.now() / 1000 + 600 }))
  env.route('/api/tls/ready', () => json(status({ phase: 'quiescing' }, { serving_https: false })))
  await mod.handleHttpsTransition(quiescing, 'http://localhost:1515', 'tok')
  assert.equal(mod.httpsTransitionState.phase.value, 'waiting', 'saved its handoff and acknowledged')
  let reads = 0
  env.route(SOURCE_STATUS, () => {
    reads += 1
    return reads === 1 ? deferred().promise : json(status({ phase: 'quiescing' }, { serving_https: false }))
  })
  env.route(TARGET_STATUS, () => { throw new TypeError('no HTTPS listener yet') })

  const stop = mod.installHttpsTransitionCoordinator('http://localhost:1515', 'tok')
  const seen = await phasesOver(timers, mod, 9_000)

  assert.ok(reads >= 2, 'the source was read again after the timeout')
  assert.ok(!seen.has('attention'), `phases seen: ${[...seen].join(', ')}`)
  assert.equal(mod.httpsTransitionState.phase.value, 'waiting')
  stop()
})

test('a stale prepared frame never takes down the overlay of a switch that moved on', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  const pinned = transition()
  seedTicket(pinned.id)
  let up = false
  env.route(TARGET_STATUS, () => {
    if (!up) throw new TypeError('not listening yet')
    return json(status({ certificate_sha256: LEAF_B }))
  })

  void mod.handleHttpsTransition(pinned, 'http://localhost:1515', '')
  await advance(timers, 46_000, 250)
  assert.equal(mod.httpsTransitionState.phase.value, 'attention')
  // A frame from before activation arrives late; the switch it describes is gone.
  void mod.handleHttpsTransition({ ...pinned, phase: 'prepared' }, 'http://localhost:1515', '')
  await flush()
  assert.equal(mod.httpsTransitionState.phase.value, 'attention')

  // And the wait it interrupted is still running: the tab moves once HTTPS is up.
  up = true
  await advance(timers, 2_000)
  assert.deepEqual(env.replaced, [HANDOFF_URL])
})

test('an active switch re-announced as activating still retries a failed move', async () => {
  installBrowser()
  const mod = await load('src/services/httpsTransition.ts')
  let attempts = 0
  globalThis.window.cremind = {
    server: {
      migrateHttps: async () => {
        attempts += 1
        return attempts === 1 ? { ok: false, error: 'The secure origin is not up yet.' } : { ok: true }
      },
    },
  }
  const active = transition({ phase: 'active' })
  await mod.handleHttpsTransition(active, 'http://localhost:1515', '')
  assert.equal(mod.httpsTransitionState.phase.value, 'attention')

  // What resume() and the channel reconcile send once plaintext went
  // recovery-only: the saved switch, as activating. Refused as older, but it is
  // still this switch, so the failed move is retried.
  await mod.handleHttpsTransition({ ...active, phase: 'activating' }, 'http://localhost:1515', '')

  assert.equal(attempts, 2)
})

// ── the Settings / setup pivot ────────────────────────────────────────────

function pivotOptions(overrides = {}) {
  return {
    agentUrl: 'http://localhost:1515',
    restartToken: 'admin-token',
    nextOrigin: 'https://localhost:1515',
    profile: 'alice',
    profileToken: 'admin-token',
    installMode: 'native',
    management: 'native',
    ...overrides,
  }
}

test('pivot: pod replacement completes the handoff with the cached ticket', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/composables/useHttpsPivot.ts')
  const pinned = transition()
  seedTicket(pinned.id)
  env.route(TARGET_STATUS, () => json(status({ certificate_sha256: LEAF_B })))
  const pivot = mod.useHttpsPivot()

  pivot.enterManualMode(pivotOptions({ transition: pinned, resumeStatus: status({ phase: 'activating' }) }))
  await advance(timers, 2_000)

  assert.deepEqual(env.replaced, [HANDOFF_URL])
  assert.equal(pivot.phase.value, 'redirecting')
  assert.equal(pivot.readiness.value?.reason, 'ready')
})

test('pivot: cancelling while a probe is in flight never navigates', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/composables/useHttpsPivot.ts')
  const pinned = transition()
  seedTicket(pinned.id)
  const answer = deferred()
  env.route(TARGET_STATUS, () => answer.promise)
  const pivot = mod.useHttpsPivot()

  pivot.enterManualMode(pivotOptions({ transition: pinned, resumeStatus: status({ phase: 'activating' }) }))
  await advance(timers, 1_000)
  assert.equal(env.callsTo(TARGET_STATUS).length, 1)
  pivot.cancelManualProbe()
  answer.resolve(json(status({ certificate_sha256: LEAF_B })))
  await advance(timers, 10_000, 250)

  assert.deepEqual(env.replaced, [])
  assert.equal(pivot.phase.value, 'idle')
  assert.equal(env.callsTo(TARGET_STATUS).length, 1, 'the cancelled loop stopped probing')
})

test('pivot: redirectNow works from the pinned transition alone', async () => {
  const env = installBrowser()
  const mod = await load('src/composables/useHttpsPivot.ts')
  const pinned = transition()
  env.route(SOURCE_STATUS, () => json(status({ phase: 'prepared' }, { serving_https: false })))
  env.route('/api/tls/activate', () => json({ error: 'The certificate changed.' }, 409))
  // Browser readiness barrier: no sibling tabs, so it settles on its own.
  globalThis.BroadcastChannel = undefined
  env.route('/api/tls/handoff', () => json({ ticket: TICKET, expires_at: Date.now() / 1000 + 600 }))
  const pivot = mod.useHttpsPivot()

  await pivot.run(pivotOptions({ transition: transition({ phase: 'prepared' }) }))
  assert.equal(pivot.phase.value, 'failed')
  pivot.redirectNow()

  assert.equal(env.replaced.length, 1)
  assert.equal(env.replaced[0], HANDOFF_URL)
  assert.equal(pinned.id, pivot.transition.value.id)
})

test('pivot: an activation whose 202 was lost to the restart still completes', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  globalThis.BroadcastChannel = undefined
  const mod = await load('src/composables/useHttpsPivot.ts')
  const prepared = transition({ phase: 'prepared' })
  let committed = false
  env.route(SOURCE_STATUS, () => (committed
    // The server came back on HTTPS: plaintext is recovery-only now.
    ? json({ error: 'HTTPS is required.' }, 426)
    : json(status({ phase: 'prepared' }, { serving_https: false }))))
  env.route('/api/tls/handoff', () => json({ ticket: TICKET, expires_at: Date.now() / 1000 + 600 }))
  env.route('/api/tls/activate', () => {
    if (committed) return json({ error: 'HTTPS is required.' }, 426)
    committed = true
    // The commit happened; the response did not survive the restart.
    throw new TypeError('connection reset')
  })
  env.route(TARGET_STATUS, () => json(status({ certificate_sha256: LEAF_B })))
  const pivot = mod.useHttpsPivot()
  let activated = null

  pivot.enterManualMode(pivotOptions({
    transition: prepared,
    // What Settings hands over: the status it rendered for the *prepared*
    // switch, which could still be cancelled and carried the prepare runbook.
    resumeStatus: status({ phase: 'prepared' }, {
      serving_https: false,
      can_cancel: true,
      quiesce_pending: 2,
      restart_required: true,
      steps: [{ kind: 'command', text: 'helm upgrade cremind cremind/cremind' }],
      instructions: ['helm upgrade cremind cremind/cremind'],
    }),
    onActivated: (value) => { activated = value },
  }))
  await advance(timers, 20_000, 100)

  assert.ok(activated, 'activation was treated as persisted')
  assert.equal(activated.transition.phase, 'activating')
  assert.equal(activated.transition.awaiting_operator, false)
  // Only what is true once plaintext went recovery-only: nothing to cancel,
  // no tab left to quiesce, nothing to restart, and no stale runbook.
  assert.equal(activated.can_cancel, false)
  assert.deepEqual(activated.steps, [])
  assert.deepEqual(activated.instructions, [])
  assert.ok(!activated.quiesce_pending, `quiesce_pending: ${activated.quiesce_pending}`)
  assert.equal(activated.restart_required, false)
  assert.equal(activated.restart_error, null)
  assert.deepEqual(env.replaced, [HANDOFF_URL])
  assert.equal(pivot.error.value, null)
})

test('pivot: Electron keeps waiting with the reason instead of flapping to failed', async (t) => {
  const timers = fakeTime(t)
  installBrowser()
  const mod = await load('src/composables/useHttpsPivot.ts')
  let attempts = 0
  globalThis.window.cremind = {
    server: {
      prepareHttpsMigration: async () => ({ ok: true }),
      migrateHttps: async () => { attempts += 1; return { ok: false, error: 'The secure origin is not up yet.' } },
      releaseHttpsMigration: async () => {},
    },
  }
  const pivot = mod.useHttpsPivot()
  const seen = new Set()

  void pivot.run(pivotOptions({
    management: 'electron', transition: transition(), resumeStatus: status({ phase: 'activating' }),
  }))
  for (let elapsed = 0; elapsed < 50_000; elapsed += 500) {
    await advance(timers, 500, 100)
    seen.add(pivot.phase.value)
  }

  assert.ok(attempts > 5, 'kept asking the main process')
  assert.ok(!seen.has('failed'), `phases seen: ${[...seen].join(', ')}`)
  assert.equal(pivot.phase.value, 'waiting')
  assert.equal(pivot.error.value, 'The secure origin is not up yet.')
  assert.equal(pivot.forwardHint.value, true)
  pivot.cancelManualProbe()
  await advance(timers, 4_000, 250)
})

test('pivot: the recovery link prefers the freshest cached ticket', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/composables/useHttpsPivot.ts')
  const pinned = transition()
  env.route(TARGET_STATUS, () => { throw new TypeError('unreachable') })
  const pivot = mod.useHttpsPivot()

  pivot.enterManualMode(pivotOptions({ transition: pinned, resumeStatus: status({ phase: 'activating' }) }))
  await advance(timers, 2_000)
  assert.equal(pivot.recoveryUrl.value, LOGIN_URL)
  seedTicket(pinned.id)
  await advance(timers, 3_500)
  assert.equal(pivot.recoveryUrl.value, HANDOFF_URL)
  assert.equal(pivot.readiness.value?.reason, 'unreachable')
  assert.equal(CA, pinned.ca_sha256)
  pivot.cancelManualProbe()
})

// ── a pivot on another address than the one that prepared the switch ─────
//
// The switch was prepared from the server's own localhost; the admin then
// activates it from a laptop at 192.168.1.5. Probing, linking to or minting a
// ticket for https://localhost:1515 from there points at the laptop itself —
// and a ticket bound to that origin is consumed, not redeemed, on arrival.

function withHandoffs(env) {
  const minted = []
  env.route('/api/tls/handoff', (_url, init) => {
    minted.push(JSON.parse(init.body))
    return json({ ticket: TICKET, expires_at: Date.now() / 1000 + 600 })
  })
  return minted
}

test('pivot: a superseded run never releases the busy state its successor took', async (t) => {
  // The caller disables its buttons for the duration of an activation and lets
  // `onSettled` re-enable them. A run that is superseded — by Cancel, by Retry —
  // resumes one microtask AFTER the new caller has taken the flag for its own
  // request, so releasing it there un-dims the buttons mid-request: a second
  // click, and a page that once again looks like it ignored the first one.
  const timers = fakeTime(t)
  const env = installBrowser()
  const mod = await load('src/composables/useHttpsPivot.ts')
  const pinned = transition()
  seedTicket(pinned.id)
  env.route(TARGET_STATUS, () => json(status({ certificate_sha256: LEAF_A })))  // never ready
  const pivot = mod.useHttpsPivot()

  const settled = []
  let busy = true
  pivot.enterManualMode(pivotOptions({
    transition: pinned,
    resumeStatus: status({ phase: 'activating' }),
    onSettled: () => { settled.push(busy) },
  }))
  await advance(timers, 2_000)
  assert.equal(settled.length, 0, 'a run still probing has not settled')

  // What Cancel does: supersede the run, then take the flag for its own request.
  pivot.cancelManualProbe()
  busy = true
  await advance(timers, 2_000)

  assert.deepEqual(settled, [], 'a superseded run owns nothing left to release')
  assert.equal(busy, true)
})

test('pivot: another address probes, links and refreshes its ticket for itself', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser({ href: `${LAN}/#/alice/c/42` })
  const mod = await load('src/composables/useHttpsPivot.ts')
  const minted = withHandoffs(env)
  let up = false
  env.route(LAN_TARGET_STATUS, () => {
    if (!up) throw new TypeError('not listening yet')
    // The target reports the switch exactly as it was prepared (localhost).
    return json(status({ certificate_sha256: LEAF_B }))
  })
  const pivot = mod.useHttpsPivot()

  pivot.enterManualMode(pivotOptions({
    agentUrl: LAN, transition: transition(), resumeStatus: status({ phase: 'activating' }),
  }))
  await advance(timers, 1_000)

  assert.ok(env.callsTo(LAN_TARGET_STATUS).length >= 1, "probed this browser's secure address")
  assert.equal(env.callsTo('localhost').length, 0, "never reached for the preparer's localhost")
  // The background refresh bound its ticket to where this tab will land.
  assert.deepEqual(minted.map(body => [body.source_origin, body.target_origin]), [[LAN, LAN_SECURE]])
  assert.equal(pivot.recoveryUrl.value, LAN_HANDOFF_URL)

  up = true
  await advance(timers, 2_000)
  assert.deepEqual(env.replaced, [LAN_HANDOFF_URL])
  assert.equal(pivot.readiness.value?.reason, 'ready')
})

test('pivot: another address signs in on its own secure address without a ticket', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser({ href: `${LAN}/#/alice/c/42` })
  const mod = await load('src/composables/useHttpsPivot.ts')
  env.route(LAN_TARGET_STATUS, () => { throw new TypeError('not listening yet') })
  const pivot = mod.useHttpsPivot()

  // No profile token: nothing can be minted, so the link is the login.
  pivot.enterManualMode(pivotOptions({
    agentUrl: LAN, profileToken: '', transition: transition(), resumeStatus: status({ phase: 'activating' }),
  }))
  await advance(timers, 1_000)

  assert.equal(pivot.recoveryUrl.value, LAN_LOGIN_URL)
  pivot.cancelManualProbe()
})

test('pivot: the pre-activation ticket is minted for this browser\'s secure address', async () => {
  const env = installBrowser({ href: `${LAN}/#/alice/c/42` })
  globalThis.BroadcastChannel = undefined
  const mod = await load('src/composables/useHttpsPivot.ts')
  env.route(`${LAN}/api/tls/status`, () => json(status({ phase: 'prepared' }, { serving_https: false })))
  env.route('/api/tls/activate', () => json({ error: 'The certificate changed.' }, 409))
  const minted = withHandoffs(env)
  const pivot = mod.useHttpsPivot()

  await pivot.run(pivotOptions({ agentUrl: LAN, transition: transition({ phase: 'prepared' }) }))
  assert.equal(pivot.phase.value, 'failed')
  assert.deepEqual(minted.map(body => [body.source_origin, body.target_origin]), [[LAN, LAN_SECURE]])
  pivot.redirectNow()

  assert.deepEqual(env.replaced, [LAN_HANDOFF_URL])
})

test('pivot: the address hint may name this browser or the announced host, nothing else', async (t) => {
  const timers = fakeTime(t)
  const env = installBrowser({ href: `${LAN}/#/alice/c/42` })
  const mod = await load('src/composables/useHttpsPivot.ts')
  env.route(LAN_TARGET_STATUS, () => { throw new TypeError('not listening yet') })
  const start = (nextOrigin) => {
    const pivot = mod.useHttpsPivot()
    pivot.enterManualMode(pivotOptions({
      agentUrl: LAN, nextOrigin, transition: transition(), resumeStatus: status({ phase: 'activating' }),
    }))
    return pivot
  }

  // Settings hints with the status it read (the announced host); setup with
  // the Host header this browser used.
  for (const hint of ['https://localhost:1515', LAN_SECURE]) {
    const pivot = start(hint)
    await advance(timers, 1_000)
    assert.equal(pivot.phase.value, 'manual', hint)
    pivot.cancelManualProbe()
  }
  const elsewhere = start('https://elsewhere.example:1515')
  await advance(timers, 500)
  assert.equal(elsewhere.phase.value, 'failed')
  assert.match(elsewhere.error.value, /does not match/)
})

// ── Settings → HTTPS: the capabilities read ───────────────────────────────

test('capabilities: a caller signal bounds the read even when the server never answers', async () => {
  const env = installBrowser()
  const api = await load('src/services/configApi.ts')
  // Accepts the connection, never answers, and ignores the abort on purpose.
  env.route('/api/services/capabilities', () => deferred().promise)
  const controller = new AbortController()
  let outcome = null
  api.fetchServiceCapabilities('http://localhost:1515', 'admin-token', { signal: controller.signal })
    .then(() => { outcome = 'resolved' }, (error) => { outcome = error })
  await flush()
  assert.equal(outcome, null, 'still waiting before the bound')

  controller.abort()
  await flush()
  assert.equal(outcome?.name, 'AbortError')
  // The signal reached the request itself, and the credential still rides along.
  const [call] = env.callsTo('/api/services/capabilities')
  assert.equal(call.init.signal, controller.signal)
  assert.equal(call.init.headers.Authorization, 'Bearer admin-token')
})

test('capabilities: positional callers without a signal are unchanged', async () => {
  const env = installBrowser()
  const api = await load('src/services/configApi.ts')
  env.route('/api/services/capabilities', () => json({ services: {}, docker_available: false }))

  const caps = await api.fetchServiceCapabilities('http://localhost:1515')

  assert.equal(caps.docker_available, false)
  assert.equal(env.calls[0].init.headers.Authorization, undefined)
})
