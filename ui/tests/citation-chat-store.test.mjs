// Where an answer's citations land in the chat store, live and on reload,
// and how the ones that never arrived get resolved.
//
// The server verifies an answer's citation tokens when it saves the answer and
// publishes the result as a `citations` stream frame (and stores it as
// `metadata.citations`). The frame can arrive before `complete` — when the
// live bubble still has only its optimistic id — or after it, and it must
// land on the answer it describes: never open a bubble of its own, never
// badge a different answer.
import assert from 'node:assert/strict'
import test from 'node:test'

import { flush, installBrowser, json, load, until } from './harness.mjs'

const env = installBrowser()
// Where the settings store finds the backend (runtimeConfig reads the bridge
// first, so import.meta.env is never touched in the bundle).
window.cremind = { config: { agentUrl: 'http://localhost:1515' } }
// The chat store imports Element Plus (for ElNotification), which pulls in
// Vue's DOM renderer; that creates a <template> element when it loads.
function fakeElement() {
  return {
    style: {}, dataset: {}, childNodes: [], classList: { add() {}, remove() {} },
    setAttribute() {}, removeAttribute() {}, appendChild() {}, insertBefore() {},
    addEventListener() {}, removeEventListener() {},
  }
}
document.createElement = fakeElement
document.createElementNS = fakeElement
document.createTextNode = fakeElement
document.createComment = fakeElement
document.body = fakeElement()
document.head = fakeElement()
document.documentElement = fakeElement()
window.navigator = globalThis.navigator

const { createPinia, setActivePinia, useChatStore, useSettingsStore, useCitationsStore } =
  await load('tests/entries/citation-stores.ts')

const A = '[doc:k7m2xq9a#3f9c2e1b]'
const B = '[doc:p4r8st0v]'

const META = {
  v: 1,
  unverified: 1,
  items: [
    {
      n: 1, token: A, status: 'verified', quote_status: 'exact',
      file: { fid: 'k7m2xq9a', name: 'report.pdf', rel_path: 'Finance/report.pdf', source: 'local', kind: 'pdf', web_link: null },
      locator: { page: 12 }, locator_label: 'p. 12', snippet: 'Revenue grew 12%.',
    },
    {
      n: 2, token: B, status: 'stale', quote_status: null,
      file: { fid: 'p4r8st0v', name: 'notes.md', rel_path: 'notes.md', source: 'local', kind: 'markdown', web_link: null },
      locator: {}, locator_label: '', snippet: '',
    },
  ],
}

let conv = 0
/** A fresh store, subscribed to a fresh conversation; returns an emitter. */
function freshConversation() {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  settings.authToken = 'jwt-alice'
  const chat = useChatStore()
  const id = `c${++conv}`
  chat.trackConversation(id, 'active')
  let seq = 0
  const emit = (type, data = {}) => {
    for (const cb of globalThis.__conversationSubscribers[id]) cb({ seq: ++seq, type, data })
  }
  return { chat, id, emit, bucket: () => chat.messagesByConversation[id] ?? [] }
}

function runTurn(emit, { text, assistantId, citationsBeforeComplete }) {
  emit('user_message', { id: `u-${assistantId}`, content: 'question' })
  emit('text', { token: text })
  if (citationsBeforeComplete) emit('citations', { citations: META, assistant_id: assistantId })
  emit('complete', { assistant_id: assistantId })
}

test('a frame before `complete` lands on the bubble that is still streaming', () => {
  const { emit, bucket } = freshConversation()
  runTurn(emit, { text: `Revenue grew ${A}; see ${B}.`, assistantId: 'a1', citationsBeforeComplete: true })

  const answers = bucket().filter(m => m.role === 'assistant')
  assert.equal(answers.length, 1, 'the frame opened no bubble of its own')
  const [answer] = answers
  assert.equal(answer.backendId, 'a1')
  assert.deepEqual(answer.citations.items.map(i => [i.token, i.status]), [[A, 'verified'], [B, 'stale']])
  assert.equal(answer.citations.unverified, 1)
  assert.equal(answer.citations.items[0].file.name, 'report.pdf')
})

test('a late frame finds its answer by persisted id, not by being last', () => {
  const { emit, bucket } = freshConversation()
  runTurn(emit, { text: `First ${A}`, assistantId: 'a1' })
  runTurn(emit, { text: 'Second, no citations', assistantId: 'a2' })

  emit('citations', { citations: META, assistant_id: 'a1' })

  const [first, second] = bucket().filter(m => m.role === 'assistant')
  assert.equal(first.citations?.items.length, 2)
  assert.equal(second.citations, undefined)
})

test('without an id, a frame after `complete` belongs to the answer that just finished', () => {
  const { emit, bucket } = freshConversation()
  runTurn(emit, { text: `Only ${A}`, assistantId: 'a1' })
  emit('citations', { citations: META, assistant_id: null })
  assert.equal(bucket().filter(m => m.role === 'assistant')[0].citations?.items.length, 2)
})

test('an id that matches nothing badges nothing and opens nothing', () => {
  const { emit, bucket } = freshConversation()
  runTurn(emit, { text: `Only ${A}`, assistantId: 'a1' })
  const before = bucket().length
  emit('citations', { citations: META, assistant_id: 'somebody-else' })
  assert.equal(bucket().length, before)
  assert.equal(bucket().find(m => m.role === 'assistant').citations, undefined)
})

test('a malformed payload is ignored, and a bogus status never reads as verified', () => {
  const { emit, bucket } = freshConversation()
  emit('user_message', { id: 'u', content: 'q' })
  emit('text', { token: `x ${A}` })
  emit('citations', { citations: 'nope', assistant_id: null })
  const answer = bucket().find(m => m.role === 'assistant')
  assert.equal(answer.citations, undefined)

  emit('citations', {
    citations: { v: 1, items: [{ token: A, status: 'totally-fine' }, { token: '<b>x</b>', status: 'verified' }] },
    assistant_id: null,
  })
  assert.deepEqual(answer.citations.items.map(i => [i.token, i.status]), [[A, 'invalid']])
  assert.equal(answer.citations.unverified, 1)
})

test('reload reads metadata.citations for agent rows only', () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const agent = chat.mapBackendMessage({
    id: 'a1', conversation_id: 'c', role: 'agent', content: `x ${A}`, parts: [],
    thinking_steps: [], token_usage: null, created_at: 1_700_000_000_000,
    metadata: { citations: META },
  })
  assert.deepEqual(agent.citations.items.map(i => i.token), [A, B])
  const user = chat.mapBackendMessage({
    id: 'u1', conversation_id: 'c', role: 'user', content: `x ${A}`, parts: [],
    thinking_steps: [], token_usage: null, created_at: 1_700_000_000_000,
    metadata: { citations: META },
  })
  assert.equal(user.citations, undefined)
})

// ── answers saved without citations: resolved on demand ─────────────────────

test('tokens asked for in one tick go out as one request, once, scoped to the conversation', async () => {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  settings.authToken = 'jwt-alice'
  settings.profileId = 'alice'
  const store = useCitationsStore()
  env.route('/api/documentation-search/citations/resolve', (_url, init) => {
    const body = JSON.parse(init.body)
    return json({
      items: Object.fromEntries(body.tokens.map(token => [token, { ...META.items[0], token, status: 'verified_elsewhere' }])),
    })
  })
  const before = env.callsTo('/api/documentation-search/citations/resolve').length

  await Promise.all([store.ensure('c9', [A]), store.ensure('c9', [B, A])])
  await flush()

  const calls = env.callsTo('/api/documentation-search/citations/resolve').slice(before)
  assert.equal(calls.length, 1, 'one request for both bubbles')
  assert.deepEqual(JSON.parse(calls[0].init.body), { tokens: [A, B], conversation_id: 'c9' })
  assert.equal(calls[0].init.headers.Authorization, 'Bearer jwt-alice')
  assert.equal(store.itemFor('c9', A).status, 'verified_elsewhere')
  assert.equal(store.itemFor('c10', A), undefined, 'another conversation is another scope')

  await store.ensure('c9', [A, B])
  assert.equal(env.callsTo('/api/documentation-search/citations/resolve').length - before, 1, 'never asked twice')
})

test("a profile switch cannot read the previous profile's answers", async () => {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  settings.authToken = 'jwt-bob'
  settings.profileId = 'bob'
  const store = useCitationsStore()
  env.route('/api/documentation-search/citations/resolve', () => json({ items: { [A]: META.items[0] } }))
  await store.ensure('shared-id', [A])
  await until(() => store.itemFor('shared-id', A), 'bob resolved')

  settings.profileId = 'carol'
  settings.authToken = 'jwt-carol'
  assert.equal(store.itemFor('shared-id', A), undefined)
})
