// A `citations` frame reaches the chat store through the multiplexed
// profile-events stream.
//
// Per-conversation events travel inside `conversation-event` frames on the one
// shared /api/profile-events/stream connection. The multiplexer forwards them
// by conversation id without looking at their type, which is what lets the new
// `citations` type through with no change there — this pins that, so a future
// allow-list of event types cannot silently drop citation badges.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, load, until } from './harness.mjs'

const env = installBrowser()
const { subscribeConversation } = await load('src/services/profileEventsStream.ts')

function sse(...frames) {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      controller.close()
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`

test('a citations event is delivered to its conversation, and only to it', async () => {
  const citations = { v: 1, unverified: 0, items: [{ n: 1, token: '[ud:k7m2xq9a]', status: 'verified' }] }
  env.route('/api/profile-events/stream', () => sse(
    frame('ready', {}),
    frame('conversation-event', { conversation_id: 'c2', seq: 1, type: 'text', data: { token: 'other' } }),
    frame('conversation-event', {
      conversation_id: 'c1', seq: 7, type: 'citations', data: { citations, assistant_id: 'a1' },
    }),
  ))

  const got = []
  const handle = subscribeConversation('http://localhost:1515', 'jwt-alice', 'c1', event => got.push(event))
  try {
    await until(() => got.length > 0, 'the citations event')
  } finally {
    handle.close()
  }

  assert.deepEqual(got, [{ seq: 7, type: 'citations', data: { citations, assistant_id: 'a1' } }])
  assert.equal(env.callsTo('/api/profile-events/stream')[0].init.headers.Authorization, 'Bearer jwt-alice')
})
