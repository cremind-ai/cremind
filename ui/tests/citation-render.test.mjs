// Citation chips in a rendered answer, and the answer as plain text for copy.
//
// The chips are HTML built by a marked extension and handed to v-html, and the
// text they are built from is written by a model that has been reading the
// user's documents — any of which may have been written to attack it. So the
// extension may emit markup only for a token of the exact citation shape
// (8 letters/digits, optional 8 hex), and a bracket that merely starts like
// one ("[ud:<script>]") comes out as inert, escaped text rather than being
// left for marked to pass through as HTML.
import assert from 'node:assert/strict'
import test from 'node:test'

import { load } from './harness.mjs'

const { createChatMarked } = await load('src/utils/markdown.ts')
const { citationMarkedExtension, toPlainFootnotes, normalizeCitationsMeta } =
  await load('src/utils/citations.ts')

const A = '[ud:k7m2xq9a#3f9c2e1b]'
const B = '[ud:p4r8st0v]'
const C = '[ud:cccccccc#00000000]'

function render(markdown, lookup) {
  const marked = createChatMarked(href => href, [citationMarkedExtension({ lookup })])
  return marked.parse(markdown)
}

/** The chips in rendered HTML, as [token, n, status] triples. */
function chips(html) {
  return [...html.matchAll(/<button type="button" class="ud-cite" data-token="([^"]*)" data-n="(\d+)" data-status="([a-z_]+)"/g)]
    .map(m => [m[1], Number(m[2]), m[3]])
}

// ── chips ───────────────────────────────────────────────────────────────────

test('each token becomes a numbered chip, numbered by first appearance', () => {
  const html = render(`First ${B}, then ${A}, then ${B} again.`)
  assert.deepEqual(chips(html), [[B, 1, 'pending'], [A, 2, 'pending'], [B, 1, 'pending']])
  assert.match(html, />1<\/button>/)
})

test('a multi-token bracket renders one chip per distinct token', () => {
  const html = render(`Both [ud:k7m2xq9a#3f9c2e1b; UD: P4R8ST0V, ud:k7m2xq9a#3f9c2e1b] agree.`)
  assert.deepEqual(chips(html), [[A, 1, 'pending'], [B, 2, 'pending']])
  assert.equal(html.replace(/<[^>]*>/g, ''), 'Both 12 agree.\n', 'the bracket itself is consumed')
})

test('full-width brackets and stray spaces still make chips', () => {
  const html = render(`Theo 【ud:k7m2xq9a#3f9c2e1b】 và [ ud: p4r8st0v ].`)
  assert.deepEqual(chips(html).map(c => c[0]), [A, B])
})

test('numbers come from the whole message, so a token in a code span keeps its number', () => {
  // cite.py counts the token inside backticks; the chips after it must not
  // renumber as if it were not there, or [2] here would be [1] in a channel.
  const html = render(`\`${C}\` then ${A}`)
  assert.deepEqual(chips(html), [[A, 2, 'pending']])
  assert.match(html, /<code>\[ud:cccccccc#00000000\]<\/code>/)
})

test('a chip carries its verification status, and a quote mismatch is marked', () => {
  const items = {
    [A]: { status: 'verified', quote_status: 'exact' },
    [B]: { status: 'stale', quote_status: 'mismatch' },
    [C]: { status: 'something-new', quote_status: null },
  }
  const html = render(`${A} ${B} ${C}`, token => items[token])
  assert.deepEqual(chips(html).map(c => c[2]), ['verified', 'stale', 'pending'])
  assert.equal((html.match(/data-quote="mismatch"/g) || []).length, 1)
})

test('the renderer can be reused: numbering resets for every message', () => {
  const marked = createChatMarked(href => href, [citationMarkedExtension()])
  chips(marked.parse(`${A} ${B}`))
  assert.deepEqual(chips(marked.parse(`${B}`)), [[B, 1, 'pending']])
})

// ── injection ───────────────────────────────────────────────────────────────

test('a document string posing as a token renders nothing executable', () => {
  const hostile = [
    '[ud:<script>alert(1)</script>]',
    '[ud:<img src=x onerror=alert(1)>]',
    '【ud:"><svg onload=alert(1)>】',
    '[UD：<iframe src=javascript:alert(1)>]',
    '[ud:k7m2xq9a" onmouseover="alert(1)]',
  ]
  for (const text of hostile) {
    const html = render(`Answer ${text} end`)
    assert.deepEqual(chips(html), [], text)
    // The paragraph is the only element: everything the bracket held is text.
    assert.doesNotMatch(html.replace(/<\/?p>/g, ''), /</, text)
    assert.match(html, /\[(ud|UD)|【ud/, `${text} stays readable as text`)
  }
})

test('chip attributes only ever hold the token alphabet', () => {
  const html = render(`${A} [UD:ILOUilou] [ud:ZZZZZZZZ#ABCDEF01]`)
  for (const [token] of chips(html)) assert.match(token, /^\[ud:[0-9a-z]{8}(#[0-9a-f]{8})?\]$/)
  assert.equal(chips(html).length, 3)
})

test('a Markdown link whose text starts like a citation is still a link', () => {
  const html = render('[ud:notes](https://example.com)')
  assert.match(html, /<a href="https:\/\/example.com">ud:notes<\/a>/)
})

// ── plain text for copy ─────────────────────────────────────────────────────

const ITEMS = normalizeCitationsMeta({
  v: 1,
  unverified: 1,
  items: [
    {
      n: 1, token: A, status: 'verified', quote_status: null,
      file: { fid: 'k7m2xq9a', name: 'report.pdf', rel_path: 'Finance/2024/report.pdf', source: 'local', kind: 'pdf' },
      locator: { page: 12 }, locator_label: 'p. 12', snippet: '…',
    },
    {
      n: 2, token: B, status: 'stale', quote_status: 'mismatch',
      file: { fid: 'p4r8st0v', name: 'Plan', rel_path: 'Plan', source: 'drive', kind: 'docx', web_link: 'https://docs.google.com/d/1' },
      locator: {}, locator_label: '', snippet: '',
    },
  ],
}).items

test('copy turns tokens into footnote numbers and lists the sources', () => {
  const text = `Revenue grew [ud:k7m2xq9a#3f9c2e1b; ud:p4r8st0v]. See ${A}.`
  assert.equal(
    toPlainFootnotes(text, ITEMS),
    'Revenue grew [1][2]. See [1].\n\n'
      + 'Sources:\n'
      + '[1] report.pdf, p. 12 — Finance/2024/report.pdf\n'
      + '[2] Plan https://docs.google.com/d/1 (changed since cited; quote does not match the source)',
  )
})

test('copy of a source that never resolved still keeps its number', () => {
  assert.equal(
    toPlainFootnotes(`${C} and ${A}`, ITEMS),
    '[1] and [2]\n\nSources:\n[1] (source unavailable)\n[2] report.pdf, p. 12 — Finance/2024/report.pdf',
  )
})

test('copy of a message without citations is the message', () => {
  assert.equal(toPlainFootnotes('plain [ud:<x>] text', ITEMS), 'plain [ud:<x>] text')
})
