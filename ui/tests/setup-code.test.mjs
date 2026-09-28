// Setup codes (cremind-tag docs/connect-setup.md §2.2): the label on a bridge
// or tag, typed or scanned, must be understood exactly as the companion's
// reference implementation (companion/src/cremind_tag/secure/codes.py) does.
//
// The vectors below were generated with that reference:
//   SetupPayload(NodeRole.TAG, 0x1A2B3C4D, bytes(range(10))).code()
// so a change on either side that breaks agreement fails here. The page only
// validates and shows the short id; the code text itself goes to the server.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, load } from './harness.mjs'

installBrowser()
const codes = await load('src/utils/setupCode.ts')

const hex = (s) => Uint8Array.from(s.match(/../g).map((b) => parseInt(b, 16)))
function payload(roleId, shortId, secretHex) {
  const out = new Uint8Array(15)
  out[0] = 0x20 | roleId
  new DataView(out.buffer).setUint32(1, shortId, true)
  out.set(hex(secretHex), 5)
  return out
}

// role, short id, secret → the reference's code() and qr_text().
const VECTORS = [
  ['tag', 0x1A2B3C4D, '00010203040506070809', '4D6KR-ART00-0G40R-40M30-E2097', 'CTAG:4D6KRART000G40R40M30E2097'],
  ['bridge', 0x01020304, '00010203040506070809', '48206-0G100-0G40R-40M30-E2096', 'CTAG:482060G1000G40R40M30E2096'],
  ['tag', 0x00000000, '00000000000000000000', '4C000-00000-00000-00000-0000X', 'CTAG:4C0000000000000000000000X'],
  ['tag', 0xFFFFFFFF, 'ffffffffffffffffffff', '4FZZZ-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZ5', 'CTAG:4FZZZZZZZZZZZZZZZZZZZZZZ5'],
  ['bridge', 0xDEADBEEF, 'a1b2c3d4e5f60718293a', '4BQVX-BEYM6-SC7N7-5YR3H-GA9TM', 'CTAG:4BQVXBEYM6SC7N75YR3HGA9TM'],
  ['tag', 0x5A5A5A5A, '00112233445566778899', '4DD5M-PJT00-8J4CT-4ANK7-F24SK', 'CTAG:4DD5MPJT008J4CT4ANK7F24SK'],
  ['bridge', 0x00000007, '00010203040506070809', '483G0-00000-0G40R-40M30-E209Q', 'CTAG:483G0000000G40R40M30E209Q'],
]
const ROLE_ID = { bridge: 2, tag: 3 }

test('the same payload encodes to the reference implementation\'s code, grouped and for the QR', () => {
  for (const [role, shortId, secret, code, qr] of VECTORS) {
    const p = payload(ROLE_ID[role], shortId, secret)
    assert.equal(codes.encodeSetupPayload(p), code)
    assert.equal(`CTAG:${codes.encodeSetupPayload(p, false)}`, qr)
  }
})

test('every reference code parses back to its role and short id, typed or scanned', () => {
  for (const [role, shortId, , code, qr] of VECTORS) {
    const want = { role, shortId: shortId.toString(16).toUpperCase().padStart(8, '0'), normalized: code.replace(/-/g, '') }
    assert.deepEqual(codes.parseSetupCode(code), want)
    assert.deepEqual(codes.parseSetupCode(qr), want)
    assert.deepEqual(codes.parseSetupCode(code, role), want)
    // Lower case, spaces instead of dashes, a lower-case QR prefix.
    assert.deepEqual(codes.parseSetupCode(code.toLowerCase().replace(/-/g, ' ')), want)
    assert.deepEqual(codes.parseSetupCode(`ctag:${code.toLowerCase()}`), want)
  }
  assert.equal(codes.parseSetupCode('4D6KR-ART00-0G40R-40M30-E2097').shortId, '1A2B3C4D')
})

test('Crockford aliases: O reads as 0, I and L as 1; the display is five groups of five', () => {
  const code = '4C000-00000-00000-00000-0000X'
  assert.equal(codes.parseSetupCode(code.replace(/0/g, 'O')).shortId, '00000000')
  assert.equal(codes.parseSetupCode(code.replace(/0/g, 'o')).shortId, '00000000')
  assert.equal(codes.normalizeSetupCode('ctag:ab-cd il'), 'ABCD11')
  assert.equal(codes.normalizeSetupCode(' 4d6kr_art00\t0g40r\n'), '4D6KRART000G40R')
  assert.equal(codes.groupSetupCode('4D6KRART000G40R40M30E2097'), '4D6KR-ART00-0G40R-40M30-E2097')
  const [, , , , qr] = VECTORS[0]
  assert.equal(codes.formatTypedSetupCode(qr), VECTORS[0][3])
  assert.equal(codes.formatTypedSetupCode('4d6kr art0'), '4D6KR-ART0')
  // A character labels never use is left as typed, so the message matches the screen.
  assert.equal(codes.formatTypedSetupCode('4d6kr u'), '4d6kr u')
})

test('every single mistyped symbol and every swap of two neighbours is refused', () => {
  const alphabet = codes.SETUP_ALPHABET
  for (const [, , , grouped] of VECTORS.slice(0, 5)) {
    const code = grouped.replace(/-/g, '')
    let checked = 0
    for (let at = 0; at < code.length; at += 1) {
      for (const ch of alphabet) {
        if (ch === code[at]) continue
        const typo = code.slice(0, at) + ch + code.slice(at + 1)
        const res = codes.checkSetupCode(typo)
        assert.equal(res.ok, false, `substitution at ${at} (${ch}) must be refused`)
        checked += 1
      }
    }
    for (let at = 0; at < code.length - 1; at += 1) {
      if (code[at] === code[at + 1]) continue
      const swapped = code.slice(0, at) + code[at + 1] + code[at] + code.slice(at + 2)
      const res = codes.checkSetupCode(swapped)
      assert.equal(res.ok, false, `swap at ${at} must be refused`)
      checked += 1
    }
    assert.ok(checked > 700)
  }
})

test('a typo in the data is reported as a failed check, with the server\'s code', () => {
  const typo = '4D6KR-ART00-0G40R-40M30-E2098' // last symbol off by one
  const res = codes.checkSetupCode(typo)
  assert.equal(res.ok, false)
  assert.equal(res.error.code, 'setup_code_invalid')
  assert.equal(res.error.problem, 'check')
  assert.match(res.error.message, /typo/)
  const swap = '4D6KR-ART00-0G40R-40M30-E2079'
  assert.equal(codes.checkSetupCode(swap).error.problem, 'check')
})

test('length, U and other characters labels never use are refused', () => {
  const code = VECTORS[0][3].replace(/-/g, '')
  assert.equal(codes.checkSetupCode('').error.problem, 'empty')
  assert.equal(codes.checkSetupCode(code.slice(0, -2)).error.problem, 'short')
  assert.equal(codes.checkSetupCode(`${code}0`).error.problem, 'long')
  const u = codes.checkSetupCode('U'.repeat(25))
  assert.equal(u.error.code, 'setup_code_invalid')
  assert.equal(u.error.problem, 'character')
  assert.match(u.error.message, /\(U\)/)
  assert.equal(codes.checkSetupCode(`${code.slice(0, 10)}!${code.slice(11)}`).error.problem, 'character')
  assert.throws(() => codes.parseSetupCode(`${code.slice(0, 24)}U`), (e) => e instanceof codes.SetupCodeError)
})

test('the role asked for is checked: a bridge label is not a tag label', () => {
  const bridge = VECTORS[1][3]
  const res = codes.checkSetupCode(bridge, 'tag')
  assert.equal(res.ok, false)
  assert.equal(res.error.code, 'setup_code_wrong_role')
  assert.equal(res.error.message, 'This is a bridge label, not a tag label.')
  assert.equal(codes.checkSetupCode(VECTORS[0][3], 'bridge').error.message, 'This is a tag label, not a bridge label.')
  assert.equal(codes.checkSetupCode(bridge, 'bridge').ok, true)
})

test('other protocol versions, gateways and unknown roles are refused (reference codes)', () => {
  // format_code(bytes([0x13]) + bytes(14)) and friends, from codes.py.
  const v1 = codes.checkSetupCode('2C000-00000-00000-00000-0000H')
  assert.equal(v1.error.code, 'setup_code_unsupported')
  const gateway = codes.checkSetupCode('44000-00000-00000-00000-0000R')
  assert.equal(gateway.error.code, 'setup_code_invalid')
  assert.equal(gateway.error.problem, 'gateway')
  const unknown = codes.checkSetupCode('5W000-00000-00000-00000-0000N')
  assert.equal(unknown.error.code, 'setup_code_invalid')
  assert.equal(unknown.error.problem, 'unknown_role')
  assert.equal(codes.encodeSetupPayload(Uint8Array.from([0x13, ...new Array(14).fill(0)])), '2C000-00000-00000-00000-0000H')
})

test('an error message never repeats the code', () => {
  const code = VECTORS[0][3]
  for (const text of [code.slice(0, 20), `${code.slice(0, 28)}8`, VECTORS[1][3]]) {
    const res = codes.checkSetupCode(text, 'tag')
    assert.equal(res.ok, false)
    assert.ok(!res.error.message.includes(code.replace(/-/g, '').slice(0, 8)), res.error.message)
    assert.ok(!res.error.message.includes(code.slice(0, 11)), res.error.message)
  }
})
