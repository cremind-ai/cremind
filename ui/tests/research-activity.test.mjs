// The Research activity panel's state in the chat store.
//
// A research job publishes full `research_activity` snapshots from its own
// task, during and after the turn that started it. The store keeps the newest
// one per conversation: a frame naming another conversation, or older than
// what the panel shows (a replay after a reconnect, a previous job), changes
// nothing. After a reload the panel comes back from the saved message's
// `metadata.research_activity`; a saved "running" job the server no longer
// knows was cut short by a restart and shows as interrupted.
import assert from 'node:assert/strict'
import test from 'node:test'

import { flush, installBrowser, json, load, until } from './harness.mjs'

const env = installBrowser()
window.cremind = { config: { agentUrl: 'http://localhost:1515' } }
// The chat store imports Element Plus, which pulls in Vue's DOM renderer.
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

const { createPinia, setActivePinia, useChatStore, useSettingsStore } = await load('tests/entries/citation-stores.ts')

function snapshot(overrides = {}) {
  return {
    job_id: 'job001',
    conversation_id: null,
    status: 'running',
    title: 'Compile the business results in MKT-report',
    mode: 'compile',
    phase: 'Reading files',
    started_at: 1_700_000_000,
    updated_at: 1_700_000_010,
    progress: { done: 1, total: 6 },
    steps: [{ id: 's1', ts: 1_700_000_005, kind: 'read', label: 'Reading MKT/q1.xlsx (1/6)', detail: null, status: 'running' }],
    total_steps: 1,
    usage: { tokens_in: 1200, tokens_out: 300, budget: 250000 },
    summary: null,
    error: null,
    ...overrides,
  }
}

let conv = 0
function freshConversation() {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  settings.authToken = 'jwt-alice'
  const chat = useChatStore()
  const id = `r${++conv}`
  chat.trackConversation(id, 'active')
  let seq = 0
  const emit = (type, data = {}) => {
    for (const cb of globalThis.__conversationSubscribers[id]) cb({ seq: ++seq, type, data })
  }
  return { chat, id, emit, bucket: () => chat.messagesByConversation[id] ?? [] }
}

test('a snapshot lands on the panel and opens no bubble', () => {
  const { chat, id, emit, bucket } = freshConversation()
  emit('research_activity', snapshot({ conversation_id: id }))
  const state = chat.researchActivityByConversation[id]
  assert.equal(state.job_id, 'job001')
  assert.equal(state.status, 'running')
  assert.deepEqual(state.progress, { done: 1, total: 6 })
  assert.equal(state.steps.length, 1)
  assert.equal(state.updateSeq, 1)
  assert.equal(bucket().length, 0, 'panel-only: no message bubble')
  assert.equal(chat.runtimes[id]?.isStreaming ?? false, false, 'a frame outside a turn starts no run')

  chat.activeConversationId = id
  assert.equal(chat.activeResearchActivity.job_id, 'job001')
})

test('later snapshots replace the state and mark the steps that changed', () => {
  const { chat, id, emit } = freshConversation()
  emit('research_activity', snapshot())
  emit('research_activity', snapshot({
    updated_at: 1_700_000_020,
    progress: { done: 2, total: 6 },
    steps: [
      { id: 's1', ts: 1, kind: 'read', label: 'Reading MKT/q1.xlsx (1/6)', detail: null, status: 'done' },
      { id: 's2', ts: 2, kind: 'read', label: 'Reading MKT/q2.xlsx (2/6)', detail: null, status: 'running' },
    ],
    total_steps: 2,
  }))
  const state = chat.researchActivityByConversation[id]
  assert.deepEqual(state.progress, { done: 2, total: 6 })
  assert.deepEqual(state.changedIds, ['s1', 's2'])
  assert.equal(state.updateSeq, 2)

  emit('research_activity', snapshot({ updated_at: 1_700_000_030, status: 'complete', summary: '140 rows' }))
  assert.equal(chat.researchActivityByConversation[id].status, 'complete')
  assert.equal(chat.researchActivityByConversation[id].summary, '140 rows')
})

test('stale frames and frames for another conversation are ignored', () => {
  const { chat, id, emit } = freshConversation()
  emit('research_activity', snapshot({ updated_at: 1_700_000_050, status: 'complete' }))

  // An older snapshot of the same job (a replay) does not undo the finish.
  emit('research_activity', snapshot({ updated_at: 1_700_000_040, status: 'running' }))
  assert.equal(chat.researchActivityByConversation[id].status, 'complete')

  // A snapshot of an earlier job does not replace the newer job.
  emit('research_activity', snapshot({ job_id: 'job000', started_at: 1_600_000_000, updated_at: 1_700_000_060 }))
  assert.equal(chat.researchActivityByConversation[id].job_id, 'job001')

  // A frame naming another conversation changes nothing here.
  emit('research_activity', snapshot({ job_id: 'job002', conversation_id: 'somewhere-else',
    started_at: 1_800_000_000, updated_at: 1_800_000_000 }))
  assert.equal(chat.researchActivityByConversation[id].job_id, 'job001')
  assert.equal(chat.researchActivityByConversation['somewhere-else'], undefined)

  // Malformed payloads are dropped.
  emit('research_activity', { nope: true })
  assert.equal(chat.researchActivityByConversation[id].job_id, 'job001')

  // A newer job does replace it.
  emit('research_activity', snapshot({ job_id: 'job002', started_at: 1_800_000_000, updated_at: 1_800_000_001 }))
  assert.equal(chat.researchActivityByConversation[id].job_id, 'job002')
})

test('dismiss closes the panel; deleting the conversation forgets it', async () => {
  const { chat, id, emit } = freshConversation()
  emit('research_activity', snapshot({ status: 'complete' }))
  chat.dismissResearchActivity(id)
  assert.equal(chat.researchActivityByConversation[id], null)

  emit('research_activity', snapshot({ status: 'complete', updated_at: 1_700_000_099 }))
  env.route(`/api/conversations/${id}`, () => json({ success: true }))
  await chat.deleteConversation(id)
  assert.equal(id in chat.researchActivityByConversation, false)
})

function savedMessages(research) {
  return [
    { id: 'u1', conversation_id: 'c', role: 'user', content: 'compile', parts: [], thinking_steps: [],
      token_usage: null, created_at: 1_700_000_000_000, metadata: {} },
    { id: 'a1', conversation_id: 'c', role: 'agent', content: 'Working on it.', parts: [], thinking_steps: [],
      token_usage: null, created_at: 1_700_000_001_000, metadata: { research_activity: research } },
  ]
}

test('reload: a saved running job the server no longer knows shows as interrupted', async () => {
  setActivePinia(createPinia())
  useSettingsStore().authToken = 'jwt-alice'
  const chat = useChatStore()
  env.route('/api/conversations/gone/research-activity', () => json({ activity: null }))
  chat.restoreResearchActivityState('gone', savedMessages(snapshot()))
  assert.equal(chat.researchActivityByConversation.gone.status, 'running')
  await until(() => chat.researchActivityByConversation.gone.status === 'interrupted', 'coerced to interrupted')
  const call = env.callsTo('/api/conversations/gone/research-activity').at(-1)
  assert.equal(call.init.headers.Authorization, 'Bearer jwt-alice')
})

test('reload: a saved running job still live adopts the live snapshot', async () => {
  setActivePinia(createPinia())
  useSettingsStore().authToken = 'jwt-alice'
  const chat = useChatStore()
  env.route('/api/conversations/live/research-activity', () =>
    json({ activity: snapshot({ updated_at: 1_700_000_500, progress: { done: 5, total: 6 } }) }))
  chat.restoreResearchActivityState('live', savedMessages(snapshot()))
  await until(() => chat.researchActivityByConversation.live.progress.done === 5, 'live snapshot adopted')
  assert.equal(chat.researchActivityByConversation.live.status, 'running')
})

test('reload: a settled job is restored as saved, without asking the server', async () => {
  setActivePinia(createPinia())
  useSettingsStore().authToken = 'jwt-alice'
  const chat = useChatStore()
  const before = env.callsTo('/api/conversations/done/research-activity').length
  chat.restoreResearchActivityState('done', savedMessages(snapshot({ status: 'needs_clarification' })))
  await flush()
  assert.equal(chat.researchActivityByConversation.done.status, 'needs_clarification')
  assert.equal(env.callsTo('/api/conversations/done/research-activity').length, before)

  // No saved snapshot: no panel.
  chat.restoreResearchActivityState('none', savedMessages(null))
  assert.equal(chat.researchActivityByConversation.none, null)
})

test('reload: a live frame that arrives first wins over the restore check', async () => {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  settings.authToken = 'jwt-alice'
  const chat = useChatStore()
  chat.trackConversation('raced', 'active')
  let release
  env.route('/api/conversations/raced/research-activity', () =>
    new Promise(resolve => { release = () => resolve(json({ activity: null })) }))
  chat.restoreResearchActivityState('raced', savedMessages(snapshot()))
  for (const cb of globalThis.__conversationSubscribers.raced) {
    cb({ seq: 1, type: 'research_activity', data: snapshot({ updated_at: 1_700_000_900, status: 'complete' }) })
  }
  await until(() => typeof release === 'function', 'restore check sent')
  release()
  await flush()
  assert.equal(chat.researchActivityByConversation.raced.status, 'complete')
})
