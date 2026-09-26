// The composer's Search tools control, below the component: the REST client
// (409/400 mapping), the client-protocol marker the fetch wrapper stamps, and
// the store every control shares — optimistic toggles that revert on failure,
// conflicts that adopt the other writer's state, saves that the composer waits
// for before it sends, the new-chat draft that rides the conversation create,
// and nothing surviving a profile switch.
import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, installBrowser, json, load, until } from './harness.mjs'

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

const M = await load('tests/entries/search-tools.ts')

const BASE = 'http://localhost:1515'
const ALL = ['documentation_search', 'cremind_documentation_search', 'memory_search', 'web_search']

const ROW = {
  documentation_search: { label: 'Documentation search', description: 'Your own indexed files.' },
  cremind_documentation_search: { label: 'Cremind documentation search', description: "Cremind's own manuals." },
  memory_search: { label: 'Memory search', description: 'Remembered facts.' },
  web_search: { label: 'Web search', description: 'The public internet.' },
}

/** A server row list; `omit` leaves ids out entirely (a hidden source). */
function rows({ omit = [], unavailable = {}, notes = {} } = {}) {
  return ALL.filter(id => !omit.includes(id)).map(id => ({
    id,
    ...ROW[id],
    available: !(id in unavailable),
    unavailable_reason: unavailable[id] ?? notes[id] ?? null,
  }))
}

function serverState(overrides = {}) {
  const enabled = overrides.enabled ?? ALL
  return {
    version: 1,
    enabled,
    effective: enabled,
    tools: rows(),
    pending_next_response: false,
    cache_warning: null,
    ...overrides,
  }
}

let seq = 0
/** A fresh Pinia with a signed-in profile, and a conversation id no other test used. */
function fresh(profile = 'alice') {
  M.setActivePinia(M.createPinia())
  const settings = M.useSettingsStore()
  settings.profileId = profile
  settings.authToken = `jwt-${profile}`
  const store = M.useSearchToolsStore()
  const id = `c${++seq}`
  return { settings, store, id, target: { kind: 'conversation', id } }
}

/** Route one conversation's search-tools endpoint to `get` / `put` handlers. */
function serveConversation(id, { get, put }) {
  env.route(`/api/conversations/${id}/search-tools`, async (_url, init) => {
    if ((init.method ?? 'GET') === 'PUT') return put(JSON.parse(init.body), init)
    return get(init)
  })
}

function putsTo(id) {
  return env.callsTo(`/api/conversations/${id}/search-tools`).filter(c => c.init.method === 'PUT')
}

function getsTo(id) {
  return env.callsTo(`/api/conversations/${id}/search-tools`).filter(c => (c.init.method ?? 'GET') === 'GET')
}

// ── the REST client ─────────────────────────────────────────────────────────

test('GET and PUT hit the conversation and room endpoints with the token', async () => {
  const bodies = []
  env.route('/api/conversations/api-1/search-tools', (_url, init) => {
    if (init.method === 'PUT') bodies.push(JSON.parse(init.body))
    return json(serverState({ version: 4 }))
  })
  env.route('/api/group-chats/room-1/search-tools', () => json(serverState({ version: 9 })))

  const got = await M.fetchSearchTools(BASE, 'jwt', 'conversation', 'api-1')
  assert.equal(got.version, 4)
  const room = await M.fetchSearchTools(BASE, 'jwt', 'group', 'room-1')
  assert.equal(room.version, 9)
  await M.saveSearchTools(BASE, 'jwt', 'conversation', 'api-1', 4, ['web_search', 'memory_search'])
  await M.saveSearchTools(BASE, 'jwt', 'conversation', 'api-1', 5, null)

  assert.deepEqual(bodies, [
    { version: 4, enabled: ['memory_search', 'web_search'] },
    { version: 5, enabled: null },
  ], 'ids go in priority order; null asks for the defaults')
  const [first] = env.callsTo('/api/conversations/api-1/search-tools')
  assert.equal(first.init.headers.Authorization, 'Bearer jwt')
})

test('the new-chat defaults come from /api/search-tools', async () => {
  env.route(`${BASE}/api/search-tools`, () => json(serverState({ version: 0, tools: rows({ omit: ['documentation_search'] }) })))
  const got = await M.fetchNewChatSearchTools(BASE, 'jwt')
  assert.equal(got.version, 0)
  assert.deepEqual(got.tools.map(r => r.id), ['cremind_documentation_search', 'memory_search', 'web_search'])
})

test('a 409 carries the current state to adopt', async () => {
  const current = serverState({ version: 7, enabled: ['web_search'] })
  env.route('/api/conversations/api-409/search-tools', () => json({
    error: 'VersionConflict',
    message: 'The search tools were changed elsewhere; showing the current choice.',
    state: current,
  }, 409))
  await assert.rejects(
    M.saveSearchTools(BASE, 'jwt', 'conversation', 'api-409', 3, []),
    (error) => {
      assert.ok(error instanceof M.SearchToolsConflictError)
      assert.equal(error.status, 409)
      assert.equal(error.state.version, 7)
      assert.deepEqual(error.state.enabled, ['web_search'])
      assert.match(error.message, /changed elsewhere/)
      return true
    },
  )
})

test('a 400 and an unreachable server surface as plain errors with a message', async () => {
  env.route('/api/conversations/api-400/search-tools', () => json({
    error: 'InvalidSelection', message: "unknown search tool 'x'",
  }, 400))
  await assert.rejects(
    M.saveSearchTools(BASE, 'jwt', 'conversation', 'api-400', 1, []),
    (error) => {
      assert.ok(error instanceof M.SearchToolsError)
      assert.ok(!(error instanceof M.SearchToolsConflictError))
      assert.equal(error.status, 400)
      assert.equal(error.code, 'InvalidSelection')
      assert.equal(error.message, "unknown search tool 'x'")
      return true
    },
  )
  // No route: the harness fetch rejects like a refused connection.
  await assert.rejects(
    M.fetchSearchTools(BASE, 'jwt', 'conversation', 'nowhere-at-all'),
    (error) => error instanceof M.SearchToolsError && error.status === 0,
  )
})

test('a 409 without a readable state is an ordinary error, not a conflict', async () => {
  env.route('/api/conversations/api-409b/search-tools', () => json({ error: 'VersionConflict', message: 'stale' }, 409))
  await assert.rejects(
    M.saveSearchTools(BASE, 'jwt', 'conversation', 'api-409b', 1, []),
    (error) => !(error instanceof M.SearchToolsConflictError) && error.status === 409,
  )
})

test('answers are narrowed: unknown ids dropped, order fixed, garbage refused', () => {
  const got = M.normalizeSearchToolsState({
    version: 2,
    enabled: ['web_search', 'a_future_source', 'documentation_search', 'web_search'],
    effective: ['web_search'],
    tools: [
      { id: 'web_search', label: 'Web search', description: '', available: true, unavailable_reason: null },
      { id: 'a_future_source', label: '?', description: '', available: true, unavailable_reason: null },
      { id: 'memory_search', label: 'Memory search', description: '', available: true,
        unavailable_reason: 'Availability varies by agent in this room.' },
    ],
    pending_next_response: true,
    cache_warning: '',
  })
  assert.deepEqual(got.enabled, ['documentation_search', 'web_search'])
  assert.deepEqual(got.tools.map(r => r.id), ['memory_search', 'web_search'])
  assert.equal(got.tools[0].available, true)
  assert.equal(got.tools[0].unavailable_reason, 'Availability varies by agent in this room.')
  assert.equal(got.pending_next_response, true)
  assert.equal(got.cache_warning, null)
  assert.equal(M.normalizeSearchToolsState('<html>'), null)
  assert.equal(M.normalizeSearchToolsState({ enabled: [], tools: [] }), null, 'no version')
})

// ── the client-protocol marker ──────────────────────────────────────────────

test('backend requests carry the client-protocol marker, and keep their own headers', () => {
  const origin = 'http://localhost:1515'
  const init = M.withClientProtocol(`${origin}/api/tools/web_search/enabled`, {
    method: 'PUT', headers: { Authorization: 'Bearer x', 'Content-Type': 'application/json' }, body: '{}',
  }, origin)
  const headers = new Headers(init.headers)
  assert.equal(headers.get(M.CLIENT_PROTOCOL_HEADER), M.CLIENT_PROTOCOL_VERSION)
  assert.equal(M.CLIENT_PROTOCOL_VERSION, '2')
  assert.equal(headers.get('Authorization'), 'Bearer x')
  assert.equal(init.method, 'PUT')
  assert.equal(init.body, '{}')

  // Relative URLs resolve against the page, which is the backend here.
  const relative = M.withClientProtocol('/api/conversations', undefined, origin)
  assert.equal(new Headers(relative.headers).get(M.CLIENT_PROTOCOL_HEADER), '2')

  // A Request input keeps its own headers when init supplies none.
  const request = new Request(`${origin}/api/clean`, { method: 'POST', headers: { Authorization: 'Bearer r' } })
  const fromRequest = new Headers(M.withClientProtocol(request, undefined, origin).headers)
  assert.equal(fromRequest.get('Authorization'), 'Bearer r')
  assert.equal(fromRequest.get(M.CLIENT_PROTOCOL_HEADER), '2')
})

test('no marker off the backend origin, on the HTTPS hand-off, or without an origin', () => {
  const origin = 'http://localhost:1515'
  const untouched = { headers: { Authorization: 'Bearer x' } }
  assert.equal(M.withClientProtocol('https://www.googleapis.com/drive/v3/files', untouched, origin), untouched)
  assert.equal(M.withClientProtocol('https://hub.cremind.io/api/skills', untouched, origin), untouched)
  // The hand-off is cross-origin by nature and never gated: leave its
  // preflight exactly as it was, on either origin.
  assert.equal(M.withClientProtocol(`${origin}/api/tls/status`, untouched, origin), untouched)
  assert.equal(M.withClientProtocol('https://localhost:1515/api/tls/handoff/redeem', untouched, origin), untouched)
  assert.equal(M.withClientProtocol(`${origin}/api/tools`, untouched, ''), untouched)
  // Already marked (by an explicit caller): not overridden.
  const marked = { headers: { [M.CLIENT_PROTOCOL_HEADER]: '3' } }
  assert.equal(M.withClientProtocol(`${origin}/api/tools`, marked, origin), marked)
})

// ── the store: optimistic saves ─────────────────────────────────────────────

test('rows come only from the server: no Documentation search until it says so', async () => {
  const { store, id, target } = fresh()
  const gate = deferred()
  serveConversation(id, { get: () => gate.promise })

  const loading = store.load(target)
  let view = store.viewFor(target)
  assert.equal(view.loaded, false)
  assert.deepEqual(view.rows, [], 'nothing assumed while the answer is on its way')

  gate.resolve(json(serverState({
    tools: rows({ omit: ['documentation_search'], unavailable: { web_search: 'Turned off in Tools settings.' } }),
  })))
  await loading
  view = store.viewFor(target)
  assert.equal(view.loaded, true)
  assert.deepEqual(view.rows.map(r => [r.rank, r.id]), [
    [1, 'cremind_documentation_search'], [2, 'memory_search'], [3, 'web_search'],
  ])
  const web = view.rows[2]
  assert.equal(web.available, false)
  assert.equal(web.reason, 'Turned off in Tools settings.')
  assert.equal(web.checked, true, 'the stored desire survives an outage')
  assert.equal(view.enabledCount, 2)
  assert.equal(view.availableCount, 2)
})

test('a toggle shows at once, saves with the known version, and adopts the answer', async () => {
  const { store, id, target } = fresh()
  const reply = deferred()
  serveConversation(id, {
    get: () => json(serverState({ version: 3 })),
    put: () => reply.promise,
  })
  await store.load(target)

  store.toggle(target, 'web_search')
  let view = store.viewFor(target)
  assert.equal(view.rows.find(r => r.id === 'web_search').checked, false, 'optimistic')
  assert.equal(view.saving, true)
  assert.equal(store.hasPendingSave(target), true)
  const [put] = putsTo(id)
  assert.deepEqual(JSON.parse(put.init.body), {
    version: 3, enabled: ['documentation_search', 'cremind_documentation_search', 'memory_search'],
  })

  const without = ALL.filter(x => x !== 'web_search')
  reply.resolve(json(serverState({ version: 4, enabled: without, effective: without })))
  assert.equal(await store.settle(target), true)
  view = store.viewFor(target)
  assert.equal(view.saving, false)
  assert.equal(view.enabledCount, 3)
  assert.equal(store.entries[`alice|conversation:${id}`].state.version, 4)
  assert.equal(store.hasPendingSave(target), false)
})

test('a failed save reverts the toggle, shows the error, and reports failure to the composer', async () => {
  const { store, id, target } = fresh()
  serveConversation(id, {
    get: () => json(serverState({ version: 2 })),
    put: () => json({ error: 'Forbidden', message: 'This conversation belongs to another profile.' }, 403),
  })
  await store.load(target)

  store.toggle(target, 'memory_search')
  assert.equal(store.viewFor(target).rows.find(r => r.id === 'memory_search').checked, false)
  assert.equal(await store.settle(target), false, 'the composer keeps its draft')

  const view = store.viewFor(target)
  assert.equal(view.rows.find(r => r.id === 'memory_search').checked, true, 'reverted')
  assert.equal(view.error, 'This conversation belongs to another profile.')
  assert.equal(view.saving, false)

  store.dismissMessages(target)
  assert.equal(store.viewFor(target).error, null)
})

test('a conflict adopts the other writer\'s state and says it changed elsewhere', async () => {
  const { store, id, target } = fresh()
  const theirs = ['web_search']
  serveConversation(id, {
    get: () => json(serverState({ version: 5 })),
    put: () => json({
      error: 'VersionConflict',
      message: 'The search tools were changed elsewhere; showing the current choice.',
      state: serverState({ version: 6, enabled: theirs, effective: theirs }),
    }, 409),
  })
  await store.load(target)

  store.toggle(target, 'memory_search')
  assert.equal(await store.settle(target), false)
  const view = store.viewFor(target)
  assert.deepEqual(view.enabled, theirs)
  assert.equal(view.conflict, 'The search tools were changed elsewhere; showing the current choice.')
  assert.equal(view.error, null)
  assert.equal(store.entries[`alice|conversation:${id}`].state.version, 6)
  assert.equal(putsTo(id).length, 1, 'the stale choice is not retried over the newer one')
})

test('rapid toggles coalesce into sequential saves, each on the version before it', async () => {
  const { store, id, target } = fresh()
  const replies = [deferred(), deferred()]
  let puts = 0
  serveConversation(id, {
    get: () => json(serverState({ version: 1 })),
    put: (body) => {
      const reply = replies[puts++]
      return reply.promise.then(() => json(serverState({
        version: body.version + 1, enabled: body.enabled, effective: body.enabled,
      })))
    },
  })
  await store.load(target)

  store.toggle(target, 'web_search')
  store.toggle(target, 'memory_search')
  store.toggle(target, 'cremind_documentation_search')
  assert.equal(putsTo(id).length, 1, 'one request in flight, the rest wait')
  assert.deepEqual(store.viewFor(target).enabled, ['documentation_search'])

  const settled = store.settle(target)
  replies[0].resolve()
  await until(() => putsTo(id).length === 2, 'the follow-up save')
  assert.deepEqual(JSON.parse(putsTo(id)[1].init.body), { version: 2, enabled: ['documentation_search'] })
  replies[1].resolve()
  assert.equal(await settled, true)
  assert.equal(putsTo(id).length, 2, 'the intermediate selection was never sent on its own')
  assert.deepEqual(store.viewFor(target).enabled, ['documentation_search'])
})

test('ending on what the in-flight save already asked for sends nothing more', async () => {
  const { store, id, target } = fresh()
  const reply = deferred()
  serveConversation(id, {
    get: () => json(serverState({ version: 1 })),
    put: () => reply.promise,
  })
  await store.load(target)
  // off (sent), on, off again — the last matches the save already in flight.
  store.toggle(target, 'web_search')
  store.toggle(target, 'web_search')
  store.toggle(target, 'web_search')
  const without = ALL.filter(x => x !== 'web_search')
  reply.resolve(json(serverState({ version: 2, enabled: without, effective: without })))
  assert.equal(await store.settle(target), true)
  assert.equal(putsTo(id).length, 1)
  assert.deepEqual(store.viewFor(target).enabled, without)
  assert.equal(store.viewFor(target).saving, false)
})

test('a save during a response says it applies from the next one, until a response adopts it', async () => {
  const { store, id, target } = fresh()
  let saved = false
  let adopted = false
  const warning = 'Changing search tools may reduce prompt-cache reuse on the next response and increase input-token cost.'
  serveConversation(id, {
    get: () => json(serverState({
      version: saved ? 2 : 1, pending_next_response: saved && !adopted,
    })),
    put: (body) => (saved = true) && json(serverState({
      version: 2, enabled: body.enabled, effective: body.enabled,
      pending_next_response: true, cache_warning: warning,
    })),
  })
  await store.load(target)
  store.toggle(target, 'memory_search', { running: true })
  await store.settle(target)

  let view = store.viewFor(target)
  assert.equal(view.pendingNextResponse, true)
  assert.equal(view.showPendingNotice, true)
  assert.equal(M.PENDING_NOTICE, 'Saved for the next response. The current response keeps its existing search tools.')
  assert.equal(view.cacheWarning, warning)

  // The next response ran on the saved selection: the server clears the flag.
  adopted = true
  store.refreshIfPending(target)
  await until(() => !store.viewFor(target).pendingNextResponse, 'the refetch')
  view = store.viewFor(target)
  assert.equal(view.showPendingNotice, false)
})

test('a save with no response running is not announced as pending-for-next', async () => {
  const { store, id, target } = fresh()
  serveConversation(id, {
    get: () => json(serverState({ version: 1 })),
    put: (body) => json(serverState({ version: 2, enabled: body.enabled, effective: body.enabled, pending_next_response: true })),
  })
  await store.load(target)
  store.toggle(target, 'memory_search', { running: false })
  await store.settle(target)
  const view = store.viewFor(target)
  assert.equal(view.pendingNextResponse, true, 'the button still marks it')
  assert.equal(view.showPendingNotice, false, 'but the running-response notice is not shown')
})

test('an unavailable source cannot be toggled; a room note leaves its row usable', async () => {
  const { store, id, target } = fresh()
  serveConversation(id, {
    get: () => json(serverState({
      tools: rows({ unavailable: { web_search: 'Turned off in Tools settings.' },
        notes: { memory_search: 'Availability varies by agent in this room.' } }),
    })),
    put: (body) => json(serverState({ version: 2, enabled: body.enabled, effective: body.enabled })),
  })
  await store.load(target)
  store.toggle(target, 'web_search')
  assert.equal(putsTo(id).length, 0)

  const memory = store.viewFor(target).rows.find(r => r.id === 'memory_search')
  assert.equal(memory.available, true)
  assert.equal(memory.note, 'Availability varies by agent in this room.')
  assert.equal(memory.reason, null)
  store.toggle(target, 'memory_search')
  await store.settle(target)
  assert.equal(putsTo(id).length, 1)
})

test('reset sends null, and is a no-op when already on the defaults', async () => {
  const { store, id, target } = fresh()
  serveConversation(id, {
    get: () => json(serverState({ version: 3, enabled: ['web_search'], effective: ['web_search'] })),
    put: (body) => json(serverState({ version: 4, enabled: body.enabled ?? ALL })),
  })
  await store.load(target)
  assert.equal(store.viewFor(target).isDefault, false)
  store.resetToDefaults(target)
  assert.equal(store.viewFor(target).isDefault, true, 'optimistic')
  await store.settle(target)
  assert.deepEqual(JSON.parse(putsTo(id)[0].init.body), { version: 3, enabled: null })
  store.resetToDefaults(target)
  await store.settle(target)
  assert.equal(putsTo(id).length, 1)
})

// ── stream frames ───────────────────────────────────────────────────────────

test('a conversation `search_tools` frame re-reads only a version this tab lacks', async () => {
  const { store, id, target } = fresh()
  let version = 2
  serveConversation(id, { get: () => json(serverState({ version })) })
  const chat = M.useChatStore()
  chat.trackConversation(id, 'active')
  await store.load(target)
  assert.equal(getsTo(id).length, 1)

  const emit = (type, data) => {
    for (const cb of globalThis.__conversationSubscribers[id]) cb({ seq: Math.random(), type, data })
  }
  emit('search_tools', { version: 2 })
  await flush()
  assert.equal(getsTo(id).length, 1, 'already holds version 2')

  version = 3
  emit('search_tools', { version: 3 })
  await until(() => store.entries[`alice|conversation:${id}`].state.version === 3, 'the re-read')
  assert.equal(getsTo(id).length, 2)
})

test('a frame for a conversation no control shows is ignored', async () => {
  const { store, id } = fresh()
  serveConversation(id, { get: () => json(serverState()) })
  store.noteRemoteVersion({ kind: 'conversation', id }, 9)
  await flush()
  assert.equal(getsTo(id).length, 0)
})

test('a room `search_tools` frame re-reads the room\'s state', async () => {
  const { store } = fresh()
  const groupId = `g${++seq}`
  let version = 1
  env.route(`/api/group-chats/${groupId}/search-tools`, () => json(serverState({ version })))
  const target = { kind: 'group', id: groupId }
  await store.load(target)
  version = 2
  await M.useGroupChatStore().handleFrame(groupId, { type: 'search_tools', data: { version: 2 } })
  await until(() => store.entries[`alice|group:${groupId}`].state.version === 2, 'the room re-read')
})

// ── the new-chat draft ──────────────────────────────────────────────────────

function serveCreate(id, captured) {
  env.route(`${BASE}/api/conversations`, async (url, init) => {
    if (url.endsWith('/api/conversations') && init.method === 'POST') {
      captured.create = JSON.parse(init.body)
      return json({ conversation: {
        id, title: 'Untitled Chat', profile: 'alice', channel_id: null, context_id: null,
        task_id: null, created_at: 1, updated_at: 1,
      } }, 201)
    }
    if (url.endsWith(`/api/conversations/${id}/messages`)) {
      captured.message = JSON.parse(init.body)
      return json({ run_id: 'r1', conversation_id: id })
    }
    throw new TypeError(`unexpected ${url}`)
  })
}

test('new-chat toggles stay local, then ride the create with the first message', async () => {
  const { store, id } = fresh()
  env.route(`${BASE}/api/search-tools`, () => json(serverState({ version: 0 })))
  const target = M.NEW_CHAT_TARGET
  await store.load(target)
  const before = env.calls.length
  store.toggle(target, 'web_search')
  assert.equal(env.calls.length, before, 'no request for a conversation that does not exist yet')
  assert.equal(store.viewFor(target).rows.find(r => r.id === 'web_search').checked, false)
  assert.equal(store.hasPendingSave(target), false)
  assert.equal(await store.settle(target), true)

  const captured = {}
  serveCreate(id, captured)
  const chat = M.useChatStore()
  await chat.sendMessage('hello')
  assert.deepEqual(captured.create.search_tools,
    ['documentation_search', 'cremind_documentation_search', 'memory_search'])
  assert.equal('search_tools' in captured.message, false, 'the message never resends the selection')
  assert.equal(chat.activeConversationId, id)
  assert.equal(store.newChatSelection(), null, 'the next new chat starts from the defaults')
})

test('an attachment upload creates the conversation with the draft too; untouched means null', async () => {
  const { store, id } = fresh()
  const captured = {}
  serveCreate(id, captured)
  const chat = M.useChatStore()
  assert.equal(await chat.ensureConversation(), id)
  assert.equal(captured.create.search_tools, null)

  const second = fresh()
  env.route(`${BASE}/api/search-tools`, () => json(serverState({ version: 0 })))
  await second.store.load(M.NEW_CHAT_TARGET)
  second.store.toggle(M.NEW_CHAT_TARGET, 'memory_search')
  second.store.toggle(M.NEW_CHAT_TARGET, 'memory_search')
  assert.equal(second.store.newChatSelection(), null, 'every source ticked again is the defaults')
  second.store.toggle(M.NEW_CHAT_TARGET, 'documentation_search')
  const again = {}
  serveCreate(second.id, again)
  await M.useChatStore().ensureConversation()
  assert.deepEqual(again.create.search_tools, ['cremind_documentation_search', 'memory_search', 'web_search'])
})

test('a failed create keeps the draft for the retry', async () => {
  const { store } = fresh()
  env.route(`${BASE}/api/search-tools`, () => json(serverState({ version: 0 })))
  await store.load(M.NEW_CHAT_TARGET)
  store.toggle(M.NEW_CHAT_TARGET, 'web_search')
  env.route(`${BASE}/api/conversations`, () => json({ error: 'nope' }, 500))
  assert.equal(await M.useChatStore().ensureConversation(), null)
  assert.deepEqual(store.newChatSelection(), ALL.filter(x => x !== 'web_search'))
})

// ── profiles ────────────────────────────────────────────────────────────────

test('a profile switch clears everything, and a late answer for the old profile is dropped', async () => {
  const { settings, store, id, target } = fresh('alice')
  env.route(`${BASE}/api/search-tools`, () => json(serverState({ version: 0 })))
  await store.load(M.NEW_CHAT_TARGET)
  store.toggle(M.NEW_CHAT_TARGET, 'web_search')
  const gate = deferred()
  serveConversation(id, { get: () => gate.promise })
  const late = store.load(target)

  store.resetForProfileSwitch()
  settings.profileId = 'bob'
  settings.authToken = 'jwt-bob'
  gate.resolve(json(serverState({ version: 8 })))
  await late

  assert.deepEqual(store.entries, {})
  assert.equal(store.newChatSelection(), null)
  assert.equal(store.viewFor(target).loaded, false, "alice's conversation shows nothing to bob")
})

test('two profiles never see each other\'s selection, even without a reset', async () => {
  const { settings, store, id, target } = fresh('alice')
  serveConversation(id, { get: (init) => json(serverState({
    version: init.headers.Authorization === 'Bearer jwt-alice' ? 1 : 2,
    enabled: init.headers.Authorization === 'Bearer jwt-alice' ? ['web_search'] : ALL,
  })) })
  await store.load(target)
  assert.deepEqual(store.viewFor(target).enabled, ['web_search'])
  settings.profileId = 'bob'
  settings.authToken = 'jwt-bob'
  assert.equal(store.viewFor(target).loaded, false)
  await store.load(target)
  assert.deepEqual(store.viewFor(target).enabled, ALL)
  settings.profileId = 'alice'
  assert.deepEqual(store.viewFor(target).enabled, ['web_search'])
})

test('the chat store\'s profile reset clears search tools too', async () => {
  const { store, id, target } = fresh()
  serveConversation(id, { get: () => json(serverState()) })
  env.route(`${BASE}/.well-known/agent-card.json`, () => json({ name: 'Agent', url: BASE }))
  env.route(`${BASE}/api/conversations?`, () => json({ conversations: [] }))
  await store.load(target)
  assert.equal(store.viewFor(target).loaded, true)
  await M.useChatStore().resetForProfileSwitch()
  assert.equal(store.viewFor(target).loaded, false)
})

// ── which conversation a composer edits ─────────────────────────────────────

test('the event-run drawer edits its run\'s conversation, never the open chat', () => {
  assert.deepEqual(M.composerSearchToolsTarget('run-conv', 'open-chat'), { kind: 'conversation', id: 'run-conv' })
  assert.equal(M.composerSearchToolsTarget(null, 'open-chat'), null, 'a run without a conversation shows no control')
  assert.deepEqual(M.composerSearchToolsTarget(undefined, 'open-chat'), { kind: 'conversation', id: 'open-chat' })
  assert.deepEqual(M.composerSearchToolsTarget(undefined, null), { kind: 'new' })
  assert.equal(M.searchToolsKey({ kind: 'group', id: 'g' }), 'group:g')
})

test('saves for the drawer\'s conversation and the open chat are independent', async () => {
  const { store } = fresh()
  const runConv = `run-${++seq}`
  const openChat = `open-${++seq}`
  const hold = deferred()
  serveConversation(runConv, {
    get: () => json(serverState({ version: 1 })),
    put: () => hold.promise,
  })
  serveConversation(openChat, { get: () => json(serverState({ version: 1 })) })
  const drawer = M.composerSearchToolsTarget(runConv, openChat)
  const main = M.composerSearchToolsTarget(undefined, openChat)
  await store.load(drawer)
  await store.load(main)
  store.toggle(drawer, 'web_search')
  assert.equal(store.hasPendingSave(drawer), true)
  assert.equal(store.hasPendingSave(main), false, 'the open chat can send at once')
  assert.equal(putsTo(openChat).length, 0)
  hold.resolve(json(serverState({ version: 2, enabled: ALL.filter(x => x !== 'web_search') })))
  assert.equal(await store.settle(drawer), true)
})

// ── usage labels ────────────────────────────────────────────────────────────

test('usage rows name documentation search, whichever key wrote them', () => {
  assert.equal(M.usageSourceTypeLabel('documents'), 'documentation search')
  assert.equal(M.usageSourceTypeLabel('userdocs'), 'documentation search')
  assert.equal(M.usageSourceTypeLabel('event_gate'), 'event filter')
  assert.equal(M.usageSourceTypeLabel('reasoning'), 'reasoning')
  assert.equal(M.usageSourceTypeLabel(null), '')
})
