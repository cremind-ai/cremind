// How User Document Search snapshots are folded in, and what they mean.
//
// Snapshots reach the page by several paths at once — the progress SSE, a 15 s
// status poll, the snapshot a settings save or a control action returns, the
// multiplexed stream's last frame replayed to a late subscriber, a cross-tab
// buffer — and nothing orders them in flight. The server stamps each one with
// (boot, seq): `seq` is one counter for the whole process, `boot` changes when
// the process does. These tests pin that a late frame can never roll the page
// back, that a restart is not mistaken for "old", and what the derived getters
// (active, attention, progress) and the transport choice say.
import assert from 'node:assert/strict'
import test from 'node:test'

import { load } from './harness.mjs'

const {
  EMPTY_STREAM_STATE, reduceSnapshot, isActive, needsAttention, failedCount, syncProgress,
  chooseTransport,
} = await load('src/stores/userDocsReducer.ts')

function snap(overrides = {}) {
  return { v: 1, boot: 'b1', seq: 1, ts: 1_000, enabled: true, state: 'idle', reason: null, ...overrides }
}

// ── ordering ────────────────────────────────────────────────────────────────

test('frames in order replace each other', () => {
  let s = reduceSnapshot(EMPTY_STREAM_STATE, snap({ seq: 1, state: 'scanning' }))
  s = reduceSnapshot(s, snap({ seq: 2, state: 'indexing' }))
  assert.equal(s.snapshot.state, 'indexing')
  assert.deepEqual(s.cursor, { boot: 'b1', seq: 2 })
})

test('a late replay of an older frame is ignored — the same state object comes back', () => {
  // A poll answered at seq 7, then the multiplexed stream hands a late
  // subscriber its last frame, seq 5: the page must not go back in time.
  const fresh = reduceSnapshot(EMPTY_STREAM_STATE, snap({ seq: 7, state: 'idle' }))
  const after = reduceSnapshot(fresh, snap({ seq: 5, state: 'indexing' }))
  assert.equal(after, fresh)
  assert.equal(after.snapshot.state, 'idle')
})

test('a duplicate (same seq) is ignored', () => {
  const s = reduceSnapshot(EMPTY_STREAM_STATE, snap({ seq: 3 }))
  assert.equal(reduceSnapshot(s, snap({ seq: 3, state: 'paused' })), s)
})

test('a new boot is taken even though its counter restarted', () => {
  // The server restarted: its first frame has seq 1, far below what we hold.
  const before = reduceSnapshot(EMPTY_STREAM_STATE, snap({ boot: 'old', seq: 900, state: 'indexing' }))
  const after = reduceSnapshot(before, snap({ boot: 'new', seq: 1, state: 'idle' }))
  assert.equal(after.snapshot.state, 'idle')
  assert.deepEqual(after.cursor, { boot: 'new', seq: 1 })
  // ...and from then on, the new process's own order applies.
  assert.equal(reduceSnapshot(after, snap({ boot: 'new', seq: 1, state: 'scanning' })), after)
})

test('a new boot is taken even when its clock reads earlier', () => {
  // Ordering by wall clock across a restart would freeze the page for good
  // whenever the clock was set back; boot/seq never compares clocks.
  const before = reduceSnapshot(EMPTY_STREAM_STATE, snap({ boot: 'a', seq: 4, ts: 2_000_000 }))
  const after = reduceSnapshot(before, snap({ boot: 'b', seq: 1, ts: 1_000 }))
  assert.equal(after.snapshot.boot, 'b')
})

test('an unstamped fallback frame is shown but does not reset the ordering', () => {
  // The server sends {v, enabled, state: 'unknown'} when it cannot build a
  // snapshot. It is the truth for now — but a stale stamped frame behind it
  // must still be refused.
  const held = reduceSnapshot(EMPTY_STREAM_STATE, snap({ seq: 10, state: 'indexing' }))
  const unknown = reduceSnapshot(held, { v: 1, enabled: false, state: 'unknown' })
  assert.equal(unknown.snapshot.state, 'unknown')
  assert.deepEqual(unknown.cursor, { boot: 'b1', seq: 10 })
  assert.equal(reduceSnapshot(unknown, snap({ seq: 9, state: 'idle' })), unknown)
  assert.equal(reduceSnapshot(unknown, snap({ seq: 11, state: 'idle' })).snapshot.state, 'idle')
})

test('junk is ignored', () => {
  assert.equal(reduceSnapshot(EMPTY_STREAM_STATE, null), EMPTY_STREAM_STATE)
  assert.equal(reduceSnapshot(EMPTY_STREAM_STATE, { v: 1 }), EMPTY_STREAM_STATE)
})

// ── active / attention ──────────────────────────────────────────────────────

test('work under way is "active"; waiting, holding and idling are not', () => {
  for (const state of ['estimating', 'scanning', 'indexing', 'reembedding']) {
    assert.equal(isActive(snap({ state })), true, state)
  }
  for (const state of ['idle', 'disabled', 'suspended', 'hold', 'awaiting_confirmation', 'paused']) {
    assert.equal(isActive(snap({ state })), false, state)
  }
  assert.equal(isActive(null), false)
})

test('attention: confirmations, holds, storage pauses and failed files', () => {
  assert.equal(needsAttention(snap({ state: 'awaiting_confirmation', reason: 'mass_delete' })), true)
  assert.equal(needsAttention(snap({ state: 'hold', reason: 'root_unavailable' })), true)
  for (const reason of ['budget', 'disk_low', 'disk_critical']) {
    assert.equal(needsAttention(snap({ state: 'paused', reason })), true, reason)
  }
  // The user's own pause is a choice, not a problem.
  assert.equal(needsAttention(snap({ state: 'paused', reason: 'user' })), false)
  assert.equal(needsAttention(snap({ state: 'idle', stages: { indexed: 10, error: 2 } })), true)
  assert.equal(needsAttention(snap({ state: 'idle', stages: { indexed: 10 } })), false)
  // An admin suspension is not something the profile can act on.
  assert.equal(needsAttention(snap({ state: 'suspended', reason: 'admin_gate' })), false)
})

test('the failed count comes from the status counts, else the preview', () => {
  assert.equal(failedCount(snap({ stages: { error: 12 }, failed_preview: [{}, {}] })), 12)
  assert.equal(failedCount(snap({ failed_preview: [{}, {}] })), 2)
  assert.equal(failedCount(null), 0)
})

// ── progress ────────────────────────────────────────────────────────────────

test('an open batch drives progress and the ETA', () => {
  const p = syncProgress(snap({
    state: 'indexing',
    batch: { label: 'first sync', total: 12840, done: 3120, failed: 0, skipped: 0, eta_s: 720 },
    stages: { dirty: 1, indexed: 5 },
  }))
  assert.equal(p.source, 'batch')
  assert.equal(p.done, 3120)
  assert.equal(p.total, 12840)
  assert.equal(p.etaS, 720)
  assert.ok(Math.abs(p.pct - 24.3) < 0.1)
})

test('without a batch, progress is read off the status counts', () => {
  // Tombstones (deletes in their grace window) and missing rows (a held mass
  // deletion) are not part of the index for progress purposes.
  const p = syncProgress(snap({
    state: 'indexing',
    batch: { label: null, total: 0, done: 0, failed: 0, skipped: 0 },
    stages: { indexed: 70, metadata_only: 5, error: 5, dirty: 20, tombstone: 50, missing: 9 },
  }))
  assert.equal(p.source, 'stages')
  assert.equal(p.total, 100)
  assert.equal(p.done, 80)
  assert.equal(p.pct, 80)
  assert.equal(p.failed, 5)
  assert.equal(p.etaS, null)
})

test('a re-embed reports passages', () => {
  const p = syncProgress(snap({ state: 'reembedding', reembed: { done: 450, total: 1000 } }))
  assert.equal(p.source, 'reembed')
  assert.equal(p.unit, 'chunks')
  assert.equal(p.pct, 45)
})

test('nothing to measure is null, not 0%', () => {
  assert.equal(syncProgress(snap({ state: 'disabled' })).pct, null)
  assert.equal(syncProgress(null).pct, null)
})

// ── transport ───────────────────────────────────────────────────────────────

const base = { hasToken: true, profileRoute: true, chatRoute: false, pageMounted: false, enabled: true }

test('My Documents streams, whatever the feature state', () => {
  assert.equal(chooseTransport({ ...base, pageMounted: true, enabled: false }), 'page-stream')
  // Even on top of a chat route (both mounted in a transition).
  assert.equal(chooseTransport({ ...base, pageMounted: true, chatRoute: true }), 'page-stream')
})

test('chat routes ride profile-events, which they hold anyway', () => {
  assert.equal(chooseTransport({ ...base, chatRoute: true, enabled: false }), 'profile-events')
})

test('other pages poll only while the feature is on (or not yet known)', () => {
  assert.equal(chooseTransport(base), 'poll')
  assert.equal(chooseTransport({ ...base, enabled: null }), 'poll')
  assert.equal(chooseTransport({ ...base, enabled: false }), 'none')
})

test('no token, or not a profile page, opens nothing', () => {
  assert.equal(chooseTransport({ ...base, hasToken: false, pageMounted: true }), 'none')
  assert.equal(chooseTransport({ ...base, profileRoute: false, chatRoute: true }), 'none')
})
