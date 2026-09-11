// The one readiness rule shared by Settings/setup (useHttpsPivot) and every
// background tab (httpsTransition). The case this file exists for: a replaced
// pod or container renews the generated leaf under the same CA, and the switch
// must still complete — while a different CA, a changed supplied certificate,
// or another installation/transition/address must not.
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CA, INSTANCE, LEAF_A, LEAF_B, advance, deferred, flush, installBrowser, json, load,
  status, transition,
} from './harness.mjs'

installBrowser()
const readiness = await load('src/services/httpsReadiness.ts')
const { evaluateHttpsReadiness, probeHttpsReadiness, sameCertificateAuthority } = readiness

const pinned = transition()

test('a renewed leaf under the same generated CA is ready', () => {
  const verdict = evaluateHttpsReadiness(pinned, status({ certificate_sha256: LEAF_B }))
  assert.equal(verdict.ready, true)
  assert.equal(verdict.reason, 'ready')
  assert.equal(verdict.responded, true)
  assert.equal(verdict.transition.certificate_sha256, LEAF_B)
  assert.equal(sameCertificateAuthority(pinned, { ...pinned, certificate_sha256: LEAF_B }), true)
})

test('a different CA is a certificate mismatch, whatever the leaf', () => {
  for (const leaf of [LEAF_A, LEAF_B]) {
    const verdict = evaluateHttpsReadiness(pinned, status({ ca_sha256: 'CA:BB:BB', certificate_sha256: leaf }))
    assert.equal(verdict.ready, false)
    assert.equal(verdict.reason, 'certificate-mismatch')
    assert.equal(verdict.responded, true)
  }
})

test('a local pin without a CA falls back to the strict leaf comparison', () => {
  const noCa = transition({ ca_sha256: null })
  assert.equal(evaluateHttpsReadiness(noCa, status({ ca_sha256: null })).ready, true)
  assert.equal(evaluateHttpsReadiness(noCa, status({ ca_sha256: null, certificate_sha256: LEAF_B })).reason,
    'certificate-mismatch')
})

test('a supplied certificate stays pinned by its exact leaf', () => {
  const custom = transition({ certificate_kind: 'custom', ca_sha256: null })
  const same = status({ certificate_kind: 'custom', ca_sha256: null })
  assert.equal(evaluateHttpsReadiness(custom, same).ready, true)
  const changed = status({ certificate_kind: 'custom', ca_sha256: null, certificate_sha256: LEAF_B })
  assert.equal(evaluateHttpsReadiness(custom, changed).reason, 'certificate-mismatch')
  // No fingerprint at all can never be verified.
  const unknown = transition({ certificate_kind: 'custom', ca_sha256: null, certificate_sha256: null })
  assert.equal(evaluateHttpsReadiness(unknown, status({
    certificate_kind: 'custom', ca_sha256: null, certificate_sha256: null,
  })).reason, 'certificate-mismatch')
})

test('edge-managed HTTPS has no fingerprint to pin', () => {
  const external = transition({ certificate_kind: 'external', ca_sha256: null, certificate_sha256: null })
  assert.equal(evaluateHttpsReadiness(external, status({
    certificate_kind: 'external', ca_sha256: null, certificate_sha256: null,
  })).ready, true)
})

test('a changed certificate kind is not the prepared certificate', () => {
  assert.equal(evaluateHttpsReadiness(pinned, status({ certificate_kind: 'custom' })).reason,
    'certificate-mismatch')
})

test('identity comes first: installation, transition, origins and port', () => {
  const cases = [
    ['another installation', status({}, { instance_id: 'b'.repeat(48) })],
    ['a transition claiming another instance', status({ instance_id: 'b'.repeat(48) })],
    ['another transition', status({ id: 'y'.repeat(32) })],
    ['no transition at all', status({}, { transition: null })],
    ['another source origin', status({ source_origin: 'http://other.example:1515' })],
    ['another target origin', status({ target_origin: 'https://localhost:9443' })],
    ['another public port', status({ public_port: 9443 })],
    ['a different port topology', status({ same_public_port: false })],
  ]
  for (const [label, answer] of cases) {
    const verdict = evaluateHttpsReadiness(pinned, answer)
    assert.equal(verdict.ready, false, label)
    assert.equal(verdict.reason, 'transition-mismatch', label)
  }
  // Checked before the certificate: a mismatched switch with a renewed leaf is
  // still reported as the wrong switch, not a certificate problem.
  assert.equal(evaluateHttpsReadiness(pinned, status({ id: 'y'.repeat(32), ca_sha256: 'CA:BB:BB' })).reason,
    'transition-mismatch')
})

test('an answer that is not the HTTPS server is not ready', () => {
  const verdict = evaluateHttpsReadiness(pinned, status({}, { serving_https: false }))
  assert.equal(verdict.reason, 'not-serving-https')
  assert.equal(verdict.responded, true)
})

test('localization maps both sides onto this browser\'s address', () => {
  const alias = transition({ source_origin: 'http://127.0.0.1:1515', target_origin: 'https://127.0.0.1:1515' })
  const localize = t => ({ ...t, source_origin: 'http://localhost:1515', target_origin: 'https://localhost:1515' })
  assert.equal(evaluateHttpsReadiness(alias, status({ certificate_sha256: LEAF_B }), localize).ready, true)
  assert.equal(evaluateHttpsReadiness(alias, status()).reason, 'transition-mismatch')
})

test('phase decides between moving, finishing and a responding failure', () => {
  const failed = evaluateHttpsReadiness(pinned, status({
    phase: 'activating', activation_error: 'Token files could not be re-signed.',
  }))
  assert.deepEqual([failed.ready, failed.reason, failed.responded, failed.message],
    [false, 'activation-failed', true, 'Token files could not be re-signed.'])
  // The top-level mirror counts too, for clients that read only that.
  const mirrored = evaluateHttpsReadiness(pinned, status({ phase: 'activating' }, { activation_error: 'Disk full.' }))
  assert.equal(mirrored.reason, 'activation-failed')
  assert.equal(mirrored.message, 'Disk full.')

  const pending = evaluateHttpsReadiness(pinned, status({ phase: 'activating' }))
  assert.deepEqual([pending.ready, pending.reason, pending.responded], [false, 'activation-pending', true])

  const invalid = evaluateHttpsReadiness(pinned, status({}, {
    ready: false, certificate_error: 'The certificate does not cover localhost.',
  }))
  assert.deepEqual([invalid.ready, invalid.reason, invalid.message],
    [false, 'certificate-invalid', 'The certificate does not cover localhost.'])

  for (const phase of ['prepared', 'quiescing', 'cancelled']) {
    assert.equal(evaluateHttpsReadiness(pinned, status({ phase })).reason, 'transition-mismatch', phase)
  }
})

test('a probe that cannot connect is unreachable, not an exception', async () => {
  const env = installBrowser()
  env.route('/api/tls/status', () => { throw new TypeError('Failed to fetch') })
  const verdict = await probeHttpsReadiness(pinned)
  assert.deepEqual([verdict.ready, verdict.reason, verdict.responded], [false, 'unreachable', false])
  assert.equal(env.calls[0].url, 'https://localhost:1515/api/tls/status')
  // Credential-free and CORS-simple: the target is learned from metadata.
  assert.equal(env.calls[0].init.headers?.Authorization, undefined)
})

test('a relay error from the target is unreachable too', async () => {
  const env = installBrowser()
  env.route('/api/tls/status', () => json({ error: 'upstream missing' }, 502))
  const verdict = await probeHttpsReadiness(pinned)
  assert.equal(verdict.reason, 'unreachable')
  assert.equal(verdict.responded, false)
})

test('a probe the network never answers times out within its bound', async () => {
  const env = installBrowser()
  // Deliberately ignores the abort signal: the bound must hold regardless.
  env.route('/api/tls/status', () => deferred().promise)
  const started = Date.now()
  const verdict = await probeHttpsReadiness(pinned, { timeoutMs: 40 })
  assert.deepEqual([verdict.ready, verdict.reason, verdict.responded], [false, 'timeout', false])
  assert.ok(Date.now() - started < 2000)
})

test('the default bound is five seconds', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_800_000_000_000 })
  const env = installBrowser()
  env.route('/api/tls/status', () => deferred().promise)
  let verdict = null
  void probeHttpsReadiness(pinned).then((value) => { verdict = value })
  await advance(t.mock.timers, 4_900)
  assert.equal(verdict, null, 'still waiting just under five seconds')
  await advance(t.mock.timers, 150)
  await flush()
  assert.equal(verdict?.reason, 'timeout')
})

test('a caller abort ends the probe immediately', async () => {
  const env = installBrowser()
  env.route('/api/tls/status', () => deferred().promise)
  const controller = new AbortController()
  const pending = probeHttpsReadiness(pinned, { signal: controller.signal })
  await flush()
  controller.abort()
  const verdict = await pending
  assert.equal(verdict.ready, false)
  assert.equal(verdict.responded, false)
})

test('the pinned instance id is the one the fixtures share', () => {
  // Guard for the fixtures themselves, so a typo cannot make every "ready"
  // assertion above pass for the wrong reason.
  assert.equal(pinned.instance_id, INSTANCE)
  assert.equal(pinned.ca_sha256, CA)
})
