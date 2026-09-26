// The citation grammar, held to the same cases as app/documents/cite.py.
//
// The browser numbers its citation chips itself; channels and the CLI number
// theirs in Python. "[2]" has to mean the same source everywhere, so the
// tolerant parse (full-width brackets, stray spaces, upper case, several
// tokens in one bracket) and the first-appearance numbering are checked
// against tests/fixtures/citation-grammar.json — the file
// tests/documents/test_cite_ui_parity.py checks the Python against.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { load } from './harness.mjs'

const { parseCitationTokens, numberTokens, tokenParts, makeToken, mentionsCitations, canonicalToken } =
  await load('src/utils/citations.ts')

const { cases } = JSON.parse(
  readFileSync(new URL('./fixtures/citation-grammar.json', import.meta.url), 'utf8'),
)

for (const c of cases) {
  test(`parity: ${c.name}`, () => {
    const parsed = parseCitationTokens(c.text).map(p => ({
      token: p.token, cite_id: p.citeId, c8: p.c8, start: p.start, end: p.end,
    }))
    assert.deepEqual(parsed, c.tokens)
    assert.deepEqual([...numberTokens(c.text)], c.numbers)
  })
}

test('the fixture covers every tolerant variant the models produce', () => {
  // Guard against the fixture being trimmed into meaninglessness: these are
  // the copying mistakes the tolerant grammar exists for.
  const texts = cases.map(c => c.text).join('\n')
  assert.match(texts, /【doc:/)
  assert.match(texts, /\[ doc: /)
  assert.match(texts, /\[DOC:/)
  assert.match(texts, /; doc:/)
  // …and the same mistakes under the pre-rename prefix, which is read forever.
  assert.match(texts, /【ud:/)
  assert.match(texts, /\[ ud: /)
  assert.match(texts, /\[UD:/)
  assert.match(texts, /; ud:/)
})

test('a pre-rename token is the same citation as its doc: form', () => {
  assert.equal(canonicalToken('[ud:k7m2xq9a#3f9c2e1b]'), '[doc:k7m2xq9a#3f9c2e1b]')
  assert.equal(canonicalToken('[doc:k7m2xq9a]'), '[doc:k7m2xq9a]')
  assert.equal(canonicalToken('[ud:K7M2XQ9A]'), null, 'only canonical (lower-case) tokens')
  assert.equal(canonicalToken('[doc:<script>]'), null)
  assert.equal(mentionsCitations('see [ud:k7m2xq9a]'), true)
})

test('a token split over its parts and back', () => {
  assert.deepEqual(tokenParts('[doc:k7m2xq9a#3f9c2e1b]'), { citeId: 'k7m2xq9a', c8: '3f9c2e1b' })
  assert.deepEqual(tokenParts('[doc:k7m2xq9a]'), { citeId: 'k7m2xq9a', c8: null })
  assert.equal(tokenParts('[doc:K7M2XQ9A]'), null, 'only canonical (lower-case) tokens have parts')
  assert.equal(tokenParts('[doc:<script>]'), null)
  assert.equal(makeToken('k7m2xq9a', '3f9c2e1b77aa'), '[doc:k7m2xq9a#3f9c2e1b]')
})

test('a message is routed through the citation renderer only when it could cite', () => {
  assert.equal(mentionsCitations('see [doc:k7m2xq9a]'), true)
  assert.equal(mentionsCitations('see [DOC：k7m2xq9a]'), true)
  assert.equal(mentionsCitations('nothing here'), false)
  assert.equal(mentionsCitations(''), false)
})
