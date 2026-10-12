// One sent message, one bubble.
//
// The chat store shows a message the moment it is sent, and the server then
// announces the stored row on the conversation stream (`user_message`). That
// frame has to be recognised as the bubble already on screen. On the ordinary
// path the POST answers before the row exists, so there is no id to match on:
// the frame is matched to the bubble that is still waiting for it.
import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, installBrowser, json, load, until } from './harness.mjs'

const env = installBrowser()
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

const { createPinia, setActivePinia, useChatStore, useSettingsStore } =
  await load('tests/entries/citation-stores.ts')

let conv = 0
let created = 0
/** Every message POST, in order; a test answers each one when it chooses. */
let posts = []
/** Set to a pending promise to keep conversation creates in flight. */
let createGate = Promise.resolve()

env.route('/api/conversations', async (url, init) => {
  if (init.method !== 'POST') return json({ conversations: [] })
  if (url.endsWith('/messages')) {
    const post = { url, text: JSON.parse(init.body).text, answer: deferred() }
    posts.push(post)
    return post.answer.promise
  }
  const id = `new${++created}`
  await createGate
  return json({
    conversation: {
      id, title: 'Untitled Chat', channel_id: null,
      context_id: null, task_id: null, created_at: 1, updated_at: 1,
    },
  })
})

const queued = () => json({ run_id: 'r', conversation_id: 'c', delivery: 'queued', message_id: null }, 202)
const injected = id => json({ run_id: 'r', conversation_id: 'c', delivery: 'injected', message_id: id }, 202)

/** A fresh store on an open conversation, or on the new-chat slot. */
function fresh({ newChat = false } = {}) {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  settings.authToken = 'jwt-alice'
  settings.profileId = 'alice'
  const chat = useChatStore()
  posts = []
  created = 0
  createGate = Promise.resolve()
  globalThis.__conversationSubscribers = {}
  let id = null
  if (!newChat) {
    id = `c${++conv}`
    chat.trackConversation(id, 'active')
    chat.activeConversationId = id
  }
  let seq = 0
  const emit = (type, data = {}, to = id) => {
    for (const cb of globalThis.__conversationSubscribers[to]) cb({ seq: ++seq, type, data })
  }
  const users = (of = id) =>
    (chat.messagesByConversation[of] ?? []).filter(m => m.role === 'user').map(m => m.content)
  return { chat, id, emit, users }
}

test('the stream echo of a sent message is not a second bubble', async () => {
  const { chat, emit, users } = fresh()
  const sent = chat.sendMessage('Hello')
  await until(() => posts.length === 1, 'the POST')
  posts[0].answer.resolve(queued())
  await sent
  emit('user_message', { id: 'u1', content: 'Hello' })
  assert.deepEqual(users(), ['Hello'])
})

test('surrounding whitespace the server strips does not split the bubble in two', async () => {
  const { chat, emit, users } = fresh()
  const sent = chat.sendMessage('Hello \n')
  await until(() => posts.length === 1, 'the POST')
  posts[0].answer.resolve(queued())
  await sent
  // The server stores, and announces, the stripped text.
  emit('user_message', { id: 'u1', content: 'Hello' })
  assert.deepEqual(users(), ['Hello'])
})

test('two messages sent back to back each keep exactly one bubble', async () => {
  const { chat, emit, users } = fresh()
  const first = chat.sendMessage("I'm very happy")
  const second = chat.sendMessage('happy')
  await until(() => posts.length === 2, 'both POSTs')
  // The second reached the server before the first turn had started, so both
  // are queued as turns of their own and neither answer carries a row id.
  posts[0].answer.resolve(queued())
  posts[1].answer.resolve(queued())
  await Promise.all([first, second])

  emit('user_message', { id: 'u1', content: "I'm very happy" })
  assert.deepEqual(users(), ["I'm very happy", 'happy'], 'the first echo found its own bubble')
  emit('text', { token: 'Wonderful!' })
  emit('complete', { assistant_id: 'a1' })
  emit('user_message', { id: 'u2', content: 'happy' })
  assert.deepEqual(users(), ["I'm very happy", 'happy'], 'so did the second, after the reply')
})

test('an echo that beats the POST answer still finds its bubble', async () => {
  const { chat, emit, users } = fresh()
  emit('user_message', { id: 'u0', content: 'start' })
  emit('text', { token: 'working' })

  const sent = chat.sendMessage('one more thing')
  await until(() => posts.length === 1, 'the POST')
  emit('user_message', { id: 'u1', content: 'one more thing', injected: true })
  posts[0].answer.resolve(injected('u1'))
  await sent
  assert.deepEqual(users(), ['start', 'one more thing'])
})

test('a replayed frame is recognised by the id its first delivery left behind', async () => {
  const { chat, emit, users } = fresh()
  const sent = chat.sendMessage('Hello')
  await until(() => posts.length === 1, 'the POST')
  posts[0].answer.resolve(queued())
  await sent
  emit('user_message', { id: 'u1', content: 'Hello' })
  emit('text', { token: 'Hi!' })
  emit('complete', { assistant_id: 'a1' })
  // A reconnect replays the ring under fresh sequence numbers.
  emit('user_message', { id: 'u1', content: 'Hello' })
  assert.deepEqual(users(), ['Hello'])
})

test('the same words from another tab are a message of their own', async () => {
  const { chat, emit, users } = fresh()
  const sent = chat.sendMessage('hi')
  await until(() => posts.length === 1, 'the POST')
  posts[0].answer.resolve(queued())
  await sent
  emit('user_message', { id: 'u1', content: 'hi' })
  // Typed again somewhere else while the turn runs: a different row.
  emit('user_message', { id: 'u2', content: 'hi', injected: true })
  assert.deepEqual(users(), ['hi', 'hi'])
})

test('a failed send stops waiting for an echo', async () => {
  const { chat, emit, users } = fresh()
  const sent = chat.sendMessage('hi')
  await until(() => posts.length === 1, 'the POST')
  posts[0].answer.resolve(json({ error: 'nope' }, 500))
  await sent
  // Never stored, so a later `hi` from elsewhere is not this bubble's echo.
  emit('user_message', { id: 'u9', content: 'hi' })
  assert.deepEqual(users(), ['hi', 'hi'])
})

test('two first messages in a row open one conversation, not two', async () => {
  const { chat, users } = fresh({ newChat: true })
  const first = chat.sendMessage('Hello')
  const second = chat.sendMessage('Hello')
  await until(() => posts.length === 2, 'both POSTs')
  posts.forEach(post => post.answer.resolve(queued()))
  await Promise.all([first, second])
  await flush()

  assert.equal(created, 1, 'one conversation was created')
  assert.equal(chat.activeConversationId, 'new1')
  assert.deepEqual(posts.map(p => p.url.split('/').at(-2)), ['new1', 'new1'])
  assert.deepEqual(users('new1'), ['Hello', 'Hello'])
})

test("a create still in flight is not handed to the next profile", async () => {
  const { chat } = fresh({ newChat: true })
  const gate = deferred()
  createGate = gate.promise
  const alice = chat.sendMessage('Hello')
  await until(() => created === 1, "alice's create")

  // Bob signs in on this tab before alice's conversation exists.
  useSettingsStore().profileId = 'bob'
  const bob = chat.sendMessage('Hello')
  await until(() => created === 2, 'a create of his own')

  gate.resolve()
  await until(() => posts.length === 2, 'both POSTs')
  posts.forEach(post => post.answer.resolve(queued()))
  await Promise.all([alice, bob])
  assert.deepEqual(posts.map(p => p.url.split('/').at(-2)).sort(), ['new1', 'new2'])
})
