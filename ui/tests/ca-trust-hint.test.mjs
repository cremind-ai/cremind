// The installer's `?ca_trusted=` hand-off, which is the only way a Docker or
// Kubernetes install can tell the Setup Wizard that the CA is already in the
// host's trust store (the server is in the container and can only read the
// container's store). It stands in for a checkbox the user would otherwise
// have to tick for work the installer already did — so the one thing it must
// never do is vouch for a certificate this server does not serve.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, load } from './harness.mjs'

installBrowser()
const { matchesCaTrustHint } = await load('src/services/caTrustHint.ts')

// What app/config/tls_auto.py publishes as tls.ca_sha256, and what both
// installers emit for it (colon-free, lowercase). Verified equal against a
// real generated CA on both install.sh's openssl pipeline and install.ps1's
// BitConverter/SHA256 pair.
const SERVED = 'D2:2E:68:13:89:2A:25:EA:DE:2B:15:90:A5:5A:10:78:34:84:F0:4C:AF:D7:24:E6:96:0C:DA:4A:05:AB:03:25'
const HINT = 'd22e6813892a25eade2b1590a55a10783484f04cafd724e6960cda4a05ab0325'
const OTHER = '2ec7c6572e79aa3ae4e50d31bd0194f41caa453c721c03df1e2b91e0b0d884bc'

test('the installer form matches the colon-separated uppercase served form', () => {
  assert.equal(matchesCaTrustHint(HINT, SERVED), true)
})

test('either side may carry colons or case', () => {
  assert.equal(matchesCaTrustHint(SERVED, SERVED), true)
  assert.equal(matchesCaTrustHint(HINT.toUpperCase(), SERVED), true)
  assert.equal(matchesCaTrustHint(SERVED.toLowerCase(), HINT), true)
})

// The case that makes this a fingerprint and not a boolean: a re-install
// regenerates the CA, and a link from the previous one must stop vouching.
test('a different CA is rejected', () => {
  assert.equal(matchesCaTrustHint(OTHER, SERVED), false)
})

test('a partial digest is rejected', () => {
  assert.equal(matchesCaTrustHint(HINT.slice(0, 16), SERVED), false)
  assert.equal(matchesCaTrustHint(HINT.slice(0, 63), SERVED), false)
  // One character over is not this CA either.
  assert.equal(matchesCaTrustHint(`${HINT}a`, SERVED), false)
})

test('nothing to compare means no', () => {
  assert.equal(matchesCaTrustHint('', SERVED), false)
  assert.equal(matchesCaTrustHint(undefined, SERVED), false)
  assert.equal(matchesCaTrustHint(HINT, null), false)
  assert.equal(matchesCaTrustHint(HINT, undefined), false)
  // A server that has not generated a CA yet publishes nothing to match.
  assert.equal(matchesCaTrustHint(HINT, ''), false)
})

// `?ca_trusted=a&ca_trusted=b` reaches Vue Router as an array. It must not
// stringify its way into a match.
test('a repeated query param is rejected', () => {
  assert.equal(matchesCaTrustHint([HINT], SERVED), false)
  assert.equal(matchesCaTrustHint([HINT, HINT], SERVED), false)
  assert.equal(matchesCaTrustHint({ toString: () => HINT }, SERVED), false)
})

// Non-hex characters are stripped, not treated as separators — so a value
// that merely contains the digest does not become the digest.
test('junk around the digest does not match', () => {
  assert.equal(matchesCaTrustHint(`${HINT}zz`, SERVED), true, 'trailing non-hex is stripped')
  assert.equal(matchesCaTrustHint(HINT.slice(0, 32) + 'zz' + HINT.slice(34), SERVED), false)
})
